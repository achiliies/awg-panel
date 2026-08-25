"""Keep the row describing the session in flight up to date.

Where a session was last used, and from which address, is only knowable while a
request is being served, and only the requests that carry a session say anything
at all. So it is recorded here rather than in a view: every authenticated
request refreshes it, including the ones the dashboard makes on its own, which
is what makes "last active" mean what an admin reads it to mean.

sessions.touch() decides how little of that is worth writing; this class only
decides when to ask.
"""

import logging
from collections.abc import Callable

from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse

from . import sessions

log = logging.getLogger(__name__)


class LoginSessionMiddleware:
    """Record each authenticated request against its session's row."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if getattr(request, "session", None) is not None:
            try:
                sessions.touch(request)
            except DatabaseError:
                # Bookkeeping, and the panel is worth more than it is: an
                # upgrade that has not run its migrations yet, or a database
                # that is momentarily locked, must not turn every page of a
                # working panel into a 500.
                log.warning("could not record this session's activity", exc_info=True)
        return self.get_response(request)
