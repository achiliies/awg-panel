"""Wire shape for both logs.

Fields are declared once in Python spelling and renamed by ``CamelCaseMixin``,
like everywhere else in the panel. The one field that is not renamed is
``detail``: its keys are data rather than field names, which is why the recorder
insists on single words for them - see apps.events.recorder.record.

Nothing here can carry key material. The event table holds names, addresses and
counts; the journal payload holds whatever a service wrote to stdout, which is
the same text ``sudo awg-panel logs`` prints and is written by code that has
never been allowed to log a private key.
"""

from rest_framework import serializers

from apps.panel.serializers import CamelCaseMixin

from . import journal, kinds


class EventSerializer(CamelCaseMixin, serializers.Serializer):
    """One row of the event log."""

    id = serializers.IntegerField()
    at = serializers.DateTimeField()
    # The kind is sent as it is stored, not as a sentence: the browser owns the
    # wording, in whichever language it is drawing. A kind the frontend does not
    # recognise is shown as itself rather than hidden, which is what makes an
    # older panel readable against a newer database.
    kind = serializers.CharField()
    severity = serializers.CharField()
    # "" for the collector, which acts on nobody's behalf.
    actor = serializers.CharField(allow_blank=True)
    actor_ip = serializers.CharField(allow_blank=True)
    target = serializers.CharField(allow_blank=True)
    detail = serializers.JSONField()


class EventPageSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of GET api/v1/events: one page, and what it is a page of."""

    events = EventSerializer(many=True)
    # Over the whole filtered set rather than the page, because it is what the
    # pager counts pages from and what the header reports.
    total = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()


class ClearedEventsSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of DELETE api/v1/events: how many rows the clear took.

    A count rather than a 204, because the page says what happened afterwards and
    "the log is empty now" is the one thing the browser already knew. What it
    could not know is how much was in it - the list it was looking at was one
    page of twenty-five.
    """

    removed = serializers.IntegerField()


class EventQuerySerializer(CamelCaseMixin, serializers.Serializer):
    """The query string of GET api/v1/events, which is entirely optional.

    Nothing here is required and nothing rejects a request, on the same reasoning
    as the client list's query serializer: every part of it comes from a
    bookmark, a shared URL or a link the panel wrote itself, and the useful reply
    to a stale one is the newest page of events rather than an error about a
    category that was renamed two versions ago.
    """

    category = serializers.ChoiceField(choices=kinds.CATEGORIES, required=False)
    severity = serializers.ChoiceField(choices=kinds.SEVERITIES, required=False)
    # Matched against the actor and the target, which between them are the two
    # names an operator has in hand: "what did admin do" and "what happened to
    # alice".
    q = serializers.CharField(required=False, allow_blank=True, max_length=64)
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=200)


class JournalLineSerializer(CamelCaseMixin, serializers.Serializer):
    """One entry as journald recorded it."""

    # Null for an entry whose timestamp could not be read; see journal._moment.
    at = serializers.DateTimeField(allow_null=True)
    # The syslog level, 0..7. The UI colours 3 and below as errors and 4 as a
    # warning, and draws the rest plainly.
    priority = serializers.IntegerField()
    message = serializers.CharField(allow_blank=True)


class JournalSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of GET api/v1/logs: one service's recent output, oldest first."""

    source = serializers.ChoiceField(choices=journal.SOURCES)
    # The systemd unit the source resolved to, so the page can name what it is
    # showing - and so an operator can type the same thing into journalctl.
    unit = serializers.CharField(allow_blank=True)
    lines = JournalLineSerializer(many=True)
    # False when there is no journal to read at all. Not an error: see
    # journal.read for why a container without systemd answers rather than 500s.
    available = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
