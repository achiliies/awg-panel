"""Read and write panel settings, and push the five that systemd needs into its env file.

The table is tiny and read on nearly every request (the session middleware asks
for sessionMaxAge, the session endpoint for the theme and the language), so
values are cached in the process. The cache is dropped on write and also
expires after a few seconds:
gunicorn runs more than one worker, and the worker that did not handle the PUT
would otherwise serve the old value until it was restarted.

Nothing here raises when the database is unavailable. A panel that cannot read
its own settings must still serve the page that lets you fix them, and this
module is imported by middleware that runs before the first migration.
"""

import errno
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from django.db import DatabaseError, transaction

from awg.errors import ValidationError
from awg.paths import atomic_write, panel_env_file
from awgui.settings import normalise_base_path

from . import defaults
from .models import Setting

log = logging.getLogger(__name__)

# Long enough to absorb a burst of dashboard polls, short enough that a change
# made in one worker reaches the others before anybody notices.
CACHE_TTL = 5.0

ENV_PANEL_BIN = "AWG_PANEL_BIN"
DEFAULT_PANEL_BIN = "/usr/local/bin/awg-panel"
RESTART_TIMEOUT = 10

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# KEY=... at the start of a line, the only form install-panel.sh writes.
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

_guard = threading.Lock()
_cache: dict[str, str] | None = None
_cache_until = 0.0


# --------------------------------------------------------------------- read


def all_settings() -> dict[str, str]:
    """Every known setting: the stored value, else the running one, else the default."""
    values = dict(defaults.DEFAULTS)
    values.update(_from_environment())
    values.update(_stored())
    return values


def get(key: str, default: str | None = None) -> str:
    """One setting as text: the stored value, else the running one, else its default."""
    stored = _stored()
    if key in stored:
        return stored[key]
    live = _from_environment()
    if key in live:
        return live[key]
    if key in defaults.DEFAULTS:
        return defaults.DEFAULTS[key]
    return "" if default is None else str(default)


def _from_environment() -> dict[str, str]:
    """The env-backed settings as the service was actually started with.

    For a setting nobody has edited, the truth is not the code's default: it is
    whatever install-panel.sh wrote and systemd handed the process. The base
    path makes this load-bearing. The installer generates a random one, writes
    it to /etc/awg-panel.env and stores no row for it, so reading it back as the
    default "/" is not merely a wrong reading - the env file is rendered from
    these values, and one save would overwrite the generated path with "/" and
    move the panel to the root of the server, off the secret path and onto
    every scanner's list, without anyone having asked for it.
    """
    live: dict[str, str] = {}
    for key, name in defaults.ENV_KEYS.items():
        value = os.environ.get(name)
        # Empty is not a value here: it is what the installer writes for "no TLS
        # certificate", and the default for those is empty anyway.
        if value:
            live[key] = value
    return live


