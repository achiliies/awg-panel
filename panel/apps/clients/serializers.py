"""Wire shapes for the client endpoints.

Fields are declared once, in Python spelling; CamelCaseMixin renames them in
both directions, so the JSON the frontend's `Client` interface expects and the
snake_case the rest of the panel speaks can never drift apart by hand.

Nothing here can emit key material. `publicKey` is the only key on the wire:
the preshared key and the client's private key exist only inside
clients/<name>.conf, and only the config download and the QR endpoint are
allowed to hand that file over.
"""

from rest_framework import serializers

from apps.clients import merge, shaping
from apps.panel.serializers import CamelCaseMixin

# The database column is 190 characters; saying so here turns an overlong
# address into a field error instead of a database exception.
EMAIL_MAX = 190
NAME_MAX = 64

# A quota is stored as a 64-bit integer. Anything past a petabyte is a typo, and
# accepting it would silently overflow the column at 2**63.
QUOTA_MAX = 1 << 60


class CeilingMixin:
    """The two bandwidth fields, and the rules that need the server to answer them.

    Shared by create and update because a ceiling means the same thing whichever
    is setting it, and because the two things worth refusing are both about the
    server rather than the client: limits switched off altogether, and an upload
    ceiling on a server with nowhere to enforce one.

    Refused here rather than clamped, which is the opposite of what the reconcile
    pass does with the same disagreement - and deliberately. Here there is
    somebody to tell: they typed the number a moment ago and the message lands
    under the box they typed it in. By the time a pass is running the number has
    been stored for a while and it is the settings that moved, and failing every
    minute on it would help nobody.
    """

    def validate_down_bps(self, value: int) -> int:
        return self._shapeable(value)

    def validate_up_bps(self, value: int) -> int:
        if value and not shaping.upload_possible():
            raise serializers.ValidationError(
                "This server does not shape upload. Turn on upload shaping in the panel's "
                "settings first - it shapes the server's own outgoing traffic, so it is off "
                "by default."
            )
        return self._shapeable(value)

    @staticmethod
    def _shapeable(value: int) -> int:
        if value and not shaping.enabled():
            raise serializers.ValidationError(
                "Bandwidth limits are switched off on this server. Turn them on in the panel's "
                "settings first."
            )
        # Only when a limit is actually being set, because this one costs a parse
        # of the server config - and a subnet wide enough to fail it is one no
        # client on this server can be given a limit on, so saying it here is the
        # only place an operator finds out at all.
        if value:
            problem = shaping.subnet_problem()
            if problem:
                raise serializers.ValidationError(
                    f"This server's tunnel subnet cannot carry speed limits: {problem}"
                )
        return value


