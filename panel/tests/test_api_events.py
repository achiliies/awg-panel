"""The event log and the service log.

Two properties matter more than the rest of this file, and both are about what
happens when something goes wrong rather than when it goes right.

Recording an event must never fail the thing it describes. A delete that removed
the client and then answered 500 because the ledger could not be written is
worse than no ledger at all, so the write is exercised against a database that
refuses it and the request is required to succeed anyway.

And the log must stay bounded. It is written by whatever arrives at the panel -
a refused sign-in is an event - so the tests below pin both halves of that: the
age window, the row ceiling, and the rule that an address already inside its
lockout writes nothing at all however many times it knocks.

The service log is asserted against journalctl's contract rather than against a
journal: the suite runs on developer machines and in containers, where there may
be no systemd at all, so the reader is driven with a captured `subprocess.run`
and the real one only has to answer "unavailable" without raising.
"""

import json
import subprocess
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import tokens
from apps.events import journal, kinds, recorder
from apps.events.models import MAX_ROWS, RETENTION_DAYS, Event
from awg import traffic
from awg.controller import reset_controller
from awg.traffic import Counters

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
ADDRESS = "203.0.113.10"


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture(autouse=True)
def fresh_controller():
    """One mock interface per test; get_controller() caches for the process."""
    reset_controller()
    yield
    reset_controller()


@pytest.fixture
def api(server_conf) -> APIClient:
    """A signed-in client against the fixture server config, carrying an address.

    The address is on the client rather than on each call because that is what a
    browser is: every request from one machine arrives from the same place, and
    the event log records where each of them came from.
    """
    user = get_user_model().objects.create_user(USERNAME)
    client = APIClient(REMOTE_ADDR=ADDRESS)
    client.force_login(user)
    return client


def kinds_recorded() -> list[str]:
    """Every event so far, oldest first, which is the order they happened in."""
    return [row.kind for row in Event.objects.order_by("id")]


def only(kind: str) -> Event:
    """The single event of this kind, asserting that there is exactly one."""
    rows = list(Event.objects.filter(kind=kind))
    assert len(rows) == 1, f"expected one {kind}, got {[row.kind for row in Event.objects.all()]}"
    return rows[0]


# ---------------------------------------------------------------- recording


