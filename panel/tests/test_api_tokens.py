"""API tokens: issuing one, using one, and taking one away again.

Four properties carry this file, and each is asserted against what the panel
does rather than against what it stored.

The secret is shown once. So the create response is the only place it is looked
for, the whole listing body is searched for it afterwards, and the row itself is
read back to prove that what is on disk could not be presented at the door.

A token has to actually be a way in. Every test that issues one goes on to make
a real request with it - including a mutating one with no CSRF header at all,
because a script has no cookie to double-submit and demanding one would make the
whole feature unusable.

Revoking and expiry have to actually stop it. A row disappearing from a list
proves nothing: whoever copied the secret still has it, so every test that ends
a token goes on to present it and requires a 401.

And a token may not enlarge itself. Holding one is not permission to issue
another, to change the password, or to turn off the second factor - otherwise
revoking a leaked token would be theatre, because whoever had it could have
minted a replacement with no expiry before anybody noticed.

Where that fourth property stops is asserted too, and deliberately. A token may
download a backup, and the archive carries the session table - so it is a way
round every rule above, in one step, by design rather than by oversight. That is
pinned here because it is the sort of thing a reader assumes cannot be true: a
test that says out loud what the archive contains is what stops the docs beside
it drifting back into claiming a boundary the panel does not have.
"""

import io
import tarfile
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import tokens
from apps.accounts.models import ApiToken, LoginSession
from apps.events.models import Event
from awg import names

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"

DAY = 86400


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(USERNAME, password=PASSWORD)


@pytest.fixture
def api(admin) -> APIClient:
    """The browser the admin is sitting at."""
    client = APIClient(REMOTE_ADDR="203.0.113.10")
    client.force_login(admin)
    return client


def bearer(secret: str, address: str = "198.51.100.7") -> APIClient:
    """A client that presents a token and nothing else - no cookie, no CSRF.

    CSRF checking is left switched on, which is what makes these assertions mean
    something: the test client normally waves it through, and with that off a
    request that only works because the token exempted it would be
    indistinguishable from one that was never checked.
    """
    client = APIClient(REMOTE_ADDR=address, enforce_csrf_checks=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
    return client


def create(api: APIClient, **body) -> dict:
    """Issue a token through the API and hand back the whole response body."""
    payload = {"name": "deploy"} | body
    response = api.post(api_url("settings/tokens"), payload, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def listed(api: APIClient) -> list[dict]:
    response = api.get(api_url("settings/tokens"))
    assert response.status_code == 200, response.content
    return response.json()["tokens"]


def kinds_recorded() -> list[str]:
    return list(Event.objects.order_by("id").values_list("kind", flat=True))


# ------------------------------------------------------------------ issuing


def test_a_new_token_comes_back_with_its_secret_and_the_row_it_made(api):
    body = create(api, expiresIn=30 * DAY)

    assert body["secret"].startswith(tokens.PREFIX)
    row = body["token"]
    assert row["name"] == "deploy"
    # The last few characters, so the row can be matched against the copy in a
    # CI configuration.
    assert body["secret"].endswith(row["hint"])
    assert row["expiresIn"] == 30 * DAY
    assert row["expiresAt"]
    assert row["expired"] is False
    assert row["renewOnUse"] is False
    assert row["lastUsedAt"] is None
    assert row["lastUsedIp"] == ""


def test_the_secret_is_never_shown_again(api):
    secret = create(api)["secret"]

    body = api.get(api_url("settings/tokens")).content.decode()

    assert secret not in body
    # Not the whole thing and not most of it either: everything but the hint.
    assert secret[: -len(listed(api)[0]["hint"])] not in body


def test_the_secret_is_not_stored_anywhere(api):
    """A database in a backup, or in a support archive, must leak no way in."""
    secret = create(api)["secret"]

    row = ApiToken.objects.get()

    assert secret not in str(row.__dict__)
    assert row.token_hash != secret
    # And the hash is the one the presented string produces, which is what makes
    # the lookup work at all.
    assert row.token_hash == tokens.digest(secret)


def test_two_tokens_cannot_share_a_name(api):
    """Names are what the activity log tells one from another by."""
    create(api, name="deploy")

    response = api.post(api_url("settings/tokens"), {"name": "Deploy"}, format="json")

    assert response.status_code == 409, response.content
    assert ApiToken.objects.count() == 1


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}])
def test_a_token_with_no_name_is_given_one(api, body):
    """A name is required and the panel supplies it, which are not in conflict.

    The activity log writes the name beside everything the token does, so a
    nameless token would leave a log saying "admin" for work nobody was present
    for. What the panel refuses is a token with no name at all; what it does not
    do is make somebody invent a word before they can have a credential.
    """
    response = api.post(api_url("settings/tokens"), body, format="json")

    assert response.status_code == 201, response.content
    name = response.json()["token"]["name"]
    assert len(name) == names.LENGTH
    assert set(name) <= set(names.ALPHABET)
    # And it is the name the row was actually stored under, not a decoration on
    # the reply: the log is about to write this word.
    assert ApiToken.objects.get().name == name


