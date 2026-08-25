"""traffic.db accounting, the part of the panel that is easiest to get quietly wrong.

The kernel's counters restart at zero every time the interface does, so the
all-time totals only exist because something folds each reading into a saved
one. If that fold is wrong nothing fails: the dashboard just shows numbers that
are too small, or absurdly large, and quota enforcement acts on them. These
tests pin the exact arithmetic the counter-epoch rule depends on.

rx is what the server received, i.e. the client's upload. The naming here stays
server-side, as it is on disk.
"""

from pathlib import Path

from awg import traffic
from awg.traffic import Counters

PEER_A = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="
PEER_B = "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4="


def _db_path(conf_dir: Path) -> Path:
    return conf_dir / "traffic.db"


def test_missing_file_is_an_empty_database(conf_dir: Path) -> None:
    """A server that has never had a peer connect is not an error condition."""
    assert traffic.read_db(_db_path(conf_dir)) == {}


def test_round_trip_through_disk(conf_dir: Path) -> None:
    path = _db_path(conf_dir)
    data = {PEER_A: Counters(100, 200, 10, 20), PEER_B: Counters(0, 0, 0, 0)}
    traffic.write_db(data, path)
    assert path.read_text(encoding="utf-8") == (f"{PEER_A} 100 200 10 20\n{PEER_B} 0 0 0 0\n")
    assert traffic.read_db(path) == data
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_new_peer_starts_from_zero() -> None:
    """The first reading is all new traffic: the peer had no row to subtract from."""
    db: dict[str, Counters] = {}
    deltas = traffic.accumulate(db, {PEER_A: (500, 900)})
    assert deltas == {PEER_A: (500, 900)}
    assert db[PEER_A] == Counters(cum_rx=500, cum_tx=900, last_rx=500, last_tx=900)


def test_deltas_are_the_difference_between_readings() -> None:
    db = {PEER_A: Counters(1000, 2000, 1000, 2000)}
    deltas = traffic.accumulate(db, {PEER_A: (1500, 2200)})
    assert deltas == {PEER_A: (500, 200)}
    assert db[PEER_A] == Counters(cum_rx=1500, cum_tx=2200, last_rx=1500, last_tx=2200)


def test_a_counter_reset_adds_the_whole_new_reading() -> None:
    """After `awg-quick down && up` the kernel counts from zero again.

    Subtracting the old raw value there would produce a negative delta, so the
    rule is: below the last reading means a new epoch, and the current value is
    entirely new traffic.
    """
    db = {PEER_A: Counters(cum_rx=10_000, cum_tx=20_000, last_rx=9_000, last_tx=19_000)}
    deltas = traffic.accumulate(db, {PEER_A: (40, 60)})
    assert deltas == {PEER_A: (40, 60)}
    assert db[PEER_A] == Counters(cum_rx=10_040, cum_tx=20_060, last_rx=40, last_tx=60)


def test_one_direction_may_reset_without_the_other() -> None:
    """rx and tx are folded independently; a shared branch would lose one of them."""
    db = {PEER_A: Counters(cum_rx=100, cum_tx=100, last_rx=100, last_tx=100)}
    assert traffic.accumulate(db, {PEER_A: (150, 5)}) == {PEER_A: (50, 5)}
    assert db[PEER_A] == Counters(cum_rx=150, cum_tx=105, last_rx=150, last_tx=5)


def test_a_peer_missing_from_the_dump_keeps_its_row(conf_dir: Path) -> None:
    """A client that has not handshaked since boot is absent from the dump.

    Its history is not gone, and zeroing it would look to the quota check like a
    client that had suddenly used nothing.
    """
    db = {
        PEER_A: Counters(1000, 2000, 1000, 2000),
        PEER_B: Counters(7, 8, 7, 8),
    }
    deltas = traffic.accumulate(db, {PEER_A: (1001, 2001)})
    assert set(deltas) == {PEER_A}
    assert db[PEER_B] == Counters(7, 8, 7, 8)


def test_an_idle_peer_produces_a_zero_delta() -> None:
    db = {PEER_A: Counters(500, 600, 500, 600)}
    assert traffic.accumulate(db, {PEER_A: (500, 600)}) == {PEER_A: (0, 0)}
    assert db[PEER_A] == Counters(500, 600, 500, 600)


def test_malformed_lines_are_skipped_not_fatal(conf_dir: Path) -> None:
    """The file is also written by a shell script, and older ones were sloppier.

    Losing one peer's history beats a panel that refuses to start.
    """
    path = _db_path(conf_dir)
    path.write_text(
        "\n"
        "short row\n"
        f"{PEER_A} 1 2 3\n"  # four columns
        f"{PEER_B} 1 2 3 4 5\n"  # six columns
        "notakey -1 2 3 4\n"
        "otherkey 1 2.5 3 4\n"
        "hexkey 0x10 2 3 4\n"
        f"{PEER_A} 10 20 30 40\n",
        encoding="utf-8",
    )
    assert traffic.read_db(path) == {PEER_A: Counters(10, 20, 30, 40)}


def test_a_repeated_key_takes_the_last_row(conf_dir: Path) -> None:
    """Bash's associative array would end up with the same value."""
    path = _db_path(conf_dir)
    path.write_text(f"{PEER_A} 1 1 1 1\n{PEER_A} 9 9 9 9\n", encoding="utf-8")
    assert traffic.read_db(path) == {PEER_A: Counters(9, 9, 9, 9)}


