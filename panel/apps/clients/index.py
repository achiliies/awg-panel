"""Keep ClientIndex level with the files, and be the only module that writes it.

The client list used to read every file the answer could possibly depend on, on
every request, and then throw almost all of it away: a page of fifty rows on a
four thousand client server parsed thirty thousand lines of server config, opened
four thousand client configs for one line each, read traffic.db, built four
thousand dictionaries and sorted them - about half a second, and the dashboard
asked for the same work again every two seconds. None of that grew with what was
being *shown*. All of it grew with the server.

A database does this properly, and the panel already has one. The catch is that
the config files are still the truth - an admin edits them by hand over SSH, a
restore replaces them wholesale, the PreDown hook writes traffic.db with nothing
else running - so a copy in the database is only worth having if it can never
quietly disagree with them.

That is what this module is. Four functions matter:

* ``current()`` is called before every read. It stats three paths, compares six
  numbers with the ones stored when the index was last built, and returns. On a
  server where nothing has changed - which is every request between one edit and
  the next - that is the entire cost.
* ``rebuild()`` runs when those numbers differ. It reads all the files once,
  writes every row, and stores the new stamps.
* ``absorb()`` is how a change the panel made itself avoids that read. It is
  registered with awg.store, which calls it inside the config lock once a
  mutation's files are written, telling it which peers were added, removed or
  edited; it writes those rows and stamps the files it did not have to open.
* ``absorb_counters()`` is the same bargain for traffic.db, which is not edited
  by anybody but is rewritten every ten seconds forever. The collector writes it
  and already knows which peers moved and what they now hold, so it says so
  rather than leaving the next request to work it out; ``refresh_counters()``
  stays as what answers when somebody else wrote that file.

So the O(clients) read still happens; it happens once per *change* instead of
once per *request*, and reads outnumber changes by whatever a polling browser
divided by an admin's typing speed comes to. absorb() removes it from the
changes the panel made itself, which is what provisioning over the API is: five
hundred clients added one POST at a time were five hundred rebuilds, and a
config the panel just wrote is one it has already parsed.

Everything that writes these tables is in this module, and that is load-bearing
rather than tidy. Two writers of a row can disagree about a column, and a column
they disagree about is a row that is wrong while the stamp says the index is
current - so nothing ever re-reads the file that would correct it, which is the
one failure this design cannot see. Both paths therefore build a row through the
same ``row_for()`` and number it through the same ``indexable()``, so they cannot
drift apart by construction, and the tests assert the two produce equal tables
column for column over ``FIELDS`` rather than over a list somebody maintains.

What is left of that risk is bounded rather than argued away. The first read in
each worker rebuilds unconditionally, so a row absorb got wrong cannot outlive a
restart; a mutation that cannot describe what it changed announces nothing and is
caught by the stamps exactly as a hand edit is; and dropping both tables
at any moment still costs one rebuild and loses nothing.

One writer in the code is not one writer on the box, though, and the difference
had to be closed with a lock of its own. The panel runs two gunicorn workers
against one SQLite file, so an edit lands while both have a request in flight and
both go to rebuild from the same files at the same moment. SQLite will not queue
that: a transaction that reads before it writes cannot take the write lock while
another holds the same read, and it fails immediately rather than waiting, so no
busy timeout can absorb it. Both rebuilds then failed, and on a panel that had
not built an index yet the fallback was an empty table - a client list answering
"this server has no clients". So rebuilding is serialised on a flock of its own,
in the run directory: whoever takes it does the work, and the rest arrive to find
the stamp already current. It is not the config lock, because none of this
changes a file anything else reads, and putting a mutation behind a browser poll
would be a real cost for no benefit.

The rebuild does one thing that is not a plain re-read: a client's AllowedIPs
lives in its own config file, and re-opening four thousand of those is most of
what made the old path slow. Each row remembers the modification time and size of
the file it read that value from, so a rebuild triggered by an unrelated change -
which is nearly all of them, since adding one client rewrites the server config
and no other client's file - stats each file and re-reads only the ones that
moved. A stat is about a microsecond and an open-and-parse about a hundred.

What this cannot see is a client config edited in place, by a text editor that
writes through the file rather than replacing it. That changes no directory
entry, so the clients directory's own timestamp does not move and nothing here
notices until the next change to anything. One client's AllowedIPs is stale until
then. It is the single hole in the scheme and it is worth stating plainly rather
than papering over: everything else - both CLIs, the panel, a restore, a hand
edit of the server config - moves one of the three stamps.
"""

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from awg import clientsenv, store, traffic
from awg.errors import AwgError, NotConfigured
from awg.lock import file_lock
from awg.paths import client_dir, index_lock_file, server_conf, traffic_db

from .models import ClientIndex, ClientIndexState, ClientMeta

log = logging.getLogger(__name__)

# A metadata row is stale once its public key leaves the server config, but a key
# rotation writes the new key to the config a moment before the row catches up.
# Rows touched recently are left alone so that window cannot eat one.
PRUNE_GRACE_SEC = 300

# How long a request will wait for another process to finish rebuilding. Long
# enough to cover a rebuild on a very large server - the waiter then gets the
# finished index rather than doing the work again - and short enough that a stuck
# holder cannot hold a page open past the point anybody is still looking at it.
# A request waiting here holds nothing that anybody else needs.
LOCK_TIMEOUT_SEC = 15.0

