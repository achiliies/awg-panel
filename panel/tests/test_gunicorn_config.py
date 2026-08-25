"""The deployed server actually starts.

Every other test here drives Django through the test client or `runserver`,
neither of which reads `deploy/gunicorn.conf.py`. That file is what systemd
uses in production, so a mistake in it is invisible to the whole suite and
shows up as a service that crash-loops on a real box.

It has happened once: a partial `logconfig_dict` replaced gunicorn's handlers
wholesale, dropping the `error_console` handler its own `gunicorn.error` logger
refers to. `dictConfig` raised, the master exited before binding, and systemd
restarted it forever. So this boots the real thing and asks it for a page.
"""

import base64
import os
import random
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

import pytest
from django.conf import settings

from apps.stats.views import MAX_LIVE_PEERS

GUNICORN = Path(sys.executable).with_name("gunicorn")
CONF = Path(settings.BASE_DIR) / "deploy" / "gunicorn.conf.py"

pytestmark = pytest.mark.skipif(
    not GUNICORN.is_file() or not CONF.is_file(),
    reason="gunicorn or deploy/gunicorn.conf.py is not present",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_port6() -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.bind(("::1", 0))
        return int(sock.getsockname()[1])


def _has_ipv6_loopback() -> bool:
    """Whether ::1 can be bound at all, which a container often will not allow."""
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        AWG_MOCK="1",
        AWG_PANEL_ENV="/nonexistent",
        DJANGO_SETTINGS_MODULE="awgui.settings",
        AWG_PANEL_LISTEN="127.0.0.1",
        AWG_PANEL_BASE_PATH="/",
    )
    env.update(extra)
    return env


def _boot(tmp_path: Path, **extra: str) -> subprocess.Popen:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    env = _env(AWG_CONF_DIR=str(tmp_path / "conf"), AWG_PANEL_DATA=str(data), **extra)
    subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        check=True,
    )
    return subprocess.Popen(
        [str(GUNICORN), "-c", str(CONF), "awgui.wsgi:application"],
        cwd=settings.BASE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Its own process group, so _stop can take the workers with it.
        start_new_session=True,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the master and every worker it left behind."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _stop(proc: subprocess.Popen) -> str:
    """Drain the process and return everything it printed.

    Only signal it if it is still running: a config error kills gunicorn before
    we get here, and terminating a process that has already exited threw the
    output away the first time this was written.

    The kill has to reach the whole group. A worker with a thread stuck on a
    client outlives a SIGTERM to the master, and because it inherited the stdout
    pipe, `communicate` then waits on a writer that is never going to close it -
    the run hangs rather than failing, which is how this was found.
    """
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            return "(gunicorn would not exit and its output could not be drained)"
    return out or ""


def _verified_context(cert: Path) -> ssl.SSLContext:
    """A client that trusts exactly `cert` and nothing else.

    The certificate is self-signed, so it is handed in as the trust root as well
    - which is the point rather than a shortcut. Verification against this
    context succeeds only if the server presents that same certificate, so a
    panel serving anything else fails the handshake instead of quietly working.

    Hostname checking is off because these connect to 127.0.0.1 while the SAN
    reads `localhost`, and resolving the name instead would put the test at the
    mercy of whether localhost comes back as ::1 first - gunicorn is bound to
    the v4 address only. The name is checked in the one test below that opens
    its own socket and can pass server_hostname explicitly.
    """
    context = ssl.create_default_context(cafile=str(cert))
    context.check_hostname = False
    return context


def _fetch(url: str, context: ssl.SSLContext | None, deadline: float) -> str | None:
    """Retry `url` until it answers or `deadline` passes; the body, or None."""
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3, context=context) as response:
                return response.read().decode()
        except (URLError, OSError):
            time.sleep(0.3)
    return None


def _status(url: str, deadline: float) -> int | None:
    """The status `url` answers with, retrying until the server is up.

    HTTPError is caught before URLError deliberately: it is a subclass, and an
    answer of 403 or 414 is the measurement here rather than a connection that
    has not come up yet.
    """
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                return int(response.status)
        except HTTPError as answered:
            return int(answered.code)
        except (URLError, OSError):
            time.sleep(0.3)
    return None