def test_a_drawn_name_does_not_land_on_one_the_account_already_has(api, monkeypatch):
    """The name is an identifier, so "very unlikely" is not the same as checked."""
    create(api, name="aaaaaaaaa")
    # Two draws, the first of them the name that is already taken. Forced,
    # because forty-five bits will not produce that collision on its own and the
    # branch that survives it has to be exercised by something.
    draws = iter(["aaaaaaaaa", "bbbbbbbbb"])
    monkeypatch.setattr(names, "random_name", lambda *args, **kwargs: next(draws))

    row = create(api, name="")["token"]

    assert row["name"] == "bbbbbbbbb"
    assert sorted(ApiToken.objects.values_list("name", flat=True)) == ["aaaaaaaaa", "bbbbbbbbb"]


def test_a_name_is_trimmed_rather_than_kept_as_typed(api):
    row = create(api, name="  nightly backup  ")["token"]

    assert row["name"] == "nightly backup"


@pytest.mark.parametrize("lifetime", [60, tokens.MAX_LIFETIME_SEC + 1, -1])
def test_an_impossible_expiry_is_refused(api, lifetime):
    """Too short for renewal to keep up with, too long to mean anything, or past."""
    response = api.post(
        api_url("settings/tokens"), {"name": "deploy", "expiresIn": lifetime}, format="json"
    )

    assert response.status_code == 400, response.content
    assert response.json()["errors"]["expiresIn"]


def test_a_token_can_be_given_no_expiry_at_all(api):
    """Allowed, and deliberately not the shape of an omission: it is a choice."""
    row = create(api, expiresIn=0)["token"]

    assert row["expiresAt"] is None
    assert row["expiresIn"] is None
    assert row["expired"] is False


def test_renewal_is_off_unless_it_is_asked_for(api):
    row = create(api, expiresIn=30 * DAY)["token"]

    assert row["renewOnUse"] is False


