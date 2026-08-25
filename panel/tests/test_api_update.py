"""Checking for a release, and starting an update from the panel.

Three separable things, tested apart because they fail apart.

`awg.release` decides what "newer" means and never touches the network here:
`fetch_release` is replaced, so what is under test is the ordering and the
refusals rather than GitHub's uptime. The pre-release rule gets the most
attention because it is the one the operator asked for in those words - only
stable releases - and because the ordering it needs is the part a naive
implementation gets backwards.

`apps.panel.update` only ever reads a file and starts a process. The file is
written by bin/awg-update, which is not installed on a machine running this
suite, so it is written by hand here in the shapes that tool produces - and in
a couple it should not, since the panel has to survive a state file from a
version of the updater that is older or newer than itself.

The endpoints are checked for who may reach them. Starting an update is closed
to API tokens for a reason no other route on that list shares: it replaces every
line of code on the server from a release fetched over the internet, so a leaked
token that could press it would choose which software the box runs next.
"""

import json
import os
import stat
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.panel import update as update_module
from awg import release

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"


def api_url(path: str) -> str:
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


@pytest.fixture
def client(account):
    api = APIClient(enforce_csrf_checks=True)
    api.force_authenticate(user=account)
    return api


@pytest.fixture
def token_client(account):
    """A caller holding an API token rather than a session cookie.

    Issued through the API the panel issues them with, so the token under test
    is the shape the panel actually mints. CSRF checking stays on, which is what
    makes the assertions mean anything: a request that only works because the
    token exempted it would otherwise be indistinguishable from one nobody
    checked.
    """
    admin = APIClient()
    admin.force_login(account)
    response = admin.post(
        f"{settings.BASE_PATH}api/v1/settings/tokens", {"name": "deploy"}, format="json"
    )
    assert response.status_code == 201, response.content
    api = APIClient(enforce_csrf_checks=True)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['secret']}")
    return api


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """Where the updater would have written its state, pointed at a tmpdir."""
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True)
    monkeypatch.setenv("AWG_PANEL_DATA", str(data))
    return data / "update"


def write_state(state_dir: Path, **fields: object) -> None:
    """One state file, as bin/awg-update would have left it.

    A "running" update gets this process's own pid unless the caller says
    otherwise, because that is what running means: the panel reads liveness out
    of /proc rather than trusting the word, so a state file claiming to be
    running with no live process behind it is one of the cases below rather than
    the ordinary one.
    """
    if fields.get("status") == "running":
        fields.setdefault("pid", str(os.getpid()))
    (state_dir / "state.json").write_text(json.dumps(fields), encoding="utf-8")


def dead_pid() -> str:
    """A pid that is not running. Forked and reaped, so it was real and is not."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        os._exit(0)
    os.waitpid(pid, 0)
    return str(pid)


def fake_tool(tmp_path: Path, monkeypatch, body: str = "exit 0") -> Path:
    """A stand-in for /usr/local/bin/awg-update that records its argv."""
    tool = tmp_path / "awg-update"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/calls\n{body}\n', encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv(update_module.ENV_TOOL, str(tool))
    return tool


def a_release(tag: str, **extra: object) -> dict:
    """One release in GitHub's shape, with the two assets a release publishes."""
    payload = {
        "tag_name": tag,
        "html_url": f"https://example.invalid/releases/tag/{tag}",
        "body": f"what changed in {tag}",
        "published_at": "2026-08-19T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": release.ASSET_BUNDLE,
                "browser_download_url": f"https://example.invalid/{tag}/{release.ASSET_BUNDLE}",
            },
            {
                "name": release.ASSET_SUMS,
                "browser_download_url": f"https://example.invalid/{tag}/{release.ASSET_SUMS}",
            },
        ],
    }
    payload.update(extra)
    return payload


