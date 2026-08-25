"""The API's description of itself, against the API.

`apps/panel/apidocs.py` is prose and examples written by hand, which is the only
way to get a description worth reading - and hand-written prose about routes is
exactly the thing that quietly stops being true. So the load-bearing test here
is not that the document parses: it is that the set of operations in the catalog
is the set of routes in the URL map, in both directions.

An endpoint added without an entry fails this file. An entry left behind by an
endpoint that was removed fails it too, which is the half that would otherwise
never be noticed: a route that no longer exists still reads perfectly well.

The rest is what a caller is entitled to assume of the document - that every
operation has words on it, that the ids are unique because a generator makes
method names out of them, that the security it declares matches what the panel
actually refuses, and that `servers` names the panel it came from rather than
some address written down at build time.
"""

import json

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from apps.accounts import tokens
from apps.panel import apidocs

pytestmark = pytest.mark.django_db

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"

# Routes that exist but are not operations to document.
#
# `health/` is the same endpoint as `health` under its other spelling, which the
# document mentions in prose rather than as a second entry. The SPA fallback is
# not an API route at all: it is the regex that serves index.html to every
# client-side path.
UNDOCUMENTED = {"health/"}


def api_url(path: str) -> str:
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(USERNAME, password=PASSWORD)


@pytest.fixture
def api(admin) -> APIClient:
    client = APIClient(REMOTE_ADDR="203.0.113.10")
    client.force_login(admin)
    return client


def registered_routes() -> set[tuple[str, str]]:
    """Every (method, path) the URL map serves under api/v1/, as a shape to compare.

    Both sides are reduced to the same spelling by `_shape`: Django writes a
    parameter as `<uuid:ident>` and the document writes it as `{id}`, and the
    names are allowed to differ - the URL map avoids `id` because it shadows a
    builtin, and the document uses the name the JSON field has. What must match
    is which segments are parameters and where, so the name is erased and the
    position kept. A parameter the document forgot to declare is caught by
    `test_a_path_parameter_is_declared_wherever_one_is_used` instead.

    Methods come from the view class rather than from the pattern, because the
    URL map does not carry them - `clients/<name>` is one route and three
    operations.
    """
    found: set[tuple[str, str]] = set()
    prefix = f"{settings.BASE_PATH.lstrip('/')}api/v1/"

    def walk(patterns, so_far: str) -> None:
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, so_far + str(entry.pattern))
                continue
            if not isinstance(entry, URLPattern):
                continue
            path = so_far + str(entry.pattern)
            if not path.startswith(prefix):
                continue
            route = path[len(prefix) :]
            if route in UNDOCUMENTED:
                continue
            for method in _methods(entry.callback):
                found.add((method, _shape(route)))

    walk(get_resolver().url_patterns, "")
    return found


def _shape(route: str) -> str:
    """`clients/<str:name>` and `clients/{name}` both -> `clients/{}`."""
    parts = []
    for part in route.split("/"):
        parameter = (part.startswith("<") and part.endswith(">")) or (
            part.startswith("{") and part.endswith("}")
        )
        parts.append("{}" if parameter else part)
    return "/".join(parts)


def _methods(callback) -> set[str]:
    """The HTTP methods a view actually implements.

    DRF's `.as_view()` hangs the class off the function it returns, so the
    handlers can be read straight off it. `health` is a plain Django function
    view with no class behind it, and the only one in this map: it reads, so it
    is a GET.
    """
    view = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
    if view is None:
        return {"get"}
    allowed = getattr(view, "http_method_names", [])
    return {method for method in allowed if method != "options" and hasattr(view, method)}


def documented_routes() -> set[tuple[str, str]]:
    return {(op.method, _shape(op.path)) for op in apidocs.CATALOG}


# ------------------------------------------------------------------- drift


def test_every_route_the_panel_serves_is_in_the_catalog():
    missing = registered_routes() - documented_routes()
    assert not missing, (
        f"these routes are served but not described in apps/panel/apidocs.py: {sorted(missing)}"
    )


