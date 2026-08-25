"""Talk to the AmneziaWG tools, or admit honestly that we cannot.

The panel has to render on machines where the tools are not there: a laptop
running with AWG_MOCK=1, a CI runner with no kernel module, a server between
`install.sh` and the first `awg-quick up`. Every read-only method here therefore
answers "no" or "unknown" instead of raising, so the status page still loads and
can say what is wrong. Only the mutating calls (syncconf, set_peer, remove_peer,
up, down, restart, service_start, service_stop) raise ToolError: a caller that
asked for a change has to be told it did not happen.

The dump parser mirrors `awg show <iface> dump`, which the bash tools consume
with the same field order: one interface line, then one line per peer, tab
separated, with "(none)" standing in for an unset value.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import paths
from .errors import ToolError

ENV_MOCK = "AWG_MOCK"

MODULE = "amneziawg"
# Named once so the two questions asked of it - is the module loaded, and which
# version got loaded - cannot drift onto different paths, and so a test can
# point them somewhere it is allowed to write.
SYSFS_MODULE = Path("/sys/module")
TIMEOUT = 30
# The three mutators below run with the config lock held, so a wedged `awg` is
# a lock nobody else can take. That used to have to fit inside the shell tools'
# `flock -w 10`; nothing in bash writes these files now, so the budget answers
# only to the panel's own waiters, which give up after lock.CONFIG_LOCK_SEC.
# Still comfortably inside it: the point of this number is to notice a tool that
# has stopped responding, and a syncconf that has not finished in ten seconds
# has. up/down keep the full TIMEOUT: they run the iptables hooks, they are
# deliberately not under the lock, and cutting them short leaves a
# half-configured interface.
SYNC_TIMEOUT = 10
# ...plus a second per megabyte of config, because the subnet is no longer
# capped at 253 clients and a /16 that is actually full is a 10 MB syncconf.
# A wedge detector, not a capacity limit: on a server that large one sync
# legitimately takes longer than the flat budget, and timing it out would fail
# an operation that was working.
SYNC_TIMEOUT_PER_MB = 1
FEATURES_TTL = 60.0
# How much of the journal to read back when a systemd job fails. Enough to hold
# awg-quick's echo of every command it ran plus the one that complained, and
# little enough that a unit which floods on the way down cannot fill an API
# response.
JOURNAL_LINES = 40

# ActiveState values that mean systemd is holding the unit up, and so would have
# something to do if it were told to stop it. "failed" is deliberately not among
# them: a unit that fell over is running ExecStop for nobody, and whatever is
# left of the interface has to be dealt with directly.
UNIT_RUNNING = frozenset({"active", "activating", "reloading", "deactivating"})

# What `awg` prints for a value that is not set.
NONE = "(none)"

# Capability name -> tokens that prove it in `awg set` usage output. The tool's
# argument names are the config keys lowercased, so a hit is hard evidence.
# A miss is not: only the imitation packets are named the same way in every
# release we care about, so the others are probed for a yes and never for a no
# (see _detect_features).
_PROBE_TOKENS: dict[str, tuple[str, ...]] = {
    "imitation_packets": ("i1", "i2", "i3", "i4", "i5"),
    "header_protection_key": ("hpk", "header-protection-key", "headerprotectionkey"),
    "content_padding": ("cpa", "content-padding", "contentpaddingaddition"),
    "timers": ("rekey-after-time", "rekeyaftertime", "reject-after-time", "rejectaftertime"),
}

# Imitation packets and header ranges landed together upstream; anything at or
# above this reports both.
_FEATURE_VERSION = (1, 5)

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))+")


@dataclass
class PeerDump:
    """One peer line of `awg show <iface> dump`."""

    public_key: str
    # Key material. Present for parity with the tool; never serialise it.
    preshared_key: str = field(default="", repr=False)
    endpoint: str = ""
    allowed_ips: str = ""
    latest_handshake: int = 0
    rx: int = 0  # server-side receive: the client's upload
    tx: int = 0  # server-side transmit: the client's download
    keepalive: str = ""  # "off" or a number of seconds, as the tool prints it


@dataclass
class Dump:
    """The whole of `awg show <iface> dump`: interface line plus peers."""

    # The server private key. It is in the tool's output, so it is in ours, but
    # repr is suppressed so a stray log line cannot leak it.
    private_key: str = field(default="", repr=False)
    public_key: str = ""
    listen_port: int = 0
    fwmark: str = ""
    peers: list[PeerDump] = field(default_factory=list)

    def transfers(self) -> dict[str, tuple[int, int]]:
        """{public_key: (rx, tx)}, the shape traffic.accumulate() consumes."""
        return {peer.public_key: (peer.rx, peer.tx) for peer in self.peers if peer.public_key}


class BaseController(Protocol):
    """What the panel needs from the tunnel, real or mocked."""

    def available(self) -> bool: ...
    def tools_version(self) -> str | None: ...
    def module_version(self) -> str | None: ...
    def module_version_on_disk(self) -> str | None: ...
    def module_loaded(self) -> bool: ...
    def iface_up(self) -> bool: ...
    def show_dump(self) -> Dump | None: ...
    def syncconf(self, stripped_text: str) -> None: ...
    def set_peer(
        self, pub: str, *, allowed_ips: str | None = None, psk: str | None = None
    ) -> None: ...
    def remove_peer(self, pub: str) -> None: ...
    def up(self) -> None: ...
    def down(self) -> None: ...
    def restart(self) -> None: ...
    def service_start(self) -> None: ...
    def service_stop(self) -> None: ...
    def features(self) -> dict: ...
    def service_state(self) -> dict: ...


def parse_dump(text: str) -> Dump:
    """Parse `awg show <iface> dump` output.

    Short or malformed lines are padded rather than rejected: a truncated dump
    from a tool we do not know is still worth the peers it did print.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return Dump()
    head = _fields(lines[0], 4)
    dump = Dump(
        private_key=_clean(head[0]),
        public_key=_clean(head[1]),
        listen_port=_int(head[2]),
        # Last, not fourth: AmneziaWG prints every obfuscation value between the
        # listen port and the fwmark, and 3.1 added two more of them. Counting
        # from the end is what keeps this parser out of that argument - plain
        # WireGuard's four-field line has fwmark in the same place either way.
        fwmark=_clean(head[-1]),
    )
    for line in lines[1:]:
        row = _fields(line, 8)
        public_key = _clean(row[0])
        if not public_key:
            continue
        dump.peers.append(
            PeerDump(
                public_key=public_key,
                preshared_key=_clean(row[1]),
                endpoint=_clean(row[2]),
                allowed_ips=_clean(row[3]),
                latest_handshake=_int(row[4]),
                rx=_int(row[5]),
                tx=_int(row[6]),
                keepalive=_clean(row[7]),
            )
        )
    return dump