# --------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("v1.1.0", "1.0.0", True),
        ("1.0.1", "1.0.0", True),
        ("2.0.0", "1.99.99", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "1.1.0", False),
        # 1.2 and 1.2.0 are the same version, not one short of the other.
        ("1.2", "1.2.0", False),
        ("1.2.0", "1.2", False),
        # The rule the old implementation had backwards. It read every run of
        # digits out of the tag, so 1.0.0-rc1 became (1, 0, 0, 1) and sorted
        # *above* 1.0.0 - every server on the release would have been offered
        # the candidate it was cut from.
        ("1.0.0-rc1", "1.0.0", False),
        ("1.0.0", "1.0.0-rc1", True),
        ("1.1.0-rc1", "1.0.0", True),
        # Inside a pre-release, a dot-separated numeric identifier is compared as
        # a number: rc.9 comes before rc.10 rather than after it.
        ("1.0.0-rc.10", "1.0.0-rc.9", True),
        # "rc10" is one alphanumeric identifier, not a word and a number, and
        # semver compares those as text - so rc10 sorts *below* rc9. Asserted
        # rather than left as a surprise, because it looks like a bug and is
        # not one. It also costs nothing: a pre-release is never installed.
        ("1.0.0-rc10", "1.0.0-rc9", False),
        # Nothing orderable in either position offers nothing.
        ("nightly", "1.0.0", False),
        ("1.0.1", "not-a-version", False),
        ("", "1.0.0", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert release.is_newer(latest, current) is expected


def test_build_metadata_is_ignored():
    """+build is not part of the version, so it cannot make one newer."""
    assert release.is_newer("1.0.0+abc", "1.0.0") is False
    assert release.is_newer("1.0.0", "1.0.0+abc") is False


# ------------------------------------------------------------------ check


def test_check_offers_a_newer_stable_release(monkeypatch):
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: a_release("v1.1.0"))
    answer = release.check("1.0.0")

    assert answer["checked"] is True
    assert answer["latest"] == "v1.1.0"
    assert answer["update_available"] is True
    assert answer["notes"] == "what changed in v1.1.0"
    # The two URLs an update needs, resolved from the release's own asset list
    # rather than built out of a template, so a release published by hand under
    # different names is a release this can say is not installable.
    assert answer["asset"].endswith(release.ASSET_BUNDLE)
    assert answer["sums"].endswith(release.ASSET_SUMS)


def test_check_says_nothing_is_newer(monkeypatch):
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: a_release("v1.0.0"))
    answer = release.check("1.0.0")

    assert answer["checked"] is True
    assert answer["update_available"] is False


def test_the_asset_is_resolved_even_when_it_is_not_an_upgrade(monkeypatch):
    """The answer `awg-update apply --force` reinstalls from.

    These two used to be filled in only when an upgrade was on offer, so the one
    run that needs them most had them empty: --force reinstalls the release
    already on the server, which is the documented repair for an install that
    stopped half way and the only route back for a server whose VERSION is
    already the new one. It failed every time, blaming the release for carrying
    no installer.
    """
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: a_release("v1.0.0"))
    answer = release.check("1.0.0")

    assert answer["update_available"] is False
    assert answer["asset"].endswith(release.ASSET_BUNDLE)
    assert answer["sums"].endswith(release.ASSET_SUMS)
    # And it is not called out of date for a reason that does not apply to it.
    assert release.ASSET_BUNDLE not in answer["reason"]


@pytest.mark.parametrize(
    "payload",
    [
        a_release("v2.0.0", prerelease=True),
        a_release("v2.0.0", draft=True),
        # The flag says stable and the tag says otherwise. Both are checked,
        # because a mirrored feed is written by hand.
        a_release("v2.0.0-rc1"),
    ],
    ids=["flagged-prerelease", "draft", "prerelease-tag"],
)
def test_check_never_offers_a_prerelease(monkeypatch, payload):
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: payload)
    answer = release.check("1.0.0")

    assert answer["update_available"] is False
    assert "pre-release" in answer["reason"]


def test_check_refuses_a_release_with_no_installer(monkeypatch):
    """A release cut by hand carries no bundle, and there is nothing to install."""
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: a_release("v1.1.0", assets=[]))
    answer = release.check("1.0.0")

    assert answer["update_available"] is True
    assert answer["asset"] == ""
    assert release.ASSET_BUNDLE in answer["reason"]


def test_check_survives_an_unreachable_feed(monkeypatch):
    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("Network is unreachable")

    monkeypatch.setattr(release, "fetch_release", unreachable)
    answer = release.check("1.0.0")

    # Not an exception and not an error status. A server behind a strict egress
    # policy is the normal case, and it is not a broken panel.
    assert answer["checked"] is False
    assert answer["update_available"] is False
    assert "could not be reached" in answer["reason"]
    assert answer["current"] == "1.0.0"


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (404, "no release yet"),
        (403, "rate-limiting"),
        (429, "rate-limiting"),
        (500, "answered 500"),
    ],
)
def test_check_explains_the_http_answer(monkeypatch, code, fragment):
    def refused(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", code, "no", {}, None)

    monkeypatch.setattr(release, "fetch_release", refused)
    assert fragment in release.check("1.0.0")["reason"]


def test_a_list_feed_yields_the_newest_stable_entry(monkeypatch):
    """/releases answers with a list; the newest publishable entry is picked out."""
    listing = [
        a_release("v1.0.0"),
        a_release("v2.0.0", prerelease=True),
        a_release("v1.3.0"),
        a_release("v9.9.9", draft=True),
    ]

    class Response:
        def read(self, _size):
            return json.dumps(listing).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: Response())
    assert release.fetch_release("https://example.invalid/releases")["tag_name"] == "v1.3.0"


