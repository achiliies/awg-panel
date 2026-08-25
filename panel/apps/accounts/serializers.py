"""Request and response shapes for sign-in and TOTP enrolment.

Fields are declared in snake_case and reach the browser in camelCase: the
CamelCaseMixin from apps.panel does the translation, in both directions, so no
name is ever spelled twice.
"""

import re

from rest_framework import serializers

from apps.panel.serializers import CamelCaseMixin

from .tokens import MAX_LIFETIME_SEC, MIN_LIFETIME_SEC

# What an authenticator app produces. Six digits is the universal default;
# eight exists and some apps offer it, so the window is deliberately wider than
# the one device the panel enrols.
_TOTP_RE = re.compile(r"^\d{6,8}$")

_TOTP_HELP = "Enter the 6-digit code from your authenticator app."

# Argon2 is slow on purpose. Without a ceiling, an unauthenticated caller could
# post a megabyte of "password" and buy a second of CPU per request.
_MAX_PASSWORD = 256

# What an account may be called. Django's own validator allows the same set, and
# the leading character is the one thing added to it: a name beginning with a
# space or a dot is a name somebody will fail to type back at the login form.
# 150 is the column, so the regex stops one short of it after the first
# character.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,149}$")

_USERNAME_HELP = "Use letters, digits, and . _ @ + - only, starting with a letter or a digit."

# What a token may be called: anything printable, which is a far wider set than
# a username allows and deliberately so. A username is typed at a login form; a
# token name is only ever read, in a list and in the activity log, so "Grafana
# (staging)" is exactly the sort of thing it should be possible to write. What is
# refused is a control character, which would corrupt the line it is shown on,
# and a name that is empty once the whitespace around it has been trimmed.
_TOKEN_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")

_TOKEN_NAME_HELP = "Give the token a name you will recognise in the activity log."


def _clean_token(value: str) -> str:
    """Normalise a typed code, or "" when the field was left empty.

    Authenticator apps display codes as "123 456" and people paste them that
    way; rejecting the space would be a self-inflicted support question.
    """
    token = re.sub(r"[\s-]", "", value or "")
    if not token:
        return ""
    if not _TOTP_RE.match(token):
        raise serializers.ValidationError(_TOTP_HELP)
    return token


class LoginSerializer(CamelCaseMixin, serializers.Serializer):
    """POST auth/login. ``totp`` is absent on the first attempt by design."""

    username = serializers.CharField(max_length=150)
    # trim_whitespace off: a leading or trailing space is a legitimate part of
    # a password, and silently stripping it locks the owner out of their panel.
    password = serializers.CharField(max_length=_MAX_PASSWORD, trim_whitespace=False)
    totp = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_totp(self, value: str) -> str:
        return _clean_token(value)


class CallingTokenSerializer(CamelCaseMixin, serializers.Serializer):
    """The API token this very request presented, for the script holding it.

    Three fields and no more: the name, so a log line can be matched to a row in
    the panel, and the two that decide whether the credential is about to stop
    working. Not the hint, not the address it was last used from, and above all
    not any other token - this is a caller identifying itself, not a listing.

    That distinction is what keeps it on the right side of the line
    apps.accounts.permissions draws. A token may not reach the credential
    surface: it cannot mint another, revoke one, read the list or change the
    password. Being told the expiry of the secret it is already holding grants
    it nothing it did not walk in with, and withholding it only means a script
    finds out that its token lapsed by failing at three in the morning.
    """

    name = serializers.CharField()
    expires_at = serializers.DateTimeField(allow_null=True)
    renew_on_use = serializers.BooleanField()


class SessionSerializer(CamelCaseMixin, serializers.Serializer):
    """GET auth/session: everything the SPA needs before it renders anything."""

    authenticated = serializers.BooleanField()
    username = serializers.CharField(allow_blank=True)
    otp_required = serializers.BooleanField()
    version = serializers.CharField()
    theme = serializers.CharField()
    language = serializers.CharField()
    base_path = serializers.CharField()
    mock = serializers.BooleanField()
    # Null for a browser, which has a session rather than a token. Always
    # present, so a caller reads one field rather than checking for one.
    token = CallingTokenSerializer(allow_null=True)


