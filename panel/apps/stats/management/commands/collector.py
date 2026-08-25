"""The collector: the one process that talks to the interface on a schedule.

`manage.py collector`, run by awg-panel-collector.service. Everything it does
follows from a single decision: the kernel's per-peer counters only go up and
are reset by a restart, so somebody has to poll them, difference them and write
the result down. Once something is polling anyway, it may as well be the only
thing that does - which is why the web side never shells out to `awg` and reads
a file this process wrote instead.

Four jobs, on four clocks:

* every trafficPollSec (2s)  poll, difference, add to today's totals, spend each
                             client's remaining quota, write live.json
* every 10s                  store today's totals - the server's and each busy
                             client's - flush traffic.db, record any handshake
                             that moved, and finish a traffic wipe the panel has
                             asked for
* every enforceIntervalSec   reconcile with the files: re-assert disabled peers,
                             re-read every limit, put back any bandwidth ceiling
                             the kernel is not holding, record when a client the
                             panel did not add was created
* every 24h                  sweep up the signed-in sessions that have expired
                             and the traffic history that has aged out, which is
                             housekeeping for the panel rather than for the
                             tunnel and is here because nothing else runs

Enforcement is split across the first and the third of those, and the split is
the point. A limit is applied on the poll's clock, because the thing it is
applied to is bytes and bytes arrive on that clock: a quota checked once a
minute is one a client on a fast link can be the better part of a gigabyte past
before anything notices, and a "10 GB" that in practice means eleven is not a
limit an operator can hand to anybody. What makes that affordable is that a poll
does not look at the clients at all - it subtracts the deltas it has just
computed from a number per client held in memory, and only a client that runs
that number down is looked up properly.

The reconcile is what those numbers are built from, and it cannot become an
event, because what it corrects happens where nothing announces it: a bring-up
loads the whole config and puts every revoked key back on the interface, a hand
edit changes a limit, a restore replaces both files at once, another worker
writes while this process is mid-cycle. So it goes on re-reading everything on a
timer, and the memory it
fills is a cache that is always allowed to be wrong - every candidate it
produces is checked against the row and the counters before a peer is touched.

Expiry rides on the same pass rather than on a check per client per minute: the
earliest date still to come is worked out when the pass builds the table, and
the next pass is booked for it if it falls sooner.

traffic.db is shared with the PreDown hook, which folds in the last reading
before an interface goes down, so it is written through awg.traffic under the
config flock rather than directly, and
only every ten seconds: the file is rewritten whole, and doing that twice a
second on an SD card is how a VPS eats its own storage. Nothing is lost by
waiting, because the kernel's counters are cumulative and awg.traffic diffs
against the values already in the file.

That last property cuts both ways, and it is why each reading is folded in
exactly once and a cycle that could not read the interface folds nothing: a raw
value below the one on disk is awg.traffic's counter-reset rule, so re-sending
a reading that something else - the PreDown hook, say - has already improved on
does not repeat a delta, it invents a whole counter epoch.

Enforcement is here rather than in a request because it has to happen while
nobody is looking. It is also deliberately conservative: it only ever switches
off a client that is over a limit the admin set, and only ever switches one back
on if the panel itself was the one that switched it off. A client an admin
switched off, or one carrying a "# Disabled" marker somebody wrote by hand,
stays disabled.

The loop's contract with the rest of the system is that it does not stop. `awg`
missing, the interface down, the database locked, the config unreadable: each of
those is logged once, with a cooldown so a permanent fault cannot fill the
journal, and the next cycle runs anyway. The only thing that ends it is SIGTERM
or SIGINT, which flush traffic.db and exit 0.
"""

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from types import FrameType

from django.core.management.base import BaseCommand, CommandParser
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from apps.accounts import sessions
from apps.clients import index, merge, shaping
from apps.clients.merge import parse_created
from apps.clients.models import ClientMeta
from apps.events import kinds, recorder
from apps.panel import settings_store
from apps.stats import history, live
from apps.stats.models import ClientDaily, DailyTotal, TrafficReset, utc_day
from awg import clientsenv, lock, paths, store, sysinfo, traffic
from awg.controller import BaseController, Dump, get_controller
from awg.errors import AwgError

log = logging.getLogger(__name__)

# Settings and their fallbacks, for the case where the database cannot be read
# at all. They match apps.panel.defaults; the collector has to keep working
# before the first migrate, when settings_store answers with defaults anyway.
DEFAULT_POLL_SEC = 2.0
DEFAULT_ENFORCE_SEC = 60.0
DEFAULT_ONLINE_THRESHOLD = 180

# One database write and one traffic.db rewrite per ten seconds, whatever the
# poll rate. The deltas behind them are summed in memory as they arrive, so this
# is what bounds the loss from a kill -9 or a power cut: at most ten seconds of
# today's figure, and nothing at all on an orderly stop, which flushes on the
# way out.
FLUSH_SEC = 10.0

# A fault that persists gets one line every five minutes, not one per cycle.
ERROR_COOLDOWN_SEC = 300.0

# How long the midnight rollover will hold on to a finished day whose rows the
# database will not take. Long enough for every lock the panel itself takes, and
# short enough that giving up costs a rounding error against a day - which it
# has to do, because nothing is counted at all while the rollover is waiting.
ROLLOVER_GRACE_SEC = 300.0

# How often expired sessions are swept up. Slow on purpose: a session is
# unreachable the moment it expires, whatever becomes of the row, so the only
# thing this clock decides is how long dead rows sit there - and the point is
# that the table stays bounded, not that any particular row goes promptly.
SWEEP_SEC = 86400.0

# A cycle that overruns its poll interval must still yield, or the loop becomes
# a spin and the box has no CPU left for the tunnel.
MIN_SLEEP_SEC = 0.05

# ClientMeta.disabled_reason values this process is allowed to reverse. A peer
# disabled by hand is somebody's decision, and re-enabling it because it happens
# to be under its quota would undo that silently.
ENFORCED_REASONS = ("quota", "expired")

# Columns the presence upsert is allowed to write. Named explicitly because the
# statement carries whole ClientMeta objects: a conflicting row must have its
# quota, expiry and offsets left exactly as they were.
PRESENCE_FIELDS = ["last_handshake", "last_endpoint"]

# What a client with no data limit has left, so that the poll loop can subtract
# from one number for everybody instead of asking whether there is a limit first.
UNLIMITED = float("inf")


@dataclass
class Watch:
    """One client's enforcement state, as the poll loop holds it between passes.

    Deliberately not a copy of the metadata row. The row is the truth and is
    re-read whenever it decides anything; what is kept here is the one number
    that has to be touched on every poll - how much more the client may transfer
    before somebody has to go and look.
    """

    name: str
    # Bytes left before the quota trips, or UNLIMITED. Only ever a hint: it is
    # spent from the deltas as they arrive, and a hint that reaches zero is what
    # sends this client to be checked properly, not what switches it off.
    remaining: float
    # Whether the config said this peer was on, as of the pass that built this.
    # Stale in both directions between passes, and harmless in both: a peer that
    # is really off sends no traffic to spend, and one that is really on is
    # checked by the next pass at the latest.
    enabled: bool


def _fold_offsets(db: dict[str, traffic.Counters], offsets: dict[str, tuple[int, int]]) -> None:
    """Take what the panel has already cleared out of the counters, in place.

    The arithmetic behind "remove all traffic", applied to both copies of it -
    the file and this process's mirror - so that neither can end up describing a
    different server than the other. What comes out is the counter less the
    offset the request stored against that client, floored at nothing, which is
    exactly the figure every reader was already arriving at by subtraction.

    A peer with no metadata row loses the lot. It is a row in traffic.db that no
    client points at - a peer added to the config by hand, or one whose client
    was removed in the seconds since the wipe - so there is nothing to subtract
    and nothing that reports it either; leaving it whole would keep a total that
    the wipe was asked to take and that nobody can see to check.

    `last_rx` and `last_tx` are not touched, and that is the whole safety of it:
    they are the raw kernel readings the next delta is measured against rather
    than totals, and awg.traffic reads a raw value below the stored one as a new
    counter epoch. Clearing them would add every peer's lifetime straight back.
    """
    for key, row in db.items():
        offset_rx, offset_tx = offsets.get(key, (row.cum_rx, row.cum_tx))
        row.cum_rx = max(row.cum_rx - offset_rx, 0)
        row.cum_tx = max(row.cum_tx - offset_tx, 0)