# How long absorb() will wait for the same lock, which is a different question
# with a different answer because absorb waits *inside* the config lock. Time
# spent here is time every other mutation is queued behind, so waiting long does
# not save a fold - it stalls the next admin's edit while a browser poll
# rebuilds.
#
# Short, therefore, and cheap to be short about: what a fold that gives up costs
# is the rebuild it was avoiding, which is the answer that was there before this
# path existed.
ABSORB_LOCK_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class Stamp:
    """What three stat() calls say about the files the index was built from.

    Compared as a whole: any field differing means a rebuild, and there is no
    attempt to work out which files changed from which fields moved. The rebuild
    reads everything anyway, and a partial refresh keyed off a guess about what
    an edit touched is the kind of cleverness this module exists to avoid.
    """

    conf_mtime_ns: int = 0
    conf_size: int = 0
    conf_inode: int = 0
    clients_mtime_ns: int = 0
    traffic_mtime_ns: int = 0
    traffic_size: int = 0

    @classmethod
    def observe(cls) -> "Stamp":
        """Read the stamps off disk. A missing file stamps as zero, not as an error.

        Zero is what the state row holds before anything has ever been built, so
        a file that does not exist yet reads as "the index is current" once one
        rebuild has recorded its absence. That is right: there is nothing to
        index, and a panel installed before the VPN should not rebuild on every
        request until somebody runs the installer.
        """
        conf = _stat(server_conf())
        clients = _stat(client_dir())
        counters = _stat(traffic_db())
        return cls(
            conf_mtime_ns=conf[0],
            conf_size=conf[1],
            conf_inode=conf[2],
            clients_mtime_ns=clients[0],
            traffic_mtime_ns=counters[0],
            traffic_size=counters[1],
        )

    @classmethod
    def stored(cls, state: ClientIndexState) -> "Stamp":
        return cls(
            conf_mtime_ns=state.conf_mtime_ns,
            conf_size=state.conf_size,
            conf_inode=state.conf_inode,
            clients_mtime_ns=state.clients_mtime_ns,
            traffic_mtime_ns=state.traffic_mtime_ns,
            traffic_size=state.traffic_size,
        )

    @property
    def config(self) -> tuple[int, ...]:
        """The part of the stamp that describes what a client *is*.

        Separated from the traffic figures because the two change on completely
        different clocks and cost completely different things to take account of.
        The config moves when an admin does something, which is rarely; traffic.db
        is rewritten by the collector every ten seconds, forever. Rebuilding the
        whole index on that second clock would be a config parse, four thousand
        stats and four thousand rows every ten seconds - more work than the
        per-request reading this replaced, and permanently on.
        """
        return (self.conf_mtime_ns, self.conf_size, self.conf_inode, self.clients_mtime_ns)

    @property
    def counters(self) -> tuple[int, ...]:
        """The other half: which version of traffic.db the usage columns are from."""
        return (self.traffic_mtime_ns, self.traffic_size)


@dataclass(frozen=True)
class MetaRow:
    """The part of a ClientMeta row a rebuild needs, without the row.

    `pk` is what ClientIndex points at, `created_at` is the fallback behind
    created_key for a peer whose config carries no "# Created" comment, and
    `updated_at` is what the prune's grace period is measured from.
    """

    pk: int
    created_at: datetime | None
    updated_at: datetime | None


def _stat(path: Path) -> tuple[int, int, int]:
    """(mtime_ns, size, inode), or zeroes for anything that cannot be stat'd."""
    try:
        info = path.stat()
    except OSError:
        return (0, 0, 0)
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def current() -> ClientIndexState:
    """Return the index state, having rebuilt it first if the files have moved.

    The one call every read path makes. Three stats and a single-row select on
    the way through, and on the overwhelmingly common path - nothing has changed
    since the last request - that is all it does.

    Two kinds of staleness, because they cost different things to fix. A config
    that has moved means a full rebuild. Counters that have moved - which is
    every ten seconds, for as long as the collector runs - mean only that two
    columns are behind, and re-reading one text file and writing the handful of
    rows whose figures actually changed is a fraction of the work.

    A rebuild that fails returns the state as it stands rather than raising.
    Serving a slightly old client list beats answering a poll with a 500, and
    nothing has been lost: the stamps are only written when a rebuild finishes,
    so the next request tries again.

    Two failures travel instead, and both for the same reason - that the honest
    answer is not an empty client list.

    There being no server config at all is not a failed read but a state the
    panel has a page for: a standalone install, an AWG_IFACE naming an interface
    that was never created, a fresh container. All three want that page rather
    than a table saying this server has no clients on it.

    And a failure before anything has ever been built has nothing to fall back
    *to*. "Slightly old" is only a kindness while there is something old to
    serve; before the first successful build the alternative is an empty table,
    which is the more alarming of the two things to be wrong about.
    """
    global _verified

    state = _state()
    observed = Stamp.observe()
    stored = Stamp.stored(state)
    if stored == observed and _verified:
        return state
    try:
        # One rebuild at a time across the whole box. Without this every gunicorn
        # worker with a request in flight reads the files and writes the same rows
        # at the same moment - and SQLite refuses that rather than queueing it,
        # because a transaction that reads before it writes cannot be upgraded
        # while another holds the same read, which is a deadlock no busy timeout
        # can wait out. Whoever gets the lock does the work; the rest arrive to
        # find the stamp already current and return it.
        #
        # Not the config lock: this writes no file anything else reads, and
        # putting a client add behind a browser poll would be a real cost for
        # no benefit.
        with file_lock(index_lock_file(), timeout=LOCK_TIMEOUT_SEC):
            # Both questions are asked again in here, because holding the lock is
            # the only place either answer is stable. This worker serves requests
            # on a pool of threads, so the ones behind the first arrive with a
            # verification that has since happened and stamps that have since
            # been written, and doing the work again on that basis is doing it
            # four times.
            first = not _verified
            if not first and stored == observed:
                return state
            if first or stored.config != observed.config:
                # Only once it has finished. Set on the way in, a rebuild that
                # failed - a locked database, a config that could not be read -
                # would spend the one unconditional pass this worker gets, and a
                # row that absorb got wrong would then have nothing left to
                # correct it before the process ends.
                state = rebuild(observed, force=first)
                _verified = True
                return state
            return refresh_counters(observed)
    except NotConfigured:
        raise
    except (AwgError, DatabaseError, OSError) as exc:
        # Never built and cannot be built is not the same as "this server has no
        # clients", and answering a client list with an empty one would be the
        # more alarming of the two lies. Once there is something to fall back on,
        # falling back is right: slightly old beats a 500 on a poll.
        if stored == Stamp():
            raise
        log.warning("cannot refresh the client index: %s", exc)
        return state


def _state() -> ClientIndexState:
    """The single state row, created empty the first time anything asks."""
    state, _ = ClientIndexState.objects.get_or_create(id=ClientIndexState.ROW_ID)
    return state