def test_renewal_without_an_expiry_is_refused_rather_than_ignored(api):
    """There is nothing to renew, and a quiet 201 would say otherwise."""
    response = api.post(
        api_url("settings/tokens"),
        {"name": "deploy", "expiresIn": 0, "renewOnUse": True},
        format="json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["errors"]["renewOnUse"]
    assert not ApiToken.objects.exists()


# ------------------------------------------------------------------- using


def test_a_token_opens_the_api_with_no_cookie_at_all(api):
    secret = create(api)["secret"]

    response = bearer(secret).get(api_url("events"))

    assert response.status_code == 200, response.content


def test_a_token_may_change_things_without_a_csrf_header(admin, api):
    """A script has no cookie to double-submit, so there is nothing to protect.

    CSRF exists because a browser attaches cookies to a request it was tricked
    into making. Nothing tricks a browser into attaching an Authorization
    header, so demanding the token *and* a CSRF token would make the feature
    unusable for the callers it exists for.

    The cookie half of the same request is made first, with CSRF checking on and
    no token supplied, and is refused - so what this proves is that the bearer
    header is the difference, rather than that nothing was being checked.
    """
    secret = create(api)["secret"]

    cookies = APIClient(enforce_csrf_checks=True)
    cookies.force_login(admin)
    refused = cookies.put(api_url("settings"), {"theme": "dark"}, format="json")
    assert refused.status_code == 403, refused.content

    response = bearer(secret).put(api_url("settings"), {"theme": "dark"}, format="json")

    assert response.status_code == 200, response.content
    assert api.get(api_url("settings")).json()["theme"] == "dark"


def test_a_token_is_not_a_signed_in_browser(api):
    """It has no session, so it must not turn up in the list of devices."""
    secret = create(api)["secret"]

    assert bearer(secret).get(api_url("events")).status_code == 200

    rows = api.get(api_url("settings/sessions")).json()["sessions"]
    assert [row["current"] for row in rows] == [True]
    assert LoginSession.objects.count() == 1


@pytest.mark.parametrize(
    "header",
    [
        "Bearer awgp_nothing-was-ever-issued-under-this",
        "Bearer not-even-the-right-shape",
        # A header with no token in it at all, which is a client bug rather than
        # an attack and still must not be read as "anonymous, carry on".
        "Bearer",
        "Bearer one two",
    ],
)
def test_a_token_that_names_nothing_is_a_401(api, header):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=header)

    response = client.get(api_url("events"))

    assert response.status_code == 401, response.content
    assert response.json()["detail"]


def test_a_refused_token_writes_nothing_to_the_activity_log(api):
    """This path is drivable by anyone; an audit trail it can grow is not one.

    A refused sign-in is recorded because django-axes bounds how many of them
    one address may produce. Nothing bounds this, so the answer is to record the
    successful use in the token's own row and nothing at all here.
    """
    create(api)
    before = Event.objects.count()

    for _ in range(5):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer awgp_guessing")
        assert client.get(api_url("events")).status_code == 401

    assert Event.objects.count() == before


def test_a_request_without_any_credentials_is_still_anonymous(api):
    """The token authenticator has to decline rather than refuse, or the login
    page itself would start answering 401 to the browser bootstrapping it."""
    response = APIClient().get(api_url("auth/session"))

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


# ---------------------------------------------------------------- expiring


def test_an_expired_token_stops_working_but_stays_in_the_list(api):
    """The row is the answer to "why did my deployment start failing on Tuesday"."""
    secret = create(api, expiresIn=DAY)["secret"]
    assert bearer(secret).get(api_url("events")).status_code == 200

    ApiToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    assert bearer(secret).get(api_url("events")).status_code == 401
    rows = listed(api)
    assert len(rows) == 1
    assert rows[0]["expired"] is True


def test_a_token_set_to_renew_gets_its_whole_life_back_on_each_use(api):
    secret = create(api, expiresIn=7 * DAY, renewOnUse=True)["secret"]
    # Most of the way through its life, which is where a daily job would find it.
    nearly = timezone.now() + timedelta(seconds=60)
    ApiToken.objects.update(expires_at=nearly)

    assert bearer(secret).get(api_url("events")).status_code == 200

    row = ApiToken.objects.get()
    assert row.expires_at > nearly + timedelta(days=6)
    assert listed(api)[0]["expired"] is False


def test_a_token_that_does_not_renew_keeps_the_expiry_it_was_given(api):
    secret = create(api, expiresIn=7 * DAY)["secret"]
    was = ApiToken.objects.get().expires_at

    assert bearer(secret).get(api_url("events")).status_code == 200

    assert ApiToken.objects.get().expires_at == was


def test_renewal_does_not_rewrite_the_row_on_every_single_request(api):
    """A script polling the dashboard must not make this the busiest table here."""
    secret = create(api, expiresIn=7 * DAY, renewOnUse=True)["secret"]
    client = bearer(secret)
    assert client.get(api_url("events")).status_code == 200
    settled = ApiToken.objects.get()

    assert client.get(api_url("events")).status_code == 200

    row = ApiToken.objects.get()
    assert row.expires_at == settled.expires_at
    assert row.last_used_at == settled.last_used_at


