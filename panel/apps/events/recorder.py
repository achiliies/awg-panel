"""Writing the event log, and keeping it bounded.

One function does the writing, and its whole contract is that it cannot fail the
thing it is describing. An audit trail that can turn a working delete into a 500
is worse than no audit trail at all: the client is gone either way, and now the
operator has an error page saying otherwise. So every failure in here is a line
in the journal and nothing more - the row is lost, the action stands.

That is also why the call sites read the way they do. ``record`` is added beside
the ``log.info`` a handler already makes, after the work has succeeded and
usually as the last thing before the response is built, so an event exists only
for something that actually happened. Nothing calls it speculatively and nothing
calls it inside a lock or a transaction: it is one small insert into a table
nothing else reads on the request path.

``prune`` is the exception to the swallowing, and on purpose. It runs in the
collector's daily sweep, which has its own way of reporting a database that has
gone away and of reopening the connection afterwards, so a failure here is that
loop's to handle rather than something to hide from it.
"""

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.sessions import client_ip

from . import kinds
from .models import MAX_ROWS, RETENTION_DAYS, Event

log = logging.getLogger(__name__)

# The most named values one event may carry, and how long a string one may be.
#
# Both are guards rather than budgets: nothing the panel records comes close, and
# what they stop is a caller handing over something unbounded - a list of four
# thousand client names, an exception message with a config file in it - and
# turning a ledger row into a page of text. What is over the limit is cut, not
# refused, because a truncated detail beside a correct kind still says what
# happened.
MAX_DETAIL_KEYS = 8
MAX_DETAIL_TEXT = 120
MAX_DETAIL_ITEMS = 20


def record(
    kind: str,
    request: HttpRequest | None = None,
    *,
    target: str = "",
    actor: str = "",
    at: datetime | None = None,
    **detail: Any,
) -> None:
    """Write one event. Never raises, whatever is wrong with the database.

    ``request`` is what the actor and the address come from, and leaving it out
    is how the collector records the decisions it makes on nobody's behalf. An
    explicit ``actor`` overrides both, for the one case in between: a change made
    on behalf of an account by something that is not handling that account's
    request.

    ``at`` is for an event being recorded slightly after the fact, which is the
    collector's whole pattern - it decides which clients to switch off, switches
    them, and then writes a row each. The clock at the moment of the write is
    close enough for everything else and is the default.

    Everything else is the detail, passed as keyword arguments because that is
    how the call sites read best::

        record(kinds.CLIENT_BULK_DELETED, request, count=len(removed))

    Keys are single words on purpose. The detail travels to the browser as data
    rather than as fields, so nothing renames it on the way, and a key with an
    underscore in it would arrive in the one payload of the panel that is not
    camelCase.
    """
    try:
        Event.objects.create(
            at=at or timezone.now(),
            kind=kind,
            severity=kinds.severity_of(kind),
            actor=(actor or _actor_of(request))[:150],
            actor_ip=_address_of(request),
            target=(target or "")[:64],
            detail=_clean(detail),
        )
    except Exception as exc:
        # Blanket, and deliberately. The interesting failure is a DatabaseError,
        # but this runs after the work it describes is already done, and there is
        # nothing raised from here worth more than the action being reported as
        # having succeeded - which it did.
        log.warning("could not record the %s event: %s", kind, exc)


def record_many(
    kind: str,
    targets: Iterable[str],
    *,
    at: datetime | None = None,
    actor: str = "",
    **detail: Any,
) -> None:
    """The same event about several things at once, in one statement. Never raises.

    For the collector, which decides about the whole server in one pass: an
    admin who lowers a default quota puts every client over it at the same
    moment, and writing that a row at a time would be four thousand separate
    transactions in the middle of a poll loop.

    Every row carries the same moment, which is the honest one - they are one
    decision - and the same detail, because what differs between them is the
    target and nothing else. There is no request here by construction: this is
    the panel acting on its own, and the actor stays empty unless a caller has
    somebody to name.
    """
    rows = [
        Event(
            at=at or timezone.now(),
            kind=kind,
            severity=kinds.severity_of(kind),
            actor=actor[:150],
            actor_ip="",
            target=(target or "")[:64],
            detail=_clean(detail),
        )
        for target in targets
    ]
    if not rows:
        return
    try:
        Event.objects.bulk_create(rows, batch_size=500)
    except Exception as exc:
        log.warning("could not record %d %s event(s): %s", len(rows), kind, exc)


