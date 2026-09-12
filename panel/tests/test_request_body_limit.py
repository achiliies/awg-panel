"""A body larger than its route takes is refused before anything reads it.

Django's DATA_UPLOAD_MAX_MEMORY_SIZE holds request.body and the plain fields of
a form to 2.5 MB, never a file part: in a larger request those are streamed to a
temporary file at whatever size they arrive. The login view is open to anyone,
and its CSRF check reads request.POST before comparing a token, so a multipart
POST there was written to /tmp in full before the 403 went back.

DRF 3.17.2 put JSON under Django's limit, and RequestBodyLimitMiddleware puts
every body under its route's, on Content-Length alone. These pin both: the
middleware by what a caller gets and by what never happens - no handler runs,
no temporary file is opened - and DRF's half with the middleware taken out,
since otherwise the suite would know about that half only from a version floor.

Most of them shrink the limit rather than build a body past 2.5 MB. The attack
as it was found is the exception, and runs at its real size.
"""

import io
import json
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.accounts.views import LoginView
from apps.panel import apidocs, backup
from awg.controller import reset_controller

pytestmark = pytest.mark.django_db

BODY_LIMIT = "awgui.middleware.RequestBodyLimitMiddleware"
SMALL_LIMIT = 1024
RESTORE_MEGABYTES = backup.MAX_UPLOAD_BYTES // (1024 * 1024)


def api_url(path: str) -> str:
    return f"{settings.BASE_PATH}api/v1/{path}"


def login_body(size: int) -> str:
    """A JSON login body of exactly `size` bytes."""
    shell = json.dumps({"username": "admin", "password": ""})
    return shell.replace('""', json.dumps("x" * (size - len(shell))))


@pytest.fixture
def login_calls(monkeypatch) -> list[int]:
    """Stand in for the login handler, recording the length of the password it parsed.

    The body is read the way the real handler reads it, so a body DRF refuses
    raises here just as it would there - and never reaches the list.
    """
    calls: list[int] = []

    def post(self, request):
        calls.append(len(request.data["password"]))
        return Response(status=204)

    monkeypatch.setattr(LoginView, "post", post)
    return calls


@pytest.fixture
def no_spooling(monkeypatch):
    """Fail the request outright if any part of its body reaches a temporary file."""

    def new_file(self, *args, **kwargs):
        raise AssertionError("a request body was spooled to a temporary file")

    monkeypatch.setattr(TemporaryFileUploadHandler, "new_file", new_file)


@pytest.fixture
def api(server_conf) -> Iterator[APIClient]:
    """Signed in against the fixture server config, around a fresh mock interface.

    get_controller() caches its mock for the life of the process, and a restore
    takes that interface down and brings it back up.
    """
    reset_controller()
    client = APIClient()
    client.force_login(get_user_model().objects.create_user("admin"))
    yield client
    reset_controller()


# ------------------------------------------------------------------ login


def test_a_huge_multipart_login_is_refused_before_anything_is_written(no_spooling):
    """The attack as found: the CSRF cookie anybody is handed, then a file part past
    2.5 MB. The CSRF check used to spool all of it before it answered 403."""
    client = APIClient(enforce_csrf_checks=True)
    client.get(api_url("auth/session"))
    junk = io.BytesIO(b"x" * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1))
    junk.name = "junk.bin"

    response = client.post(api_url("auth/login"), {"junk": junk}, format="multipart")

    assert response.status_code == 413
    assert response.json() == {
        "detail": "This route takes a request body of at most 2.5 MB.",
        "errors": {},
    }


@pytest.mark.parametrize(("size", "status"), [(SMALL_LIMIT, 204), (SMALL_LIMIT + 1, 413)])
def test_the_limit_is_the_last_byte_allowed(settings, login_calls, size, status):
    """Django's own check is `>`, and a middleware one byte stricter would refuse a
    body Django is happy to read."""
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = SMALL_LIMIT

    response = APIClient().post(
        api_url("auth/login"), login_body(size), content_type="application/json"
    )

    assert response.status_code == status
    assert len(login_calls) == (1 if status == 204 else 0)


def test_json_is_held_to_the_limit_even_without_the_middleware(settings, login_calls):
    """DRF's half. Before 3.17.2 it read JSON straight off the stream, past Django's
    check, so with the middleware taken out this is the only thing between a large
    body and the handler - and its refusal has to be the panel's 413, not Django's
    HTML page."""
    settings.MIDDLEWARE = [name for name in settings.MIDDLEWARE if name != BODY_LIMIT]
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = SMALL_LIMIT

    response = APIClient().post(
        api_url("auth/login"), login_body(SMALL_LIMIT + 1), content_type="application/json"
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "The request body is larger than the panel accepts.",
        "errors": {},
    }
    assert login_calls == []


def test_a_body_django_will_not_parse_is_answered_in_the_panels_shape(settings):
    """Too many form fields, found by the login view's CSRF check - outside DRF, where
    only Django's handler400 can answer."""
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 5
    client = APIClient(enforce_csrf_checks=True)
    client.get(api_url("auth/session"))

    response = client.post(
        api_url("auth/login"),
        "&".join(f"field{number}=x" for number in range(10)),
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "The panel refused that request.", "errors": {}}


# ---------------------------------------------------------------- restore


def test_restore_takes_an_archive_past_the_limit_every_other_route_has(settings, api, data_dir):
    """The one route that takes a file. A real backup, with the general limit put
    below its size, has to restore exactly as it would have."""
    payload = b"".join(api.get(api_url("backup")).streaming_content)
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = len(payload) // 2
    upload = io.BytesIO(payload)
    upload.name = "awg-backup.tar.gz"

    response = api.post(api_url("restore"), {"file": upload}, format="multipart")

    assert response.status_code == 200, response.content


def test_restore_has_a_ceiling_of_its_own():
    """Checked on the length alone, which is why the test can claim a body it never
    sends - and why it is refused before anybody is asked who is sending it."""
    response = APIClient().generic(
        "POST",
        api_url("restore"),
        b"x",
        content_type="multipart/form-data; boundary=x",
        CONTENT_LENGTH=str(backup.MAX_UPLOAD_BYTES + 1),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == (
        f"This route takes a request body of at most {RESTORE_MEGABYTES} MB."
    )


def test_the_api_reference_names_the_ceiling_restore_has():
    document = apidocs.document("http://testserver/api/v1", "0")

    note = document["paths"]["/restore"]["post"]["responses"]["413"]["description"]

    assert f"{RESTORE_MEGABYTES} MB" in note