# Set once per process, by the first rebuild that finishes. absorb() writes rows
# without reading the files, so a bug in it is a row nothing corrects: the stamp
# says the index is current, and being current is exactly what stops anything
# looking at the file again. One unconditional rebuild per worker bounds how long
# such a row can survive to the life of that worker, which any restart or deploy
# ends, and costs one rebuild on a process that has just started and has nothing
# cached anyway.
#
# By the first rebuild that *finishes*, which is the whole of what it is worth:
# a pass that gave up on a locked database has verified nothing, and marking it
# done would spend the guarantee without delivering it. Read and written under
# the index lock for the same reason - four threads that each take it for
# themselves are four rebuilds of the same files. Tests that want the plain path
# set this to True.
_verified = False


def rebuild(observed: Stamp | None = None, *, force: bool = False) -> ClientIndexState:
    """Read the files and write every row, in one transaction.

    The stamp is taken *before* the files are read, never after. A write landing
    while this runs is then guaranteed to leave the stored stamp describing a
    file older than the one on disk, so the next request rebuilds and picks the
    change up. Stamping afterwards would record the new file against rows built
    from the old one, and the disagreement would last until something else
    changed - which is the one failure this whole design exists to make
    impossible.
    """
    observed = observed if observed is not None else Stamp.observe()
    scan = store.scan_server()
    env = clientsenv.read_env()
    counters = _counters()

    with transaction.atomic():
        # If somebody rebuilt from these same files while this one was reading
        # them, there is nothing to do: both observed the same stamp, so both
        # would write the same rows. current() takes a lock that makes this rare,
        # but this function is callable on its own and the check is a select.
        state = _state()
        if not force and Stamp.stored(state) == observed:
            return state

        keys = _named_keys(scan.peers)
        metas = _metas(keys)
        # Every stored row, by public key: its id, the values it holds, and the
        # stamp of the client config file those values were read from.
        stored = {
            row[0]: (row[1], row[2:])
            for row in ClientIndex.objects.values_list("meta__public_key", "id", *FIELDS)
        }

        rows = []
        for position, peer in indexable(scan):
            was = stored.get(peer.public_key)
            rows.append(
                (
                    peer.public_key,
                    row_for(
                        peer,
                        metas[peer.public_key],
                        position,
                        _usage(counters, peer.public_key, was[1] if was else None),
                        _client_conf(peer.name, was[1] if was else None, env),
                    ),
                )
            )

        _sync(rows, stored)
        _prune(set(keys), metas)
        return _store_stamp(_stamp_to_keep(observed, state, counters), scan)


class Unabsorbable(Exception):
    """This change cannot be applied to the rows in hand, so rebuild instead.

    Raised inside absorb's transaction, which is what makes it safe to raise
    halfway through: the partial work rolls back, and the caller marks the index
    stale so the next read rebuilds. Every one of these is a correctness escape
    hatch and none of them is an error an operator can act on, so they are logged
    at debug and cost one rebuild.
    """


# How many peers a single change may name before the fast path stops being the
# cheap option. Each removal costs a statement of its own to close the gap it
# left in `position`, and the keys travel in an IN clause that SQLite declares a
# 999 parameter limit for - so a bulk delete of a thousand expired clients is
# both slower here and closer to that edge than one rebuild would be.
ABSORB_MAX_PEERS = 64

# Where begin() leaves the stamp it took, for the absorb() that follows it on the
# same thread. Never read anywhere else, and never outlives the mutation that
# took it - see finish().
_pending = threading.local()


def begin() -> None:
    """Note how the files looked before the mutation about to run touches them.

    Registered with awg.store beside absorb() and called at the top of every
    mutation that goes on to announce, while the files are still untouched and
    the config lock is already held.

    Without it absorb cannot tell its own change apart from somebody else's. The
    index may be describing the files as they were two edits ago - another
    worker added a peer, a hand edit renamed one - and applying this delta to
    that copy and calling the result current would drop whatever happened in
    between, permanently: the stamp would say the index is level with the files,
    so nothing would ever read them again to find out otherwise.

    Thread-local because the mutation, this, and absorb all run on one thread
    inside one lock, and two gunicorn threads can be mid-mutation at once.
    """
    _pending.stamp = Stamp.observe()


def finish() -> None:
    """Forget that stamp, whatever became of the mutation that took it.

    Registered beside begin() and called by awg.store on the way out of every
    mutation, taken or not, announced or not. Which is the point: a mutation
    that ends without announcing is ordinary - a name already taken, a peer
    already in the state being asked for, a write that raised - and the stamp it
    took describes files that have since been written.

    A stamp left standing is not stale in the harmless sense. It was taken
    before *that* mutation, so it matches what the index has stored, and the
    next change on this thread that reaches absorb without a stamp of its own
    would be measured against it, pass the check that exists to catch exactly
    this, and mark the index level with files nothing here has read. What is
    dropped that way is whatever landed in between, silently and until the
    worker restarts. Clearing it costs nothing and is what makes forgetting to
    call begin() a re-read rather than an error.
    """
    _pending.stamp = None