def test_creating_a_client_records_who_did_it_and_from_where(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")

    row = only(kinds.CLIENT_CREATED)
    assert row.target == "phone"
    assert row.actor == USERNAME
    assert row.actor_ip == ADDRESS
    assert row.severity == kinds.INFO


def test_deleting_a_client_is_a_warning_because_it_cannot_be_undone(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.delete(api_url("clients/phone"))

    assert only(kinds.CLIENT_DELETED).severity == kinds.WARNING


def test_an_edit_that_renames_and_changes_fields_records_both(api):
    """One request, two decisions, and the rename is the one worth its own row.

    Folding them together would lose whichever of the two somebody searches for,
    and every row after the rename is about the new name - so that is the name
    the rename itself is filed under, with the old one in the detail.
    """
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.put(api_url("clients/phone"), {"name": "laptop", "note": "desk"}, format="json")

    renamed = only(kinds.CLIENT_RENAMED)
    assert renamed.target == "laptop"
    assert renamed.detail == {"name": "phone"}

    updated = only(kinds.CLIENT_UPDATED)
    assert updated.target == "laptop"
    assert updated.detail == {"fields": ["note"]}


def test_switching_a_client_off_is_its_own_kind_rather_than_an_edit(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")

    assert kinds.CLIENT_DISABLED in kinds_recorded()
    assert kinds.CLIENT_UPDATED not in kinds_recorded()


def test_an_edit_that_puts_a_client_over_its_limit_says_so_as_the_collector_would(api):
    """The same kind either side, because it is the same decision.

    An operator scanning for clients their data limit stopped must not have to
    know whether the collector reached the verdict or the edit that lowered the
    limit did. What differs is the actor: the collector acts on nobody's behalf
    and leaves it empty, and here the admin whose edit caused it is named, one
    row above their own "updated".
    """
    created = api.post(api_url("clients"), {"name": "phone"}, format="json").json()
    traffic.write_db({created["publicKey"]: Counters(10_000, 0, 10_000, 0)})

    api.put(api_url("clients/phone"), {"quotaBytes": 4000}, format="json")

    row = only(kinds.CLIENT_QUOTA_REACHED)
    assert row.target == "phone"
    assert row.actor == USERNAME
    assert row.severity == kinds.WARNING
    assert kinds.CLIENT_DISABLED not in kinds_recorded()


def test_a_bulk_removal_is_one_row_with_a_count(api):
    """Not one per client: a sweep of five hundred would bury the day it happened.

    The categories are recorded instead of the names, because a list of names is
    capped by what a detail may hold - complete on a sweep of three and silently
    short on a sweep of thirty - while the categories are exact at any size.
    """
    for name in ("one", "two", "three"):
        api.post(api_url("clients"), {"name": name}, format="json")
        api.put(api_url(f"clients/{name}"), {"enabled": False}, format="json")

    response = api.post(api_url("clients/bulk-remove"), {"disabled": True}, format="json")
    assert response.status_code == 200, response.content

    row = only(kinds.CLIENT_BULK_DELETED)
    assert row.detail == {"count": 3, "expired": False, "disabled": True}


def test_a_sweep_that_removed_nothing_writes_nothing(api):
    api.post(api_url("clients/bulk-remove"), {"expired": True}, format="json")

    assert Event.objects.filter(kind=kinds.CLIENT_BULK_DELETED).count() == 0


def test_ending_no_other_sessions_writes_nothing(api):
    """The same rule as the sweep above, on the one other button that can do nothing.

    The panel only offers "sign out everywhere else" when there is somewhere
    else, so a zero here is a stale page or a session that expired between the
    list and the click - and a line saying nought browsers were signed out is
    about neither of those.
    """
    response = api.post(api_url("settings/sessions/revoke-others"))
    assert response.status_code == 200, response.content
    assert response.json()["ended"] == 0

    assert Event.objects.filter(kind=kinds.AUTH_SESSIONS_REVOKED).count() == 0


def test_a_settings_save_records_the_values_it_is_allowed_to_quote(api):
    """Which settings moved, and what the ones fit to quote became.

    The value is what makes the line worth reading - "speed limits (off)" rather
    than "something about speed limits" - and it stops at anything that says how
    to reach this panel. The secret path is the reason that rule exists: it is
    recorded as having changed, and never as what it changed to.
    """
    response = api.put(
        api_url("settings"),
        {"theme": "dark", "trafficPollSec": "5", "webBasePath": "/p/99887766/"},
        format="json",
    )
    assert response.status_code == 200, response.content

    row = only(kinds.PANEL_SETTINGS_SAVED)
    assert row.detail["count"] == 3
    assert row.detail["keys"] == ["theme", "trafficPollSec", "webBasePath"]
    # In the order the settings page shows them, and without the path anywhere.
    assert row.detail["values"] == ["theme=dark", "trafficPollSec=5"]
    assert "99887766" not in json.dumps(row.detail)


def test_ending_a_session_records_which_browser_it_was(api):
    """The row it was picked from is deleted, so this line is the only description left."""
    other = APIClient(
        REMOTE_ADDR="198.51.100.23",
        HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    )
    other.force_login(get_user_model().objects.get(username=USERNAME))
    other.get(api_url("auth/session"))

    listed = api.get(api_url("settings/sessions")).json()["sessions"]
    ident = next(row["id"] for row in listed if not row["current"])
    assert api.delete(api_url(f"settings/sessions/{ident}")).status_code == 204

    row = only(kinds.AUTH_SESSION_REVOKED)
    # Where the browser that was ended was, not where the admin ending it is:
    # that one is in actor_ip, as it is on every other row.
    assert row.target == "198.51.100.23"
    assert row.actor_ip == ADDRESS
    assert row.detail == {"browser": "Firefox", "platform": "Linux"}


def test_renaming_the_account_is_recorded_under_both_names(server_conf):
    """A rename that only recorded the new name would say nothing about what changed."""
    user = get_user_model().objects.create_user(USERNAME, password=PASSWORD)
    client = APIClient(REMOTE_ADDR=ADDRESS)
    client.force_login(user)

    response = client.post(
        api_url("settings/account"),
        {"current": PASSWORD, "username": "operator"},
        format="json",
    )
    assert response.status_code == 200, response.content

    row = only(kinds.AUTH_USERNAME_CHANGED)
    assert row.detail["name"] == USERNAME
    assert row.target == "operator"
    # The actor is the account as it is now. It is the same person either way,
    # and the name they will be recorded under from here on is the useful one.
    assert row.actor == "operator"
    assert row.actor_ip == ADDRESS
    assert row.severity == kinds.INFO


def test_the_collector_records_without_an_actor(api):
    """A client switched off for its quota was switched off by nobody.

    An empty actor is a fact rather than a missing value, and it is what the UI
    draws as the panel acting on its own.
    """
    recorder.record_many(kinds.CLIENT_QUOTA_REACHED, ["phone", "laptop"])

    rows = Event.objects.filter(kind=kinds.CLIENT_QUOTA_REACHED)
    assert sorted(row.target for row in rows) == ["laptop", "phone"]
    assert {row.actor for row in rows} == {""}
    assert {row.actor_ip for row in rows} == {""}
    assert {row.severity for row in rows} == {kinds.WARNING}


def test_recording_never_fails_the_request_it_describes(api, monkeypatch):
    """The client is gone either way; a 500 would only lie about it.

    Broken at the model rather than by closing the connection, because what has
    to be proved is that no exception from the ledger reaches the handler - and
    the handler must still answer 204.
    """
    api.post(api_url("clients"), {"name": "phone"}, format="json")

    def explode(*args, **kwargs):
        raise RuntimeError("the ledger is on fire")

    monkeypatch.setattr(Event.objects, "create", explode)
    response = api.delete(api_url("clients/phone"))

    assert response.status_code == 204
    assert Event.objects.filter(kind=kinds.CLIENT_DELETED).count() == 0


def test_a_detail_keeps_only_what_survives_json(api):
    """A whitelist, so a private key or a whole request cannot reach the column.

    Anything richer than a string, a number, a boolean or a flat list of those is
    dropped without comment: the alternative is an exception on the audit path.
    """
    recorder.record(
        kinds.CLIENT_CREATED,
        target="phone",
        count=3,
        flag=True,
        name="x" * 500,
        fields=["a", "b"],
        secret=object(),
        mapping={"nested": 1},
    )

    detail = only(kinds.CLIENT_CREATED).detail
    assert detail["count"] == 3
    assert detail["flag"] is True
    assert len(detail["name"]) == recorder.MAX_DETAIL_TEXT
    assert detail["fields"] == ["a", "b"]
    assert "secret" not in detail
    assert "mapping" not in detail


# ------------------------------------------------------------------- signing in


def test_a_refused_sign_in_is_recorded_as_a_warning(server_conf, settings):
    get_user_model().objects.create_user(USERNAME, password=PASSWORD)
    client = APIClient(REMOTE_ADDR=ADDRESS)

    client.get(api_url("auth/session"))  # issues the CSRF cookie
    response = client.post(
        api_url("auth/login"), {"username": USERNAME, "password": "wrong"}, format="json"
    )
    assert response.status_code == 401

    row = only(kinds.AUTH_SIGN_IN_FAILED)
    assert row.severity == kinds.WARNING
    assert row.actor == ""
    assert row.actor_ip == ADDRESS
    # The name is what was typed, not an account: it belongs in the detail, where
    # it reads as a claim rather than as a fact about who did this. Beside it,
    # which attempt this was out of how many the address gets - one refusal says
    # nothing on its own, and a run of them is the whole signal.
    assert row.detail == {"name": USERNAME, "tries": 1, "limit": settings.AXES_FAILURE_LIMIT}


def test_knocking_at_a_locked_door_writes_nothing(server_conf, settings):
    """The one path in the panel that an outsider can drive as often as they like.

    django-axes caps the attempts it counts, but nothing caps how many requests
    arrive once an address is locked out - so a row per refusal would be a way to
    fill a disk. What the flood costs is the lockout line and nothing after it.
    """
    get_user_model().objects.create_user(USERNAME, password=PASSWORD)
    client = APIClient(REMOTE_ADDR=ADDRESS)
    client.get(api_url("auth/session"))

    for _ in range(settings.AXES_FAILURE_LIMIT + 10):
        client.post(
            api_url("auth/login"), {"username": USERNAME, "password": "wrong"}, format="json"
        )

    counted = Event.objects.filter(
        kind__in=(kinds.AUTH_SIGN_IN_FAILED, kinds.AUTH_LOCKED_OUT)
    ).count()
    # The attempts that led up to the lockout, and the lockout. Not one of the
    # ten that followed it.
    assert counted == settings.AXES_FAILURE_LIMIT
    assert Event.objects.filter(kind=kinds.AUTH_LOCKED_OUT).count() == 1


# ------------------------------------------------------------------- reading


def test_the_list_answers_newest_first(api):
    for name in ("one", "two", "three"):
        api.post(api_url("clients"), {"name": name}, format="json")

    body = api.get(api_url("events")).json()

    assert [row["target"] for row in body["events"]] == ["three", "two", "one"]
    assert body["total"] == 3
    assert body["page"] == 1


def test_the_wire_shape_is_camel_case_and_the_detail_is_not(api):
    """`detail` is the one payload here whose keys are data rather than fields."""
    recorder.record(kinds.CLIENT_BULK_DELETED, target="", count=2)

    row = api.get(api_url("events")).json()["events"][0]

    assert set(row) == {"id", "at", "kind", "severity", "actor", "actorIp", "target", "detail"}
    assert row["kind"] == kinds.CLIENT_BULK_DELETED
    assert row["detail"] == {"count": 2}


def test_a_category_narrows_by_the_half_in_front_of_the_dot(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.post(api_url("server/restart"))

    body = api.get(api_url("events?category=server")).json()

    assert body["total"] == 1
    assert body["events"][0]["kind"] == kinds.SERVER_RESTARTED


def test_severity_narrows_to_what_is_worth_scanning_for(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.delete(api_url("clients/phone"))

    body = api.get(api_url("events?severity=warning")).json()

    assert [row["kind"] for row in body["events"]] == [kinds.CLIENT_DELETED]


def test_the_search_matches_the_target_and_the_actor(api):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.post(api_url("clients"), {"name": "laptop"}, format="json")

    by_target = api.get(api_url("events?q=lap")).json()
    assert [row["target"] for row in by_target["events"]] == ["laptop"]

    by_actor = api.get(api_url(f"events?q={USERNAME}")).json()
    assert by_actor["total"] == 2


def test_a_query_string_this_version_does_not_know_answers_the_first_page(api):
    """A stale bookmark deserves the log, not a 400 about a filter word."""
    api.post(api_url("clients"), {"name": "phone"}, format="json")

    response = api.get(api_url("events?category=banana&page=zero"))

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_paging_walks_backwards_through_the_log(api):
    for index in range(5):
        recorder.record(kinds.CLIENT_CREATED, target=f"client{index}")

    first = api.get(api_url("events?pageSize=2")).json()
    second = api.get(api_url("events?pageSize=2&page=2")).json()

    assert [row["target"] for row in first["events"]] == ["client4", "client3"]
    assert [row["target"] for row in second["events"]] == ["client2", "client1"]
    assert first["total"] == second["total"] == 5
    assert second["pageSize"] == 2


def test_both_logs_need_a_session():
    anonymous = APIClient()

    assert anonymous.get(api_url("events")).status_code == 401
    assert anonymous.get(api_url("logs")).status_code == 401


# ------------------------------------------------------------------ clearing


def test_clearing_takes_every_row_and_says_how_many(api):
    for name in ("one", "two", "three"):
        api.post(api_url("clients"), {"name": name}, format="json")

    response = api.delete(api_url("events"))

    assert response.status_code == 200, response.content
    assert response.json()["removed"] == 3


def test_clearing_leaves_a_row_saying_who_cleared_it(api):
    """A cleared log is a log with one line in it, never a blank one.

    That line is the whole reason this is not a silent delete: an audit trail
    that can be made to have never existed is not one. It carries the count as
    well, because "412 events were taken" is the only remaining fact about what
    was in there.
    """
    for name in ("one", "two"):
        api.post(api_url("clients"), {"name": name}, format="json")

    api.delete(api_url("events"))

    row = only(kinds.PANEL_EVENTS_CLEARED)
    assert row.detail == {"count": 2}
    assert row.actor == USERNAME
    assert row.actor_ip == ADDRESS
    assert row.severity == kinds.WARNING


def test_clearing_ignores_the_filters_the_list_was_read_with(api):
    """The one operation whose meaning must not depend on a search box.

    An operator who typed a client's name in to read that client's history is the
    last person who should find that pressing this took exactly those rows.
    """
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    api.post(api_url("server/restart"))

    api.delete(api_url("events?category=server&q=phone"))

    assert kinds_recorded() == [kinds.PANEL_EVENTS_CLEARED]


def test_clearing_an_empty_log_writes_nothing(api):
    """The same rule as a sweep that removed nothing: the button is not offered."""
    response = api.delete(api_url("events"))

    assert response.json()["removed"] == 0
    assert Event.objects.count() == 0


def test_a_token_may_read_the_log_but_not_empty_it(api, server_conf):
    """The credential line one step along from apps.accounts.permissions.

    A token is handed out expecting to be able to take it back, and that only
    works while the panel can be asked afterwards what it did. So a script keeps
    the read - the log is worth exporting - and the clear needs somebody at the
    login form.
    """
    recorder.record(kinds.CLIENT_CREATED, target="phone")
    admin = get_user_model().objects.get(username=USERNAME)
    _, secret = tokens.issue(admin, name="reader", lifetime=None, renew_on_use=False)
    bearer = APIClient(REMOTE_ADDR=ADDRESS, enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")

    assert bearer.get(api_url("events")).status_code == 200

    refused = bearer.delete(api_url("events"))
    assert refused.status_code == 403
    # The sentence as well as the code, so a 403 that came from somewhere else -
    # CSRF, a stale route - cannot pass for this rule being enforced.
    assert "API token" in refused.json()["detail"]
    assert Event.objects.filter(kind=kinds.CLIENT_CREATED).count() == 1


def test_clearing_needs_a_session_like_the_reads_do():
    anonymous = APIClient()

    assert anonymous.delete(api_url("events")).status_code == 401


# ------------------------------------------------------------------- pruning


def test_pruning_takes_what_has_aged_out_and_leaves_the_rest(api):
    recorder.record(kinds.CLIENT_CREATED, target="old")
    recorder.record(kinds.CLIENT_CREATED, target="new")
    Event.objects.filter(target="old").update(
        at=timezone.now() - timedelta(days=RETENTION_DAYS + 1)
    )

    assert recorder.prune() == 1
    assert [row.target for row in Event.objects.all()] == ["new"]


def test_the_ceiling_keeps_the_newest_rows(api):
    """The backstop against a flood, and during one the newest rows are the story."""
    recorder.record_many(kinds.CLIENT_CREATED, [f"c{index}" for index in range(MAX_ROWS + 5)])

    assert recorder.prune() == 5
    assert Event.objects.count() == MAX_ROWS
    # The oldest five went, so the newest of the survivors is the last recorded.
    assert Event.objects.order_by("id").first().target == "c5"


def test_pruning_an_ordinary_table_does_nothing(api):
    recorder.record(kinds.CLIENT_CREATED, target="phone")

    assert recorder.prune() == 0
    assert Event.objects.count() == 1


# --------------------------------------------------------------- service log


def journal_line(message: str, priority: int = 6, micros: object = 1_700_000_000_000_000) -> str:
    return json.dumps(
        {"MESSAGE": message, "PRIORITY": str(priority), "__REALTIME_TIMESTAMP": str(micros)}
    )


def fake_journalctl(monkeypatch, stdout: str, returncode: int = 0, stderr: str = ""):
    """Answer journalctl from a string, and hand back the command it was given."""
    seen: dict[str, list[str]] = {}

    def run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(journal.subprocess, "run", run)
    return seen


def test_the_journal_is_parsed_oldest_first_with_its_levels(api, monkeypatch):
    fake_journalctl(
        monkeypatch,
        "\n".join(
            [
                journal_line("collector started", micros=1_700_000_000_000_000),
                journal_line("cannot read the interface", priority=4, micros=1_700_000_060_000_000),
            ]
        ),
    )

    body = api.get(api_url("logs?source=collector")).json()

    assert body["available"] is True
    assert body["unit"] == "awg-panel-collector"
    assert [line["message"] for line in body["lines"]] == [
        "collector started",
        "cannot read the interface",
    ]
    assert [line["priority"] for line in body["lines"]] == [6, 4]
    assert body["lines"][0]["at"].startswith("2023-11-14T")


def test_a_line_that_will_not_parse_costs_only_that_line(api, monkeypatch):
    fake_journalctl(monkeypatch, "\n".join(["{not json", journal_line("still here")]))

    body = api.get(api_url("logs")).json()

    assert [line["message"] for line in body["lines"]] == ["still here"]


@pytest.mark.parametrize(
    "micros",
    [
        # Reads as an integer perfectly well and is no date at all: journald's
        # own field is microseconds, so a corrupt one lands hundreds of
        # thousands of years out and datetime says so with a ValueError.
        "99999999999999999999",
        # Past what the platform's time_t holds, which is an OverflowError.
        "1" + "0" * 30,
        # Large enough that dividing it to seconds overflows a float before
        # datetime is reached at all.
        "1" + "0" * 400,
        # The same thing from the other end.
        "-99999999999999999999",
    ],
)
def test_a_timestamp_that_is_no_date_costs_only_its_line(api, monkeypatch, micros):
    """The parse was guarded and the conversion was not.

    A number that `int()` reads happily can still be outside every date there
    is, and datetime says so in three different ways depending on how far out it
    is and which platform is asked. All of them used to leave _moment, pass
    straight through the per-line guard in _parse and come out of the endpoint
    as a 500 - so one unreadable entry took away the whole log at exactly the
    moment somebody had opened it to find out what was wrong.
    """
    fake_journalctl(
        monkeypatch,
        "\n".join([journal_line("bad clock", micros=micros), journal_line("still here")]),
    )

    response = api.get(api_url("logs"))

    assert response.status_code == 200
    body = response.json()
    assert [line["message"] for line in body["lines"]] == ["bad clock", "still here"]
    # No time against it rather than an invented one, which is what the UI
    # already draws for an entry journald gave no timestamp at all.
    assert body["lines"][0]["at"] is None
    assert body["lines"][1]["at"] is not None


def test_a_message_that_is_not_utf8_arrives_decoded(api, monkeypatch):
    """journald encodes such a message as bytes, and a service writing one is
    exactly the kind of thing somebody opens this tab to see."""
    raw = json.dumps({"MESSAGE": [104, 105, 255], "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1"})
    fake_journalctl(monkeypatch, raw)

    body = api.get(api_url("logs")).json()

    assert body["lines"][0]["message"].startswith("hi")


def test_the_source_names_a_unit_from_a_fixed_table(api, monkeypatch):
    """No unit name from a query string ever reaches journalctl."""
    seen = fake_journalctl(monkeypatch, "")

    body = api.get(api_url("logs?source=tunnel&lines=50")).json()

    assert body["unit"] == "awg-quick@awg0"
    assert "awg-quick@awg0" in seen["cmd"]
    assert "50" in seen["cmd"]


def test_an_unknown_source_is_answered_rather_than_run(api, monkeypatch):
    called: list[list[str]] = []
    monkeypatch.setattr(journal.subprocess, "run", lambda cmd, **kwargs: called.append(list(cmd)))

    body = api.get(api_url("logs?source=/etc/shadow")).json()

    assert body["available"] is False
    assert body["lines"] == []
    assert called == []


def test_the_line_count_is_capped_and_a_junk_one_defaults(api, monkeypatch):
    seen = fake_journalctl(monkeypatch, "")

    api.get(api_url("logs?lines=999999"))
    assert str(journal.MAX_LINES) in seen["cmd"]

    api.get(api_url("logs?lines=banana"))
    assert str(journal.DEFAULT_LINES) in seen["cmd"]


def test_a_server_without_journalctl_answers_rather_than_failing(api, monkeypatch):
    """A container has no systemd, and neither does the development tree. A 500
    here would read as the panel being broken at the moment somebody came to
    find out what was."""

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(journal.subprocess, "run", missing)

    body = api.get(api_url("logs")).json()

    assert body["available"] is False
    assert "journalctl" in body["reason"]


def test_journalctl_refusing_comes_back_as_its_own_sentence(api, monkeypatch):
    fake_journalctl(monkeypatch, "", returncode=1, stderr="Failed to add match: Invalid argument\n")

    body = api.get(api_url("logs")).json()

    assert body["available"] is False
    assert body["reason"] == "Failed to add match: Invalid argument"


def test_the_real_reader_never_raises_on_this_machine():
    """Whatever this box is, journal.read has to answer with a dict."""
    result = journal.read("panel", lines=1)

    assert set(result) == {"source", "unit", "lines", "available", "reason"}
    assert isinstance(result["lines"], list)
