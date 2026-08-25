"""The collector loop: accounting, enforcement, and staying alive.

This process is the only thing that polls the interface, so everything it gets
wrong is silent. A dropped delta shows up as a dashboard that under-reports; a
negative delta after an interface restart shows up as usage going backwards and
a quota that never triggers; a cycle that raises takes traffic history and quota
enforcement down with it and nothing says so until somebody notices the charts
are flat.

So the tests here drive the real loop rather than its parts: a scripted
controller stands in for `awg show dump`, and what is asserted is what ends up
in live.json, in traffic.db and in the server config - the three files everything
else in the panel reads.
"""

import json
import time
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db import DatabaseError
from django.utils import timezone

from apps.accounts.models import LoginSession
from apps.clients import index, merge
from apps.clients.models import ClientIndex, ClientMeta
from apps.panel import settings_store
from apps.stats.management.commands.collector import ROLLOVER_GRACE_SEC, Collector
from apps.stats.models import CLIENT_HISTORY_DAYS, ClientDaily, DailyTotal, utc_day
from awg import shaper, store, sysinfo, traffic
from awg.conf import parse_conf, strip_conf
from awg.controller import Dump, PeerDump, reset_controller
from awg.errors import AwgError, LockTimeout, ToolError
from awg.paths import live_state_file, traffic_db
from awg.traffic import Counters

pytestmark = pytest.mark.django_db

# The fixture's two peers: the named one the mock puts on the interface, and the
# one carrying a "# Disabled" marker, which the kernel must never learn about.
CLIENT1_KEY = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="
DISABLED_KEY = "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4="

# Exactly the keys apps/stats/live.py promises and the frontend reads.
BLOB_KEYS = {
    "ts",
    "ifaceUp",
    # Since when the tunnel has been up, and the netdev that claim is about.
    # Nothing in the kernel records the first, so the collector observes it and
    # uses the second to know when its memory has expired.
    "ifaceSince",
    "ifaceIndex",
    "online",
    "total",
    "totalRateRx",
    "totalRateTx",
    "peers",
    "system",
}
PEER_KEYS = {
    "rateRx",
    "rateTx",
    "rx",
    "tx",
    "handshake",
    # When the peer last sent anything, and the connectivity word the collector
    # derived from it. The word is sent so the browser never derives one: a copy
    # of the thresholds over there is what let a client the API had called
    # offline go on reading "idle" on the page for a whole day.
    "lastRx",
    "status",
    "endpoint",
    "online",
}
SYSTEM_KEYS = {
    "cpu",
    "memUsed",
    "memTotal",
    # Bytes rather than mebibytes, and the only two figures here that are about
    # a service rather than the host: what the tunnel costs, and what the panel
    # costs. See awg/sysinfo.py for what each one counts.
    "memCore",
    "memPanel",
    # Swap in bytes, and zero for a total on a machine that has none - which is
    # a different claim from an empty swap device and is drawn differently.
    "swapUsed",
    "swapTotal",
    "uptime",
    "load",
    # Rates, worked out between two polls: the counters they come from are since
    # boot and nothing reading this file has a second reading to subtract.
    "diskRead",
    "diskWrite",
    "diskBusy",
    # How full the root filesystem is, which is the question the three rates
    # above cannot answer. Used and free do not add up to total: a filesystem
    # keeps a reserve only root may write into.
    "diskUsed",
    "diskFree",
    "diskTotal",
    "wanRx",
    "wanTx",
}


class ScriptedController:
    """A stand-in for the tunnel that answers with the dumps it was handed.

    The last dump repeats once the script runs out, so a test that runs several
    cycles only has to describe the readings that differ. `raises_on` is the set
    of 1-based poll numbers that fail instead of answering, and `stop_after`
    ends the loop after that many polls by setting the event the collector waits
    on - which is how the multi-cycle tests stay bounded without a real clock.
    """

    def __init__(self, dumps, *, raises_on=(), error=None, stop_after=None) -> None:
        self.dumps = list(dumps)
        self.raises_on = set(raises_on)
        self.error = error or RuntimeError("the AmneziaWG tools fell over")
        self.stop_after = stop_after
        self.stop = None
        self.calls = 0
        self.synced: list[str] = []
        self.up = True

    def show_dump(self) -> Dump | None:
        self.calls += 1
        if self.stop_after is not None and self.calls >= self.stop_after and self.stop is not None:
            self.stop.set()
        if self.calls in self.raises_on:
            raise self.error
        if not self.dumps:
            return None
        return self.dumps[min(self.calls - 1, len(self.dumps) - 1)]

    def iface_up(self) -> bool:
        return self.up

    def syncconf(self, stripped_text: str) -> None:
        self.synced.append(stripped_text)


def dump_of(*peers: tuple[str, int, int], handshake: int | None = None) -> Dump:
    """A dump holding the given (public key, rx, tx) readings."""
    seen = int(time.time()) if handshake is None else handshake
    return Dump(
        private_key="",
        public_key="",
        listen_port=41234,
        fwmark="",
        peers=[
            PeerDump(
                public_key=key,
                endpoint="198.51.100.7:51820",
                allowed_ips="10.13.13.4/32",
                latest_handshake=seen,
                rx=rx,
                tx=tx,
                keepalive="25",
            )
            for key, rx, tx in peers
        ],
    )


def run_once(controller: ScriptedController) -> Collector:
    """One full cycle plus the shutdown flush, the way `collector --once` runs it."""
    collector = Collector(controller=controller, once=True)
    collector.run()
    return collector


def today_row() -> tuple[int, int]:
    """Today's stored total, or zeros if no flush has opened the row yet."""
    row = DailyTotal.objects.filter(day=utc_day(timezone.now())).first()
    return (row.rx, row.tx) if row is not None else (0, 0)


def conf_text(server_conf) -> str:
    return server_conf.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_controller():
    """One mock interface per test; the controller is a process-wide singleton.

    Enforcement re-applies the interface through awg.store, which reaches for
    get_controller() rather than the collector's own, so the cached mock has to
    start each test with no memory of the peers it was told to drop.
    """
    reset_controller()
    yield
    reset_controller()


# ------------------------------------------------------------ the once pass


def test_once_writes_a_well_formed_live_json(server_conf, data_dir):
    """`collector --once` against the mock is what `make dev` and CI both run."""
    call_command("collector", "--once")

    path = live_state_file()
    blob = json.loads(path.read_text(encoding="utf-8"))

    assert set(blob) == BLOB_KEYS
    assert blob["ifaceUp"] is True
    assert abs(blob["ts"] - time.time()) < 60
    assert set(blob["system"]) == SYSTEM_KEYS
    assert len(blob["system"]["load"]) == 3

    # The disabled peer is not on the interface, so it is not in the blob: what
    # is reported here is what the kernel holds, not what the config lists.
    assert set(blob["peers"]) == {CLIENT1_KEY}
    assert blob["total"] == 1
    assert set(blob["peers"][CLIENT1_KEY]) == PEER_KEYS

    peer = blob["peers"][CLIENT1_KEY]
    assert peer["rx"] > 0 and peer["tx"] > 0
    # The connectivity word, so nothing downstream has to work one out. A reader
    # that works it out needs the thresholds and a clock, and keeping those in
    # step with this process is what failed before: a copy of the idle window in
    # the browser went stale and a client the API had called offline read "idle"
    # there for a whole day.
    assert peer["status"] == merge.connectivity(peer["online"], peer["lastRx"], time.time())
    # No previous poll to difference against, so no rate can be computed
    # honestly; zero is the truthful answer rather than a guess.
    assert peer["rateRx"] == 0 and peer["rateTx"] == 0

    # Group-readable, not world-readable: peer keys and endpoints are in it.
    assert path.stat().st_mode & 0o777 == 0o640


def test_once_records_todays_traffic(server_conf):
    call_command("collector", "--once")

    rows = list(DailyTotal.objects.all())
    assert len(rows) == 1, "one row a day for the whole server, whatever the peer count"
    assert rows[0].day == utc_day(timezone.now())
    assert rows[0].rx > 0
    assert rows[0].tx > 0


# --------------------------------------------------------------- accounting


def test_counters_are_differenced_and_survive_an_interface_restart(server_conf):
    """The counter-epoch rule, end to end through the loop.

    A reading below the last one means the kernel started a new counter epoch,
    so the whole current value is new traffic. Subtracting would give a negative
    delta, which the charts would draw and the quota check would credit back.
    """
    controller = ScriptedController(
        [
            dump_of((CLIENT1_KEY, 100, 200)),
            dump_of((CLIENT1_KEY, 300, 500)),
            # `awg-quick down && up`: the counters restart from zero.
            dump_of((CLIENT1_KEY, 50, 60)),
        ]
    )

    for _ in range(3):
        run_once(controller)

    # 100+200+50 received and 200+300+60 sent, the restart's whole new epoch
    # included: a subtracted delta would have gone negative and taken today's
    # figure down with it.
    assert today_row() == (350, 560)
    # All-time totals keep climbing across the restart; the raw columns follow
    # the kernel back down to where it now is.
    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=350, cum_tx=560, last_rx=50, last_tx=60
    )