def absorb(conf: store.ServerConf, change: store.ConfChange) -> None:
    """Fold a config write the panel just made into the index, without re-reading it.

    Registered with awg.store as an observer, so this runs on the thread that
    did the write, inside the config lock it still holds, once every file the
    change touches is on disk.

    What it saves is the rebuild: a config the panel wrote is a config the panel
    already has parsed, and re-reading thirty thousand lines to learn what it
    just did is most of the cost. Provisioning is where that matters. One client
    added in a browser triggers a rebuild nobody perceives, but five hundred
    added over the API one request at a time is five hundred of them.

    On four thousand clients an add and the read after it costs 424 ms with a
    rebuild between them and 177 ms without, so provisioning five hundred goes
    from about three and a half minutes to about one and a half. The rest is the
    add itself, which is irreducible here: the server config is one file, so
    adding a peer re-parses and rewrites all of it whatever this does. What is
    removed is the *second* pass over it, on the read afterwards.

    Every path out of here that is not the fast one leaves the stamp describing
    files older than the ones on disk, which is today's behaviour exactly: the
    next read rebuilds. So this is only ever an optimisation over an answer that
    was already correct, and a change it cannot apply, a lock it cannot take or a
    row it cannot find all cost a rebuild rather than accuracy.
    """
    # Read once and cleared here rather than only in finish(), so that a config
    # write announced twice cannot have the second one placed by the first one's
    # stamp.
    before = getattr(_pending, "stamp", None)
    _pending.stamp = None
    try:
        if before is None:
            raise Unabsorbable("the mutation never said when it started")
        # Against a rebuild in another worker, not against another writer: the
        # config lock this is already inside serialises those. Taken in that
        # order everywhere, and rebuild takes only this one, so the pair cannot
        # deadlock.
        #
        # On its own budget, because this is the one place the index lock is
        # waited for by somebody holding the config lock - see
        # ABSORB_LOCK_TIMEOUT_SEC.
        with file_lock(index_lock_file(), timeout=ABSORB_LOCK_TIMEOUT_SEC):
            _absorb(conf, change, before)
    # Neither of these invalidates, and it would be a mistake to. The stamp is
    # written once, at the end of _absorb, inside its transaction - so anything
    # that gets here left it describing the files as they were before this
    # mutation wrote them, and the mutation has written. It is already older than
    # what is on disk, which is the whole of what makes the next read rebuild.
    #
    # Zeroing it on top of that would say something further and untrue: current()
    # reads an all-zero stamp as "never built", and answers a failed rebuild from
    # that state by raising rather than serving what it has. A poll arriving
    # during a locked database would then get a 500 where it used to get a
    # slightly old client list.
    except Unabsorbable as exc:
        log.debug("rebuilding the client index instead of folding a change in: %s", exc)
    except (AwgError, DatabaseError, OSError) as exc:
        log.warning("could not fold a config change into the client index: %s", exc)


def _absorb(conf: store.ServerConf, change: store.ConfChange, before: Stamp) -> None:
    """Apply one described change to the rows, then stamp the files as read.

    The stamp is taken after the writes rather than before, which is the reverse
    of what rebuild() does and is only sound because of where this runs. A
    rebuild reads files nobody is holding still, so it stamps first and lets a
    concurrent write show up as a stamp that looks old. This runs inside the
    config lock, holding the config it just wrote, so between that write and the
    stat here nothing that takes the lock can intervene.

    What can still intervene is a writer that takes no lock - a text editor on
    the server config. That edit lands in the file, this stats it, and the stamp
    then describes a file this never read. The window is the few microseconds
    between the two, against an edit that is already racing the panel for the
    same file, and the alternative - stamping before the write - would record
    the config as unread every time and give up the whole saving.
    """
    parts = [part for part in (change.added, change.removed, change.updated) if part]
    if len(parts) > 1:
        # No mutation in awg.store produces one, and the three helpers below are
        # only correct on their own: `position` is read out of the config as it
        # is now, while a removal shifts the rows that are still stored, so an
        # insert and a removal in one change would renumber against each other.
        # Refusing beats carrying an ordering that is only sometimes right.
        raise Unabsorbable("a change that adds, removes and edits at once")
    total = sum(len(part) for part in parts)
    if total > ABSORB_MAX_PEERS:
        raise Unabsorbable(f"{total} peers in one change")

    env = clientsenv.read_env()
    scan = store.scan_of(conf, env)
    with transaction.atomic():
        state = _state()
        if Stamp.stored(state).config != before.config:
            # The index was not describing the files this change started from, so
            # this delta does not lead from what is stored to what is on disk.
            # Something else wrote in between - another worker, a hand edit over
            # SSH, a restore - or the index has simply never been built, which
            # stores as zeroes and lands here too. Applying the delta anyway
            # would drop whatever that was and then stamp the result current,
            # which is the one way this table can be wrong and stay wrong.
            #
            # Only the config half is compared. traffic.db moves every ten
            # seconds under the collector and says nothing about which peers
            # exist, so requiring it to match would hand nearly every change back
            # to the rebuild for no reason.
            raise Unabsorbable("the index was already behind the files")

        at = {peer.public_key: (position, peer) for position, peer in indexable(scan)}
        if len(at) != sum(1 for peer in scan.peers if peer.name):
            # A public key on two peer entries, which only a hand edit or a
            # botched merge produces. indexable keeps the first and drops the
            # rest, so a named peer here has no row of its own - and a removal
            # addressed by public key would delete the row belonging to its twin,
            # while the positions of everything after it moved by one more than
            # this can account for. The rebuild handles the same config by
            # logging and keeping the first, which is the right place for it.
            raise Unabsorbable("a public key appears on more than one peer")
        _absorb_removed(change.removed)
        _absorb_added(change.added, at, env)
        _absorb_updated(change.updated, at, env)

        # Only the config half of the stamp. traffic.db was never opened here -
        # a new peer has no counters and an edited one keeps the ones it had - so
        # claiming it as read would freeze the usage column until it next moved.
        was = Stamp.stored(state)
        observed = Stamp.observe()
        _store_stamp(
            replace(
                observed,
                traffic_mtime_ns=was.traffic_mtime_ns,
                traffic_size=was.traffic_size,
            ),
            scan,
        )


def _absorb_removed(keys: tuple[str, ...]) -> None:
    """Drop the rows for departed peers and close the gaps they left in `position`.

    Positions are indices into the config's peer list, so removing a peer shifts
    every peer after it down by one. Closing that with one UPDATE per removal -
    highest first, so the shifts do not tread on each other - keeps the column
    exactly what a rebuild would have written, which is what lets the two paths
    be compared row for row in the tests rather than merely "in the same order".
    """
    if not keys:
        return
    doomed = ClientIndex.objects.filter(meta__public_key__in=keys)
    positions = sorted(doomed.values_list("position", flat=True), reverse=True)
    doomed.delete()
    for position in positions:
        ClientIndex.objects.filter(position__gt=position).update(position=F("position") - 1)

    # The same rule _prune applies on a rebuild, narrowed to the keys that just
    # left rather than read off the whole table. The grace period is still what
    # stops a key rotation - which writes the new key a moment before the row
    # follows it - from taking somebody's quota with it.
    cutoff = timezone.now() - timedelta(seconds=PRUNE_GRACE_SEC)
    removed, _ = ClientMeta.objects.filter(public_key__in=keys, updated_at__lt=cutoff).delete()
    if removed:
        log.info("pruned %d client metadata row(s) for removed peer(s)", removed)