class LoginSessionSerializer(CamelCaseMixin, serializers.Serializer):
    """One row of GET settings/sessions: a browser that can reach this panel.

    Output only. The session key it is really keyed by is deliberately absent:
    it is the cookie value of the browser being described, so `id` is an opaque
    identifier that means nothing outside this endpoint.
    """

    id = serializers.UUIDField()
    # The session making this very request, which is the one that may not be
    # revoked and the one the list puts first.
    current = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    last_seen_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    # "" when the address could not be read, or came from a header the panel was
    # not told to trust. Never a guess.
    ip = serializers.CharField(allow_blank=True)
    # Read out of the user agent, and both blank when it says nothing familiar.
    # The raw string is sent as well, because it is the only part of this that
    # is evidence rather than interpretation.
    browser = serializers.CharField(allow_blank=True)
    platform = serializers.CharField(allow_blank=True)
    user_agent = serializers.CharField(allow_blank=True)


class LoginSessionListSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of GET settings/sessions."""

    sessions = LoginSessionSerializer(many=True)


class RevokedSessionsSerializer(CamelCaseMixin, serializers.Serializer):
    """How many browsers were signed out, so the UI can say so in one sentence."""

    ended = serializers.IntegerField()


class CredentialsSerializer(CamelCaseMixin, serializers.Serializer):
    """POST settings/account: the current password, and what to change.

    One request for both halves of the credential, because they are entered on
    one form and because a rename and a new password are the same act - the
    admin is replacing what signs them in. Either half may be left out, and a
    request that asks for neither is refused rather than answered with a
    cheerful 200 that changed nothing.
    """

    current = serializers.CharField(max_length=_MAX_PASSWORD, trim_whitespace=False)
    # Blank, or the name the account already has, means "leave the name alone".
    username = serializers.CharField(required=False, allow_blank=True, default="", max_length=150)
    # trim_whitespace off for the same reason the login form has it off: a space
    # at either end is part of the password somebody chose.
    new = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=_MAX_PASSWORD,
        trim_whitespace=False,
    )

    def validate_username(self, value: str) -> str:
        name = (value or "").strip()
        if name and not _USERNAME_RE.match(name):
            raise serializers.ValidationError(_USERNAME_HELP)
        return name

    def validate(self, attrs: dict) -> dict:
        if not attrs.get("username") and not attrs.get("new"):
            raise serializers.ValidationError("Fill in a new username, a new password, or both.")
        return attrs


class TotpEnableSerializer(CamelCaseMixin, serializers.Serializer):
    """POST settings/2fa/enable. No code means "start enrolment"."""

    code = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_code(self, value: str) -> str:
        return _clean_token(value)


class TotpEnrolmentSerializer(CamelCaseMixin, serializers.Serializer):
    """Step one of enrolment: what the operator scans or types into their app."""

    secret = serializers.CharField()
    provisioning_uri = serializers.CharField()
    qr = serializers.CharField()


class TotpStatusSerializer(CamelCaseMixin, serializers.Serializer):
    """The result of turning two-factor on or off."""

    enabled = serializers.BooleanField()


def _clean_name(value: str) -> str:
    """A token name with its surrounding whitespace gone, or a field error."""
    name = (value or "").strip()
    if not name or not _TOKEN_NAME_RE.match(name):
        raise serializers.ValidationError(_TOKEN_NAME_HELP)
    return name


class ApiTokenSerializer(CamelCaseMixin, serializers.Serializer):
    """One row of GET settings/tokens. Output only, and never the secret.

    The secret appears in exactly one response in the whole API - the one that
    creates it - and this serializer is why that is a structural fact rather
    than a discipline: there is no field here it could be written into.
    """

    id = serializers.UUIDField()
    name = serializers.CharField()
    # The last few characters of the secret, so a row can be matched against the
    # copy in a CI configuration without the secret being recoverable from it.
    hint = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    # Null for a token that does not expire, which is a choice rather than an
    # omission - see the create serializer below.
    expires_at = serializers.DateTimeField(allow_null=True)
    # The window in seconds, which is what renewal restores on each use. Null
    # exactly when expires_at is.
    expires_in = serializers.IntegerField(allow_null=True)
    renew_on_use = serializers.BooleanField()
    # Null until the token has been presented once.
    last_used_at = serializers.DateTimeField(allow_null=True)
    last_used_ip = serializers.CharField(allow_blank=True)
    # Decided against the server's clock, because that is the clock the token is
    # actually checked against.
    expired = serializers.BooleanField()


class ApiTokenListSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of GET settings/tokens."""

    tokens = ApiTokenSerializer(many=True)


