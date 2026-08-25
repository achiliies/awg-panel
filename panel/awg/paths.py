"""Where the panel reads and writes.

Every location is derived from the environment on each call rather than frozen
at import time: the test suite points AWG_CONF_DIR and AWG_PANEL_DATA at a
tmpdir with monkeypatch, and the collector process is started by systemd with
the same env file the web unit uses.
"""

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ENV_IFACE = "AWG_IFACE"
ENV_CONF_DIR = "AWG_CONF_DIR"
ENV_DATA_DIR = "AWG_PANEL_DATA"
ENV_RUN_DIR = "AWG_PANEL_RUN"
ENV_PANEL_ENV = "AWG_PANEL_ENV"

DEFAULT_IFACE = "awg0"
DEFAULT_CONF_DIR = "/etc/amnezia/amneziawg"
DEFAULT_DATA_DIR = "/var/lib/awg-panel"
DEFAULT_RUN_DIR = "/run/awg-panel"
DEFAULT_PANEL_ENV = "/etc/awg-panel.env"

# install.sh creates the config tree with `install -d -m 700`; anything the
# panel creates must be just as unreadable.
DIR_MODE = 0o700


def iface() -> str:
    """Interface name, e.g. "awg0"."""
    return os.environ.get(ENV_IFACE) or DEFAULT_IFACE


def conf_dir() -> Path:
    """Directory owned by the bash tools; the panel is a second writer, not the owner."""
    return Path(os.environ.get(ENV_CONF_DIR) or DEFAULT_CONF_DIR)


def server_conf() -> Path:
    return conf_dir() / f"{iface()}.conf"


def client_dir() -> Path:
    return conf_dir() / "clients"


def env_file() -> Path:
    return conf_dir() / "clients.env"


def traffic_db() -> Path:
    return conf_dir() / "traffic.db"


def lock_file() -> Path:
    """flock target for every writer of the config directory.

    The path the shell tools used to take too, kept rather than renamed: hooks
    and scripts left on a box from before still expect it here, and a lock file
    nobody else takes costs nothing to keep compatible.
    """
    return conf_dir() / ".lock"


def index_lock_file() -> Path:
    """flock target for rebuilding the client index. Panel-only, and deliberately so.

    Not the config lock. Rebuilding reads the config and writes nothing back to
    it, so making a browser poll queue behind it - or worse, making a client add
    queue behind a browser poll - would be paying the cost of a mutex for
    something that mutates no file anything else reads.

    In the run dir because it is worth nothing after a reboot: a lock file left
    behind by a killed process is not held by anything, and a tmpfs clears it in
    any case.
    """
    return run_dir() / "index.lock"


def data_dir() -> Path:
    """Panel-only state: SQLite DB, secret key, live stats."""
    return Path(os.environ.get(ENV_DATA_DIR) or DEFAULT_DATA_DIR)


def run_dir() -> Path:
    """Volatile state, on a tmpfs: rewritten often, worth nothing after a reboot."""
    return Path(os.environ.get(ENV_RUN_DIR) or DEFAULT_RUN_DIR)


def master_key_file() -> Path:
    return data_dir() / "master.key"


def live_state_file() -> Path:
    """Collector output served by /api/v1/stats/live.

    In the run dir, because this is the one file the panel rewrites on a timer
    for as long as it is installed - every trafficPollSec, whole, with the two
    fsyncs atomic_write owes every other caller. On a big server the blob is
    most of a megabyte, and on the data dir that came to tens of gigabytes of
    writes a day against an SD card or a cheap VPS SSD, for a file whose entire
    value expires in two seconds. On a tmpfs it costs nothing and the fsyncs
    are memory barriers.

    Nothing is lost by the move. /run survives a restart of either unit and is
    cleared only by a reboot, and the one field here that outlives a cycle -
    the interface epoch - is already refused by the collector's own restore
    guards when it predates the current boot. So tmpfs keeps exactly the cases
    that restore accepts, and drops exactly the ones it would have thrown away.
    """
    return run_dir() / "live.json"


def panel_env_file() -> Path:
    """Env file both systemd units read, rewritten when web settings change."""
    return Path(os.environ.get(ENV_PANEL_ENV) or DEFAULT_PANEL_ENV)


def ensure_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create path (and parents) if missing. Existing permissions are left alone."""
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    return path


def atomic_write(path: Path | str, text: str, mode: int = 0o600, sync_dir: bool = True) -> None:
    """Write text to path so a concurrent reader sees either the old or the new file.

    The temp file is created in the target directory - a rename across
    filesystems is not atomic - and is named so that it never matches the
    `clients/*.conf` glob anything else globs for. `awg-quick` reads the server
    config at boot without taking any lock, so a half-written one is not a
    theoretical concern.

    `sync_dir=False` leaves the parent directory unsynced, for a caller writing
    many files into one directory that will sync it once at the end. It does not
    weaken what any single file gets: the contents are still fsynced before the
    rename, so no file can appear holding the wrong bytes. What is deferred is
    only the durability of the rename itself, and only until the batch finishes.
    See write_batch, which is the one thing that should be passing it.
    """

    def write(handle) -> None:
        handle.write(text.encode("utf-8"))

    _atomic_publish(Path(path), write, mode, sync_dir=sync_dir)


@contextlib.contextmanager
def write_batch(directory: Path | str) -> Iterator[None]:
    """Write many files into one directory, syncing the directory once at the end.

    _atomic_publish fsyncs twice per file: once for the contents, once for the
    directory so the rename itself survives a power cut. The second is the same
    fsync on the same directory every time, and re-issuing it per file is what
    made re-issuing every client config cost 26 seconds on a four thousand
    client server - 83% of it spent in fsync, with the config lock held
    throughout, which is long enough that nothing else could write at all.

    So a batch takes it once, after the last rename. The failure this gives up
    is narrow and worth naming: lose power in the middle of a batch and some
    renames may not have landed, leaving those clients holding their previous
    config. That was already the outcome for every client the batch had not
    reached yet, and the previous config is a working one - it is what they are
    running now. Nothing can be left half written, because the contents are
    still synced before each rename.
    """
    try:
        yield
    finally:
        _sync_dir(Path(directory))


def atomic_copy(source: Path | str, path: Path | str, mode: int = 0o600) -> None:
    """Install source's bytes at path, with the same all-or-nothing guarantee.

    Bytes rather than text because this also carries traffic.db and the panel's
    SQLite file, which have to arrive byte-identical. The source's own mode is
    never inherited: these files come out of an archive somebody uploaded.
    """

    def write(handle) -> None:
        with open(source, "rb") as incoming:
            shutil.copyfileobj(incoming, handle)

    _atomic_publish(Path(path), write, mode)


def _atomic_publish(
    target: Path, write: Callable[[Any], None], mode: int, sync_dir: bool = True
) -> None:
    parent = ensure_dir(target.parent)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if sync_dir:
        _sync_dir(parent)


def _sync_dir(parent: Path) -> None:
    """Make the renames in this directory durable.

    Durability of a rename needs the directory synced, not just the file. Not
    every filesystem permits it, and a failure here does not undo a good write,
    so it is suppressed rather than raised.
    """
    with contextlib.suppress(OSError):
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
