"""A deadline on client socket I/O, which the gthread worker does not have.

Gunicorn's `timeout` is a heartbeat between the arbiter and the worker, not a
limit on how long a request may take to arrive. A gthread worker answers that
heartbeat from its accept loop, so a peer that starts sending and then stops is
never noticed: the pool thread reading it blocks in `recv` with no deadline, the
arbiter goes on seeing a healthy worker, and `timeout` never fires. As many such
peers as there are threads - four, by default - and that worker cannot serve
anyone again for as long as it lives.

It takes a fragment, not silence. A connection sits in the poller until it is
readable, so a peer that says literally nothing is never handed to the pool and
costs nothing; the damage starts once it sends part of a request, or part of a
TLS record, and leaves the rest unsent. Over TLS the handshake is a read like
any other, so half a ClientHello parks a thread just as effectively as half a
request.

That is not hypothetical here. The panel is reachable on its own port with no
proxy in front to absorb slow clients, and on this deployment all four threads
of one worker were found parked in `ssl.recv` against a single peer - each with
bytes still unread in the kernel, the signature of a half-sent record - while
the other worker carried the whole panel; requests landed on a live worker or a
dead one roughly half the time.

The fix is one change in behaviour: every accepted socket gets a timeout. It
bounds the handshake and every later recv and send, so a stalled peer costs a
thread for `AWG_PANEL_CLIENT_TIMEOUT` seconds instead of forever. A healthy
client pays nothing, because a timeout trips on total silence rather than on a
slow trickle - `recv` returns as soon as any byte arrives.

It has to be a patch; gunicorn exposes no setting for it. Two places need it,
and both are wrapped rather than rewritten, because `TConn.init` is not the same
function from one gunicorn to the next - 23 leaves the TLS handshake to the
first read, while 26 completes it inline to negotiate ALPN and may build an
HTTP/2 parser instead. Rewriting it would mean silently dropping whichever of
those the installed version does:

  * `TConn.init` gets the timeout applied after it returns, which covers plain
    HTTP and the re-init that a keepalive connection goes through, both of which
    clear it again via `setblocking(True)`.
  * `ssl_wrap_socket` gets it applied to the socket on the way in, so the
    handshake is bounded wherever that version chooses to perform it, and the
    wrapped socket inherits a deadline from birth.

A reverse proxy that buffers requests is the more complete answer and would make
this module unnecessary; until the panel grows one, this keeps a handful of
stalled clients from taking the panel down.
"""

import logging
import os

log = logging.getLogger(__name__)

# Long enough that no real browser on a lossy VPN link trips it, short enough
# that a wedged thread comes back on a timescale an admin will not notice.
DEFAULT_TIMEOUT = 30.0

ENV_TIMEOUT = "AWG_PANEL_CLIENT_TIMEOUT"

# Set on the gthread module once patched, so a second call is a no-op rather
# than a wrapper around a wrapper.
_MARKER = "_awg_client_deadline"


def configured_timeout() -> float:
    """The deadline in seconds. Zero or negative disables the patch entirely."""
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %ss", ENV_TIMEOUT, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT


def install(timeout: float | None = None) -> bool:
    """Give every accepted connection a read/write deadline. True if applied.

    Called from `post_fork`, so it runs once per worker before the pool is busy.
    Never raises: a panel that starts without the deadline is the behaviour we
    already had, while a panel that will not start at all is worse.
    """
    seconds = configured_timeout() if timeout is None else float(timeout)
    if seconds <= 0:
        return False

    try:
        from gunicorn.workers import gthread
    except ImportError:  # pragma: no cover - gunicorn is a hard dependency in production
        return False

    if getattr(gthread, _MARKER, False):
        return True

    original_init = getattr(gthread.TConn, "init", None)
    original_wrap = getattr(gthread.sock, "ssl_wrap_socket", None)
    if original_init is None or original_wrap is None:  # pragma: no cover - upgrade guard
        log.warning("gunicorn's gthread is not the shape this expects; no client deadline set")
        return False

    def ssl_wrap_socket(raw, cfg):
        """Wrap for TLS, with the handshake on a socket that cannot wait forever."""
        raw.settimeout(seconds)
        wrapped = original_wrap(raw, cfg)
        # The wrapped socket does inherit the timeout, but it is a new object
        # and this is too load-bearing to leave implicit.
        wrapped.settimeout(seconds)
        return wrapped

    def init(self):
        """TConn.init, and then the deadline it cleared with setblocking(True)."""
        result = original_init(self)
        self.sock.settimeout(seconds)
        return result

    gthread.sock.ssl_wrap_socket = ssl_wrap_socket
    gthread.TConn.init = init
    setattr(gthread, _MARKER, seconds)
    log.info("client socket deadline set to %ss", seconds)
    return True
