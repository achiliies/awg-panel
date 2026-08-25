"""The mutex guarding /etc/amnezia/amneziawg, and the machinery behind it.

The panel is the only writer of `<conf_dir>`, but it is not a single writer: two
gunicorn workers, several threads each, and a collector process all mutate these
files, so they still have to queue rather than interleave. This is the flock on
`<conf_dir>/.lock` that makes them.

It is the same lock file the bash tools used to take, kept rather than renamed
because `awg-quick` hooks and anything else left on a box from before still
expect it there, and because a lock nobody else takes costs nothing to keep
compatible.

`file_lock` is the same thing over any path, for work that has to be serialised
between panel processes without a CLI command ever queueing behind it.
"""

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import LockTimeout
from .paths import ensure_dir, lock_file

# flock is per open file description, so two threads that each open .lock
# contend correctly. What flock cannot do is tell us we already hold it, and
# the store calls locked helpers from locked methods; hence a depth counter.
# It is thread-local because the fd it guards belongs to one thread's stack, and
# keyed by path so that holding one lock says nothing about holding another.
_state = threading.local()

_POLL_START = 0.01
_POLL_MAX = 0.2

# How long a waiter gives up after, and a number this side now owns. It used to
# be dictated from outside: `flock -w 10` in the shell tools was what a CLI
# command waited, so any panel operation holding this lock for longer than ten
# seconds was one that made somebody's command fail. That is why re-issuing
# every client config had to fit inside ten seconds on a server of any size,
# and it never could.
#
# Nothing in bash writes these files any more, so the ceiling is gone and the
# only waiters are the panel's own workers. Thirty seconds is chosen for them:
# long enough that a request arriving during a settings save on a large server
# waits for it and then succeeds, rather than failing while the work it needed
# was already half done, and short enough to still be a stuck-holder detector
# rather than a hang.
#
# Named rather than left in a default so that anything waiting *inside* this
# lock can be written against it - see apps.clients.index.
CONFIG_LOCK_SEC = 30.0


@contextmanager
def config_lock(timeout: float = CONFIG_LOCK_SEC) -> Iterator[None]:
    """Hold the config lock for the duration of the block.

    Re-entrant within a thread: nested uses just bump a counter and the file
    lock is released once, when the outermost block exits.

    Raises LockTimeout if another process still holds it after `timeout`.
    """
    with file_lock(lock_file(), timeout):
        yield


@contextmanager
def file_lock(path: Path, timeout: float = CONFIG_LOCK_SEC) -> Iterator[None]:
    """Hold an exclusive flock on `path` for the duration of the block.

    Re-entrant within a thread, per path. Raises LockTimeout if another process
    still holds it after `timeout`.
    """
    held = getattr(_state, "held", None)
    if held is None:
        held = _state.held = {}
    key = str(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    fd = _open_lock(path)
    try:
        _acquire(fd, path, timeout)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _open_lock(path: Path) -> int:
    ensure_dir(path.parent)
    existed = path.exists()
    # O_CLOEXEC: while the lock is held we run `awg`, `awg-quick` and
    # `systemctl`; a child that inherited this descriptor would keep the lock
    # alive for as long as it lived.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    if not existed:
        os.fchmod(fd, 0o600)
    return fd


def _acquire(fd: int, path: Path, timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 0.0)
    delay = _POLL_START
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockTimeout(
                    f"timed out after {timeout:g}s waiting for the lock at {path}. "
                    "Another panel operation is still running; try again in a moment."
                ) from None
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _POLL_MAX)
