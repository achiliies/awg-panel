"""Wire shape for the stats summary.

Fields are declared once in Python spelling and renamed by ``CamelCaseMixin``,
like everywhere else in the panel. The other stats payload does not pass through
a serializer at all: the live blob is already camelCase and half of it is keyed
by peer public key, which is data rather than field names, so it is served
straight out of the file the collector wrote.

Nothing here can carry key material.
"""

from rest_framework import serializers

from apps.panel.serializers import CamelCaseMixin


class StatsSummarySerializer(CamelCaseMixin, serializers.Serializer):
    """GET api/v1/stats/summary - the dashboard's cards."""

    total_clients = serializers.IntegerField()
    online_clients = serializers.IntegerField()
    disabled_clients = serializers.IntegerField()
    # Bytes since midnight UTC, from the running total the collector keeps
    # rather than from the kernel's counters, which have no sense of when.
    today_rx = serializers.IntegerField()
    today_tx = serializers.IntegerField()
    # All-time across every client, from traffic.db minus the panel's offsets.
    total_rx = serializers.IntegerField()
    total_tx = serializers.IntegerField()
    # Seconds since the host booted, not since the panel started.
    uptime = serializers.IntegerField()
    iface_up = serializers.BooleanField()


class TrafficPointSerializer(CamelCaseMixin, serializers.Serializer):
    """One bucket of a traffic series - a day or a month, and what moved in it."""

    # "2026-08-13" for a day, "2026-08" for a month. A label rather than a
    # moment, on purpose: see history.Point for why a month must not travel as
    # a timestamp.
    period = serializers.CharField()
    rx = serializers.IntegerField()
    tx = serializers.IntegerField()


class TrafficHistorySerializer(CamelCaseMixin, serializers.Serializer):
    """Both series in one answer, for the server or for one client.

    Every period in the window is present, including the ones nothing moved on -
    a gap in a chart's axis is a lie about when the quiet stretch was, and the
    empty days are usually the ones somebody is looking for.
    """

    daily = TrafficPointSerializer(many=True)
    monthly = TrafficPointSerializer(many=True)
    # The first day this history has anything on, whatever window was asked for,
    # and null when it has nothing at all. It is what the window was cut to at
    # the older end, so a caller can tell "quiet" from "before our time" - and
    # it is the floor a date picker should offer, since asking for anything
    # earlier can only come back empty.
    earliest = serializers.DateField(allow_null=True)


class TrafficResetSerializer(CamelCaseMixin, serializers.Serializer):
    """POST api/v1/stats/traffic/reset - what the wipe took.

    Three counts rather than one, because they are three different sentences and
    a total of them would be a number of rows rather than a fact about anything.
    The caller has just been told its own history is gone; what it can usefully
    say afterwards is how much there was.
    """

    # Clients whose all-time figures were above zero, which is what "cleared the
    # traffic of N clients" means. A client that had never moved a byte is not
    # counted, because nothing about it changed.
    clients = serializers.IntegerField()
    # Stored days of the server's own history, and of every client's, that the
    # request deleted. The collector may take a few more of each when it folds -
    # the rows it wrote back between the request and its next flush - and those
    # are not counted here, because this answer is written before they exist.
    server_days = serializers.IntegerField()
    client_days = serializers.IntegerField()