# ------------------------------------------------------------- what is recorded


def test_the_first_use_says_when_and_from_where(api):
    secret = create(api)["secret"]

    assert bearer(secret, address="192.0.2.44").get(api_url("events")).status_code == 200

    row = listed(api)[0]
    assert row["lastUsedAt"]
    assert row["lastUsedIp"] == "192.0.2.44"


def test_a_token_presented_from_somewhere_new_says_so_at_once(api):
    """The one field here an admin might act on, so it does not wait a minute."""
    secret = create(api)["secret"]
    assert bearer(secret, address="192.0.2.44").get(api_url("events")).status_code == 200

    assert bearer(secret, address="198.51.100.7").get(api_url("events")).status_code == 200

    assert ApiToken.objects.get().last_used_ip == "198.51.100.7"


def test_last_used_is_written_to_the_minute_and_no_finer(api):
    secret = create(api)["secret"]
    client = bearer(secret)
    assert client.get(api_url("events")).status_code == 200
    unchanged = ApiToken.objects.get().last_used_at

    assert client.get(api_url("events")).status_code == 200
    assert ApiToken.objects.get().last_used_at == unchanged

    aged = timezone.now() - timedelta(seconds=tokens.LAST_USED_RESOLUTION_SEC + 1)
    ApiToken.objects.update(last_used_at=aged)

    assert client.get(api_url("events")).status_code == 200

    assert ApiToken.objects.get().last_used_at > aged


def test_what_a_token_does_is_logged_under_the_tokens_name(api):
    """The account name would be true and useless: every token authenticates as it."""
    secret = create(api, name="nightly backup")["secret"]

    response = bearer(secret).put(api_url("settings"), {"theme": "dark"}, format="json")

    assert response.status_code == 200, response.content
    saved = Event.objects.filter(kind="panel.settings-saved").get()
    assert saved.actor == "api:nightly backup"
    assert saved.actor_ip == "198.51.100.7"


def test_issuing_and_revoking_are_both_events(api):
    body = create(api, name="deploy", expiresIn=30 * DAY, renewOnUse=True)

    assert api.delete(api_url(f"settings/tokens/{body['token']['id']}")).status_code == 204

    created, revoked = Event.objects.order_by("id")
    assert created.kind == "auth.token-created"
    assert created.actor == USERNAME
    assert created.target == "deploy"
    assert created.detail == {"seconds": 30 * DAY, "renew": True}
    assert revoked.kind == "auth.token-revoked"
    assert revoked.target == "deploy"


# ------------------------------------------------------------------ changing