def _absorb_added(
    keys: tuple[str, ...], at: dict[str, tuple[int, store.PeerScan]], env: dict[str, str]
) -> None:
    """Insert a row per new peer, opening its metadata row and reading its config file.

    Counters start at zero rather than being looked up, and that is exact rather
    than approximate: a peer announced as added carries a keypair generated a
    moment ago, and traffic.db is keyed by public key, so there is nothing there
    to find. Reading it to prove that would be the O(clients) file read this
    exists to skip.
    """
    found = []
    for key in keys:
        entry = at.get(key)
        if entry is None:
            raise Unabsorbable(f"peer {key[:12]} was announced as added but is not in the config")
        found.append(entry)

    # Lowest position first, which matters only when there is more than one and
    # then decides the answer. Each insert shifts the rows at or after it, so a
    # later peer inserted first is shifted again by an earlier one and ends up
    # one place further down than the config puts it - with the row it displaced
    # above it rather than below. Ascending, each shift lands on rows that are
    # all genuinely after the peer being inserted. No caller announces two adds
    # today; sorting is a line, and the alternative is a rule that happens to
    # hold because of the order a caller builds a tuple in.
    for position, peer in sorted(found, key=lambda entry: entry[0]):
        # An append leaves nothing to shift and this matches no rows, which is
        # every add_client. It is here for an insert in the middle, which would
        # otherwise duplicate a position.
        ClientIndex.objects.filter(position__gte=position).update(position=F("position") + 1)
        meta, _ = ClientMeta.objects.get_or_create(public_key=peer.public_key)
        row_for(peer, meta, position, (0, 0), _client_conf(peer.name, None, env)).save()


def _absorb_updated(
    keys: tuple[str, ...], at: dict[str, tuple[int, store.PeerScan]], env: dict[str, str]
) -> None:
    """Recompute the row of a peer that stayed, from the same builder a rebuild uses.

    Its counters are carried over rather than re-read for the same reason they
    are in a rebuild that could not open traffic.db: they are a column this
    change says nothing about, and the next refresh_counters brings them level.
    """
    for key in keys:
        found = at.get(key)
        if found is None:
            raise Unabsorbable(f"peer {key[:12]} was announced as updated but is not in the config")
        position, peer = found
        row = ClientIndex.objects.select_related("meta").filter(meta__public_key=key).first()
        if row is None:
            raise Unabsorbable(f"peer {key[:12]} was announced as updated but has no row")
        was = tuple(getattr(row, field) for field in FIELDS)
        fresh = row_for(
            peer,
            row.meta,
            position,
            (row.cum_rx, row.cum_tx),
            _client_conf(peer.name, was, env),
        )
        fresh.pk = row.pk
        fresh.save()


def indexable(scan: store.ServerScan) -> Iterator[tuple[int, store.PeerScan]]:
    """The peers a row is kept for, each with the position that row records.

    Shared by the rebuild and by absorb() so that both agree on what `position`
    means, which they have to: it is the tiebreaker every other sort falls back
    on, so a fast path that counted it differently from a rebuild would reorder
    the client list the moment one ran after the other.

    Positions are counted over every peer in the config, including the ones
    skipped here. That is deliberate - the number's job is to preserve the order
    of the file, not to be dense - and it means skipping a peer never shifts the
    ones after it.
    """
    taken: set[str] = set()
    for position, peer in enumerate(scan.peers):
        if not peer.name:
            # Counted against the address pool by scan_server and left out of the
            # rows: the API addresses a client by name, so a peer without one has
            # no identity to offer.
            continue
        if peer.public_key in taken:
            # Two peer entries sharing a public key. Only a hand edit or a botched
            # merge produces one, and the kernel will not hold both either - but a
            # row per peer would be two rows against the one metadata row they
            # share, which the database refuses, and the failed rebuild would
            # leave the panel showing no clients at all until somebody found the
            # duplicate. The first entry wins, which is the same one every lookup
            # by name in awg.store settles on.
            log.warning("public key of client %s appears twice in the config", peer.name)
            continue
        taken.add(peer.public_key)
        yield position, peer


def row_for(
    peer: store.PeerScan,
    meta: "MetaRow | ClientMeta",
    position: int,
    usage: tuple[int, int],
    conf_read: tuple[str, bool, int, int],
) -> ClientIndex:
    """One row, from the five things a row is made of. The only place that builds one.

    Pulled out of the rebuild's loop so that absorb() can produce a row for a
    single peer without reproducing this, because reproducing it is precisely
    the failure that makes a second writer dangerous: two builders that agree on
    thirteen columns and differ on the fourteenth leave a row that is wrong, and
    the stamp then says the index is current, so nothing ever reads the file
    again to find out. One function means the two paths cannot drift, and a
    column added later lands in both by construction rather than by remembering.

    The five arguments are the five things this cannot work out for itself, each
    from a different place: the peer entry from the server config, the metadata
    row it hangs off, where the peer sits among the peers, its counters from
    traffic.db, and what its own config file said when it was last read. A caller
    that has to think about where each of those comes from is a caller that
    cannot accidentally leave one at a default.
    """
    allowed, present, mtime, size = conf_read
    return ClientIndex(
        meta_id=meta.pk,
        name=peer.name,
        ip=peer.ip,
        ip6=peer.ip6,
        enabled=peer.enabled,
        position=position,
        created_key=peer.created or _iso(meta.created_at) or "",
        name_key=(peer.name or peer.public_key[:12]).strip().casefold(),
        ip_order=address_order(peer.ip),
        cum_rx=usage[0],
        cum_tx=usage[1],
        allowed_ips=allowed,
        has_conf_file=present,
        conf_mtime_ns=mtime,
        conf_size=size,
    )


def _stamp_to_keep(
    observed: Stamp, state: ClientIndexState, counters: dict[str, traffic.Counters] | None
) -> Stamp:
    """The stamp to record, which is what was observed unless a file went unread.

    A rebuild that could not read traffic.db carried the previous usage figures
    forward rather than zeroing them, and recording traffic.db as current would
    then claim those figures came from the file on disk. Worse, it would stop
    anything looking again until that file next changed - so a permissions
    problem cleared a second later would leave the usage column frozen for as
    long as nothing wrote to it. Leaving that half of the stamp behind is what
    makes the next request try the file again.
    """
    if counters is not None:
        return observed
    was = Stamp.stored(state)
    return replace(observed, traffic_mtime_ns=was.traffic_mtime_ns, traffic_size=was.traffic_size)


