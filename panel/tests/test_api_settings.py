"""Panel settings, the admin account, and the backup archive.

Settings are the one part of the panel with no file on disk behind it, so the
assertions here are about the two things that can go wrong when a value lives in
a database: a key nobody defined being stored anyway, and a stored key not
coming back. Both are checked against the Setting table rather than against the
response, because a handler that echoes its input is exactly the bug in
question.

The backup test is the strict one. The archive layout is the format rather than
this module's own choice: awg-menu built it with `tar -czf out -C /etc/amnezia
amneziawg -C /var/lib awg-panel` and refused to restore anything without
`amneziawg/<iface>.conf` in it, and every archive taken off a server before
backups moved into the panel is one of those. A restore that will not read them
is a backup that fails on the day it is needed.
"""

import errno
import io
import os
import tarfile
from importlib import import_module
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.panel import defaults, settings_store
from apps.panel.models import Setting
from apps.stats import live
from awg import paths, ports, store
from awg.controller import reset_controller
from awg.paths import iface, panel_env_file

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-quite-different-passphrase"
NEW_USERNAME = "operator-2"


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    """Drop the settings cache around every test.

    settings_store holds values for a few seconds so a 2 s dashboard poll does
    not hit SQLite each time. That cache outlives the per-test database
    rollback, so without this a value written by one test would be served to the
    next as though it had been stored.
    """
    settings_store.invalidate()
    yield
    settings_store.invalidate()


@pytest.fixture(autouse=True)
def panel_wrapper(monkeypatch, tmp_path):
    """A stand-in for /usr/local/bin/awg-panel that records instead of restarting.

    Saving one of the five settings systemd reads ends in `awg-panel
    restart-deferred`. On a machine where the panel is installed - which is
    exactly where this suite is most likely to be run - the real wrapper would
    bounce the live service.

    A recording stub rather than a path that does not exist. That was safe, but
    it sent every save down the "the panel could not restart itself" branch, so
    the path an actual install takes was the one thing these tests never ran.
    """
    # Not tmp_path/awg-panel: conftest already uses that name for the stand-in
    # data directory.
    binary = tmp_path / "bin" / "awg-panel"
    binary.parent.mkdir(exist_ok=True)
    calls = binary.parent / "calls"
    binary.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n', encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("AWG_PANEL_BIN", str(binary))
    return calls


@pytest.fixture(autouse=True)
def fresh_controller():
    """One mock interface per test; the backup refreshes traffic counters through it."""
    reset_controller()
    yield
    reset_controller()


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(USERNAME, password=PASSWORD)


@pytest.fixture
def api(admin) -> APIClient:
    client = APIClient()
    client.force_login(admin)
    return client


# ----------------------------------------------------------------- settings


def test_defaults_are_served_when_nothing_is_stored(api):
    """A panel that has never been configured still answers with a complete object."""
    response = api.get(api_url("settings"))

    assert response.status_code == 200
    assert not Setting.objects.exists()
    assert response.json() == defaults.DEFAULTS