def test_a_token_can_be_renamed_without_being_reissued(api):
    ident = create(api, name="deploy")["token"]["id"]

    response = api.put(api_url(f"settings/tokens/{ident}"), {"name": "ci"}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["name"] == "ci"
    assert listed(api)[0]["name"] == "ci"
    # The rename is readable as one: both halves of it are in the event.
    event = Event.objects.get(kind="auth.token-updated")
    assert event.target == "ci"
    assert event.detail["name"] == "deploy"


def test_renaming_a_token_leaves_its_secret_working(api):
    """It is the same credential; only what the log calls it has changed."""
    body = create(api, name="deploy")
    secret = body["secret"]

    api.put(api_url(f"settings/tokens/{body['token']['id']}"), {"name": "ci"}, format="json")

    assert bearer(secret).get(api_url("events")).status_code == 200


def test_renewal_can_be_turned_on_afterwards(api):
    ident = create(api, name="deploy", expiresIn=DAY)["token"]["id"]

    response = api.put(api_url(f"settings/tokens/{ident}"), {"renewOnUse": True}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["renewOnUse"] is True


def test_renewal_cannot_be_turned_on_for_a_token_with_no_expiry(api):
    ident = create(api, name="deploy", expiresIn=0)["token"]["id"]

    response = api.put(api_url(f"settings/tokens/{ident}"), {"renewOnUse": True}, format="json")

    assert response.status_code == 409, response.content
    assert ApiToken.objects.get().renew_on_use is False


def test_a_rename_onto_a_name_already_in_use_is_refused(api):
    create(api, name="deploy")
    ident = create(api, name="ci")["token"]["id"]

    response = api.put(api_url(f"settings/tokens/{ident}"), {"name": "deploy"}, format="json")

    assert response.status_code == 409, response.content
    assert sorted(row["name"] for row in listed(api)) == ["ci", "deploy"]


def test_a_change_that_changes_nothing_is_refused_rather_than_reported_as_saved(api):
    ident = create(api)["token"]["id"]

    response = api.put(api_url(f"settings/tokens/{ident}"), {}, format="json")

    assert response.status_code == 400, response.content


def test_saving_the_values_a_token_already_had_writes_no_event(api):
    """An event saying something changed, on a day nothing did, is worse than none."""
    body = create(api, name="deploy", expiresIn=DAY)
    ident = body["token"]["id"]

    response = api.put(
        api_url(f"settings/tokens/{ident}"), {"name": "deploy", "renewOnUse": False}, format="json"
    )

    assert response.status_code == 200, response.content
    assert kinds_recorded() == ["auth.token-created"]


# ------------------------------------------------------------------ revoking


def test_revoking_a_token_stops_it_on_the_next_request(api):
    body = create(api)
    secret = body["secret"]
    assert bearer(secret).get(api_url("events")).status_code == 200

    response = api.delete(api_url(f"settings/tokens/{body['token']['id']}"))

    assert response.status_code == 204, response.content
    assert bearer(secret).get(api_url("events")).status_code == 401
    assert listed(api) == []
    # Nothing is kept: a row held "for the record" is a hash that still matches
    # a secret somebody has.
    assert not ApiToken.objects.exists()


def test_revoking_something_that_has_already_gone_is_a_plain_404(api):
    response = api.delete(api_url(f"settings/tokens/{uuid.uuid4()}"))

    assert response.status_code == 404
    assert response.json()["detail"]


def test_another_accounts_token_is_neither_listed_nor_revocable(api, admin):
    """One account today does not make the filter optional."""
    stranger = get_user_model().objects.create_user("someone-else")
    theirs, secret = tokens.issue(stranger, name="theirs", lifetime=None, renew_on_use=False)

    rows = listed(api)
    revoke = api.delete(api_url(f"settings/tokens/{theirs.pk}"))

    assert rows == []
    # 404 rather than 403: the caller has no business learning that the id
    # exists at all.
    assert revoke.status_code == 404, revoke.content
    assert ApiToken.objects.filter(pk=theirs.pk).exists()
    # And it still works, because nothing about it was touched.
    assert bearer(secret).get(api_url("events")).status_code == 200


# ------------------------------------------------------- what a token may not do


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        # Minting a replacement for the one that leaked.
        ("post", "settings/tokens", {"name": "second"}),
        ("get", "settings/tokens", None),
        # Taking the account away from the person who issued it.
        ("post", "settings/account", {"current": PASSWORD, "new": "another-long-passphrase"}),
        # Removing the second factor that stands between a stolen password and
        # the panel.
        ("post", "settings/2fa/disable", {"password": PASSWORD}),
        ("post", "settings/2fa/enable", {}),
        # Reading, or ending, the browsers a person is signed in with.
        ("get", "settings/sessions", None),
        ("post", "settings/sessions/revoke-others", None),
    ],
)
def test_a_token_cannot_touch_the_credentials_that_authorise_it(api, method, path, body):
    secret = create(api)["secret"]
    client = bearer(secret)

    call = getattr(client, method)
    response = call(api_url(path), body, format="json") if body is not None else call(api_url(path))

    # 403, not 401: the caller is authenticated and still may not do this.
    assert response.status_code == 403, response.content
    assert response.json()["detail"]


def test_a_token_cannot_restore_a_backup_over_the_account(api):
    """The one route outside the account pages that has to be closed to a token.

    A restore replaces db.sqlite3, which is where the password hash and these
    very tokens live - so a token allowed to upload an archive could hand the
    account to whoever chose the archive. Downloading one stays open; what that
    costs is the test below.
    """
    secret = create(api)["secret"]

    response = bearer(secret).post(api_url("restore"), {}, format="multipart")

    assert response.status_code == 403, response.content
    assert bearer(secret).get(api_url("backup")).status_code == 200