# The columns a rebuild computes, in the order the comparison in _sync reads
# them. `meta` is not among them: a row is identified by its peer's public key,
# so a row whose key changed is a different client and a different row.
FIELDS = (
    "name",
    "ip",
    "ip6",
    "allowed_ips",
    "enabled",
    "has_conf_file",
    "created_key",
    "name_key",
    "ip_order",
    "position",
    "cum_rx",
    "cum_tx",
    "conf_mtime_ns",
    "conf_size",
)

# Where in a FIELDS tuple the client config file's own stamp and the values read
# from it sit, so _client_conf can be handed the previous reading without the
# caller unpacking fourteen columns to find four.
_CONF_SLICE = (
    FIELDS.index("conf_mtime_ns"),
    FIELDS.index("conf_size"),
    FIELDS.index("allowed_ips"),
    FIELDS.index("has_conf_file"),
)

# And where the counters sit, for the same reason: keeping the last reading when
# traffic.db cannot be read.
_USAGE_SLICE = (FIELDS.index("cum_rx"), FIELDS.index("cum_tx"))


def _sync(rows: list[tuple[str, ClientIndex]], stored: dict[str, tuple[int, tuple]]) -> None:
    """Write the rows that are not already what they should be, and delete the rest.

    A rebuild recomputes every row from the files whatever happens - that is what
    keeps this table honest, and it is not what costs anything. Writing is what
    costs: Django batches inserts to SQLite at 999 placeholders a statement,
    which with these columns is sixty rows, so re-inserting four thousand of them
    is sixty round trips and about a quarter of a second.

    Almost none of it is ever needed. Adding a client changes one row and leaves
    three thousand nine hundred and ninety nine byte-identical; so does renaming
    one, disabling one, or editing one client's routes. So the recomputed row is
    compared against what is stored, and only the differences are written.

    This is a saving in the writing, never in the deciding. Every row was still
    built from the files a moment ago, and a row that matches is a row the files
    agree with - so nothing can be left stale by skipping it, which is the one
    thing that would make this trade a bad one.
    """
    create, update = [], []
    for key, row in rows:
        was = stored.pop(key, None)
        if was is None:
            create.append(row)
        elif was[1] != tuple(getattr(row, field) for field in FIELDS):
            row.pk = was[0]
            update.append(row)
    if create:
        ClientIndex.objects.bulk_create(create, batch_size=500)
    if update:
        ClientIndex.objects.bulk_update(update, FIELDS, batch_size=200)
    # Whatever is left in `stored` was not in the config this time round.
    if stored:
        gone = [entry[0] for entry in stored.values()]
        ClientIndex.objects.filter(pk__in=gone).delete()
        log.info("dropped %d client index row(s) with no peer in the config", len(gone))


def _prune(keys: set[str], metas: dict[str, "MetaRow"]) -> None:
    """Delete metadata rows whose peer has left the config.

    This used to happen on every read of the client list, because that was the
    only moment the panel was guaranteed to know the full set of live public
    keys - a peer can leave the config while nothing here is running, through a
    hand edit or a restore. A rebuild is that moment now, and a better one: it is the only thing
    that reads the whole config, and it runs when the config has actually changed
    rather than whenever a browser polled.

    The grace period is unchanged and still load-bearing. A key rotation writes
    the new key to the config a moment before the row follows it, and without the
    window that gap reads as a removal and takes somebody's quota with it.

    A config with no named peers is the one case where none of this reasoning
    holds. That is what a truncated write, a half-finished restore or a botched
    hand edit leaves behind - parse_conf reports it as an empty peer list rather
    than an error - so acting on it would delete every quota, expiry date, usage
    offset and note on the server, irreversibly. Removing the last client is
    rare, its row is dropped by the delete handler anyway, and a row nothing
    matches is ignored by every reader. Skipping this costs nothing and the
    alternative cannot be undone.
    """
    if not keys:
        return
    cutoff = timezone.now() - timedelta(seconds=PRUNE_GRACE_SEC)
    # Picked out of the rows already in hand rather than with an `exclude(...__in)`
    # carrying a placeholder per client - see _metas for why that is worth
    # avoiding. What is left is a delete by primary key over the few that qualify.
    stale = [
        row.pk
        for key, row in metas.items()
        if key not in keys and row.updated_at is not None and row.updated_at < cutoff
    ]
    if not stale:
        return
    removed, _ = ClientMeta.objects.filter(pk__in=stale).delete()
    if removed:
        log.info("pruned %d client metadata row(s) with no peer in the config", removed)


def _usage(
    counters: dict[str, traffic.Counters] | None, key: str, was: tuple | None
) -> tuple[int, int]:
    """This client's all-time counters, or the last ones that were read for it.

    Zero for a client traffic.db has no row for, because that is what it means -
    but the previous reading when the file itself could not be read, since
    zeroing every usage figure on the server is not a truthful way to say "the
    disk did not answer".
    """
    if counters is None:
        rx_at, tx_at = _USAGE_SLICE
        return (was[rx_at], was[tx_at]) if was is not None else (0, 0)
    counter = counters.get(key)
    return (counter.cum_rx, counter.cum_tx) if counter else (0, 0)


def _named_keys(peers: list[store.PeerScan]) -> list[str]:
    """The public keys of the peers that can be listed, each one once.

    Deduplicated because a metadata row is opened per key and a config can name
    the same key twice; see the rebuild for what that costs if it is not caught.
    """
    seen: dict[str, None] = {}
    for peer in peers:
        if peer.name:
            seen.setdefault(peer.public_key)
    return list(seen)


