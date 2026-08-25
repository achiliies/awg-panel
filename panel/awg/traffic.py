"""traffic.db - all-time transfer counters, and the only place they survive.

The kernel's per-peer counters restart from zero whenever the interface does, so
`awg show <iface> dump` alone cannot answer "how much has this client used".
This is a small text database beside the server config holding, per public key,
the cumulative totals plus the last raw counters seen, so only real deltas are
ever added. install.sh wires `PreDown = awg-panel manage trafficsync` into the
config to catch the final delta before an orderly restart.

Knowing when the counters restarted is the whole difficulty, and there are two
answers here. An orderly teardown says so itself: the PreDown fold ends the
epoch, zeroing the stored raw values, so the first reading of the new interface
is counted in full. Everything else - a reboot, a crash, `ip link del` - is
caught by traffic.epoch beside this file, which holds the boot id and the
interface's ifindex; either changes when the interface is recreated, and a fold
that sees a different pair treats every stored raw value as zero.

A raw value below the stored one is still read as a restart, but it is now the
last line of defence rather than the mechanism. On its own it never caught the
case that costs the most: a peer that moves more bytes after the restart than
it had moved before it, whose new counter is above the old one and whose delta
therefore silently loses everything the peer had transferred in the old epoch.

This module is the same algorithm in Python, over the same file, under the same
lock, because both front-ends run on the same box and either may be the one
polling. The format is fixed by the bash side:

    <public_key> <cum_rx> <cum_tx> <last_raw_rx> <last_raw_tx>

space separated, no header, 0600. From the server's point of view rx is the
client's upload and tx its download; the labels are swapped in the UI, never
here.

Lines that do not parse are dropped rather than raising: the file is written by
two programs, one of them a shell script whose older versions could leave a row
with missing columns, and losing one peer's history is a far better outcome for
the admin than a panel that will not start.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .lock import config_lock
from .paths import atomic_write, iface, traffic_db

# Counters are unsigned and printf'd with %s, so anything with a sign, a decimal
# point or a 0x prefix did not come from a writer we recognise.
_UINT_RE = re.compile(r"^[0-9]+$")

# Changes on every boot, and the only half of the epoch that survives one: an
# ifindex is small and is handed out again from the start after a reboot, so a
# fresh interface can be given the number the old one had.
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


@dataclass
class Counters:
    """One peer's row: cumulative totals, and the raw values behind them."""

    cum_rx: int = 0
    cum_tx: int = 0
    last_rx: int = 0
    last_tx: int = 0


def read_db(path: Path | str | None = None) -> dict[str, Counters]:
    """Load traffic.db. A missing file is an empty database, not an error.

    Not taken under config_lock(): writers replace the file atomically, so a
    reader sees either the whole old file or the whole new one. Callers that
    read in order to write must hold the lock across both.
    """
    db: dict[str, Counters] = {}
    try:
        raw = _target(path).read_bytes()
    except FileNotFoundError:
        return db

    for line in raw.splitlines():
        try:
            fields = line.decode("utf-8").split()
        except UnicodeDecodeError:
            continue
        if len(fields) != 5:
            continue
        pub, numbers = fields[0], fields[1:]
        if not all(_UINT_RE.match(value) for value in numbers):
            continue
        cum_rx, cum_tx, last_rx, last_tx = (int(value) for value in numbers)
        # A repeated key is what bash would end up with: the last row wins.
        db[pub] = Counters(cum_rx, cum_tx, last_rx, last_tx)
    return db


def write_db(data: dict[str, Counters], path: Path | str | None = None) -> None:
    """Replace traffic.db with `data`, atomically and 0600.

    Rows are written in the order of the mapping, which for a read-modify-write
    cycle keeps existing peers where they were and appends new ones; the bash
    version iterates an associative array and has no order at all, so nothing
    depends on it. Like read_db, this does not take the lock itself.
    """
    lines = [
        f"{pub} {row.cum_rx} {row.cum_tx} {row.last_rx} {row.last_tx}\n"
        for pub, row in data.items()
    ]
    atomic_write(_target(path), "".join(lines), mode=0o600)


def accumulate(
    db: dict[str, Counters], dump: dict[str, tuple[int, int]], *, restarted: bool = False
) -> dict[str, tuple[int, int]]:
    """Fold one `awg show dump` reading into `db`, mutating it in place.

    `dump` maps public key to the raw (rx, tx) counters. Returns
    {pubkey: (delta_rx, delta_tx)} for the peers in this dump only - the
    collector reports those as live rates and sums them into today's total.

    Peers in the db but not in the dump keep their row untouched: they are
    simply peers the running interface has not seen since it came up (or peers
    that no longer exist, whose rows are dropped explicitly on removal).

    `restarted` says the counters in `dump` belong to a different run of the
    interface than the ones on file, so every stored raw value is worth zero and
    the whole of each reading is new. sync_db works it out from traffic.epoch; a
    caller folding a dump it obtained some other way can say so directly.

    Deliberately lock-free: the collector calls this between a locked read and a
    locked write, and store operations compose it inside a larger locked block.
    """
    deltas: dict[str, tuple[int, int]] = {}
    for pub, (raw_rx, raw_tx) in dump.items():
        row = db.get(pub)
        if row is None:
            row = Counters()
            db[pub] = row
        last_rx = 0 if restarted else row.last_rx
        last_tx = 0 if restarted else row.last_tx
        # Below the last seen value means the interface restarted without anything
        # having recorded that it did. Kept for the reading that arrives before
        # the epoch can be read at all, and for a counter that moves backwards
        # for some reason this module has no way to know about.
        delta_rx = raw_rx - last_rx if raw_rx >= last_rx else raw_rx
        delta_tx = raw_tx - last_tx if raw_tx >= last_tx else raw_tx
        row.cum_rx += delta_rx
        row.cum_tx += delta_tx
        row.last_rx = raw_rx
        row.last_tx = raw_tx
        deltas[pub] = (delta_rx, delta_tx)
    return deltas


