"""One row per thing that happened, and the two numbers that bound the table.

An append-only ledger, which is what makes it different from every other table
the panel keeps. Nothing here is ever updated: a row is written once by whoever
did the thing, read back by the events endpoint, and eventually swept. That is
also why it is its own app rather than a table in ``stats`` - the tables there
hold counters that are rewritten in place all day, and folding a ledger in
beside them would put two opposite lifecycles under one name.

Deliberately not derived from anything. Almost every event here leaves a trace
somewhere else - a client's row appears, a config file loses a peer, a session
disappears - and the panel could in principle reconstruct some of it by
comparing states. It could not reconstruct *who*, and it could not reconstruct
anything at all about the things that leave no trace: a login, a backup
downloaded, a client deleted. So this is written at the moment of the act, by
the code performing it, or it does not exist.

rx/tx-style figures are not in here either, for the same reason the other
direction: what a client transferred is a measurement that belongs in
apps.stats, and a ledger that also carried numbers would be asked to be a chart.
"""

from django.db import models

from . import kinds

# How long an event is kept before the collector's daily sweep takes it.
#
# Half a year, which is chosen against what the log is actually read for. The
# question it answers is "what happened to this client", and the gap between
# something going wrong and somebody asking about it is days, occasionally
# weeks. Six months is far past that and is still short enough that the table
# does not become an archive nobody has decided to keep.
#
# What it bounds is small either way. An event is a row of short strings and a
# handful of named values - call it 250 bytes with its index share - and a busy
# panel writes a few dozen a day, so the expected table is a couple of megabytes
# and the ceiling below is 5 MB. Both are noise beside the traffic history in
# the same file.
RETENTION_DAYS = 180

# The most rows kept whatever their age, applied after the window above.
#
# The window alone bounds an *ordinary* panel. It does not bound a panel that is
# being attacked: a failed sign-in is an event, and while django-axes caps a
# single address at five attempts per cool-off, nothing caps the number of
# addresses. So there is a second ceiling that does not care why the rows are
# there, and the oldest go first - during a flood the newest rows are the ones
# describing it.
#
# Twenty thousand is about four months of a genuinely busy panel, so on any
# normal server this never fires and retention is the age window alone.
MAX_ROWS = 20_000


class Event(models.Model):
    """One thing that happened, and who or what did it."""

    # Set from the clock by the recorder rather than with auto_now_add, because
    # the collector records events for decisions it made earlier in the same
    # pass, and the moment worth keeping is when the client was switched off -
    # not when the row reached SQLite.
    at = models.DateTimeField()
    # One of apps.events.kinds. Not a `choices` list: see that module for why a
    # kind this build does not recognise has to remain readable rather than
    # become a database error.
    kind = models.CharField(max_length=32)
    severity = models.CharField(max_length=7, default=kinds.INFO)
    # The account that did it, or "" for the collector, which acts on nobody's
    # behalf. An empty actor is what the UI draws as "the panel" - it is a real
    # distinction and not a missing value, which is why there is no null here.
    actor = models.CharField(max_length=150, blank=True, default="")
    # Text rather than GenericIPAddressField, for the same reason
    # accounts.LoginSession stores it that way: behind an unusual proxy the
    # address can be something that is not an address, and "" is a far better
    # answer than a failed write on the audit trail.
    actor_ip = models.CharField(max_length=45, blank=True, default="")
    # What it happened to, in the spelling the operator would recognise: a
    # client name, an interface, a settings group. Free text because the things
    # events are about do not share a key - and a name rather than an id
    # deliberately, since half of these are about something that no longer
    # exists by the time anybody reads the row.
    target = models.CharField(max_length=64, blank=True, default="")
    # The named values the browser's sentence needs, and nothing else: see
    # recorder.record for what is allowed in here and why it is filtered rather
    # than trusted. Never key material, never a password, never a whole request.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        # Newest first, by id rather than by `at`, and with no index on either.
        #
        # Rows are appended and never moved, so the primary key is already in
        # chronological order and walking it backwards is the newest-first page
        # - answered by the index SQLite maintains anyway, at no extra write
        # cost per event. Ordering by `at` would need an index of its own to
        # avoid a sort, and would still need the id to break ties between two
        # events recorded in the same microsecond, which the collector does.
        #
        # The filters ride on the same walk. A category or a severity narrows
        # rows the scan is already visiting, and it stops as soon as it has a
        # page - so the cost is a page of rows, except for a filter that matches
        # nothing at all, which walks the table. At the ceiling above that is
        # twenty thousand short rows, which SQLite reads in a few milliseconds,
        # and this list is on the panel's slow clock rather than the live one.
        ordering = ["-id"]
        verbose_name = "event"
        verbose_name_plural = "events"

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.kind} {self.target}".rstrip()