def test_a_busy_peer_keeps_its_whole_epoch_when_the_interface_restarts(server_conf, monkeypatch):
    """The case traffic.epoch was added for, on the path that feeds today's chart.

    A peer that transfers more after a restart than it had before it hands the
    next fold a reading *above* the stored one, so nothing in the numbers says
    the interface ever went away. awg.traffic settles that from traffic.epoch,
    and traffic.db has been right about it since - but the collector keeps a
    second copy of the same counters in memory, diffs against that on the 2s
    poll clock, and was never told. So the file's all-time total came out right
    while today's figure, the client's own row and the number quota enforcement
    spends were all short by the whole of the peer's previous epoch, and stayed
    short: nothing re-seeds the mirror, so the gap never closed.
    """
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 1000, 2000))])
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    collector._flush_traffic_db()
    collector._reconcile()
    collector._store_today()
    collector._store_client_today()
    assert today_row() == (1000, 2000)

    # `ip link del` and back: a new netdev, counters from zero, and a peer busy
    # enough to climb past where it was before the next flush comes round.
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 8")
    controller.dumps = [dump_of((CLIENT1_KEY, 1500, 2500))]
    controller.calls = 0
    collector.cycle()
    collector._flush_traffic_db()
    collector._store_today()
    collector._store_client_today()

    # 1500 of the new epoch on top of the 1000 of the old one, in all three
    # places - not the 500 a subtraction against a dead interface would give.
    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=2500, cum_tx=4500, last_rx=1500, last_tx=2500
    )
    assert collector._counters[CLIENT1_KEY] == Counters(
        cum_rx=2500, cum_tx=4500, last_rx=1500, last_tx=2500
    )
    assert today_row() == (2500, 4500)
    assert client_row() == (2500, 4500)


def test_an_epoch_that_has_not_moved_is_still_a_difference(server_conf, monkeypatch):
    """The other direction, which is the one that would double every total."""
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    controller.dumps = [dump_of((CLIENT1_KEY, 130, 260))]
    controller.calls = 0
    collector.cycle()

    assert collector._counters[CLIENT1_KEY] == Counters(
        cum_rx=130, cum_tx=260, last_rx=130, last_tx=260
    )


def test_an_unreadable_epoch_leaves_the_mirror_on_the_counters_alone(server_conf, monkeypatch):
    """A kernel that publishes neither file, and a suite running nowhere near a
    real interface, both arrive here. "" is not evidence of a restart, so the
    fold falls back on the raw values exactly as it did before any of this."""
    monkeypatch.setattr(traffic, "current_epoch", lambda: "")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    controller.dumps = [dump_of((CLIENT1_KEY, 130, 260))]
    controller.calls = 0
    collector.cycle()

    assert collector._counters[CLIENT1_KEY] == Counters(
        cum_rx=130, cum_tx=260, last_rx=130, last_tx=260
    )


def test_a_reading_from_before_a_restart_is_dropped_rather_than_folded(server_conf, monkeypatch):
    """The window a failed poll does not already cover.

    The interface can go away in the last poll interval before the flush, which
    leaves a reading from the old run of it in hand and no cycle in between to
    clear it. Folding it then has sync_db find a changed epoch, treat an old
    counter as a whole new one and add the peer's entire lifetime to its total.
    """
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 1000, 2000))])
    collector = Collector(controller=controller, once=True)
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    collector._flush_traffic_db()

    # A poll, and then the interface is recreated before the flush clock fires.
    controller.dumps = [dump_of((CLIENT1_KEY, 1100, 2100))]
    controller.calls = 0
    collector.cycle()
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 8")

    collector._flush_traffic_db()

    # The hundred bytes between the last flush and the teardown are gone, which
    # is what the PreDown hook is there to fold and is lost anyway on a crash.
    # The alternative was adding 1100 twice, for good.
    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=1000, cum_tx=2000, last_rx=1000, last_tx=2000
    )


def test_the_mirror_takes_back_what_the_hook_folded_in_behind_it(server_conf, monkeypatch):
    """traffic.db knows things the mirror can never observe for itself.

    PreDown runs `awg-panel manage trafficsync` in another process and folds the
    last reading of a dying interface straight into the file. The collector
    never polls that reading - the interface is gone by its next cycle, and what
    comes back is a counter from zero - so those bytes exist in the file and
    nowhere else. The mirror is what the dashboard's usage figure and the fast
    quota path are read from, so it drifted below the file by every orderly
    teardown the server had ever had, and only a collector restart put it back.

    The flush is the one moment the two are level, so the flush is where the
    mirror takes the file's answer rather than keeping its own.
    """
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    collector._flush_traffic_db()

    # PreDown, in the process this one cannot see: 400 bytes the collector had
    # no cycle left to poll.
    traffic.sync({CLIENT1_KEY: (500, 200)}, ending=True)
    assert traffic.read_db()[CLIENT1_KEY].cum_rx == 500
    assert collector._counters[CLIENT1_KEY].cum_rx == 100

    # Up again: a new netdev, and the kernel counting from zero.
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 8")
    controller.dumps = [dump_of((CLIENT1_KEY, 50, 10))]
    controller.calls = 0
    collector.cycle()
    collector._flush_traffic_db()

    assert collector._counters[CLIENT1_KEY] == traffic.read_db()[CLIENT1_KEY]
    assert collector._counters[CLIENT1_KEY] == Counters(
        cum_rx=550, cum_tx=210, last_rx=50, last_tx=10
    )


def test_a_peer_the_file_has_not_heard_of_keeps_its_place_in_the_mirror(server_conf):
    """The mirror takes the file's rows without taking its silences.

    A peer first seen by a poll whose reading was then dropped is in the mirror
    and not yet in the file. Replacing the mirror wholesale would take its row
    away, and the next reading would be folded from a last_rx of nothing - the
    peer's whole counter, counted again as new traffic.
    """
    controller = ScriptedController(
        [dump_of((CLIENT1_KEY, 100, 200)), dump_of((CLIENT1_KEY, 150, 250), (DISABLED_KEY, 70, 80))]
    )
    collector = Collector(controller=controller, once=True)
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    collector._flush_traffic_db()

    # A second peer arrives, and the reading carrying it never reaches the file.
    collector.cycle()
    collector._unflushed = None
    controller.dumps = [dump_of((CLIENT1_KEY, 160, 260))]
    controller.calls = 0
    collector.cycle()
    collector._flush_traffic_db()

    assert DISABLED_KEY not in traffic.read_db()
    assert collector._counters[DISABLED_KEY] == Counters(
        cum_rx=70, cum_tx=80, last_rx=70, last_tx=80
    )


def test_a_poll_taken_before_the_interface_went_down_is_never_folded(server_conf):
    """The window that matters, and the one a naive fix leaves open.

    Poll at t, interface stops at t+1, PreDown runs `awg-panel manage trafficsync`
    and writes the true final counters, and only then does the 10s flush clock
    fire - holding a reading from before the stop. awg.traffic reads that as a
    counter reset and adds the peer's whole epoch a second time, permanently,
    into the figure quota enforcement reads.
    """
    controller = ScriptedController([dump_of((CLIENT1_KEY, 1000, 2000))])
    collector = Collector(controller=controller, once=True)

    collector.cycle()
    collector._flush_traffic_db()
    # A second poll that never gets flushed, exactly as the 2s/10s clocks do.
    controller.dumps = [dump_of((CLIENT1_KEY, 1100, 2000))]
    controller.calls = 0
    collector.cycle()

    # The interface goes down; PreDown catches the last bytes the collector
    # never saw, in the form awg.traffic writes them.
    traffic.write_db({CLIENT1_KEY: Counters(cum_rx=1200, cum_tx=2000, last_rx=1200, last_tx=2000)})
    controller.dumps = []
    controller.up = False

    collector.cycle()
    collector._flush_traffic_db()
    collector.shutdown()

    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=1200, cum_tx=2000, last_rx=1200, last_tx=2000
    )


def test_a_cycle_without_a_dump_does_not_re_fold_the_last_one(server_conf):
    """The already-flushed variant of the same thing."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 1000, 2000))])
    collector = Collector(controller=controller, once=True)

    collector.cycle()
    collector._flush_traffic_db()
    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=1000, cum_tx=2000, last_rx=1000, last_tx=2000
    )

    traffic.write_db({CLIENT1_KEY: Counters(cum_rx=1200, cum_tx=2000, last_rx=1200, last_tx=2000)})
    controller.dumps = []
    controller.up = False

    collector.cycle()
    collector._flush_traffic_db()
    collector.shutdown()

    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=1200, cum_tx=2000, last_rx=1200, last_tx=2000
    )


def test_a_reading_is_consumed_by_the_flush_that_writes_it(server_conf):
    """Asserted on the field rather than on the totals: re-folding an unchanged
    reading is arithmetically a no-op, so the counters cannot tell the two
    implementations apart and a test written against them would pass either
    way."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)

    # The flush clock is driven by hand here; cycle() would otherwise fire it.
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    assert collector._unflushed == {CLIENT1_KEY: (100, 200)}

    collector._flush_traffic_db()
    assert collector._unflushed is None


def test_a_failed_write_keeps_the_reading_for_the_next_flush(server_conf, monkeypatch):
    """Dropping it would lose that traffic for good when the interface never
    comes back - and retrying is safe only because a cycle that cannot read the
    interface clears the reading first."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 500, 600))])
    collector = Collector(controller=controller, once=True)
    # The flush clock is driven by hand here; cycle() would otherwise fire it.
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()

    def explode(_transfers):
        raise AwgError("the disk is full")

    monkeypatch.setattr(traffic, "sync_db", explode)
    collector._flush_traffic_db()
    assert collector._unflushed == {CLIENT1_KEY: (500, 600)}

    monkeypatch.undo()
    collector._flush_traffic_db()
    assert traffic.read_db()[CLIENT1_KEY] == Counters(
        cum_rx=500, cum_tx=600, last_rx=500, last_tx=600
    )


def test_a_flush_leaves_the_client_list_needing_nothing(server_conf, monkeypatch):
    """End to end: the usage a flush wrote is in the index, and the index knows it.

    Without the stamp moving with the rows the saving is nil - the figures would
    already be right and every reader would go and prove it again by parsing the
    whole file and comparing every row.
    """
    client = store.add_client("phone")
    controller = ScriptedController([dump_of((client.public_key, 4096, 2048))])
    index.current()

    run_once(controller)

    def fail(*args, **kwargs):
        raise AssertionError("traffic.db was read again after the collector had written it")

    monkeypatch.setattr(index, "_counters", fail)
    monkeypatch.setattr(index, "rebuild", fail)
    index.current()

    row = ClientIndex.objects.get(meta__public_key=client.public_key)
    assert (row.cum_rx, row.cum_tx) == (4096, 2048)


def test_a_flush_after_something_else_wrote_leaves_the_rebuild_to_do_it(server_conf):
    """The collector is not the only writer of that file. When its delta does not
    lead from what the index stored to what is on disk, it hands the whole job
    back rather than stamping a table it has only half corrected."""
    client = store.add_client("phone")
    controller = ScriptedController([dump_of((client.public_key, 10, 10))])
    index.current()
    # A PreDown hook's fold for a peer this collector never polled.
    other = store.add_client("laptop")
    traffic.sync({other.public_key: (700, 800)})

    run_once(controller)

    # Refused, so the stamp still says there is something to read...
    assert index.Stamp.stored(index._state()).counters != index.Stamp.observe().counters
    # ...and reading it picks up both peers.
    index.current()
    assert ClientIndex.objects.get(meta__public_key=other.public_key).cum_rx == 700
    assert ClientIndex.objects.get(meta__public_key=client.public_key).cum_rx == 10


def test_an_idle_peer_moves_nothing(server_conf):
    """Most peers on most polls have moved nothing. The second pass sees the
    same counters, so it adds nothing and leaves the row where it was."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 100))])

    run_once(controller)
    run_once(controller)

    assert today_row() == (100, 100)


