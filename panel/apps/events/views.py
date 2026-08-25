"""Reading both logs, and the one thing that may be done to one of them.

Every route here needs an authenticated session: DRF's defaults supply
SessionAuthentication plus IsAuthenticated. That matters more here than on most
endpoints - one of these lists who signed in and from where, and another hands
over whatever the services have been saying about themselves.

The reads are not on the live clock. The event log moves when somebody does
something and the journal moves when a service says something, and neither is
worth asking about twice a second - so the browser reads the events on the same
slow clock as its other lists, and the journal only when the tab is opened or the
button pressed. That difference is not decoration: every read of the journal
forks a process, and every read of the events table is one page of one small
table.

The exception to all of that is the clear, which is the only write in this app
that is not the recorder's, and the only route here that is a mutation at all -
so it is the only one Django's CSRF check has anything to say about. It is a
person emptying their own ledger: refused to an API token, and it writes a row
of its own on the way out. See EventListView.delete.
"""

from django.db.models import Q, QuerySet
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import NotAnApiToken

from . import journal, kinds, recorder
from .models import Event
from .serializers import (
    ClearedEventsSerializer,
    EventPageSerializer,
    EventQuerySerializer,
    JournalSerializer,
)

# What one page holds when the caller does not say. Twenty-five is about a
# screen of a list whose rows are one line each, and small enough that the count
# behind the pager is honest about there being more.
DEFAULT_PAGE_SIZE = 25


class ClearedByAPersonOnly(NotAnApiToken):
    """The same check as the credential routes, refused with a different sentence.

    apps.accounts.permissions draws its line around the credentials themselves,
    and this is not one of them - but it is the same argument one step along. A
    token is the credential an admin hands out and expects to be able to take
    back, and the reason that works is that the panel can be asked afterwards
    what the token did. If holding one were enough to empty the ledger, the
    answer to "what did this leaked token do before I noticed" would be nothing
    at all, and every row it had written about itself would be gone with it.

    So the log is emptied by a person at the login form, and a script that finds
    itself wanting to is told which of the two credentials this needs. What the
    rule is not, again, is a wall: a token may still fetch the backup that
    carries db.sqlite3, and this table is in it. It keeps the ledger out of the
    ordinary reach of a script, which is what makes a cleared log an act somebody
    chose rather than a side effect of an automation going wrong.
    """

    message = "An API token cannot clear the activity log. Sign in to the panel to do this."


#: What DELETE on the collection carries. IsAuthenticated is named again because
#: returning a list from get_permissions replaces the defaults outright.
CLEAR_PERMISSIONS = (IsAuthenticated, ClearedByAPersonOnly)


