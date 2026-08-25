"""The list of signed-in browsers, and signing one of them out from here.

Two properties are worth more than the rest of this file put together, and both
are asserted against behaviour rather than against a row.

Revoking has to actually revoke. A row disappearing from a list proves nothing:
the browser it described still holds a cookie, so every test that ends a session
goes on to make a request with that client and requires a 401. The panel's own
session is the mirror image - it may not be ended here at all, because the
browser would keep sending a cookie for a session that no longer exists and
every page would fail with nothing to explain it.

And a session key must never reach the browser. It is a cookie value: anything
that can read the page - an extension, a proxy log, a screenshot in a support
thread - would be one copy away from being signed in as that session. So the
whole response body is searched for it, not just the field that would obviously
carry it.
"""

import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import sessions
from apps.accounts.models import LoginSession

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, "
    "like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(USERNAME, password=PASSWORD)


def browser(agent: str = CHROME, address: str = "203.0.113.10") -> APIClient:
    """A client that carries one device's address and user agent on every request.

    Given per client rather than per call on purpose: a browser does not change
    address between two pages, and a listing request that arrived from somewhere
    else would rewrite the very row it was asked about.
    """
    return APIClient(HTTP_USER_AGENT=agent, REMOTE_ADDR=address)


def signed_in(user, agent: str = CHROME, address: str = "203.0.113.10") -> APIClient:
    """A signed-in browser with a row already recorded for it."""
    client = browser(agent, address)
    client.force_login(user)
    # force_login() creates the session without making a request, so the row is
    # opened by the middleware on the first one - exactly as it is for a session
    # that was already open when the panel was upgraded.
    client.get(api_url("auth/session"))
    return client


@pytest.fixture
def api(admin) -> APIClient:
    """The browser these tests are "sitting at"."""
    return signed_in(admin)


def other_browser(user, agent: str = IPHONE, address: str = "198.51.100.7") -> APIClient:
    """A second signed-in client, as if the same admin were on another device."""
    return signed_in(user, agent, address)


def listed(client: APIClient) -> list[dict]:
    response = client.get(api_url("settings/sessions"))
    assert response.status_code == 200, response.content
    return response.json()["sessions"]


def only_other(rows: list[dict]) -> dict:
    others = [row for row in rows if not row["current"]]
    assert len(others) == 1, rows
    return others[0]


# ------------------------------------------------------------------ listing


