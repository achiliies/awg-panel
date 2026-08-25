"""Assemble the Client shape the API serves out of the three places it lives.

A client is spread across three stores, each of which is authoritative for a
different thing and none of which can be trusted to know about the others:

* the server config and clients/<name>.conf - name, key, address, scope, whether
  the "# Disabled" marker is set. Read here through ClientIndex, which
  apps.clients.index keeps level with those files and which is what makes a page
  of clients a query rather than a scan.
* traffic.db - all-time counters, written by the collector and by the PreDown
  hook. Also reached through the index, for the same reason: sorting by usage
  has to be a column.
* the panel database - quota, expiry, contact details, usage offsets, why a
  peer was switched off, and the last handshake and endpoint the collector saw.
  Joined to, never copied, so an edit shows on the next request.

plus live.json, which the collector rewrites every couple of seconds with the
last `awg show dump` it took. That file is a cache by definition: if the
collector is not running it goes stale, and rates read out of a stale blob would
show traffic that stopped minutes ago. So its age is checked and the rates are
dropped when it is old, while the handshake timestamps in it stay usable because
they are absolute. It is also the one source still read whole on every request -
one file, one parse, however many clients the server holds.

The dump is also the only place a handshake exists at all, and the kernel drops
every one of them when the interface goes down - a reboot, a settings change, a
restore from backup. So the last one is read back out of the database whenever
the live dump has none, which is what makes "last seen" and "connected from"
outlive a restart. Only the display falls back: whether a peer is online right
now is decided on the live value alone.

Everything a merge produces is derived here rather than stored: `online`,
`status`, `lastSeen`, `quotaPercent` and the all-time totals are functions of
the four sources above, and storing any of them would create a fifth thing that
can be wrong. What ClientIndex holds is never one of them - it copies what a file says
and nothing that is worked out from what a file says.

The list comes back oldest client first. Every caller wants an order and only
one of them can have an opinion about which peer the config happens to list
second, so the answer is settled once, in the column the index is ordered by, on
the one property of a client that never changes after it is created.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Greatest

from apps.panel import settings_store
from awg import clientsenv, store, subnet6, traffic
from awg.conf import parse_client_conf
from awg.errors import AwgError, NotFound
from awg.paths import client_dir, live_state_file

from . import index
from .models import ClientIndex, ClientMeta

log = logging.getLogger(__name__)

# live.json is written every trafficPollSec. A few missed cycles is a busy box;
# beyond that the collector is down and its rates are fiction.
STALE_CYCLES = 5
MIN_STALE_SEC = 15

# How long after the last evidence of a peer it stops being "idle" and becomes
# "offline". Idle is the moment a device might be changing networks, not a state
# a client sits in: a phone moving from wifi to mobile data is back inside a few
# seconds, and anything that has not come back by now is gone.
#
# This used to be a day, which meant a client that disconnected could not read
# as offline until the next one - the panel had three words for a peer's state
# and only ever used two of them.
IDLE_WINDOW_SEC = 90

# A connected client sends a keepalive every PersistentKeepalive seconds, so the
# packets arriving are the fastest honest answer to "is it there". Two intervals
# of silence is the tightest this can be drawn: it allows exactly one lost
# keepalive, and a single dropped packet is a thing that happens to a phone on a
# train rather than a client that has gone.
LIVENESS_FACTOR = 2

# ...but never less than this, so a hand-edited keepalive of a second or two
# cannot turn ordinary jitter into a disconnection.
MIN_LIVENESS_SEC = 30

DEFAULT_ONLINE_THRESHOLD = 180
DEFAULT_POLL_SEC = 2

# What `awg` prints for an endpoint that is not set. It reaches live.json
# whenever the collector passes the dump through without cleaning it.
NONE_ENDPOINT = "(none)"

# Values of ClientMeta.disabled_reason that mean something was enforced rather
# than chosen. Everything else, including a peer disabled from the CLI, is
# reported as "manual".
ENFORCED_REASONS = ("quota", "expired")


# The status words the list can be narrowed to, and the sort keys it can be
# ordered by. Both sets are the table's, not the database's: a client is
# selected here by the same word the status column shows it under, so a row the
# operator can see under "Online" is a row "Online" returns.
STATUS_FILTERS = ("all", "active", "online", "offline", "disabled", "expired")
SORT_KEYS = ("created", "name", "ip", "usage", "lastHandshake")

DEFAULT_PAGE_SIZE = 50
# A bound on the JSON handed to a browser, and now on the work behind it too: the
# rows that survive the page are the only ones built, so this is the number of
# dictionaries a request makes as well as the number it sends. It bounds a
# *number* of rows, and nothing that asks for a number needs more than this: a
# caller wanting nine hundred is walking a list, and walking it five hundred at
# a time is the same walk in two steps.
MAX_PAGE_SIZE = 500

# The page size that means "no page": every row the query selected, however many
# that is.
#
# It is a separate value rather than a big number because it is a separate
# request. MAX_PAGE_SIZE bounds a caller who named a size, and the panel's own
# "All" is a caller who declined to - it does not know the count when it asks,
# and it cannot, because the count is in the answer. Serving that by capping it
# at five hundred was the wrong answer twice over: an operator with 1072 clients
# picked All and got three pages, and the word on the control was a lie about
# what the control did.
#
# What it costs is honest and it is the caller's to accept: a row is a dict
# built here and a line of JSON on the wire, so this is a request proportional
# to the server, which is exactly what every other bound in this module exists
# to avoid. The panel warns before it draws one. Nothing else asks for it -
# every internal caller names a size or takes the default.
UNPAGED = 0


@dataclass(frozen=True)
class ClientQuery:
    """What GET api/v1/clients was asked for: which clients, in what order, which page.

    Every field has the value the list had before any of this existed - no
    search, no filter, oldest first, page one - so a caller that passes nothing
    gets the old answer, only shorter.
    """

    search: str = ""
    status: str = "all"
    sort: str = "created"
    descending: bool = False
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass
class ClientList:
    """Exactly the body of GET api/v1/clients.

    `clients` is one page. The counts beside it are about the whole server, and
    they are here because the page cannot be read without them: an operator
    needs to know how many rows their search actually found, the pagination
    needs `total` to know how many pages there are, and a sweep button promises
    a number that has to be counted over every client rather than over the
    twenty on screen - it deletes the clients a filter is hiding as readily as
    the ones in view.
    """

    clients: list[dict[str, Any]] = field(default_factory=list)
    subnet_cidr: str = ""
    free_ips: int = 0
    # Clients matching the search and the filter, before the page was cut.
    total: int = 0
    # Named clients on the server, whatever was asked for.
    total_all: int = 0
    # Of those, the ones `clients/remove-expired` would actually take.
    expired_count: int = 0
    # The two other numbers a bulk removal can promise: the clients that are
    # switched off - by an admin or by their own data limit - and the union of
    # those with the expired ones. The union is counted rather than added up,
    # because a client whose date has gone is very often switched off as well
    # and adding the two would count it twice. See _sweep_counts.
    disabled_count: int = 0
    expired_or_disabled_count: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


class Live:
    """The collector's last dump, and the clock a set of rows is read against.

    One of these is made per request and handed to every row built from it, so
    that a page of fifty clients is fifty readings taken at one instant against
    one blob. It costs a file read and a JSON parse however many clients the
    server holds, which is why it stayed on the request path when the config
    reading came off it: the blob is the one source here that is genuinely a
    single file.
    """

    __slots__ = ("peers", "fresh", "now", "threshold", "liveness", "has_ipv6")

    def __init__(self, has_ipv6: bool = False) -> None:
        blob, self.fresh = _read_live()
        peers = blob.get("peers")
        self.peers: dict[str, Any] = peers if isinstance(peers, dict) else {}
        self.now = time.time()
        self.threshold = settings_store.get_int("onlineThresholdSec", DEFAULT_ONLINE_THRESHOLD)
        # The keepalive every client config is issued with, which is what makes a
        # peer's silence mean something. Read per request, like the blob itself:
        # an admin who changes it should not have to restart anything.
        self.liveness = liveness_window(
            _keepalive(), settings_store.get_int("trafficPollSec", DEFAULT_POLL_SEC)
        )
        self.has_ipv6 = has_ipv6

    def online_keys(self) -> set[str]:
        """The peers the dump says are connected right now.

        Asked for only by the two status filters that no column can answer. A
        disabled peer is never in the dump - the kernel is not given one - so it
        cannot appear here, which is the right answer for both of them.
        """
        return {
            key
            for key, peer in self.peers.items()
            if isinstance(peer, dict)
            and _is_online(
                peer, _int(peer.get("handshake")), self.now, self.threshold, self.liveness
            )
        }


def merge_clients(query: ClientQuery | None = None) -> ClientList:
    """One page of clients, chosen by the database, with the counts the page needs.

    The selection is a query now, and used to be a scan. Every client on the
    server was built into a dictionary before the page was cut - which meant a
    config parse, a client config file opened per client, and a sort over the
    lot - and then all but fifty were thrown away. About half a second on four
    thousand clients, on an endpoint every open tab polls, growing with the
    server rather than with the answer.

    So filtering, searching, ordering and counting happen in SQL against
    ClientIndex, which apps.clients.index keeps level with the files, and only
    the rows that survive the page are merged with the live dump and turned into
    dictionaries. The one thing still proportional to the server is intersecting
    with that dump for the two connectivity filters, which is bounded and costs
    milliseconds - see _selected.

    The counts are still counts over the whole server, because that is what they
    mean: an operator needs to know how many rows their search found, the
    pagination needs a total to know how many pages there are, and a sweep
    button promises a number counted over every client rather than over the
    twenty on screen. They are aggregates now instead of passes over a list.
    """
    query = query or ClientQuery()
    state = index.current()
    live = Live(state.has_ipv6)

    selected = _selected(query, live)
    total = len(selected) if isinstance(selected, list) else selected.count()
    page, start = _page_bounds(query, total)
    window = (
        selected[:] if query.page_size == UNPAGED else selected[start : start + query.page_size]
    )
    sweep = _sweep_counts(live.now)

    return ClientList(
        clients=[_row(entry, live) for entry in _entries(window)],
        subnet_cidr=state.subnet_cidr,
        free_ips=state.free_ips,
        total=total,
        total_all=ClientIndex.objects.count(),
        expired_count=sweep["expired"],
        disabled_count=sweep["disabled"],
        expired_or_disabled_count=sweep["either"],
        page=page,
        page_size=query.page_size,
    )


def _sweep_counts(now: float) -> dict[str, int]:
    """How many clients a bulk removal would take, for each way of asking.

    Three numbers, because the two sets overlap and the caller has to be able to
    put a figure on any of the three selections the panel offers: the expired,
    the switched-off, and both together. A client whose date has gone is usually
    switched off as well - by the collector, for that very date - so adding the
    first two would count it twice and promise a removal of more clients than
    the server holds.

    One aggregate rather than three counts. Each of these is a scan of every
    client on the server joined to its metadata, and this runs on the endpoint
    every open tab polls; asking for them together is one pass instead of three.
    Which is also why the expired count is now taken here rather than beside the
    others - it is the same scan, and it was already paying for one.

    The conditions are the same Q objects the client list filters by, so the
    number on the button and the rows behind the matching filter cannot drift
    apart: `sweepable_q` is what the sweep acts on, and OFF is what the table
    calls disabled.
    """
    expired = sweepable_q(now)
    return ClientIndex.objects.annotate(used=USED).aggregate(
        expired=Count("id", filter=expired),
        disabled=Count("id", filter=OFF),
        either=Count("id", filter=expired | OFF),
    )


def _selected(query: ClientQuery, live: Live) -> Any:
    """The clients matching this query, in order, as something that can be sliced.

    A queryset for every filter the database can settle on its own, which is all
    of them but two. Whether a peer is connected is the live dump's answer and
    the dump is a JSON file, so "online" and "offline" are settled by walking an
    ordered list of public keys and testing each against it. That is one column
    out of the database and a dictionary lookup per client - a few milliseconds
    at four thousand - rather than the row-per-client construction that used to
    stand behind every filter.

    The list walked is already the working clients and only those: _status_q
    narrows both words to what a column can say about them before the dump is
    opened, so what is decided here is only the half that needs it.

    Both forms support len() and a slice, which is the whole of what the caller
    needs from them.
    """
    base = _queryset(query, live.now)
    if query.status not in ("online", "offline"):
        return base

    online = live.online_keys()
    wanted = query.status == "online"
    return [
        key for key in base.values_list("meta__public_key", flat=True) if (key in online) is wanted
    ]


def totals() -> dict[str, int]:
    """The four client figures on the dashboard cards, as four aggregates.

    These used to be sums over a freshly merged list of every client on the
    server - the whole of the client list's work, for four numbers, on an
    endpoint the dashboard polls every two seconds rather than every thirty. It
    was the most expensive thing the panel did and the least of what it showed.

    `online` is the one that stays a scan of the dump, and it has to be the same
    scan the online filter and every row's lamp use. The dump carries a count of
    its own that the collector wrote, and taking it would be cheaper and
    occasionally wrong by a client or two - it was counted against the clock of
    the collector's last cycle, not this request's. An operator who counts the
    green dots on the table has to get the number on the card, so both come from
    online_keys and neither is derived twice.

    Counting that scan on its own is not the same number, though, and that is
    what working_keys is for: the dump holds peers the table has no row for and
    peers it has a different word for, and either can make the card read three of
    two. The keys it returns are one column of every client, which is the same
    shape the online filter already pays for and the same reason - a set of keys
    is what the blob can be intersected with.
    """
    index.current()
    live = Live()
    online = live.online_keys()
    totals = ClientIndex.objects.aggregate(
        clients=Count("id"),
        disabled=Count("id", filter=Q(enabled=False)),
        rx=Sum(Greatest(F("cum_rx") - F("meta__offset_rx"), Value(0))),
        tx=Sum(Greatest(F("cum_tx") - F("meta__offset_tx"), Value(0))),
    )
    return {
        "total_clients": totals["clients"] or 0,
        "online_clients": sum(1 for key in working_keys() if key in online),
        "disabled_clients": totals["disabled"] or 0,
        "total_rx": totals["rx"] or 0,
        "total_tx": totals["tx"] or 0,
    }


def _entries(window: Any) -> list[ClientIndex]:
    """The index rows for one page, with their metadata, in the order asked for.

    A slice of the queryset arrives ordered and already carrying its metadata -
    _queryset asked for it, and it has to, because every row built from this
    reads the joined columns and a page of fifty would otherwise be fifty more
    queries. Evaluating it is all there is to do.

    A page selected as public keys - which is what the two connectivity filters
    return - has to be fetched and then put back into the order it was chosen in,
    because `IN` says nothing about ordering.
    """
    if not isinstance(window, list):
        return list(window)
    rows = {
        row.meta.public_key: row
        for row in ClientIndex.objects.select_related("meta").filter(meta__public_key__in=window)
    }
    return [rows[key] for key in window if key in rows]


# ------------------------------------------------------------- the selection
#
# What the filters and sorts used to compute per row in Python, written once as
# expressions the database evaluates. Each is the same arithmetic as the function
# it replaced, and the pairing is deliberate: an operator who filters by a word
# has to get the rows that show that word, so the two definitions have to be
# provably the same one.

# All-time bytes as the panel counts them: the counters out of traffic.db, less
# the offsets an admin's "clear counter" left behind. Clamped at each half rather
# than at the sum, exactly as _row does it, so a counter cleared while traffic.db
# was rebuilt underneath cannot make usage go negative.
USED = Greatest(F("cum_rx") - F("meta__offset_rx"), Value(0)) + Greatest(
    F("cum_tx") - F("meta__offset_tx"), Value(0)
)

# Over its data limit, which is the one enforcement the status word says even
# when the collector has not got round to switching the peer off.
OVER_QUOTA = Q(meta__quota_bytes__gt=0) & Q(used__gte=F("meta__quota_bytes"))

# The two halves of the table's status column that are not about connectivity: a
# peer is "off" if the config says so or its usage does.
OFF = Q(enabled=False) | OVER_QUOTA


def working_keys() -> Any:
    """The public keys of the clients a connectivity word can be said about.

    Which is the same condition the two connectivity filters narrow to, read as
    keys rather than as rows, because the live dump is a dictionary and a set of
    keys is what it can be intersected with. Both callers exist and they have to
    agree - the card counts what the table would list - so the rule is here once
    and neither of them writes it out.

    The dump is not that set and cannot stand in for it. It holds a peer the
    panel has no row for at all, since a peer with no "# Client" comment cannot
    be listed and is still served by the kernel, and it holds a peer this
    excludes, since a client over its limit goes on transferring until the
    collector reaches it. Counting the dump alone is what made the dashboard
    able to report more clients online than the server has.

    Unordered, and explicitly so. ClientIndex orders by created_key and position
    by default, so leaving the ordering alone put a sort of every row on the
    server behind a set that is only ever asked whether it contains a key -
    SQLite cannot answer a three-term order from the one-column index, so the
    plan was a full scan into a temp b-tree. On four thousand clients that was
    two of the five milliseconds this took, for an order no caller can observe.
    """
    return (
        ClientIndex.objects.annotate(used=USED)
        .filter(~OFF)
        .order_by()
        .values_list("meta__public_key", flat=True)
    )


def expired_q(now: float) -> Q:
    """Clients whose date has passed - the date, and nothing else.

    Not the status word, which never says "expired" for any client: a lapsed
    client reads as disabled if the collector switched it off and as online if an
    admin has since switched it back on, and neither of those is about the date.
    """
    return Q(meta__expires_at__isnull=False) & Q(meta__expires_at__lte=_moment(now))


def sweepable_q(now: float) -> Q:
    """The lapsed clients `clients/remove-expired` would actually delete.

    One short of every expired client, and the same rule the sweep itself
    applies: a client an admin has switched back on since its date went is one
    the sweep refuses to take, so counting it would put a number on the button
    that the server then declines to act on.

    An override is tied to the exact date it forgives, which is why this compares
    the two columns rather than testing a flag - the same comparison
    ClientMeta.expiry_overridden makes, in the one form a query can use.
    """
    return expired_q(now) & ~Q(meta__expiry_override_at=F("meta__expires_at"))


# ------------------------------------------------------------------ the verdict


def verdict(meta: ClientMeta, used: int, now: datetime) -> str:
    """Why this client should be switched off right now, or "" if it should be on.

    The one definition of enforcement, because two things act on it and they have
    to agree. The collector applies it on a schedule, against every client; a
    request applies it to the single client an admin has just edited, so that
    raising a limit or moving a date takes effect while they are still looking at
    the screen rather than at the next pass. Two copies of this rule would be two
    answers to "should this peer be on", and the visible symptom would be a panel
    that switches a client on and the collector switching it back off.

    Expiry is checked first: a client past its date stays off whatever its usage
    says, and reporting "quota" for it would send the admin to raise a limit that
    is not the problem.

    Unless the admin has already answered for this date. An override says the
    expiry has been considered; it forgives that one date and nothing else, so
    moving the date brings enforcement straight back. Quota is still enforced
    underneath it - the decision that was made was about the calendar.

    `used` is passed rather than computed because its two callers have it from
    different places: the collector holds the counters in memory, and a request
    reads them out of traffic.db. Both subtract the same offsets first.
    """
    if meta.expires_at is not None and meta.expires_at <= now and not meta.expiry_overridden:
        return "expired"
    if meta.quota_bytes > 0 and used >= meta.quota_bytes:
        return "quota"
    return ""


def used_bytes(meta: ClientMeta, counters: traffic.Counters | None) -> int:
    """All-time bytes as the panel counts them: traffic.db, less the panel's offsets.

    Clamped at each half rather than at the sum, the same way _row does it, so a
    counter cleared while traffic.db was rebuilt underneath cannot make usage
    come out negative.
    """
    if counters is None:
        return 0
    return max(counters.cum_rx - meta.offset_rx, 0) + max(counters.cum_tx - meta.offset_tx, 0)


def _queryset(query: ClientQuery, now: float) -> Any:
    """Everything matching the search and the status filter, in the order asked for."""
    rows = ClientIndex.objects.select_related("meta").annotate(used=USED)
    needle = query.search.strip()
    if needle:
        rows = rows.filter(_search_q(needle))
    return rows.filter(_status_q(query.status, now)).order_by(*_order(query.sort, query.descending))


def _search_q(needle: str) -> Q:
    """The same five fields the table searched when it did this in the browser.

    They are what an admin knows about a client they are trying to find.
    Substring, not prefix, because half a note or the middle of an address is how
    the box is actually used - which also means no index can serve it, and the
    database scans. That is fine and worth saying: a scan of one column across
    four thousand rows is a fraction of a millisecond, where the same search used
    to require every row to have been built first.

    `endpoint` is the stored one rather than the live dump's. The two agree
    except in the seconds after a client moves networks, the collector writes the
    new one as it sees it, and searching a JSON blob is not something a query can
    do.
    """
    return (
        Q(name__icontains=needle)
        | Q(ip__icontains=needle)
        | Q(meta__note__icontains=needle)
        | Q(meta__email__icontains=needle)
        | Q(meta__last_endpoint__icontains=needle)
    )


def _status_q(status: str, now: float) -> Q:
    """The status filter as a condition, and never more than half of one.

    Read off the same facts the row's own status word is read off, so a client
    the table shows as disabled is a client "disabled" returns. "expired" is the
    one that does not read that word - it reads the date, which is a separate
    question a client can answer yes to while also being online, and it is the
    question the sweep goes by.

    "online" and "offline" are the two no column can finish, because whether a
    peer is connected is the live dump's answer; _selected consults it. But the
    half that *is* a column belongs here, and it is the same half for both of
    them: neither word is ever said about a client that is not working, so a
    disabled or over-quota peer is excluded before the dump is asked about it.
    Leaving that out is what let "offline" answer with every peer the kernel has
    never been given - which is every disabled client on the server, under a word
    the table reserves for a client that is switched on and not there.
    """
    if status == "disabled":
        return OFF
    if status in ("active", "online", "offline"):
        return ~OFF
    if status == "expired":
        return expired_q(now)
    return Q()


def _order(sort: str, descending: bool) -> tuple[str, ...]:
    """The ordering for one of the columns the table can be sorted on.

    Every key but "created" ties on the order the clients were added, which ties
    in turn on the order the config lists them in. That is what the in-memory
    sort got from Python's sort being stable, and it is what stops two clients
    with equal usage, or a pair that have both never handshaken, from swapping
    places between one poll and the next.

    "created" is the exception, because it *is* that order: reversing it reverses
    the whole thing, ties included, which is what reversing the list used to do.

    "lastHandshake" orders on the collector's stored copy rather than on the live
    dump, which the row itself prefers wherever it has one. The two differ for at
    most one flush - the collector writes a handshake to the database as it sees
    it move - so a client that has just reconnected can sort as though it had not
    for a few seconds while its own row already says it has. Ordering on the live
    value instead would mean reading every client to place fifty, which is the
    whole of what this change was for; the alternative was thought about and cost
    more than the seconds it saves.

    It is also the handshake rather than `lastSeen` that this sorts on, though
    the column above it shows the latter, and that is deliberate: the receive
    clock lives only in the collector's blob and has no database column to order
    by. The two can disagree by the couple of minutes between one rekey and the
    next, which does not reach what this sort is for - finding the clients
    nobody has used for days - and buying the exactness would mean storing a
    timestamp that changes every twenty-five seconds, per client, forever.
    """
    if sort == "created":
        return ("-created_key", "-position") if descending else ("created_key", "position")
    column = {
        "name": "name_key",
        "ip": "ip_order",
        "usage": "used",
        "lastHandshake": "meta__last_handshake",
    }[sort]
    return (f"-{column}" if descending else column, "created_key", "position")


def _moment(now: float) -> datetime:
    """A clock reading as the aware UTC datetime the expiry columns are compared against."""
    # (datetime.UTC reads better but landed in 3.11; pyproject still supports the
    # 3.10 that Ubuntu 22.04 ships.)
    return datetime.fromtimestamp(now, tz=dt_timezone.utc)  # noqa: UP017


def _page_bounds(query: ClientQuery, total: int) -> tuple[int, int]:
    """The page actually being served, and the row it starts at.

    A page past the end is answered with the last one that has rows rather than
    with an empty list. Deleting the only client on page nine is the ordinary
    way to end up there, and an operator who does it should see page eight, not
    an empty table with no hint that the rest of the list is still where it was.

    An unpaged request is page one of one, whatever page it asked for. There is
    no arithmetic to do and the division below would be by zero, but the reason
    to answer rather than raise is that `?pageSize=0&page=4` is a bookmark of a
    view that has since been asked for whole - and every other stale thing in
    that query string is answered rather than rejected.
    """
    if query.page_size == UNPAGED:
        return 1, 0
    pages = max(1, -(-total // query.page_size))
    page = min(max(query.page, 1), pages)
    return page, (page - 1) * query.page_size


def merge_client(name: str) -> dict[str, Any]:
    """One client by name, in the same shape as a list row, plus the fields only it carries.

    A lookup rather than a search. This used to build every row on the server and
    walk them for the one with a matching name - deliberately, because a second
    row-building path is a second place for `online` or `quotaPercent` to be
    computed differently, and one config parse was thought a fair price for
    keeping the merge in one piece. The merge is still in one piece; `_row` is
    still the only thing that builds one. What has gone is the need to build four
    thousand of them to reach the fifty-first.

    `dns` is added here and nowhere else, which is the one place this shape is
    wider than a row - see _client_dns for why it cannot be on one.
    """
    # First, so that a client added over SSH a second ago is found rather than
    # 404'd: the config has moved, the stamp says so, and the rebuild happens
    # before the lookup that would otherwise miss it.
    state = index.current()
    entry = ClientIndex.objects.select_related("meta").annotate(used=USED).filter(name=name).first()
    if entry is None:
        raise NotFound(f"there is no client called '{name}'.")
    return {**_row(entry, Live(state.has_ipv6)), "dns": _client_dns(name)}


def parse_created(value: str | None) -> datetime | None:
    """A "# Created" comment as an aware UTC datetime, or None if it is not one.

    Only the exact format the panel writes is accepted.
    Anything else is somebody's hand edit: it is still shown in the client list
    verbatim, because the file is the truth about itself, but it is not copied
    into a database column that outlives the file and would then be quoted as
    though the panel had written it.
    """
    if not value:
        return None
    try:
        moment = datetime.strptime(value.strip(), store.CREATED_FMT)
    except (TypeError, ValueError):
        return None
    # (datetime.UTC reads better but landed in 3.11; pyproject still supports
    # the 3.10 that Ubuntu 22.04 ships.)
    return moment.replace(tzinfo=dt_timezone.utc)  # noqa: UP017


# ------------------------------------------------------------------ one row


def leaks_ipv6(allowed_ips: str, server_has_ipv6: bool) -> bool:
    """Is this client still sending its IPv6 around the tunnel instead of through it?

    True only for a client that asked for everything and is getting less: a
    config routing 0.0.0.0/0 and not ::/0, on a server that can carry IPv6.
    That client's own machine is dual-stack more often than not, and every
    address it reaches over IPv6 - which today is most large sites - goes out
    over its own connection, unencrypted, while the VPN reports itself up.

    A split tunnel is not leaking. It routes what somebody chose to route, and
    everything else was always meant to go around.

    Nothing is claimed for a server that carries no IPv6: those clients are not
    behind, the server is, and the client list has a different thing to say
    about that.
    """
    if not server_has_ipv6:
        return False
    return subnet6.needs_ipv6(allowed_ips)


def _row(entry: ClientIndex, live: Live) -> dict[str, Any]:
    """One client as the API serves it, out of its index row and the live dump.

    Still the only place a row is built, which is the point: `online`,
    `quotaPercent` and the status word are derived here and stored nowhere, so
    there is no second definition of any of them to drift.

    What arrives has already been chosen. The index row carries what the config
    files said when they were last read and the metadata row beside it carries
    what an admin set, both out of the query that picked this client; the live
    dump is consulted here and only here, for the peer this row is about.
    """
    meta = entry.meta
    peer = live.peers.get(meta.public_key)
    peer = peer if isinstance(peer, dict) else {}

    # traffic.db holds the true all-time totals and is never edited to satisfy a
    # reset. The offsets are the panel's own view of "since I last cleared this", so a
    # cleared counter cannot go negative if the db was rebuilt behind us.
    rx_bytes = max(entry.cum_rx - meta.offset_rx, 0)
    tx_bytes = max(entry.cum_tx - meta.offset_tx, 0)
    used = rx_bytes + tx_bytes

    now = live.now
    quota_bytes = meta.quota_bytes
    expires_at = meta.expires_at

    # Whether the peer is connected *now* is a question only the live blob can
    # answer, so it is asked before the stored fallback is folded in: a
    # handshake the collector recorded before the last reboot is a memory, not a
    # connection, and treating it as one would show a rebooted server full of
    # online clients.
    live_handshake = _int(peer.get("handshake"))
    online = entry.enabled and _is_online(peer, live_handshake, now, live.threshold, live.liveness)
    handshake = live_handshake or meta.last_handshake
    endpoint = _endpoint(peer) or meta.last_endpoint
    # The most recent moment anything was heard from this peer. Which of the two
    # is newer depends on the client: one that is only sending keepalives moves
    # its counter every 25 seconds and rekeys far more rarely.
    last_seen = max(handshake, _int(peer.get("lastRx")))

    return {
        "name": entry.name,
        "public_key": meta.public_key,
        "ip": entry.ip,
        "allowed_ips": entry.allowed_ips,
        "enabled": entry.enabled,
        "disabled_reason": _reason(entry.enabled, meta),
        # The config comment first, then the database copy, which is the
        # precedence `created_key` already resolved: the comment is the file's
        # statement about itself, and the copy is what answers after a hand edit
        # or a restore has taken it away.
        "created_at": entry.created_key or None,
        "expires_at": expires_at,
        # Not a state of its own, and deliberately not folded into either of the
        # two words beside it: it is why a client whose date has gone is still
        # answering, which is a thing the row would otherwise look self-
        # contradictory about.
        "expiry_overridden": meta.expiry_overridden,
        "quota_bytes": quota_bytes,
        # Straight off the joined metadata row, which every list row already
        # carries: these are not indexed, filtered on or sorted by, so they cost
        # nothing beyond the two columns in a query that was already being made.
        "down_bps": meta.down_bps,
        "up_bps": meta.up_bps,
        "email": meta.email,
        "note": meta.note,
        "online": online,
        # Two different questions, and the row answers both because the answers
        # are minutes apart. The handshake is when the keys were last exchanged,
        # which is what the kernel reports and what the sort runs on; `last_seen`
        # is when this peer was last heard from at all. Anything showing an
        # operator how long a client has been away wants the second one - the
        # first climbs to a couple of minutes on a client in active use, every
        # time, because that is simply how often WireGuard rekeys.
        "last_handshake": handshake,
        "last_seen": last_seen,
        "endpoint": endpoint,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        # What was taken off the two figures above, so that a reader holding the
        # live blob can work them out for itself.
        #
        # The blob carries the collector's own running totals, refreshed every
        # two seconds - the same numbers it enforces limits against, and up to
        # ten seconds ahead of the file these rows are read from. They are the
        # totals before any counter was cleared, though, and the record of what
        # was cleared is here rather than anywhere the collector can see it. So
        # it is sent: a page polling the blob can subtract these and show usage
        # on the blob's clock instead of on this endpoint's.
        "offset_rx": meta.offset_rx,
        "offset_tx": meta.offset_tx,
        # Rates come from the collector's last cycle and are the one thing in
        # this row that is meaningless once the file goes stale.
        "rate_rx": _int(peer.get("rateRx")) if live.fresh else 0,
        "rate_tx": _int(peer.get("rateTx")) if live.fresh else 0,
        "quota_used": used,
        "quota_percent": _quota_percent(used, quota_bytes),
        "status": _status(entry.enabled, meta, online, last_seen, used, quota_bytes, now),
        "ip6": entry.ip6,
        "leaks_ipv6": leaks_ipv6(entry.allowed_ips, live.has_ipv6),
    }


def _reason(enabled: bool, meta: ClientMeta) -> str:
    """Why this peer is off, with the config file having the last word.

    An admin who re-enabled a client by deleting its "# Disabled" marker by hand
    left a reason behind in the database that no longer applies, and one who
    added the marker the same way never wrote a reason at all. Neither should
    show up as a contradiction.
    """
    if enabled:
        return ""
    stored = meta.disabled_reason or ""
    return stored if stored in ENFORCED_REASONS else "manual"


def liveness_window(keepalive: str | int, poll: float = DEFAULT_POLL_SEC) -> int:
    """How long a peer's packets may stop before it counts as gone, or 0 for "cannot tell".

    Every config this project issues carries `PersistentKeepalive`, so a
    connected client is sending something on that interval whether or not
    anybody is using it. That makes silence meaningful, and it is the only
    signal that answers "has this client gone" in less time than a rekey.

    A server whose clients.env sets no keepalive has no such signal: a quiet
    client and a departed one look identical, and the handshake clock is all
    there is. Nothing is guessed in that case - this returns 0 and the caller
    falls back.

    `poll` is the collector's interval, and the window can never be tighter than
    a couple of those: the record of when a peer last sent something is only as
    current as the last time anything looked, so on a server polling every
    minute a connected client would otherwise read as silent for no reason but
    that nobody had checked.
    """
    try:
        seconds = int(str(keepalive).strip() or 0)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return max(seconds * LIVENESS_FACTOR, MIN_LIVENESS_SEC, int(poll) * LIVENESS_FACTOR)


def is_online(handshake: int, last_rx: int, now: float, threshold: int, liveness: int) -> bool:
    """Whether a peer is connected right now.

    Both the collector and the client list ask this, and they have to agree: the
    dashboard's "3 of 8 online" and the lamp on a row are the same claim, and an
    operator who counts the green dots must get the same number.

    The client's own packets answer first. `last_rx` is when its byte counter
    last moved, which for a keepalive client is never longer ago than the
    keepalive interval - so once it has been quiet for `liveness`, it is gone,
    and that is known about a minute after it happens rather than three.

    The handshake is the fallback, for a peer that has not yet been seen to send
    anything since the collector started and for servers with no keepalive to
    rely on. It is a much blunter instrument: the kernel only rekeys every couple
    of minutes, so a handshake stays fresh long after the client behind it has
    stopped.
    """
    if liveness > 0 and last_rx > 0:
        return (now - last_rx) <= liveness
    return handshake > 0 and (now - handshake) <= threshold


def connectivity(online: bool, last_seen: int, now: float) -> str:
    """The one word for whether a peer is there: online, idle or offline.

    The whole of the connectivity question, in the one function that answers it.
    The collector calls it so the browser does not have to: a page that derives
    this itself needs the thresholds, needs a clock to compare them against, and
    needs both to keep agreeing with a server it cannot see - which is three
    things to get wrong in a place where getting them wrong is invisible until
    somebody reports that their client never goes offline.

    Enforcement - disabled, over quota, expired - is deliberately not here. It is
    not a fact about the connection, it outranks one, and only the config and the
    database know it.
    """
    if online:
        return "online"
    if last_seen > 0 and (now - last_seen) <= IDLE_WINDOW_SEC:
        return "idle"
    return "offline"


def _is_online(
    peer: dict[str, Any], handshake: int, now: float, threshold: int, liveness: int
) -> bool:
    if handshake > 0 or _int(peer.get("lastRx")) > 0:
        return is_online(handshake, _int(peer.get("lastRx")), now, threshold, liveness)
    # No handshake in the blob at all is different from a handshake of zero:
    # an older collector may only have written the flag it computed itself.
    if "handshake" not in peer:
        return bool(peer.get("online"))
    return False


def _status(
    enabled: bool,
    meta: ClientMeta,
    online: bool,
    last_seen: int,
    used: int,
    quota_bytes: int,
    now: float,
) -> str:
    """The single word the UI shows in the status column.

    It answers one question - is this client working, and if it is, is it there -
    and it never answers a second one. "expired" is not among the words it can
    say, for any client, however long ago the date went. Whether a date has
    passed is a fact with a column of its own beside this one, in words that
    question deserves; saying it twice would cost the only place on the row that
    can tell an admin whether a dark client was switched off by hand or stopped
    by its data limit, and it would put this word in the position of contradicting
    the one next to it every time the two disagree - which, now that an admin can
    keep a lapsed client on, they legitimately do.

    So a client the collector switched off for its date reads as disabled, which
    is what it is; the date is one column to the right, `disabledReason` still
    carries the why for anything that wants it, and a lapsed client an admin has
    switched back on reads as whatever it is actually doing.

    Enforcement still wins over connectivity where the two describe the same
    thing: a client over its quota is over its quota whether or not the collector
    has got around to switching it off, and saying "online" for the minute in
    between would be a lie the admin then has to reconcile.

    Below that, three words about whether the peer is here, measured from the
    last moment anything arrived from it. "idle" is the few minutes in which a
    device that has just dropped might be changing networks and about to come
    back; past that it is simply not here, and the row says so.
    """
    if not enabled:
        return "quota" if meta.disabled_reason == "quota" else "disabled"
    if quota_bytes > 0 and used >= quota_bytes:
        return "quota"
    return connectivity(online, last_seen, now)


def _quota_percent(used: int, quota_bytes: int) -> int:
    """0 to 100, and 0 when there is no quota. Rounded down, so 100 means 100."""
    if quota_bytes <= 0:
        return 0
    return min(100, used * 100 // quota_bytes)


def _endpoint(peer: dict[str, Any]) -> str:
    value = str(peer.get("endpoint") or "").strip()
    return "" if value == NONE_ENDPOINT else value


def _client_dns(name: str | None) -> str:
    """The DNS line out of this client's own config, or "" if it has none.

    Read from the file rather than from clients.env: a client can be issued with
    a DNS server of its own, and the edit form has to show what it will change
    rather than what the server default happens to be.

    Called for one client at a time, deliberately. This is the only field in the
    whole merge that costs a file of its own - the config parse, the traffic read
    and the live read are one apiece however many clients there are, but this
    opens clients/<name>.conf, so putting it on a row made the client list cost
    one open and one parse per client on the server. A panel left on a four
    thousand client server did four thousand of them every thirty seconds, per
    tab, to draw fifty rows that have no DNS column - and the only thing that
    ever reads the field is the edit dialog, for the single client somebody just
    clicked. So it is fetched when that dialog opens, which is one file at the
    moment it is actually wanted.
    """
    if not name:
        return ""
    try:
        text = (client_dir() / f"{name}.conf").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return parse_client_conf(text).get("DNS", "")


# ---------------------------------------------------------------- the stores


def _keepalive() -> str:
    """The PersistentKeepalive clients.env hands every client, or "" if it cannot be read.

    Unreadable means no liveness signal rather than a default one: guessing 25
    here on a server that issues configs without a keepalive would call every
    quiet client disconnected.
    """
    try:
        return clientsenv.read_env().get("KEEPALIVE", "")
    except (AwgError, OSError) as exc:
        log.debug("cannot read the keepalive from clients.env: %s", exc)
        return ""


def _read_live() -> tuple[dict[str, Any], bool]:
    """The collector's blob and whether it is recent enough to quote rates from."""
    try:
        raw = live_state_file().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, False
    try:
        blob = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # The collector writes it atomically, so a broken file means something
        # else wrote there. Nothing in this module is worth failing a request
        # over; the panel simply has no live data.
        log.debug("live state at %s is not valid JSON", live_state_file())
        return {}, False
    if not isinstance(blob, dict):
        return {}, False

    poll = settings_store.get_int("trafficPollSec", DEFAULT_POLL_SEC)
    limit = max(MIN_STALE_SEC, poll * STALE_CYCLES)
    age = time.time() - _int(blob.get("ts"))
    return blob, 0 <= age <= limit


def _iso(value: Any) -> str | None:
    """A datetime as the RFC 3339 string the config comments use, or None."""
    if value is None:
        return None
    # datetime.UTC would read better, but it landed in 3.11 and pyproject still
    # supports the 3.10 that Ubuntu 22.04 ships.
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
