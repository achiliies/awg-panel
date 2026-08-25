"""Statistics: the live blob, the dashboard's cards, and the one write.

Neither endpoint touches `awg`. The live blob is a file the collector wrote, and
today's figure is one row the collector maintains, so a dashboard left open all
day costs a file read every two seconds and nothing else. Anything that needs
the interface itself lives in the collector, which polls it once for every
consumer.

Every route here needs an authenticated session: DRF's defaults supply
SessionAuthentication plus IsAuthenticated. Three of the four are reads, so
CSRF has nothing to guard on them; the fourth is the wipe, which is a POST and
is checked like every other mutation in the panel.

Client state is never cached. The summary re-reads the config, traffic.db and
the metadata table through apps.clients.merge, which is the same code path the
client table uses, so the cards and the table can never disagree about how many
clients are online.
"""

import logging
from datetime import datetime
from typing import Any

from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clients import merge
from apps.clients.models import ClientMeta
from apps.events import kinds, recorder
from awg import sysinfo, traffic
from awg.controller import get_controller

from . import history, live
from .models import DailyTotal, TrafficReset, utc_day
from .serializers import (
    StatsSummarySerializer,
    TrafficHistorySerializer,
    TrafficResetSerializer,
)

log = logging.getLogger(__name__)

# The most peers one ?peers= request may name.
#
# Bounded by the request line, not by the page. This used to be
# apps.clients.merge.MAX_PAGE_SIZE, on the reasoning that a page of clients is
# why the parameter exists so the two could not disagree - but they did, and
# through a third limit neither of them mentioned. The keys travel in the URL,
# and a percent-encoded public key costs about 51 bytes, so the 500 that cap
# allowed was a 25 KB request line: refused by the web server long before this
# module saw it. The cap was unreachable and the number was fiction.
#
# So it comes from the transport instead. deploy/gunicorn.conf.py pins
# limit_request_line at 8190, which holds roughly 160 keys; 150 leaves the
# margin, and test_a_full_peers_query_fits_the_request_line keeps the two
# numbers honest if either moves - it asks a real gunicorn, because the Django
# test client has no request line for anything to be too long for. The panel's
# own page is 50, so this is three times what the UI asks for and the ceiling
# is only reachable by hand.
MAX_LIVE_PEERS = 150


class LiveStatsView(APIView):
    """GET api/v1/stats/live - the collector's last report, for the peers asked about.

    Deliberately not built from an `awg show dump` here. This is the most
    polled endpoint in the panel, and the whole point of the collector is that
    one process talks to the interface however many browsers are watching.
    A missing or stale file is not an error: the blob carries the timestamp it
    was written at, and the UI says so.

    `?peers=` is what keeps this endpoint the size of a screen rather than the
    size of the server. The blob holds every peer the kernel does - it has to,
    because the totals beside it are sums over all of them - but a browser
    drawing twenty rows needs twenty, and on a server with a few thousand
    clients the difference is most of a megabyte every two seconds per open
    tab. The aggregate block is always whole; only `peers` is narrowed.

    Absent, the parameter means every peer, which is what the endpoint has
    always answered and what an unaware caller still gets. Present and empty
    means none of them - the dashboard's case, which reads only the totals.

    `confStamp` is added here rather than read out of the file, because it is
    the one field that is not the collector's report: see live.conf_stamp for
    what it is for and why the server config alone. The response has never been
    the blob verbatim - `peers` is narrowed above - so this is the same seam.
    """

    def get(self, request: Request) -> Response:
        blob = live.read_live()
        wanted = request.query_params.get("peers")
        if wanted is not None:
            blob["peers"] = _pick_peers(blob["peers"], wanted)
        blob["confStamp"] = live.conf_stamp()
        return Response(blob)


def _pick_peers(peers: dict[str, Any], wanted: str) -> dict[str, Any]:
    """The named peers, in the blob's own spelling of them.

    A key that is not in the blob is left out rather than answered with an empty
    entry: "the collector is reporting nothing for this peer" is a state the UI
    already has a reading for, and inventing a zeroed peer would report a
    disabled client as connected-but-idle.

    Capped at MAX_LIVE_PEERS, so a hand-made request with ten thousand keys in
    it cannot turn a cheap endpoint into a long one. A request that long is
    refused by the web server first; this is the backstop for one that is not.
    """
    keys = [key for key in wanted.split(",") if key][:MAX_LIVE_PEERS]
    return {key: peers[key] for key in keys if key in peers}