def _client_conf(name: str, was: tuple | None, env: dict[str, str]) -> tuple[str, bool, int, int]:
    """What this client's own config says, re-reading the file only if it moved.

    Takes the row stored for this client last time, as a FIELDS tuple, and
    returns (allowed_ips, file exists, mtime_ns, size).

    The stat is the point of the whole function. A rebuild is nearly always
    triggered by the server config changing, and adding, renaming, disabling or
    deleting one client leaves every other client's file exactly as it was - so
    on a four thousand client server this reads one file and stats the rest. An
    open and a parse is around a hundred microseconds and a stat is around one,
    which on that server is the difference between a rebuild of nine hundred
    milliseconds and one of six hundred.

    A file that has gone stamps (0, 0) and is re-read, which costs one failed
    open and leaves `allowed_ips` at whatever the server default is. That is what
    the old path answered for a client whose config file is missing, and it is
    also why (0, 0) is never treated as a match: a config that appears later has
    to be picked up.
    """
    mtime_at, size_at, allowed_at, present_at = _CONF_SLICE
    mtime, size = store.client_conf_stat(name)
    if (
        was is not None
        and (mtime, size) != (0, 0)
        and (was[mtime_at], was[size_at]) == (mtime, size)
    ):
        return was[allowed_at], was[present_at], mtime, size
    return store.client_allowed_ips(name, env), (mtime, size) != (0, 0), mtime, size


def refresh_counters(observed: Stamp | None = None) -> ClientIndexState:
    """Bring cum_rx and cum_tx level with traffic.db, and touch nothing else.

    The cheap half of staying current, and the half that runs constantly: the
    collector rewrites traffic.db every ten seconds and a client's usage is a
    column here because sorting and the quota filter read it.

    Reading that file is unavoidable and proportional to the server. Writing is
    not: in any ten second window a handful of clients moved a byte and the rest
    are exactly as they were, so only the rows that actually differ are written.
    On a mostly idle server that is nothing at all, and the whole refresh is one
    file read and one query that finds no work to do.
    """
    observed = observed if observed is not None else Stamp.observe()
    counters = _counters()
    if counters is None:
        # Nothing is written and the stamp is left where it was, so the next
        # request comes straight back here rather than waiting for the file to
        # move again. The figures on screen stay at the last reading that was
        # actually taken.
        return _state()

    with transaction.atomic():
        # Four columns rather than whole model instances: this runs every ten
        # seconds for the life of the panel, and building four thousand objects
        # to discover that none of them changed is the sort of cost that does not
        # show up in a profile of one request and does show up in a load average.
        moved = []
        for pk, key, rx, tx in ClientIndex.objects.values_list(
            "id", "meta__public_key", "cum_rx", "cum_tx"
        ):
            counter = counters.get(key)
            fresh_rx = counter.cum_rx if counter else 0
            fresh_tx = counter.cum_tx if counter else 0
            if (rx, tx) != (fresh_rx, fresh_tx):
                moved.append((fresh_rx, fresh_tx, pk))
        _write_counters(moved)

        state = _state()
        state.traffic_mtime_ns = observed.traffic_mtime_ns
        state.traffic_size = observed.traffic_size
        state.save(update_fields=["traffic_mtime_ns", "traffic_size", "built_at"])
        return state


def _write_counters(rows: list[tuple[int, int, int]]) -> None:
    """Write (cum_rx, cum_tx, id) triples onto ClientIndex, in one prepared statement.

    This was `bulk_update` and is deliberately not any more. Django renders that
    as a single UPDATE carrying a `CASE WHEN id=? THEN ? ... END` arm per row per
    column, and SQLite charges for every arm: two thousand rows cost about 420 ms
    against 3 ms for the same work through executemany.

    The 420 ms is worth spelling out, because the wasted CPU is the lesser half
    of it. Both callers run inside a transaction, so for that whole time this
    holds SQLite's write lock - and the panel's other writers all read before
    they write, which is the one shape no busy timeout can help. SQLite refuses
    that upgrade immediately rather than queueing it, so a client added while
    this statement ran did not wait 420 ms; it failed to fold its change into the
    index at all and left the next reader to rebuild from the files. On four
    thousand clients, with the collector flushing every ten seconds, that was
    roughly one add in twenty.

    Narrowing the statement does not remove the race - nothing here could - but
    it takes the window from 420 ms to 3 ms, which is the difference between a
    collision being routine and being a curiosity.

    The SQL is built from the model rather than written out, so renaming a field
    is a migration and an error here, not a query that silently updates nothing.
    """
    if not rows:
        return
    quote = connection.ops.quote_name
    table = quote(ClientIndex._meta.db_table)
    columns = [quote(ClientIndex._meta.get_field(name).column) for name in ("cum_rx", "cum_tx")]
    primary = quote(ClientIndex._meta.pk.column)
    with connection.cursor() as cursor:
        cursor.executemany(
            f"UPDATE {table} SET {columns[0]}=%s, {columns[1]}=%s WHERE {primary}=%s",
            rows,
        )


def _index_ids() -> dict[str, int]:
    """Every indexed peer's public key mapped to its ClientIndex row id.

    The whole column rather than the keys a caller is interested in, for the
    reason _metas gives: Django declares SQLite's parameter limit as 999, so an
    `__in` naming a peer per client is a query with thousands of placeholders,
    which older SQLite refuses outright. This table holds a row per peer, so
    there is nothing to narrow to anyway.
    """
    return dict(ClientIndex.objects.values_list("meta__public_key", "id"))


def absorb_counters(moved: dict[str, traffic.Counters], before: Stamp, after: Stamp) -> bool:
    """Take the usage the collector has just written, instead of reading it back.

    The counterpart of absorb() for the other file, and it exists for the same
    reason: the process that wrote traffic.db already knows which peers moved and
    what they now hold, and refresh_counters was working that out again from
    scratch every ten seconds for the life of the panel. That is a parse of the
    whole file and a pass over every index row, on a clock that never stops, to
    write the handful of rows a busy ten seconds actually changes. Told directly,
    the cost stops being about the size of the server and becomes about how much
    of it is transferring.

    `before` and `after` are stats of traffic.db taken either side of that write,
    under the lock that made it atomic. They are what makes this safe rather than
    merely fast:

    * `before` has to match what the index last absorbed. If it does not, this
      delta does not lead from what is stored to what is on disk - a PreDown
      hook landed in between, or the index has never been built - and
      applying it anyway would leave rows that are wrong under a stamp saying
      they are current, which is the one failure nothing here would ever correct.
      So nothing is written and nothing is stamped: the next read finds the file
      moved and does the full refresh, which is exactly what used to happen every
      time.
    * `after` is what gets stored, so the next read sees the file it stats and
      the file the index was built from as the same one and returns without
      opening anything. Leaving it out would be the whole point missed: the rows
      would already be right, and every reader would go and prove it again.

    Taking the stat after the write rather than before is deliberate and is the
    reverse of what rebuild() does. rebuild reads files nobody is holding still,
    so it stamps first and lets a concurrent write show up as a stamp that looks
    old; this runs inside the lock, where the file cannot move underneath it.

    Returns whether the update was applied, which the caller logs rather than
    acts on - the fallback for a refusal is the behaviour that was there before.
    """
    if not moved:
        return False
    with transaction.atomic():
        state = _state()
        if Stamp.stored(state).counters != before.counters:
            return False

        ids = _index_ids()
        _write_counters(
            [
                (counter.cum_rx, counter.cum_tx, ids[key])
                for key, counter in moved.items()
                if key in ids
            ]
        )

        state.traffic_mtime_ns = after.traffic_mtime_ns
        state.traffic_size = after.traffic_size
        state.save(update_fields=["traffic_mtime_ns", "traffic_size", "built_at"])
    return True


