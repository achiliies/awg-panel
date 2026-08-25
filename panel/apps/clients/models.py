"""The half of a client that a config file cannot hold.

A client is a `[Peer]` block in awg0.conf plus a rendered clients/<name>.conf,
and those files are the source of truth: `awg-quick` loads them at boot, an
admin can edit them over SSH, and a restore replaces them wholesale - none of
which consults this database. So nothing here duplicates them. Quota, expiry, bandwidth ceilings, contact details, the usage offsets, the reason a peer was
switched off and whether an admin has overruled its expiry have nowhere to live
in a WireGuard config, and those - only those - are stored below.

The last handshake and the address it came from are the exception that proves
the rule. They are not config, they are the kernel's runtime state, and the
kernel forgets them the moment the interface goes down - so a reboot, a settings
change that restarts the tunnel, or a restore from backup used to leave every
client reading "never seen". The collector copies them here as it sees them,
which makes them survive all three; the live value always wins while there is
one.

When a client was added is a copy for a different reason. It does live in the
config, as the "# Created" comment written beside every peer, and that comment
stays the source of truth. But a comment is the first thing a
hand edit, a rewritten peer block or a restore from a config that predates the
comment loses, and there is no way to work the date out again afterwards. So it
is mirrored here the moment a client is added and backfilled by the collector
for the ones added over SSH.

Keyed by public key, not by name. A rename in the config file is a one-word edit
to a comment line, and nothing announces it; keying on the key means an admin
who changes "# Client = phone" to "# Client = work-phone" with an editor keeps
that client's quota and note. `name` is a display cache for the same reason it is not the key:
it may be out of date, and every read path prefers the config.

A row whose public key is no longer in the server config is stale - the client
was removed elsewhere - and is ignored by every reader and pruned in passing by
apps.clients.merge.

ClientIndex, at the bottom of this file, is the one thing here that does
duplicate the config, and it is a different kind of table: a copy kept for speed,
rebuilt from the files whenever they change, holding nothing that would be lost
if it were dropped. The rule above still holds for everything a client *is* -
ClientIndex is read in place of the config, never written back to it, and the
files remain the only authority on themselves.
"""

from django.db import models


class ClientMeta(models.Model):
    """Panel-only metadata for one peer."""

    public_key = models.CharField(max_length=64, unique=True, db_index=True)
    # Display cache, refreshed on every write the panel makes. The config's
    # "# Client" comment wins wherever the two disagree.
    name = models.CharField(max_length=64, blank=True, default="")
    email = models.CharField(max_length=190, blank=True, default="")
    note = models.TextField(blank=True, default="")
    # 0 is unlimited, which is also the default, so a client created from the
    # CLI with no row at all behaves the same as one created here without one.
    quota_bytes = models.BigIntegerField(default=0)
    # How fast this client may go, in bits per second, and 0 for no ceiling.
    # Here for the same reason the quota is: a WireGuard config has nowhere to
    # write a rate, and the kernel structure that enforces one is derived from
    # the client's address rather than stored, so this is the only record that
    # an operator ever decided anything.
    #
    # What is actually in force is a different question with a different answer,
    # and it is deliberately not kept beside this: awg.shaper reads it back out
    # of the kernel. These two can disagree - a tunnel that came up before the
    # panel did, a hand-run `tc qdisc del` - and telling them apart is the whole
    # job of the reconcile pass, which a stored copy of "applied" would make
    # impossible.
    #
    # Bits per second rather than the megabits an operator says, for the reason
    # the quota is bytes rather than gigabytes: it is what tc takes, and a unit
    # conversion belongs at the edge where somebody can see it.
    down_bps = models.BigIntegerField(default=0)
    # Upload needs a WAN interface to shape on, so unlike the download ceiling
    # this one can be set on a server that cannot honour it. It is still stored:
    # the setting that turns WAN shaping on can be turned on later, and a limit
    # silently discarded because the server was not ready for it yet is worse
    # than one that starts working when the server is.
    up_bps = models.BigIntegerField(default=0)
    # Indexed because two things ask the table for a date rather than for a
    # client: the collector wants the earliest one still to come, so it knows
    # when it next has to look at all, and the list filters and the sweep want
    # the ones already gone. Both are a walk to one end of an ordered column,
    # and without the index both are a scan of every row.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The expiry an admin has overruled by switching this client back on, and
    # the whole of what makes "expired" and "switched off" two facts instead of
    # one. It holds a copy of the date rather than a flag, so that moving the
    # expiry re-arms enforcement on its own: the copy no longer matches, and a
    # decision made about last month's date cannot go on forgiving a new one.
    expiry_override_at = models.DateTimeField(null=True, blank=True)
    # Subtracted from the all-time totals in traffic.db when the panel reports
    # usage. Kept here rather than by rewriting traffic.db, because that file is
    # the only record of all-time usage there is: editing it to satisfy a "reset
    # counters" click would destroy history nothing could rebuild.
    offset_rx = models.BigIntegerField(default=0)
    offset_tx = models.BigIntegerField(default=0)
    # When the offsets above were last taken, and with them this client's stored
    # history. None means never.
    #
    # Not kept for the record - the log line says that. It is here because the
    # collector holds today's per-client figure in memory and writes it out as
    # an absolute total, so a history the panel deletes underneath it comes
    # straight back on the next flush with the morning still in it. Nothing
    # announces a request to that process, but it reads every one of these rows
    # once a minute anyway, and a stamp it can compare against the one it saw
    # last time turns the deletion into something it can notice. See
    # apps.stats.management.commands.collector.Collector._note_history_resets.
    history_reset_at = models.DateTimeField(null=True, blank=True)
    # "" | manual | quota | expired. Why the "# Disabled" marker is in the
    # config; the marker itself is what actually revokes the key.
    disabled_reason = models.CharField(max_length=16, blank=True, default="")
    # Unix seconds of the last handshake the collector ever saw, and the
    # endpoint it came from. 0 and "" mean this peer has not connected since the
    # panel was installed - never "not connected right now", which is what the
    # kernel reports after a restart and is why these are kept at all. Only ever
    # moved forwards: the live dump is the better answer whenever it has one.
    last_handshake = models.BigIntegerField(default=0)
    last_endpoint = models.CharField(max_length=128, blank=True, default="")
    # When the client was added, in UTC, mirroring the config's "# Created"
    # comment to the second. Set by hand rather than with auto_now_add, because
    # the row is not the client: the collector opens one for any peer it sees a
    # handshake from, and stamping that moment would report a client added over
    # SSH last year as added the first time the panel happened to be running.
    # None means nobody ever recorded one, which the client list shows as
    # unknown rather than guessing.
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "public_key"]
        verbose_name = "client metadata"
        verbose_name_plural = "client metadata"

    def __str__(self) -> str:
        return self.name or self.public_key

    @property
    def expiry_overridden(self) -> bool:
        """Whether an admin has switched this client on in the knowledge that it has lapsed.

        Asked by the collector before it enforces an expiry and by the client
        list before it reports one, so both of them answer the question the same
        way. An override is tied to the exact date it forgives: any other expiry,
        including none at all, is a date nobody has made a decision about yet.
        """
        return self.expires_at is not None and self.expiry_override_at == self.expires_at