def test_a_put_persists_and_round_trips(api):
    response = api.put(
        api_url("settings"),
        {"configEndpointHost": "vpn.example.com", "theme": "dark", "trafficPollSec": "5"},
        format="json",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is False
    assert body["settings"]["configEndpointHost"] == "vpn.example.com"
    # Untouched keys come back at their defaults, so the UI can render the whole
    # form from one response.
    assert body["settings"]["language"] == defaults.DEFAULTS["language"]

    assert Setting.objects.get(key="configEndpointHost").value == "vpn.example.com"
    assert Setting.objects.get(key="trafficPollSec").value == "5"
    # Only what changed is written; the rest stays a default rather than a copy
    # of one, so a future release can change a default and have it take effect.
    assert set(Setting.objects.values_list("key", flat=True)) == {
        "configEndpointHost",
        "theme",
        "trafficPollSec",
    }

    settings_store.invalidate()
    stored = api.get(api_url("settings")).json()
    assert stored["configEndpointHost"] == "vpn.example.com"
    assert stored["theme"] == "dark"
    assert stored["trafficPollSec"] == "5"


def test_an_unknown_key_is_rejected_rather_than_silently_stored(api):
    response = api.put(
        api_url("settings"),
        {"configEndpointHost": "vpn.example.com", "favouriteColour": "green"},
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert "favouriteColour" in body["errors"]
    assert "favouriteColour" in body["errors"]["favouriteColour"]
    # The whole save is refused, so the valid half of a payload with a typo in
    # it does not go in half-applied.
    assert not Setting.objects.exists()
    stored = api.get(api_url("settings")).json()
    assert stored["configEndpointHost"] == defaults.DEFAULTS["configEndpointHost"]


def test_a_bad_value_is_reported_against_its_own_field(api):
    response = api.put(api_url("settings"), {"webPort": "not-a-port"}, format="json")

    assert response.status_code == 400
    assert "webPort" in response.json()["errors"]
    assert not Setting.objects.exists()


def test_a_certificate_the_service_cannot_see_says_why(api):
    """/root reads as empty under ProtectHome=yes, so "no such file" would be a lie.

    The file is usually right there and root can cat it; only the service cannot.
    Reporting a missing file sends the admin off to re-check a path that was
    correct, so the message has to name the hardening that is hiding it.
    """
    response = api.put(
        api_url("settings"),
        {"tlsCertPath": "/root/cert/panel.crt", "tlsKeyPath": "/root/cert/panel.key"},
        format="json",
    )

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert "ProtectHome" in errors["tlsCertPath"], errors["tlsCertPath"]
    assert "ProtectHome" in errors["tlsKeyPath"], errors["tlsKeyPath"]
    assert not Setting.objects.exists()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root walks into a 0000 directory, so there is no denial to provoke"
)
def test_a_path_the_service_cannot_walk_is_a_field_error_not_a_crash(api, tmp_path):
    """is_file() re-raises everything except "not there", including a denied parent."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        response = api.put(
            api_url("settings"), {"tlsCertPath": str(locked / "panel.crt")}, format="json"
        )
    finally:
        locked.chmod(0o700)  # or tmp_path cleanup fails

    assert response.status_code == 400, response.content
    assert "tlsCertPath" in response.json()["errors"]


def test_a_certificate_pair_that_exists_is_accepted_and_restarts_the_service(api, tmp_path):
    cert = tmp_path / "panel.crt"
    key = tmp_path / "panel.key"
    cert.write_text("-- not really a certificate --\n", encoding="utf-8")
    key.write_text("-- not really a key --\n", encoding="utf-8")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key)},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert response.json()["needsRestart"] is True
    written = panel_env_file().read_text(encoding="utf-8")
    assert f"AWG_PANEL_TLS_CERT={cert}" in written
    # gunicorn reads this rather than inferring HTTPS from the two paths.
    assert "AWG_PANEL_TLS=1" in written


def test_the_url_after_a_save_is_a_name_the_certificate_covers(api, tmp_path, make_certificate):
    """The panel is administered by IP and the certificate is for a domain.

    Handing the browser back https://<ip>/ sends it somewhere its own
    certificate cannot match, and the page navigates there on a countdown. A
    name error at that moment looks exactly like a panel that did not come back.
    """
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key)},
        format="json",
        HTTP_HOST="203.0.113.10:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"].startswith("https://panel.example.com:2097/")
    assert "203.0.113.10" not in body["url"]
    # And say so, because that name has to resolve to this box for the handover
    # to land.
    assert any("panel.example.com" in warning for warning in body["warnings"]), body["warnings"]


SECRET_PATH = "/p/ab12cd34/"


@pytest.fixture
def secret_base_path(monkeypatch):
    """Mount the panel under a random prefix, the way install-panel.sh does.

    The installer writes the generated path to /etc/awg-panel.env and stores no
    row for it, so the environment is where it has to be read from - and every
    other test in this file runs at "/", where a dropped base path is invisible.
    """
    monkeypatch.setenv("AWG_PANEL_BASE_PATH", SECRET_PATH)
    settings_store.invalidate()
    return SECRET_PATH


def test_the_url_after_a_save_carries_the_secret_base_path(
    api, tmp_path, make_certificate, secret_base_path
):
    """The panel does not answer at the root of the server, so nor may the handover.

    The page navigates to this address on a countdown and the admin gets no
    second chance at it. Dropping the prefix hands them a 404 on a panel that
    came back up perfectly, which is the one failure they cannot tell apart from
    a service that died - and the whole point of the prefix is that it is not
    something anybody remembers.
    """
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key)},
        format="json",
        HTTP_HOST="203.0.113.10:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"] == f"https://panel.example.com:2097{secret_base_path}"
    # And every line that sends the admin somewhere says the same whole address,
    # because these are what is left on screen after the overlay is dismissed.
    assert all(secret_base_path in warning for warning in body["warnings"] if "://" in warning), (
        body["warnings"]
    )
    assert any(body["url"] in warning for warning in body["warnings"]), body["warnings"]


def test_turning_tls_off_hands_back_the_whole_http_address(
    api, tmp_path, make_certificate, secret_base_path, clients_env
):
    """Coming back off HTTPS moves the browser too, and to a prefix it must keep.

    And off the name, which is the part that is not obvious. Every response the
    panel served over HTTPS carried an HSTS header, so this browser has been
    told to use HTTPS for panel.example.com for a year; handing it back
    http://panel.example.com/ hands it an address it will silently turn into an
    https:// one and fail on. The server's own address never carried that
    header, so it is the one that still answers.
    """
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")
    settings_store.set_many({"tlsCertPath": str(cert), "tlsKeyPath": str(key)})

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": "", "tlsKeyPath": ""},
        format="json",
        HTTP_HOST="panel.example.com:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"] == f"http://203.0.113.10:2097{secret_base_path}"
    assert any(body["url"] in warning for warning in body["warnings"]), body["warnings"]
    # And why it is not the address they are looking at, so a bookmark that
    # stops working is not mistaken for a panel that did not come back.
    assert any("panel.example.com" in warning for warning in body["warnings"]), body["warnings"]


def test_turning_tls_off_keeps_a_name_when_there_is_no_address_to_offer(
    api, tmp_path, make_certificate
):
    """With the endpoint left to auto-detect there is nothing better to send them to.

    Guessing is worse than staying put: the address is only worth moving to
    because it is known to reach the box, and the panel is not going to spend
    the metadata service's timeout inside a save that is about to restart it. So
    the name stands, and the warning says what the browser will do with it.
    """
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")
    settings_store.set_many({"tlsCertPath": str(cert), "tlsKeyPath": str(key)})

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": "", "tlsKeyPath": ""},
        format="json",
        HTTP_HOST="panel.example.com:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"].startswith("http://panel.example.com:2097/")
    assert any("HSTS" in warning for warning in body["warnings"]), body["warnings"]


def test_turning_tls_off_at_an_address_leaves_the_address_alone(api, tmp_path, make_certificate):
    """No browser pins an IP, so the one in the URL bar goes on working."""
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")
    settings_store.set_many({"tlsCertPath": str(cert), "tlsKeyPath": str(key)})

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": "", "tlsKeyPath": ""},
        format="json",
        HTTP_HOST="198.51.100.4:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"].startswith("http://198.51.100.4:2097/")
    assert not any("HSTS" in warning for warning in body["warnings"]), body["warnings"]


def test_a_pinned_listen_address_outranks_the_server_address_too(
    api, tmp_path, make_certificate, clients_env
):
    """It is where the service will be, and the only warning it needs is its own."""
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")
    settings_store.set_many({"tlsCertPath": str(cert), "tlsKeyPath": str(key)})

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": "", "tlsKeyPath": "", "webListen": "10.0.0.5"},
        format="json",
        HTTP_HOST="panel.example.com:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"].startswith("http://10.0.0.5:2097/")
    assert not any("HTTPS for" in warning for warning in body["warnings"]), body["warnings"]


def test_a_name_the_panel_never_served_over_https_is_kept(api, clients_env):
    """Only a certificate of the panel's own could have pinned anything.

    A panel behind a reverse proxy answers plain HTTP on a name the proxy owns,
    with both paths empty the whole time. Moving that admin to the server's IP
    on a save that changed neither would take them off the only address the
    proxy forwards.
    """
    response = api.put(
        api_url("settings"),
        {"webPort": "9443"},
        format="json",
        HTTP_HOST="panel.example.com:2097",
    )

    assert response.status_code == 200, response.content
    assert response.json()["url"].startswith("http://panel.example.com:9443/")


def test_a_new_secret_path_is_quoted_in_full_in_the_warning(api, secret_base_path):
    """The one save that signs the admin out is the one that must spell out where to sign in."""
    response = api.put(api_url("settings"), {"webBasePath": "/p/99887766/"}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"].endswith("/p/99887766/")
    assert any(body["url"] in warning for warning in body["warnings"]), body["warnings"]


def test_the_host_in_the_request_is_kept_when_the_certificate_covers_it(
    api, tmp_path, make_certificate
):
    cert, key, _ = make_certificate(tmp_path, "panel.example.com", "*.vpn.example.com")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key)},
        format="json",
        HTTP_HOST="box.vpn.example.com:2097",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    # The wildcard covers it, so there is nothing to correct and no warning to give.
    assert body["url"].startswith("https://box.vpn.example.com:2097/")
    assert not any("certificate" in warning for warning in body["warnings"]), body["warnings"]


def test_a_pinned_listen_address_outranks_the_name_on_the_certificate(
    api, tmp_path, make_certificate
):
    """That address is where the service will be; a name it does not answer on is a dead end."""
    cert, key, _ = make_certificate(tmp_path, "panel.example.com")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key), "webListen": "10.0.0.5"},
        format="json",
        HTTP_HOST="203.0.113.10:2097",
    )

    assert response.status_code == 200, response.content
    assert response.json()["url"].startswith("https://10.0.0.5:2097/")


def test_a_key_from_a_different_certificate_is_refused(api, tmp_path, make_certificate):
    """gunicorn builds the SSL context on the restart this save schedules."""
    cert, _, _ = make_certificate(tmp_path, "panel.example.com")
    _, stranger_key, _ = make_certificate(tmp_path, "other.example.com")

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(stranger_key)},
        format="json",
    )

    assert response.status_code == 400, response.content
    assert "tlsKeyPath" in response.json()["errors"]
    assert not Setting.objects.exists()


def test_a_matching_pair_is_accepted(api, tmp_path, make_certificate):
    cert, key, material = make_certificate(tmp_path, "panel.example.com")
    # The same key, written out again elsewhere: it is the key material that has
    # to match, not the path it was saved to.
    elsewhere, _, _ = make_certificate(tmp_path / "sub", "panel.example.com", key_of=material)

    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(elsewhere), "tlsKeyPath": str(key)},
        format="json",
    )

    assert response.status_code == 200, response.content


def test_half_a_certificate_pair_is_refused_on_the_field_still_empty(api, tmp_path):
    """gunicorn will not start on a cert with no key, and the restart is automatic."""
    cert = tmp_path / "panel.crt"
    cert.write_text("-- not really a certificate --\n", encoding="utf-8")

    response = api.put(api_url("settings"), {"tlsCertPath": str(cert)}, format="json")

    assert response.status_code == 400
    assert "tlsKeyPath" in response.json()["errors"]
    assert not Setting.objects.exists()


# --------------------------------------- the address handed out in client configs


def enable_tls(api, cert, key):
    """Save a certificate pair the way the TLS tab does, and assert it took."""
    response = api.put(
        api_url("settings"),
        {"tlsCertPath": str(cert), "tlsKeyPath": str(key)},
        format="json",
    )
    assert response.status_code == 200, response.content


def test_the_certificate_route_lists_what_the_file_covers(api, tmp_path, make_certificate):
    """The Settings page offers these as the address to write into client configs.

    Read off the file rather than out of the settings, because a renewal can
    change the names without anything in the panel being saved.
    """
    cert, key, _ = make_certificate(tmp_path, "vpn.example.com", "*.example.com")
    enable_tls(api, cert, key)

    body = api.get(api_url("settings/certificate")).json()

    # Wildcards sort last: a name a browser matches is never the one to send
    # somebody to, and that ordering is what the picker in Settings renders.
    assert body["names"] == ["vpn.example.com", "*.example.com"]
    assert body["domain"] == "vpn.example.com"
    assert body["path"] == str(cert)


def test_the_certificate_route_offers_no_domain_from_an_address_certificate(
    api, tmp_path, make_certificate
):
    """The address is reported, because HTTPS is served on it and it can expire.

    It is not reported as a domain to hand out, though. Client configs already
    carry that address, so a page that offered it as the name to swap in would
    be asking which of two identical answers to write into the file.
    """
    cert, key, _ = make_certificate(tmp_path, "198.51.100.7")
    enable_tls(api, cert, key)

    body = api.get(api_url("settings/certificate")).json()

    assert body["names"] == ["198.51.100.7"]
    assert body["domain"] == ""


def test_the_certificate_route_answers_when_there_is_no_certificate(api):
    body = api.get(api_url("settings/certificate")).json()
    assert body == {
        "path": "",
        "names": [],
        "domain": "",
        # Not zero: there is no certificate, so there is no date to count from,
        # and a zero here would read on the page as one expiring today.
        "expiresInDays": None,
        "problem": "",
        "keyPath": "",
        "keyProblem": "",
    }


def test_a_certificate_can_be_read_before_it_is_saved(api, tmp_path, make_certificate):
    """The one question the Settings page has to answer before the save, not after.

    Turning HTTPS on moves the browser to the name inside the certificate, and
    the page says where it will reconnect while the path is still only typed. If
    the only readable certificate were the stored one, that line could offer
    nothing better than the IP in the URL bar - which is the address the
    handover is about to stop using.
    """
    cert, _, _ = make_certificate(tmp_path, "new.example.com")

    body = api.get(api_url("settings/certificate"), {"path": str(cert)}).json()

    assert body["path"] == str(cert)
    assert body["names"] == ["new.example.com"]
    assert body["domain"] == "new.example.com"
    assert body["problem"] == ""


def test_asking_about_one_certificate_never_answers_about_another(api, tmp_path, make_certificate):
    """The path comes back with the answer, because a stale one is worse than none.

    A reply about the certificate being replaced would name a domain with every
    appearance of naming the new one's, and the page navigates to it.
    """
    old, key, _ = make_certificate(tmp_path, "old.example.com")
    enable_tls(api, old, key)
    new, _, _ = make_certificate(tmp_path / "new", "new.example.com")

    body = api.get(api_url("settings/certificate"), {"path": str(new)}).json()

    assert body["path"] == str(new)
    assert body["domain"] == "new.example.com"


def test_a_path_that_is_not_one_has_no_names_and_says_why(api):
    """Half-typed and relative paths arrive on every keystroke, and none is read.

    gunicorn is handed absolute paths or nothing, and a relative one would be
    opened against the service's working directory - a file nobody asked about.
    So there is nothing to say about what it covers, and the reason it could not
    be used goes where the page can put it under the box.
    """
    body = api.get(api_url("settings/certificate"), {"path": "etc/ssl/panel.crt"}).json()

    assert body["path"] == "etc/ssl/panel.crt"
    assert body["names"] == []
    assert body["domain"] == ""
    assert "absolute" in body["problem"], body["problem"]


def test_a_certificate_that_is_not_there_says_so_before_the_save(api):
    """The save is what the page is trying not to have to reach.

    Turning HTTPS on fills the two boxes with a suggested pair of paths, and on
    most machines neither file exists. Finding that out from the save means
    finding it out after agreeing to restart the web service for it, so the same
    verdict has to be available while the path is only typed - in the same
    words, because it is the same check.
    """
    body = api.get(
        api_url("settings/certificate"),
        {"path": "/etc/ssl/certs/awg-panel.crt", "key": "/etc/ssl/private/awg-panel.key"},
    ).json()

    assert body["problem"] == "There is no file at /etc/ssl/certs/awg-panel.crt."
    assert body["keyPath"] == "/etc/ssl/private/awg-panel.key"
    # Not asserted word for word: /etc/ssl/private is root-only on Debian, so a
    # test user is told it cannot get in there rather than that the file is
    # missing. Which of the two it is comes from the same check as the save,
    # which is what the rest of this asserts.
    assert body["keyProblem"]

    # The same words the save would have used, so the page is not maintaining a
    # second wording that can drift away from the one that refuses the save.
    refused = api.put(
        api_url("settings"),
        {
            "tlsCertPath": "/etc/ssl/certs/awg-panel.crt",
            "tlsKeyPath": "/etc/ssl/private/awg-panel.key",
        },
        format="json",
    )
    assert refused.status_code == 400
    assert refused.json()["errors"]["tlsCertPath"] == body["problem"]
    assert refused.json()["errors"]["tlsKeyPath"] == body["keyProblem"]


def test_a_certificate_the_service_cannot_see_says_why_before_the_save_too(api):
    """ProtectHome=yes empties /root for the service, and the save says exactly that."""
    body = api.get(
        api_url("settings/certificate"), {"path": "/root/panel.crt", "key": "/root/panel.key"}
    ).json()

    assert "ProtectHome" in body["problem"], body["problem"]
    assert "ProtectHome" in body["keyProblem"], body["keyProblem"]


def test_a_key_from_another_certificate_is_reported_before_the_save(
    api, tmp_path, make_certificate
):
    """Two files that both exist and are not a pair: gunicorn would refuse to start.

    Nothing that only stats the two paths notices, so this is the one refusal an
    admin can walk into with both boxes looking right. It is worth as much
    before the confirmation as after it.
    """
    cert, _, _ = make_certificate(tmp_path, "panel.example.com")
    _, stranger, _ = make_certificate(tmp_path / "other", "other.example.com")

    body = api.get(
        api_url("settings/certificate"), {"path": str(cert), "key": str(stranger)}
    ).json()

    assert body["problem"] == ""
    assert "does not belong" in body["keyProblem"], body["keyProblem"]
    # The certificate itself is fine, so what it covers is still worth reading.
    assert body["names"] == ["panel.example.com"]


def test_half_a_pair_is_not_a_fault_the_certificate_route_reports(api, tmp_path, make_certificate):
    """An empty box is a form half filled in, and the page has its own word for it.

    Reporting it here would put an error under the certificate the moment the
    first character of the key path is typed, which is every save of a TLS pair
    on its way through.
    """
    cert, _, _ = make_certificate(tmp_path, "panel.example.com")

    body = api.get(api_url("settings/certificate"), {"path": str(cert)}).json()

    assert body["problem"] == ""
    assert body["keyProblem"] == ""


def test_choosing_the_domain_stores_it_and_restarts_nothing(api, tmp_path, make_certificate):
    """Nothing about this reaches /etc/awg-panel.env, so the panel stays where it is."""
    cert, key, _ = make_certificate(tmp_path, "vpn.example.com")
    enable_tls(api, cert, key)

    response = api.put(api_url("settings"), {"configEndpointMode": "domain"}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["needsRestart"] is False
    assert Setting.objects.get(key="configEndpointMode").value == "domain"
    # And warns about nothing: the save did exactly what it was asked to do, and
    # the page names the address it will hand out as it confirms it. A warning
    # here is a banner across the top of the page on every single save of this
    # setting, which is how a page teaches people to dismiss warnings unread.
    assert response.json()["warnings"] == []


def test_asking_for_a_domain_with_no_certificate_is_refused(api):
    """Accepting it would leave the panel silently handing out the IP it was told not to."""
    response = api.put(api_url("settings"), {"configEndpointMode": "domain"}, format="json")

    assert response.status_code == 400, response.content
    assert "configEndpointHost" in response.json()["errors"]
    assert not Setting.objects.filter(key="configEndpointMode").exists()


def test_a_domain_typed_by_hand_needs_no_certificate(api):
    """The tunnel's name and the panel's are not always the same name."""
    response = api.put(
        api_url("settings"),
        {"configEndpointMode": "domain", "configEndpointHost": "gateway.example.com"},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert Setting.objects.get(key="configEndpointHost").value == "gateway.example.com"


def test_something_that_is_not_a_domain_is_refused(api):
    response = api.put(
        api_url("settings"),
        {"configEndpointMode": "domain", "configEndpointHost": "https://vpn.example.com/"},
        format="json",
    )

    assert response.status_code == 400
    assert "configEndpointHost" in response.json()["errors"]


def test_turning_tls_off_is_not_blocked_by_the_domain_setting(api, tmp_path, make_certificate):
    """The choice is checked when it is made, not held against every later save.

    Otherwise an admin moving the panel behind a reverse proxy would be refused
    by a setting about client configs, with no obvious way to see the connection.
    """
    cert, key, _ = make_certificate(tmp_path, "vpn.example.com")
    enable_tls(api, cert, key)
    api.put(api_url("settings"), {"configEndpointMode": "domain"}, format="json")

    response = api.put(api_url("settings"), {"tlsCertPath": "", "tlsKeyPath": ""}, format="json")

    assert response.status_code == 200, response.content
    # And say what it did to the configs, because nothing else on the page will.
    assert any("server config" in warning for warning in response.json()["warnings"]), (
        response.json()["warnings"]
    )


def read_only_directory(monkeypatch):
    """Make the atomic write fail exactly as a live install does.

    ProtectSystem=full leaves /etc read-only and the unit grants the panel
    /etc/awg-panel.env itself, not the directory. So creating the temp file an
    atomic replace needs fails with EROFS while the target file is perfectly
    writable - which is why this is faked at that seam rather than by chmod.
    """
    real = paths.atomic_write

    def refuse(path, *args, **kwargs):
        if Path(path) == panel_env_file():
            raise OSError(errno.EROFS, "Read-only file system", f"/etc/.{Path(path).name}.tmp")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(paths, "atomic_write", refuse)
    monkeypatch.setattr(settings_store, "atomic_write", refuse)


def test_the_env_file_is_written_even_when_its_directory_refuses_a_temp_file(api, monkeypatch):
    """The panel is given the file, not the directory it sits in."""
    panel_env_file().write_text("AWG_PANEL_PORT=2097\nAWG_PANEL_TLS=0\n", encoding="utf-8")
    read_only_directory(monkeypatch)

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    written = panel_env_file().read_text(encoding="utf-8")
    assert "AWG_PANEL_PORT=9443" in written
    assert "AWG_PANEL_PORT=2097" not in written
    # And it is a real save, not a degraded one: the restart is on and the page
    # is told where to reconnect.
    assert response.json()["needsRestart"] is True


def test_a_web_port_something_else_holds_is_refused(api, monkeypatch):
    """The one setting here that is refused rather than warned about.

    A port change restarts the web service and the page follows it to the new
    address; a panel that cannot bind there is one nobody can reach from a
    browser at all, so the save has to fail while there is still a panel to
    fail it in."""
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": True)
    monkeypatch.setattr(ports, "holder", lambda port, proto="udp": "nginx")

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 400, response.content
    error = response.json()["errors"]["webPort"]
    assert "nginx" in error and "9443" in error, error
    # Refused means refused: nothing stored, nothing restarted.
    assert not Setting.objects.filter(key="webPort").exists()


def test_a_free_web_port_is_saved(api, monkeypatch):
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": False)

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    assert Setting.objects.get(key="webPort").value == "9443"


def test_the_port_the_panel_is_already_on_is_never_probed(api, monkeypatch):
    """It is held by this panel. Reporting that would be reporting that the
    panel is running, in the words of something being in the way."""
    asked: list[int] = []
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": asked.append(port) or True)

    response = api.put(api_url("settings"), {"webPort": "2097", "theme": "dark"}, format="json")

    assert response.status_code == 200, response.content
    assert asked == []


def test_a_change_that_could_not_be_applied_does_not_move_the_browser(api, monkeypatch):
    """The overlay navigates on needsRestart and gets no second chance.

    When the environment file cannot be written the service is never restarted,
    so it goes on serving the old address. Announcing the new one sends the
    admin somewhere nothing is listening, which is exactly how a panel is lost.
    """
    monkeypatch.setattr(settings_store, "write_panel_env", lambda values: False)

    response = api.put(
        api_url("settings"), {"webPort": "9443"}, format="json", HTTP_HOST="panel.example.com:2097"
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is False
    assert ":9443" not in body["url"]
    assert body["url"].startswith("http://panel.example.com:2097/")
    assert body["warnings"], "a save that did not take has to say so"
    # The value is still stored: the admin fixes the file by hand and restarts,
    # and the panel has to agree with what they put there.
    assert Setting.objects.get(key="webPort").value == "9443"


def test_a_save_keeps_the_secret_base_path_the_installer_generated(api, monkeypatch):
    """That path lives in the env file and nowhere else.

    install-panel.sh generates it, writes it there, and stores no row for it. So
    a settings map built from the code's defaults says "/" for it, and the env
    file is rendered from that map: one unrelated save would move the whole
    panel to the root of the server - off the secret path, onto every scanner's
    list - without anyone having touched the field.
    """
    monkeypatch.setenv("AWG_PANEL_BASE_PATH", "/p/79c2c772/")
    settings_store.invalidate()

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    written = panel_env_file().read_text(encoding="utf-8")
    assert "AWG_PANEL_BASE_PATH=/p/79c2c772/" in written
    # And the form shows what the service is really serving, not the default it
    # would have been reset to.
    assert response.json()["settings"]["webBasePath"] == "/p/79c2c772/"


def test_an_edited_base_path_still_wins_over_the_running_one(api, monkeypatch):
    """The environment is the fallback for an unedited setting, not an override."""
    monkeypatch.setenv("AWG_PANEL_BASE_PATH", "/p/79c2c772/")
    settings_store.invalidate()

    response = api.put(api_url("settings"), {"webBasePath": "/p/newpath/"}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["settings"]["webBasePath"] == "/p/newpath/"
    assert "AWG_PANEL_BASE_PATH=/p/newpath/" in panel_env_file().read_text(encoding="utf-8")


def test_saving_the_same_value_repairs_an_env_file_that_drifted(api, panel_wrapper):
    """The form already shows the right answer; pressing Save has to make it true.

    The database keeps the setting even when the write of the env file fails, so
    the two disagree and re-entering the same value changes no row. Comparing
    against the file instead of the row is what lets a save fix it.
    """
    api.put(api_url("settings"), {"webPort": "9443"}, format="json")
    # As if someone had edited it back by hand to reach a panel they had lost.
    panel_env_file().write_text("AWG_PANEL_PORT=2097\n", encoding="utf-8")

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["needsRestart"] is True
    assert "AWG_PANEL_PORT=9443" in panel_env_file().read_text(encoding="utf-8")
    assert panel_wrapper.read_text(encoding="utf-8").strip().endswith("restart-deferred")


def test_a_save_that_touches_nothing_systemd_reads_leaves_the_service_alone(api, panel_wrapper):
    """The comparison is only worth making about the settings that were sent."""
    panel_env_file().write_text("AWG_PANEL_PORT=1234\n", encoding="utf-8")

    response = api.put(api_url("settings"), {"theme": "dark"}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["needsRestart"] is False
    # A theme is no reason to bounce the web service, however stale the file is.
    assert not panel_wrapper.exists()
    assert panel_env_file().read_text(encoding="utf-8") == "AWG_PANEL_PORT=1234\n"


def test_a_panel_that_cannot_restart_itself_does_not_pretend_it_did(api, monkeypatch, tmp_path):
    """A dev checkout or a container: the env file is written, nothing restarts.

    The change applies at the next start, so it is saved and reported - but the
    page must stay where it is, because that is where the service still is.
    """
    monkeypatch.setenv("AWG_PANEL_BIN", str(tmp_path / "no-such-awg-panel"))

    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is False
    assert ":9443" not in body["url"]
    assert any("restart" in warning for warning in body["warnings"]), body["warnings"]
    # Written all the same: the next start is what picks it up.
    assert "AWG_PANEL_PORT=9443" in panel_env_file().read_text(encoding="utf-8")


def test_a_web_setting_needs_a_restart_and_reaches_the_env_file(api, panel_wrapper):
    """The five settings systemd reads are decided at startup, so saving one is not enough."""
    response = api.put(api_url("settings"), {"webPort": "9443"}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is True
    assert ":9443" in body["url"]
    # Something has to be said: the port has to be opened in the firewall, and
    # nothing else will say it.
    assert body["warnings"]
    # And the restart is really scheduled, deferred so this response is written
    # before the socket goes.
    assert panel_wrapper.read_text(encoding="utf-8").strip() == "restart-deferred"

    written = panel_env_file().read_text(encoding="utf-8")
    assert "AWG_PANEL_PORT=9443" in written
    assert f"AWG_PANEL_BASE_PATH={defaults.DEFAULTS['webBasePath']}" in written
    # Unquoted, as install-panel.sh writes them: its env_get is a bare sed and
    # would hand the quotes straight back into the next env_put.
    assert 'AWG_PANEL_PORT="9443"' not in written


# ------------------------------------------------------------------ account


def test_changing_the_password_requires_the_current_one(api, admin):
    wrong = api.post(
        api_url("settings/account"),
        {"current": "not-the-password", "new": NEW_PASSWORD},
        format="json",
    )

    assert wrong.status_code == 400
    assert "current" in wrong.json()["errors"]
    admin.refresh_from_db()
    assert admin.check_password(PASSWORD)

    changed = api.post(
        api_url("settings/account"), {"current": PASSWORD, "new": NEW_PASSWORD}, format="json"
    )
    assert changed.status_code == 200
    admin.refresh_from_db()
    assert admin.check_password(NEW_PASSWORD)
    # The browser that made the change stays signed in; every other session is
    # dropped, which is the point of changing a password after losing a laptop.
    assert api.get(api_url("settings")).status_code == 200


def test_any_password_the_owner_picks_is_accepted(api, admin):
    """No policy stands between the admin and the password they chose.

    The four cases are the four validators this panel used to carry - too short,
    a common word, all digits, and the account's own name - so a length floor or
    a dictionary put back into AUTH_PASSWORD_VALIDATORS fails here rather than
    surprising somebody at the settings page. Each one is checked by signing in
    with it, not only by reading the hash back: a password the panel stores but
    will not accept at the login form would be worse than a refusal.
    """
    current = PASSWORD
    for chosen in ("x", "password", "12345678", USERNAME):
        answer = api.post(
            api_url("settings/account"), {"current": current, "new": chosen}, format="json"
        )

        assert answer.status_code == 200, answer.json()
        admin.refresh_from_db()
        assert admin.check_password(chosen)

        fresh = APIClient()
        signed_in = fresh.post(
            api_url("auth/login"), {"username": USERNAME, "password": chosen}, format="json"
        )
        assert signed_in.status_code == 200, chosen

        current = chosen


def test_the_new_password_is_the_one_that_signs_in(api, admin):
    assert (
        api.post(
            api_url("settings/account"),
            {"current": PASSWORD, "new": NEW_PASSWORD},
            format="json",
        ).status_code
        == 200
    )

    fresh = APIClient()
    old = fresh.post(
        api_url("auth/login"), {"username": USERNAME, "password": PASSWORD}, format="json"
    )
    assert old.status_code == 401

    new = fresh.post(
        api_url("auth/login"), {"username": USERNAME, "password": NEW_PASSWORD}, format="json"
    )
    assert new.status_code == 200, new.content
    assert new.json()["authenticated"] is True


def test_the_account_can_be_renamed_and_signs_in_under_the_new_name(api, admin):
    renamed = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": NEW_USERNAME},
        format="json",
    )

    assert renamed.status_code == 200, renamed.content
    # The answer is the bootstrap payload, so the SPA has the new name without
    # asking for it again.
    assert renamed.json()["username"] == NEW_USERNAME
    admin.refresh_from_db()
    assert admin.get_username() == NEW_USERNAME
    # The password was not touched by a rename, and the old name no longer
    # names anything.
    assert admin.check_password(PASSWORD)

    fresh = APIClient()
    gone = fresh.post(
        api_url("auth/login"), {"username": USERNAME, "password": PASSWORD}, format="json"
    )
    assert gone.status_code == 401

    now = fresh.post(
        api_url("auth/login"), {"username": NEW_USERNAME, "password": PASSWORD}, format="json"
    )
    assert now.status_code == 200, now.content
    assert now.json()["username"] == NEW_USERNAME


def test_a_rename_and_a_new_password_can_travel_together(api, admin):
    both = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": NEW_USERNAME, "new": NEW_PASSWORD},
        format="json",
    )

    assert both.status_code == 200, both.content
    admin.refresh_from_db()
    assert admin.get_username() == NEW_USERNAME
    assert admin.check_password(NEW_PASSWORD)
    # The session that made the change survives both halves of it.
    assert api.get(api_url("settings")).status_code == 200


def test_a_refused_rename_leaves_the_password_alone(api, admin):
    """One request, so neither half may land while the other is refused."""
    get_user_model().objects.create_user("operator", password=NEW_PASSWORD)

    refused = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": "operator", "new": NEW_PASSWORD},
        format="json",
    )

    assert refused.status_code == 400
    admin.refresh_from_db()
    assert admin.get_username() == USERNAME
    assert admin.check_password(PASSWORD)


@override_settings(
    AUTH_PASSWORD_VALIDATORS=[
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 10},
        }
    ]
)
def test_validators_added_to_the_settings_are_enforced_again(api, admin):
    """This panel ships AUTH_PASSWORD_VALIDATORS empty, on the grounds that the
    single account belongs to whoever runs the server. A deployment that wants a
    floor back should get one by editing that setting and nothing else, so the
    route is checked here against a validator it does not normally have.

    The rename in the same request is refused with it, which is the older reason
    this test exists: the password is validated after the new name is on the
    instance but before anything is written, so a refusal leaves both halves of
    the credential as they were.
    """
    refused = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": NEW_USERNAME, "new": "short"},
        format="json",
    )

    assert refused.status_code == 400
    assert refused.json()["detail"]
    admin.refresh_from_db()
    assert admin.get_username() == USERNAME
    assert admin.check_password(PASSWORD)


def test_a_rename_is_refused_when_another_account_holds_the_name(api, admin):
    get_user_model().objects.create_user("operator", password=NEW_PASSWORD)

    for attempt in ("operator", "OPERATOR"):
        taken = api.post(
            api_url("settings/account"),
            {"current": PASSWORD, "username": attempt},
            format="json",
        )
        assert taken.status_code == 400, taken.content
        assert "username" in taken.json()["errors"]

    admin.refresh_from_db()
    assert admin.get_username() == USERNAME


def test_a_name_that_could_not_be_typed_back_is_refused(api, admin):
    for attempt in (".hidden", "with space", 'quote"name', ""):
        bad = api.post(
            api_url("settings/account"),
            {"current": PASSWORD, "username": attempt},
            format="json",
        )
        assert bad.status_code == 400, attempt
    admin.refresh_from_db()
    assert admin.get_username() == USERNAME


def test_a_request_that_changes_neither_half_is_refused(api, admin):
    nothing = api.post(api_url("settings/account"), {"current": PASSWORD}, format="json")

    assert nothing.status_code == 400
    assert nothing.json()["detail"]


# ------------------------------------------------------------------- backup


def test_backup_is_the_archive_awg_menu_restores(api, server_conf, data_dir):
    """Two top-level directories, named after the real ones, whatever the paths are here."""
    (data_dir / "master.key").write_text("stand-in", encoding="utf-8")

    response = api.get(api_url("backup"))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/gzip"
    assert "attachment" in response["Content-Disposition"]
    assert ".tar.gz" in response["Content-Disposition"]

    payload = b"".join(response.streaming_content)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = archive.getnames()

    # The restore looks for exactly this member before it will unpack anything,
    # which is the check awg-menu's restore made of the same archives.
    assert f"amneziawg/{iface()}.conf" in names
    assert "amneziawg/clients.env" in names
    assert "awg-panel/master.key" in names
    # Nothing outside the two directories the TUI knows how to put back.
    assert {name.split("/")[0] for name in names} == {"amneziawg", "awg-panel"}
    # The lock file and the atomic-write leftovers are machine-local state, and
    # a restored .lock would be a lock held by a process that no longer exists.
    assert not [name for name in names if name.rsplit("/", 1)[-1].startswith(".")]


def test_backup_leaves_the_live_blob_out(api, server_conf, data_dir):
    """It lives in the run dir now, and a restored one would be a lie about the past.

    Nothing filters it: the archive is the two directories, and the run dir is
    not one of them. Worth a test anyway, because the blob was in the data dir
    for as long as the backup format has existed, and moving it back would put
    a stale set of rates and online flags into every archive again.
    """
    live.write_live({**live.empty_blob(), "ts": 1_700_000_000})

    response = api.get(api_url("backup"))

    assert response.status_code == 200
    payload = b"".join(response.streaming_content)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = archive.getnames()

    assert not [name for name in names if name.rsplit("/", 1)[-1] == "live.json"]


def test_restore_swaps_each_file_in_rather_than_truncating_it(api, server_conf, data_dir):
    """Every panel worker re-reads awg0.conf per request and awg-quick reads it
    at boot, neither of them under the lock a restore holds. A copy that
    truncates the target and refills it can be read halfway through - and half a
    server config is a config with half the clients in it."""
    payload = b"".join(api.get(api_url("backup")).streaming_content)
    before = server_conf.stat().st_ino
    marker = "# Client = only-after-the-backup"
    server_conf.write_text(f"{server_conf.read_text(encoding='utf-8')}\n{marker}\n", "utf-8")

    upload = io.BytesIO(payload)
    upload.name = "awg-backup.tar.gz"
    response = api.post(api_url("restore"), {"file": upload}, format="multipart")

    assert response.status_code == 200, response.content
    text = server_conf.read_text(encoding="utf-8")
    assert marker not in text
    # A fresh inode is the observable half of "published by rename": the bytes
    # a concurrent reader had open were never touched.
    assert server_conf.stat().st_ino != before
    assert server_conf.stat().st_mode & 0o777 == 0o600
    # And nothing of the swap is left lying beside the config it replaced.
    assert not [entry.name for entry in server_conf.parent.iterdir() if entry.name.endswith(".tmp")]


# ------------------------------------------------------------------ restore


def _restore(api, payload: bytes):
    upload = io.BytesIO(payload)
    upload.name = "awg-backup.tar.gz"
    return api.post(api_url("restore"), {"file": upload}, format="multipart")


def _env_file(**values: str) -> None:
    """Write the env file the running service would have been started from."""
    written = {
        "AWG_PANEL_LISTEN": "0.0.0.0",
        "AWG_PANEL_PORT": "2097",
        "AWG_PANEL_BASE_PATH": "/",
        "AWG_PANEL_TLS": "0",
        "AWG_PANEL_TLS_CERT": "",
        "AWG_PANEL_TLS_KEY": "",
    }
    written.update(values)
    panel_env_file().write_text(
        "".join(f"{name}={value}\n" for name, value in written.items()), encoding="utf-8"
    )


def test_restore_leaves_the_panel_at_the_address_it_is_answering_on(api, server_conf, data_dir):
    """A restored database must not be able to tell the Settings page a lie.

    The five settings systemd reads live in two places and a restore replaces
    only one of them: the database comes out of the archive, /etc/awg-panel.env
    does not. Turn HTTPS on, take a backup, turn it off, restore it, and the
    panel reported HTTPS on while gunicorn had no certificate loaded and was
    serving plain HTTP - an address that does not exist, offered as the one to
    use.

    The running service wins rather than the archive, because it is the only one
    of the two that can be checked. The certificate and key are files on disk
    that no backup contains, so a restore that turned TLS back on would be
    naming two paths that may not be there, on a service that will not start
    without them.
    """
    _env_file()  # plain HTTP on 2097 at the root, which is what is running
    # What the archive's database says, which is where the panel used to be.
    Setting.objects.create(key="tlsCertPath", value="/etc/ssl/panel.crt")
    Setting.objects.create(key="tlsKeyPath", value="/etc/ssl/panel.key")
    Setting.objects.create(key="webPort", value="9443")
    settings_store.invalidate()

    response = _restore(api, b"".join(api.get(api_url("backup")).streaming_content))

    assert response.status_code == 200, response.content
    settings_store.invalidate()
    served = api.get(api_url("settings")).json()
    assert served["tlsCertPath"] == ""
    assert served["tlsKeyPath"] == ""
    assert served["webPort"] == "2097"
    # And it says so, rather than quietly dropping part of what was restored.
    detail = response.json()["detail"]
    assert "TLS certificate and port" in detail
    assert "left at the address it is answering on" in detail


def test_restore_restarts_the_service_holding_the_database_it_replaced(
    api, server_conf, data_dir, panel_wrapper
):
    """Every worker keeps a handle on the inode atomic_copy replaced.

    Left running they answer out of the pre-restore database and write into a
    file that no longer has a name, so the restore looks as though it did
    nothing and everything done after it is lost on the next real restart. The
    advice to run `awg-panel restart` was already in the response and nothing
    showed it - and repairing a half-applied restore by hand is not a job to
    hand to the admin in the first place.
    """
    _env_file()

    response = _restore(api, b"".join(api.get(api_url("backup")).streaming_content))

    assert response.status_code == 200, response.content
    assert response.json()["restarting"] is True
    assert "restart-deferred" in panel_wrapper.read_text(encoding="utf-8")


def test_a_conf_only_archive_restarts_nothing(api, server_conf, data_dir, panel_wrapper):
    """awg-menu wrote one of these on a server where the panel was not installed.

    It replaces no database, so there is nothing for a restart to pick up and
    bouncing the panel would only look like the restore broke it.
    """
    _env_file()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(server_conf.parent, arcname="amneziawg")

    response = _restore(api, buffer.getvalue())

    assert response.status_code == 200, response.content
    assert response.json()["restarting"] is False
    assert not panel_wrapper.exists()


def test_restore_drops_client_configs_the_backup_does_not_have(
    api, server_conf, clients_env, data_dir
):
    """A peer added after the backup loses its entry but kept its private key.

    _install_tree overlays rather than replaces, which is what keeps a
    half-finished restore recoverable. The cost was a clients/<name>.conf with
    nothing behind it: the server holds no peer for that key any more, so the
    file cannot connect anybody, and what is left is a live private key nobody
    is tracking, under a name the next `POST /clients` would either refuse or
    write over.
    """
    payload = b"".join(api.get(api_url("backup")).streaming_content)
    store.add_client("laptop")
    orphan = paths.client_dir() / "laptop.conf"
    assert orphan.is_file()

    response = _restore(api, payload)

    assert response.status_code == 200, response.content
    assert "laptop" not in [view.name for view in store.list_clients()]
    assert not orphan.exists()
    assert "laptop" in response.json()["detail"]


def test_restore_keeps_client_configs_the_backup_does_have(api, server_conf, clients_env, data_dir):
    """The other half of that rule, and the more expensive one to get wrong.

    The pruning decides what to delete by reading the restored server config, so
    a mistake in it takes out exactly the client configs a restore exists to
    bring back.
    """
    store.add_client("phone")
    payload = b"".join(api.get(api_url("backup")).streaming_content)
    kept = paths.client_dir() / "phone.conf"
    assert kept.is_file()
    kept.unlink()

    assert _restore(api, payload).status_code == 200, "restore failed"

    assert kept.is_file()
    assert "phone" in [view.name for view in store.list_clients()]


def test_restore_refuses_an_archive_whose_database_will_not_open(
    api, server_conf, data_dir, tmp_path
):
    """A truncated download passes every other check in here.

    It is the right size, in the right place and under a top-level name the
    archive is allowed to carry, so the only thing that ever notices is SQLite -
    and by then the file has been written over the one database holding the
    accounts, the quotas and the history, with nothing left to go back to.
    """
    staging = tmp_path / "stage"
    (staging / "amneziawg").mkdir(parents=True)
    (staging / "awg-panel").mkdir(parents=True)
    (staging / "amneziawg" / f"{iface()}.conf").write_text(
        server_conf.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # A real header and nothing behind it, which is what half a download looks
    # like: a magic-bytes check would pass this.
    (staging / "awg-panel" / "db.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(staging / "amneziawg", arcname="amneziawg")
        tar.add(staging / "awg-panel", arcname="awg-panel")

    response = _restore(api, buffer.getvalue())

    assert response.status_code == 400, response.content
    assert "Nothing has been changed" in response.json()["detail"]
    assert not (data_dir / "db.sqlite3").exists()


def test_restore_refuses_an_archive_whose_database_is_a_directory(
    api, server_conf, data_dir, tmp_path
):
    """The check used to ask `is_file()` and read False as "no database here".

    An archive from before the panel existed genuinely has none - awg-menu wrote
    those, and they must still restore. A directory called db.sqlite3 is not
    that: it is a malformed archive, it answers False to the same question, and
    it went through to an _install_tree that would put a directory where the
    panel's database belongs.
    """
    staging = tmp_path / "stage"
    (staging / "amneziawg").mkdir(parents=True)
    (staging / "awg-panel" / "db.sqlite3").mkdir(parents=True)
    (staging / "awg-panel" / "db.sqlite3" / "inside").write_text("x", encoding="utf-8")
    (staging / "amneziawg" / f"{iface()}.conf").write_text(
        server_conf.read_text(encoding="utf-8"), encoding="utf-8"
    )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(staging / "amneziawg", arcname="amneziawg")
        tar.add(staging / "awg-panel", arcname="awg-panel")

    response = _restore(api, buffer.getvalue())

    assert response.status_code == 400, response.content
    assert "Nothing has been changed" in response.json()["detail"]
    assert not (data_dir / "db.sqlite3").is_dir()


def test_restore_refuses_an_archive_whose_interface_config_will_not_run(
    api, server_conf, data_dir, tmp_path
):
    """The config had only its name checked, and a name is not a configuration.

    _checked_members proves `amneziawg/<iface>.conf` is in the archive.
    Everything about its contents was left to `awg-quick up`, which runs after
    the tunnel is down and after the file has been written over the working
    one - and _install_tree overwrites, so at that point the config that did
    work is gone. The restore returned 200 with a note saying the tunnel had
    not come back.

    The check runs against the staging copy, so the interface is still up and
    the file on disk is still the old one when this is refused. Both are
    asserted: a 400 that had already taken the tunnel down would be the same
    bug with a better status code.
    """
    kept = server_conf.read_text(encoding="utf-8")
    staging = tmp_path / "stage"
    (staging / "amneziawg").mkdir(parents=True)
    (staging / "awg-panel").mkdir(parents=True)
    # Half a config: the header arrived, the key did not. A truncated download
    # of a real backup looks exactly like this.
    (staging / "amneziawg" / f"{iface()}.conf").write_text(
        "[Interface]\nAddress = 10.8.0.1/24\nListenPort = 51820\n", encoding="utf-8"
    )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(staging / "amneziawg", arcname="amneziawg")
        tar.add(staging / "awg-panel", arcname="awg-panel")

    response = _restore(api, buffer.getvalue())

    assert response.status_code == 400, response.content
    assert "Nothing has been changed" in response.json()["detail"]
    assert server_conf.read_text(encoding="utf-8") == kept


def test_restore_refuses_an_archive_with_an_unusable_peer_key(api, server_conf, data_dir, tmp_path):
    """awg-quick refuses the whole file over one bad key.

    So a peer whose PublicKey did not survive the trip is not a restore that
    comes back one client short: it is a restore whose tunnel does not come
    back at all, discovered after the old config has been overwritten.
    """
    kept = server_conf.read_text(encoding="utf-8")
    staging = tmp_path / "stage"
    (staging / "amneziawg").mkdir(parents=True)
    (staging / "awg-panel").mkdir(parents=True)
    (staging / "amneziawg" / f"{iface()}.conf").write_text(
        f"{kept}\n[Peer]\n# Client = truncated\nPublicKey = not-a-key\nAllowedIPs = 10.8.0.9/32\n",
        encoding="utf-8",
    )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(staging / "amneziawg", arcname="amneziawg")
        tar.add(staging / "awg-panel", arcname="awg-panel")

    response = _restore(api, buffer.getvalue())

    assert response.status_code == 400, response.content
    assert "truncated" in response.json()["detail"]
    assert server_conf.read_text(encoding="utf-8") == kept


def test_restore_still_takes_an_archive_with_no_database_at_all(
    api, server_conf, clients_env, data_dir
):
    """The case the loose check was written for, which has to keep working: a
    backup awg-menu wrote, carrying the config directory and nothing else."""
    payload = b"".join(api.get(api_url("backup")).streaming_content)

    stripped = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as src:
        with tarfile.open(fileobj=stripped, mode="w:gz") as out:
            for member in src.getmembers():
                if member.name.split("/")[0] == "awg-panel":
                    continue
                out.addfile(member, src.extractfile(member) if member.isfile() else None)

    assert _restore(api, stripped.getvalue()).status_code == 200


# --------------------------------------------------- the link rate that was


class _FakeApps:
    """Just enough of a migration's app registry to run one against real models."""

    def __init__(self, model) -> None:
        self._model = model

    def get_model(self, app_label: str, name: str):
        return self._model


# Imported rather than referenced, because a module whose name starts with a
# digit cannot be written as an import statement.
_TOGGLE = import_module("apps.panel.migrations.0003_shaper_toggle")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("stored", "on_after"),
    [
        ("1000", True),  # a server that was shaping goes on shaping
        ("1", True),  # a megabit was a rate like any other, and meant "on"
        ("0", False),  # switched off, and off is already the default
        ("nonsense", True),  # unreadable: keep enforcing rather than quietly stop
    ],
)
def test_a_stored_link_rate_becomes_the_switch_that_replaced_it(stored, on_after):
    """A row exists only where somebody set a rate, which is where shaping was on."""
    Setting.objects.create(key="shaperLinkMbps", value=stored)

    _TOGGLE.carry_over(_FakeApps(Setting), None)

    assert not Setting.objects.filter(key="shaperLinkMbps").exists()
    assert Setting.objects.filter(key="shaperOn", value="1").exists() is on_after


@pytest.mark.django_db
def test_a_server_that_never_set_a_link_rate_is_left_alone():
    """No row means the default, and the default was and is shaping off."""
    _TOGGLE.carry_over(_FakeApps(Setting), None)

    assert not Setting.objects.filter(key="shaperOn").exists()