def test_signing_in_records_the_browser_it_was_done_from(admin):
    """The row is opened by the login itself, so a new session is in the list at once."""
    client = browser()
    response = client.post(
        api_url("auth/login"), {"username": USERNAME, "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200, response.content

    rows = listed(client)

    assert len(rows) == 1
    row = rows[0]
    assert row["current"] is True
    assert row["ip"] == "203.0.113.10"
    assert row["browser"] == "Chrome"
    assert row["platform"] == "Windows"
    assert row["userAgent"] == CHROME
    # Every time in the payload describes this session and nothing else.
    assert row["createdAt"] and row["lastSeenAt"] and row["expiresAt"]


def test_the_session_you_are_using_is_first_and_says_so(api, admin):
    """It is the one row the admin has to recognise before judging any of the others."""
    newer = other_browser(admin)
    # The other browser is the one that made the most recent request, so without
    # the rule it would sort above this one.
    newer.get(api_url("auth/session"))

    rows = listed(api)

    assert len(rows) == 2
    assert rows[0]["current"] is True
    assert rows[1]["current"] is False
    assert rows[1]["browser"] == "Safari"
    assert rows[1]["platform"] == "iPhone"
    assert rows[1]["ip"] == "198.51.100.7"


def test_the_session_key_is_nowhere_in_the_answer(api, admin):
    """It is a cookie value. Handing it out would make the list a way in."""
    other_browser(admin)
    keys = list(LoginSession.objects.values_list("session_key", flat=True))
    assert len(keys) == 2

    body = api.get(api_url("settings/sessions")).content.decode()

    for key in keys:
        assert key not in body


def test_another_account_is_not_listed_and_cannot_be_revoked(api):
    """One account today does not make the filter optional."""
    stranger = get_user_model().objects.create_user("someone-else")
    other_browser(stranger)
    theirs = LoginSession.objects.get(user=stranger)

    rows = listed(api)
    revoke = api.delete(api_url(f"settings/sessions/{theirs.pk}"))

    assert [row["current"] for row in rows] == [True]
    # 404 rather than 403: the caller has no business learning that the id
    # exists at all.
    assert revoke.status_code == 404, revoke.content
    assert LoginSession.objects.filter(pk=theirs.pk).exists()


def test_a_session_that_has_ended_elsewhere_drops_out_of_the_list(api, admin):
    """Changing the password deletes sessions directly, and so does clearsessions."""
    other_browser(admin)
    ended = LoginSession.objects.exclude(session_key=api.session.session_key).get()
    Session.objects.filter(session_key=ended.session_key).delete()

    rows = listed(api)

    assert [row["current"] for row in rows] == [True]
    # And the row it left behind is cleared up rather than kept for ever.
    assert not LoginSession.objects.filter(pk=ended.pk).exists()


def test_an_expired_session_is_not_offered_as_something_to_end(api, admin):
    other_browser(admin)
    stale = LoginSession.objects.exclude(session_key=api.session.session_key).get()
    Session.objects.filter(session_key=stale.session_key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )

    assert [row["current"] for row in listed(api)] == [True]


def test_a_session_open_since_before_the_upgrade_still_appears(admin):
    """Nothing recorded it when it began, so the middleware opens its row later."""
    client = browser()
    client.force_login(admin)
    LoginSession.objects.all().delete()

    client.get(api_url("auth/session"))

    rows = listed(client)
    assert len(rows) == 1
    assert rows[0]["current"] is True


# ------------------------------------------------------------------ revoking


def test_revoking_a_session_signs_that_browser_out_immediately(api, admin):
    """Not at its next expiry: the session is deleted, so its cookie names nothing."""
    victim = other_browser(admin)
    assert victim.get(api_url("settings")).status_code == 200
    target = only_other(listed(api))

    response = api.delete(api_url(f"settings/sessions/{target['id']}"))

    assert response.status_code == 204, response.content
    assert victim.get(api_url("settings")).status_code == 401
    assert [row["current"] for row in listed(api)] == [True]
    assert not Session.objects.filter(session_key=victim.session.session_key).exists()


def test_you_cannot_end_the_session_you_are_using(api):
    """The browser would go on sending a cookie for a session that is gone, and
    every page would answer 401 with nothing to explain it. Sign out does this
    properly and is one click away."""
    mine = [row for row in listed(api) if row["current"]][0]

    response = api.delete(api_url(f"settings/sessions/{mine['id']}"))

    assert response.status_code == 400, response.content
    assert "sign out" in response.json()["detail"].lower()
    # Still signed in, and the session was not touched.
    assert api.get(api_url("settings")).status_code == 200
    assert LoginSession.objects.filter(pk=mine["id"]).exists()


def test_revoking_something_that_has_already_gone_is_a_plain_404(api):
    response = api.delete(api_url(f"settings/sessions/{uuid.uuid4()}"))

    assert response.status_code == 404
    assert response.json()["detail"]


def test_signing_out_everywhere_else_keeps_this_one(api, admin):
    """What an admin reaches for when a laptop goes missing, without picking rows."""
    laptop = other_browser(admin, agent=CHROME, address="198.51.100.7")
    phone = other_browser(admin)

    response = api.post(api_url("settings/sessions/revoke-others"))

    assert response.status_code == 200, response.content
    assert response.json()["ended"] == 2
    assert laptop.get(api_url("settings")).status_code == 401
    assert phone.get(api_url("settings")).status_code == 401
    assert api.get(api_url("settings")).status_code == 200
    assert [row["current"] for row in listed(api)] == [True]


def test_signing_out_ends_the_row_as_well_as_the_session(api, admin):
    """Otherwise every sign-out would leave a device in the list for ever."""
    watcher = other_browser(admin)
    assert len(listed(watcher)) == 2

    assert api.post(api_url("auth/logout")).status_code == 204

    assert [row["current"] for row in listed(watcher)] == [True]
    assert LoginSession.objects.filter(user=admin).count() == 1


def test_changing_the_account_signs_every_other_browser_out(api, admin):
    """A rename replaces what the other browsers signed in with, so they go too."""
    laptop = other_browser(admin, agent=CHROME, address="198.51.100.7")

    response = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": "operator"},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert laptop.get(api_url("settings")).status_code == 401
    assert api.get(api_url("settings")).status_code == 200
    assert [row["current"] for row in listed(api)] == [True]


def test_a_password_change_keeps_this_browsers_row_rather_than_starting_a_new_one(api, admin):
    """The session key is cycled underneath it, and the row follows it.

    Without that the list would show this browser as having signed in at the
    moment the password changed - the row it really belongs to having been left
    behind on a key nothing answers to any more.
    """
    started = listed(api)[0]["createdAt"]

    response = api.post(
        api_url("settings/account"),
        {"current": PASSWORD, "new": "a-quite-different-passphrase"},
        format="json",
    )
    assert response.status_code == 200, response.content

    rows = listed(api)
    assert len(rows) == 1
    assert rows[0]["current"] is True
    assert rows[0]["createdAt"] == started
    assert LoginSession.objects.filter(user=admin).count() == 1


# ------------------------------------------------------------- what is recorded


def test_last_used_follows_the_browser_but_not_every_single_request(api):
    """The dashboard polls every two seconds; this column is read to the minute."""
    row = LoginSession.objects.get(session_key=api.session.session_key)
    unchanged = row.last_seen_at

    api.get(api_url("auth/session"))
    row.refresh_from_db()
    assert row.last_seen_at == unchanged

    # Backdated past the resolution, which is what a browser left open does on
    # its own between two glances at the panel.
    aged = timezone.now() - timedelta(seconds=sessions.LAST_SEEN_RESOLUTION_SEC + 1)
    LoginSession.objects.filter(pk=row.pk).update(last_seen_at=aged)

    api.get(api_url("auth/session"))

    row.refresh_from_db()
    assert row.last_seen_at > aged


def test_a_session_that_moves_to_another_address_says_so_at_once(api):
    """The one thing in the row an admin might act on, so it does not wait a minute."""
    api.get(api_url("auth/session"), REMOTE_ADDR="192.0.2.44")

    row = LoginSession.objects.get(session_key=api.session.session_key)
    assert row.ip == "192.0.2.44"


def test_the_forwarded_header_is_not_believed_unless_a_proxy_was_declared(api):
    """Unproxied it is pure client input, and a list reporting whatever an
    attacker typed is worse than one reporting the address we can see."""
    api.get(
        api_url("auth/session"),
        REMOTE_ADDR="192.0.2.44",
        HTTP_X_FORWARDED_FOR="10.0.0.1, 8.8.8.8",
    )

    row = LoginSession.objects.get(session_key=api.session.session_key)
    assert row.ip == "192.0.2.44"


def test_an_address_that_is_not_an_address_is_recorded_as_unknown(api):
    """A unix socket or an odd proxy must not turn every page into a 500."""
    api.get(api_url("auth/session"), REMOTE_ADDR="not-an-address")

    row = LoginSession.objects.get(session_key=api.session.session_key)
    assert row.ip == ""


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (CHROME, ("Chrome", "Windows")),
        (IPHONE, ("Safari", "iPhone")),
        # Edge claims to be Chrome and Safari, Chrome claims to be Safari, and
        # every one of them claims to be Mozilla. Order is the whole rule.
        (
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
            ("Edge", "Windows"),
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
            ("Firefox", "Linux"),
        ),
        # Android says Linux too, and the more specific answer is the useful one.
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Mobile Safari/537.36",
            ("Chrome", "Android"),
        ),
        # Somebody scripting against the API is a real session and has to be
        # recognisable rather than shown as "unknown".
        ("curl/8.5.0", ("curl", "")),
        ("", ("", "")),
        ("something nobody has ever shipped", ("", "")),
    ],
)
def test_the_device_is_read_out_of_the_user_agent_or_left_blank(agent, expected):
    assert sessions.describe_agent(agent) == expected


