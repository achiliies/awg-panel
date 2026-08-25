"""Removing every traffic figure on the server, in the two places it happens.

The endpoint and the collector's fold are one feature and are tested together,
because the thing that can go wrong with it is the seam between them. Either
half on its own is easy to get right: the request empties tables, the collector
subtracts. What is worth asserting is the property they only have jointly - that
from the moment the request answers until long after the fold, every figure the
panel reports is zero, and that a byte crossing the tunnel in between is counted
exactly once rather than lost or restored along with the morning.

The other half of the seam is the direction nobody tests by accident: the
collector holds today's totals and traffic.db in memory and rewrites both from
it, so a wipe it was not told about is undone within ten seconds. Several of
these tests are about that undoing not happening.
"""

import time

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.clients import index
from apps.clients.models import ClientIndex, ClientMeta
from apps.events.models import Event
from apps.stats.management.commands.collector import Collector
from apps.stats.models import ClientDaily, DailyTotal, TrafficReset, utc_day
from awg import store, traffic
from awg.traffic import Counters

from .test_collector import CLIENT1_KEY, ScriptedController, dump_of

pytestmark = pytest.mark.django_db

RESET_URL = "/api/v1/stats/traffic/reset"


def api_url(path: str) -> str:
    return f"/api/v1/{path}"


@pytest.fixture
def api(server_conf) -> APIClient:
    user = get_user_model().objects.create_user("admin")
    client = APIClient()
    client.force_login(user)
    return client


@pytest.fixture
def busy(server_conf) -> ClientMeta:
    """The fixture's client, with a lifetime of traffic behind it.

    Everything a wipe is supposed to take, in the four places it lives: the
    counters in traffic.db, the copy of them the client list sorts by, the
    server's stored days and the client's own.
    """
    traffic.write_db(
        {CLIENT1_KEY: Counters(cum_rx=6_000, cum_tx=90_000, last_rx=6_000, last_tx=90_000)}
    )
    # Built here rather than by the first request, so that the counters the list
    # sorts by are in place before anything asks - and so that a test can look
    # at that copy directly.
    index.rebuild(force=True)
    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    meta.name = store.list_clients()[0].name
    meta.save(update_fields=["name"])

    today = utc_day(timezone.now())
    DailyTotal.objects.create(day=today, rx=6_000, tx=90_000)
    ClientDaily.objects.create(meta=meta, day=today, rx=6_000, tx=90_000)
    return meta


def totals(api: APIClient) -> tuple[int, int]:
    """What the dashboard's all-time card would say."""
    body = api.get(api_url("stats/summary")).json()
    return body["totalRx"], body["totalTx"]


def client_row(api: APIClient, name: str) -> dict:
    body = api.get(api_url("clients")).json()
    return next(row for row in body["clients"] if row["name"] == name)


# ----------------------------------------------------------------- the request


def test_the_request_takes_every_figure_the_panel_reports(api, busy):
    """All four at once, which is the whole of what the button promises."""
    assert totals(api) != (0, 0)

    response = api.post(RESET_URL)
    assert response.status_code == 200, response.content

    assert totals(api) == (0, 0)
    name = store.list_clients()[0].name
    row = client_row(api, name)
    assert (row["rxBytes"], row["txBytes"]) == (0, 0)

    history = api.get(api_url("stats/traffic")).json()
    assert all(point["rx"] == 0 and point["tx"] == 0 for point in history["daily"])
    assert history["earliest"] is None
    mine = api.get(api_url(f"clients/{name}/traffic")).json()
    assert all(point["rx"] == 0 and point["tx"] == 0 for point in mine["daily"])


def test_the_request_says_how_much_it_took(api, busy):
    body = api.post(RESET_URL).json()
    assert body == {"clients": 1, "serverDays": 1, "clientDays": 1}


def test_a_client_that_never_moved_a_byte_is_not_counted(api, server_conf):
    """ "Cleared the traffic of one client" has to be about a client that had
    some. Nothing changed for a peer whose totals were already zero, and saying
    otherwise would make the count a client count with a longer name."""
    ClientMeta.objects.get_or_create(
        public_key=CLIENT1_KEY, defaults={"name": store.list_clients()[0].name}
    )
    assert api.post(RESET_URL).json()["clients"] == 0


def test_the_clients_themselves_are_untouched(api, busy, server_conf):
    """The one thing an admin has to be able to believe before pressing it: this
    is about numbers, and nobody's tunnel stops working."""
    before = store.get_client(store.list_clients()[0].name)
    conf = server_conf.read_text(encoding="utf-8")

    api.post(RESET_URL)

    after = store.get_client(before.name)
    assert (after.public_key, after.ip, after.enabled) == (
        before.public_key,
        before.ip,
        before.enabled,
    )
    assert server_conf.read_text(encoding="utf-8") == conf