def _metas(keys: list[str]) -> dict[str, "MetaRow"]:
    """A metadata row per named peer, opening one for any peer that has none.

    ClientIndex hangs off ClientMeta so that a single query can filter on quota
    and expiry and sort by the stored handshake, which means every indexed peer
    needs a row. Opening one changes nothing an operator can see: every column
    has the default the client list already assumed for a peer with no row at
    all, and the collector has always opened them on the same terms for any peer
    it saw a handshake from.

    The whole table is read rather than the rows for these keys, and that is not
    laziness. Django declares SQLite's parameter limit as 999, so `public_key__in`
    with a peer per client is a query with four thousand placeholders in it -
    which the SQLite the panel ships against happens to allow and older ones
    reject outright. There is nothing to narrow to anyway: this table holds a row
    per peer, so the filter would exclude only what _prune is about to delete.

    Never deletes, which is _prune's job and is kept separate for the reason
    stated there: a quota removed in passing cannot be got back.
    """
    known = _meta_rows()
    missing = [ClientMeta(public_key=key) for key in keys if key not in known]
    if not missing:
        return known
    ClientMeta.objects.bulk_create(missing, batch_size=500, ignore_conflicts=True)
    # Re-read rather than trusting the objects just created: ignore_conflicts
    # leaves their primary keys unset on every backend, and the relation about to
    # be built out of them needs one.
    return _meta_rows()


def _meta_rows() -> dict[str, "MetaRow"]:
    """The three columns of ClientMeta a rebuild reads, by public key.

    Three values rather than whole model instances. A rebuild needs a metadata
    row's id to point at, its creation date to fall back to and the moment it was
    last touched to decide whether it may be pruned, and nothing else - and
    building four thousand full objects to read three fields off each is most of
    a tenth of a second that buys nothing.
    """
    return {
        key: MetaRow(pk, created_at, updated_at)
        for key, pk, created_at, updated_at in ClientMeta.objects.values_list(
            "public_key", "id", "created_at", "updated_at"
        )
    }


def _counters() -> dict[str, traffic.Counters] | None:
    """traffic.db, or None if it could not be read at all.

    None and an empty dict are different answers and the difference matters. A
    missing traffic.db is an empty one - read_db says so, and every client
    genuinely has no recorded usage. A file that exists and cannot be read is a
    permissions problem or a failing disk, and answering it with zeroes would
    write "this client has never sent a byte" over every usage figure on the
    server, which is a claim rather than an absence.

    So the callers keep what they last saw instead, and leave the stamp alone so
    the next request tries again rather than waiting for the file to change.
    """
    try:
        return traffic.read_db()
    except (AwgError, OSError) as exc:
        log.warning("cannot read %s for the client index: %s", traffic_db(), exc)
        return None


def _store_stamp(observed: Stamp, scan: store.ServerScan) -> ClientIndexState:
    state = _state()
    state.conf_mtime_ns = observed.conf_mtime_ns
    state.conf_size = observed.conf_size
    state.conf_inode = observed.conf_inode
    state.clients_mtime_ns = observed.clients_mtime_ns
    state.traffic_mtime_ns = observed.traffic_mtime_ns
    state.traffic_size = observed.traffic_size
    state.subnet_cidr = scan.subnet_cidr
    state.free_ips = scan.free_ips
    state.has_ipv6 = scan.has_ipv6
    state.save()
    return state


def invalidate() -> None:
    """Force the next read to rebuild, whatever the files say.

    For the one case the stamps cannot cover: a caller that knows it has changed
    something the three watched paths do not describe. Nothing in the panel needs
    it today - every write goes through the server config or a client file - and
    it is here because a restore that puts back a config with the same size and
    an older timestamp is exactly the sort of thing that turns up later.
    """
    ClientIndexState.objects.filter(id=ClientIndexState.ROW_ID).update(
        conf_mtime_ns=0, conf_size=0, conf_inode=0, clients_mtime_ns=0, traffic_mtime_ns=0
    )


def _iso(value: datetime | None) -> str | None:
    """A stored datetime as the RFC 3339 string the config comments use.

    The fallback behind `created_key`, for a peer whose config carries no
    "# Created" comment. Both formats sort as text in the order they happened,
    which is what lets one column hold either.
    """
    if value is None:
        return None
    # (datetime.UTC reads better but landed in 3.11; pyproject still supports
    # the 3.10 that Ubuntu 22.04 ships.)
    return value.astimezone(dt_timezone.utc).strftime(store.CREATED_FMT)  # noqa: UP017


def address_order(ip: str) -> int:
    """A tunnel address as a number, or -1 for one this cannot order.

    Here rather than in apps.clients.merge, which is where it used to live and
    which now imports it from here: the ordering is the database's to do, so the
    arithmetic that decides it belongs beside the column it is written into. Text
    order would put .10 before .2.
    """
    octets = ip.split("/")[0].split(".")
    if len(octets) != 4:
        return -1
    value = 0
    for octet in octets:
        if not octet.isdigit() or not 0 <= int(octet) <= 255:
            return -1
        value = value * 256 + int(octet)
    return value