class EventListView(APIView):
    """The ledger as a collection: GET reads a page of it, DELETE empties it.

    GET answers what has happened to this panel, newest first.

    Everything is optional and nothing is rejected. `category` is one of the
    four the kinds module defines and is what the filter chips send; `severity`
    narrows to the events worth scanning for; `q` matches the actor or the target,
    which between them are the two names an operator has in hand.

    Paged rather than capped, because this is the one list in the panel that is
    read backwards through time: a cap would answer "the last hundred things"
    and leave the question that starts on page four unanswerable. The page size
    is bounded by the query serializer and the whole table by
    apps.events.models.MAX_ROWS, so the widest thing this can be asked for is
    two hundred rows of short strings.
    """

    def get_permissions(self) -> list:
        """Read as anything authenticated; clear only as a signed-in person.

        Per method rather than per view, because the two halves of this route
        are not equally reachable and a view-wide rule would have to pick one of
        them: closing the whole thing to tokens would take the log away from the
        scripts that read it, and leaving the whole thing open would let one
        erase it. See ClearedByAPersonOnly for why the clear is the half that is
        narrowed.
        """
        if self.request.method == "DELETE":
            return [permission() for permission in CLEAR_PERMISSIONS]
        return super().get_permissions()

    def get(self, request: Request) -> Response:
        params = EventQuerySerializer(data=request.query_params)
        wanted = dict(params.validated_data) if params.is_valid() else {}

        rows = _narrow(Event.objects.all(), wanted)
        page = wanted.get("page", 1)
        page_size = wanted.get("page_size", DEFAULT_PAGE_SIZE)
        # Counted before the slice and over the narrowed set, so the pager
        # counts the pages of what is being shown rather than of the table.
        total = rows.count()
        start = (page - 1) * page_size

        result = {
            "events": list(rows[start : start + page_size]),
            "total": total,
            "page": page,
            "page_size": page_size,
        }
        return Response(EventPageSerializer(result).data)

    def delete(self, request: Request) -> Response:
        """DELETE api/v1/events - empty the ledger, and say how much went.

        The whole table, never the filtered set, and the filters on the query
        string are ignored rather than honoured. A clear that took only what the
        list happened to be showing would be the one operation in the panel whose
        effect depends on a search box three fields away - and the operator who
        typed a client's name into it to read that client's history is the last
        person who should discover that pressing this took exactly those rows.
        Emptying the log is one act with one meaning, and the dialog in front of
        it says so.

        The row this writes is the point of the whole thing. It goes in after the
        delete, so a cleared log is a log with a single line in it saying who
        cleared it, from where, and how many events they took - which is the
        difference between an operator tidying up and an audit trail that can be
        made to have never existed. It costs nothing to keep: it is the one row
        the next clear removes.

        Nothing else is touched. The journal beside it is journald's and is not
        the panel's to delete, the traffic history is measurements rather than a
        ledger, and no client, key or setting is involved at all.
        """
        removed, _ = Event.objects.all().delete()
        # An empty table is left as one, by the same rule as a sweep that removed
        # nothing: the page does not offer this button with nothing to clear, so
        # a zero here is a stale tab or a second press, and "cleared 0 events" is
        # a line about neither.
        if removed:
            recorder.record(kinds.PANEL_EVENTS_CLEARED, request, count=removed)
        return Response(ClearedEventsSerializer({"removed": removed}).data)


def _narrow(rows: QuerySet[Event], wanted: dict) -> QuerySet[Event]:
    """Apply whichever of the three filters the query string asked for.

    The category is matched as a prefix of the kind rather than against a column
    of its own, because it is one: the kind is ``<category>.<what-happened>``,
    and a second column holding the half in front of the dot would be a fact
    stored twice and a chance for the two to disagree.
    """
    category = wanted.get("category")
    if category:
        rows = rows.filter(kind__startswith=f"{category}.")
    severity = wanted.get("severity")
    if severity:
        rows = rows.filter(severity=severity)
    text = (wanted.get("q") or "").strip()
    if text:
        rows = rows.filter(Q(target__icontains=text) | Q(actor__icontains=text))
    return rows


class JournalView(APIView):
    """GET api/v1/logs?source=panel&lines=200 - what a service has been saying.

    `source` is one of `panel`, `collector` or `tunnel`, which the journal module
    turns into a systemd unit; anything else is answered as unavailable rather
    than as an error, and no unit name from the query string ever reaches
    journalctl. `lines` is capped there too.

    Not a stream. A tail that followed the journal would hold a worker open for
    as long as a tab was left on this page, and the panel runs two of them - so
    this answers with what is there now and the page asks again when the
    operator wants it to.
    """

    def get(self, request: Request) -> Response:
        # Truncated because the answer echoes it back: an unknown source is
        # reported as the one that was asked for, and there is no reason for a
        # reply to carry eight kilobytes of somebody's query string.
        source = (request.query_params.get("source") or journal.SOURCES[0])[:32]
        result = journal.read(source, _lines(request.query_params.get("lines")))
        return Response(JournalSerializer(result).data)


def _lines(raw: str | None) -> int:
    """How many lines were asked for, defaulting rather than refusing.

    A hand-typed or stale value is answered with the default page of the log,
    which is the same rule the client list applies to its query string: nobody is
    served by a 400 about a number when the thing they asked for is "the log".
    """
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return journal.DEFAULT_LINES