def test_a_fork_can_repoint_the_check(monkeypatch):
    monkeypatch.setenv(release.ENV_REPO, "someone/their-fork")
    assert release.release_url() == release.RELEASE_URL.format(repo="someone/their-fork")

    # A whole URL wins, for an operator behind a mirror.
    monkeypatch.setenv(release.ENV_URL, "https://mirror.invalid/latest.json")
    assert release.release_url() == "https://mirror.invalid/latest.json"


def test_the_default_repository_is_used_when_nothing_says_otherwise(monkeypatch):
    """An unset variable means the project's own releases, not a check switched off.

    This is the whole reason the check works out of the box. Requiring the
    variable meant every stock install had updates disabled until somebody
    edited a file they had never been told about.
    """
    monkeypatch.delenv(release.ENV_REPO, raising=False)
    monkeypatch.delenv(release.ENV_URL, raising=False)
    assert release.release_url() == release.RELEASE_URL.format(repo=release.DEFAULT_REPO)


def test_a_nonsense_repository_disables_the_check(monkeypatch):
    """Which is how an operator turns it off: name something that is not a repo."""
    monkeypatch.setenv(release.ENV_REPO, "not a repository")
    monkeypatch.delenv(release.ENV_URL, raising=False)
    assert release.release_url() == ""
    assert "No release source" in release.check("1.0.0")["reason"]


def test_a_plaintext_feed_url_is_ignored(monkeypatch):
    """http:// is not a release feed. The bytes are executed as root afterwards."""
    monkeypatch.setenv(release.ENV_URL, "http://mirror.invalid/latest.json")
    monkeypatch.setenv(release.ENV_REPO, "someone/their-fork")
    assert release.release_url() == release.RELEASE_URL.format(repo="someone/their-fork")


# ----------------------------------------------------------------- status