class Collector:
    """The polling loop. Owns no state that matters if the process is killed."""

    def __init__(self, controller: BaseController | None = None, once: bool = False) -> None:
        self.controller = controller or get_controller()
        self.once = once
        self.stop = threading.Event()

        # Mirror of traffic.db, seeded from disk so the first cycle after a
        # restart reports the bytes since the last flush and not the whole
        # lifetime of the interface.
        self._counters: dict[str, traffic.Counters] = {}
        # Which run of the interface the mirror's raw values were read in, held
        # here rather than re-read from traffic.epoch because that file is only
        # rewritten by the flush: a poll comparing against it would find every
        # one of the five polls in a flush interval to be the restart, and count
        # the whole reading five times over. Seeded from the file at startup,
        # which is the one moment the two are level.
        self._epoch = ""
        # The last raw dump, kept for checking which peers the kernel holds.
        self._transfers: dict[str, tuple[int, int]] = {}
        # {public_key: unix seconds} the peer's receive counter last moved. Not
        # persisted on purpose: after a restart this process has watched nobody,
        # and claiming a client had been silent since before it was looking
        # would report a room full of connected peers as gone. An empty mirror
        # sends every peer through the handshake fallback for one keepalive
        # interval, which is the length of time it takes to know better.
        self._last_rx: dict[str, int] = {}
        # The reading that has not been folded into traffic.db yet. Separate
        # from _transfers, and consumed by the flush, because a cycle with no
        # dump must fold nothing: awg.traffic reads a raw value below the one
        # on disk as a counter reset and adds the whole thing again, so
        # re-folding a reading the PreDown hook has already superseded invents
        # an entire counter epoch of traffic.
        self._unflushed: dict[str, tuple[int, int]] | None = None
        # The epoch that reading was taken in, so the flush can tell that the
        # interface went away underneath it. See _flush_traffic_db.
        self._unflushed_epoch = ""
        # Today's transfer across every client, and the UTC day it belongs to.
        # Seeded from the stored row at startup so a restart resumes the day's
        # figure instead of beginning it again at zero, and written back every
        # flush. The whole of what used to be a table of per-client sample rows.
        self._today = date.min
        self._today_rx = 0
        self._today_tx = 0
        # Whether the pair above has moved since it was last stored. A server
        # with nothing connected moves no bytes for hours at a time, and there
        # is no reason to rewrite the same row every ten seconds through it.
        self._today_dirty = False
        # Since when the rollover has been unable to store the finished day, on
        # the monotonic clock, or 0.0 when it is not waiting on anything.
        self._rollover_since = 0.0
        # The same figure again, split by client: {public_key: [rx, tx]} for
        # today only, and the subset of those keys whose numbers have moved
        # since the last flush.
        #
        # Held per client as well as summed because the two answer different
        # questions and neither can be derived from the other here. The sum is
        # the server's day and has to keep counting clients that are later
        # deleted; the split is each client's own day and has to stop when they
        # are. Both are absolute totals rather than increments, which is what
        # makes a flush that fails safe to simply repeat.
        #
        # Bounded by the clients that transferred today, not by the clients the
        # server holds: a peer that moved nothing is never in here. On a server
        # where every one of four thousand clients is busy it is a few hundred
        # kilobytes, beside the counters mirror that is already that size.
        self._client_today: dict[str, list[int]] = {}
        self._client_dirty: set[str] = set()
        # {public_key: ClientMeta primary key}, rebuilt by each reconcile out of
        # the rows it reads anyway. The history rows hang off the metadata row,
        # so a write needs its id - and this is where that id can be had for
        # nothing, rather than for a query per flush naming every client that
        # moved.
        #
        # Allowed to be a pass behind, like every other cache this loop keeps. A
        # client added since the last reconcile simply has no id yet, and its
        # bytes stay in `_client_today` until one arrives; because what is
        # written is the day's total rather than a delta, waiting costs nothing
        # but the delay.
        self._meta_ids: dict[str, int] = {}
        # {public_key: the history_reset_at this loop has already acted on}, so
        # that a stamp which has moved since is a reset this loop has yet to
        # honour. A key absent from the map has never been compared rather than
        # never been reset, and the difference matters on the pass that fills
        # it: see _note_history_resets.
        self._history_reset: dict[str, datetime | None] = {}
        # {public_key: (handshake, endpoint)} as the database already has it,
        # and the subset of that which it does not have yet. The mirror is what
        # makes this cheap: a peer re-handshakes about every two minutes, so on
        # all but one poll in sixty the comparison finds nothing to write and no
        # statement is issued at all.
        self._presence: dict[str, tuple[int, str]] = {}
        self._unsaved: dict[str, tuple[int, str]] = {}

        # Since when the tunnel has been up, in Unix seconds, and the netdev
        # generation that claim is about. Restored from the last blob at
        # startup: a collector restart must not reset an uptime the tunnel
        # itself never lost.
        self._iface_since = 0
        self._iface_index = 0

        # {public_key: Watch} for every client a limit could ever be applied to,
        # rebuilt by each reconcile. This is what moves quota enforcement off the
        # reconcile's clock and onto the poll's: a client that has just crossed
        # its limit is found on the next poll rather than at the next pass, and
        # the difference is the bytes it moves in between. At a hundred megabits
        # a minute of that is three quarters of a gigabyte, which on a ten
        # gigabyte allowance is not a limit anybody would recognise.
        self._watch: dict[str, Watch] = {}

        self._prev_clock: float | None = None
        self._next_flush = 0.0
        self._next_enforce = 0.0
        self._next_sweep = 0.0
        self._logged: dict[str, float] = {}
        self._iface_down = False

    # ---------------------------------------------------------------- running

    def run(self) -> None:
        """Poll until asked to stop, then flush what is buffered and return."""
        self._install_signals()
        self._bootstrap()
        self._counters = self._load_counters()
        self._epoch = traffic.read_epoch()
        self._presence = self._load_presence()
        self._load_today()
        self._load_history_resets()
        self._restore_iface_since()

        log.info(
            "collector started on %s (poll %.3gs, enforce %.3gs)",
            paths.iface(),
            self._poll_sec(),
            self._enforce_sec(),
        )

        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception as exc:
                # Deliberately everything: a bug in one cycle must not take the
                # tunnel's accounting and quota enforcement down with it.
                self._trouble("cycle", "collector cycle failed: %s", exc)
            if self.once:
                break
            self.stop.wait(max(self._poll_sec() - (time.monotonic() - started), MIN_SLEEP_SEC))

        self.shutdown()

    def cycle(self) -> None:
        """One poll: read the interface, write the live blob, run whatever is due."""
        now = time.monotonic()
        dump = self._read_dump()
        elapsed = now - self._prev_clock if self._prev_clock is not None else 0.0
        self._prev_clock = now

        deltas: dict[str, tuple[int, int]] = {}
        if dump is not None:
            self._transfers = dump.transfers()
            self._unflushed = self._transfers
            # Read once for the fold below and carried with the reading, so a
            # restart between this poll and the flush that folds it is caught
            # there too rather than being folded as though it were a delta.
            epoch = traffic.current_epoch()
            self._unflushed_epoch = epoch
            # The same question traffic.sync_db asks of the file, asked of the
            # mirror, and asked the same way: both halves have to be known for a
            # difference to mean anything, so an epoch that cannot be read and
            # an epoch never recorded are both silence rather than a restart.
            # Without it the mirror goes on diffing against raw values belonging
            # to an interface that no longer exists - which is the whole of what
            # traffic.epoch was added to stop, left open on the path that feeds
            # today's totals and quota enforcement.
            restarted = bool(epoch) and bool(self._epoch) and epoch != self._epoch
            if epoch:
                self._epoch = epoch
            # Mutates the mirror and hands back only what moved this cycle,
            # with the counter-reset rule the bash tool uses.
            deltas = traffic.accumulate(self._counters, self._transfers, restarted=restarted)
            self._add_today(deltas)
            self._spend(deltas)
            self._note_presence(dump)
        else:
            # The interface could not be read, so anything still waiting to be
            # folded was taken before whatever went wrong - and the orderly
            # version of "went wrong" is an `awg-quick down`, whose PreDown hook
            # has already written a later reading than this one.
            # Folding it now would be read as a counter reset and add the
            # peer's whole epoch again. Dropping it loses nothing: awg.traffic
            # diffs against the file, so the next successful poll carries the
            # same bytes.
            self._unflushed = None
            self._unflushed_epoch = ""

        self._write_live(dump, deltas, elapsed)

        if now >= self._next_flush:
            self._next_flush = now + FLUSH_SEC
            self._store_today()
            self._store_client_today()
            self._flush_traffic_db()
            self._store_presence()
            # Last, and after the flush rather than before it: the fold below
            # subtracts the offsets from what traffic.db holds, and the flush is
            # what puts this cycle's bytes there to be subtracted from. Run
            # first, it would leave the poll's own readings sitting in memory,
            # ready to be written back into a file that had just been cleared.
            self._apply_traffic_reset()

        if now >= self._next_enforce:
            self._next_enforce = now + self._enforce_sec()
            self._reconcile()

        if now >= self._next_sweep:
            self._next_sweep = now + SWEEP_SEC
            self._sweep_sessions()
            self._prune_history()
            self._prune_events()

    def shutdown(self) -> None:
        """Last flush before exit. Called on the way out of run(), whatever ended it."""
        self._store_today()
        self._store_client_today()
        self._flush_traffic_db()
        self._store_presence()
        # In the same order as a cycle's flush, and for the same reason. A wipe
        # asked for in the last ten seconds would otherwise wait for this
        # process to come back - which it does survive, since the row saying so
        # is stored rather than remembered, but a restart is not something an
        # admin should have to wait through to see a button take effect.
        self._apply_traffic_reset()
        log.info("collector stopped")

    # ------------------------------------------------------------- the tunnel

    def _read_dump(self) -> Dump | None:
        """The interface's live state, or None with one line in the log.

        A missing `awg`, an interface that was never brought up and one that
        went down five minutes ago all arrive here as None. That is a normal
        state for this process - it is what a server looks like between
        `install.sh` and the first `awg-quick up` - so it is reported once and
        polling continues.
        """
        try:
            dump = self.controller.show_dump()
        except AwgError as exc:
            self._trouble("dump", "cannot read the interface: %s", exc)
            return None

        if dump is None:
            if not self._iface_down:
                self._iface_down = True
                log.warning(
                    "no live interface: either awg is not installed or %s is down. Live stats "
                    "and quota enforcement resume by themselves once it is up.",
                    paths.iface(),
                )
                # Edge triggered, like the log line it sits under: the interface
                # being down is a state and the log wants the moment it changed.
                # A panel installed before the tunnel starts records one of these
                # and then nothing more, which is exactly right - that server has
                # never been up rather than having gone down repeatedly.
                recorder.record(kinds.SERVER_IFACE_DOWN, target=paths.iface())
            return None

        if self._iface_down:
            self._iface_down = False
            log.info("%s is up again", paths.iface())
            recorder.record(kinds.SERVER_IFACE_UP, target=paths.iface())
        return dump

    def _restore_iface_since(self) -> None:
        """Take back the tunnel's start time from the blob written before the restart.

        Two guards, because a remembered moment is only worth something while it
        still describes the interface in front of us. The netdev must be the
        same one - a down/up gives the tunnel a new index - and the moment must
        fall inside this boot, because indexes start again from low numbers
        after a reboot and an old one can match a new interface by coincidence.

        Anything that fails either test is dropped rather than repaired: this
        process is about to observe the interface anyway, and the worst that
        costs is an uptime that starts counting from now.
        """
        blob = live.read_live()
        since = int(blob.get("ifaceSince") or 0)
        index = int(blob.get("ifaceIndex") or 0)
        now = time.time()
        if since <= 0 or index <= 0 or since > now:
            return
        if index != sysinfo.iface_index(paths.iface()):
            return
        uptime = sysinfo.uptime()
        if uptime > 0 and since < now - uptime:
            return
        self._iface_since = since
        self._iface_index = index

    def _note_iface(self, up: bool, now: float) -> tuple[int, int]:
        """Since when the tunnel has been up, and which netdev that is about.

        An observation rather than a fact, because there is nothing to read: the
        kernel stamps no creation time on an interface. The first cycle that
        finds it up is the moment recorded, and it stands until the interface
        goes away or comes back as a different device.

        So the figure is a floor. A tunnel that was already up before anything
        watched it reads as younger than it is - which is the honest direction
        for this error to go, and why the dashboard says "up since" rather than
        claiming to know when it started.
        """
        if not up:
            self._iface_since = 0
            self._iface_index = 0
            return 0, 0
        index = sysinfo.iface_index(paths.iface())
        if self._iface_since <= 0 or index != self._iface_index:
            self._iface_since = int(now)
            self._iface_index = index
        return self._iface_since, self._iface_index

    def _write_live(
        self, dump: Dump | None, deltas: dict[str, tuple[int, int]], elapsed: float
    ) -> None:
        """Write the blob the dashboard reads. A failure here is not fatal."""
        threshold = settings_store.get_int("onlineThresholdSec", DEFAULT_ONLINE_THRESHOLD)
        now = time.time()
        peers: dict[str, dict[str, object]] = {}
        online = 0
        total_rx = 0
        total_tx = 0

        liveness = merge.liveness_window(self._keepalive(), self._poll_sec())

        for peer in dump.peers if dump else ():
            delta_rx, delta_tx = deltas.get(peer.public_key, (0, 0))
            # No elapsed time on the first cycle after a start, so no rate can
            # be honestly computed; zero is the truthful answer, not a guess.
            rate_rx = int(delta_rx / elapsed) if elapsed > 0 else 0
            rate_tx = int(delta_tx / elapsed) if elapsed > 0 else 0
            counters = self._counters.get(peer.public_key)
            # Bytes *received* from the peer, which is the client proving it is
            # still there. Bytes sent to it prove only that something here still
            # thinks it is worth writing to - a dead client's tx keeps moving
            # while a session that was open drains its retransmits.
            if delta_rx > 0:
                self._last_rx[peer.public_key] = int(now)
            last_rx = self._last_rx.get(peer.public_key, 0)
            is_online = merge.is_online(peer.latest_handshake, last_rx, now, threshold, liveness)
            # A rate is a claim about a connection, so a peer that has not got
            # one reports no rate. The kernel goes on writing to a client that
            # has gone - a handshake retried at its last endpoint every few
            # seconds while anything on this side still has packets for its
            # address - and that came out here as a couple of hundred bytes a
            # second appearing on a row, falling back to zero, and appearing
            # again, for a device that had been switched off for an hour. The
            # last-seen column beside it was counting the minutes up correctly
            # the whole time, which is what made the pair unreadable.
            #
            # Only what is reported changes. The bytes are real and they are
            # already in the accounting above, counted against the client that
            # was addressed exactly as they always were; what they are not is
            # throughput, because there is nothing at the other end receiving
            # them. A peer that is really carrying traffic is sending its own
            # packets back, which is the whole of what makes it online here.
            if not is_online:
                rate_rx = 0
                rate_tx = 0
            online += 1 if is_online else 0
            total_rx += rate_rx
            total_tx += rate_tx
            peers[peer.public_key] = {
                "rateRx": rate_rx,
                "rateTx": rate_tx,
                "rx": counters.cum_rx if counters else 0,
                "tx": counters.cum_tx if counters else 0,
                "handshake": peer.latest_handshake,
                # When this peer's counter last moved. Absent from a blob an
                # older collector wrote, which the client list reads as "no
                # liveness signal" and falls back to the handshake for.
                "lastRx": last_rx,
                "endpoint": peer.endpoint,
                "online": is_online,
                # The connectivity word itself, so a reader polling this file
                # every two seconds never has to derive one. Deriving it needs
                # the thresholds, a clock to measure them against, and both to
                # go on agreeing with a server the reader cannot see; sending
                # the answer costs eight bytes and removes all three.
                #
                # Connectivity only. Whether the peer is disabled, over quota or
                # expired is not a fact about the connection and is not known
                # here - the client list layers that on top.
                "status": merge.connectivity(is_online, max(peer.latest_handshake, last_rx), now),
            }

        iface_since, iface_index = self._note_iface(dump is not None, now)
        blob = {
            "ts": int(now),
            "ifaceUp": dump is not None,
            # Keyed to the same "up" as the flag above rather than to the netdev
            # existing, so the card that reports an uptime and the banner that
            # reports the tunnel down can never contradict each other.
            "ifaceSince": iface_since,
            "ifaceIndex": iface_index,
            "online": online,
            # Peers the kernel holds this cycle, which is the config minus the
            # disabled ones. The client list is the place that counts clients.
            "total": len(peers),
            "totalRateRx": total_rx,
            "totalRateTx": total_tx,
            "peers": peers,
            "system": sysinfo.snapshot(),
        }
        try:
            live.write_live(blob)
        except OSError as exc:
            self._trouble("live", "cannot write %s: %s", paths.live_state_file(), exc)

    # ------------------------------------------------------------- accounting

    def _load_counters(self) -> dict[str, traffic.Counters]:
        """Seed the in-memory mirror from traffic.db, tolerating a missing file."""
        try:
            return traffic.read_db()
        except OSError as exc:
            self._trouble("traffic-read", "cannot read %s: %s", paths.traffic_db(), exc)
            return {}

    def _load_today(self) -> bool:
        """Seed today's running total from the stored row. Says whether it could.

        Without this a restart would begin the day again at zero, and the ten
        seconds a hard kill can cost would instead be everything since midnight.

        A read that fails leaves the day unset rather than at zero, and that
        distinction is the whole of what the return value is for. The row holds
        an absolute total and is written by overwriting it, so a collector that
        started accumulating from an unseeded zero would replace a day's figure
        with the few minutes it had been up - turning a database that was briefly
        unreadable into a day's traffic that is gone. Unset means the caller
        tries again instead, which is right for both reasons this fails: the
        table not existing yet on a fresh install, and a lock held for a moment
        by the web process.

        The per-client rows are seeded in the same breath, and they need it more
        than the total does. Both are written as absolutes, so an unseeded map
        would not merely lose the morning - it would overwrite it: the first
        flush after a lunchtime restart would replace a client's whole day with
        the two minutes since. One query for the clients that have transferred
        today, which on a busy server is a few thousand small rows once, at
        startup, and on most servers is a handful.
        """
        today = utc_day(timezone.now())
        try:
            row = DailyTotal.objects.filter(day=today).first()
            started = list(
                ClientDaily.objects.filter(day=today).values_list("meta__public_key", "rx", "tx")
            )
        except DatabaseError as exc:
            # No table yet - the collector starts before the first migrate on a
            # fresh install - or a database that cannot be read.
            self._trouble("today-read", "cannot read today's traffic total: %s", exc)
            connections.close_all()
            return False
        self._recovered("today-read")
        self._today = today
        if row is not None:
            self._today_rx = row.rx
            self._today_tx = row.tx
        # Not marked dirty: this is exactly what the database already holds, and
        # writing it straight back would be a statement per restart saying
        # nothing that was not already said.
        self._client_today = {key: [rx, tx] for key, rx, tx in started}
        return True

    def _load_history_resets(self) -> None:
        """Record where every client's reset stamp stood when this process started.

        So that a key missing from the map afterwards means "added since the last
        pass" and nothing else. Without this seed every client on the server is
        missing from it once, on the first reconcile, and _note_history_resets
        cannot tell that from a client created a moment ago - which leaves it
        choosing between honouring a reset it has nothing to undo and skipping
        one it does.

        Honouring them all is the worse of those two and is why this is a query
        rather than a special case: what a reset does here is drop the client's
        running day, and at startup that day was seeded from the rows the reset
        already emptied. Dropping it again would take a client's whole morning
        off a server that had merely been restarted, for a reset made weeks ago.

        A read that fails leaves the map empty, which is the behaviour this
        replaces and is bounded by the next restart: the pass that follows
        records every stamp it sees, and only the clients whose reset landed in
        that window are affected.
        """
        try:
            stamps = list(ClientMeta.objects.values_list("public_key", "history_reset_at"))
        except DatabaseError as exc:
            self._trouble("history-seed", "cannot read the client reset stamps: %s", exc)
            connections.close_all()
            return
        self._recovered("history-seed")
        self._history_reset = dict(stamps)

    def _add_today(self, deltas: dict[str, tuple[int, int]]) -> None:
        """Add this cycle's bytes to today's running totals, rolling over at midnight.

        Twice over: once collapsed into the two integers the server's own day is,
        and once against the client that moved them. The second is a dictionary
        update per peer that actually transferred, which is what the loop above
        has already narrowed `deltas` to - a server where nothing is moving does
        nothing here either.

        The day is decided once, for both. That is the point of doing them in one
        place: the two rows are read side by side - a client's day against the
        server's - and a rollover that reached one of them a poll before the
        other would put a slice of midnight's traffic on different dates in the
        two answers.

        The day is checked on every poll rather than at flush time. Checked at
        the flush the last ten seconds of a day would be added to the next one,
        which is a visible jump on the card at midnight; checked here the error
        is bounded by the poll interval, and the bytes of the poll that straddles
        midnight land on the day the poll ended in.

        Nothing is counted at all until the seed has been read, because until
        then this process does not know what today already holds and would write
        its own few minutes over the whole of it. What that costs is the bytes of
        the polls taken while the database was unreadable - which is a figure
        that could not have been written down anyway - and it is bounded by the
        one thing that ends it, which is a read that works.
        """
        if self._today == date.min and not self._load_today():
            return

        today = utc_day(timezone.now())
        if today != self._today:
            # Store what the old day ended on before letting go of it, or a
            # server left running overnight loses its last ten seconds - and,
            # if it were only reset in memory, would go on to overwrite the
            # finished day with the new one's total.
            #
            # And do not let go of it until that has actually happened. Both
            # stores answer a failure by staying dirty so the next flush carries
            # the same figure, which is the right answer everywhere except here:
            # the rollover used to clear the flags and zero the totals whatever
            # they said, so a database locked for a moment at midnight - a
            # backup, the web process, anything holding SQLite - threw away the
            # retry at the one moment there was not going to be another chance
            # to take it, and the tail of the finished day went with it.
            #
            # Nothing is added to either day while this is stuck. That is the
            # same trade the seed above makes: the bytes of the polls taken
            # while the database was unreadable are a figure that could not have
            # been written down anyway, and it is better to lose them than to
            # put them on the wrong date.
            if self._today != date.min and not self._roll_over(now=time.monotonic()):
                return
            self._today = today
            self._today_rx = 0
            self._today_tx = 0
            self._today_dirty = False
            # Dropped rather than zeroed: yesterday's rows are written and a new
            # day's map should hold only the clients that transfer in it, or a
            # server would carry every client that has ever been busy in memory
            # until the process restarts.
            self._client_today = {}
            self._client_dirty = set()

        moved_rx = 0
        moved_tx = 0
        for key, (delta_rx, delta_tx) in deltas.items():
            if not delta_rx and not delta_tx:
                continue
            moved_rx += delta_rx
            moved_tx += delta_tx
            client = self._client_today.get(key)
            if client is None:
                client = [0, 0]
                self._client_today[key] = client
            client[0] += delta_rx
            client[1] += delta_tx
            self._client_dirty.add(key)
        if not moved_rx and not moved_tx:
            return
        self._today_rx += moved_rx
        self._today_tx += moved_tx
        self._today_dirty = True

    def _roll_over(self, now: float) -> bool:
        """Put the finished day beyond reach of the rollover, or refuse to let go.

        Both rows, and both have to land: the server's day and every client's
        are read side by side, and a rollover that saved one of them would leave
        the two answers disagreeing about the same night for as long as the
        history is kept.

        The refusal is bounded, because it is not free. A poll that returns here
        adds nothing to either day, so a database that never comes back would
        stop the accounting rather than lose the end of one night - which is the
        larger of the two failures, and the reason this gives up after
        ROLLOVER_GRACE_SEC and says in the log exactly what the finished day is
        short of. Five minutes is long enough for every lock a panel takes and
        short enough that what it costs is a rounding error against a day.
        """
        # Once, on the first attempt: the map only has to be put right the one
        # time, and a rollover already blocked on a locked database should not
        # be asking the same question of it every two seconds.
        if self._client_dirty and not self._rollover_since:
            self._resolve_meta_ids(self._client_dirty)
        stored = self._store_today()
        # Not short-circuited: the client rows are worth writing whether or not
        # the server's row went in, and on a retry the one that already
        # succeeded is a no-op.
        stored_clients = self._store_client_today()
        if stored and stored_clients:
            self._rollover_since = 0.0
            return True

        if not self._rollover_since:
            self._rollover_since = now
            return False
        if now - self._rollover_since < ROLLOVER_GRACE_SEC:
            return False

        lost = []
        if not stored:
            lost.append(f"the server's total ({self._today_rx} rx, {self._today_tx} tx)")
        if not stored_clients:
            lost.append(f"{len(self._client_dirty)} client row(s)")
        log.error(
            "the database would not take %s before the day turned over; %s lost",
            self._today,
            " and ".join(lost),
        )
        self._rollover_since = 0.0
        return True

    def _store_today(self) -> bool:
        """Write today's running total, in one upsert of one row.

        A write that fails leaves the accumulator dirty, so the next flush
        carries the same figure plus whatever has moved since. Nothing is lost
        by a failure and nothing is double counted by the retry: the row is
        overwritten with an absolute total, not incremented by a delta.

        Says whether the stored row is now level with what is held here, which
        on the flush clock nothing looks at - the dirty flag already carries the
        retry - and at midnight is the whole question: the rollover is the one
        caller that cannot simply try again later, because it is about to let go
        of the figure a retry would have carried.
        """
        if not self._today_dirty or self._today == date.min:
            return True
        try:
            DailyTotal.objects.update_or_create(
                day=self._today,
                defaults={"rx": self._today_rx, "tx": self._today_tx},
            )
        except DatabaseError as exc:
            self._trouble("today-write", "cannot store today's traffic total: %s", exc)
            # A database file replaced under us (a restore) leaves a connection
            # that will fail forever; dropping it makes the next try reconnect.
            connections.close_all()
            return False
        self._recovered("today-write")
        self._today_dirty = False
        return True

    def _store_client_today(self) -> bool:
        """Write today's row for each client that has moved since the last flush.

        One statement for all of them. The whole reason this is affordable, and
        the whole difference from the per-client sample table that was removed,
        is that the row is keyed by the day rather than by the moment: a client
        transferring for eight hours straight is written a hundred times an hour
        and is still one row, because every write after the first is an update
        of the same row with a larger total.

        Only the clients that moved. A dump carries every peer on the interface
        and on any real server most of them are idle in any given ten seconds, so
        the statement is sized by what happened rather than by what exists.

        Absolute totals, like the server's own row and for the same two reasons:
        a flush that fails can simply be repeated without double counting, and a
        restart resumes the day from what is stored rather than beginning it
        again - which is what _load_today seeds this map for.

        A client whose metadata row has not been seen yet keeps its bytes and its
        place in the dirty set, and is written by the flush after the next
        reconcile. That is the whole handling it needs: what is held is the day's
        running total, so a row that arrives late arrives complete.

        Except at midnight, where there is no flush after the next reconcile to
        wait for and the map is about to be dropped. _roll_over puts it right
        first - see _resolve_meta_ids - so what arrives here on that pass is
        every client whose bytes can still be written anywhere.

        Says whether everything writable went in, for the same reason
        _store_today does. A client with no id is not counted against that
        answer: waiting for a row that has not been read yet is the ordinary
        state mid-day, and waiting for one that does not exist would stall the
        rollover for good.
        """
        if not self._client_dirty or self._today == date.min:
            return True
        pending = {key: self._meta_ids[key] for key in self._client_dirty if key in self._meta_ids}
        if not pending:
            return True

        rows = [
            ClientDaily(
                meta_id=meta_id,
                day=self._today,
                rx=self._client_today[key][0],
                tx=self._client_today[key][1],
            )
            for key, meta_id in pending.items()
        ]
        try:
            ClientDaily.objects.bulk_create(
                rows,
                update_conflicts=True,
                update_fields=["rx", "tx"],
                unique_fields=["meta", "day"],
            )
        except DatabaseError as exc:
            # Everything stays dirty, so the next flush carries the same totals
            # plus whatever has moved since. The one fault that persists here is
            # an id for a client that has since been deleted, which fails the
            # foreign key - and the next reconcile rebuilds the map without it.
            self._trouble(
                "client-today-write",
                "cannot store today's traffic for %d client(s): %s",
                len(rows),
                exc,
            )
            connections.close_all()
            return False
        self._recovered("client-today-write")
        self._client_dirty -= pending.keys()
        return True

    def _resolve_meta_ids(self, keys: set[str]) -> None:
        """Settle which of the clients holding bytes can still be written against.

        The rollover's alone, and the reason it is not simply left to the next
        reconcile is that there is no next flush for the day being let go of.
        Two things go wrong there, and one query answers both.

        A client added since the last pass has bytes and no id. Mid-day that
        costs nothing - what is held is the day's running total, so a row that
        arrives late arrives complete - but at midnight the map goes and the
        bytes go with it, so the id is fetched now.

        A client deleted since the last pass has an id that no longer points at
        anything, and writing against it fails the foreign key. Mid-day the next
        reconcile drops it and the flush after that succeeds; at midnight it
        would fail every two seconds until the grace period ran out, taking the
        clients that could have been written down with it. So a key with no row
        is let go of here: its history hung off the metadata row and went when
        that did, and there is nowhere left for its bytes to be written.
        """
        try:
            found = dict(
                ClientMeta.objects.filter(public_key__in=keys).values_list("public_key", "pk")
            )
        except DatabaseError as exc:
            self._trouble(
                "meta-ids", "cannot look up the metadata of %d client(s): %s", len(keys), exc
            )
            connections.close_all()
            return
        self._recovered("meta-ids")
        self._meta_ids.update(found)
        for key in keys - found.keys():
            self._meta_ids.pop(key, None)
            self._client_dirty.discard(key)
            self._client_today.pop(key, None)

    def _note_history_resets(self, meta: dict[str, ClientMeta]) -> None:
        """Let go of what this process holds for a client whose history was cleared.

        "Reset usage" deletes the client's stored days in the request that asks
        for it, and that deletion is undone by this process unless it is told:
        the day is held here as an absolute total and written by overwriting the
        row, so the next flush after a reset would put the whole morning back,
        with the bytes the admin has just cleared in it.

        Nothing can announce the request - the panel runs in another process,
        and the one channel between them is the database - so the request leaves
        a stamp on the metadata row and this compares it against the one it saw
        last time. That comparison is free: the reconcile has already read every
        one of these rows for the enforcement pass, and the column comes with
        them.

        A key it has no previous stamp for is a client added since the last
        pass, because _load_history_resets records every other one at startup.
        That used to be lumped in with a stamp that has not moved and skipped,
        on the grounds that a new client has no history to undo - which is true
        of the database and false of this loop's memory. A client created and
        then reset inside one reconcile period has bytes in `_client_today`
        already, and skipping the pass that clears them is what puts them back
        on the next flush, a minute after an admin watched the figure go to
        zero. So a new client carrying a stamp is honoured like any other; a new
        client without one has genuinely never been reset and is only recorded.

        The rows go a second time as well as the memory, and that is not
        belt-and-braces: up to a minute passes between the request and this, and
        a client transferring through it has had today's row written back
        underneath the deletion. Bounded by the day the reset happened, so a
        reset just before midnight cannot take the new day with it.

        What this costs the history is the bytes that moved between the request
        and this pass, which are counted into a row that is then deleted. They
        are bytes that moved after an admin asked for the client's history to be
        emptied, and losing under a minute of them is the smaller of the two
        wrong answers available.
        """
        acted: dict[str, datetime | None] = {}
        for key, row in meta.items():
            stamp = row.history_reset_at
            known = key in self._history_reset
            if known and self._history_reset[key] == stamp:
                acted[key] = stamp
                continue
            if not known and stamp is None:
                # Added since the last pass and never reset. There is nothing to
                # undo and no day to sweep; record where it stands.
                acted[key] = None
                continue

            # Today starts again from here, whether or not the delete below
            # works: this map is the thing that would write the cleared bytes
            # back, and it is not worth keeping for a retry.
            self._client_today.pop(key, None)
            self._client_dirty.discard(key)

            if stamp is None:
                # The stamp was taken off the row by hand rather than set by a
                # reset. There is no day to sweep and nothing was asked for.
                acted[key] = None
                continue

            try:
                removed = history.clear_client(row.pk, through=utc_day(stamp))
            except DatabaseError as exc:
                self._trouble(
                    "history-clear", "cannot clear the traffic history of %s: %s", key, exc
                )
                connections.close_all()
                # The old stamp is kept, so the next pass tries the same client
                # again rather than treating the reset as done.
                acted[key] = self._history_reset[key]
                continue
            self._recovered("history-clear")
            acted[key] = stamp
            if removed:
                log.info(
                    "%d traffic history row(s) removed after a reset for client %s",
                    removed,
                    row.name or key,
                )

        # Wholesale, like the id map beside it: a client deleted since the last
        # pass leaves no entry behind to be compared against a row that no
        # longer exists.
        self._history_reset = acted

    def _apply_traffic_reset(self) -> None:
        """Finish a wipe the panel began: the counters, the mirror and the day.

        "Remove all traffic" is the one operation in the panel that cannot be
        done by whichever process was asked for it. The request can empty the
        stored days and write an offset against every client, and that is enough
        for every figure a browser reads - but the counters those offsets are
        subtracted from live in traffic.db, and this process is holding a mirror
        of that file which it rewrites from memory every ten seconds. A wipe
        nobody told it about would be undone before the page had finished
        reloading.

        So the request leaves a row saying a wipe was asked for, and this reads
        it on the flush clock. One primary key lookup on a one-row table every
        ten seconds, beside the four writes that are already there, and on all
        but one of those cycles it finds nothing to do.

        What it does is take the offsets *out* of the counters rather than zero
        them: the new total is what the file holds less what the panel has
        already cleared, floored at nothing. That is what makes the fold
        invisible. Before it, every reader subtracts the offset and sees zero;
        after it, both halves are zero and the answer has not moved - and a
        client that transferred in between keeps those bytes, because they moved
        after the wipe and are real. Zeroing outright would have thrown them
        away and made the figure jump backwards a second time.

        `last_rx` and `last_tx` are deliberately left where they are. They are
        not totals at all but the raw kernel readings the next delta is measured
        from, and clearing them would make awg.traffic read the interface's next
        report as a new counter epoch and add every peer's entire lifetime back.

        The running day goes with it, in memory as well as in the table, because
        this process holds today as an absolute total and writes it by
        overwriting the row - so a flush after the wipe would put the whole
        morning back. Bounded by the day the wipe was asked for, so one made at
        one minute to midnight cannot take the new day with it.

        What that costs is the traffic between the request and this pass, which
        is counted into rows that are then deleted. It is under ten seconds of
        it, it moved after an admin asked for every figure on the server to be
        cleared, and losing it is the smaller of the two wrong answers - the
        other being a cleared server that still remembers this morning.

        Once only, which is what the stamps on the row are for: folding twice
        would take a client's post-wipe bytes off as well. A failure part of the
        way through leaves the row as it was, so the next flush tries again;
        what a retry can cost is one flush interval of bytes for a client that
        was transferring through it, because the second fold subtracts an offset
        the first has already taken and floors at nothing.
        """
        try:
            row = TrafficReset.objects.first()
        except DatabaseError as exc:
            self._trouble("traffic-reset", "cannot read the traffic reset: %s", exc)
            connections.close_all()
            return
        if row is None or not row.pending:
            return

        through = utc_day(row.requested_at)
        try:
            offsets = {
                key: (offset_rx, offset_tx)
                for key, offset_rx, offset_tx in ClientMeta.objects.values_list(
                    "public_key", "offset_rx", "offset_tx"
                )
            }
        except DatabaseError as exc:
            self._trouble("traffic-reset", "cannot read the usage offsets: %s", exc)
            connections.close_all()
            return

        try:
            with lock.config_lock():
                # Read here rather than through _load_counters, which answers an
                # unreadable file with an empty database. That is the right
                # answer when it is seeding a mirror and the wrong one here: it
                # would be written straight back, taking every peer's last raw
                # reading with it, and the next poll would read the counters it
                # no longer has anything to compare against as a fresh epoch.
                db = traffic.read_db()
                _fold_offsets(db, offsets)
                traffic.write_db(db)
        except (AwgError, OSError) as exc:
            self._trouble("traffic-reset", "cannot clear %s: %s", paths.traffic_db(), exc)
            return
        # The same arithmetic on this process's own copy, rather than a re-read
        # of what was just written. It is the same answer when the flush above
        # this one succeeded and the right one when it did not: the mirror is
        # then ahead of the file by a cycle, and re-reading would quietly hand
        # those bytes to the next delta as though the counters had restarted.
        _fold_offsets(self._counters, offsets)

        if self._today <= through:
            # Dropped rather than rewritten: the rows are going, and what this
            # holds is the figure that would put them back.
            self._today_rx = 0
            self._today_tx = 0
            self._today_dirty = False
            self._client_today = {}
            self._client_dirty = set()

        try:
            with transaction.atomic():
                ClientMeta.objects.update(offset_rx=0, offset_tx=0)
                server_days, client_days = history.clear_everything(through=through)
                row.applied_at = row.requested_at
                row.save(update_fields=["applied_at"])
            index.refresh_counters()
        except DatabaseError as exc:
            self._trouble("traffic-reset", "cannot settle the traffic reset: %s", exc)
            connections.close_all()
            return
        # Cleared at the end rather than after the read, so a fold that keeps
        # failing keeps its cooldown: the read is the one step that works every
        # time, and forgetting the fault on the strength of it would put a
        # warning in the journal every ten seconds for as long as the disk was
        # full.
        self._recovered("traffic-reset")
        # Sooner rather than at the next interval: a client switched off for its
        # quota has nothing left to enforce, and the pass that works that out is
        # the same one that switches it back on. Waiting a minute to do it would
        # read as the wipe having half worked.
        self._next_enforce = 0.0
        log.info(
            "traffic cleared: %d server day(s) and %d client day(s) removed, %d counter(s) folded",
            server_days,
            client_days,
            len(db),
        )

    def _prune_history(self) -> None:
        """Drop the client rows that have aged out of the retention window.

        On the sweep's daily clock, beside the expired sessions, and there for
        the same reason they are: it is housekeeping that keeps a table bounded,
        nothing depends on it having happened at any particular moment, and the
        rows it does not take today are exactly the rows it takes tomorrow.
        """
        try:
            removed = history.prune()
        except DatabaseError as exc:
            self._trouble("history-prune", "cannot sweep old traffic history: %s", exc)
            connections.close_all()
            return
        self._recovered("history-prune")
        if removed:
            log.info("%d expired traffic history row(s) removed", removed)

    def _prune_events(self) -> None:
        """Drop the events that have aged out, and any left over the ceiling.

        Beside the history sweep and on the same daily clock, for the same
        reason: a table that only ever grows needs somebody to bound it, and
        nothing depends on this having happened at any particular moment.

        The one thing worth saying about it separately is that the ceiling
        matters here and does not there. Traffic history is written by this
        process at a rate this process chooses; events are written by whatever
        arrives at the panel, and a refused sign-in is one of them - so the
        window alone would bound an ordinary panel and not a flooded one. See
        apps.events.models for both numbers.
        """
        try:
            removed = recorder.prune()
        except DatabaseError as exc:
            self._trouble("event-prune", "cannot sweep the event log: %s", exc)
            connections.close_all()
            return
        self._recovered("event-prune")
        if removed:
            log.info("%d expired event(s) removed", removed)

    def _flush_traffic_db(self) -> None:
        """Fold the reading taken since the last flush into the shared traffic.db.

        Under the config flock and through awg.traffic, which is also how the
        PreDown hook writes this file, so the two queue behind each other rather
        than overwriting one another.

        Every dump is folded at most once, and a cycle that could not read the
        interface folds nothing. Both matter: awg.traffic diffs against what is
        already in the file, and a raw value below the stored one is the
        counter-reset rule, so a second fold of a reading the PreDown hook has
        already improved on is read as a new epoch and adds the peer's entire
        lifetime again.

        A write that fails puts the reading back, so the next flush retries it.
        That is only safe because a cycle that cannot read the interface clears
        it: the reading can never outlive the epoch it was taken in, which is
        the one thing that would turn a retry into an invented epoch.

        A reading whose epoch has turned over since it was taken is dropped for
        the same reason, and it is the one case a failed poll does not already
        cover: the interface can go away in the last poll interval before the
        flush, leaving a reading from the old run of it in hand and no cycle in
        between to clear it. Folding that reading now would have sync_db find a
        changed epoch, treat the whole of an old counter as new traffic and add
        the peer's entire lifetime. What dropping it costs is the bytes between
        the last flush and the teardown, which is what the PreDown hook is there
        to fold and is lost anyway when the interface is not taken down in an
        orderly way.

        The mirror is re-seeded from what the file now holds, because the file
        is the one that knows everything: the PreDown hook adds to it behind
        this process's back, and a mirror that never hears about it drifts below
        the totals quota enforcement and the dashboard are read from. Merged
        rather than replaced - a peer first seen by a poll whose reading was
        dropped is in the mirror and not yet in the file, and taking its row
        away would hand its next reading to the fold as a whole new epoch.

        The client list is told what changed on the way past. It keeps a copy of
        these totals in a column, because that is what sorting by usage and
        filtering by quota read, and its way of staying level was to notice this
        file had moved and work the difference out again from scratch - a parse
        of every line and a pass over every row, every ten seconds, to write the
        handful that a busy ten seconds actually changed. This process already
        knows which peers moved and what they now hold, so it says so.
        """
        transfers, self._unflushed = self._unflushed, None
        taken_in, self._unflushed_epoch = self._unflushed_epoch, ""
        if not transfers:
            return
        epoch = traffic.current_epoch()
        if epoch and taken_in and epoch != taken_in:
            log.warning(
                "%s restarted while a reading was waiting to be folded; dropping it",
                paths.iface(),
            )
            return
        try:
            # One window for the write and the two stats around it. The stats are
            # what let the copy be updated rather than rebuilt, and they only mean
            # anything while nothing else can write in between - config_lock is
            # re-entrant, so this is the same lock traffic.sync_db takes and it is
            # released once.
            with lock.config_lock():
                before = index.Stamp.observe()
                db, deltas = traffic.sync_db(transfers)
                after = index.Stamp.observe()
        except (AwgError, OSError) as exc:
            self._unflushed = transfers
            self._unflushed_epoch = taken_in
            self._trouble("traffic-write", "cannot update %s: %s", paths.traffic_db(), exc)
            return
        self._recovered("traffic-write")
        self._counters.update(db)
        self._absorb_counters(db, deltas, before, after)

    def _absorb_counters(
        self,
        db: dict[str, traffic.Counters],
        deltas: dict[str, tuple[int, int]],
        before: index.Stamp,
        after: index.Stamp,
    ) -> None:
        """Hand the client list the rows it would otherwise re-derive from the file.

        Only the peers whose totals actually moved, which is what makes this
        worth doing: a dump carries every peer on the interface and most of them
        are idle in any given ten seconds.

        The totals come from what the file now says rather than from this
        process's mirror. They are almost always the same thing, and the case
        where they are not is the one that matters - a PreDown hook added to a
        peer since the last flush, and writing the mirror over it would take
        the client's usage backwards on the dashboard.

        A failure here costs nothing but the saving. The stamp is only advanced
        when the rows are written, so a refusal or an error leaves the copy
        exactly as it was and the next read notices the file has moved and
        rebuilds - which is what happened on every flush before this existed.
        """
        moved = {key: db[key] for key, (rx, tx) in deltas.items() if (rx or tx) and key in db}
        if not moved:
            return
        try:
            if not index.absorb_counters(moved, before, after):
                log.debug(
                    "the client list was not level with %s; leaving it to rebuild",
                    paths.traffic_db(),
                )
        except DatabaseError as exc:
            self._trouble("counters-absorb", "cannot pass on the usage figures: %s", exc)
            connections.close_all()

    # --------------------------------------------------------------- presence

    def _load_presence(self) -> dict[str, tuple[int, str]]:
        """Seed the mirror of what the database already knows about handshakes.

        One query at startup buys the comparison in _note_presence, which is
        what keeps a peer that handshook before this process started from being
        written out again on the first flush.

        The history writer's id map is filled from the same rows, because they
        are already being read and it needs exactly one more column. Without it
        the first flush after a start would find no id for anybody and defer
        every client's history to the reconcile ten seconds later - correct, but
        a delay bought for nothing.

        The reset stamps come along for the same ride, and they need the ride
        more than the ids do. Filled here they are compared from the first
        reconcile onwards, so a reset made a few seconds after this process
        started is honoured like any other; left to be filled by that reconcile
        instead, it would be recorded as the first thing this loop ever knew
        about the client and acted on by nothing.
        """
        try:
            rows = ClientMeta.objects.values_list(
                "public_key", "last_handshake", "last_endpoint", "id", "history_reset_at"
            )
            loaded = list(rows)
        except DatabaseError as exc:
            # No table yet, or a database that cannot be read. An empty mirror
            # only costs one redundant upsert per peer.
            self._trouble("presence-read", "cannot read the last handshakes: %s", exc)
            return {}
        self._meta_ids = {key: pk for key, _, _, pk, _ in loaded}
        self._history_reset = {key: reset for key, _, _, _, reset in loaded}
        return {key: (handshake, endpoint) for key, handshake, endpoint, _, _ in loaded}

    def _note_presence(self, dump: Dump) -> None:
        """Remember any handshake that moved, so a restart cannot forget it.

        Only forwards, and only for a peer that has actually handshaken: the
        kernel reports 0 for a peer that has not connected *since the interface
        came up*, and letting that overwrite a stored time is exactly the
        forgetting this is here to prevent. An endpoint that has gone empty is
        held for the same reason.
        """
        for peer in dump.peers:
            handshake = int(peer.latest_handshake or 0)
            if handshake <= 0:
                continue
            known = self._unsaved.get(peer.public_key) or self._presence.get(peer.public_key)
            endpoint = peer.endpoint.strip()[:128] or (known[1] if known else "")
            if known is not None and (handshake < known[0] or (handshake, endpoint) == known):
                continue
            self._unsaved[peer.public_key] = (handshake, endpoint)

    def _store_presence(self) -> None:
        """Write the moved handshakes, in one upsert.

        A row is created for a peer that has none, because a peer that appeared
        in the config without going through the panel - a restore, a hand edit -
        has no metadata until somebody edits it, and "when was this last
        connected" should not depend on that. Every
        other column keeps its default, which is what the client list already
        assumes for a peer with no row at all.

        Whatever the database refuses is kept and retried on the next flush;
        the mirror is only advanced once the write is accepted, so a failure
        cannot leave it claiming a handshake that was never stored.
        """
        if not self._unsaved:
            return
        pending, self._unsaved = self._unsaved, {}
        rows = [
            ClientMeta(public_key=key, last_handshake=handshake, last_endpoint=endpoint)
            for key, (handshake, endpoint) in pending.items()
        ]
        try:
            ClientMeta.objects.bulk_create(
                rows,
                update_conflicts=True,
                update_fields=PRESENCE_FIELDS,
                unique_fields=["public_key"],
            )
        except DatabaseError as exc:
            # Newer readings win over the ones going back in the queue.
            self._unsaved = {**pending, **self._unsaved}
            self._trouble("presence-write", "cannot record the last handshakes: %s", exc)
            connections.close_all()
            return
        self._recovered("presence-write")
        self._presence.update(pending)

    # ------------------------------------------------------------ enforcement

    def _spend(self, deltas: dict[str, tuple[int, int]]) -> None:
        """Take this poll's bytes off what each client has left, and act on any that ran out.

        The fast half of enforcement, and the reason it is worth having: a quota
        applied once a minute is a quota a client on a fast link can be most of a
        gigabyte past before anything notices. Here it is applied on the same
        clock the counters are read on.

        Cheap enough to run on every poll because it is not a pass over the
        clients. Only a peer that moved bytes can have crossed a line, so this
        walks the deltas - which is the peers on the interface that are actually
        transferring - and does one subtraction and one comparison for each. A
        server where nothing is moving does nothing at all.

        What comes out of it is a list of candidates, not a decision. `remaining`
        is spent from the mirror in memory and never re-read, so it drifts:
        against an admin who has just raised a limit in the panel, against a
        counter somebody cleared, against a PreDown hook that landed between
        passes. Every candidate is therefore checked against its row and
        its counters before anything is switched off, and a candidate that turns
        out to be fine has its figure corrected on the spot rather than coming
        back on the next poll and every poll after it.
        """
        due: list[str] = []
        for key, (delta_rx, delta_tx) in deltas.items():
            if not delta_rx and not delta_tx:
                continue
            watch = self._watch.get(key)
            if watch is None or not watch.enabled:
                continue
            watch.remaining -= delta_rx + delta_tx
            if watch.remaining <= 0:
                due.append(key)
        if due:
            self._settle(due)

    def _settle(self, keys: list[str]) -> None:
        """Check the clients that have run out against what is actually stored.

        One query for the rows behind them, which is a handful even on a server
        that has just had a limit lowered underneath everybody, and the verdict
        the panel and the reconcile both use. A client that survives it - the
        limit moved, the counter was cleared, the row is gone - has its remaining
        figure rebuilt from what was just read, so the same candidate does not
        arrive again on the next poll.

        The counters come from the mirror rather than from traffic.db, because
        the mirror is what the file is about to be written from: it is the same
        arithmetic ten seconds early, not a second opinion, and waiting for the
        flush would put back most of the delay this is here to remove. "About
        to be written" is what _switch guarantees - it folds the reading before
        it revokes anything - and without that guarantee this would be enforcing
        on a figure the file never receives and the admin never sees.
        """
        try:
            rows = {row.public_key: row for row in ClientMeta.objects.filter(public_key__in=keys)}
        except DatabaseError as exc:
            self._trouble(
                "spend-db", "cannot read the metadata of %d client(s): %s", len(keys), exc
            )
            connections.close_all()
            return
        self._recovered("spend-db")

        now = timezone.now()
        verdicts: dict[str, tuple[ClientMeta, str]] = {}
        for key in keys:
            watch = self._watch[key]
            row = rows.get(key)
            if row is None:
                # The row went while this was watching - the client was removed
                # from the panel. Nothing to enforce and nothing to enforce it
                # against; the next reconcile drops the watch with it.
                watch.remaining = UNLIMITED
                continue
            used = merge.used_bytes(row, self._counters.get(key))
            verdict = merge.verdict(row, used, now)
            if verdict:
                verdicts[watch.name] = (row, verdict)
            else:
                watch.remaining = self._headroom(row, used)

        # Marked disabled from what the store reports it actually changed, the
        # same way the reconcile does it, and never from what was asked for.
        #
        # A switch can fail - the config lock held past its budget by a long
        # settings save, `awg` itself erroring - and it leaves the peer on the
        # interface with its key still live. Marking the watch disabled anyway
        # would take that peer out of _spend's reach, because _spend skips a
        # watch that is not enabled: the one client known to be over its limit
        # would be the one client nothing was still counting, and it would go on
        # transferring unwatched until the next reconcile came round to find it.
        # That is the delay the fast path exists to remove, reintroduced at
        # exactly the moment it is needed.
        #
        # Left enabled, with the remaining figure it has already spent, the next
        # poll that carries bytes for it settles it again and retries the switch.
        for name in self._switch(verdicts, enabled=False):
            self._watch[verdicts[name][0].public_key].enabled = False

    @staticmethod
    def _headroom(row: ClientMeta, used: int) -> float:
        """How much more this client may transfer before somebody has to look."""
        return UNLIMITED if row.quota_bytes <= 0 else float(row.quota_bytes - used)

    def _reconcile(self) -> None:
        """Bring everything back level with the files, and rebuild what the poll loop spends.

        The slow half, and the half that cannot be replaced by an event, because
        the things it corrects happen where nothing announces them: a bring-up
        loads the whole configuration onto the interface and puts a disabled
        peer's key back on it, a hand edit changes a quota comment, a restore
        replaces both files at once. Nothing here can be subscribed to, so it is
        read fresh every time.

        It also decides what the fast path is allowed to act on. Every watch is
        rebuilt from the config and the metadata, so a limit raised, lowered,
        cleared or newly set is picked up here at the latest - and, for the
        direction that matters to whoever is waiting, immediately by the request
        that made the change.
        """
        # scan_server rather than list_clients: this pass reads a peer's key,
        # name, enabled flag and creation comment, and every one of those is in
        # the server config. A ClientView also carries the AllowedIPs from the
        # client's own file, which nothing here looks at and which costs an open
        # and a parse per client to fetch - on four thousand clients, five
        # sixths of what the read used to cost, once a minute, forever.
        try:
            scan = store.scan_server()
            peers = scan.peers
        except AwgError as exc:
            self._trouble("enforce-conf", "cannot read the server config: %s", exc)
            return
        self._recovered("enforce-conf")

        self._reassert_disabled(peers)

        try:
            meta = {row.public_key: row for row in ClientMeta.objects.all()}
        except DatabaseError as exc:
            self._trouble("enforce-db", "cannot read client metadata: %s", exc)
            connections.close_all()
            return
        self._recovered("enforce-db")

        # Rebuilt wholesale rather than updated, out of rows this pass has read
        # for its own reasons, so the history writer gets its ids for no query at
        # all. Wholesale is what makes it self-correcting: a client deleted since
        # the last pass is simply absent from the new map, which is the one thing
        # that clears an id the foreign key would now reject.
        self._meta_ids = {key: row.pk for key, row in meta.items()}

        self._note_history_resets(meta)

        self._backfill_created(peers, meta)

        # Both the file and the mirror, taking whichever is further on. Each
        # knows something the other does not: the file has whatever the PreDown
        # hook folded in behind this process's back, and the mirror has up to
        # ten seconds of polling the file has not been written from yet.
        # They are two readings of one set of counters that only ever climb, so
        # the higher is simply the later, and there is no case where preferring
        # it credits a client with bytes it did not send.
        #
        # This used to read the file alone, on the grounds that it is what the
        # client list showed as usage and enforcing on a figure the admin could
        # not see would be indistinguishable from a bug. Both halves of that
        # have since stopped being true: the switch folds the mirror into the
        # file before it revokes a key, and the panel reads the mirror's own
        # totals out of the live blob. What the reasoning left behind was a pass
        # that overwrote what the poll loop had already spent with a figure up
        # to a flush interval behind it - handing a client back headroom it had
        # used, once a minute, and more often than that whenever
        # `enforceIntervalSec` is set lower.
        counters = self._load_counters()
        now = timezone.now()
        watch: dict[str, Watch] = {}
        # The same peers by key, so that the switching below can correct the ones
        # it changes before the shaping pass reads them. The objects are this
        # pass's own - nothing else holds a reference to the scan - so correcting
        # them in place is cheaper than carrying a second set of overrides.
        by_key: dict[str, store.PeerScan] = {}
        verdicts: dict[str, tuple[ClientMeta, str]] = {}
        relieved: dict[str, tuple[ClientMeta, str]] = {}
        soonest: datetime | None = None
        for peer in peers:
            row = meta.get(peer.public_key)
            if row is None or not peer.name:
                # No quota, no expiry, nothing to enforce. An unnamed peer
                # cannot be addressed by name, which is how the store finds it.
                continue
            used = max(
                merge.used_bytes(row, counters.get(peer.public_key)),
                merge.used_bytes(row, self._counters.get(peer.public_key)),
            )
            verdict = merge.verdict(row, used, now)
            if peer.enabled and verdict:
                verdicts[peer.name] = (row, verdict)
            elif not peer.enabled and not verdict and row.disabled_reason in ENFORCED_REASONS:
                relieved[peer.name] = (row, "")
            watch[peer.public_key] = Watch(
                name=peer.name, remaining=self._headroom(row, used), enabled=peer.enabled
            )
            by_key[peer.public_key] = peer
            if row.expires_at is not None and row.expires_at > now and not row.expiry_overridden:
                soonest = row.expires_at if soonest is None else min(soonest, row.expires_at)

        self._watch = watch
        # Corrected from what the store reports it actually changed, rather than
        # from what was asked for: a switch that failed, or that found the peer
        # already in that state, must not leave the poll loop believing it moved.
        for name in self._switch(verdicts, enabled=False):
            key = verdicts[name][0].public_key
            watch[key].enabled = False
            by_key[key].enabled = False
        for name in self._switch(relieved, enabled=True):
            key = relieved[name][0].public_key
            watch[key].enabled = True
            by_key[key].enabled = True
        # After the switching, and reading the peers it has just corrected, so
        # that a client this pass revoked leaves the shaping structure in the
        # same pass rather than keeping a class for a key that is no longer on
        # the interface - and a client it let back on gets its ceiling with it.
        self._shape(scan, meta)
        self._wake_for(soonest, now)

    def _shape(self, scan: store.ServerScan, meta: dict[str, ClientMeta]) -> None:
        """Bring the kernel's bandwidth ceilings back level with what the panel wants.

        The third place enforcement happens, and the only one that cannot be
        skipped. A request applies the client it changed and the PostUp hook
        applies all of them at bring-up, but neither covers a hook that did not
        fire, a config too old to carry one, an operator who ran `tc qdisc del`
        to test something, or an interface that came back while the panel was
        mid-upgrade. None of those announces itself, and every one of them leaves
        a server that looks entirely healthy and is holding nobody to the rate
        they were sold.

        It costs two commands and a dictionary comparison. What is wanted is
        built from the peer list and the metadata this pass has already read, so
        no query is added; what is in force is read back out of the kernel in one
        `tc class show` per shaped interface, whatever the size of the server;
        and only the addresses whose numbers disagree are written. On a server
        where nothing has changed since the last pass - which is nearly every
        pass - that is the whole of it.

        Failures are logged and dropped like every other outside touch in this
        file. The next pass runs the same comparison against the same two
        sources and reaches the same conclusion, so nothing is lost by one that
        did not finish.
        """
        try:
            plan = shaping.plan_from(scan)
            shaping.reconcile(plan, shaping.wanted_from(scan.peers, meta, plan))
        except (AwgError, OSError) as exc:
            self._trouble("shape", "cannot apply the bandwidth limits: %s", exc)
            return
        self._recovered("shape")

    def _wake_for(self, soonest: datetime | None, now: datetime) -> None:
        """Pull the next pass forward to the first expiry, if one falls before it.

        Expiry is the half of enforcement that no amount of watching the counters
        will find: a client that has stopped transferring still runs out of days.
        Rather than ask every client every minute whether its date has gone, the
        earliest date still to come is worked out once, here, and the next pass
        is booked for it.

        So the cost of expiry is one comparison per pass rather than one per
        client per pass, and its precision stops depending on how the reconcile
        interval happens to be set: a date that falls thirty seconds after this
        pass is enforced thirty seconds after this pass, not at the next one.

        Only ever earlier, never later. This is a floor on when the next pass
        runs and never a substitute for it - everything else the reconcile
        corrects still has to happen on its own schedule.
        """
        if soonest is None:
            return
        due = time.monotonic() + (soonest - now).total_seconds()
        self._next_enforce = min(self._next_enforce, due)

    def _switch(self, wanted: dict[str, tuple[ClientMeta, str]], *, enabled: bool) -> list[str]:
        """Set the config markers and record why, in that order. Returns what moved.

        One call for the whole pass rather than one per client, because the
        config file is a single file: switching thirty peers off one at a time is
        thirty parses and thirty rewrites of it, sequentially, with the lock held
        the whole way - and on a wide server that is a stall every panel request
        waits out. Clients usually cross a limit
        one at a time, but an admin who lowers a default quota puts the whole set
        over at once, which is exactly when a pause is least welcome.

        The config file is what actually revokes the key - ``set_clients_enabled``
        writes the markers and re-applies the interface - so it is written first.
        A database write that fails afterwards leaves the clients correctly
        disabled with their reason showing as "manual", which is a cosmetic
        problem; the other order would leave reasons recorded for something that
        never happened.

        Only the peers the store reports as having actually moved are recorded,
        and only those are handed back. A name it skipped was already in the
        state being asked for, or had been removed from the config since this
        pass read it, and neither is something to write a reason about or to let
        the poll loop believe it changed.

        Switching a peer off takes its key off the interface, so this is the
        last moment its counters can be read at all - the next dump has no such
        peer in it, and the reading waiting to be folded is replaced by that
        dump on the very next poll. Folding first is the same guard install.sh
        wires into PreDown for the whole interface, applied to the one peer
        about to leave it.
        """
        if not wanted:
            return []
        if not enabled:
            self._flush_traffic_db()
        try:
            moved = store.set_clients_enabled(list(wanted), enabled)
        except AwgError as exc:
            self._trouble("switch", "cannot change %d client(s): %s", len(wanted), exc)
            return []
        self._recovered("switch")

        # Grouped by reason so this is one statement per distinct reason - two at
        # the very most - rather than one per client. `name` rides along because
        # it is a display cache the config is the authority on, and this pass has
        # just read the config.
        by_reason: dict[str, list[ClientMeta]] = {}
        for name in moved:
            row, reason = wanted[name]
            row.disabled_reason = reason
            row.name = name
            by_reason.setdefault(reason, []).append(row)
        try:
            for rows in by_reason.values():
                ClientMeta.objects.bulk_update(rows, ["disabled_reason", "name"], batch_size=500)
        except DatabaseError as exc:
            self._trouble(
                "enforce-db", "cannot record why %d client(s) changed: %s", len(moved), exc
            )
            connections.close_all()

        # One event per client as well as one log line, and these are the ones
        # the event log exists for: nobody pressed anything, so this decision is
        # the only record of why a client that worked yesterday is dark today.
        # The actor is left empty, which is what the panel draws as itself.
        #
        # Grouped and written per kind, for the same reason the reasons above are
        # grouped: an admin who lowers a default quota puts the whole server over
        # it at once, and that is precisely the moment not to do four thousand
        # inserts one transaction at a time. All of them carry the moment the
        # pass decided rather than the moment each row reached SQLite.
        at = timezone.now()
        by_kind: dict[str, list[str]] = {}
        for name in moved:
            reason = wanted[name][1]
            if enabled:
                log.info("client %s re-enabled: it is inside its quota and expiry again", name)
                kind = kinds.CLIENT_RESTORED
            elif reason == "quota":
                log.warning("client %s disabled: it has used its whole quota", name)
                kind = kinds.CLIENT_QUOTA_REACHED
            else:
                log.warning("client %s disabled: its expiry date has passed", name)
                kind = kinds.CLIENT_EXPIRED
            by_kind.setdefault(kind, []).append(name)
        for kind, names in by_kind.items():
            recorder.record_many(kind, names, at=at)
        return moved

    def _backfill_created(self, peers: list[store.PeerScan], meta: dict[str, ClientMeta]) -> None:
        """Copy the config's "# Created" comment into any row that has no date yet.

        A peer that arrived without going through the panel has its creation
        time in the server config and nowhere else, and a comment is the first
        casualty of a hand
        edit, a rewritten peer block or a restore from a config written before
        the comment existed. Once that happens the date cannot be worked out
        again from anything, so the panel keeps its own copy - and this is where
        the ones it did not create get theirs.

        Written once per client and never again: a row that already has a date
        is skipped, so on all but the first pass after a client appears this is
        a comparison per peer and no statement at all. A peer whose comment is
        missing or malformed is left alone rather than stamped with today, which
        would be a fabricated date that then looks authoritative.
        """
        pending = []
        for peer in peers:
            row = meta.get(peer.public_key)
            if row is not None and row.created_at is not None:
                continue
            created = parse_created(peer.created)
            if created is None:
                continue
            pending.append(
                ClientMeta(public_key=peer.public_key, name=peer.name or "", created_at=created)
            )
        if not pending:
            return

        try:
            ClientMeta.objects.bulk_create(
                pending,
                update_conflicts=True,
                update_fields=["created_at"],
                unique_fields=["public_key"],
            )
        except DatabaseError as exc:
            # Nothing is lost by failing: the config still carries the comment,
            # and the next pass tries the same rows again.
            self._trouble("created-write", "cannot record when clients were added: %s", exc)
            connections.close_all()
            return
        self._recovered("created-write")
        log.debug("recorded the creation date of %d client(s)", len(pending))

    def _reassert_disabled(self, peers: list[store.PeerScan]) -> None:
        """Take disabled peers off the interface if something put them back.

        Cheap to check and rare to act on: the keys the kernel holds came from
        the last dump, and a disabled peer among them means another writer
        re-synced the whole config while it was marked off.
        """
        disabled = {peer.public_key for peer in peers if not peer.enabled}
        leaked = disabled.intersection(self._transfers)
        if not leaked:
            return
        log.warning(
            "%d disabled peer(s) were live on %s; re-applying the configuration",
            len(leaked),
            paths.iface(),
        )
        try:
            store.apply_live(self.controller)
        except AwgError as exc:
            self._trouble("reassert", "cannot re-apply the interface configuration: %s", exc)

    # ----------------------------------------------------------- housekeeping

    def _sweep_sessions(self) -> None:
        """Reap the signed-in sessions that have run out.

        The one thing this process does that the tunnel knows nothing about, and
        it is here because this is the panel's only component that is always
        running and already has somewhere to hang a job on a timer. The
        alternative was a systemd unit and a timer of its own, which is two more
        files to install, upgrade and get wrong, for two DELETEs a day.

        A failure is logged and dropped like every other database touch in this
        file. Nothing depends on it having happened: the rows it did not take are
        exactly the rows the next pass finds, and an expired session is already
        unusable whether or not anybody has deleted it.
        """
        try:
            removed = sessions.sweep_expired()
        except DatabaseError as exc:
            self._trouble("session-sweep", "cannot sweep expired sessions: %s", exc)
            connections.close_all()
            return
        self._recovered("session-sweep")
        if removed:
            log.info("%d expired session(s) removed", removed)

    # --------------------------------------------------------------- plumbing

    def _bootstrap(self) -> None:
        """With AWG_MOCK=1 and no config, write the demo server so there is something to poll."""
        try:
            store.bootstrap_if_missing()
        except AwgError as exc:
            log.warning("could not create the demo configuration: %s", exc)

    def _keepalive(self) -> str:
        """The keepalive clients.env issues, or "" when it cannot be read.

        Read every cycle rather than cached: it is one small file, an admin can
        change it from the panel or by hand, and the alternative is a liveness
        rule that goes on using a number the server has stopped handing out.
        """
        try:
            return clientsenv.read_env().get("KEEPALIVE", "")
        except (AwgError, OSError) as exc:
            self._trouble("keepalive", "cannot read the keepalive from clients.env: %s", exc)
            return ""

    def _poll_sec(self) -> float:
        return float(max(settings_store.get_int("trafficPollSec", int(DEFAULT_POLL_SEC)), 1))

    def _enforce_sec(self) -> float:
        return float(max(settings_store.get_int("enforceIntervalSec", int(DEFAULT_ENFORCE_SEC)), 5))

    def _install_signals(self) -> None:
        """Stop at the end of the current cycle, so the final flush still happens."""

        def handler(signum: int, frame: FrameType | None) -> None:
            log.info("signal %d received, finishing the current cycle", signum)
            self.stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, handler)
            except ValueError:
                # Not the main thread: a test driving the loop directly, which
                # stops it by setting the event itself.
                return

    def _trouble(self, key: str, message: str, *args: object) -> None:
        """Log a recurring fault once per cooldown, and at debug in between.

        A server with no `awg` installed hits the same failure every two
        seconds. One line every five minutes says what is wrong without burying
        everything else in the journal.
        """
        now = time.monotonic()
        last = self._logged.get(key)
        if last is None or now - last >= ERROR_COOLDOWN_SEC:
            self._logged[key] = now
            log.warning(message, *args)
        else:
            log.debug(message, *args)

    def _recovered(self, key: str) -> None:
        """Forget a fault, so the next occurrence is logged immediately."""
        self._logged.pop(key, None)


class Command(BaseCommand):
    help = (
        "Poll the AmneziaWG interface: live stats, today's traffic, quota and expiry "
        "enforcement. Runs until stopped."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single cycle, flush, and exit. Useful for cron-style checks and tests.",
        )

    def handle(self, *args: object, **options: object) -> None:
        Collector(once=bool(options.get("once"))).run()