class ClientIndex(models.Model):
    """A copy of what the config files say, so a page of clients costs a query.

    Everything here is already true somewhere else. The config is still what a
    client *is* - this table is read instead of it, not in place of it, and the
    difference is the whole of why it is safe.

    It exists because reading the config is O(clients) and answering the client
    list was O(clients) with it: fifty rows on a four thousand client server cost
    one parse of a thirty thousand line file, four thousand opens of
    clients/<name>.conf for the one line of each that the row needs, four
    thousand row dicts and a sort over all of them - about half a second, on an
    endpoint every open tab polls, and again every two seconds for the dashboard
    cards. Paginating the response bounded what was sent and not one byte of what
    was read. A column with an index answers the same question in the time it
    takes to find fifty rows, and stays that way at forty thousand clients.

    So this holds the fields a page is *chosen* by - the ones filtering, sorting
    and searching read - for every peer, and nothing else is stored here at all.
    Quota, expiry, notes and the last handshake stay in ClientMeta and are joined
    to, never copied: they are edited through the panel and by the collector, and
    a copy of them would be a copy that can be out of date in a way no file
    timestamp could reveal. What is copied is exactly what a file says, and a
    file says when it last changed.

    Disposable, and meant to be. Every column can be recomputed from the config,
    the client config files and traffic.db, `awg.index` does exactly that
    whenever the stamps in ClientIndexState stop matching what is on disk, and a
    row deleted or a table emptied costs one rebuild and nothing else. That is
    the property that makes the copy safe to have: it can be wrong, briefly, and
    it cannot be wrong in a way that survives being noticed.
    """

    # CASCADE, and the reason it is a relation at all: the filters read quota and
    # expiry, the sorts read the stored handshake, and the search reads the note
    # and the email - all of which live in ClientMeta. Joining is what lets one
    # query answer with those included and still come back with fifty rows. The
    # rebuild opens a metadata row for any peer that has none, so this is never
    # the null side of anything; a peer with no metadata gets one full of
    # defaults, which is what every reader already assumed it had.
    meta = models.OneToOneField(ClientMeta, on_delete=models.CASCADE, related_name="index")

    # ---- what the server config says
    name = models.CharField(max_length=64)
    ip = models.CharField(max_length=45, blank=True, default="")
    ip6 = models.CharField(max_length=45, blank=True, default="")
    # From the client's own config file, not the peer entry: this is what the
    # client sends through the tunnel, and it is what says whether the client is
    # still leaking its IPv6 around it.
    allowed_ips = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    has_conf_file = models.BooleanField(default=False)

    # ---- the sort keys, resolved once at rebuild rather than per request
    #
    # Each is the value the old in-memory sort computed for every row on every
    # request. Materialising them is what lets the database do the ordering: an
    # expression over a column can be indexed and `casefold()` cannot.
    #
    # `created_key` is the config's "# Created" comment, falling back to the
    # database's copy - the same precedence the row itself shows. It is compared
    # as text rather than parsed into a date because RFC 3339 in UTC already
    # sorts chronologically as text, so ordering a full /16 costs string
    # comparisons and not sixty thousand date parses.
    #
    # The default order of this table, and the order the list reads in: a history,
    # with the clients this server has held longest at the top and the one added a
    # minute ago at the bottom, where it was appended to the config and where the
    # operator who just added it watched it appear. A client whose date nothing
    # knows sorts first - the empty string is below every timestamp, and a client
    # with no recorded date is one that predates the panel keeping them.
    created_key = models.CharField(max_length=32, blank=True, default="", db_index=True)
    # The name the table displays, casefolded: a client with no name is listed
    # under the head of its public key, and sorting has to agree with that.
    name_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # The tunnel address as an integer, or -1 for a peer whose address is not one
    # this can order. Text order would put .10 before .2.
    ip_order = models.BigIntegerField(default=-1, db_index=True)
    # Where the peer sits in the config file. Every other key ties on this, which
    # is what stops two clients added in the same second, or a pair that have
    # both never handshaken, from trading places between one poll and the next.
    position = models.IntegerField(default=0)

    # ---- traffic.db, which is a text file and cannot be joined to
    #
    # The all-time totals, before the panel's offsets are taken off them. The
    # offsets stay in ClientMeta: they are what "clear this counter" writes, and
    # an admin who clears one has to see the effect on the next request, not
    # after the next time a config file happens to change.
    cum_rx = models.BigIntegerField(default=0)
    cum_tx = models.BigIntegerField(default=0)

    # ---- this client's own config file, as it was when `allowed_ips` was read
    #
    # A rebuild is triggered by the server config changing, and most of them
    # change nothing about any client file. Comparing these two numbers is a
    # stat() per client instead of an open, a read and a parse - about a
    # microsecond against a hundred - so a rebuild after an unrelated edit costs
    # milliseconds rather than the better part of a second. Zero means the file
    # was missing when it was last looked at, which never matches a real stat and
    # so is re-read every time, which is what a file that may appear deserves.
    conf_mtime_ns = models.BigIntegerField(default=0)
    conf_size = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["created_key", "position"]
        verbose_name = "client index entry"
        verbose_name_plural = "client index"

    def __str__(self) -> str:
        return self.name or self.meta.public_key


