"""One place where a core exception becomes an HTTP status code.

The rest of the panel raises the types in ``awg.errors`` and never builds an
error response by hand, so every failure reaches the browser in the same shape:

    {"detail": "<an actionable sentence>", "errors": {"<field>": "<message>"}}

``errors`` is empty unless the failure is per-field, which is what lets the UI
attach a message to the input that caused it.
"""

import logging
import os
import re
from typing import Any

from django.core.exceptions import RequestDataTooBig
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from awg.errors import (
    AwgError,
    Conflict,
    LockTimeout,
    NotConfigured,
    NotFound,
    ToolError,
    ValidationError,
)

log = logging.getLogger(__name__)

# A base64 X25519 key or PSK. Tool output is quoted back to the user, and a key
# in an error message ends up in a screenshot on a support forum.
_KEYLIKE = re.compile(r"[A-Za-z0-9+/]{43}=")

# awg-quick echoes each command it runs to stderr with this prefix. The line
# that explains the failure is one of the others.
_ECHO_PREFIX = "[#]"

_STDERR_LIMIT = 300

# How long the caller should wait before retrying a request that lost the race
# for the config lock. The lock itself is held for well under a second by every
# operation except an interface restart.
_RETRY_AFTER = "5"


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF exception handler: map awg.errors to status codes, normalise the rest."""
    if isinstance(exc, ValidationError):
        return _error(str(exc), status.HTTP_400_BAD_REQUEST, errors=exc.errors)

    if isinstance(exc, NotFound):
        return _error(str(exc), status.HTTP_404_NOT_FOUND)

    if isinstance(exc, Conflict):
        # With the code when there is one, which is how a name already in use is
        # told from an address pool with nothing left in it. Both are 409s, and
        # the panel answers one of them by drawing another name.
        return _error(str(exc), status.HTTP_409_CONFLICT, code=exc.code)

    if isinstance(exc, NotConfigured):
        # Not a fault, so no traceback: the panel can legitimately be running
        # before the VPN exists. 503 plus a code the SPA can branch on, so it
        # renders "AmneziaWG is not installed here" instead of a crash page.
        log.info("no server config: %s", exc)
        return _error(str(exc), status.HTTP_503_SERVICE_UNAVAILABLE, code="not_configured")

    if isinstance(exc, LockTimeout):
        response = _error(str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        response["Retry-After"] = _RETRY_AFTER
        return response

    if isinstance(exc, ToolError):
        # 502: the panel worked, the thing it delegates to did not.
        log.warning("tool failed: %s", exc)
        return _error(_tool_message(exc), status.HTTP_502_BAD_GATEWAY)

    if isinstance(exc, AwgError):
        # A core failure with no mapping of its own. Its message is still
        # written for a human, which beats a traceback and a bare 500.
        log.error("unmapped core error: %s", exc, exc_info=True)
        return _error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)

    if isinstance(exc, DjangoValidationError):
        # Password validators and model.full_clean() raise Django's, not DRF's.
        return _error(*_from_django_validation(exc))

    if isinstance(exc, RequestDataTooBig):
        # Nearly always refused on its Content-Length by RequestBodyLimitMiddleware
        # before it is read. What can still get here is a body within its route's
        # limit whose form fields alone are past Django's, and without this it was
        # Django's HTML 400 - plus a traceback in the journal for every one.
        log.info("refused a request body: %s", exc)
        return _error(
            "The request body is larger than the panel accepts.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, NotAuthenticated):
        # DRF downgrades 401 to 403 when no authenticator can offer a
        # WWW-Authenticate challenge, which SessionAuthentication cannot. The
        # SPA distinguishes the two: 401 means "show the login page", 403 means
        # "you are logged in and still may not do this".
        response.status_code = status.HTTP_401_UNAUTHORIZED

    _normalise(response)
    return response


def _error(
    detail: str,
    status_code: int,
    errors: dict[str, str] | None = None,
    code: str = "",
) -> Response:
    """The one error shape: {detail, errors}, plus a machine-readable code when
    the client needs to branch on the reason rather than the wording."""
    body: dict[str, Any] = {"detail": detail, "errors": dict(errors or {})}
    if code:
        body["code"] = code
    return Response(body, status=status_code)


def _tool_message(exc: ToolError) -> str:
    """Name the tool and quote what it complained about, never the command line.

    The command line carries file paths and, for some operations, the path to a
    key file. The program name plus its own error text is everything the
    operator needs and nothing they should not have in a browser tab.
    """
    program = os.path.basename(exc.cmd[0]) if exc.cmd else "the AmneziaWG tools"
    summary = _summarise_stderr(exc.stderr)
    if summary:
        return f"{program} failed: {summary}"
    if exc.returncode is not None:
        return (
            f"{program} exited with status {exc.returncode} and said nothing. "
            "Check the server status page and the system journal."
        )
    return f"{program} could not be run. Check that the AmneziaWG tools are installed."


def _summarise_stderr(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    meaningful = [line for line in lines if not line.startswith(_ECHO_PREFIX)]
    chosen = (meaningful or lines)[-1]
    chosen = _KEYLIKE.sub("<redacted>", chosen)
    if len(chosen) > _STDERR_LIMIT:
        chosen = chosen[: _STDERR_LIMIT - 3].rstrip() + "..."
    return chosen


def _from_django_validation(exc: DjangoValidationError) -> tuple[str, int, dict[str, str]]:
    errors: dict[str, str] = {}
    if hasattr(exc, "error_dict"):
        for field, messages in exc.message_dict.items():
            errors[field] = " ".join(str(m) for m in messages)
        detail = "; ".join(f"{field}: {msg}" for field, msg in errors.items())
    else:
        detail = " ".join(str(m) for m in exc.messages)
    return detail or "That input is not valid.", status.HTTP_400_BAD_REQUEST, errors


def _normalise(response: Response) -> None:
    """Give DRF's own errors the panel's shape.

    A serializer failure arrives as {"field": ["message"]}; a permission failure
    as {"detail": "..."}. The SPA reads one shape, so field errors are folded
    into `errors` and a sentence is built for `detail`.
    """
    data = response.data
    if not isinstance(data, dict):
        # A non-field DRF error can be a bare list.
        detail = _first_message(data) or "The request could not be completed."
        response.data = {"detail": detail, "errors": {}}
        return

    if "detail" in data:
        response.data = {"detail": str(data["detail"]), "errors": {}}
        return

    errors = {str(field): _first_message(value) for field, value in data.items()}
    errors = {field: message for field, message in errors.items() if message}
    detail = "; ".join(f"{field}: {message}" for field, message in errors.items())
    response.data = {
        "detail": detail or "The request could not be completed.",
        "errors": errors,
    }


def _first_message(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return _first_message(value[0]) if value else ""
    if isinstance(value, dict):
        for nested in value.values():
            message = _first_message(nested)
            if message:
                return message
        return ""
    return str(value)