class ClientSerializer(CamelCaseMixin, serializers.Serializer):
    """One merged client. Output only: every field is derived in apps.clients.merge."""

    name = serializers.CharField()
    public_key = serializers.CharField()
    ip = serializers.CharField(allow_blank=True)
    allowed_ips = serializers.CharField(allow_blank=True)
    enabled = serializers.BooleanField()
    disabled_reason = serializers.CharField(allow_blank=True)
    # A string, not a datetime: it is the "# Created" comment from the config
    # file, and reformatting it would make the API disagree with the file.
    created_at = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    # Whether this client is past that date and switched on anyway, because an
    # admin said so. Sent on every row rather than inferred from the two fields
    # around it: a browser cannot tell a deliberate reprieve from the minute
    # before the collector's next pass, and they look identical on the row.
    expiry_overridden = serializers.BooleanField()
    quota_bytes = serializers.IntegerField()
    # How fast this client may go, in bits per second, 0 for no ceiling. What an
    # operator asked for rather than what the kernel is doing: the two agree
    # except between a bring-up and whichever pass notices, and closing that gap
    # is what the reconcile in apps.clients.shaping is for. Nothing on the wire
    # reports the kernel's own side of it, deliberately - it is one server-wide
    # fact and this is a per-client row, so answering it here would mean a
    # `tc class show` per page of clients to say the same thing on every line.
    down_bps = serializers.IntegerField()
    up_bps = serializers.IntegerField()
    email = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)
    online = serializers.BooleanField()
    # Unix seconds, as the kernel reports it. 0 means never.
    last_handshake = serializers.IntegerField()
    # Unix seconds at which anything was last heard from this peer, which is the
    # later of its handshake and the moment its receive counter last moved. The
    # field to show as "last seen": the handshake alone reads as minutes stale on
    # a client that is transferring right now, because rekeying is that rare.
    last_seen = serializers.IntegerField()
    endpoint = serializers.CharField(allow_blank=True)
    # From the server's point of view, matching `awg show dump` and traffic.db:
    # rx is the client's upload, tx its download.
    rx_bytes = serializers.IntegerField()
    tx_bytes = serializers.IntegerField()
    # Already subtracted from the two above. Sent so that a page reading the
    # collector's live blob can subtract them from its raw totals and arrive at
    # the same figure two seconds sooner - see merge._row.
    offset_rx = serializers.IntegerField()
    offset_tx = serializers.IntegerField()
    rate_rx = serializers.IntegerField()
    rate_tx = serializers.IntegerField()
    quota_used = serializers.IntegerField()
    quota_percent = serializers.IntegerField()
    # The one field the list does not carry. It comes from the client's own
    # config file, so unlike everything above it costs a read per client rather
    # than a read per request, and the table has no column for it.
    #
    # Absent and blank are different answers and the distinction is the point:
    # absent is a list row, which was never asked; blank is a single client that
    # was asked and has no DNS line of its own. A default of "" here would
    # collapse the two and let the edit form clear a client's DNS by loading a
    # row that never carried it.
    dns = serializers.CharField(allow_blank=True, required=False)
    status = serializers.CharField()
    # The IPv6 address the server routes to this client, blank when it routes
    # none, and whether the config this client is actually holding still sends
    # its IPv6 around the tunnel rather than through it.
    #
    # The second is sent rather than derived in the browser because deriving it
    # needs the server's own prefix, the client's route list and the rule that
    # relates them, and a UI that got that rule slightly wrong would quietly
    # stop reporting the leak it exists to show.
    ip6 = serializers.CharField(allow_blank=True)
    leaks_ipv6 = serializers.BooleanField()


class ClientListSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of GET api/v1/clients: one page, and the counts it is a page of."""

    clients = ClientSerializer(many=True)
    subnet_cidr = serializers.CharField()
    free_ips = serializers.IntegerField()
    total = serializers.IntegerField()
    total_all = serializers.IntegerField()
    expired_count = serializers.IntegerField()
    # What each option of `clients/bulk-remove` would take, counted over the
    # whole server. The third is the union rather than the sum: the sets overlap
    # wherever the collector has switched a lapsed client off.
    disabled_count = serializers.IntegerField()
    expired_or_disabled_count = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()


class ClientQuerySerializer(CamelCaseMixin, serializers.Serializer):
    """The query string of GET api/v1/clients, which is entirely optional.

    Nothing here is required and nothing rejects a request: a filter word this
    version does not know, or a page of "banana", is an old bookmark or a
    hand-typed URL, and answering it with the unnarrowed first page is more use
    than a 400. Only `pageSize` is clamped rather than defaulted, because a
    caller asking for more rows than the panel will send should still get rows.

    Zero is the one `pageSize` outside that range rather than below it: it asks
    for every row the query selected, and it is how the panel's own "All" is
    sent - see merge.UNPAGED for why that is a different request from naming a
    large number rather than the same one written differently.
    """

    q = serializers.CharField(required=False, allow_blank=True, max_length=200)
    status = serializers.ChoiceField(choices=merge.STATUS_FILTERS, required=False)
    sort = serializers.ChoiceField(choices=merge.SORT_KEYS, required=False)
    direction = serializers.ChoiceField(choices=("asc", "desc"), required=False)
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(
        required=False, min_value=merge.UNPAGED, max_value=merge.MAX_PAGE_SIZE
    )

    def to_query(self) -> merge.ClientQuery:
        data = self.validated_data
        return merge.ClientQuery(
            search=data.get("q", ""),
            status=data.get("status", "all"),
            sort=data.get("sort", "created"),
            descending=data.get("direction", "asc") == "desc",
            page=data.get("page", 1),
            page_size=data.get("page_size", merge.DEFAULT_PAGE_SIZE),
        )


class ClientCreateSerializer(CeilingMixin, CamelCaseMixin, serializers.Serializer):
    """POST api/v1/clients. Nothing is required; every field falls back to clients.env.

    The name is checked again by awg.store, which owns the rule; this only
    bounds the length so a 10 MB string never reaches the file system layer.

    Left out or sent blank, the server draws one - see awg.store._free_name.
    Blank counts as absent rather than as an error because the panel's own form
    prefills a suggestion, and an operator who clears that box is asking for a
    different name rather than for a message about the one they just deleted.
    """

    name = serializers.CharField(required=False, allow_blank=True, max_length=NAME_MAX)
    allowed_ips = serializers.CharField(required=False, allow_blank=True, max_length=512)
    dns = serializers.CharField(required=False, allow_blank=True, max_length=256)
    quota_bytes = serializers.IntegerField(required=False, min_value=0, max_value=QUOTA_MAX)
    # Absent is the same as 0, which is what every client has had until now: no
    # ceiling, and nothing attached to any interface on its account.
    down_bps = serializers.IntegerField(required=False, min_value=0, max_value=shaping.MAX_BPS)
    up_bps = serializers.IntegerField(required=False, min_value=0, max_value=shaping.MAX_BPS)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    email = serializers.CharField(required=False, allow_blank=True, max_length=EMAIL_MAX)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ClientBulkRemoveSerializer(CamelCaseMixin, serializers.Serializer):
    """POST api/v1/clients/bulk-remove: which categories of client to delete.

    Both default to false, so a body that names neither is a request to remove
    nothing rather than a request to remove everything. The view refuses that
    outright instead of answering "removed 0", because the two are the same
    reply to very different mistakes and one of them is a caller whose flag
    never reached the server.
    """

    expired = serializers.BooleanField(required=False, default=False)
    disabled = serializers.BooleanField(required=False, default=False)


class ClientBulkLimitSerializer(CeilingMixin, CamelCaseMixin, serializers.Serializer):
    """POST api/v1/clients/bulk-limit: one limit, written over every client.

    Both are required rather than defaulted. This endpoint destroys every
    hand-set limit on the server and there is nothing to undo it with, so a body
    that forgot half of itself must not be read as "and clear all the upload
    limits while you are here".
    """

    down_bps = serializers.IntegerField(min_value=0, max_value=shaping.MAX_BPS)
    up_bps = serializers.IntegerField(min_value=0, max_value=shaping.MAX_BPS)


class ClientUpdateSerializer(CeilingMixin, CamelCaseMixin, serializers.Serializer):
    """PUT api/v1/clients/<name>. Any subset; an absent key means "leave it alone".

    `expiresAt: null` is different from an absent `expiresAt`: the first clears
    the expiry, the second keeps it. That is why nothing here has a default.
    """

    name = serializers.CharField(required=False, max_length=NAME_MAX)
    allowed_ips = serializers.CharField(required=False, allow_blank=True, max_length=512)
    dns = serializers.CharField(required=False, allow_blank=True, max_length=256)
    quota_bytes = serializers.IntegerField(required=False, min_value=0, max_value=QUOTA_MAX)
    # 0 clears a ceiling, which is what an absent value never does. Sending it is
    # how the panel's "no limit" is spelled.
    down_bps = serializers.IntegerField(required=False, min_value=0, max_value=shaping.MAX_BPS)
    up_bps = serializers.IntegerField(required=False, min_value=0, max_value=shaping.MAX_BPS)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    email = serializers.CharField(required=False, allow_blank=True, max_length=EMAIL_MAX)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    enabled = serializers.BooleanField(required=False)