def current_epoch() -> str:
    """A token for this run of the interface, or "" when it cannot be read.

    The boot id and the interface's ifindex. Either one alone is not enough: an
    ifindex is reused after a reboot, and a boot id does not change when the
    interface is torn down and brought back on a machine that stays up. Together
    they change on both, which is the whole question this answers.

    An unreadable pair is not an error and not a restart. It is a kernel that
    does not publish one of these files, or a test running nowhere near a real
    interface, and the honest answer there is that this mechanism has nothing to
    say - "" is never equal to a stored epoch and never overwrites one, so the
    fold falls back on the counters alone exactly as it did before.
    """
    boot = _read_first_line(_BOOT_ID)
    index = _read_first_line(Path(f"/sys/class/net/{iface()}/ifindex"))
    return f"{boot} {index}" if boot and index else ""


def read_epoch(path: Path | str | None = None) -> str:
    """The epoch traffic.db's raw counters were last read in, or "" if unknown.

    Unknown is what a server upgrading into this sees once, and it is read as
    "cannot tell", not as a restart: the counters on file are the best evidence
    there is, and discarding them on the strength of a missing file would double
    every peer's total on the first fold after the upgrade.
    """
    return _read_first_line(_epoch_target(_target(path)))


def write_epoch(value: str, path: Path | str | None = None) -> None:
    """Record the epoch the raw counters in traffic.db belong to."""
    atomic_write(_epoch_target(_target(path)), f"{value}\n", mode=0o600)


def sync(
    dump: dict[str, tuple[int, int]],
    path: Path | str | None = None,
    *,
    ending: bool = False,
) -> dict[str, tuple[int, int]]:
    """Fold a dump into traffic.db on disk and return this pass's deltas.

    The whole read-modify-write runs under config_lock(), so the collector and a
    PreDown hook firing mid-restart cannot interleave and lose a peer's totals.

    `ending` is for the PreDown fold: see sync_db.
    """
    return sync_db(dump, path, ending=ending)[1]


def sync_db(
    dump: dict[str, tuple[int, int]],
    path: Path | str | None = None,
    *,
    ending: bool = False,
) -> tuple[dict[str, Counters], dict[str, tuple[int, int]]]:
    """The same fold, handing back the file's new contents as well as the deltas.

    For a caller that has to tell somebody else what the file now says. Reading
    it back afterwards would answer the same question, and on a server with four
    thousand peers it would answer it by parsing four thousand lines that this
    call already has in memory - and would answer a slightly different question
    besides, because the lock is released in between.

    The totals are what came out of the file, not what the caller believed was
    in it: a PreDown hook may have added to a peer since the caller last looked,
    and its own mirror would put that back.

    `ending` marks this as the last fold of an interface that is going away, and
    is the PreDown hook's alone. It is a fallback and nothing more: where the
    epoch is readable it changes nothing, because the epoch is evidence and this
    is only a claim - and a claim that is false whenever an admin runs the hook's
    command by hand on a running interface. Zeroing the raw counters on the
    strength of it would then make the collector's next reading look like a whole
    epoch of new traffic and double every peer's total.

    Where the epoch cannot be read at all, though, it is the only signal there
    is, and it is a good one: the interface really is about to be destroyed and
    the counters this just recorded really are the last of them. So the raw
    values are zeroed there and the next bring-up is counted in full.

    The stored epoch is deliberately left alone by a teardown. It describes an
    interface that is going away, and that is exactly what makes it useful: the
    next fold compares the new interface against it, finds it different, and
    knows.
    """
    target = _target(path)
    with config_lock():
        db = read_db(target)
        stored = read_epoch(target)
        epoch = current_epoch()
        # Both halves have to be known for a difference to mean anything. An
        # epoch we cannot read and an epoch never recorded are the same silence,
        # and neither is evidence that the interface restarted.
        restarted = bool(epoch) and bool(stored) and epoch != stored
        deltas = accumulate(db, dump, restarted=restarted)
        if ending and not epoch:
            for row in db.values():
                row.last_rx = 0
                row.last_tx = 0
        write_db(db, target)
        if epoch and epoch != stored:
            write_epoch(epoch, target)
    return db, deltas


def _target(path: Path | str | None) -> Path:
    return Path(path) if path is not None else traffic_db()


def _epoch_target(target: Path) -> Path:
    """traffic.epoch beside traffic.db, whichever traffic.db is in play.

    Derived from the database path rather than named in paths.py, so a caller
    passing a path of its own - the tests, and anything restoring into a staging
    directory - gets a matching pair without having to say so twice.
    """
    return target.with_suffix(".epoch")


def _read_first_line(path: Path) -> str:
    """The file's first line stripped, or "" if it cannot be read at all.

    Every file this reads is one the panel does not own: two under /proc and
    /sys that a kernel need not publish, and one of its own that a restore or a
    disk may have taken away. None of them is worth an exception here - the
    caller's fallback is correct in every case.
    """
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, UnicodeDecodeError, IndexError):
        return ""