# ------------------------------------------------------------------ sweeping


def test_the_sweep_takes_what_has_expired_and_leaves_what_has_not(api, admin):
    """Nothing else ever deletes an expired session, so both tables only grew.

    Signing out is the rare way for a session to end - an admin closes the tab
    and comes back next week - and no read path revisits an expired one, so
    without this a row per login accumulated for the life of the install.
    """
    other_browser(admin)
    stale = LoginSession.objects.exclude(session_key=api.session.session_key).get()
    Session.objects.filter(session_key=stale.session_key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )

    removed = sessions.sweep_expired()

    assert removed == 1
    assert not Session.objects.filter(session_key=stale.session_key).exists()
    assert not LoginSession.objects.filter(pk=stale.pk).exists()
    # And the browser this test is sitting at is untouched, in both tables and
    # in the only way that actually matters.
    assert LoginSession.objects.filter(session_key=api.session.session_key).exists()
    assert api.get(api_url("auth/session")).status_code == 200


def test_the_sweep_takes_a_row_whose_session_has_already_gone(api, admin):
    """A row outlives its session whenever something ends one directly.

    Changing the password does exactly that, and so does revoking from another
    device: the session row goes and this one is left naming nothing. The list
    clears those in passing, but only for the account whose page somebody has
    opened - which on a panel nobody visits that page of is never.
    """
    other_browser(admin)
    orphan = LoginSession.objects.exclude(session_key=api.session.session_key).get()
    Session.objects.filter(session_key=orphan.session_key).delete()

    assert sessions.sweep_expired() == 1
    assert not LoginSession.objects.filter(pk=orphan.pk).exists()


def test_the_sweep_is_quiet_when_there_is_nothing_to_take(api):
    """It runs daily forever; the common case is a panel with one live session."""
    assert sessions.sweep_expired() == 0
    assert LoginSession.objects.count() == 1
    assert api.get(api_url("auth/session")).status_code == 200
