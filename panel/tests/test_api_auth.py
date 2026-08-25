"""The gate: sign-in, sessions, CSRF and the secret base path.

These are the four things every other endpoint takes for granted, so they are
tested against behaviour rather than against implementation. A wrong password
and a username that does not exist have to produce the same bytes, not merely
the same status code; a mutation without a CSRF token has to be refused before
the handler runs, which is checked by looking at the files afterwards; and a
request outside the base path has to be indistinguishable from a server with
nothing on it, which means an empty body and none of the headers the panel adds
to everything else.

The lockout test drives the real login form six times because that is what an
attacker does. It asserts that the answer changes shape - 401 "wrong password"
becomes 403 "blocked, wait" - since the SPA renders a countdown from it, and
that the correct password does not open the door once the address is blocked.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient

from apps.panel import settings_store
from apps.panel.models import Setting
from awgui.middleware import (
    TrustedProxyMiddleware,
    client_ip_from_forwarded_for,
    peer_is_trusted,
)
from awgui.settings import _env_flag

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"

# Every request that changes something, with a body good enough to reach the
# handler. None of them may run without a CSRF token, including login: it is
# exempt from authentication, not from CSRF, or a third-party page could sign a
# browser into an account of its choosing.
MUTATIONS = [
    ("post", "auth/login", {"username": USERNAME, "password": PASSWORD}),
    ("post", "auth/logout", {}),
    ("put", "settings", {"configEndpointHost": "attacker.example"}),
    ("post", "settings/account", {"current": PASSWORD, "new": "another-good-password"}),
    ("post", "settings/2fa/enable", {}),
    ("post", "settings/2fa/disable", {"password": PASSWORD}),
    ("post", "settings/sessions/revoke-others", {}),
    # Any well-formed id will do: the token has to be checked before the handler
    # ever looks the session up.
    ("delete", "settings/sessions/2f1c4e6a-0000-4000-8000-000000000000", {}),
    ("post", "restore", {}),
    ("post", "update/check", {}),
    ("post", "update/apply", {}),
    ("put", "server", {"dns": "9.9.9.9"}),
    ("post", "server/reconfigure", {}),
    ("post", "server/restart", {}),
    ("post", "clients", {"name": "intruder"}),
    ("put", "clients/client1", {"note": "changed"}),
    ("delete", "clients/client1", {}),
    ("post", "clients/client1/reset-keys", {}),
    ("post", "clients/client1/reset-usage", {}),
]

# Everything an anonymous caller must be turned away from. health and
# auth/session are deliberately absent: they are the two the login page itself
# needs.
GUARDED = [
    ("get", "settings"),
    # Who is signed in, and from where, is not for an anonymous caller.
    ("get", "settings/sessions"),
    ("post", "settings/sessions/revoke-others"),
    ("get", "backup"),
    ("get", "update/status"),
    ("get", "server"),
    ("get", "server/params"),
    ("get", "server/status"),
    ("get", "clients"),
    ("get", "clients/export.zip"),
    ("get", "clients/client1"),
    ("get", "clients/client1/config"),
    ("get", "clients/client1/qr"),
    # When a client was on the tunnel, and how much they moved, day by day.
    ("get", "clients/client1/traffic"),
    ("get", "stats/live"),
    ("get", "stats/summary"),
    ("get", "stats/traffic"),
    ("post", "clients"),
    ("post", "auth/logout"),
    ("post", "server/restart"),
    ("put", "server"),
]


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


def send(
    client: APIClient, method: str, path: str, payload: dict | None = None, **extra: object
) -> HttpResponse:
    """One request by method name, so a table of endpoints can be driven from data."""
    return getattr(client, method)(api_url(path), payload or {}, format="json", **extra)


def login(client: APIClient, password: str = PASSWORD, username: str = USERNAME) -> HttpResponse:
    """Sign in the way the login form does."""
    return client.post(
        api_url("auth/login"), {"username": username, "password": password}, format="json"
    )


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    """Drop the settings cache around every test.

    settings_store keeps values for a few seconds so the dashboard poll does not
    hit SQLite every time. That cache outlives the per-test database rollback,
    so a panel name written by one test would otherwise turn up in the session
    payload of the next.
    """
    settings_store.invalidate()
    yield
    settings_store.invalidate()


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(USERNAME, password=PASSWORD)


@pytest.fixture
def anon() -> APIClient:
    """A browser that has never signed in, with CSRF enforced as in production.

    The Django test client waives CSRF by default, which is convenient
    everywhere else and useless here.
    """
    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def signed_in(anon: APIClient) -> APIClient:
    """A signed-in browser with CSRF enforced.

    The account has no usable password on purpose: what these tests need is a
    session, and Argon2 is slow enough that hashing one for every case in the
    table below would double the time this file takes.
    """
    anon.force_login(get_user_model().objects.create_user(USERNAME))
    return anon


def csrf_token(client: APIClient) -> str:
    """Do what the SPA does on boot: read the cookie GET auth/session sets."""
    client.get(api_url("auth/session"))
    return client.cookies[settings.CSRF_COOKIE_NAME].value


# --------------------------------------------------------------------- login


def test_login_starts_a_session_and_returns_the_bootstrap_payload(admin):
    client = APIClient()

    response = login(client)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["authenticated"] is True
    assert body["username"] == USERNAME
    assert body["otpRequired"] is False
    assert body["basePath"] == settings.BASE_PATH
    assert body["version"] == settings.PANEL_VERSION
    # The suite runs against the mock controller, and the UI has to say so
    # rather than present invented traffic as if it came off a real interface.
    assert body["mock"] is True

    assert settings.SESSION_COOKIE_NAME in response.cookies
    assert client.session["_auth_user_id"] == str(admin.pk)
    # A later request is authenticated by the cookie alone.
    assert client.get(api_url("auth/session")).json()["authenticated"] is True


def test_a_wrong_password_and_an_unknown_user_are_answered_identically(admin):
    """The panel has one account, usually called admin, so a distinction only helps a guesser."""
    wrong_password = login(APIClient(), password="not-the-password")
    unknown_user = login(APIClient(), username="someone-else", password="not-the-password")

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.json()["detail"] == "Wrong username or password."


def test_repeated_failures_lock_the_address_out_with_a_distinguishable_403(admin):
    client = APIClient()
    attempts = [login(client, password="not-the-password") for _ in range(6)]

    assert attempts[0].status_code == 401
    blocked = [response for response in attempts if response.status_code == 403]
    assert blocked, [response.status_code for response in attempts]

    body = blocked[0].json()
    assert body["detail"] != attempts[0].json()["detail"]
    # Seconds until the address is allowed to try again; the login form counts
    # down with it instead of inviting an attempt that cannot succeed.
    assert int(body["errors"]["lockout"]) > 0

    # The lockout is about the address, so knowing the password is no way out.
    correct = login(client)
    assert correct.status_code == 403
    assert not client.session.get("_auth_user_id")


def test_the_login_endpoint_is_the_only_way_in_without_a_session():
    anonymous = APIClient()

    assert anonymous.get(api_url("health")).status_code == 200
    assert anonymous.get(api_url("auth/session")).json()["authenticated"] is False

    for method, path in GUARDED:
        response = send(anonymous, method, path)
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        assert response.json()["detail"]


# -------------------------------------------------------------------- logout


def test_logout_clears_the_session(admin):
    client = APIClient()
    assert login(client).status_code == 200
    session_key = client.session.session_key

    response = client.post(api_url("auth/logout"), {}, format="json")

    assert response.status_code == 204
    assert not Session.objects.filter(session_key=session_key).exists()
    assert client.get(api_url("auth/session")).json()["authenticated"] is False
    assert client.get(api_url("settings")).status_code == 401


# ------------------------------------------------------------------- session


def test_the_session_endpoint_answers_anonymous_callers_without_leaking(admin):
    """It is how the login page learns the panel name and gets its CSRF cookie."""
    client = APIClient()

    response = client.get(api_url("auth/session"))

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["username"] == ""
    # False for anonymous callers whether or not the admin has a second factor:
    # otherwise the login page reports on the account's protection.
    assert body["otpRequired"] is False
    assert body["basePath"] == settings.BASE_PATH
    assert settings.CSRF_COOKIE_NAME in response.cookies


def test_the_session_payload_follows_the_stored_panel_settings(admin):
    settings_store.set_many({"theme": "dark", "language": "ru"})
    client = APIClient()
    assert login(client).status_code == 200

    body = client.get(api_url("auth/session")).json()

    assert body["theme"] == "dark"
    assert body["language"] == "ru"


# ---------------------------------------------------------------------- CSRF


@pytest.mark.parametrize(("method", "path", "payload"), MUTATIONS, ids=[m[1] for m in MUTATIONS])
def test_every_mutation_is_refused_without_a_csrf_token(
    signed_in, server_conf, server_conf_text, method, path, payload
):
    response = send(signed_in, method, path, payload)

    assert response.status_code == 403, response.content
    # Both rejection paths - DRF's session authenticator and the csrf_protect
    # decorator on the login view - say that the token is what was missing,
    # which is what tells the SPA to reload rather than to sign in again.
    detail = response.json()["detail"].lower()
    assert "csrf" in detail or "security token" in detail, detail
    # Nothing ran: the config is byte-identical and no setting was written.
    assert server_conf.read_text(encoding="utf-8") == server_conf_text
    assert not Setting.objects.exists()


def test_the_same_request_goes_through_with_the_token_the_spa_sends(signed_in):
    """Control for the test above: the 403s are about the token, not about the payload."""
    token = csrf_token(signed_in)

    response = signed_in.put(
        api_url("settings"),
        {"configEndpointHost": "attacker.example"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 200, response.content
    assert Setting.objects.get(key="configEndpointHost").value == "attacker.example"


# ---------------------------------------------------------------- base path


def test_a_path_outside_the_base_path_is_a_bare_404():
    """The secret prefix is not authentication, but it only hides anything if a miss is silent."""
    inside = APIClient()
    health = api_url("health")
    assert inside.get(health).status_code == 200
    # A wrong path under the right prefix is a normal, explained 404.
    known = inside.get(api_url("no-such-endpoint"))
    assert known.status_code == 404
    assert known.json()["detail"]

    with override_settings(BASE_PATH="/p/ab12cd34/"):
        # The same URL that just answered, now outside the prefix. A fresh
        # client, because the middleware chain reads the base path once when it
        # is built.
        outside = APIClient().get(health)

    assert outside.status_code == 404
    assert outside.content == b""
    # No fingerprint: none of the headers the panel puts on everything else, and
    # no redirect to the real prefix.
    assert "Content-Security-Policy" not in outside.headers
    assert "Location" not in outside.headers


# ----------------------------------------------------- the address behind a proxy


@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        # nginx's $proxy_add_x_forwarded_for appends the peer it saw, so the
        # right-most entry is the only one the proxy vouches for.
        ("203.0.113.9", "203.0.113.9"),
        ("198.51.100.4, 203.0.113.9", "203.0.113.9"),
        ("2001:db8::1, 203.0.113.9", "203.0.113.9"),
        # A client that appends junk must not get to pick its own bucket.
        ("203.0.113.9, not-an-address", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("   ", "127.0.0.1"),
    ],
)
def test_the_lockout_address_is_the_one_the_proxy_vouches_for(forwarded, expected):
    """django-axes counts failures per address, and behind a proxy every request
    arrives from 127.0.0.1 - so without this the whole internet shares one
    bucket and any stranger can lock the admin out of their own server."""
    request = RequestFactory().post("/login")
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    request.META["HTTP_X_FORWARDED_FOR"] = forwarded

    assert client_ip_from_forwarded_for(request) == expected


@pytest.mark.parametrize(
    ("allow", "peer", "trusted"),
    [
        # The default, and the deployment docs/PANEL.md describes: nginx on
        # loopback, everyone else reaching the port directly.
        (None, "127.0.0.1", True),
        (None, "203.0.113.9", False),
        (None, "::1", False),
        # An operator's own list, in both the forms gunicorn takes for the
        # same key.
        ("10.0.0.2", "10.0.0.2", True),
        ("10.0.0.2", "10.0.0.3", False),
        ("127.0.0.1, 10.0.0.2", "10.0.0.2", True),
        ("*", "203.0.113.9", True),
        ("*, 10.0.0.2", "203.0.113.9", True),
        # And CIDR, for a proxy on a container bridge whose address moves.
        ("172.17.0.0/16", "172.17.0.4", True),
        ("172.17.0.0/16", "172.18.0.4", False),
        # Nothing usable to compare against is not a reason to believe it.
        ("10.0.0.2", None, False),
        ("10.0.0.2", "", False),
        ("not-an-address", "10.0.0.2", False),
    ],
)
def test_only_a_declared_proxy_is_believed(monkeypatch, allow, peer, trusted):
    """Which peer sent the header is the whole question the header cannot answer."""
    if allow is None:
        monkeypatch.delenv("AWG_PANEL_FORWARDED_ALLOW_IPS", raising=False)
    else:
        monkeypatch.setenv("AWG_PANEL_FORWARDED_ALLOW_IPS", allow)
    assert peer_is_trusted(peer) is trusted


def test_a_direct_caller_cannot_pick_its_own_lockout_bucket(monkeypatch):
    """The bypass this is all for.

    AXES_LOCKOUT_PARAMETERS is ["ip_address"], so an X-Forwarded-For the caller
    writes itself is a fresh bucket on every attempt: five tries per address
    becomes unlimited tries against the one account the panel has. The panel
    binds 0.0.0.0 by default, so turning AWG_PANEL_TRUST_PROXY on without
    firewalling the port used to be enough to hand that out.
    """
    monkeypatch.delenv("AWG_PANEL_FORWARDED_ALLOW_IPS", raising=False)
    request = RequestFactory().post("/login")
    request.META["REMOTE_ADDR"] = "203.0.113.9"
    request.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.77"

    # Counted against the socket it actually arrived on, so the attacker's own
    # attempts pile up in one bucket - which is what the limit is.
    assert client_ip_from_forwarded_for(request) == "203.0.113.9"


@pytest.mark.parametrize(
    "header",
    [
        "HTTP_X_FORWARDED_FOR",
        "HTTP_X_FORWARDED_PROTO",
        "HTTP_X_FORWARDED_HOST",
        "HTTP_X_FORWARDED_PORT",
    ],
)
def test_forwarded_headers_from_an_untrusted_peer_never_reach_the_stack(monkeypatch, header):
    """Removed rather than ignored, because the readers cannot be made to ask.

    SECURE_PROXY_SSL_HEADER and USE_X_FORWARDED_HOST are Django's own and read
    META directly; neither takes a condition. X-Forwarded-Proto: https was also
    the way past HttpsOnlyMiddleware, which is what let a direct caller reach
    the login form on a panel that believes it is behind TLS.
    """
    monkeypatch.delenv("AWG_PANEL_FORWARDED_ALLOW_IPS", raising=False)
    seen = {}

    def capture(request):
        seen.update(request.META)
        return HttpResponse(b"")

    request = RequestFactory().get("/")
    request.META["REMOTE_ADDR"] = "203.0.113.9"
    request.META[header] = "https" if header.endswith("PROTO") else "evil.example.com"

    TrustedProxyMiddleware(capture)(request)

    assert header not in seen
    assert header not in request.META


def test_a_declared_proxys_forwarded_headers_are_left_alone(monkeypatch):
    """The proxy deployment has to keep working, which is the point of the flag."""
    monkeypatch.delenv("AWG_PANEL_FORWARDED_ALLOW_IPS", raising=False)
    seen = {}

    def capture(request):
        seen.update(request.META)
        return HttpResponse(b"")

    request = RequestFactory().get("/")
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    request.META["HTTP_X_FORWARDED_FOR"] = "203.0.113.9"
    request.META["HTTP_X_FORWARDED_PROTO"] = "https"

    TrustedProxyMiddleware(capture)(request)

    assert seen["HTTP_X_FORWARDED_FOR"] == "203.0.113.9"
    assert seen["HTTP_X_FORWARDED_PROTO"] == "https"


def test_the_forwarded_header_is_ignored_unless_a_proxy_was_declared():
    """It is pure client input otherwise, and honouring it would make the
    lockout a formality: one header per attempt and nobody is ever locked."""
    assert getattr(settings, "AXES_CLIENT_IP_CALLABLE", None) is None or _env_flag(
        "AWG_PANEL_TRUST_PROXY"
    )
