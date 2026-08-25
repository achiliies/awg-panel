"""Keep an active host firewall in step with the port the tunnel listens on.

This is `fw_move` from lib/firewall.sh, in Python, called from the same save
that restarts the interface. Without it a port changed from the panel leaves
ufw still allowing only the old one: the save reports success, the tunnel comes
back up, and every client stops connecting - which is precisely the failure the
shell tools have always avoided by moving the rule themselves.

ufw and firewalld are handled, and only when they are actually running. Every
other filter, and every cloud security group, is out of reach from here and
stays the operator's job, which is why `move_port` returns the name of whatever
took the change rather than a success flag: the caller has to say what is still
left to do by hand.

Nothing here raises. A firewall that will not cooperate must not fail a save
that has already been written to disk - it is a rule to fix, not a reason to
leave the config half-applied.
"""

import logging
import shutil
import subprocess

from .controller import mock_enabled

log = logging.getLogger(__name__)

# ufw waits on its own lock and firewall-cmd talks to a D-Bus service; neither
# is quick, and neither may hold up a settings save for long.
TIMEOUT = 15

UFW = "ufw"
FIREWALLD = "firewalld"


def move_port(new: int, old: int = 0, proto: str = "udp") -> str:
    """Allow `new`, then drop `old` once the new rule is in place.

    Returns the backend that took the change ("ufw", "firewalld"), or "" when
    no supported firewall is active - which is not a failure, only a host that
    filters somewhere else. `old` of 0 just opens the new port.
    """
    if not _port_ok(new) or (old and not _port_ok(old)):
        return ""
    # AWG_MOCK is a laptop or a CI runner. Rewriting the developer's own
    # firewall because a demo config changed port would be indefensible.
    if mock_enabled():
        return ""

    if _ufw_active():
        if not _run([UFW, "allow", f"{new}/{proto}"]):
            return ""
        if old and old != new:
            _run([UFW, "delete", "allow", f"{old}/{proto}"])
        return UFW

    if _firewalld_active():
        if not _run(["firewall-cmd", "-q", "--permanent", f"--add-port={new}/{proto}"]):
            return ""
        if old and old != new:
            _run(["firewall-cmd", "-q", "--permanent", f"--remove-port={old}/{proto}"])
        # Permanent rules are the ones that survive a reboot; the reload is what
        # puts them in force now.
        _run(["firewall-cmd", "-q", "--reload"])
        return FIREWALLD

    return ""


def note(backend: str, new: int, old: int = 0, proto: str = "udp") -> str:
    """One sentence about the firewall, for the admin who has to finish the job.

    There is always something to say: either the host firewall was updated and
    the cloud security group was not, or nothing was updated at all and the
    whole thing is manual.
    """
    if backend == UFW:
        moved = f" The old rule for {old}/{proto} was removed." if old and old != new else ""
        return (
            f"ufw now allows {new}/{proto}.{moved} A cloud security group, if there is one, "
            "still needs the new port opened by hand."
        )
    if backend == FIREWALLD:
        moved = f" The old rule for {old}/{proto} was removed." if old and old != new else ""
        return (
            f"firewalld now allows {new}/{proto} permanently.{moved} A cloud security group, if "
            "there is one, still needs the new port opened by hand."
        )
    return (
        f"No ufw or firewalld rule was changed, so allow inbound {new}/{proto} yourself - in the "
        "host firewall if you run one, and in the cloud security group. Until that is done no "
        "client can reach the tunnel."
    )


# ------------------------------------------------------------------ internals


def _port_ok(port: int) -> bool:
    return isinstance(port, int) and 1 <= port <= 65535


def _ufw_active() -> bool:
    """True when ufw is installed and enforcing, the same test the shell makes."""
    if not shutil.which(UFW):
        return False
    out = _capture([UFW, "status"])
    return any(line.strip() == "Status: active" for line in (out or "").splitlines())


def _firewalld_active() -> bool:
    if not shutil.which("firewall-cmd"):
        return False
    # --state exits non-zero when the daemon is not running, which is the whole
    # question; its output ("running"/"not running") is not needed.
    return _capture(["firewall-cmd", "--state"]) is not None


def _run(cmd: list[str]) -> bool:
    """Run one firewall command. False on any failure, and never an exception."""
    return _capture(cmd) is not None


def _capture(cmd: list[str]) -> str | None:
    """stdout of a successful command, or None if it failed in any way at all."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s could not be run: %s", cmd[0], exc)
        return None
    if proc.returncode != 0:
        log.info("%s exited %d: %s", " ".join(cmd), proc.returncode, proc.stderr.strip()[:200])
        return None
    return proc.stdout
