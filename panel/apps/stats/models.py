"""Traffic history: one row a day for the whole server, and one per client.

traffic.db holds all-time totals and nothing else, because a per-peer running
total is all a counter file can be kept honest as. The dashboard also reports what has moved
*today*, and the kernel cannot answer that - its counters only go up, with no
record of when. So the collector adds up the deltas it is already computing and
writes the running total here.

This table used to be two, keyed by public key: a row per client per ten-second
flush, rolled up hourly and pruned on a retention window. It was sized for the
per-client history charts, and those were never built - the endpoint that read
them had no caller in the UI. What it cost was a row per connected client every
ten seconds, which on a four thousand client server is four hundred inserts a
second and twenty gigabytes resident, all of it to serve one figure on one card.

So the aggregation moved out of the database and into the collector, where the
numbers were already passing through memory. Nothing is keyed by client any
more, and nothing scales with the client count: the write is a single upsert
whatever the server holds.

The charts have since been built, and ClientDaily is what they read - so it is
worth being exact about how it differs from the table that was removed, because
the two hold the same kind of fact and only one of them was affordable. What was
expensive was never the per-client keying: it was the *sample rate*. A row per
client per flush is a row whose count grows with time whatever the server does,
and at a ten-second flush that is 8,640 rows per client per day. A row per client
per day is one row, rewritten in place until the day ends. The same four thousand
client server that cost thirty-four million rows a day costs four thousand, and
only for the clients that actually moved bytes.

Accumulated rather than derived, and that is the part worth keeping. The obvious
alternative is to snapshot the summed traffic.db totals at midnight and subtract
- but that sum falls when a client is deleted, because removing a client drops
its row outright, so deleting somebody at noon would take their whole lifetime
off today's figure. Bytes that moved today moved today, whoever has since been
removed.

That is also why the two tables here are not derived from one another. Summing
every ClientDaily row for a day very nearly answers what DailyTotal holds, and
the gap between them is precisely the clients that have since been deleted:
their per-client rows go with them, and the server's day keeps their bytes. So
the server's figure is read from the server's row, never summed from the
clients', and the panel is never in a position to report a day as smaller than
it was because somebody was removed from it afterwards.

There is a third table here that holds no traffic at all. TrafficReset is one
row saying that an admin asked for every figure on the server to be cleared and
whether the collector has finished doing it - the one thing in the panel that
cannot be done by whichever process was asked, because half of what has to be
cleared is in another one's memory.

rx and tx are the server's directions, matching `awg show dump` and traffic.db:
rx is the clients' upload, tx their download. The labels are swapped in the UI,
never in storage.
"""

from datetime import date, datetime
from datetime import timezone as dt_timezone

from django.db import models


def utc_day(moment: datetime) -> date:
    """Which UTC day a moment falls in - the key of the row that counts it.

    Beside the model rather than beside either caller, because the collector
    writes the row and the summary endpoint reads it and the two must never
    disagree about where the day ends. A figure that jumped at midnight because
    the writer and the reader rounded differently would be a bug nobody could
    see except in the hour it happened.

    Converted rather than read off the datetime as it arrives: ``.date()`` on an
    aware value gives the date in *its own* zone, so the same instant would be
    the 6th or the 7th depending on who constructed it. The instant decides.
    """
    return moment.astimezone(dt_timezone.utc).date()  # noqa: UP017


class DailyTotal(models.Model):
    """Every client's transfer over one UTC day, summed."""

    # The day this covers, at midnight UTC. Stored as a date rather than a
    # timestamp because that is what it is, and because it makes the row the
    # collector wants a lookup on today's date rather than a range query.
    #
    # UTC, like every other stored moment in the panel. Which day that is will
    # not match the calendar on the wall of an admin far enough east or west,
    # and that is the trade for a figure two admins in two countries can compare
    # without first agreeing whose day it is.
    day = models.DateField(primary_key=True)
    rx = models.BigIntegerField(default=0)
    tx = models.BigIntegerField(default=0)

    class Meta:
        # Newest first: every reader wants today, and the only other thing this
        # table can be asked is what the last few days looked like.
        ordering = ["-day"]

    def __str__(self) -> str:
        return f"{self.day:%Y-%m-%d} rx={self.rx} tx={self.tx}"


# How long a client's daily rows are kept before the collector sweeps them up.
#
# Thirteen months and a fortnight, which is the shortest window that can always
# answer the question the monthly chart asks. That chart shows twelve whole
# months, so on the last day of a long month it needs the first day of the month
# eleven back - 365 days on its own is a day or two short of that in the wrong
# part of the year, and the twelfth column would arrive half empty. The margin
# is what stops the oldest month thinning out as the day advances.
#
# It is also the one number here that bounds the table, so it is worth saying
# what it bounds it to. A row exists only for a client that moved bytes on that
# day, so the ceiling is clients x 400 and the floor is nothing at all: a
# thirty client panel holds twelve thousand rows, about a megabyte. The four
# thousand client server the module docstring measures the old table against
# reaches 1.6 million rows and a couple of hundred megabytes - but only if every
# one of those clients transfers something every day for over a year, and that
# is the worst case rather than the expected one.
#
# Deliberately not a setting. The two retention settings this panel used to have
# were removed in panel migration 0002 because nothing read them, and the reason
# they were worth removing applies here too: it is a control an admin cannot
# evaluate without knowing the row arithmetic above, and getting it wrong is
# either a chart with holes in it or a database growing for no benefit.
CLIENT_HISTORY_DAYS = 400