def test_sync_reads_folds_and_writes(conf_dir: Path) -> None:
    """The whole cycle on disk, under the lock the bash tools also take."""
    path = _db_path(conf_dir)
    traffic.write_db({PEER_A: Counters(100, 100, 100, 100)}, path)

    assert traffic.sync({PEER_A: (250, 300), PEER_B: (7, 9)}, path) == {
        PEER_A: (150, 200),
        PEER_B: (7, 9),
    }
    assert traffic.read_db(path) == {
        PEER_A: Counters(250, 300, 250, 300),
        PEER_B: Counters(7, 9, 7, 9),
    }

    # A restart between two syncs: totals keep climbing from where they were.
    assert traffic.sync({PEER_A: (5, 5)}, path) == {PEER_A: (5, 5)}
    assert traffic.read_db(path)[PEER_A] == Counters(255, 305, 5, 5)


def test_sync_keeps_existing_rows_in_place(conf_dir: Path) -> None:
    """Row order is not load-bearing, but churning it would make every diff useless."""
    path = _db_path(conf_dir)
    traffic.write_db({PEER_A: Counters(1, 1, 1, 1), PEER_B: Counters(2, 2, 2, 2)}, path)
    traffic.sync({PEER_B: (3, 3)}, path)
    assert [line.split()[0] for line in path.read_text(encoding="utf-8").splitlines()] == [
        PEER_A,
        PEER_B,
    ]


# ------------------------------------------------------------------- epochs
#
# The counters alone cannot say whether the interface restarted. A reading below
# the stored one is proof it did, but the opposite is not proof it did not: a
# peer that moves more after a restart than it had moved before it produces a
# reading above the stored one, and diffing there loses that peer's entire
# previous epoch. traffic.epoch is what settles the question for those.


def _epoch_path(conf_dir: Path) -> Path:
    return conf_dir / "traffic.epoch"


def test_a_restart_is_counted_in_full_even_when_the_counter_climbs(
    conf_dir: Path, monkeypatch
) -> None:
    """The undercount this file exists to stop, at the arithmetic level."""
    path = _db_path(conf_dir)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    traffic.sync({PEER_A: (1000, 2000)}, path)
    assert _epoch_path(conf_dir).read_text(encoding="utf-8").strip() == "boot-1 7"

    # Same boot, new interface: a higher ifindex and counters from zero. The
    # reading is above the stored one, so nothing about it says "restart".
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 8")
    assert traffic.sync({PEER_A: (1500, 2500)}, path) == {PEER_A: (1500, 2500)}
    assert traffic.read_db(path)[PEER_A] == Counters(2500, 4500, 1500, 2500)


def test_an_unchanged_epoch_still_diffs(conf_dir: Path, monkeypatch) -> None:
    """The interface has not moved, so the stored counters are still the peer's."""
    path = _db_path(conf_dir)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    traffic.sync({PEER_A: (1000, 2000)}, path)
    assert traffic.sync({PEER_A: (1200, 2200)}, path) == {PEER_A: (200, 200)}


def test_an_unknown_epoch_is_not_read_as_a_restart(conf_dir: Path, monkeypatch) -> None:
    """The first fold after an upgrade has no stored epoch to compare against.

    Treating that as a restart would add every peer's current counter to a total
    that already contains it, doubling the lot on a single fold.
    """
    path = _db_path(conf_dir)
    traffic.write_db({PEER_A: Counters(1000, 2000, 1000, 2000)}, path)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    assert traffic.sync({PEER_A: (1100, 2100)}, path) == {PEER_A: (100, 100)}


def test_an_unreadable_epoch_leaves_the_stored_one_alone(conf_dir: Path, monkeypatch) -> None:
    """A kernel publishing nothing must not overwrite what a kernel that did wrote."""
    path = _db_path(conf_dir)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    traffic.sync({PEER_A: (1000, 2000)}, path)

    monkeypatch.setattr(traffic, "current_epoch", lambda: "")
    traffic.sync({PEER_A: (1100, 2100)}, path)
    assert _epoch_path(conf_dir).read_text(encoding="utf-8").strip() == "boot-1 7"


def test_ending_zeroes_the_raw_counters_only_without_an_epoch(conf_dir: Path, monkeypatch) -> None:
    """The PreDown hook's claim is a fallback, and acting on it otherwise is unsafe.

    An admin running the hook's command by hand on a live interface makes the
    same claim the teardown does. Where an epoch can be read it is ignored, so
    the next fold still diffs; where none can, it is the only warning there is.
    """
    path = _db_path(conf_dir)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    traffic.sync({PEER_A: (1000, 2000)}, path, ending=True)
    assert traffic.read_db(path)[PEER_A] == Counters(1000, 2000, 1000, 2000)

    monkeypatch.setattr(traffic, "current_epoch", lambda: "")
    traffic.sync({PEER_A: (1200, 2200)}, path, ending=True)
    assert traffic.read_db(path)[PEER_A] == Counters(1200, 2200, 0, 0)


def test_ending_leaves_the_epoch_for_the_next_fold_to_find(conf_dir: Path, monkeypatch) -> None:
    """A teardown keeps the epoch on purpose: it is what the new interface differs from."""
    path = _db_path(conf_dir)
    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 7")
    traffic.sync({PEER_A: (1000, 2000)}, path, ending=True)
    assert traffic.read_epoch(path) == "boot-1 7"

    monkeypatch.setattr(traffic, "current_epoch", lambda: "boot-1 8")
    assert traffic.sync({PEER_A: (1500, 2500)}, path) == {PEER_A: (1500, 2500)}