class ApiTokenIssuedSerializer(CamelCaseMixin, serializers.Serializer):
    """The body of POST settings/tokens: the row, and the one sight of the secret.

    The two are separate fields rather than one merged object so that nothing
    downstream can pass this to something expecting a listing row and quietly
    carry the secret along with it.
    """

    token = ApiTokenSerializer()
    secret = serializers.CharField()


class ApiTokenCreateSerializer(CamelCaseMixin, serializers.Serializer):
    """POST settings/tokens: what to call it, how long it lives, and whether use renews it.

    ``expires_in`` is a number of seconds rather than a date because that is
    also the renewal window, and a token whose "reset on use" restored it to a
    fixed date would not be renewing anything. Zero - or the field left out -
    means it never expires, which is why it has to be said explicitly in the
    request rather than being what an absent field quietly produces.

    ``name`` left out or sent blank means the panel draws one - see
    apps.accounts.tokens.free_name. A token with no name at all is what this
    used to refuse, and it was right to: the activity log writes the name beside
    everything the token does, so a nameless token leaves a log that says
    "admin" for work nobody was present for. A random name is a poorer label
    than "nightly backup" and a far better one than nothing, and it can be
    renamed afterwards.
    """

    name = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    expires_in = serializers.IntegerField(required=False, allow_null=True, default=None)
    renew_on_use = serializers.BooleanField(required=False, default=False)

    def validate_name(self, value: str) -> str:
        # Blank is the request to be named, so it passes through as blank rather
        # than through the rule that refuses an empty name.
        name = (value or "").strip()
        return _clean_name(name) if name else ""

    def validate_expires_in(self, value: int | None) -> int | None:
        return _clean_lifetime(value)


class ApiTokenUpdateSerializer(CamelCaseMixin, serializers.Serializer):
    """PUT settings/tokens/<id>: the two things about a token that may change.

    Both are optional and a request that carries neither is refused, by the same
    rule the account form follows: a 200 that changed nothing is a worse answer
    than a sentence saying so.
    """

    name = serializers.CharField(required=False, max_length=64)
    renew_on_use = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate_name(self, value: str) -> str:
        return _clean_name(value)

    def validate(self, attrs: dict) -> dict:
        if attrs.get("name") is None and attrs.get("renew_on_use") is None:
            raise serializers.ValidationError("Change the name, the renewal setting, or both.")
        return attrs


def _clean_lifetime(value: int | None) -> int | None:
    """Seconds of life, or None for a token that does not expire."""
    if value is None or value == 0:
        return None
    if value < 0:
        raise serializers.ValidationError("An expiry cannot be in the past.")
    if value < MIN_LIFETIME_SEC:
        raise serializers.ValidationError(
            f"The shortest a token may live is {MIN_LIFETIME_SEC // 60} minutes."
        )
    if value > MAX_LIFETIME_SEC:
        raise serializers.ValidationError(
            f"The longest a token may live is {MAX_LIFETIME_SEC // 86400} days."
        )
    return value


class TotpDisableSerializer(CamelCaseMixin, serializers.Serializer):
    """POST settings/2fa/disable. The password is the proof, not a code.

    A lost phone is the normal reason to be here, so demanding the second factor
    to remove the second factor would leave the server shell as the only way out.
    """

    password = serializers.CharField(max_length=_MAX_PASSWORD, trim_whitespace=False)

    def to_internal_value(self, data: dict) -> dict:
        # The password-change endpoint next door spells this field "current".
        # Accepting both costs three lines and saves an avoidable 400 when the
        # two forms in the UI are filled in from the same component.
        if isinstance(data, dict) and "password" not in data and "current" in data:
            data = {**data, "password": data["current"]}
        return super().to_internal_value(data)