class ClientIndexState(models.Model):
    """What the files looked like when ClientIndex was last built.

    One row, and the whole of the freshness check. Serving a page compares these
    numbers against three stat() calls; equal means the copy is current and the
    query can go straight to the index, and that comparison is what the whole
    design rests on, so it is worth saying exactly what each number catches.

    The server config is stamped by modification time, size *and* inode, because
    it can be changed in two different ways. The panel rewrites it through a temp
    file and a rename, which lands a new inode and may leave the size identical.
    Anything appending in place - `cat >> awg0.conf`, an editor configured to
    write back rather than replace - keeps the inode and moves the size. Either
    one alone would miss the other; a hand edit that happened to preserve both is
    caught by the modification time, which is nanoseconds here and cannot
    plausibly repeat.

    The clients directory is stamped by modification time alone, which changes
    when an entry is added, removed or renamed within it. Every writer of a
    client config on both sides - `awg.paths.atomic_write` and bash's
    write_client_conf - builds a temp file beside the target and renames it over,
    so a rewritten client config moves this too. What it does not catch is a text
    editor writing one of those files in place, which changes no directory entry
    at all; that leaves one client's AllowedIPs stale until anything else moves,
    and it is the one gap in this scheme rather than an oversight in it.

    traffic.db is stamped because usage is a column here and sorting by it has to
    be current. It is rewritten whole, atomically, by both the collector and the
    PreDown hook.

    Nothing stamps ClientMeta, and nothing needs to: none of it is copied here.
    """

    # A single row, pinned. Its absence is a panel that has never built an index
    # and is about to.
    ROW_ID = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=ROW_ID)

    conf_mtime_ns = models.BigIntegerField(default=0)
    conf_size = models.BigIntegerField(default=0)
    conf_inode = models.BigIntegerField(default=0)
    clients_mtime_ns = models.BigIntegerField(default=0)
    traffic_mtime_ns = models.BigIntegerField(default=0)
    traffic_size = models.BigIntegerField(default=0)
    built_at = models.DateTimeField(auto_now=True)

    # The three facts about the server, rather than about any client, that the
    # client list answers with. Each was a parse of the server config on every
    # request - two of them on top of the parse the rows themselves cost, since
    # asking whether the tunnel carries IPv6 read the file again - and each is
    # settled by the same rebuild that settles the rows, from the same read.
    #
    # `free_ips` counts unnamed peers as the occupants they are, which is why it
    # is a number here and not a count over ClientIndex: a peer with no
    # "# Client" comment holds an address and has no row.
    subnet_cidr = models.CharField(max_length=64, blank=True, default="")
    free_ips = models.IntegerField(default=0)
    has_ipv6 = models.BooleanField(default=False)

    class Meta:
        verbose_name = "client index state"
        verbose_name_plural = "client index state"

    def __str__(self) -> str:
        return f"built {self.built_at:%Y-%m-%d %H:%M:%S}"
