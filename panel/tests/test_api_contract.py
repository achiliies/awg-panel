"""The SPA's TypeScript types against what the API actually returns.

`tsc` checks the frontend against itself and pytest checks the backend against
itself; nothing checks them against each other. A renamed serializer field is
invisible to both and shows up as an empty column in the browser, so this test
reads `frontend/src/api/types.ts` and asserts that every field the SPA expects
is really present in a live response.

It is deliberately one-directional: extra keys in a response are fine (the API
may serve more than this SPA reads), missing ones are not.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db

TYPES = Path(settings.BASE_DIR) / "frontend" / "src" / "api" / "types.ts"

# `  fieldName?: type` / `  fieldName: type` at one indent level inside a block,
# capturing the `?` so an optional member can be told from a required one.
_FIELD_RE = re.compile(r"^\s{2}([a-zA-Z_][a-zA-Z0-9_]*)(\??)\s*:", re.MULTILINE)


def _interface(name: str) -> set[str]:
    """Fields declared directly in `interface <name> { ... }` and not marked optional.

    Nested object literals are indented further, so anchoring on exactly two
    spaces keeps this to the interface's own members.

    `?` is the SPA saying a response may legitimately arrive without the field,
    which leaves this test nothing to assert: `Client.dns` is served on a single
    client and deliberately not on a list row, so demanding it of every response
    would fail the one that is behaving exactly as designed. Where an optional
    field does have to be present somewhere, the test for that endpoint says so
    - see test_api_clients.py for dns.
    """
    source = TYPES.read_text(encoding="utf-8")
    match = re.search(
        rf"(?:export\s+)?(?:interface|type)\s+{re.escape(name)}\b[^{{]*{{(.*?)^}}",
        source,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        pytest.skip(f"{name} is not declared in types.ts")
    return {field for field, optional in _FIELD_RE.findall(match.group(1)) if not optional}


@pytest.fixture
def ui(client, server_conf):
    """A logged-in session against a config the mock controller can serve."""
    user = get_user_model().objects.create_user(username="admin", password="testpass12345")
    client.force_login(user)
    return client


def _get(ui, path: str):
    response = ui.get(f"{settings.BASE_PATH}api/v1/{path}")
    assert response.status_code == 200, (
        f"{path} -> {response.status_code}: {response.content[:200]}"
    )
    return json.loads(response.content)


def _assert_covers(declared: set[str], actual: dict, what: str) -> None:
    missing = sorted(declared - set(actual))
    assert not missing, (
        f"{what}: the SPA reads {missing} but the API does not send them. Sent: {sorted(actual)}"
    )


@pytest.mark.skipif(not TYPES.is_file(), reason="frontend/src/api/types.ts is not present")
class TestTypesMatchResponses:
    def test_session(self, ui):
        _assert_covers(_interface("Session"), _get(ui, "auth/session"), "Session")

    def test_clients_envelope_and_row(self, ui):
        payload = _get(ui, "clients")
        _assert_covers(_interface("ClientsResponse"), payload, "ClientsResponse")
        assert payload["clients"], "the mock bootstrap should give us at least one client"
        _assert_covers(_interface("Client"), payload["clients"][0], "Client")

    def test_server_config(self, ui):
        _assert_covers(_interface("ServerConfig"), _get(ui, "server"), "ServerConfig")

    def test_server_status(self, ui):
        _assert_covers(_interface("ServerStatus"), _get(ui, "server/status"), "ServerStatus")

    def test_param_spec(self, ui):
        params = _get(ui, "server/params")
        rows = params["params"] if isinstance(params, dict) else params
        assert rows, "the parameter catalog drives the whole Server page"
        declared = _interface("ParamSpec")
        # Optional bounds legitimately vary per parameter, so check the union
        # across the catalog rather than demanding every key on every row.
        union: set[str] = set()
        for row in rows:
            union |= set(row)
        _assert_covers(declared, dict.fromkeys(union), "ParamSpec")

    def test_live_stats(self, ui):
        live = _get(ui, "stats/live")
        _assert_covers(_interface("LiveStats"), live, "LiveStats")
        _assert_covers(_interface("SystemStats"), live["system"], "SystemStats")
        if live.get("peers"):
            peer = next(iter(live["peers"].values()))
            _assert_covers(_interface("LivePeer"), peer, "LivePeer")

    def test_stats_summary(self, ui):
        _assert_covers(_interface("StatsSummary"), _get(ui, "stats/summary"), "StatsSummary")

    def test_traffic_history(self, ui):
        """Both endpoints, because the SPA draws them with one component.

        A field renamed on one of them is not a broken chart - it is one of the
        two charts silently reading zeroes, which is exactly the failure this
        file exists to catch.
        """
        for path in ("stats/traffic", "clients/client1/traffic"):
            body = _get(ui, path)
            _assert_covers(_interface("TrafficHistory"), body, f"TrafficHistory ({path})")
            for series in ("daily", "monthly"):
                assert body[series], f"{path}: {series} came back empty"
                _assert_covers(
                    _interface("TrafficPoint"), body[series][0], f"TrafficPoint ({path}.{series})"
                )

    def test_traffic_reset(self, ui):
        """The one write in this file, and it earns the exception.

        Everything the SPA can say after a wipe comes out of this answer - the
        page it is on knows only that there is nothing left - so a field renamed
        here is a confirmation line with a hole in it, on the one screen where
        the reader most wants to be told what just happened.
        """
        response = ui.post(f"{settings.BASE_PATH}api/v1/stats/traffic/reset")
        assert response.status_code == 200, response.content
        _assert_covers(
            _interface("TrafficResetResult"),
            json.loads(response.content),
            "TrafficResetResult",
        )

    def test_settings(self, ui):
        _assert_covers(_interface("Settings"), _get(ui, "settings"), "Settings")

    def test_api_token(self, ui):
        """Both shapes, because the secret is only ever in one of them.

        The row the list serves and the envelope the create call answers with are
        different types on purpose, and the one that carries the secret is shown
        to somebody exactly once - so a field renamed out from under it is not a
        blank column, it is a token nobody can use and no way to find out why.
        """
        created = ui.post(
            f"{settings.BASE_PATH}api/v1/settings/tokens",
            data=json.dumps({"name": "contract", "expiresIn": 30 * 86400}),
            content_type="application/json",
        )
        assert created.status_code == 201, created.content
        issued = json.loads(created.content)
        _assert_covers(_interface("ApiTokenIssued"), issued, "ApiTokenIssued")
        _assert_covers(_interface("ApiToken"), issued["token"], "ApiToken")

        rows = _get(ui, "settings/tokens")["tokens"]
        assert rows, "the token just created should be in the list"
        _assert_covers(_interface("ApiToken"), rows[0], "ApiToken")