def prune(now: datetime | None = None) -> int:
    """Drop the events that have aged out, then whatever is left over the ceiling.

    The two limits are applied in that order because they mean different things.
    The window is the policy - events older than this are not worth keeping. The
    ceiling is a backstop against a flood, and it is only ever reached when the
    window has already failed to bound the table, so applying it second means it
    does nothing at all on a server where the window is enough.

    Returns how many rows went, for the collector's log line.
    """
    moment = now or timezone.now()
    removed, _ = Event.objects.filter(at__lt=moment - timedelta(days=RETENTION_DAYS)).delete()

    # The id of the newest row that is over the ceiling: everything at or below
    # it is one row too many. Read as a single value rather than counted first,
    # so the common case - a table under the ceiling, which is every ordinary
    # panel - is one query that comes back empty.
    over = list(Event.objects.order_by("-id").values_list("id", flat=True)[MAX_ROWS : MAX_ROWS + 1])
    if over:
        extra, _ = Event.objects.filter(id__lte=over[0]).delete()
        removed += extra
    return removed


def _actor_of(request: HttpRequest | None) -> str:
    """Who is making this request, or "" for the collector and for nobody.

    An API token answers first and answers with itself - "api:deploy" rather
    than the account it authenticates as. Both are true, and only one of them is
    worth writing down: every token on a panel authenticates as the same single
    account, so recording that account would make the log say "admin" for work
    nobody was sitting at a keyboard for. Read from ``request.auth``, which is
    where DRF leaves whatever authenticated the request, and via an attribute
    rather than an import so this module still knows nothing about that app's
    models.

    An anonymous caller is "" as well, which the UI draws the same way as the
    collector. That is not a hole: the only events an unauthenticated request can
    produce are a refused sign-in and a lockout, and both are about an address
    rather than an account - the name that was tried is in the detail, where it
    reads as a claim rather than as a fact about who did this.
    """
    named = getattr(getattr(request, "auth", None), "actor_name", "")
    if named:
        return str(named)
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    return user.get_username()


def _address_of(request: HttpRequest | None) -> str:
    """The address this came from, by the same rule the session list uses.

    X-Forwarded-For is honoured only where the operator has declared a proxy,
    because otherwise it is a header the client writes itself - and an audit
    trail that records whatever address an attacker typed is worse than one that
    records the proxy's.
    """
    if request is None:
        return ""
    try:
        return client_ip(request)
    except Exception:
        # A header the panel could not make sense of is not worth losing the row
        # over; "" is already this column's word for "could not be read".
        return ""


def _clean(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep the values that can survive a round trip through JSON, drop the rest.

    A whitelist rather than a serializer, and that is the point: what must never
    reach this table is a private key, a password or a whole request object, and
    the way to be sure of it is for the column to hold only the small named
    values a sentence is assembled from. Anything richer than a string, a number,
    a boolean or a flat list of those is dropped without comment - silently,
    because the alternative is an exception on the audit path, which the module
    docstring rules out.
    """
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if len(out) >= MAX_DETAIL_KEYS:
            break
        if isinstance(value, (list, tuple)):
            out[key] = [
                item
                for item in (_scalar(entry) for entry in list(value)[:MAX_DETAIL_ITEMS])
                if item is not None
            ]
            continue
        cleaned = _scalar(value)
        if cleaned is not None:
            out[key] = cleaned
    return out


def _scalar(value: Any) -> str | int | float | bool | None:
    """One detail value, or None for anything that has no business being stored."""
    # bool is a subclass of int and comes back unchanged, which is what the
    # browser wants: a flag stays a flag through JSON rather than becoming 1.
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_DETAIL_TEXT]
    return None