def test_the_backup_a_token_may_download_carries_the_panels_own_database(api, data_dir):
    """Where the rule above ends, asserted rather than left to be discovered.

    The archive is the configuration directory plus the panel's data directory,
    and the second of those is db.sqlite3 with the signing key beside it. The
    database holds django_session, whose primary key is the awgsessionid cookie
    itself and not a hash of it - so a token that fetches one archive can put on
    a signed-in browser's cookie, and is then past every 403 asserted above.

    That is a deliberate trade and apps.panel.views.BackupView says why: the same
    file already carries the server's private key and every client's, so anything
    trusted to copy a backup off the box nightly is trusted with the tunnel
    regardless. What it must not be is a surprise, which is what this test is for.
    It fails the moment the archive stops containing what the documentation beside
    it says it does - in either direction.
    """
    # The suite runs against its own database, so the two files a deployment
    # keeps in this directory are planted rather than assumed. What is asserted
    # is that the data directory travels whole, not that sqlite wrote anything.
    (data_dir / "db.sqlite3").write_bytes(b"SQLite format 3\x00")
    (data_dir / "secret.key").write_text("signing-key\n")

    secret = create(api)["secret"]
    response = bearer(secret).get(api_url("backup"))
    assert response.status_code == 200, response.content

    blob = b"".join(response.streaming_content) if response.streaming else response.content
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = set(archive.getnames())

    assert "awg-panel/db.sqlite3" in members, sorted(members)
    assert "awg-panel/secret.key" in members, sorted(members)


def test_a_token_cannot_revoke_a_token(api):
    body = create(api)
    secret = body["secret"]

    response = bearer(secret).delete(api_url(f"settings/tokens/{body['token']['id']}"))

    assert response.status_code == 403, response.content
    assert ApiToken.objects.exists()


def test_a_token_has_no_session_to_sign_out_of(api):
    secret = create(api)["secret"]

    response = bearer(secret).post(api_url("auth/logout"))

    assert response.status_code == 403, response.content
    assert bearer(secret).get(api_url("events")).status_code == 200


def test_a_token_can_still_ask_who_it_is(api):
    """Which is how a script checks its credential without changing anything."""
    secret = create(api)["secret"]

    response = bearer(secret).get(api_url("auth/session"))

    assert response.status_code == 200, response.content
    assert response.json()["authenticated"] is True
    assert response.json()["username"] == USERNAME


# ------------------------------------------------------------------ the plumbing


def test_a_token_belonging_to_a_disabled_account_is_refused(admin):
    _, secret = tokens.issue(admin, name="deploy", lifetime=None, renew_on_use=False)
    assert bearer(secret).get(api_url("events")).status_code == 200

    admin.is_active = False
    admin.save(update_fields=["is_active"])

    assert bearer(secret).get(api_url("events")).status_code == 401


def test_deleting_the_account_takes_its_tokens_with_it(admin):
    _, secret = tokens.issue(admin, name="deploy", lifetime=None, renew_on_use=False)

    admin.delete()

    assert not ApiToken.objects.exists()
    assert bearer(secret).get(api_url("events")).status_code == 401


def test_two_tokens_issued_a_moment_apart_are_not_the_same_string(admin):
    _, first = tokens.issue(admin, name="one", lifetime=None, renew_on_use=False)
    _, second = tokens.issue(admin, name="two", lifetime=None, renew_on_use=False)

    assert first != second
    assert tokens.authenticate(first).name == "one"
    assert tokens.authenticate(second).name == "two"


def test_a_secret_with_whitespace_around_it_still_works(admin):
    """Copied out of a terminal, pasted into a config file, read back with a newline."""
    _, secret = tokens.issue(admin, name="deploy", lifetime=None, renew_on_use=False)

    assert tokens.authenticate(f"  {secret}\n") is not None