def _peers_query(count: int) -> str:
    """`count` public keys, encoded the way the panel encodes them.

    Seeded rather than random so a failure reproduces. Real keys are what
    matters: they are base64 of 32 bytes, and `+`, `/` and the trailing `=` all
    percent-encode, so the length this produces is the length the browser sends
    - which a string of `peer0,peer1,...` would not have been.
    """
    keys = [base64.b64encode(random.Random(seed).randbytes(32)).decode() for seed in range(count)]
    return quote(",".join(keys), safe="")


def test_a_full_peers_query_fits_the_request_line(tmp_path):
    """MAX_LIVE_PEERS keys reach the application, and well past it does not.

    This is the only test that can say so. Every other test of `?peers=` goes
    through the Django test client, which builds a request object directly and
    never has a request line for anything to be too long for - so the cap on
    that parameter was checked everywhere except against the one limit that
    actually bounds it. It had been set to the largest page the client list will
    serve, 500, which is a 25 KB URL: gunicorn's stock limit_request_line of
    4094 would have refused it with a 414 before Django saw the request, and the
    panel would have shown the live poll failing on large pages with nothing in
    its own logs to say why.

    So the number is checked against a real server reading a real request line.
    The full query is measured against a single-key one rather than against a
    status code written out here: what is being asserted is that the length
    changes nothing, and pinning the number the panel happens to turn away
    unauthenticated requests with would fail this test the day that changed.
    """
    port = _free_port()
    proc = _boot(tmp_path, AWG_PANEL_PORT=str(port))
    try:
        url = f"http://127.0.0.1:{port}/api/v1/stats/live?peers="
        short = _status(url + _peers_query(1), time.monotonic() + 45)
        assert short is not None, f"the panel never answered; gunicorn said:\n{_stop(proc)}"

        status = _status(url + _peers_query(MAX_LIVE_PEERS), time.monotonic() + 15)
        assert status == short, (
            f"a full {MAX_LIVE_PEERS}-key query answered {status} where one key answered {short}; "
            f"raise limit_request_line or lower MAX_LIVE_PEERS"
        )

        # The far side, so the limit is shown to be real rather than assumed:
        # four times the cap is a URL no configuration of this panel accepts.
        # 400 rather than the 414 the status name would suggest - gunicorn
        # answers every request-line error with Bad Request, and only a fronting
        # nginx calls this one URI Too Long. Either way it never reaches Django,
        # which is the part that matters.
        over = _status(url + _peers_query(MAX_LIVE_PEERS * 4), time.monotonic() + 15)
        assert over == 400, f"a {MAX_LIVE_PEERS * 4}-key query answered {over}, not a refusal"
        assert proc.poll() is None, "gunicorn died on a long URL"
    finally:
        if proc.poll() is None:
            _stop(proc)


@pytest.mark.parametrize(
    ("listen", "expected"),
    [
        ("0.0.0.0", ("0.0.0.0", 2097)),
        ("127.0.0.1", ("127.0.0.1", 2097)),
        ("::", ("::", 2097)),
        ("::1", ("::1", 2097)),
        ("2001:db8::5", ("2001:db8::5", 2097)),
        # Already bracketed by an operator editing the env file by hand.
        ("[::1]", ("::1", 2097)),
        # Present and blank, which is a half-written env file rather than a
        # request to listen nowhere.
        ("", ("0.0.0.0", 2097)),
    ],
)
def test_the_listen_address_reaches_gunicorn_intact(monkeypatch, listen, expected):
    """AWG_PANEL_LISTEN survives the trip into `bind` for every address it takes.

    bin/awg-menu offers an IPv6 bind address and validates one, and the panel's
    own settings page writes the same key, so "::1" is a value an operator can
    choose without doing anything unusual. It used to be interpolated straight
    into `f"{host}:{port}"`, and gunicorn splits host from port on a bare colon
    unless the host is bracketed: "::1:2097" parsed as an empty host and an
    empty port, the master raised before binding, and systemd restarted it for
    ever. The panel never came up and never logged why, because nothing of the
    panel had run.

    The assertion goes through gunicorn's own parser rather than comparing the
    string, because the string is not the contract - what the address parses
    back to is, and a shape that merely looks right is exactly what was wrong
    before.
    """
    from gunicorn.util import parse_address

    monkeypatch.setenv("AWG_PANEL_LISTEN", listen)
    monkeypatch.setenv("AWG_PANEL_PORT", "2097")
    namespace: dict[str, object] = {}
    exec(compile(CONF.read_text(encoding="utf-8"), str(CONF), "exec"), namespace)  # noqa: S102
    assert parse_address(str(namespace["bind"])) == expected


@pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback to bind")
def test_gunicorn_serves_the_panel_over_ipv6(tmp_path):
    """The same boot as test_gunicorn_serves_the_panel, on the address that broke it.

    Parsing the bind string correctly is most of it, but only a real master
    binding a real socket says the panel answers there - and the failure this
    covers was a service that exited before it bound anything, which no amount
    of inspecting the configuration would have caught.
    """
    port = _free_port6()
    proc = _boot(tmp_path, AWG_PANEL_LISTEN="::1", AWG_PANEL_PORT=str(port))
    try:
        deadline = time.monotonic() + 45
        body = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"gunicorn exited with {proc.returncode}:\n{_stop(proc)}")
            try:
                with urlopen(f"http://[::1]:{port}/api/v1/health", timeout=2) as response:
                    body = response.read().decode()
                    break
            except (URLError, OSError):
                time.sleep(0.5)
        assert '"ok": true' in body or '"ok":true' in body, (
            f"health never answered on ::1; gunicorn said:\n{_stop(proc)}"
        )
        assert proc.poll() is None, "gunicorn died after answering"
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_gunicorn_serves_the_panel(tmp_path):
    port = _free_port()
    proc = _boot(tmp_path, AWG_PANEL_PORT=str(port))
    try:
        deadline = time.monotonic() + 45
        body = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"gunicorn exited with {proc.returncode}:\n{_stop(proc)}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                    body = response.read().decode()
                    break
            except (URLError, OSError):
                time.sleep(0.5)
        assert '"ok": true' in body or '"ok":true' in body, (
            f"health never answered; gunicorn said:\n{_stop(proc)}"
        )
        assert proc.poll() is None, "gunicorn died after answering"
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_https_serves_the_panel(tmp_path, make_certificate):
    """The HTTPS half of test_gunicorn_serves_the_panel, verified rather than waved through.

    Every other TLS test here reaches the panel only to establish that it came
    up, and does it with verification off, so none of them would notice a server
    that answered with the wrong certificate - or with one it had generated for
    itself. Trusting only the configured file turns the handshake into the
    assertion: this passes if and only if what gunicorn was given is what it
    serves.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
    )
    try:
        body = _fetch(
            f"https://127.0.0.1:{port}/api/v1/health",
            _verified_context(cert),
            time.monotonic() + 45,
        )
        assert body is not None, f"health never answered over TLS; gunicorn said:\n{_stop(proc)}"
        assert '"ok": true' in body or '"ok":true' in body, body[:200]
        assert proc.poll() is None, "gunicorn died after answering"
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_a_certificate_from_elsewhere_is_not_accepted(tmp_path, make_certificate):
    """The check above has to be capable of failing, and this is what proves it.

    A client trusting one certificate and offered another must not connect. If
    this ever passes, _verified_context has stopped verifying and every
    assertion resting on it is worthless.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    stranger, _, _ = make_certificate(tmp_path / "other", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
    )
    try:
        assert _answers(port, "https", time.monotonic() + 45), (
            f"the panel never came up at all:\n{_stop(proc)}"
        )
        refused = _fetch(
            f"https://127.0.0.1:{port}/api/v1/health",
            _verified_context(stranger),
            time.monotonic() + 5,
        )
        assert refused is None, f"a certificate the client does not trust was accepted: {refused!r}"
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_the_certificate_served_is_the_one_on_disk(tmp_path, make_certificate):
    """Byte for byte, and under the name it was issued for.

    Chain verification proves the server holds a key the trusted certificate
    covers; it does not prove the certificate on the wire is the same file the
    operator pointed at, and it says nothing about the name. Both matter here,
    because the panel's URL is built from the name on that certificate - a
    server answering under a name its certificate does not carry is a browser
    warning on every visit.

    This is the one place the SAN is exercised, so it opens the socket itself
    and names the host rather than resolving it.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
    )
    try:
        assert _answers(port, "https", time.monotonic() + 45), (
            f"the panel never came up at all:\n{_stop(proc)}"
        )
        context = ssl.create_default_context(cafile=str(cert))
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as tls:
                served = tls.getpeercert(binary_form=True)
                names = tls.getpeercert()["subjectAltName"]
        assert served == ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="utf-8")), (
            "the certificate served is not the file AWG_PANEL_TLS_CERT points at"
        )
        assert ("DNS", "localhost") in names, names
    finally:
        if proc.poll() is None:
            _stop(proc)


@pytest.mark.skipif(
    not (settings.FRONTEND_DIST / "index.html").is_file(),
    reason="frontend/dist is not built in this checkout",
)
def test_the_shell_loads_over_tls(tmp_path, make_certificate):
    """The page itself, not just the health endpoint, over the real transport.

    tests/test_spa_csp.py already pins what the shell contains, but it renders
    through the Django test client, where there is no socket and no TLS. The
    health endpoint is a short JSON body that fits one record; the shell is the
    largest thing the panel serves, and reading it back whole is what would
    catch a response truncated or mangled on its way out over TLS.

    `_boot` sets AWG_PANEL_BASE_PATH=/, so that is where the shell is, whatever
    base path this checkout's settings would otherwise compute.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
    )
    try:
        html = _fetch(f"https://127.0.0.1:{port}/", _verified_context(cert), time.monotonic() + 45)
        assert html is not None, f"the shell never loaded over TLS:\n{_stop(proc)}"
        assert "<html" in html.lower(), html[:200]
        assert "</html>" in html.lower(), (
            f"the shell came back truncated at {len(html)} bytes: {html[-200:]!r}"
        )
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_tls_without_a_certificate_refuses_to_start(tmp_path):
    """Falling back to plain HTTP when TLS was asked for would put the admin
    password on the wire while every screen claims it is encrypted.

    Output goes to a file rather than a pipe: gunicorn dies during config load,
    and draining a pipe from a process that has already gone came back empty.
    """
    log = tmp_path / "gunicorn.log"
    env = _env(
        AWG_CONF_DIR=str(tmp_path / "conf"),
        AWG_PANEL_DATA=str(tmp_path / "data"),
        AWG_PANEL_PORT=str(_free_port()),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT="",
    )
    with log.open("w") as sink:
        result = subprocess.run(
            [str(GUNICORN), "-c", str(CONF), "awgui.wsgi:application"],
            cwd=settings.BASE_DIR,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    output = log.read_text(errors="replace")
    assert result.returncode != 0, f"gunicorn should refuse this configuration:\n{output[:400]}"
    assert "AWG_PANEL_TLS_CERT" in output, output[:400]


def _listening(port: int, deadline: float) -> bool:
    """Whether anything accepts a TCP connection on `port` before `deadline`."""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def test_a_mismatched_certificate_and_key_still_never_serves_cleartext(tmp_path, make_certificate):
    """Two files that are each valid and do not belong together.

    The settings API refuses this pair before it can be saved, but that is a
    check on the way in and the files outlive it: a certificate renewed in place
    without its key, or a path edited in /etc/awg-panel.env by hand, arrives at
    gunicorn having passed nobody. The empty-certificate case above is caught
    during config load and the service dies loudly; this one is not.

    What gunicorn 26 actually does, which is worth writing down: it starts, logs
    `Listening at: https://...`, and stays up. The pair is only loaded when a
    connection arrives, so every request dies in the worker with
    `[X509: KEY_VALUES_MISMATCH]` while systemd goes on seeing a healthy
    service. The panel is unreachable and nothing restarts it.

    A cleartext request to that port is answered - unlike the healthy TLS port,
    which says nothing at all. The handshake fails before a byte of the request
    is read, and gunicorn's error path writes its own `403 Forbidden` back over
    what is still a plain socket, with the OpenSSL message in the body. So the
    port does emit cleartext here, and this test would be lying if it claimed
    otherwise.

    What it pins is the part that matters: no handshake completes, and nothing
    of the panel - no shell, no session, no health payload - is served in the
    clear. gunicorn's own refusal page is allowed, a 2xx is not. Whether the
    failure arrives as an alert, a reset, a 403 or a refusal to boot at all is
    left free, so that teaching gunicorn to reject the pair up front would
    improve this deployment without breaking this test.
    """
    cert, _, _ = make_certificate(tmp_path / "held", "localhost")
    _, stranger_key, _ = make_certificate(tmp_path / "stranger", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(stranger_key),
        AWG_PANEL_CLIENT_TIMEOUT="5",
    )
    try:
        if not _listening(port, time.monotonic() + 30):
            # Refusing to boot is a perfectly good answer to this configuration.
            assert proc.poll() is not None, "the port never opened and gunicorn is still running"
            return

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                with context.wrap_socket(raw, server_hostname="localhost") as tls:
                    pytest.fail(f"a handshake completed on a mismatched pair: {tls.version()}")
        except OSError:
            pass  # An alert, a reset or a hang-up: all of them mean no session.

        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                answer = sock.recv(1024)
            except ConnectionResetError:
                answer = b""
        if answer.startswith(b"HTTP/"):
            fields = answer.split(b" ")
            status = fields[1] if len(fields) > 1 else b""
            assert not status.startswith(b"2"), f"the panel answered in the clear: {answer[:200]!r}"
        assert b'"ok"' not in answer, f"the health payload came back in the clear: {answer[:200]!r}"
        assert b"<form" not in answer.lower(), f"a form came back in the clear: {answer[:200]!r}"
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_plain_http_gets_nothing_from_the_tls_port(tmp_path, make_certificate):
    """Turning HTTPS on has to take HTTP away, and here it is the socket that does it.

    awgui.middleware.HttpsOnlyMiddleware is the same rule one layer up, for the
    proxy deployment where gunicorn is listening in the clear by design. This
    pins the other half: with the certificate held here, a cleartext request
    gets no HTTP response at all - not a redirect, and certainly not the login
    form. If that ever regressed into a plain-HTTP fallback the admin's password
    would cross the network while every screen said otherwise.

    What comes back instead is not fixed, and the test must not care which of
    the two it gets. OpenSSL recognises a request line where a record header
    should be and fails the handshake with HTTP_REQUEST without answering, so
    nothing is ever written back; the connection then closes with the rest of
    the request still unread in the receive queue, which is the case where the
    kernel sends RST rather than FIN. Whether the client sees that as an empty
    read or as ECONNRESET is a race it cannot win either way, and a reset is if
    anything the stronger result - not one byte came back.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
        AWG_PANEL_CLIENT_TIMEOUT="5",
    )
    try:
        assert _answers(port, "https", time.monotonic() + 45), (
            f"the panel never came up at all:\n{_stop(proc)}"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(
                b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            try:
                answer = sock.recv(1024)
            except ConnectionResetError:
                answer = b""
        assert not answer.startswith(b"HTTP/"), f"served over plain HTTP: {answer[:200]!r}"
        assert b"ok" not in answer, f"a payload came back in the clear: {answer[:200]!r}"
        assert proc.poll() is None, f"the worker died:\n{_stop(proc)}"
    finally:
        if proc.poll() is None:
            _stop(proc)


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 not on PATH")
def test_config_is_importable_on_its_own(tmp_path):
    """A syntax error or a bad env read here breaks the service, not a test."""
    result = subprocess.run(
        [sys.executable, "-c", f"exec(open({str(CONF)!r}).read())"],
        env=_env(AWG_PANEL_PORT="2097"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------- stalled clients
#
# A peer that connects and then says nothing used to cost a thread forever, and
# over TLS it cost the whole worker: the handshake ran on the accept loop. Both
# are what awgui.client_deadline fixes, and neither is visible to a test that
# only ever makes well-behaved requests. These open real sockets and leave them
# silent.


# A TLS record header announcing 512 bytes of handshake that never arrive. It
# has to be a partial send rather than none at all: gthread leaves a connection
# in the poller until it is readable, so a peer that says literally nothing is
# never enqueued and costs nothing. The damage starts once it says just enough.
PARTIAL_TLS = b"\x16\x03\x01\x02\x00"

# Request line and one header, and no blank line to end them.
PARTIAL_REQUEST = b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"


def _stalled_sockets(port: int, count: int, payload: bytes) -> list[socket.socket]:
    """Connect `count` times, send an unfinishable fragment, then go quiet."""
    held = []
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(payload)
        held.append(sock)
    return held


def _answers(port: int, scheme: str, deadline: float) -> bool:
    """Whether /api/v1/health responds before `deadline`, retrying meanwhile."""
    context = None
    if scheme == "https":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    while time.monotonic() < deadline:
        try:
            url = f"{scheme}://127.0.0.1:{port}/api/v1/health"
            with urlopen(url, timeout=3, context=context) as response:
                if '"ok"' in response.read().decode():
                    return True
        except (URLError, OSError):
            time.sleep(0.3)
    return False


def test_half_open_tls_handshakes_do_not_exhaust_the_thread_pool(tmp_path, make_certificate):
    """The shape this deployment actually failed in, over the transport it uses.

    Gunicorn leaves `do_handshake_on_connect` off, so the handshake happens on
    the pool thread's first read. A peer that sends a record header and then
    stops holds that thread in `ssl.recv` exactly the way a half-sent request
    holds it in the parser - which is where all four threads of the wedged
    worker were found.
    """
    cert, key, _ = make_certificate(tmp_path / "tls", "localhost")
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_WORKERS="1",
        AWG_PANEL_THREADS="2",
        AWG_PANEL_TLS="1",
        AWG_PANEL_TLS_CERT=str(cert),
        AWG_PANEL_TLS_KEY=str(key),
        AWG_PANEL_CLIENT_TIMEOUT="3",
    )
    held: list[socket.socket] = []
    try:
        assert _answers(port, "https", time.monotonic() + 45), (
            f"the panel never came up at all:\n{_stop(proc)}"
        )
        held = _stalled_sockets(port, 5, PARTIAL_TLS)
        assert _answers(port, "https", time.monotonic() + 25), (
            f"half-open handshakes kept the thread pool:\n{_stop(proc)}"
        )
        assert proc.poll() is None, f"the worker died:\n{_stop(proc)}"
    finally:
        for sock in held:
            sock.close()
        if proc.poll() is None:
            _stop(proc)


def test_stalled_clients_do_not_exhaust_the_thread_pool(tmp_path):
    """More half-sent requests than threads, and the panel still has to answer.

    Each one is readable enough to be handed to the pool and incomplete enough
    that the parser waits for the rest. Without a deadline on the socket that
    wait never ends, and the next caller - the admin - finds no thread left.
    """
    port = _free_port()
    proc = _boot(
        tmp_path,
        AWG_PANEL_PORT=str(port),
        AWG_PANEL_WORKERS="1",
        AWG_PANEL_THREADS="2",
        AWG_PANEL_CLIENT_TIMEOUT="3",
    )
    held: list[socket.socket] = []
    try:
        assert _answers(port, "http", time.monotonic() + 45), (
            f"the panel never came up at all:\n{_stop(proc)}"
        )
        held = _stalled_sockets(port, 5, PARTIAL_REQUEST)
        # Longer than the deadline: the threads those peers hold have to come
        # back before this can pass, which is the whole point.
        assert _answers(port, "http", time.monotonic() + 25), (
            f"stalled peers kept the thread pool:\n{_stop(proc)}"
        )
        assert proc.poll() is None, f"the worker died:\n{_stop(proc)}"
    finally:
        for sock in held:
            sock.close()
        if proc.poll() is None:
            _stop(proc)