def test_traffic_db_stays_in_the_format_bash_reads(server_conf):
    """Five space-separated columns, no header, 0600 - the format on every server."""
    call_command("collector", "--once")

    path = traffic_db()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines

    for line in lines:
        fields = line.split(" ")
        assert len(fields) == 5, line
        assert all(field.isdigit() for field in fields[1:]), line

    assert set(traffic.read_db()) == {line.split(" ")[0] for line in lines}
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_pre_existing_row_is_kept_and_extended(server_conf):
    """The file is shared with the PreDown hook, which may have written
    it a second ago; a collector that rebuilt it from scratch would lose the
    history of every peer that is currently offline."""
    traffic.write_db(
        {
            CLIENT1_KEY: Counters(1000, 2000, 1000, 2000),
            DISABLED_KEY: Counters(7, 8, 7, 8),
        }
    )

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 1500, 2200))]))

    db = traffic.read_db()
    assert db[CLIENT1_KEY] == Counters(1500, 2200, 1500, 2200)
    assert db[DISABLED_KEY] == Counters(7, 8, 7, 8)
    # Only what moved this pass reaches today's figure, not the pre-existing
    # all-time totals the file was already carrying.
    assert today_row() == (500, 200)


def test_a_busy_server_still_writes_exactly_one_row(server_conf):
    """The whole point of the change this table came out of.

    Five thousand peers moving traffic in one window used to be five thousand
    inserts, and a cap on the retry buffer that a flush this size tripped over
    on a healthy database. Summed in memory first, the peer count does not reach
    the database at all: one row, holding every peer's bytes.
    """
    keys = [f"{n:0>43}=" for n in range(5000)]

    run_once(ScriptedController([dump_of(*((key, 100, 200) for key in keys))]))

    assert DailyTotal.objects.count() == 1
    assert today_row() == (100 * len(keys), 200 * len(keys))


# ------------------------------------------------------------------- the day


# ------------------------------------------------------- the day, per client


def client_row(key: str = CLIENT1_KEY) -> tuple[int, int]:
    """One client's stored figure for today, or zeros if no flush has opened it."""
    row = ClientDaily.objects.filter(meta__public_key=key, day=utc_day(timezone.now())).first()
    return (row.rx, row.tx) if row is not None else (0, 0)


def test_a_client_gets_its_own_row_for_the_day(server_conf):
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert client_row() == (100, 200)
    # The same bytes, counted once against the client and once against the
    # server. They are two readings of one set of deltas, so on a server nobody
    # has been deleted from they agree exactly - and the day they are cut on is
    # decided in one place so that they go on agreeing.
    assert today_row() == (100, 200)


def test_a_client_that_moved_nothing_gets_no_row(server_conf):
    """What keeps this table the size of a busy day rather than the size of the
    server. Most peers on most polls have moved nothing, and a row per idle
    client is the growth the per-client sample table was removed for."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    run_once(controller)
    run_once(controller)

    assert ClientDaily.objects.count() == 1
    assert client_row() == (100, 200)


def test_every_busy_client_is_one_row_and_one_statement(server_conf):
    """A hundred clients transferring is a hundred rows, not a hundred rows per
    flush: the row is keyed by the day, so every write after the first rewrites
    the same one."""
    keys = [f"{n:0>43}=" for n in range(100)]
    controller = ScriptedController([dump_of(*((key, 100, 200) for key in keys))])

    run_once(controller)
    run_once(controller)

    assert ClientDaily.objects.count() == len(keys)
    assert ClientDaily.objects.filter(day=utc_day(timezone.now())).count() == len(keys)


def test_a_clients_day_is_resumed_after_a_restart(server_conf):
    """The row holds an absolute total and is written by overwriting it, so a
    collector that started from an unseeded zero would not merely lose the
    morning - it would replace it with the minutes since it came up."""
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    assert client_row() == (100, 200)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 250, 500))]))

    assert client_row() == (250, 500)
    assert ClientDaily.objects.count() == 1


def test_midnight_gives_a_client_a_new_row_and_finishes_the_old_one(server_conf, monkeypatch):
    """The rollover is decided once for both tables, so a client's day and the
    server's day can never be cut on different sides of midnight."""
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    day_two = day_one + timedelta(seconds=20)

    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    Collector(controller=controller, once=True).run()

    clock[0] = day_two
    controller.dumps = [dump_of((CLIENT1_KEY, 175, 260))]
    controller.calls = 0
    Collector(controller=controller, once=True).run()

    rows = {
        row.day: (row.rx, row.tx)
        for row in ClientDaily.objects.filter(meta__public_key=CLIENT1_KEY)
    }
    assert rows[utc_day(day_one)] == (100, 200)
    assert rows[utc_day(day_two)] == (75, 60)


def test_a_client_with_no_metadata_row_yet_keeps_its_bytes_for_the_next_flush(server_conf):
    """The id map is a pass behind, and a client added since the last reconcile
    has no id to write against. What is held is the day's running total rather
    than a delta, so a row that arrives late arrives complete."""
    collector = Collector(controller=ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    collector._today = utc_day(timezone.now())
    collector._client_today = {CLIENT1_KEY: [100, 200]}
    collector._client_dirty = {CLIENT1_KEY}
    collector._meta_ids = {}

    collector._store_client_today()

    assert ClientDaily.objects.count() == 0
    assert collector._client_dirty == {CLIENT1_KEY}, "the bytes must not be dropped"

    meta = ClientMeta.objects.create(public_key=CLIENT1_KEY)
    collector._meta_ids = {CLIENT1_KEY: meta.pk}
    collector._store_client_today()

    assert client_row() == (100, 200)


def test_a_refused_client_write_is_retried_with_what_has_moved_since(server_conf, monkeypatch):
    """Nothing is lost by a failure and nothing is double counted by the retry:
    the row is overwritten with an absolute total, not incremented by a delta.

    The client stays dirty across the refusal, which is the half that matters.
    Clearing it would drop the bytes on the floor; leaving it puts the whole
    day's figure - not just the retry's share of it - into the next statement.
    """
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)

    def refuse(*args, **kwargs):
        raise DatabaseError("attempt to write a readonly database")

    monkeypatch.setattr(ClientDaily.objects, "bulk_create", refuse)
    collector.cycle()
    assert ClientDaily.objects.count() == 0
    assert collector._client_dirty == {CLIENT1_KEY}

    monkeypatch.undo()
    controller.dumps = [dump_of((CLIENT1_KEY, 150, 275))]
    controller.calls = 0
    collector.cycle()
    collector.shutdown()

    assert client_row() == (150, 275)
    assert collector._client_dirty == set()


def test_a_clients_history_goes_when_the_client_is_removed(server_conf):
    """Deleting the metadata row is what deletes the history, in all five places
    a client can be removed from - see apps.stats.models.ClientDaily."""
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    assert ClientDaily.objects.count() == 1

    ClientMeta.objects.filter(public_key=CLIENT1_KEY).delete()

    assert ClientDaily.objects.count() == 0
    # The server's own day keeps the bytes: they moved, whoever has since gone.
    assert today_row() == (100, 200)


def reset_usage_elsewhere(key: str = CLIENT1_KEY) -> ClientMeta:
    """What the panel's reset endpoint does, in the process this one cannot see."""
    meta = ClientMeta.objects.get(public_key=key)
    ClientDaily.objects.filter(meta=meta).delete()
    ClientMeta.objects.filter(pk=meta.pk).update(history_reset_at=timezone.now())
    return meta


def test_a_usage_reset_takes_the_day_this_process_is_still_holding(server_conf):
    """The half of the reset that happens over here, and why it is needed.

    Today's per-client figure is held in memory and written out as an absolute
    total, so a client that goes on transferring after the reset has its whole
    day - the cleared morning included - written back over the deletion within
    ten seconds. The stamp on the metadata row is the only way that news
    crosses, and the reconcile is where it is read.
    """
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = run_once(controller)
    assert client_row() == (100, 200)

    meta = reset_usage_elsewhere()

    # A minute in which the client keeps transferring and no reconcile has run.
    collector._next_flush = 0.0
    collector._next_enforce = time.monotonic() + 3600
    controller.dumps = [dump_of((CLIENT1_KEY, 130, 260))]
    controller.calls = 0
    collector.cycle()
    assert client_row() == (130, 260), "the row is back, which is what is corrected below"

    collector._reconcile()

    assert ClientDaily.objects.filter(meta=meta).count() == 0
    # And the memory with it, so the next flush cannot put it back a third time:
    # what this client has for today now is what it moves from here.
    collector._next_flush = 0.0
    controller.dumps = [dump_of((CLIENT1_KEY, 145, 275))]
    controller.calls = 0
    collector.cycle()
    assert client_row() == (15, 15)