class AwgController:
    """BaseController over the real `awg`, `awg-quick`, `modinfo` and `systemctl`."""

    def __init__(self, iface: str | None = None) -> None:
        self._iface = iface
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    @property
    def iface(self) -> str:
        """Resolved at call time: the test suite moves AWG_IFACE around."""
        return self._iface or paths.iface()

    @property
    def unit(self) -> str:
        """The systemd unit that owns this interface at boot."""
        return f"awg-quick@{self.iface}"

    # ------------------------------------------------------------- discovery

    def available(self) -> bool:
        """True when `awg` is on PATH."""
        return shutil.which("awg") is not None

    def tools_version(self) -> str | None:
        """First line of `awg --version`, e.g. "amneziawg-tools v3.1.20260812 - https://amnezia.org"."""

        def probe() -> str | None:
            out = _try(["awg", "--version"])
            if not out:
                return None
            return out.strip().splitlines()[0].strip() or None

        return self._cached("tools_version", probe)

    def module_version(self) -> str | None:
        """Version of the module the kernel is *running*, or None if there is none.

        Read from sysfs rather than from modinfo, because those are different
        questions and the answers diverge exactly when it matters. modinfo reads
        the .ko file on disk; sysfs reports what was loaded from it. An upgrade
        that installed a new module but could not unload the old one - which
        install.sh reports and defers to the next reboot - leaves the file new
        and the kernel old, and every behaviour anyone is looking at is the old
        one's.

        Falls back to modinfo when nothing is loaded, so a stopped tunnel still
        reports the version it would come up on.
        """

        def probe() -> str | None:
            return _sysfs_module_version() or self._module_version_on_disk()

        return self._cached("module_version", probe)

    def module_version_on_disk(self) -> str | None:
        """Version of the installed .ko, which is what the next boot will load."""
        return self._cached("module_version_on_disk", self._module_version_on_disk)

    @staticmethod
    def _module_version_on_disk() -> str | None:
        out = _try(["modinfo", "-F", "version", MODULE])
        if out and out.strip():
            return out.strip().splitlines()[0].strip()
        # -F is a kmod extension; older modinfo needs the full output parsed
        # the way lib/common.sh's kmod_installed parses it.
        out = _try(["modinfo", MODULE])
        for line in (out or "").splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip() or None
        return None

    def module_loaded(self) -> bool:
        """True when the kernel has the module. Read from /sys and /proc, no subprocess."""
        if SYSFS_MODULE.joinpath(MODULE).is_dir():
            return True
        try:
            with open("/proc/modules", encoding="utf-8", errors="replace") as handle:
                return any(line.split(" ", 1)[0] == MODULE for line in handle)
        except OSError:
            return False

    def iface_up(self) -> bool:
        """True when the interface exists, the same test `ip link show` makes in bash."""
        return Path("/sys/class/net").joinpath(self.iface).exists()

    # ------------------------------------------------------------------ read

    def show_dump(self) -> Dump | None:
        """Live interface and peer state, or None when there is nothing to read."""
        if not self.available() or not self.iface_up():
            return None
        out = _try(["awg", "show", self.iface, "dump"])
        if out is None:
            return None
        return parse_dump(out)

    # --------------------------------------------------------------- mutate

    def syncconf(self, stripped_text: str) -> None:
        """Apply a stripped config to the running interface without dropping sessions.

        `awg syncconf` wants a path, and bash hands it one via process
        substitution; here it is a real temp file, created 0600 because the text
        carries the server private key, and removed whatever happens next.
        """
        tmp = _secret_file(stripped_text)
        try:
            _run(["awg", "syncconf", self.iface, tmp], timeout=_sync_timeout(stripped_text))
        finally:
            _unlink(tmp)

    def set_peer(self, pub: str, *, allowed_ips: str | None = None, psk: str | None = None) -> None:
        """Add or update one peer on the running interface."""
        cmd = ["awg", "set", self.iface, "peer", pub]
        if allowed_ips is not None:
            cmd += ["allowed-ips", allowed_ips]
        tmp = None
        if psk is not None:
            # The tool only accepts a preshared key from a file, which also keeps
            # it off a command line that ps can read.
            tmp = _secret_file(psk + "\n")
            cmd += ["preshared-key", tmp]
        try:
            _run(cmd, timeout=SYNC_TIMEOUT)
        finally:
            if tmp:
                _unlink(tmp)

    def remove_peer(self, pub: str) -> None:
        """Drop one peer from the running interface."""
        _run(["awg", "set", self.iface, "peer", pub, "remove"], timeout=SYNC_TIMEOUT)

    def up(self) -> None:
        _run(["awg-quick", "up", self.iface])

    def down(self) -> None:
        _run(["awg-quick", "down", self.iface])

    def restart(self) -> None:
        """Full down/up. Needed for ListenPort, Address and MTU; drops every session."""
        # Down is allowed to fail: the interface may already be gone, and the
        # point of the call is the up that follows. Same order as awg-menu.
        try:
            self.down()
        except ToolError:
            pass
        self.up()

    def service_start(self) -> None:
        """Bring the tunnel up and leave it up.

        Through the unit where there is one, because that is what makes a later
        stop work: systemd only runs ExecStop for a unit it started. Where the
        unit is missing, or where starting it left no interface behind,
        `awg-quick` finishes the job - what was asked for is a tunnel that is
        up, not a job that exited zero.

        Idempotent: an interface that is already there is the state the caller
        asked for, and a second `systemctl start` against it would run
        `awg-quick up` on a name the kernel already has and fail.
        """
        if self.iface_up():
            return
        error = None
        if self._unit_loaded():
            # A unit sitting in `failed` - parked there by an ExecStop that ran
            # against an interface somebody had already taken away - will not
            # start again until that is cleared.
            self._reset_failed()
            error = self._job("start")
            if self.iface_up():
                return
            if error is not None:
                raise error
        # Either there is no unit, or systemd believed it was already active and
        # so did nothing: RemainAfterExit means a start against that belief
        # exits zero without running anything, and the interface is still gone.
        self.up()

    def service_stop(self) -> None:
        """Take the tunnel down and leave it down, until somebody starts it or the box reboots.

        Whether systemd is holding the interface up decides which tool takes it
        down, and asking is not a formality. install.sh enables the unit and
        then brings the tunnel up with `awg-quick`, and every restart since has
        gone the same way, so on a perfectly ordinary server the unit is
        `enabled` and `inactive` over a tunnel that is running. `systemctl stop`
        against that exits zero, runs no ExecStop, and leaves every client
        connected - a stop that reports success and does nothing, which is the
        one failure an operator has no way to see.

        So: the unit first when systemd thinks it is up, because that is the
        path that keeps its view true and runs ExecStop under systemd's own
        timeout, then `awg-quick` if the interface is still there. Either way
        the hooks run - PreDown folds the kernel's per-peer counters into
        traffic.db and PostDown takes the upload shaper off the WAN interface -
        which is the whole reason neither path reaches for `ip link del`.

        Idempotent, and judged on the interface rather than on an exit code: a
        `systemctl stop` that failed after the tunnel was already gone got the
        operator what they asked for.
        """
        error = None
        if self._unit_active():
            error = self._job("stop")
        if self.iface_up():
            # systemd was either not the one holding it up, or could not finish.
            # Its own words first when it has some, since they name the step
            # that broke; awg-quick's otherwise.
            if error is not None:
                raise error
            self.down()
        self._reset_failed()

    # ------------------------------------------------------------- features

    def features(self) -> dict:
        """What this installation supports.

        Values are booleans, plus the two version strings and "unknown": the
        list of capability names whose boolean is an assumption rather than an
        observation. Unknown always assumes yes - a false negative would make
        validate.py reject a parameter that works, which is worse than passing
        one the kernel ignores - and the UI says so out loud.
        """
        cached = dict(self._cached("features", self._detect_features, ttl=FEATURES_TTL) or {})
        cached["unknown"] = list(cached.get("unknown") or ())  # callers must not edit the cache
        return cached

    def service_state(self) -> dict:
        """awg-quick@<iface> as systemd sees it."""
        return {
            "unit": self.unit,
            "active": _systemctl(["is-active", self.unit]),
            "enabled": _systemctl(["is-enabled", self.unit]),
        }

    # ------------------------------------------------------------- internals

    def _job(self, verb: str) -> ToolError | None:
        """Run a systemd job and hand the failure back rather than raising it.

        The caller decides what a non-zero exit means, because on its own it is
        not the answer: what was asked for is an interface in a particular
        state, and a job can fail having got there anyway - or exit zero without
        going near it.
        """
        cmd = ["systemctl", verb, self.unit]
        since = time.time() - 1
        proc = _run(cmd, check=False)
        if proc.returncode == 0:
            return None
        return ToolError(cmd, _unit_output(self.unit, since) or proc.stderr, proc.returncode)

    def _reset_failed(self) -> None:
        """Clear a unit parked in `failed`, so the next start is not refused.

        Where it gets parked: ExecStop is `awg-quick down`, and run against an
        interface that is already gone - taken away by hand, or by this
        controller a moment earlier - it exits non-zero and takes the unit into
        `failed` with it. Nothing else clears that, and `systemctl start` on a
        unit whose start limit it has counted against will not.
        """
        if _systemctl(["show", "-p", "ActiveState", "--value", self.unit]) == "failed":
            _run(["systemctl", "reset-failed", self.unit], check=False)

    def _unit_active(self) -> bool:
        """Whether systemd believes it is the one holding the interface up.

        Which is a different question from whether the interface exists, and the
        two disagree in both directions: `RemainAfterExit=yes` keeps the unit
        `active` over a tunnel `awg-quick down` removed behind its back, and an
        `awg-quick up` that never went through systemd leaves it `inactive` over
        a tunnel that is running. Only this answer decides whether `systemctl
        stop` would do anything at all.
        """
        return _systemctl(["show", "-p", "ActiveState", "--value", self.unit]) in UNIT_RUNNING

    def _unit_loaded(self) -> bool:
        """Whether systemd has the unit file at all.

        Every install this project performs enables `awg-quick@<iface>`, but the
        panel also runs against an AmneziaWG that was set up by hand, where it
        may not exist. LoadState answers precisely; is-active and is-enabled
        both blur "no such unit" into wording that changes between releases.
        """
        return _systemctl(["show", "-p", "LoadState", "--value", self.unit]) == "loaded"

    def _detect_features(self) -> dict:
        tools = self.tools_version()
        module = self.module_version()
        usage = _usage_text()
        version = _max_version(tools, module)
        unknown: list[str] = []
        caps: dict[str, bool] = {}

        if usage is None:
            # Nothing to look at. Everything is an assumption.
            caps = dict.fromkeys(_PROBE_TOKENS, True)
            caps["header_ranges"] = True
            unknown = [*caps]
        else:
            has_imitation = _mentions(usage, _PROBE_TOKENS["imitation_packets"])
            for name, tokens in _PROBE_TOKENS.items():
                if name == "imitation_packets":
                    caps[name] = has_imitation
                elif not has_imitation:
                    # Tools this old predate all of it; a no here is real.
                    caps[name] = False
                else:
                    caps[name] = True
                    if not _mentions(usage, tokens):
                        unknown.append(name)
            caps["header_ranges"] = has_imitation
            if has_imitation and (version is None or version < _FEATURE_VERSION):
                unknown.append("header_ranges")

        return {
            "tools_version": tools,
            "module_version": module,
            "module_version_on_disk": self.module_version_on_disk(),
            "module_loaded": self.module_loaded(),
            "header_ranges": caps["header_ranges"],
            "imitation_packets": caps["imitation_packets"],
            "header_protection_key": caps["header_protection_key"],
            "content_padding": caps["content_padding"],
            "timers": caps["timers"],
            "unknown": sorted(set(unknown)),
        }

    def _cached(self, key: str, produce: Callable[[], Any], ttl: float = FEATURES_TTL) -> Any:
        """Memoise a probe for ttl seconds; features() is read on every status poll."""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
        value = produce()
        with self._lock:
            self._cache[key] = (now, value)
        return value


