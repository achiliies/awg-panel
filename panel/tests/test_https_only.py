"""With HTTPS on, the panel answers nothing over plain HTTP.

The switch on the settings page reads as a statement about the wire, and where
gunicorn holds the certificate it is one: that socket replies to a cleartext
request with a TLS alert, which is what test_gunicorn_config pins from the
outside. Behind a reverse proxy it was not. `AWG_PANEL_TRUST_PROXY` turns
`PANEL_TLS` on so cookies get `Secure` and every screen says https://, while
gunicorn goes on listening in the clear - and until the middleware these tests
cover, anything that arrived on that socket was served the login form, then
handed the password back out of the POST that followed.

So the tests are about what a cleartext caller gets, not about which deployment
produced it: a reader is bounced to the same URL over HTTPS, anything that
writes is refused before it reaches a session or a password check, and a path
outside the secret base path still looks like a server with nothing on it.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.panel.models import Setting

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"

# The proxy deployment, as awgui.settings wires it when AWG_PANEL_TRUST_PROXY=1.
FORWARDED = override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))


def api_url(path: str) -> str:
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def tls(settings):
    """A panel that believes it is reached over HTTPS, however that was arranged."""
    settings.PANEL_TLS = True
    return settings


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------- reading


def test_a_page_asked_for_in_the_clear_is_not_served(tls, client):
    """The shell, not just the API: an HTTP request must not come back with the panel."""
    response = client.get(settings.BASE_PATH)

    assert response.status_code == 302
    assert response["Location"] == f"https://testserver{settings.BASE_PATH}"
    assert response.content == b""


def test_the_api_is_not_served_in_the_clear_either(tls, client):
    """health is the endpoint the login page reaches for first, so it is the one
    most likely to be probed over http - and it must not answer there."""
    response = client.get(api_url("health"))

    assert response.status_code == 302
    assert b"ok" not in response.content


def test_the_redirect_keeps_the_host_the_path_and_the_query(tls, client):
    response = client.get(f"{api_url('health')}?verbose=1", headers={"host": "panel.example.com"})

    assert response["Location"] == f"https://panel.example.com{api_url('health')}?verbose=1"


def test_the_redirect_is_temporary(tls, client):
    response = client.get(settings.BASE_PATH)

    assert response.status_code == 302, (
        "a permanent redirect is cached long past the day HTTPS goes off again, "
        "and the bare IP the panel hands back then is exactly what it would rewrite"
    )


def test_a_head_request_is_bounced_like_any_other_read(tls, client):
    assert client.head(settings.BASE_PATH).status_code == 302


def test_https_is_served_normally(tls, client):
    """The point is refusing the other scheme, not breaking this one."""
    response = client.get(api_url("health"), secure=True)

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_with_https_off_plain_http_is_served(client):
    """A panel behind an SSH tunnel, or one not set up for TLS at all, is a
    supported deployment and has to keep working."""
    response = client.get(api_url("health"))

    assert response.status_code == 200
    assert response.json()["ok"] is True


# --------------------------------------------------------------- writing


def test_a_login_in_the_clear_is_refused_rather_than_redirected(tls, client):
    """The password is already spent by the time this arrives; what must not
    happen is a session minted over cleartext on top of it.

    A redirect would drop the body on the way through, so the request would fail
    silently and look like a panel that logs nobody in. The refusal says why.
    """
    get_user_model().objects.create_user(USERNAME, password=PASSWORD)
    credentials = {"username": USERNAME, "password": PASSWORD}

    response = client.post(api_url("auth/login"), credentials, format="json")

    assert response.status_code == 400
    assert b"HTTPS" in response.content
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    # And the browser really is not signed in behind the refusal.
    assert client.get(api_url("auth/session"), secure=True).json()["authenticated"] is False
    # The same credentials over TLS do open the door, so it was the scheme that
    # turned the first one away and not the password.
    over_tls = client.post(api_url("auth/login"), credentials, format="json", secure=True)
    assert over_tls.status_code == 200, over_tls.content


def test_a_write_in_the_clear_is_refused_before_the_handler(tls, client):
    response = client.put(
        api_url("settings"), {"configEndpointHost": "attacker.example"}, format="json"
    )

    assert response.status_code == 400
    assert not Setting.objects.filter(value="attacker.example").exists()


def test_a_delete_in_the_clear_is_refused(tls, client):
    response = client.delete(api_url("clients/client1"))

    assert response.status_code == 400


# --------------------------------------------------------------- behind a proxy


def test_the_scheme_a_trusted_proxy_reports_is_believed(tls, client):
    """nginx terminating TLS is the documented deployment: gunicorn sees plain
    HTTP and the header is the only thing that says otherwise."""
    with FORWARDED:
        response = client.get(api_url("health"), headers={"x-forwarded-proto": "https"})

    assert response.status_code == 200


def test_a_proxy_that_forwards_plain_http_is_not_served(tls, client):
    """The misconfiguration this exists for: TLS believed on, scheme says no."""
    with FORWARDED:
        response = client.get(api_url("health"), headers={"x-forwarded-proto": "http"})

    assert response.status_code == 302


def test_reaching_the_socket_behind_the_proxy_is_not_served(tls, client):
    """A panel left on 0.0.0.0 with a proxy in front answers on its own port
    too, and nothing there sets the header the proxy would have set."""
    with FORWARDED:
        response = client.post(api_url("auth/login"), {"username": "a", "password": "b"})

    assert response.status_code == 400


# --------------------------------------------------------------- the base path


def test_a_path_outside_the_base_path_is_still_a_bare_404_in_the_clear(tls):
    """The scanner walking port 2097 learns nothing new. A redirect would tell
    it there is a service here to redirect, which is the one thing the empty 404
    exists to withhold - so the base-path check has to come first."""
    health = api_url("health")

    with override_settings(BASE_PATH="/p/ab12cd34/"):
        # The URL that answers here, now outside the prefix - and a fresh client,
        # because the middleware chain reads the base path once when it is built.
        response = APIClient().get(health)

    assert response.status_code == 404
    assert response.content == b""
    assert "Location" not in response.headers
