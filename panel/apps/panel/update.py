"""What the panel knows about updating itself.

Two halves, and the split matters. Deciding whether there is a newer release is
pure reading and lives in :mod:`awg.release`, shared with bin/awg-update so that
"newer" means one thing on this server. Performing the update is not something a
web request can do at all - it restarts the service handling the request, and
half of it happens after the browser has been answered - so this module does not
try. It starts bin/awg-update and then only ever *reads* the state file that
tool writes.

That indirection is the design rather than an accident of it. The updater runs
in its own transient systemd unit, so it is not a child of gunicorn and is not
killed when the install restarts gunicorn; the panel's own view of the update is
a file on disk that survives the restart, so the page an operator is watching
picks the story back up where it left off once the panel answers again.
"""

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from awg.errors import AwgError, Conflict, ValidationError
from awg.paths import data_dir

log = logging.getLogger(__name__)

# The engine. install.sh puts it here; AWG_UPDATE_TOOL repoints it, which is
# what the test suite does.
ENV_TOOL = "AWG_UPDATE_TOOL"
DEFAULT_TOOL = "/usr/local/bin/awg-update"

# How long to wait for `awg-update start` to hand the work to systemd and come
# back. It does nothing slow - one systemd-run and a state file - so a start
# that has not returned in this long is one that is not going to.
START_TIMEOUT = 30

# How long a state file may say "starting" with no process recorded against it
# before that is read as a launch that never happened.
STARTING_GRACE = 60

# How much of the log the status endpoint carries. The page shows a live tail
# while the server reinstalls itself, and the whole log of a module rebuild is
# several hundred kilobytes of gcc that nobody reads on a phone.
LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 64 * 1024

STATUSES = ("idle", "running", "succeeded", "failed")

# Every key the state file can carry, with what an absent one means. Fixed here
# rather than passed through, so a state file written by an older or newer
# bin/awg-update can never reach the serializer with a field it does not declare
# or without one it does.
_BLANK = {
    "status": "idle",
    "phase": "",
    "detail": "",
    "from_version": "",
    "to_version": "",
    "started": "",
    "finished": "",
    "backup": "",
}


def tool_path() -> str:
    return os.environ.get(ENV_TOOL) or DEFAULT_TOOL


def state_dir() -> Path:
    return data_dir() / "update"


def state_file() -> Path:
    return state_dir() / "state.json"


def log_file() -> Path:
    return state_dir() / "log"


def available() -> str:
    """Why an update cannot be started from here, or "" when it can.

    Checked before offering the button rather than only when it is pressed: a
    panel installed some other way, or one whose updater has been removed, is
    better off saying so on the page than accepting a click and failing.
    """
    tool = tool_path()
    if not os.path.isfile(tool):
        return (
            f"The updater is not installed at {tool}, so this panel cannot update itself. "
            "Re-run the installer once from a release bundle and it will be."
        )
    if not os.access(tool, os.X_OK):
        return f"{tool} is not executable, so this panel cannot update itself."
    return ""


def read_state() -> dict:
    """The updater's state file, with every field present and nothing extra.

    A missing, unreadable or half-written file is "idle" rather than an error.
    The file is replaced atomically by the tool that writes it, so the only way
    to read a broken one is for something else to have damaged it, and that is
    not a reason for this endpoint to fail.

    A "running" update whose worker is gone is reported as failed. Not written
    back - the state file belongs to bin/awg-update and this module only reads
    it, and the same tool settles it on its own next run - but the page must not
    be left with a progress bar spinning for the rest of the server's life
    because a process was killed without reaching its own exit trap.
    """
    state = dict(_BLANK)
    try:
        with state_file().open() as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    for key in state:
        value = raw.get(key)
        if value is not None:
            state[key] = str(value)
    if state["status"] not in STATUSES:
        state["status"] = "idle"
    if state["status"] == "running" and not _worker_alive(raw, state):
        state["status"] = "failed"
        state["detail"] = "The update stopped before it finished. The log below is where it got to."
    return state