def test_the_wipe_is_recorded(api, busy):
    api.post(RESET_URL)
    event = Event.objects.get(kind="panel.traffic-cleared")
    assert event.severity == "warning"
    assert event.detail == {"count": 1, "days": 1}


def test_a_stranger_cannot_clear_the_traffic(server_conf, busy):
    assert APIClient().post(RESET_URL).status_code in (401, 403)
    assert DailyTotal.objects.exists()


def test_a_token_may_clear_the_traffic(api, busy):
    """Unlike emptying the activity log, which is refused to a token.

    The line drawn there is that a token must not be able to erase the record of
    what it did, and this cannot: the record is the row it writes to that same
    log, and the log is not what this empties. What it takes is measurements, and
    a script that rotates a server between tenants is exactly the caller that
    should not have to drive a browser to do it.
    """
    issued = api.post(api_url("settings/tokens"), {"name": "rotate"}, format="json")
    assert issued.status_code == 201, issued.content
    secret = issued.json()["secret"]

    bearer = APIClient(enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
    assert bearer.post(RESET_URL).status_code == 200
    assert not DailyTotal.objects.exists()
    assert Event.objects.filter(kind="panel.traffic-cleared").exists()


# -------------------------------------------------------------- the two halves


def test_the_offset_covers_what_the_collector_has_not_flushed_yet(api, busy):
    """The live blob is up to ten seconds fresher than traffic.db, and it is what
    the browser merges over the row it is given. An offset taken from the file
    alone would leave the newest seconds of a busy client's usage on screen
    after a wipe that was supposed to take all of it."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 6_500, 91_000))])
    collector = Collector(controller=controller, once=True)
    # A poll, and deliberately no flush: the bytes are now in the blob and in
    # the collector's memory, and not in the file.
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()

    api.post(RESET_URL)

    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert (meta.offset_rx, meta.offset_tx) == (6_500, 91_000)


def test_the_collector_folds_the_offsets_out_of_the_counters(api, busy):
    api.post(RESET_URL)
    collector = Collector(controller=ScriptedController([]), once=True)
    collector._apply_traffic_reset()

    row = traffic.read_db()[CLIENT1_KEY]
    assert (row.cum_rx, row.cum_tx) == (0, 0)
    # The raw readings stay: they are what the next delta is measured from, and
    # a zero here would be read as a counter reset and add the lot straight back.
    assert (row.last_rx, row.last_tx) == (6_000, 90_000)
    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert (meta.offset_rx, meta.offset_tx) == (0, 0)
    index = ClientIndex.objects.get(meta=meta)
    assert (index.cum_rx, index.cum_tx) == (0, 0)


def test_the_reported_figure_does_not_move_when_the_collector_folds(api, busy):
    """The property the two halves only have together. Before the fold the
    offset covers the counter; after it both are zero; a reader watching
    throughout never sees the old number come back."""
    api.post(RESET_URL)
    assert totals(api) == (0, 0)

    Collector(controller=ScriptedController([]), once=True)._apply_traffic_reset()
    assert totals(api) == (0, 0)


def test_bytes_that_move_between_the_two_halves_are_kept(api, busy):
    """They moved after the wipe, so they are this server's traffic now. Zeroing
    the counters outright rather than subtracting the offset would throw them
    away and make the figure jump backwards a second time."""
    api.post(RESET_URL)

    controller = ScriptedController([dump_of((CLIENT1_KEY, 6_400, 90_300))])
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    collector._flush_traffic_db()
    collector._apply_traffic_reset()

    row = traffic.read_db()[CLIENT1_KEY]
    assert (row.cum_rx, row.cum_tx) == (400, 300)
    assert totals(api) == (400, 300)


def test_the_fold_happens_once(api, busy):
    """A second pass must not subtract an offset it has already taken. What it
    would take the second time is exactly the traffic since the wipe."""
    api.post(RESET_URL)
    collector = Collector(controller=ScriptedController([]), once=True)
    collector._apply_traffic_reset()

    traffic.write_db({CLIENT1_KEY: Counters(cum_rx=400, cum_tx=300, last_rx=6_400, last_tx=90_300)})
    collector._apply_traffic_reset()

    assert traffic.read_db()[CLIENT1_KEY].cum_rx == 400
    row = TrafficReset.objects.get()
    assert row.applied_at == row.requested_at


def test_the_collector_lets_go_of_the_day_it_is_holding(api, busy):
    """Today is an absolute total in memory, written by overwriting the row. A
    flush after a wipe that did not clear it would put the whole morning back."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 6_000, 90_000))])
    collector = Collector(controller=controller, once=True)
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    assert collector._today_rx or collector._today_tx

    api.post(RESET_URL)
    collector._apply_traffic_reset()
    collector._store_today()
    collector._store_client_today()

    assert not DailyTotal.objects.exists()
    assert not ClientDaily.objects.exists()


def test_a_flush_before_the_fold_does_not_survive_it(api, busy):
    """The ten seconds between the request and the collector noticing, in which
    it writes today's row back out of the memory it is still holding. Those rows
    are taken by the fold, bounded by the day the wipe was asked for."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 6_000, 90_000))])
    collector = Collector(controller=controller, once=True)
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()

    api.post(RESET_URL)
    # The window: a flush lands after the request and before the fold.
    collector._store_today()
    collector._store_client_today()
    assert DailyTotal.objects.exists()

    collector._apply_traffic_reset()
    assert not DailyTotal.objects.exists()
    assert not ClientDaily.objects.exists()


def test_a_wipe_asked_for_while_the_collector_was_down_is_still_applied(api, busy):
    """The reason the row carries two stamps rather than being compared against
    something this process remembers: a process that has just started remembers
    nothing, and the wipe would be forgotten by the one restart most likely to
    follow it."""
    api.post(RESET_URL)

    # A brand new loop, exactly as systemd would start it.
    fresh = Collector(controller=ScriptedController([]), once=True)
    fresh._apply_traffic_reset()

    assert traffic.read_db()[CLIENT1_KEY].cum_rx == 0
    row = TrafficReset.objects.get()
    assert row.applied_at == row.requested_at


def test_a_second_wipe_reopens_the_fold(api, busy):
    """One row, and it holds a question rather than a queue: asking again before
    the collector has caught up is the same clearing asked for twice."""
    api.post(RESET_URL)
    Collector(controller=ScriptedController([]), once=True)._apply_traffic_reset()
    assert not TrafficReset.objects.get().pending

    api.post(RESET_URL)
    assert TrafficReset.objects.get().pending


def test_a_peer_with_no_client_row_loses_its_counters_too(api, busy):
    """A row in traffic.db that nothing points at - a peer added to the config by
    hand, or one whose client went in the seconds after the wipe. There is no
    offset to subtract and nothing reports it, so leaving it whole would keep a
    total the wipe was asked to take and that nobody can see to check."""
    stranger = "0000000000000000000000000000000000000000000="
    db = traffic.read_db()
    db[stranger] = Counters(cum_rx=1_000, cum_tx=2_000, last_rx=1_000, last_tx=2_000)
    traffic.write_db(db)

    api.post(RESET_URL)
    Collector(controller=ScriptedController([]), once=True)._apply_traffic_reset()

    row = traffic.read_db()[stranger]
    assert (row.cum_rx, row.cum_tx) == (0, 0)
    assert (row.last_rx, row.last_tx) == (1_000, 2_000)


def test_the_fold_runs_on_the_flush_clock(api, busy):
    """Wired into the loop rather than only callable by hand, and after the flush
    rather than before it: the flush is what puts this cycle's bytes into the
    file the fold subtracts from."""
    api.post(RESET_URL)
    controller = ScriptedController([dump_of((CLIENT1_KEY, 6_000, 90_000))])
    Collector(controller=controller, once=True).run()

    assert traffic.read_db()[CLIENT1_KEY].cum_rx == 0
    assert not TrafficReset.objects.get().pending


def test_a_client_stopped_by_its_quota_comes_back(api, busy):
    """It has nothing left to enforce once its counter is at zero, and leaving it
    dark would read as the wipe not having worked. Done by the collector rather
    than by the request, which is why the pass is booked for the next cycle."""
    name = store.list_clients()[0].name
    ClientMeta.objects.filter(public_key=CLIENT1_KEY).update(
        quota_bytes=1_000, disabled_reason="quota"
    )
    store.set_client_enabled(name, False)
    assert not store.get_client(name).enabled

    api.post(RESET_URL)
    collector = Collector(controller=ScriptedController([]), once=True)
    collector._apply_traffic_reset()
    assert collector._next_enforce == 0.0
    collector._reconcile()

    assert store.get_client(name).enabled
    assert ClientMeta.objects.get(public_key=CLIENT1_KEY).disabled_reason == ""