def test_the_catalog_describes_nothing_the_panel_does_not_serve():
    stale = documented_routes() - registered_routes()
    assert not stale, (
        "these entries in apps/panel/apidocs.py name routes the panel no longer serves: "
        f"{sorted(stale)}"
    )


def test_operation_ids_are_unique():
    ids = [op.id for op in apidocs.CATALOG]
    assert len(ids) == len(set(ids))


def test_every_operation_says_what_it_does():
    for op in apidocs.CATALOG:
        assert op.summary, f"{op.id} has no summary"
        assert op.description, f"{op.id} has no description"
        # The summary is a heading in the panel's own reference and a comment in
        # a generated client. A sentence ending in a full stop reads as neither.
        assert not op.summary.endswith("."), f"{op.id}'s summary is a sentence"
        assert op.tag in {name for name, _ in apidocs.TAGS}, f"{op.id} has an unknown tag"


def test_a_path_parameter_is_declared_wherever_one_is_used():
    for op in apidocs.CATALOG:
        wanted = {
            part[1:-1] for part in op.path.split("/") if part.startswith("{") and part.endswith("}")
        }
        declared = {param.name for param in op.params if param.where == "path"}
        assert wanted == declared, f"{op.id} declares {declared} for a path taking {wanted}"


# ---------------------------------------------------------------- document