def test_status_with_nothing_ever_run(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    answer = update_module.status()

    assert answer["status"] == "idle"
    assert answer["log"] == ""
    assert answer["unavailable"] == ""


def test_status_reads_what_the_updater_wrote(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    write_state(
        state_dir,
        status="running",
        phase="install",
        detail="Installing 1.1.0 - this takes a few minutes",
        from_version="1.0.0",
        to_version="1.1.0",
        started="2026-08-19T09:12:04Z",
        backup="/var/lib/awg-panel/backups/awg-backup-20260819-091205.tar.gz",
    )
    (state_dir / "log").write_text("==> building\n==> installing\n", encoding="utf-8")

    answer = update_module.status()
    assert answer["status"] == "running"
    assert answer["phase"] == "install"
    assert answer["from_version"] == "1.0.0"
    assert answer["to_version"] == "1.1.0"
    assert answer["backup"].endswith(".tar.gz")
    assert "installing" in answer["log"]


@pytest.mark.parametrize(
    "content",
    ["", "not json at all", "[]", '{"status": "somersault"}'],
    ids=["empty", "garbage", "a-list", "an-unknown-status"],
)
def test_a_state_file_the_panel_cannot_read_is_idle(state_dir, tmp_path, monkeypatch, content):
    """Never an exception. This endpoint is polled every two seconds by a page
    watching the server reinstall itself, and the one thing it must not do is
    turn a damaged file into "the update failed"."""
    fake_tool(tmp_path, monkeypatch)
    (state_dir / "state.json").write_text(content, encoding="utf-8")

    assert update_module.status()["status"] == "idle"


def test_a_state_file_from_another_version_keeps_its_shape(state_dir, tmp_path, monkeypatch):
    """Fields the panel does not know are dropped; fields it needs are supplied.

    The updater is a shell script on the same server and can be a version ahead
    of this code - which is exactly what it is during the run that installs the
    new one.
    """
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="succeeded", something_new="42")

    answer = update_module.status()
    assert answer["status"] == "succeeded"
    assert "something_new" not in answer
    assert answer["phase"] == ""
    assert answer["detail"] == ""


def test_the_log_tail_is_bounded(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    (state_dir / "log").write_text("".join(f"line {n}\n" for n in range(5000)), encoding="utf-8")

    tail = update_module.read_log()
    assert tail.count("\n") < update_module.LOG_TAIL_LINES
    # The end of the file, which is where an update that is still going is.
    assert tail.endswith("line 4999")


def test_a_worker_that_died_is_not_still_running(state_dir, tmp_path, monkeypatch):
    """The one thing the state file cannot say for itself.

    A worker killed by the OOM killer, or by a `systemctl stop`, never reaches
    its own exit trap - so the file it leaves behind says "running" and would say
    it for the rest of the server's life, with the panel's progress bar spinning
    above it.
    """
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="running", phase="install", pid=dead_pid())

    answer = update_module.status()
    assert answer["status"] == "failed"
    assert "stopped before it finished" in answer["detail"]


def test_the_moment_after_the_button_is_pressed_is_not_a_failure(state_dir, tmp_path, monkeypatch):
    """`start` writes the state before the worker exists to record itself in it,
    and the page's first poll lands inside that window."""
    fake_tool(tmp_path, monkeypatch)
    write_state(
        state_dir,
        status="running",
        phase="starting",
        pid="",
        started=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    assert update_module.status()["status"] == "running"


def test_a_launch_that_never_happened_is_noticed(state_dir, tmp_path, monkeypatch):
    """The same window, an hour later. Nothing is coming."""
    fake_tool(tmp_path, monkeypatch)
    stale = datetime.now(UTC) - timedelta(hours=1)
    write_state(
        state_dir,
        status="running",
        phase="starting",
        pid="",
        started=stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    assert update_module.status()["status"] == "failed"


def test_a_state_file_from_a_previous_boot_is_never_running(state_dir, tmp_path, monkeypatch):
    """The case that used to wedge the whole feature.

    A worker lost to a hard reset or the OOM killer leaves "running" with a pid
    in it. Pids are reissued from the bottom after a reboot, so that number is
    somebody else's a few minutes later - and every reader believed the update
    was still going: the progress bar never stopped, the button stayed on 409,
    and `awg-update cancel` refused because it asked the same question. The boot
    id settles it without having to guess at the pid at all.
    """
    fake_tool(tmp_path, monkeypatch)
    write_state(
        state_dir,
        status="running",
        phase="install",
        pid=str(os.getpid()),  # alive, and emphatically not the updater
        boot="00000000-0000-0000-0000-000000000000",
    )

    assert update_module.status()["status"] == "failed"


def test_a_reused_pid_within_one_boot_is_not_the_worker(state_dir, tmp_path, monkeypatch):
    """The same mistake inside a single uptime, which the boot id cannot catch.

    The kernel will not hand out a pid with the same start time twice, so the
    two together identify the process the pid on its own only gestures at.
    """
    fake_tool(tmp_path, monkeypatch)
    write_state(
        state_dir,
        status="running",
        phase="install",
        pid=str(os.getpid()),
        boot=update_module._boot_id(),
        pid_start="1",  # this process started at some other time than tick 1
    )

    assert update_module.status()["status"] == "failed"


def test_the_running_worker_is_left_alone(state_dir, tmp_path, monkeypatch):
    """And the checks above do not call a live update dead."""
    fake_tool(tmp_path, monkeypatch)
    write_state(
        state_dir,
        status="running",
        phase="install",
        pid=str(os.getpid()),
        boot=update_module._boot_id(),
        pid_start=update_module._proc_started(str(os.getpid())),
    )

    assert update_module.status()["status"] == "running"


def test_a_state_file_without_the_new_fields_is_read_the_old_way(state_dir, tmp_path, monkeypatch):
    """Written by the awg-update a server is upgrading *from*, which recorded
    neither. An absent answer is not a failed one."""
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="running", phase="install", pid=str(os.getpid()))

    assert update_module.status()["status"] == "running"


def test_a_finished_update_is_left_alone(state_dir, tmp_path, monkeypatch):
    """Liveness is only ever consulted about a run that claims to be going."""
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="succeeded", phase="done", pid=dead_pid(), detail="Updated.")

    answer = update_module.status()
    assert answer["status"] == "succeeded"
    assert answer["detail"] == "Updated."


def test_a_panel_without_the_updater_says_so(state_dir, monkeypatch, tmp_path):
    monkeypatch.setenv(update_module.ENV_TOOL, str(tmp_path / "nothing-here"))
    assert "cannot update itself" in update_module.status()["unavailable"]


def test_an_updater_that_is_not_executable_says_so(state_dir, monkeypatch, tmp_path):
    tool = tmp_path / "awg-update"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(stat.S_IRUSR)
    monkeypatch.setenv(update_module.ENV_TOOL, str(tool))

    assert "not executable" in update_module.status()["unavailable"]


# ------------------------------------------------------------------ start


def test_start_hands_the_work_to_the_updater(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    update_module.start()

    assert (tmp_path / "calls").read_text(encoding="utf-8").strip() == "start"


def test_start_passes_force_through(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    update_module.start(force=True)

    assert (tmp_path / "calls").read_text(encoding="utf-8").strip() == "start --force"


def test_start_refuses_while_one_is_running(state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="running", phase="install")

    with pytest.raises(Exception, match="already running"):
        update_module.start()
    assert not (tmp_path / "calls").exists()


def test_start_reports_the_updaters_own_refusal(state_dir, tmp_path, monkeypatch):
    """Its sentence, not one invented here: it knows which precondition failed."""
    fake_tool(tmp_path, monkeypatch, body='echo "no release helper on this server" >&2\nexit 1')

    with pytest.raises(Exception, match="no release helper"):
        update_module.start()


def test_start_refuses_without_an_updater(state_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(update_module.ENV_TOOL, str(tmp_path / "nothing-here"))

    with pytest.raises(Exception, match="cannot update itself"):
        update_module.start()


# -------------------------------------------------------------- endpoints


def test_status_endpoint_answers_the_state_file(client, state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="succeeded", from_version="1.0.0", to_version="1.1.0")

    response = client.get(api_url("update/status"))
    assert response.status_code == 200
    # camelCase on the way out, like every other payload the browser sees.
    assert response.json()["fromVersion"] == "1.0.0"
    assert response.json()["toVersion"] == "1.1.0"
    assert response.json()["status"] == "succeeded"


def test_a_token_may_watch_an_update(token_client, state_dir, tmp_path, monkeypatch):
    """Reading how a deployment went is a fair thing for the script that started it."""
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="running")

    assert token_client.get(api_url("update/status")).status_code == 200


def test_a_token_may_not_start_one(token_client, state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)

    response = token_client.post(api_url("update/apply"), {}, format="json")
    assert response.status_code == 403
    # And nothing was started before the permission was checked.
    assert not (tmp_path / "calls").exists()


def test_apply_starts_the_update_and_answers_at_once(client, state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)

    response = client.post(api_url("update/apply"), {}, format="json")
    assert response.status_code == 200
    assert (tmp_path / "calls").read_text(encoding="utf-8").strip() == "start"


def test_apply_records_who_started_it(client, state_dir, tmp_path, monkeypatch):
    from apps.events.models import Event

    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="idle", from_version="1.0.0", to_version="")

    client.post(api_url("update/apply"), {}, format="json")

    event = Event.objects.filter(kind="panel.update-started").first()
    assert event is not None
    assert event.actor == USERNAME


def test_apply_refuses_a_second_update(client, state_dir, tmp_path, monkeypatch):
    fake_tool(tmp_path, monkeypatch)
    write_state(state_dir, status="running", phase="download")

    response = client.post(api_url("update/apply"), {}, format="json")
    # 409, the panel's own status for "this collides with what is already
    # happening", rather than a 400 about a request that was perfectly formed.
    assert response.status_code == 409
    assert "already running" in json.dumps(response.json())


def test_check_endpoint_never_fails_on_the_network(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("Network is unreachable")

    monkeypatch.setattr(release, "fetch_release", unreachable)

    response = client.post(api_url("update/check"), {}, format="json")
    assert response.status_code == 200
    assert response.json()["checked"] is False
    assert response.json()["updateAvailable"] is False


def test_check_endpoint_reports_the_installed_version(client, monkeypatch):
    monkeypatch.setattr(release, "fetch_release", lambda *a, **k: a_release("v99.0.0"))

    body = client.post(api_url("update/check"), {}, format="json").json()
    assert body["current"] == settings.PANEL_VERSION
    assert body["latest"] == "v99.0.0"
    assert body["updateAvailable"] is True
    # The download URLs are the updater's business and are not sent to a browser
    # that never downloads anything.
    assert "asset" not in body and "sums" not in body


# ------------------------------------------------------- the shipped tool


def test_the_updater_is_shipped_executable():
    """bin/awg-update is installed with `install -m 755`, but a mode of 644 in
    the tree is still a bundle that unpacks a file nothing can run - and the
    release workflow asserts on the bit rather than fixing it."""
    tool = Path(__file__).resolve().parents[2] / "bin" / "awg-update"
    assert tool.is_file()
    assert os.access(tool, os.X_OK)


def test_the_updater_and_the_panel_agree_on_the_default_tool_path():
    """The path apps.panel.update runs is the path install.sh writes."""
    installer = (Path(__file__).resolve().parents[2] / "install.sh").read_text(encoding="utf-8")
    assert update_module.DEFAULT_TOOL == "/usr/local/bin/awg-update"
    assert "awg-menu awg-uninstall awg-update" in installer
