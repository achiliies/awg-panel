"""The service log, read out of journald on demand and never stored.

This is ``sudo awg-panel logs`` without the ssh. What it shows is the same three
units an operator would name by hand - the web service, the collector and the
tunnel - and it exists because the moment somebody needs it is the moment the
panel is the only thing they can still reach.

Nothing is copied into the database. journald already holds these lines, already
rotates them and already survives a restart, and a second copy would be a second
retention policy to get wrong for no gain. The cost of that choice is that this
endpoint shells out, which is why it is the one read in the panel that is neither
polled nor cheap: it is asked for when a person opens the tab, and the number of
lines is capped so that "asked for" cannot mean a hundred megabytes.

Which units may be read is a fixed table, not a parameter. The source names in
the API are ``panel``, ``collector`` and ``tunnel``; the systemd unit each of
them stands for is worked out here. That is what keeps a query string from
becoming a way to read any unit on the machine - and the tunnel's unit is
per-interface anyway, so it could not be a constant on the browser's side even
if it were safe to let one through.
"""

import json
import logging
import subprocess
from datetime import datetime
from datetime import timezone as dt_timezone

from awg import paths

log = logging.getLogger(__name__)

# The sources the API offers, in the order the UI lists them.
SOURCES = ("panel", "collector", "tunnel")

# The two panel units are constants; the tunnel's is named after whichever
# interface this install manages, so it is resolved per call.
_UNITS = {
    "panel": "awg-panel-web",
    "collector": "awg-panel-collector",
}

DEFAULT_LINES = 200
MAX_LINES = 1000

# journalctl reads an index and answers in milliseconds. Anything past this is a
# journal on a disk that is not answering, and the operator is better served by
# a message saying so than by a request that hangs until the browser gives up.
TIMEOUT_SEC = 10

MISSING = (
    "journalctl is not available on this server, so the service log cannot be read here. "
    "The panel's own event log above does not depend on it."
)


def unit_for(source: str) -> str:
    """The systemd unit a source name stands for, or "" if it is not one of ours."""
    if source == "tunnel":
        return f"awg-quick@{paths.iface()}"
    return _UNITS.get(source, "")


def read(source: str, lines: int = DEFAULT_LINES) -> dict:
    """The last few lines this unit wrote, newest last.

    Newest last rather than first, which is the opposite of the event log beside
    it and is deliberate: this is a transcript, read in the order the service
    wrote it, and reversing it would break every multi-line traceback in the
    journal into its lines in the wrong order.

    A journal that cannot be read is an answer rather than an error. There is no
    systemd in a container, none in the development tree, and none on a host
    where the operator has swapped it out - all three arrive here as
    ``available: false`` and a sentence, because a 500 on this tab would read as
    the panel being broken at exactly the moment somebody came here to find out
    what was.
    """
    unit = unit_for(source)
    if not unit:
        return _empty(source, "", "That is not a service this panel can read.")

    wanted = max(1, min(int(lines), MAX_LINES))
    command = [
        "journalctl",
        "--unit",
        unit,
        "--lines",
        str(wanted),
        "--output",
        "json",
        "--no-pager",
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT_SEC, check=False
        )
    except FileNotFoundError:
        return _empty(source, unit, MISSING)
    except subprocess.TimeoutExpired:
        return _empty(source, unit, f"journalctl did not answer within {TIMEOUT_SEC} seconds.")
    except OSError as exc:
        log.warning("cannot run journalctl: %s", exc)
        return _empty(source, unit, f"journalctl could not be run: {exc}")

    if proc.returncode != 0:
        reason = (proc.stderr or "").strip().splitlines()
        return _empty(source, unit, reason[0] if reason else "journalctl refused the request.")

    return {
        "source": source,
        "unit": unit,
        "lines": _parse(proc.stdout),
        "available": True,
        "reason": "",
    }


def _empty(source: str, unit: str, reason: str) -> dict:
    return {"source": source, "unit": unit, "lines": [], "available": False, "reason": reason}


def _parse(stdout: str) -> list[dict]:
    """One entry per line of journalctl's JSON output, in the order it wrote them.

    A line that will not parse is skipped rather than failing the batch. The
    journal can hold an entry whose fields this does not expect - written by
    something other than these services under the same unit, or by a systemd
    older or newer than the one this was written against - and losing one line of
    a transcript is a great deal better than losing the transcript.
    """
    out: list[dict] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = _message(entry.get("MESSAGE"))
        if not message:
            continue
        out.append(
            {
                "at": _moment(entry.get("__REALTIME_TIMESTAMP")),
                "priority": _priority(entry.get("PRIORITY")),
                "message": message,
            }
        )
    return out


def _message(value: object) -> str:
    """The line itself, however journald chose to encode it.

    Usually a string. A message that is not valid UTF-8 arrives as a list of byte
    values instead, which is journald being honest rather than journald being
    broken - and a service writing one is exactly the kind of thing somebody
    opens this tab to see, so it is decoded here rather than dropped.
    """
    if isinstance(value, str):
        return value.rstrip("\n")
    if isinstance(value, list):
        try:
            return bytes(int(part) for part in value).decode("utf-8", "replace").rstrip("\n")
        except (TypeError, ValueError):
            return ""
    return ""


def _moment(value: object) -> datetime | None:
    """journald's microseconds-since-the-epoch as an aware UTC moment.

    None when it cannot be read, which the UI draws as a line with no time
    against it: the message is the part worth showing, and inventing "now" for
    an entry from last week would be worse than admitting to not knowing.

    The conversion is inside the guard as well as the parse, because a number
    that reads perfectly well can still be no date at all. A corrupt timestamp
    is an ordinary integer to `int()` and out of range to `datetime`, which says
    so with a ValueError below a few hundred thousand years, an OverflowError
    past what the platform's time_t holds, and an OSError instead of either on
    some platforms; a value large enough puts the division itself over what a
    float can carry. Every one of those used to leave this function, pass
    through _parse - which drops a line it cannot read rather than failing the
    batch - and come out of the endpoint as a 500, so a single unreadable entry
    took away the whole log at exactly the moment somebody had opened it to find
    out what was wrong.
    """
    try:
        return datetime.fromtimestamp(int(str(value)) / 1_000_000, tz=dt_timezone.utc)  # noqa: UP017
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _priority(value: object) -> int:
    """The syslog level, 0 (emergency) to 7 (debug).

    Anything unreadable is 6, "informational", which is what an entry with no
    priority at all means to journald - and is the level the UI draws without
    colouring, so a malformed field cannot dress an ordinary line up as an error.
    """
    try:
        level = int(str(value))
    except (TypeError, ValueError):
        return 6
    return level if 0 <= level <= 7 else 6