def test_the_servers_day_survives_a_clients_reset(server_conf):
    """A client's figures are cleared because an admin asked for them to be; the
    server still carried those bytes, and DailyTotal is the record of that."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller)
    collector.cycle()

    reset_usage_elsewhere()
    collector._reconcile()

    assert today_row() == (100, 200)


def test_a_reset_made_before_this_process_started_is_not_acted_on_twice(server_conf):
    """The stamp says a reset happened, not that this loop owes it anything.

    Seeded at startup for exactly this reason: without the seed the first
    reconcile would read every stored stamp as news and sweep up the rows the
    reset already left behind - which after a restart are the bytes that moved
    *since* it, and are nobody's cleared history.
    """
    meta = ClientMeta.objects.create(
        public_key=CLIENT1_KEY, history_reset_at=timezone.now() - timedelta(hours=1)
    )
    ClientDaily.objects.create(meta=meta, day=utc_day(timezone.now()), rx=5, tx=6)

    run_once(ScriptedController([]))

    assert client_row() == (5, 6)


def test_a_client_seen_for_the_first_time_keeps_the_days_its_reset_did_not_cover(
    server_conf,
):
    """A reset sweeps up to the day it was asked for and no further.

    Which is what makes a client added since the last pass safe to honour: the
    stamp says when, so a reset made yesterday cannot take today's row with it
    however late this reads about it.
    """
    collector = Collector(controller=ScriptedController([]), once=True)
    collector._reconcile()

    meta = ClientMeta.objects.create(
        public_key="Z" * 43 + "=", history_reset_at=timezone.now() - timedelta(days=1)
    )
    ClientDaily.objects.create(meta=meta, day=utc_day(timezone.now()), rx=5, tx=6)

    collector._reconcile()

    assert ClientDaily.objects.filter(meta=meta).count() == 1


def test_a_client_created_and_reset_inside_one_pass_still_loses_its_day(server_conf):
    """The gap between "never compared" and "unchanged".

    A key absent from the map used to be skipped outright, on the reasoning that
    a client this loop had not met could have no history in its memory to undo.
    That holds for the database and not for this process: a client created a few
    seconds ago has already been polled, and its bytes are sitting in
    `_client_today` waiting to be written as an absolute total. Skipping the
    pass that clears them is what puts the cleared morning back on the next
    flush, a minute after an admin watched the figure go to zero.
    """
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector._reconcile()

    # Added and polled after that pass, so the reconcile below is the first one
    # ever to see this key.
    ClientMeta.objects.filter(public_key=CLIENT1_KEY).delete()
    collector._history_reset.pop(CLIENT1_KEY, None)
    meta = ClientMeta.objects.create(public_key=CLIENT1_KEY, name="client1")
    collector.cycle()
    collector._reconcile()
    collector._store_client_today()
    assert client_row() == (100, 200)

    # And now the admin resets it, still inside the same reconcile period.
    collector._history_reset.pop(CLIENT1_KEY, None)
    reset_usage_elsewhere()

    collector._reconcile()
    collector._next_flush = 0.0
    controller.dumps = [dump_of((CLIENT1_KEY, 115, 215))]
    controller.calls = 0
    collector.cycle()

    assert ClientDaily.objects.filter(meta=meta).count() == 1
    # What it has for today is what it has moved since the reset, not the
    # hundred bytes that were cleared plus them.
    assert client_row() == (15, 15)


def test_the_sweep_drops_history_past_the_retention_window(server_conf):
    collector = Collector(controller=ScriptedController([]), once=True)
    meta = ClientMeta.objects.create(public_key=CLIENT1_KEY)
    old = utc_day(timezone.now()) - timedelta(days=CLIENT_HISTORY_DAYS + 1)
    ClientDaily.objects.create(meta=meta, day=old, rx=1, tx=1)
    ClientDaily.objects.create(meta=meta, day=utc_day(timezone.now()), rx=2, tx=2)

    collector._prune_history()

    assert [row.day for row in ClientDaily.objects.all()] == [utc_day(timezone.now())]


def test_the_running_total_is_resumed_after_a_restart(server_conf):
    """A restart must not begin the day again at zero.

    The accumulator lives in memory, so without seeding it from the row a
    collector restart at eleven at night would report the day as having started
    then - and the ten seconds a hard kill can cost would instead be everything
    since midnight.
    """
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    assert today_row() == (100, 200)

    # A second process, with nothing in memory, against counters that have moved
    # on: the fresh delta is added to the stored figure rather than replacing it.
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 250, 500))]))

    assert today_row() == (250, 500)
    assert DailyTotal.objects.count() == 1


def test_a_seed_that_could_not_be_read_is_retried_rather_than_assumed(server_conf, monkeypatch):
    """A process that does not know what today holds must not start counting.

    The row is written by overwriting it with an absolute total, so a collector
    that began from zero because its first read failed would replace the whole
    day with however long it had been up. The read is retried instead, and
    nothing is accumulated until one succeeds.
    """
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 1000, 2000))]))
    assert today_row() == (1000, 2000)

    failed = []
    real = DailyTotal.objects.filter

    def flaky(*args, **kwargs):
        if not failed:
            failed.append(1)
            raise DatabaseError("database is locked")
        return real(*args, **kwargs)

    # A second process whose seeding read lands on a database held for a moment
    # by the web side, and which recovers on the next poll.
    monkeypatch.setattr(DailyTotal.objects, "filter", flaky)
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 1100, 2100))]))
    monkeypatch.undo()

    assert today_row() == (1100, 2100)


def test_midnight_starts_a_new_row_and_leaves_the_old_one_finished(server_conf, monkeypatch):
    """The day is checked every poll, so the rollover cannot lose the last
    flush interval of a day or carry it into the next one."""
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    day_two = day_one + timedelta(seconds=20)

    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector.run()
    assert DailyTotal.objects.get(day=utc_day(day_one)).rx == 100

    clock[0] = day_two
    controller.dumps = [dump_of((CLIENT1_KEY, 175, 260))]
    controller.calls = 0
    collector = Collector(controller=controller, once=True)
    collector.run()

    # Yesterday keeps what it ended on; only the new bytes land on the new day.
    assert DailyTotal.objects.get(day=utc_day(day_one)).rx == 100
    assert DailyTotal.objects.get(day=utc_day(day_two)).rx == 75
    assert DailyTotal.objects.get(day=utc_day(day_two)).tx == 60


def test_a_day_the_database_would_not_take_is_not_rolled_over(server_conf, monkeypatch):
    """The retry that used to be thrown away at the one moment it was needed.

    Both stores answer a failure by staying dirty so the next flush carries the
    same figure, and everywhere except here that is the whole handling needed.
    The rollover used to clear those flags and zero the totals whatever they
    said, so a database locked for a moment at midnight - a backup, the web
    process, anything holding SQLite - lost however much of the finished day
    had not been written yet, permanently, because there was no next flush that
    still held it.
    """
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    day_two = day_one + timedelta(seconds=20)
    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector.run()
    assert DailyTotal.objects.get(day=utc_day(day_one)).rx == 100

    # The last ten seconds of the day, which no flush has stored yet.
    collector = Collector(controller=controller, once=True)
    collector._counters = collector._load_counters()
    collector._load_today()
    controller.dumps = [dump_of((CLIENT1_KEY, 160, 200))]
    controller.calls = 0
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    assert collector._today_dirty is True

    def refuse(*args, **kwargs):
        raise DatabaseError("database is locked")

    # Midnight arrives while the database will not take the row.
    monkeypatch.setattr(DailyTotal.objects, "update_or_create", refuse)
    clock[0] = day_two
    controller.dumps = [dump_of((CLIENT1_KEY, 170, 210))]
    controller.calls = 0
    collector.cycle()

    assert collector._today == utc_day(day_one), "the finished day is still held"
    assert collector._today_dirty is True

    # And the moment it lets go, the day it was holding lands complete.
    monkeypatch.undo()
    collector.cycle()
    collector._store_today()

    assert DailyTotal.objects.get(day=utc_day(day_one)).rx == 160


def test_a_rollover_that_stays_blocked_gives_up_rather_than_stopping_the_count(
    server_conf, monkeypatch
):
    """Refusing to let go is not free: nothing is counted while it waits.

    So a database that never comes back would stop the accounting altogether
    rather than lose the end of one night, which is the larger of the two
    failures. After the grace period the finished day is written off, with a
    line saying what it was short of.
    """
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    day_two = day_one + timedelta(seconds=20)
    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector.run()

    collector = Collector(controller=controller, once=True)
    collector._counters = collector._load_counters()
    collector._load_today()
    controller.dumps = [dump_of((CLIENT1_KEY, 160, 200))]
    controller.calls = 0
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()

    def refuse(*args, **kwargs):
        raise DatabaseError("database is locked")

    monkeypatch.setattr(DailyTotal.objects, "update_or_create", refuse)
    clock[0] = day_two
    collector.cycle()
    assert collector._today == utc_day(day_one)

    # The same fault, still there when the grace period has run out.
    collector._rollover_since -= ROLLOVER_GRACE_SEC + 1
    controller.dumps = [dump_of((CLIENT1_KEY, 180, 220))]
    controller.calls = 0
    collector.cycle()

    assert collector._today == utc_day(day_two)
    # Counting resumes on the new day rather than stopping with the old one.
    monkeypatch.undo()
    controller.dumps = [dump_of((CLIENT1_KEY, 200, 240))]
    controller.calls = 0
    collector.cycle()
    collector._store_today()
    assert DailyTotal.objects.get(day=utc_day(day_two)).rx == 20


def test_a_client_with_no_id_yet_is_looked_up_before_the_day_turns_over(server_conf, monkeypatch):
    """The other half of what a rollover can lose.

    A client whose metadata row the last reconcile did not see keeps its bytes
    and waits for the flush after the next pass, which costs nothing at all -
    except at midnight, where there is no later flush that will ever carry them
    and the map is about to be dropped. So the ids that are missing are looked
    up on the one pass that cannot afford to wait for them.
    """
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)
    collector._load_today()
    collector._next_flush = time.monotonic() + 3600
    collector.cycle()
    # Polled, but no reconcile has run, so the loop has no id to write against.
    assert collector._meta_ids == {}
    assert collector._store_client_today() is True
    assert client_row() == (0, 0)

    clock[0] = day_one + timedelta(seconds=20)
    collector.cycle()

    assert ClientDaily.objects.get(day=utc_day(day_one)).rx == 100


def test_a_client_deleted_before_midnight_does_not_stall_the_rollover(server_conf, monkeypatch):
    """The other way the id map can be wrong when the day turns over.

    A client removed since the last reconcile leaves an id behind that no longer
    points at anything, and writing a history row against it fails the foreign
    key. Mid-day the next pass rebuilds the map without it and the flush after
    that goes through; at midnight there is no next flush, so it would fail
    every poll until the grace period ran out and take the clients that could
    have been written down with it.
    """
    day_one = timezone.now().replace(hour=23, minute=59, second=50, microsecond=0)
    clock = [day_one]
    monkeypatch.setattr("apps.stats.management.commands.collector.timezone.now", lambda: clock[0])

    other = store.add_client("phone")
    ClientMeta.objects.create(public_key=other.public_key, name="phone")
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200), (other.public_key, 10, 20))])
    collector = Collector(controller=controller, once=True)
    collector._load_today()
    collector._next_flush = time.monotonic() + 3600
    collector._reconcile()
    collector.cycle()
    assert other.public_key in collector._meta_ids

    # Removed in the panel, in the last seconds of the day.
    ClientMeta.objects.filter(public_key=other.public_key).delete()

    clock[0] = day_one + timedelta(seconds=20)
    collector.cycle()

    assert collector._today == utc_day(clock[0]), "the day turned over rather than stalling"
    # The client that still exists keeps its day; the one that went takes its
    # bytes with it, because its history went when its metadata row did.
    finished = ClientDaily.objects.filter(day=utc_day(day_one))
    assert [(row.meta.public_key, row.rx, row.tx) for row in finished] == [(CLIENT1_KEY, 100, 200)]


def test_a_refused_write_is_retried_with_what_has_moved_since(server_conf, monkeypatch):
    """The row holds an absolute total, so a retry cannot double count - and a
    write that never lands loses nothing but the ten seconds before the next."""
    controller = ScriptedController([dump_of((CLIENT1_KEY, 100, 200))])
    collector = Collector(controller=controller, once=True)

    def refuse(*args, **kwargs):
        raise DatabaseError("attempt to write a readonly database")

    monkeypatch.setattr(DailyTotal.objects, "update_or_create", refuse)
    collector.cycle()
    assert DailyTotal.objects.count() == 0
    assert collector._today_dirty is True

    monkeypatch.undo()
    controller.dumps = [dump_of((CLIENT1_KEY, 150, 275))]
    controller.calls = 0
    collector.cycle()
    collector.shutdown()

    assert today_row() == (150, 275)


# -------------------------------------------------------------- enforcement


def test_a_client_over_its_quota_is_disabled_and_comes_back(server_conf):
    """Disabling writes the config marker; the reason lives in the database.

    Raising the quota has to bring the client back by itself, because the
    collector is the only thing watching - and an admin who raised a limit and
    saw nothing happen would switch it back on by hand and lose the reason.
    """
    client = store.add_client("phone")
    ClientMeta.objects.create(public_key=client.public_key, name="phone", quota_bytes=1000)
    controller = ScriptedController([dump_of((client.public_key, 2000, 0))])

    run_once(controller)

    assert store.get_client("phone").enabled is False
    assert "# Disabled = " in conf_text(server_conf)
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"
    # The marker is enforcement only because the stripped config drops the peer.
    assert client.public_key not in strip_conf(parse_conf(conf_text(server_conf)))

    ClientMeta.objects.filter(public_key=client.public_key).update(quota_bytes=10**9)
    run_once(controller)

    assert store.get_client("phone").enabled is True
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == ""
    assert client.public_key in strip_conf(parse_conf(conf_text(server_conf)))


def _watched(server_conf, name: str, **meta: object) -> tuple[object, ClientMeta]:
    """A client with metadata, ready to be driven cycle by cycle."""
    client = store.add_client(name)
    row = ClientMeta.objects.create(public_key=client.public_key, name=name, **meta)
    return client, row


def test_a_quota_crossed_between_passes_is_caught_on_the_next_poll(server_conf):
    """The whole point of spending the limit in the poll loop. A minute of a
    hundred megabit link is three quarters of a gigabyte past the line, and on a
    ten gigabyte allowance that is not a limit anybody could hand out."""
    client, _ = _watched(server_conf, "phone", quota_bytes=1000)
    controller = ScriptedController(
        [dump_of((client.public_key, 100, 0)), dump_of((client.public_key, 2000, 0))]
    )
    collector = Collector(controller=controller)

    collector.cycle()
    assert store.get_client("phone").enabled is True

    # No reconcile this time round: what acts is the number left in memory.
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"
    assert client.public_key not in strip_conf(parse_conf(conf_text(server_conf)))


def test_a_switch_that_failed_is_tried_again_on_the_next_poll(server_conf, monkeypatch):
    """The switch is what actually revokes the key, and it can fail: the config
    lock is shared with every other writer, which may hold it for as long as
    its own budget allows, and `awg` itself can error. Believing it anyway would leave
    the watch marked disabled while the peer was still on the interface - and
    _spend skips a watch that is not enabled, so the one client known to be over
    its limit would be the one client nothing was counting any more. It would go
    on transferring until the reconcile came round, which is the delay the poll
    loop exists to remove."""
    client, _ = _watched(server_conf, "phone", quota_bytes=1000)
    controller = ScriptedController(
        [
            dump_of((client.public_key, 100, 0)),
            dump_of((client.public_key, 2000, 0)),
            dump_of((client.public_key, 3000, 0)),
        ]
    )
    collector = Collector(controller=controller)
    collector.cycle()

    # Nothing from here on may depend on the reconcile: the poll loop has to
    # recover on its own, which is the whole of what this is about.
    collector._next_enforce = time.monotonic() + 3600

    switch = store.set_clients_enabled

    def refuse(_names, _enabled):
        raise LockTimeout("another operation is still holding the lock")

    monkeypatch.setattr(store, "set_clients_enabled", refuse)
    collector.cycle()
    assert store.get_client("phone").enabled is True

    # Put back by hand rather than with undo(), which would also lift the
    # patches the fixture holds the config path open with.
    monkeypatch.setattr(store, "set_clients_enabled", switch)
    collector.cycle()

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"
    assert client.public_key not in strip_conf(parse_conf(conf_text(server_conf)))


def test_the_reading_that_crossed_the_limit_is_written_before_the_key_goes(server_conf):
    """The poll loop enforces on the mirror, and the mirror is only ever written
    to traffic.db by folding the dump it came from. Taking the peer off the
    interface removes it from every dump after that, so unless the reading is
    folded first the bytes that tripped the limit are never recorded anywhere:
    the panel goes on showing a client switched off for its quota at eighty
    percent of it, and traffic.db under-reports the same client for
    good."""
    client, row = _watched(server_conf, "phone", quota_bytes=1000)
    controller = ScriptedController(
        [
            dump_of((client.public_key, 100, 0)),
            dump_of((client.public_key, 2000, 0)),
            # Revoked, so the kernel no longer holds it.
            dump_of(),
        ]
    )
    collector = Collector(controller=controller)

    collector.cycle()
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()
    assert store.get_client("phone").enabled is False

    # More polls, and then the ten second flush comes round to a dump this peer
    # is not in.
    collector.cycle()
    collector._flush_traffic_db()

    used = merge.used_bytes(row, traffic.read_db().get(client.public_key))
    assert used >= row.quota_bytes


def test_a_client_switched_off_for_its_quota_is_not_let_straight_back_on(server_conf):
    """The reconcile reads traffic.db, so anything the poll loop enforced on and
    did not write there reads back as a client inside its limits - and this pass
    switches those back on. A minute later it is over again, and the client
    spends its life going on and off while the panel disagrees with itself about
    whether the limit was reached."""
    client, _ = _watched(server_conf, "phone", quota_bytes=1000)
    controller = ScriptedController(
        [
            dump_of((client.public_key, 100, 0)),
            dump_of((client.public_key, 2000, 0)),
            dump_of(),
        ]
    )
    collector = Collector(controller=controller)

    collector.cycle()
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()
    collector.cycle()
    collector._flush_traffic_db()
    collector._reconcile()

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"


def test_a_limit_raised_between_passes_is_read_before_anything_is_switched_off(server_conf):
    """The figure in memory is a hint and is allowed to be wrong. An admin who
    raised a limit thirty seconds ago has already had the client switched back on
    by the request that did it, and a poll loop acting on its stale copy would
    switch it straight off again - the exact fight this must not pick."""
    client, row = _watched(server_conf, "phone", quota_bytes=1000)
    controller = ScriptedController(
        [dump_of((client.public_key, 100, 0)), dump_of((client.public_key, 2000, 0))]
    )
    collector = Collector(controller=controller)
    collector.cycle()

    ClientMeta.objects.filter(pk=row.pk).update(quota_bytes=10**9)
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()

    assert store.get_client("phone").enabled is True
    # And corrected on the spot, so the same candidate does not arrive again on
    # this poll and every poll until the next pass.
    assert collector._watch[client.public_key].remaining > 0


def test_a_pass_does_not_hand_back_the_headroom_the_poll_loop_has_spent(server_conf):
    """The reconcile rebuilds every watch, and what it rebuilds them from decides
    how much of the fast path survives the rebuild.

    traffic.db is written every ten seconds and the mirror moves every two, so a
    pass that seeded `remaining` from the file alone credited the client with
    everything it had transferred since the last flush - once a minute on the
    default interval, and once every five seconds if `enforceIntervalSec` is
    turned down to its floor, which is fast enough to refill the allowance faster
    than the poll loop can spend it. The limit then waits for the file to cross
    on its own clock, which is the delay the poll loop exists to remove.
    """
    client, _ = _watched(server_conf, "phone", quota_bytes=10_000)
    controller = ScriptedController(
        [
            dump_of((client.public_key, 1_000, 0)),
            dump_of((client.public_key, 9_500, 0)),
            dump_of((client.public_key, 10_200, 0)),
        ]
    )
    collector = Collector(controller=controller)

    # First cycle flushes, so the file and the mirror agree at 1,000.
    collector.cycle()
    assert traffic.read_db()[client.public_key].cum_rx == 1_000

    # From here the file is frozen and only the mirror moves: 9,500 of a 10,000
    # allowance is spent, and traffic.db still says 1,000.
    collector._next_flush = time.monotonic() + 3600
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()
    assert traffic.read_db()[client.public_key].cum_rx == 1_000
    assert collector._watch[client.public_key].remaining == 500

    # A pass now, with the client still honestly inside its limit, so nothing
    # here switches anything off and what is left is purely what it rebuilt.
    collector._reconcile()
    assert store.get_client("phone").enabled is True
    assert collector._watch[client.public_key].remaining == 500

    # And the 700 bytes that cross the line are still the ones that trip it.
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"


def test_a_pass_still_reads_what_was_folded_in_behind_it(server_conf):
    """The other half of the same seed, and why it is a maximum rather than a
    swap. The PreDown hook writes traffic.db under the same lock and
    tells this process nothing, so the file holds bytes the mirror has never
    seen - and a pass that trusted only its own memory would enforce as though
    they had not happened."""
    client, _ = _watched(server_conf, "phone", quota_bytes=10_000)
    controller = ScriptedController([dump_of((client.public_key, 100, 0))])
    collector = Collector(controller=controller)
    collector.cycle()

    # Somebody else's fold: the file is now well past the limit and the mirror,
    # which is what the collector would have believed on its own, is at 100.
    db = traffic.read_db()
    db[client.public_key] = Counters(cum_rx=50_000, cum_tx=0, last_rx=50_000, last_tx=0)
    traffic.write_db(db)

    collector._reconcile()

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"


def test_a_client_with_no_limit_is_never_a_candidate(server_conf):
    """Nothing to spend, so nothing to look up however much it transfers."""
    client, _ = _watched(server_conf, "phone", quota_bytes=0)
    controller = ScriptedController(
        [dump_of((client.public_key, 0, 0)), dump_of((client.public_key, 10**12, 10**12))]
    )
    collector = Collector(controller=controller)
    collector.cycle()
    collector._next_enforce = time.monotonic() + 3600
    collector.cycle()

    assert store.get_client("phone").enabled is True
    assert collector._watch[client.public_key].remaining == float("inf")


def test_an_expiry_before_the_next_pass_pulls_it_forward(server_conf):
    """A client that has stopped transferring still runs out of days, and no
    amount of watching the counters finds that. Asking every client every minute
    is the thing being avoided, so the earliest date is worked out once and the
    next pass is booked for it."""
    client, _ = _watched(
        server_conf, "phone", expires_at=timezone.now() + timedelta(seconds=5), quota_bytes=0
    )
    collector = Collector(controller=ScriptedController([dump_of((client.public_key, 1, 1))]))

    collector.cycle()

    # Not the sixty seconds the interval would have given it.
    assert collector._next_enforce <= time.monotonic() + 6


def test_an_expiry_well_past_the_next_pass_changes_nothing(server_conf):
    """Only ever earlier. The reconcile still has everything else to correct."""
    client, _ = _watched(
        server_conf, "phone", expires_at=timezone.now() + timedelta(days=30), quota_bytes=0
    )
    collector = Collector(controller=ScriptedController([dump_of((client.public_key, 1, 1))]))

    collector.cycle()

    assert collector._next_enforce > time.monotonic() + 30


def test_a_pass_that_stops_several_clients_rewrites_the_config_once(server_conf, monkeypatch):
    """Clients cross a limit one at a time until an admin lowers a default quota,
    and then the whole set crosses in the same instant. One rewrite each would be
    one full parse of the server config each, sequentially, with the config lock
    held across the lot - a stall every panel request on the box waits out."""
    peers = [store.add_client(f"phone{n}") for n in range(3)]
    for n, peer in enumerate(peers):
        ClientMeta.objects.create(public_key=peer.public_key, name=f"phone{n}", quota_bytes=1000)
    controller = ScriptedController([dump_of(*((peer.public_key, 2000, 0) for peer in peers))])

    writes: list[int] = []
    original = store._write_conf
    monkeypatch.setattr(
        store, "_write_conf", lambda conf: (writes.append(len(conf.peers)), original(conf))[1]
    )

    run_once(controller)

    assert len(writes) == 1, writes
    stripped = strip_conf(parse_conf(conf_text(server_conf)))
    assert all(peer.public_key not in stripped for peer in peers)
    assert set(
        ClientMeta.objects.filter(disabled_reason="quota").values_list("name", flat=True)
    ) == {"phone0", "phone1", "phone2"}


def test_a_pass_with_nothing_to_change_does_not_rewrite_the_config(server_conf, monkeypatch):
    """Every pass re-asserts a decision the last one already applied. Writing the
    file to say what it already says would move the timestamp the client index
    stamps, and put its rebuild on a sixty second timer for the life of the
    panel."""
    client = store.add_client("phone")
    ClientMeta.objects.create(public_key=client.public_key, name="phone", quota_bytes=1000)
    controller = ScriptedController([dump_of((client.public_key, 2000, 0))] * 2)
    run_once(controller)
    before = conf_text(server_conf)

    writes: list[int] = []
    monkeypatch.setattr(store, "_write_conf", lambda conf: writes.append(len(conf.peers)))

    run_once(controller)

    assert writes == []
    assert conf_text(server_conf) == before


def test_a_client_past_its_expiry_is_disabled_with_that_reason(server_conf):
    client = store.add_client("phone")
    ClientMeta.objects.create(
        public_key=client.public_key,
        name="phone",
        quota_bytes=0,
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    run_once(ScriptedController([dump_of((client.public_key, 10, 10))]))

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "expired"
    assert client.public_key not in strip_conf(parse_conf(conf_text(server_conf)))


def test_a_lapsed_client_an_admin_switched_back_on_is_left_on(server_conf):
    """The panel's switch has to outlast this loop or it does nothing at all.

    Before the reprieve was written down, switching an expired client on bought
    it one enforcement interval and no more, which made a passed date and a
    switched-off peer the same fact - and left an admin who wanted to give
    somebody a few more days no way to do it but to edit the date they had run
    out on.
    """
    client = store.add_client("phone")
    expires = timezone.now() - timedelta(days=1)
    ClientMeta.objects.create(
        public_key=client.public_key,
        name="phone",
        expires_at=expires,
        expiry_override_at=expires,
    )

    run_once(ScriptedController([dump_of((client.public_key, 10, 10))]))

    assert store.get_client("phone").enabled is True
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == ""
    assert client.public_key in strip_conf(parse_conf(conf_text(server_conf)))


def test_moving_the_date_puts_the_expiry_back_in_force(server_conf):
    """The reprieve forgives the one date it was given. A client handed a new one
    and then left to lapse against that is switched off like any other - which is
    also what makes the record safe to keep: it cannot go on excusing a client
    forever without somebody deciding so again.
    """
    client = store.add_client("phone")
    expires = timezone.now() - timedelta(days=1)
    ClientMeta.objects.create(
        public_key=client.public_key,
        name="phone",
        expires_at=expires,
        expiry_override_at=expires,
    )
    controller = ScriptedController([dump_of((client.public_key, 10, 10))])
    run_once(controller)
    assert store.get_client("phone").enabled is True

    ClientMeta.objects.filter(public_key=client.public_key).update(
        expires_at=timezone.now() - timedelta(hours=1)
    )
    run_once(controller)

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "expired"
    assert client.public_key not in strip_conf(parse_conf(conf_text(server_conf)))


def test_a_client_kept_on_past_its_date_is_still_stopped_by_its_quota(server_conf):
    """The decision an admin made was about the calendar. Data is measured rather
    than decided, and a client that has used everything it was given is stopped
    on the evidence - the way to keep that one going is to raise the limit or
    clear the counter.
    """
    client = store.add_client("phone")
    expires = timezone.now() - timedelta(days=1)
    ClientMeta.objects.create(
        public_key=client.public_key,
        name="phone",
        quota_bytes=1000,
        expires_at=expires,
        expiry_override_at=expires,
    )

    run_once(ScriptedController([dump_of((client.public_key, 2000, 0))]))

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "quota"


def test_a_client_inside_its_limits_is_left_alone(server_conf):
    client = store.add_client("phone")
    ClientMeta.objects.create(
        public_key=client.public_key,
        name="phone",
        quota_bytes=10**9,
        expires_at=timezone.now() + timedelta(days=1),
    )

    run_once(ScriptedController([dump_of((client.public_key, 2000, 3000))]))

    assert store.get_client("phone").enabled is True
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == ""


def test_a_client_disabled_by_hand_is_not_re_enabled(server_conf):
    """A "# Disabled" marker written by hand and the panel's own switch both mean somebody decided
    this. Turning it back on because it happens to be under its quota would undo
    that silently."""
    client = store.add_client("phone")
    ClientMeta.objects.create(public_key=client.public_key, name="phone", disabled_reason="manual")
    store.set_client_enabled("phone", False)

    run_once(ScriptedController([dump_of((client.public_key, 1, 1))]))

    assert store.get_client("phone").enabled is False
    assert ClientMeta.objects.get(public_key=client.public_key).disabled_reason == "manual"


def test_a_disabled_peer_found_live_is_taken_off_the_interface(server_conf):
    """A bring-up loads the whole config, which puts a disabled peer's key back
    on the interface. This loop is what takes it off again."""
    client = store.add_client("phone")
    store.set_client_enabled("phone", False)
    # The dump still lists it, which is exactly the state that has to be fixed.
    controller = ScriptedController([dump_of((client.public_key, 5, 5))])

    run_once(controller)

    assert controller.synced, "the collector did not re-apply the configuration"
    assert client.public_key not in controller.synced[-1]


# ----------------------------------------------------------------------- rates


def live_peer(collector: Collector, key: str = CLIENT1_KEY) -> dict:
    """One peer out of the blob the last cycle wrote."""
    return json.loads(live_state_file().read_text(encoding="utf-8"))["peers"][key]


def two_cycles(controller: ScriptedController) -> Collector:
    """Two polls of one collector, which is the fewest that can report a rate."""
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    collector.cycle()
    return collector


def test_a_peer_that_is_there_reports_what_it_is_moving(server_conf):
    """The control for the test below: an ordinary client keeps its rate."""
    collector = two_cycles(
        ScriptedController(
            [
                dump_of((CLIENT1_KEY, 1_000, 2_000)),
                dump_of((CLIENT1_KEY, 3_000, 9_000)),
            ]
        )
    )

    peer = live_peer(collector)
    assert peer["online"] is True
    assert peer["rateRx"] > 0 and peer["rateTx"] > 0


def test_a_peer_that_has_gone_reports_no_rate_however_much_is_sent_to_it(server_conf):
    """The kernel keeps writing to a client that has been switched off.

    Anything on this side still holding packets for a departed client's address
    makes the tunnel retry a handshake at its last endpoint every few seconds,
    and every one of those is bytes out with nothing coming back. Reported as a
    rate, they read as a client downloading a few hundred bytes a second forever
    - flickering to zero and back beside a last-seen column that is correctly
    counting the minutes since anything was heard from it.

    So the peer's own packets decide. Nothing is subtracted from the accounting:
    the bytes were sent and they stay counted against the client they were
    addressed to, which is what the totals below check.
    """
    gone = int(time.time()) - 600
    controller = ScriptedController(
        [
            dump_of((CLIENT1_KEY, 500, 1_000), handshake=gone),
            # Only tx moves: the server is talking, the client is not.
            dump_of((CLIENT1_KEY, 500, 9_000), handshake=gone),
        ]
    )
    collector = Collector(controller=controller, once=True)
    collector.cycle()
    # Ten minutes since the last thing was heard from it, which is where the
    # mirror of that would stand on a server whose client went quiet while this
    # process was watching. Set by hand because the test has one clock and the
    # first poll of any peer necessarily hears from it.
    collector._last_rx[CLIENT1_KEY] = gone
    collector.cycle()

    peer = live_peer(collector)
    assert peer["online"] is False
    assert peer["rateRx"] == 0 and peer["rateTx"] == 0

    blob = json.loads(live_state_file().read_text(encoding="utf-8"))
    assert blob["totalRateRx"] == 0 and blob["totalRateTx"] == 0

    collector.shutdown()
    assert traffic.read_db()[CLIENT1_KEY].cum_tx == 9_000


# ----------------------------------------------------------------- last seen


def test_the_last_handshake_is_recorded_for_a_peer_with_no_metadata(server_conf):
    """A client added from the CLI has no row until somebody edits it in the
    panel, and "when was this last connected" must not wait for that."""
    seen = int(time.time()) - 30
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200), handshake=seen)]))

    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert meta.last_handshake == seen
    assert meta.last_endpoint == "198.51.100.7:51820"


def test_recording_a_handshake_leaves_the_rest_of_the_metadata_alone(server_conf):
    """The write is an upsert carrying whole rows, so the columns it does not
    name have to come back untouched - quota and offsets among them."""
    expires = timezone.now() + timedelta(days=30)
    ClientMeta.objects.create(
        public_key=CLIENT1_KEY,
        name="client1",
        quota_bytes=5 * 1024**3,
        expires_at=expires,
        offset_rx=7,
        offset_tx=9,
        note="the one in the drawer",
    )
    seen = int(time.time()) - 30

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200), handshake=seen)]))

    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert meta.last_handshake == seen
    assert (meta.quota_bytes, meta.offset_rx, meta.offset_tx) == (5 * 1024**3, 7, 9)
    assert meta.note == "the one in the drawer"
    assert meta.expires_at == expires
    # One row per peer, not one per handshake: the upsert must find the row it
    # already has. (The disabled fixture peer has a row of its own by now, put
    # there by the creation-date backfill, so this counts only this client's.)
    assert ClientMeta.objects.filter(public_key=CLIENT1_KEY).count() == 1


def test_the_creation_date_is_copied_out_of_the_config(server_conf):
    """A peer that arrived outside the panel has its date in one place only.

    The comment is the truth while it is there, but nothing can reconstruct it
    once a hand edit or a restore has dropped it, so the collector takes a copy
    of every one it finds.
    """
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    row = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert row.created_at is not None
    assert row.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-08-04T18:00:00Z"


def test_a_recorded_creation_date_is_never_rewritten(server_conf):
    """The copy is the fallback for a comment that has gone; a comment that comes
    back changed - a restore, a hand edit - does not get to overwrite it."""
    stored = timezone.now() - timedelta(days=400)
    ClientMeta.objects.create(public_key=CLIENT1_KEY, name="client1", created_at=stored)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert ClientMeta.objects.get(public_key=CLIENT1_KEY).created_at == stored


def test_a_peer_with_no_creation_comment_is_not_given_a_date(server_conf):
    """An invented date would be indistinguishable from a real one afterwards."""
    text = conf_text(server_conf)
    server_conf.write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("# Created")) + "\n",
        encoding="utf-8",
    )

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert ClientMeta.objects.get(public_key=CLIENT1_KEY).created_at is None


def test_a_restarted_interface_does_not_erase_the_last_handshake(server_conf):
    """The kernel reports 0 for a peer that has not connected since the
    interface came up, which is the state this whole column exists to survive."""
    seen = int(time.time()) - 45
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200), handshake=seen)]))

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200), handshake=0)]))

    meta = ClientMeta.objects.get(public_key=CLIENT1_KEY)
    assert meta.last_handshake == seen
    assert meta.last_endpoint == "198.51.100.7:51820"


def test_an_unchanged_handshake_is_not_written_again(server_conf):
    """What keeps this cheap: a peer re-handshakes about every two minutes, so
    almost every poll finds nothing to write and issues no statement at all."""
    dump = dump_of((CLIENT1_KEY, 100, 200), handshake=int(time.time()) - 30)
    collector = run_once(ScriptedController([dump]))

    collector._note_presence(dump)

    assert collector._unsaved == {}


# ------------------------------------------------- how long the tunnel has run

# The kernel stamps no creation time on an interface, so "up since" is this
# process's own observation, remembered through live.json and thrown away as
# soon as it stops describing the interface in front of it. Everything below is
# about that second half: a figure this one cannot check is a figure it must not
# keep, because an uptime that survives the restart it should have noticed is
# worse than one that starts counting again.


@pytest.fixture
def netdev(tmp_path, monkeypatch):
    """A fake /sys/class/net/awg0 whose index a test can change, as a restart does."""
    root = tmp_path / "sys" / "class" / "net"
    iface = root / "awg0"
    iface.mkdir(parents=True)
    (iface / "ifindex").write_text("12\n", encoding="utf-8")
    monkeypatch.setattr(sysinfo, "SYS_CLASS_NET", root)
    return iface


def read_blob() -> dict:
    return json.loads(live_state_file().read_text(encoding="utf-8"))


def write_blob(**changes) -> None:
    """Rewrite live.json the way a previous run of the collector left it."""
    live_state_file().write_text(json.dumps({**read_blob(), **changes}), encoding="utf-8")


def test_the_blob_says_since_when_the_tunnel_has_been_up(server_conf, netdev):
    """The first cycle that finds the interface up is the moment recorded."""
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    blob = read_blob()
    assert blob["ifaceUp"] is True
    assert abs(blob["ifaceSince"] - time.time()) < 60
    assert blob["ifaceIndex"] == 12


def test_a_tunnel_that_is_down_claims_no_uptime(server_conf, netdev):
    """Zero rather than a stale timestamp: the card and the banner have to agree."""
    run_once(ScriptedController([]))

    blob = read_blob()
    assert blob["ifaceUp"] is False
    assert blob["ifaceSince"] == 0
    assert blob["ifaceIndex"] == 0


def test_a_collector_restart_keeps_an_uptime_the_tunnel_never_lost(
    server_conf, netdev, monkeypatch
):
    """Upgrading the panel must not make the tunnel look newly started."""
    # A box up for a day, so the hour this test remembers falls inside its boot.
    # Left to the real clock the case would only hold on a machine that happened
    # to have been up longer than the tunnel, and a fresh CI runner is not one.
    monkeypatch.setattr(sysinfo, "uptime", lambda: 86400.0)
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    earlier = int(time.time()) - 3600
    write_blob(ifaceSince=earlier)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert read_blob()["ifaceSince"] == earlier


def test_a_tunnel_restarted_while_nobody_watched_starts_counting_again(
    server_conf, netdev, monkeypatch
):
    """`awg-quick down && up` gives the netdev a new index, which is the tell.

    Without it a collector that was stopped across the restart would restore a
    start time from a tunnel that no longer exists and report an uptime through
    an outage every client noticed.
    """
    # The index has to be what rejects the remembered moment, so put the boot
    # far enough back that the other guard has nothing to say about it.
    monkeypatch.setattr(sysinfo, "uptime", lambda: 86400.0)
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    earlier = int(time.time()) - 3600
    write_blob(ifaceSince=earlier)
    (netdev / "ifindex").write_text("13\n", encoding="utf-8")

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    blob = read_blob()
    assert blob["ifaceIndex"] == 13
    assert abs(blob["ifaceSince"] - time.time()) < 60


def test_a_start_time_from_before_this_boot_is_not_believed(server_conf, netdev, monkeypatch):
    """Indexes start again from low numbers after a reboot, so one can match by
    coincidence; a moment before the box came up cannot be about this tunnel."""
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    monkeypatch.setattr(sysinfo, "uptime", lambda: 600.0)
    write_blob(ifaceSince=int(time.time()) - 3600)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert abs(read_blob()["ifaceSince"] - time.time()) < 60


def test_an_interface_with_no_index_still_reports_an_uptime(server_conf, monkeypatch):
    """A container without /sys/class/net loses the fingerprint, not the figure.

    The uptime then restarts with the collector, because there is nothing left
    to prove the remembered one still belongs to this interface.
    """
    monkeypatch.setattr(sysinfo, "SYS_CLASS_NET", live_state_file().parent / "nothing")

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    blob = read_blob()
    assert blob["ifaceIndex"] == 0
    assert abs(blob["ifaceSince"] - time.time()) < 60


# ------------------------------------------------------------- housekeeping


def test_a_pass_sweeps_up_the_sessions_that_have_expired(server_conf):
    """The loop is the only thing that reaps them, so the wiring is the risk.

    What a sweep should take is apps.accounts.sessions' rule and is tested
    against that rule in test_api_sessions.py. What is asserted here is only
    that the loop reaches it at all - a timer that is never checked fails
    silently and forever, and neither side can see that on its own.
    """
    user = get_user_model().objects.create_user("admin")
    session = SessionStore()
    session.create()
    Session.objects.filter(session_key=session.session_key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    row = LoginSession.objects.create(
        session_key=session.session_key,
        user=user,
        created_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    call_command("collector", "--once")

    assert not Session.objects.filter(session_key=session.session_key).exists()
    assert not LoginSession.objects.filter(pk=row.pk).exists()


def test_a_database_that_cannot_be_swept_does_not_stop_the_pass(server_conf, monkeypatch):
    """Housekeeping is worth less than the accounting it shares a cycle with."""
    monkeypatch.setattr(
        "apps.stats.management.commands.collector.sessions.sweep_expired",
        lambda: (_ for _ in ()).throw(DatabaseError("database is locked")),
    )

    call_command("collector", "--once")

    # The cycle still did its real work.
    assert today_row() != (0, 0)


# ------------------------------------------------------------- staying alive


def test_a_tool_failure_is_not_fatal(server_conf):
    """`awg` missing or the interface down is a normal state for this process:
    it is what a server looks like between install.sh and the first bring-up."""
    traffic.write_db({CLIENT1_KEY: Counters(10, 20, 10, 20)})
    controller = ScriptedController([], raises_on=[1], error=ToolError(["awg", "show"], "no dev"))

    run_once(controller)

    blob = json.loads(live_state_file().read_text(encoding="utf-8"))
    assert blob["ifaceUp"] is False
    assert blob["peers"] == {}
    # Nothing was read, so nothing may be written: a flush of empty counters
    # would be indistinguishable from every client having used nothing.
    assert traffic.read_db() == {CLIENT1_KEY: Counters(10, 20, 10, 20)}


def test_the_loop_survives_a_controller_that_raises(server_conf, monkeypatch):
    """One bad cycle must not end the process.

    The controller here fails on every odd poll with an error the collector has
    no handler for, which is the case that matters: a handled failure proves
    nothing about the ones nobody anticipated.
    """
    # The real 2s poll would make this test take ten seconds to prove a point
    # about cycles, not about clocks.
    monkeypatch.setattr(Collector, "_poll_sec", lambda self: 0.01)
    controller = ScriptedController(
        [dump_of((CLIENT1_KEY, 400, 900))], raises_on=[1, 3, 5], stop_after=6
    )

    collector = Collector(controller=controller, once=False)
    controller.stop = collector.stop
    collector.run()

    assert controller.calls >= 6
    # The good cycles in between still did their job.
    assert today_row() == (400, 900)
    assert json.loads(live_state_file().read_text(encoding="utf-8"))["peers"]
    assert traffic.read_db()[CLIENT1_KEY] == Counters(400, 900, 400, 900)


# --------------------------------------------------------------------- shaping
#
# The third place a bandwidth ceiling is applied, and the only one that catches
# what the other two cannot see. A request applies the client it changed and the
# PostUp hook applies all of them at bring-up; neither covers a hook that never
# fired, a config too old to carry one, or an operator who removed a qdisc by
# hand to test something. Every one of those leaves a server that looks entirely
# healthy and is holding nobody to the rate they were sold.


class FakeTc:
    """tc, recorded rather than run. `state` answers the reads by command prefix."""

    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.state = state or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.inputs.append(kwargs.get("input") or "")
        line = " ".join(cmd)
        best = ""
        for prefix in self.state:
            if line.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        return _Proc(self.state.get(best, ""))

    @property
    def lines(self) -> list[str]:
        """Every command that reached tc, with the batched ones expanded."""
        out: list[str] = []
        for call, payload in zip(self.calls, self.inputs, strict=True):
            if "-batch" in call:
                out.extend(line for line in payload.splitlines() if line)
            else:
                out.append(" ".join(call[1:]))
        return out

    def matching(self, *words: str) -> list[str]:
        return [line for line in self.lines if all(word in line for word in words)]


class _Proc:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


# A tunnel with the whole download structure on it - the root qdisc and the
# filter that hashes into the v4 table - already holding the one ceiling this
# server wants. Both pieces, because "attached" is a question about every one of
# them: a fixture that answered only the root would put this on a server the
# shaper considers half built and rebuilds on every pass.
ATTACHED_AND_HOLDING = {
    "tc qdisc show dev awg0 root": "qdisc htb 1: root refcnt 2 default 2",
    "tc filter show dev awg0 parent 1: protocol ip": "filter pref 1 u32 fh 800: link 10:",
    "tc -j class show dev awg0": json.dumps(
        [{"class": "htb", "handle": "1:12", "rate": 10_000_000}]
    ),
}


@pytest.fixture
def tc(monkeypatch):
    """A host that can shape, with nothing attached to begin with."""
    fake = FakeTc()
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(shaper.subprocess, "run", fake)
    return fake


def shaped(key: str, down: int, **fields: object) -> None:
    """Give a peer a ceiling in the database, without touching the kernel."""
    ClientMeta.objects.update_or_create(public_key=key, defaults={"down_bps": down, **fields})


def test_a_pass_puts_back_a_ceiling_the_kernel_is_not_holding(server_conf, tc):
    """The case the whole pass exists for: the interface came up without them."""
    settings_store.set_many({"shaperOn": "1"})
    shaped(CLIENT1_KEY, 10_000_000)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert tc.matching("qdisc replace dev awg0 root", "htb")
    assert tc.matching("class replace dev awg0", "rate 10000000bit")


def test_a_pass_writes_nothing_when_the_kernel_already_agrees(server_conf, tc):
    """Which is nearly every pass, and is why this is affordable once a minute."""
    settings_store.set_many({"shaperOn": "1"})
    shaped(CLIENT1_KEY, 10_000_000)
    tc.state = ATTACHED_AND_HOLDING

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert not tc.matching("class replace")
    assert not tc.matching("qdisc del")


def test_a_pass_touches_no_qdisc_at_all_while_shaping_is_off(server_conf, tc):
    """A server that never asked for this has to be the server it was before."""
    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))
    assert not tc.matching("replace")
    assert not tc.matching("del")


def test_a_peer_the_pass_has_just_revoked_leaves_the_structure_with_it(server_conf, tc):
    """Shaping runs after the switching and reads the peers as this pass leaves them.

    A second client keeps the structure standing, so what is asserted is the
    expired client's own class going rather than the whole root coming down -
    which is what would happen, correctly but less informatively, if it were the
    only one left.
    """
    settings_store.set_many({"shaperOn": "1"})
    shaped(CLIENT1_KEY, 10_000_000, name="client1", expires_at=timezone.now() - timedelta(days=1))
    other = store.add_client("second")
    shaped(other.public_key, 5_000_000, name="second")
    tc.state = ATTACHED_AND_HOLDING

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert ClientMeta.objects.get(public_key=CLIENT1_KEY).disabled_reason == "expired"
    assert tc.matching("class del dev awg0", "1:12")
    # The structure stays up for the client that still wants it.
    assert not tc.matching("qdisc del dev awg0 root")
    assert tc.matching("class replace dev awg0", "rate 5000000bit")


def test_the_structure_comes_down_when_the_last_shaped_client_is_revoked(server_conf, tc):
    """And with it every class: there is nothing left for the tunnel's queue to do."""
    settings_store.set_many({"shaperOn": "1"})
    shaped(CLIENT1_KEY, 10_000_000, name="client1", expires_at=timezone.now() - timedelta(days=1))
    tc.state = ATTACHED_AND_HOLDING

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 100, 200))]))

    assert tc.matching("qdisc del dev awg0 root")
    assert not tc.matching("class replace dev awg0", "1:12")


def test_a_tc_that_cannot_be_run_does_not_take_the_cycle_with_it(server_conf, monkeypatch):
    """Like every other outside touch here: the next pass reaches the same verdict."""
    settings_store.set_many({"shaperOn": "1"})
    shaped(CLIENT1_KEY, 10_000_000)
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper.shutil, "which", lambda name: f"/usr/sbin/{name}")

    def explode(*args, **kwargs):
        raise OSError("tc is not where it was")

    monkeypatch.setattr(shaper.subprocess, "run", explode)

    run_once(ScriptedController([dump_of((CLIENT1_KEY, 400, 900))]))

    # Everything else in the cycle still happened.
    assert today_row() == (400, 900)
    assert traffic.read_db()[CLIENT1_KEY] == Counters(400, 900, 400, 900)
