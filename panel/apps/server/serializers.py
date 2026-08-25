"""Wire shapes for the server endpoints.

Fields are declared once, in Python spelling; ``CamelCaseMixin`` from apps.panel
renames them for the browser in both directions. Nothing here re-validates an
obfuscation parameter: ``awg.validate`` owns those rules and the sentences that
explain them, and a second copy of a rule is a copy that drifts.

The server private key is not a field of any serializer in this module. It is
not in ``store.ServerView`` either, so there is nothing to forget to exclude.
"""

from rest_framework import serializers

from apps.panel.serializers import CamelCaseMixin
from awg import validate


class ServerSerializer(CamelCaseMixin, serializers.Serializer):
    """GET api/v1/server. ``publicKey`` is derived; ``PrivateKey`` never leaves the disk."""

    iface = serializers.CharField()
    address = serializers.CharField(allow_blank=True)
    subnet_cidr = serializers.CharField(allow_blank=True)
    subnet_capacity = serializers.IntegerField()
    # The tunnel's IPv6 network and what this server does with it. Both blank
    # when the tunnel carries no IPv6 - a server that predates it and whose
    # clients are still routing only IPv4. Read-only: the mode follows from
    # what the host actually has, which the installer works out, and choosing
    # it in a form would only let an admin pick one the machine cannot do.
    subnet6_cidr = serializers.CharField(allow_blank=True)
    subnet6_mode = serializers.CharField(allow_blank=True)
    listen_port = serializers.IntegerField()
    mtu = serializers.IntegerField()
    public_key = serializers.CharField(allow_blank=True)
    dns = serializers.CharField(allow_blank=True)
    endpoint_host = serializers.CharField(allow_blank=True)
    endpoint_port = serializers.CharField(allow_blank=True)
    allowed_ips_default = serializers.CharField(allow_blank=True)
    keepalive = serializers.CharField(allow_blank=True)
    # Keyed by config key ("Jc", "H1"), so these keys are data and stay as they
    # are written in awg0.conf; only field names are camelCased.
    params = serializers.DictField(child=serializers.CharField(allow_blank=True))
    post_up = serializers.ListField(child=serializers.CharField())
    post_down = serializers.ListField(child=serializers.CharField())
    pre_down = serializers.ListField(child=serializers.CharField())


class ServerUpdateSerializer(CamelCaseMixin, serializers.Serializer):
    """PUT api/v1/server. Every field is optional: the UI sends only what changed.

    Scalars are accepted as text even where they are numbers, because the value
    is handed to ``awg.validate``, which knows that a listen port has to be
    1-65535 and can say so in a sentence. An IntegerField here would answer
    "A valid integer is required" first and hide the better message.
    """

    listen_port = serializers.CharField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    subnet_cidr = serializers.CharField(required=False, allow_blank=True)
    # The pre-CIDR spelling (three octets, /24 implied). Still accepted so a
    # script written against the old API keeps working; store._normalise
    # turns either one into the server's Address.
    subnet_base = serializers.CharField(required=False, allow_blank=True)
    mtu = serializers.CharField(required=False, allow_blank=True)
    dns = serializers.CharField(required=False, allow_blank=True)
    endpoint_host = serializers.CharField(required=False, allow_blank=True)
    endpoint_port = serializers.CharField(required=False, allow_blank=True)
    allowed_ips_default = serializers.CharField(required=False, allow_blank=True)
    keepalive = serializers.CharField(required=False, allow_blank=True)
    # An empty string clears a parameter: the line comes out of the config
    # rather than being written blank, which awg-quick reads as malformed.
    params = serializers.DictField(
        required=False, child=serializers.CharField(allow_blank=True, trim_whitespace=False)
    )
    post_up = serializers.ListField(required=False, child=serializers.CharField(allow_blank=True))
    post_down = serializers.ListField(required=False, child=serializers.CharField(allow_blank=True))
    pre_down = serializers.ListField(required=False, child=serializers.CharField(allow_blank=True))

    def validate_params(self, value: dict[str, str]) -> dict[str, str]:
        """Reject names the catalog does not know rather than dropping them silently.

        A mistyped key would otherwise be accepted, saved nowhere, and reported
        as success - and the admin would go looking for the reason their
        obfuscation change did nothing.
        """
        unknown = sorted(key for key in value if key not in validate.PARAMS)
        if unknown:
            raise serializers.ValidationError(
                f"{', '.join(unknown)}: not a parameter this panel knows about. "
                "Use the names from the parameter list."
            )
        return value