class StatsSummaryView(APIView):
    """GET api/v1/stats/summary - the numbers on the dashboard cards."""

    def get(self, request: Request) -> Response:
        # Over every client, not a page of them: each of these is a total over
        # the server, and a card that counted only the clients the list happens
        # to be showing would report whatever the operator last scrolled to.
        # Aggregates rather than a merged list, because this is polled every two
        # seconds and building a row per client to add four numbers up was the
        # most expensive thing the panel did.
        today_rx, today_tx = today_bytes()
        result = {
            **merge.totals(),
            "today_rx": today_rx,
            "today_tx": today_tx,
            "uptime": int(sysinfo.uptime()),
            # Read now rather than taken from the blob: it is one stat() call,
            # and an interface that went down since the last collector cycle
            # should show as down immediately.
            "iface_up": get_controller().iface_up(),
        }
        return Response(StatsSummarySerializer(result).data)


class TrafficHistoryView(APIView):
    """GET api/v1/stats/traffic - what the whole server carried, by day and by month.

    The card this feeds sits beside the "today" card, and both read the same
    rows: today's figure is simply the last point of the daily series, so the two
    cannot disagree however long a dashboard is left open.

    Not polled on the live blob's clock, and it does not need to be. The
    collector rewrites today's row every ten seconds, so the newest bar in the
    series moves at most that fast - the panel reads this on the slow list clock
    and the bar is never more than half a minute behind what the card above it
    says. The rest of the series has not changed since midnight.

    `?from=` and `?to=` name a window, as YYYY-MM-DD, and either may be left
    out. Both series are cut to it, so one range control drives both tabs of the
    chart rather than the reader having to ask two differently shaped questions
    to compare a month against the days inside it. Left off entirely the answer
    is the two default windows, which is what the page opens on.

    Still one read of one-row-a-day, and still no pagination: apps.stats.history
    caps each series at what a chart can draw, so the widest thing this can
    answer is a few hundred rows either way.
    """

    def get(self, request: Request) -> Response:
        window = history.parse_window(request.query_params)
        return Response(TrafficHistorySerializer(history.server_series(window=window)).data)


