"""Gunicorn settings for the panel's web service.

Everything tunable comes from /etc/awg-panel.env, which the UI rewrites when
the listen address, port or TLS settings change; systemd then restarts this
service. Keep the file free of anything that has to be edited by hand.
"""

import multiprocessing
import os
import sys
from pathlib import Path

# The env file's booleans are parsed by awgui.envflags, so that this file and
# awgui.settings cannot read the same key two different ways - they did, and
# AWG_PANEL_TLS=true meant HTTPS to Django and plain HTTP to gunicorn.
#
# The import needs help getting there. Gunicorn loads this file before it puts
# its working directory on sys.path, so at this point sys.path[0] is the
# directory the `gunicorn` script lives in and the panel is not importable at
# all. __file__ is the reliable way back to the panel root; it is absent only
# when a test exec()s this file, and there the panel is importable already.
try:
    from awgui.envflags import env_flag
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from awgui.envflags import env_flag


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _host(name: str, default: str) -> str:
    """The listen address as gunicorn's own parser needs to see it.

    AWG_PANEL_LISTEN takes an IPv6 address - bin/awg-menu offers one and
    validates one - and gunicorn splits host from port on a bare colon unless
    the host is in brackets. "::1" and port 2097 therefore parsed as an empty
    host and an empty port, and the service died at startup with "'' is not a
    valid port number" before it bound anything. systemd then restarted it
    forever, and nothing in the panel's own logs mentioned the address, because
    the panel was never reached.

    Empty reads as the default for the same reason it does in _int above: an
    env file with the key present and blank is a half-written one, not an
    instruction.
    """
    host = (os.environ.get(name, "") or default).strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


bind = f"{_host('AWG_PANEL_LISTEN', '0.0.0.0')}:{_int('AWG_PANEL_PORT', 2097)}"

# One admin, mostly 2-second polls: two workers keep a slow `awg-quick up`
# from blocking the dashboard, and more would only waste memory on a small VPS.
workers = min(_int("AWG_PANEL_WORKERS", 2), multiprocessing.cpu_count() * 2 + 1)
threads = _int("AWG_PANEL_THREADS", 4)
worker_class = "gthread"

timeout = 120  # awg-quick down/up on a busy interface is not instant
graceful_timeout = 20
keepalive = 5

# Set here rather than left to gunicorn's default, because one route depends on
# the number and the default is too small for it. GET stats/live takes ?peers=,
# which puts a page of public keys in the request line - 44 base64 characters
# each, and `+`, `/` and `=` all percent-encode, so a key costs about 51 bytes
# on the wire. Gunicorn's stock 4094 runs out around 78 of them and refuses the
# request without it ever reaching Django - as a bare 400, since gunicorn
# answers every request-line error that way, so nothing in the panel's own logs
# would have explained why the live poll died on large pages.
# 8190 is also what a fronting nginx accepts without extra configuration - its
# large_client_header_buffers default is 8k - so the direct and proxied
# deployments agree about the longest URL the panel will answer.
limit_request_line = 8190


def post_fork(server, worker):
    """Give this worker's connections a deadline gunicorn has no setting for.

    `timeout` above is the arbiter's heartbeat, not a limit on how long a client
    may take to send its request, and a gthread worker keeps answering that
    heartbeat while every one of its threads is blocked reading from a peer that
    went quiet. Without this, one stalled client costs a thread permanently and
    four of them cost the worker. See awgui.client_deadline for the details.
    """
    try:
        from awgui.client_deadline import install

        install()
    except Exception as exc:  # never let this stop the service from booting
        worker.log.warning("could not set the client socket deadline: %s", exc)


accesslog = "-" if env_flag("AWG_PANEL_ACCESS_LOG") else None
errorlog = "-"
loglevel = os.environ.get("AWG_PANEL_LOG_LEVEL", "info")
# No logconfig_dict here. Gunicorn merges one over its own defaults rather than
# replacing them, so a partial dict silently drops the `error_console` handler
# that its `gunicorn.error` logger refers to, and dictConfig then fails with
# "Unable to configure logger 'gunicorn.error'" - the master exits before it
# ever binds, and systemd restarts it forever. Gunicorn's stock logging already
# goes to stderr, which is what journald wants; the duplicated timestamp in the
# journal is not worth that risk.

# TLS terminated here when no reverse proxy is in front. Both paths must exist
# or gunicorn refuses to start, which is the right failure: silently serving
# plain HTTP after someone asked for TLS would be worse.
if env_flag("AWG_PANEL_TLS", strict=True):
    certfile = os.environ.get("AWG_PANEL_TLS_CERT") or ""
    keyfile = os.environ.get("AWG_PANEL_TLS_KEY") or ""
    if not certfile or not keyfile:
        # Leaving these unset would have gunicorn fall back to plain HTTP while
        # the operator, the env file and the UI all believe TLS is on, and the
        # password would cross the network in the clear. Refuse instead; the
        # journal says exactly which value is missing.
        missing = "AWG_PANEL_TLS_CERT" if not certfile else "AWG_PANEL_TLS_KEY"
        raise RuntimeError(
            f"AWG_PANEL_TLS is on but {missing} is empty. Set both the certificate and the key "
            f"in /etc/awg-panel.env, or set AWG_PANEL_TLS=0 and terminate TLS in a proxy."
        )

forwarded_allow_ips = os.environ.get("AWG_PANEL_FORWARDED_ALLOW_IPS", "127.0.0.1")
proc_name = "awg-panel-web"