def _worker_alive(raw: dict, state: dict) -> bool:
    """Is the process that wrote this state file still running?

    Read out of /proc rather than by asking systemd, because this is polled every
    two seconds while an update runs and two stat calls are the whole cost. The
    pid belongs to the transient unit's own process, which is not a child of this
    one, so waitpid-style answers are not available either way.

    A pid on its own is not enough, and this is the half that used to be wrong. A
    worker killed without reaching its exit trap leaves the file saying "running"
    with a pid in it, and after the reboot that usually follows, that number
    belongs to something else - so the page showed a progress bar that never
    stopped, on an update that had ended days ago. The updater records the boot
    it ran under and the worker's own start time beside the pid; both are checked
    here, and both are absent on a file written by an older version, which is
    read the old way rather than as a failure.
    """
    boot = str(raw.get("boot") or "").strip()
    if boot and boot != _boot_id():
        return False
    pid = str(raw.get("pid") or "").strip()
    if pid.isdigit():
        if not Path("/proc", pid).exists():
            return False
        want = str(raw.get("pid_start") or "").strip()
        return not want or want == _proc_started(pid)
    # Started, and not yet far enough along to have recorded itself. A fraction
    # of a second in practice, and it must not read as a dead update - the first
    # poll after the button is pressed lands inside it. Bounded by the clock all
    # the same, so a launch that silently started nothing is still noticed.
    if state["phase"] != "starting":
        return False
    return _seconds_since(state["started"]) < STARTING_GRACE


def _boot_id() -> str:
    """This boot's id, or "" where the kernel does not publish one."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _proc_started(pid: str) -> str:
    """Field 22 of /proc/<pid>/stat: when this process started, in clock ticks.

    Everything up to the ") " closing the command name is dropped first, because
    that name is the one field that can contain spaces and brackets of its own
    and would otherwise shift every column after it.
    """
    try:
        raw = Path("/proc", pid, "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    _, _, rest = raw.rpartition(") ")
    fields = rest.split()
    return fields[19] if len(fields) > 19 else ""


def _seconds_since(stamp: str) -> float:
    """Age of one of the updater's UTC timestamps, or infinity if unreadable."""
    try:
        started = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(UTC) - started).total_seconds()


def read_log(lines: int = LOG_TAIL_LINES) -> str:
    """The tail of the current update's log, or "" when there has not been one.

    Read from the end rather than by loading the file, because by the middle of
    a module build it is large and this is polled every couple of seconds.
    """
    path = log_file()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
                # The seek almost certainly landed inside a line; drop it rather
                # than show half of one.
                handle.readline()
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(tail.splitlines()[-lines:])


def status() -> dict:
    """State plus log tail plus why this server cannot update, in one answer.

    One endpoint rather than three because the page needs all of it on every
    poll, and because they have to agree with each other: a status read a second
    apart from its own log would show a finished update above the log of it
    still running.
    """
    answer = read_state()
    answer["log"] = read_log()
    answer["unavailable"] = available()
    return answer


def start(*, force: bool = False) -> dict:
    """Hand the work to bin/awg-update and answer with the state it left behind.

    Returns as soon as the updater has been handed to systemd, which is the
    point: everything after that outlives this request, and the request has to
    finish so the browser has something to poll with.
    """
    reason = available()
    if reason:
        # A 400 rather than a 500: nothing is broken, this server simply has no
        # updater on it, and the sentence says how to get one.
        raise ValidationError(reason)

    current = read_state()
    if current["status"] == "running":
        # The 409 the rest of the panel uses for "your request collides with
        # what is already happening". The page turns the button off while an
        # update runs, so reaching this means two browsers, or a stale one.
        raise Conflict(
            "An update is already running on this server. Watch it finish rather than "
            "starting a second one."
        )

    command = [tool_path(), "start"]
    if force:
        command.append("--force")
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            timeout=START_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AwgError(
            "The updater did not start within 30 seconds. Nothing has been changed; "
            "try it over SSH with: sudo awg-update apply"
        ) from exc
    except OSError as exc:
        raise AwgError(f"The updater could not be run: {exc}") from exc

    if result.returncode != 0:
        # The tool's own sentence, which says which precondition failed - no
        # release helper, an update already running, a lock it could not take -
        # rather than a generic failure this module would have to guess at.
        detail = (result.stderr or result.stdout or "").strip()
        raise AwgError(detail or "The updater refused to start and said nothing about why.")

    return status()