class TrafficResetView(APIView):
    """POST api/v1/stats/traffic/reset - clear every traffic figure on the server.

    The server-wide sibling of `clients/<name>/reset-usage`, and it takes the
    one thing that endpoint deliberately leaves: the server's own days. A single
    client's reset is not a claim that the bytes never crossed the wire, so
    DailyTotal survives it; this is that claim, made about the whole server by
    somebody who was shown what it means first.

    What goes is every figure the panel can show: each client's all-time totals,
    each client's stored days and months, the server's stored days and months,
    and with them today's card and everything the charts draw. What is *not*
    touched is anything about the clients themselves - no key, address, quota,
    expiry or config is involved, and nothing disconnects.

    It is finished in two places, and that is the only complicated thing about
    it. Here the offsets are written, the tables are emptied and the wipe is
    recorded; the collector, on its next flush, subtracts those same offsets out
    of traffic.db and lets go of the running day it is holding in memory. See
    apps.stats.models.TrafficReset for why that split exists at all - the short
    version is that the collector's mirror of traffic.db would otherwise write
    the whole thing back within ten seconds.

    The order matters and is the reason nothing flickers in between. The offset
    stored against a client is the larger of what traffic.db says it has moved
    and what the collector's last report says, so from the moment this returns
    every reader - the list, the cards, the live blob the browser merges over
    them - subtracts a figure at least as large as the one it is reading and
    arrives at zero. When the collector folds, both halves go to zero together
    and the answer does not move. Bytes that cross the tunnel in between are
    counted, and should be: they moved after the wipe.

    A client switched off for its quota is switched back on by the collector's
    next enforcement pass, which finds nothing left to enforce. Not done here,
    though the single-client reset does do it: that one writes one client into
    the config, and this would write every one of them inside a request, where
    the collector already has a pass that does it off the request path.

    Open to an API token, unlike clearing the activity log. The line drawn there
    is about a token being able to erase what it did, and this cannot: the row
    it writes to the ledger is the record, and the ledger is not what this
    empties.
    """

    def post(self, request: Request) -> Response:
        now = timezone.now()
        # The two places a client's all-time total can be read, and the reason
        # both are: the file is written every ten seconds out of the memory the
        # blob is written from every two, so the blob is the fresher of the two
        # and the file is the one that survives the collector being stopped.
        # The larger is the later, which is the same rule the browser uses when
        # it merges these two sources over each other.
        counters = traffic.read_db()
        peers = live.read_live().get("peers") or {}

        rows = list(ClientMeta.objects.all())
        cleared = 0
        for row in rows:
            counter = counters.get(row.public_key)
            peer = peers.get(row.public_key)
            if merge.used_bytes(row, counter) > 0:
                cleared += 1
            row.offset_rx = max(counter.cum_rx if counter else 0, _reported(peer, "rx"))
            row.offset_tx = max(counter.cum_tx if counter else 0, _reported(peer, "tx"))

        with transaction.atomic():
            ClientMeta.objects.bulk_update(rows, ["offset_rx", "offset_tx"], batch_size=500)
            server_days, client_days = history.clear_everything()
            # Last, and inside the transaction with the rest: this row is what
            # asks the collector to finish the job, and a wipe that emptied the
            # tables without it would be undone by the next flush.
            TrafficReset.objects.update_or_create(
                pk=TrafficReset.ROW,
                defaults={"requested_at": now, "applied_at": None},
            )

        log.info(
            "traffic cleared for %d client(s): %d server day(s), %d client day(s)",
            cleared,
            server_days,
            client_days,
        )
        # `count` rather than `clients`, like every other bulk kind in the
        # ledger: it is the number the sentence is pluralised on, and a kind
        # that spelled it differently would be the one row in the log that could
        # not say "1 client" and "12 clients" from the same string.
        recorder.record(kinds.PANEL_TRAFFIC_CLEARED, request, count=cleared, days=server_days)
        result = {
            "clients": cleared,
            "server_days": server_days,
            "client_days": client_days,
        }
        return Response(TrafficResetSerializer(result).data)


def _reported(peer: Any, direction: str) -> int:
    """One direction of a peer's total out of the live blob, or zero.

    The blob is JSON written by another process and read here without a
    serializer, so a field that is missing, null or not a number at all is
    treated as nothing rather than trusted into an offset. It is only ever the
    larger half of a max() against traffic.db, so "nothing" costs the freshness
    and never the correctness.
    """
    if not isinstance(peer, dict):
        return 0
    value = peer.get(direction)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def today_bytes(now: datetime | None = None) -> tuple[int, int]:
    """Bytes transferred by every client since midnight UTC.

    One row, read by primary key. The collector keeps the running total in
    memory and writes it every ten seconds, so this is never more than that far
    behind - and never a scan, whatever the client count or how long the panel
    has been up.

    A day with no row is a day nothing has moved on yet, which is zero rather
    than an error: the row is created by the first flush after the first byte,
    so a freshly installed panel and a quiet night look the same and both are
    reported honestly.

    UTC, like every other stored moment in the panel. Which day that is will not
    match the calendar on the wall of an admin far enough east or west, and that
    is the trade for a figure two admins in two countries can compare without
    first agreeing whose day it is. Every timestamp the panel *displays* is still
    converted, in the browser, to the zone of whoever is reading it.
    """
    moment = now or timezone.now()
    try:
        row = DailyTotal.objects.filter(day=utc_day(moment)).first()
    except DatabaseError as exc:
        # No table yet, or a database file replaced by a restore. A zero beside
        # the working all-time totals beats a 500 on the whole dashboard.
        log.debug("today's traffic total unavailable: %s", exc)
        return 0, 0
    return (row.rx, row.tx) if row is not None else (0, 0)