class ClientDaily(models.Model):
    """One client's transfer over one UTC day.

    The row is created by the first flush after the client's first byte of the
    day and rewritten in place by every flush after it, so a client that is idle
    costs nothing at all and a client that is busy costs one row. Both halves
    matter: the first is what keeps a mostly-quiet server's table small, and the
    second is what stops the table growing with the clock the way the sample
    table this replaces did.

    Hung off ClientMeta rather than keyed by public key, which is the one place
    this parts company with how the panel stores everything else per client -
    and it is worth saying why, because the public key is otherwise exactly the
    right identifier and is what traffic.db uses.

    The reason is that a history has to end when the client does, and a client
    can end in five places: the delete endpoint, the bulk remove, and three
    paths in apps.clients.index that drop metadata for a peer which left the
    config through a hand edit or a restore. Keyed by public key this table
    would have to be swept in all five, and the fifth is the worst of them -
    apps.clients.index._prune is a rebuild path with a grace period and an
    explicit guard against acting on a truncated config, precisely because
    deleting on a bad read there is unrecoverable. Adding history to what that
    decision can destroy is not an improvement to it.

    A cascade puts the deletion at the one point that already decides the
    question, with the guards it already has. It also settles the case that
    every one of the five would have missed: a key rotation writes a new public
    key into the same metadata row, so a history keyed on the key would be
    orphaned under the old one and start again from nothing, for what is to the
    admin the same client with new credentials. Keyed on the row, it simply
    follows.

    Taken by "reset usage", along with the all-time counters that click zeroes.
    It used to be exempt, on the reasoning that a day's history is a record of
    what happened on that day and rewriting it would be the panel disagreeing
    with itself about a date that has passed. That reasoning still holds for
    DailyTotal, which is why the server's day survives one client's reset - but
    it was the wrong answer for the client's own rows, because a reset is not a
    correction to the record. It is an admin declaring this client's usage to
    start from here, usually because the client is being handed to somebody
    else, and a chart still holding the last tenant's evenings answers a
    question nobody asked.

    Deleted rather than offset, unlike the counters, because there is nowhere
    to hold the offset: the all-time figures have a second copy in traffic.db
    that an offset is subtracted from, and these rows are the only copy of
    themselves. The confirmation in the browser says so before anything happens.
    """

    meta = models.ForeignKey("clients.ClientMeta", on_delete=models.CASCADE, related_name="daily")
    # Indexed on its own as well as under the constraint below, because the two
    # readers want opposite things from this column. A chart asks for one
    # client's last few hundred days, which the composite index answers by
    # walking that client's rows in date order. The sweep asks for every
    # client's rows before a date, which that index cannot answer at all - the
    # client comes first in it, so the only way through is every row.
    day = models.DateField(db_index=True)
    rx = models.BigIntegerField(default=0)
    tx = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            # One row per client per day, and the target the collector's upsert
            # names in its ON CONFLICT clause - which is what makes a flush a
            # single statement over the clients that moved rather than a read,
            # a comparison and a write for each of them.
            models.UniqueConstraint(fields=["meta", "day"], name="uniq_client_day"),
        ]
        # Newest first, matching DailyTotal: a reader asking for a window wants
        # the recent end of it, and the chart reverses what it draws anyway.
        ordering = ["-day"]

    def __str__(self) -> str:
        return f"{self.meta_id} {self.day:%Y-%m-%d} rx={self.rx} tx={self.tx}"


class TrafficReset(models.Model):
    """The wipe an admin asked for, and the one the collector has carried out.

    "Remove all traffic" is the only thing in the panel that has to be done by
    two processes. The request can empty the tables and store an offset against
    every client in the moment it is made, and that is enough for everything the
    browser reads - but the numbers themselves live in two more places the web
    process cannot reach into: traffic.db, which the collector is holding a
    mirror of and would write back from memory, and the running day that mirror
    feeds. So the request records that a wipe was asked for, and the collector
    finishes it on its next flush.

    One row, because there is one answer to "has the wipe been done": a second
    request before the collector has caught up is not a second wipe queued
    behind the first, it is the same clearing asked for again, and the fold that
    settles it settles both. ``requested_at`` is overwritten and ``applied_at``
    goes back to null, which puts the pair back into the state the collector
    acts on.

    Two columns rather than one stamp compared against something the collector
    remembers, because what it remembers does not survive being restarted - and
    a wipe asked for while the collector was stopped is exactly the case that
    must not be forgotten. Stored rather than inferred, so the answer to "is
    there a fold outstanding" is the same after a reboot as before it.

    Equal stamps mean there is nothing to do, which is also what makes the fold
    happen once. It has to: subtracting the offsets from the counters a second
    time would take a client's post-wipe bytes off as well, and clearing the
    running day again would drop the traffic since the last flush on the floor.
    """

    # The single row. A primary key with a default rather than an autofield, so
    # the upsert the request makes is a plain update_or_create on a known key
    # and there is no way to end up with two of these.
    ROW = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=ROW)
    requested_at = models.DateTimeField()
    # Null until the collector has folded the offsets into traffic.db and let go
    # of the day it was holding. Equal to requested_at once it has.
    applied_at = models.DateTimeField(null=True, blank=True)

    @property
    def pending(self) -> bool:
        """Whether a wipe is still waiting for the collector to finish it."""
        return self.applied_at != self.requested_at

    def __str__(self) -> str:
        return f"requested={self.requested_at:%Y-%m-%dT%H:%M:%SZ} applied={self.applied_at}"
