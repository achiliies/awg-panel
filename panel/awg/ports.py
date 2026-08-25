"""Is anything on this host already bound to a port, and what is it?

This is `port_busy` / `port_holder` from lib/common.sh, in Python, asked by the
save that moves the tunnel's listen port. Two services cannot share one UDP
port: the second one to ask for it does not get it, and for the tunnel that
means an interface that either refuses to come up or comes up and answers
nothing. Neither failure names the port it was about, and the admin who caused
it is looking at a panel that reported the save as successful.

What this can see is worth stating, because the answer is used to warn and
never to refuse a save. It sees sockets bound on this machine at the moment it
asks. It cannot see a service that is installed and stopped and will want its
port back at the next boot, and it knows nothing about a cloud security group,
which is the other half of why a tunnel nobody can reach is unreachable. UDP
has no listening state either - `-l` on a UDP socket means unconnected, which
is what a resolver and a WireGuard interface both look like - so "busy" here is
reliable and "free" is only likely.

Nothing raises. A machine without iproute2, or an `ss` that will not run, is a
question that cannot be answered rather than a save that should fail.
"""

import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

# ss reads /proc and answers immediately or not at all; the timeout is only
# here so that a save can never be held up by one.
TIMEOUT = 5

SS = "ss"

# users:(("systemd-resolve",pid=612,fd=13)) - the first name is the one to say.
_HOLDER = re.compile(r'users:\(\("([^"]+)"')


def busy(port: int, proto: str = "udp") -> bool:
    """True when something is bound to this port. False also means "cannot tell"."""
    return bool(_sockets(port, proto))


def holder(port: int, proto: str = "udp") -> str:
    """The name of the process holding it, or "" when ss will not say.

    Empty is not "free": an unprivileged caller is told the socket exists and
    not whose it is, so the warning still stands and only the culprit's name is
    missing. The panel runs as root, so in practice it gets the name.
    """
    match = _HOLDER.search(_sockets(port, proto))
    return match.group(1) if match else ""


def _sockets(port: int, proto: str) -> str:
    """ss's own lines for this port, or "" if there are none to have."""
    if not isinstance(port, int) or not 1 <= port <= 65535 or proto not in ("tcp", "udp"):
        return ""
    if not shutil.which(SS):
        return ""
    # -H drops the header so an empty result really is an empty string, and the
    # filter is ss's own so the port is matched rather than grepped for - 5182
    # must not answer for 51820.
    cmd = [SS, "-Hlnp", "-u" if proto == "udp" else "-t", f"sport = :{port}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ss could not be run: %s", exc)
        return ""
    if proc.returncode != 0:
        log.info("ss exited %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return ""
    return proc.stdout.strip()