def mock_enabled() -> bool:
    """True when AWG_MOCK asks for the in-memory controller."""
    return (os.environ.get(ENV_MOCK) or "").strip().lower() in {"1", "true", "yes", "on"}


_instances: dict[bool, BaseController] = {}
_instances_lock = threading.Lock()


def get_controller() -> BaseController:
    """The controller for this process: MockController when AWG_MOCK=1.

    Cached, because the mock's synthetic traffic only means anything if it
    accumulates between calls, and the real one keeps a feature cache worth
    reusing across requests.
    """
    mock = mock_enabled()
    found = _instances.get(mock)
    if found is not None:
        return found
    with _instances_lock:
        found = _instances.get(mock)
        if found is None:
            if mock:
                from .mock import MockController  # imports us back for Dump

                found = MockController()
            else:
                found = AwgController()
            _instances[mock] = found
    return found


def reset_controller() -> None:
    """Forget the cached controller. Tests move AWG_CONF_DIR between cases."""
    with _instances_lock:
        _instances.clear()


# ---------------------------------------------------------------- subprocess


def _sync_timeout(payload: str) -> int:
    """How long a lock-held `awg` call may take before it counts as wedged."""
    return SYNC_TIMEOUT + SYNC_TIMEOUT_PER_MB * (len(payload) // (1024 * 1024))


def _run(cmd: list[str], check: bool = True, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run a tool. Never shell=True: none of these arguments are ours to quote."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ToolError(cmd, message=f"{cmd[0]} is not installed on this server") from exc
    except OSError as exc:
        raise ToolError(cmd, message=f"cannot run {cmd[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(cmd, message=f"{cmd[0]} did not finish within {timeout}s") from exc
    if check and proc.returncode != 0:
        raise ToolError(cmd, proc.stderr, proc.returncode)
    return proc


def _try(cmd: list[str]) -> str | None:
    """stdout of cmd, or None if the tool is missing, slow or unhappy.

    Every read-only path goes through this: a status page must never 500
    because a binary is absent.
    """
    try:
        return _run(cmd).stdout
    except ToolError:
        return None


def _sysfs_module_version() -> str | None:
    """Version of the loaded module, straight out of sysfs. No subprocess.

    /sys/module/<name>/version exists for any module built with MODULE_VERSION,
    which this one is. Absent means either nothing is loaded or a module that
    does not declare one; both are "cannot say", and the caller falls back.
    """
    try:
        text = SYSFS_MODULE.joinpath(MODULE, "version").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def _systemctl(args: list[str]) -> str:
    """systemctl's one-word answer. is-active exits non-zero yet still prints it."""
    try:
        proc = _run(["systemctl", *args], check=False)
    except ToolError:
        return "unknown"
    out = proc.stdout.strip() or proc.stderr.strip()
    return out.splitlines()[0].strip() if out else "unknown"


def _unit_output(unit: str, since: float) -> str:
    """What the unit's own process printed, so a failure can be quoted properly.

    `systemctl start` that fails says only that the job failed and tells the
    reader to go and look in the journal; the line worth reading - awg-quick
    naming the parameter the kernel would not take, or the port already in use -
    is in the journal it points at. An operator in a browser cannot follow that
    instruction, so follow it for them.

    Matching on _SYSTEMD_UNIT rather than passing -u, which is sugar for that
    plus the messages *about* the unit that PID 1 logs. Those always come last,
    and "Failed to start AmneziaWG via awg-quick(8) for awg0" would then be the
    line quoted back - true, and the one thing the reader already knows.
    """
    # The suffix matters here and nowhere else: systemctl fills in ".service"
    # for a name that has no type, and a journal match field does not - it is
    # compared against the unit name verbatim, so a name without it matches
    # nothing and reads as a unit that logged in silence.
    out = _try(
        [
            "journalctl",
            f"_SYSTEMD_UNIT={unit}.service",
            "--since",
            f"@{since:.0f}",
            "-n",
            str(JOURNAL_LINES),
            "--no-pager",
            "-o",
            "cat",
        ]
    )
    return (out or "").strip()


def _usage_text() -> str | None:
    """`awg set` usage, lowercased. Printed to stderr with no interface given."""
    try:
        proc = _run(["awg", "set"], check=False)
    except ToolError:
        return None
    text = (proc.stdout + "\n" + proc.stderr).strip().lower()
    return text or None


def _secret_file(text: str) -> str:
    """Write text to a fresh 0600 file and return its path.

    mkstemp is 0600 already; the explicit chmod documents that this is not an
    accident, because what lands here is key material.
    """
    fd, tmp = tempfile.mkstemp(prefix="awg-panel.", suffix=".conf")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------- small parts


def _fields(line: str, count: int) -> list[str]:
    row = line.split("\t")
    if len(row) < count:
        row += [""] * (count - len(row))
    return row


def _clean(value: str) -> str:
    value = value.strip()
    return "" if value == NONE else value


def _int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return 0


def _mentions(usage: str, tokens: tuple[str, ...]) -> bool:
    """True when usage names one of these arguments as a whole word."""
    return any(re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", usage) for token in tokens)


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _max_version(*texts: str | None) -> tuple[int, ...] | None:
    found = [v for v in (_version_tuple(t) for t in texts) if v is not None]
    return max(found) if found else None
