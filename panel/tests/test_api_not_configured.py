"""The panel running before, or without, an AmneziaWG installation.

This is not a hypothetical: `install-panel.sh --standalone` installs the panel
on its own, a wrong AWG_IFACE points at an interface that was never created,
and a fresh container starts with an empty /etc/amnezia. All three used to
surface as a 500 with a traceback in the journal, which reads as a panel bug
and gives the UI nothing to branch on.
"""

import pytest
from django.contrib.auth import get_user_model

from awg import store
from awg.errors import NotConfigured

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client_logged_in(client, conf_dir):
    """A logged-in session against a config directory with no server config."""
    get_user_model().objects.create_user(username="admin", password="testpass12345")
    client.force_login(get_user_model().objects.get(username="admin"))
    return client


def test_store_raises_not_configured(conf_dir):
    """The core says so precisely, rather than with a generic AwgError."""
    with pytest.raises(NotConfigured) as excinfo:
        store.read_server()
    message = str(excinfo.value)
    assert "no server config" in message
    # The message has to tell an operator what to do about it.
    assert "AWG_IFACE" in message or "not installed" in message


@pytest.mark.parametrize("path", ["clients", "server"])
def test_endpoints_return_503_not_500(admin_client_logged_in, path):
    response = admin_client_logged_in.get(f"/api/v1/{path}")
    assert response.status_code == 503, "a missing VPN install is not a server fault"
    body = response.json()
    assert body["code"] == "not_configured"
    assert "no server config" in body["detail"]


def test_status_still_answers_without_a_config(admin_client_logged_in):
    """The status page is the one screen that has to work when nothing else
    does - it is where the operator finds out what is wrong."""
    response = admin_client_logged_in.get("/api/v1/server/status")
    assert response.status_code == 200


def test_health_needs_no_config_and_no_login(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