def test_the_document_is_shaped_like_openapi(api):
    body = api.get(api_url("openapi.json")).json()

    assert body["openapi"].startswith("3.1")
    assert body["info"]["version"] == settings.PANEL_VERSION
    assert body["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    # One path object per distinct path, with the methods hanging off it.
    assert body["paths"]["/clients"]["get"]["operationId"] == "clientList"
    assert body["paths"]["/clients"]["post"]["operationId"] == "clientCreate"
    assert body["paths"]["/clients/{name}"]["delete"]["responses"]["204"]


def test_the_one_upload_is_declared_as_an_upload(api):
    """`POST restore` takes a file, and the page builds `curl -F` from that.

    Declared as JSON it would be rendered with `-d`, which posts the example
    string as a body and is refused - a command that cannot work is worse to
    hand somebody than none.
    """
    body = api.get(api_url("openapi.json")).json()

    content = body["paths"]["/restore"]["post"]["requestBody"]["content"]
    assert list(content) == ["multipart/form-data"]
    assert content["multipart/form-data"]["schema"]["properties"]["file"]["format"] == "binary"

    # And nothing else claims to be one.
    for path, methods in body["paths"].items():
        for method, operation in methods.items():
            if path == "/restore":
                continue
            declared = operation.get("requestBody", {}).get("content", {})
            assert "multipart/form-data" not in declared, f"{method} {path}"


def test_the_document_names_the_panel_it_came_from(api):
    body = api.get(api_url("openapi.json")).json()

    url = body["servers"][0]["url"]
    # Absolute, so an import into a tool needs no editing, and carrying the
    # base path, so it works on a panel mounted under a secret prefix.
    assert url.startswith("http://")
    assert url.endswith(f"{settings.BASE_PATH}api/v1")


def test_the_document_is_json_a_tool_can_read(api):
    response = api.get(api_url("openapi.json"))

    assert response["Content-Type"].startswith("application/json")
    json.dumps(response.json())


def test_credential_routes_are_declared_session_only(api):
    body = api.get(api_url("openapi.json")).json()

    # What the document promises...
    tokens_get = body["paths"]["/settings/tokens"]["get"]
    assert tokens_get["security"] == [{"sessionAuth": []}]
    clients_get = body["paths"]["/clients"]["get"]
    assert {"bearerAuth": []} in clients_get["security"]


def test_what_the_document_promises_is_what_the_panel_enforces(api, admin):
    """Every operation declared token-friendly is one a token is really allowed.

    Checked against a live request rather than against the permission classes,
    and only for the reads: a document that says a script may call something the
    panel then refuses is worse than no document, and this is the assertion that
    catches a route whose permissions changed under an entry that did not.
    """
    _, secret = tokens.issue(admin, name="reader", lifetime=None, renew_on_use=False)
    bearer = APIClient(REMOTE_ADDR="198.51.100.7", enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")

    for op in apidocs.CATALOG:
        if op.method != "get" or "{" in op.path or op.auth == apidocs.AUTH_NONE:
            continue
        response = bearer.get(api_url(op.path))
        refused = response.status_code in {401, 403}
        if op.auth == apidocs.AUTH_SESSION:
            assert refused, (
                f"{op.id} is documented session-only and answered {response.status_code}"
            )
        else:
            assert not refused, f"{op.id} is documented as open to a token and answered 403"


def test_the_description_needs_a_credential_of_some_kind():
    anonymous = APIClient()
    assert anonymous.get(api_url("openapi.json")).status_code == 401


def test_a_token_may_read_the_description(api, admin):
    """The caller most likely to want it is a script being written against it."""
    _, secret = tokens.issue(admin, name="deploy", lifetime=None, renew_on_use=False)
    bearer = APIClient(REMOTE_ADDR="198.51.100.7", enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")

    response = bearer.get(api_url("openapi.json"))

    assert response.status_code == 200
    assert response.json()["paths"]["/clients"]["get"]


# ----------------------------------------------------------- self-identity


def test_a_session_carries_no_token_in_its_payload(api):
    assert api.get(api_url("auth/session")).json()["token"] is None


def test_a_token_learns_which_credential_it_is_using(api, admin):
    """The one thing a script can ask about its own credential, and why.

    The token list is closed to tokens and stays closed. But a job that runs at
    three in the morning has to be able to find out that the secret it holds
    runs out on Friday, and the alternative to answering here is that it finds
    out by failing.
    """
    _, secret = tokens.issue(admin, name="nightly backup", lifetime=30 * 86400, renew_on_use=True)
    bearer = APIClient(REMOTE_ADDR="198.51.100.7", enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")

    body = bearer.get(api_url("auth/session")).json()

    assert body["authenticated"] is True
    assert body["token"]["name"] == "nightly backup"
    assert body["token"]["renewOnUse"] is True
    assert body["token"]["expiresAt"]
    # Its own row and nothing more: not the hint, not the address, and above all
    # not any other token this account holds.
    assert set(body["token"]) == {"name", "expiresAt", "renewOnUse"}
    assert secret not in json.dumps(body)


def test_a_token_that_never_expires_says_so_rather_than_omitting_the_field(api, admin):
    _, secret = tokens.issue(admin, name="forever", lifetime=None, renew_on_use=False)
    bearer = APIClient(REMOTE_ADDR="198.51.100.7", enforce_csrf_checks=True)
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")

    body = bearer.get(api_url("auth/session")).json()

    assert body["token"]["expiresAt"] is None
    assert body["token"]["renewOnUse"] is False


def test_an_anonymous_caller_is_told_about_no_token():
    body = APIClient().get(api_url("auth/session")).json()

    assert body["authenticated"] is False
    assert body["token"] is None


# ----------------------------------------------------------------- the page


@pytest.mark.skipif(
    not (settings.FRONTEND_DIST / "index.html").is_file(),
    reason="frontend/dist is not built in this checkout",
)
def test_the_api_page_survives_a_hard_refresh(client):
    """`api-docs`, and not `api`, is what makes this pass.

    Every path beginning `api/` is excluded from the SPA fallback so that a
    missing bundle 404s as a missing bundle rather than as HTML with a
    JavaScript content type. A client-side route sitting under that prefix
    works while the app is running and dies the moment somebody reloads the
    page or opens a bookmark, which is the worst way for a route to be wrong.
    """
    response = client.get(f"{settings.BASE_PATH}api-docs")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