class ServerSaveResultSerializer(CamelCaseMixin, serializers.Serializer):
    """What saving cost, and whether the running interface already carries it.

    See contract 11a: obfuscation is never applied hot, so ``needs_restart``
    means the panel took the interface down and back up rather than syncing it.
    ``applied`` is false only when that could not be done - the interface was
    already down, or the restart failed - and ``warnings`` then says which.
    """

    needs_restart = serializers.BooleanField()
    must_reimport = serializers.BooleanField()
    applied = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())


class ParamSpecSerializer(CamelCaseMixin, serializers.Serializer):
    """One row of GET api/v1/server/params: the rule and the words that explain it.

    ``supported`` is added by the view from the controller's feature detection.
    Unsupported parameters are still listed - the UI greys them out and says
    why, which is information; hiding them would leave an admin wondering where
    the imitation packets went.
    """

    key = serializers.CharField()
    group = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.CharField()
    help_short = serializers.CharField()
    help_long = serializers.CharField()
    must_match_client = serializers.BooleanField()
    importer_safe = serializers.BooleanField()
    min = serializers.IntegerField(allow_null=True)
    max = serializers.IntegerField(allow_null=True)
    recommended = serializers.CharField(allow_null=True)
    default = serializers.CharField(allow_null=True)
    optional = serializers.BooleanField()
    # Names one of the flags in the status endpoint's `features` object, in the
    # same spelling, so the UI can look it up. Null means it works everywhere.
    feature = serializers.CharField(allow_null=True)
    supported = serializers.BooleanField()


class ReconfigureSerializer(CamelCaseMixin, serializers.Serializer):
    """POST api/v1/server/reconfigure. Every field optional, so ``{}`` still works.

    ``scope`` says which half of the page asked: the obfuscation set every
    client speaks, or the AmneziaWG 3.0 group that only a 3.0 peer understands.
    They are drawn separately because they are set separately - an admin who
    redraws the junk sizes has not asked to be given a header protection key.

    Nothing describes the form. There were an ``mtu`` and an ``s4`` here, each
    added so a preview could be drawn against a value the form held and the disk
    did not, and both were the same mistake: a draw is measured at save time
    against the merged config, so a number that is only in a form is never the
    one in force when the check runs, and drawing against it produced exactly
    the sets the check then refused. Everything this needs comes from the config.
    """

    profile = serializers.ChoiceField(
        choices=sorted(validate.PROFILES), default=validate.DEFAULT_PROFILE
    )
    scope = serializers.ChoiceField(choices=("obfuscation", "advanced"), default="obfuscation")


class ParamPreviewSerializer(CamelCaseMixin, serializers.Serializer):
    """A generated obfuscation set. Nothing is written until the admin saves it.

    A slot can come back blank, which is how "remove this line" is spelled: the
    generated decoy session is three to five packets long, so I4 and I5 are
    often empty on purpose rather than by omission.
    """

    params = serializers.DictField(child=serializers.CharField(allow_blank=True))
    warnings = serializers.ListField(child=serializers.CharField())


class ServerStatusSerializer(CamelCaseMixin, serializers.Serializer):
    """GET api/v1/server/status - contract 5.5."""

    iface_up = serializers.BooleanField()
    module_loaded = serializers.BooleanField()
    tools_version = serializers.CharField(allow_null=True)
    module_version = serializers.CharField(allow_null=True)
    # Already camelCased by the view: these are dictionary keys, not field
    # names, so the mixin leaves them alone by design.
    features = serializers.DictField()
    service_active = serializers.CharField()
    service_enabled = serializers.CharField()
    listening = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())