def get_int(key: str, default: int = 0) -> int:
    """One setting as a number. A cleared or unparsable value falls back to `default`."""
    try:
        return int(get(key, str(default)).strip())
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    """One setting as a flag. Anything unrecognised, empty included, falls back to `default`."""
    raw = get(key, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


# -------------------------------------------------------------------- write


# Shadows the builtin, deliberately: get/set is the vocabulary every caller of a
# settings store expects. Nothing in this module needs set() the type.
def set(key: str, value: object) -> None:
    """Store one setting. Values are text; anything else is stringified."""
    set_many({key: value})


def set_many(changes: Mapping[str, object]) -> None:
    """Store several settings in one transaction, then drop the cache."""
    if not changes:
        return
    rows: dict[str, str] = {}
    for key, value in changes.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise ValidationError({str(key): "A setting name must be 1 to 64 characters."})
        rows[key] = "" if value is None else str(value)

    with transaction.atomic():
        for key, value in rows.items():
            Setting.objects.update_or_create(key=key, defaults={"value": value})
    invalidate()


def invalidate() -> None:
    """Forget the cached values; the next read goes to the database."""
    global _cache, _cache_until
    with _guard:
        _cache = None
        _cache_until = 0.0


def _stored() -> dict[str, str]:
    global _cache, _cache_until
    with _guard:
        if _cache is not None and time.monotonic() < _cache_until:
            return _cache

    try:
        rows = dict(Setting.objects.values_list("key", "value"))
    except DatabaseError as exc:
        # No table yet (first boot, before migrate), or the file was swapped out
        # from under us by a restore. Defaults still make a usable panel.
        log.debug("settings unavailable, using defaults: %s", exc)
        return {}

    with _guard:
        _cache = rows
        _cache_until = time.monotonic() + CACHE_TTL
    return rows


# ------------------------------------------------- applying the web settings


def panel_env_values(values: Mapping[str, str]) -> dict[str, str]:
    """The /etc/awg-panel.env variables implied by the current settings."""
    out = {env: str(values.get(key, "")) for key, env in defaults.ENV_KEYS.items()}
    # The env file is read by systemd, by awg-panel and by awgui.settings, and
    # all three have to agree on the trailing slash.
    out["AWG_PANEL_BASE_PATH"] = normalise_base_path(values.get("webBasePath"))
    # Derived rather than stored: TLS is on exactly when both paths are set, and
    # a separate switch could disagree with them.
    out["AWG_PANEL_TLS"] = "1" if values.get("tlsCertPath") and values.get("tlsKeyPath") else "0"
    return out


def read_panel_env() -> dict[str, str] | None:
    """Every variable /etc/awg-panel.env assigns, or None when it cannot be read.

    This file, not os.environ, is what the service is configured by: it is what
    systemd renders the next start from, so it stays the answer to "where is the
    panel" across the restart a settings change or a restore schedules.

    None rather than {} for an unreadable file, because the two are different
    answers. A dev checkout has no env file and no service to be wrong about; a
    file that exists and assigns nothing would be a genuine finding.
    """
    try:
        text = panel_env_file().read_text(encoding="utf-8")
    except OSError:
        return None

    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_ASSIGN.match(line)
        if match:
            # The last assignment wins, which is what bash does when it sources it.
            found[match.group(1)] = line.split("=", 1)[1]
    return found


def panel_env_is_current(values: Mapping[str, str]) -> bool:
    """Does the env file already say what these settings imply?

    The database and the file can drift apart - an admin edits the file to get
    back in, a save's write fails, a backup restores a database written on
    another machine - and the UI has no way to say so, because re-entering the
    same values changes no row, and a save that changes nothing writes nothing
    and restarts nothing. The admin is then left pressing Save on a form that
    already shows the right answer, watching it do nothing.

    So the question is asked of the file rather than of the previous row. An
    unreadable file answers "current": there is nothing to compare, and a dev
    checkout with no env file at all must not restart anything.
    """
    have = read_panel_env()
    if have is None:
        return True
    return all(have.get(name) == value for name, value in panel_env_values(values).items())


def write_panel_env(values: Mapping[str, str]) -> bool:
    """Rewrite the variables we own in /etc/awg-panel.env, leaving every other line alone.

    Values are written unquoted, exactly as install-panel.sh writes them: its
    env_get is a bare `sed -n "s/^KEY=//p"` and would hand the quotes straight
    back into the next env_put. Everything written here has already been through
    defaults.clean(), which rejects whitespace and shell metacharacters.

    Returns False when the file cannot be written - a dev checkout, a container,
    an install that never created it - so the caller can say so instead of
    pretending the change took.
    """
    target = panel_env_file()
    wanted = panel_env_values(values)

    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as exc:
        log.warning("cannot read %s: %s", target, exc)
        return False

    lines = original.splitlines()
    seen: list[str] = []
    for index, line in enumerate(lines):
        match = _ENV_ASSIGN.match(line)
        name = match.group(1) if match else ""
        if name in wanted:
            # Every occurrence, not just the first: bash keeps the last one, so
            # a duplicate left behind would win.
            lines[index] = f"{name}={wanted[name]}"
            seen.append(name)
    for name, value in wanted.items():
        if name not in seen:
            lines.append(f"{name}={value}")

    text = "\n".join(lines) + "\n"
    try:
        atomic_write(target, text, mode=0o600)
        return True
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            log.warning("cannot write %s: %s", target, exc)
            return False
        log.info("%s: %s - rewriting the file in place instead", target, exc)
    return _write_in_place(target, text)


def _write_in_place(target: Path, text: str) -> bool:
    """Rewrite an existing file through its own inode, without a temporary beside it.

    The unit runs with ProtectSystem=full, so /etc is read-only, and it is
    granted /etc/awg-panel.env itself rather than the directory it sits in. An
    atomic replace needs to create a temp file next to the target and then
    rename over it, and both of those are operations on the directory: the whole
    write fails with EROFS while the file it is trying to write is perfectly
    writable. That failure is silent enough to be dangerous - the settings land
    in the database, the service is never restarted into them, and the panel
    goes on serving the old address while the UI says the change was saved.

    The trade is atomicity. This file is a few hundred bytes, written in one
    call, and read once at service start rather than by a concurrent reader, so
    a torn write needs a crash inside a single small write() to happen at all.
    A panel that cannot apply its own settings is the larger risk.
    """
    try:
        with open(target, "r+b") as handle:  # r+ so a failure to open changes nothing
            handle.write(text.encode("utf-8"))
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        log.warning("cannot write %s: %s", target, exc)
        return False
    return True


def request_web_restart() -> bool:
    """Ask awg-panel to restart the web service a couple of seconds from now.

    Deferred on purpose: this runs inside the request that changed the setting,
    and restarting synchronously would drop the connection before the response
    was written, so the browser would show a network error instead of "saved".

    Returns False when the wrapper is not installed, which is the normal case in
    a dev checkout or a container with no systemd.
    """
    binary = os.environ.get(ENV_PANEL_BIN) or shutil.which("awg-panel") or DEFAULT_PANEL_BIN
    if not os.access(binary, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [binary, "restart-deferred"],
            capture_output=True,
            text=True,
            timeout=RESTART_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not schedule a panel restart: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("awg-panel restart-deferred failed: %s", (result.stderr or "").strip())
        return False
    return True
