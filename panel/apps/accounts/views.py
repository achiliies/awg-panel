"""Sign in, sign out, the SPA's bootstrap payload, the account, TOTP and tokens.

Everything from the account form downwards carries ``CREDENTIAL_PERMISSIONS``,
which is the one rule worth reading before the code: a request that an API token
authenticated may not touch the credentials that authorise it. See
apps.accounts.permissions for why that is a property of the feature rather than
caution about it.
"""

import base64
import io
import logging
import uuid
from urllib.parse import quote, urlencode

import segno
from axes.helpers import get_cool_off, get_failure_limit
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, update_session_auth_hash
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.signals import user_login_failed
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django_otp import DEVICE_ID_SESSION_KEY, devices_for_user
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.events import kinds, recorder
from awg.errors import Conflict, ValidationError
from awgui.middleware import panel_setting

from . import sessions, tokens
from .models import ApiToken
from .permissions import CREDENTIAL_PERMISSIONS
from .serializers import (
    ApiTokenCreateSerializer,
    ApiTokenIssuedSerializer,
    ApiTokenListSerializer,
    ApiTokenSerializer,
    ApiTokenUpdateSerializer,
    CredentialsSerializer,
    LoginSerializer,
    LoginSessionListSerializer,
    RevokedSessionsSerializer,
    SessionSerializer,
    TotpDisableSerializer,
    TotpEnableSerializer,
    TotpEnrolmentSerializer,
    TotpStatusSerializer,
)

log = logging.getLogger(__name__)

# The SPA reveals its code field on exactly this string. It is a marker rather
# than a sentence because it is a step in the handshake, not a failure the user
# has to read; docs/API.md pins it.
TOTP_REQUIRED = "totp_required"

# Deliberately the same answer for a wrong password and a username that does not
# exist. The panel has one account and its name is usually "admin", so the only
# thing a distinction would speed up is somebody else's guessing.
BAD_CREDENTIALS = "Wrong username or password."

BAD_TOTP = "That code is not valid. Check your authenticator app and try again."

# The proof for every change to the account itself, so it is worded once here
# and shown under the field it belongs to.
BAD_CURRENT = "That is not your current password."

# Shown when the device is enrolled but the code was refused mid-enrolment,
# where a wrong phone clock is by far the most common cause.
BAD_TOTP_ENROL = (
    "That code did not match. Wait for the next one, and check that the clock on "
    "the phone is set automatically."
)

_QR_BOX = 6
_QR_BORDER = 2


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    """POST api/v1/auth/login -> the session payload, or 401/403.

    csrf_protect by hand: DRF only enforces CSRF for a request that already has
    a session user, so without it the login form would be the one unprotected
    mutation in the panel and a third-party page could sign a browser into an
    account of its choosing. GET auth/session issues the cookie the SPA sends.
    """

    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]
        token = serializer.validated_data["totp"]

        # The DRF request object is passed on purpose. axes writes its
        # per-request flags onto whatever object it is handed, and its
        # middleware only inspects the underlying HttpRequest, so keeping the
        # flag on the wrapper lets the lockout be answered here in the panel's
        # JSON error shape instead of being replaced by the plain-text body
        # axes returns by default. The database record - the thing that makes
        # the lockout outlive the request - is written either way.
        user = authenticate(request, username=username, password=password)
        if user is None:
            return _refused(request, username, BAD_CREDENTIALS)

        devices = list(devices_for_user(user, confirmed=True))
        device = None
        if devices:
            if not token:
                return _error(TOTP_REQUIRED, status.HTTP_401_UNAUTHORIZED)
            # A device that is backing off after a wrong code refuses the right
            # one in exactly the same way, so check that first and say which of
            # the two it is. Not counted as a login failure: nothing was tried.
            wait = min((_throttle_wait(candidate) for candidate in devices), default=0)
            if wait:
                message = _throttle_message(wait)
                return _error(message, status.HTTP_401_UNAUTHORIZED, {"totp": message})
            device = next(
                (candidate for candidate in devices if candidate.verify_token(token)), None
            )
            if device is None:
                # authenticate() succeeded, so axes saw no failure and would
                # never count this. Without the signal the code field is an
                # unlimited guessing oracle against a six-digit secret.
                user_login_failed.send(
                    sender=__name__, credentials={"username": username}, request=request
                )
                return _refused(request, username, BAD_TOTP, {"totp": BAD_TOTP})

        # auth_login() only rotates the session key when the session was
        # anonymous or belonged to somebody else; re-authenticating as the same
        # user keeps it. Rotating first makes it unconditional, which is the
        # whole point of doing it at login.
        request.session.cycle_key()
        auth_login(request, user)
        if device is not None:
            otp_login(request, device)
        # After login(), because that is what settles the session key this row
        # is named by; before the response, so the browser appears in the
        # session list from its very first page.
        sessions.remember(request)

        log.info("%s signed in%s", username, " with a TOTP code" if device else "")
        recorder.record(kinds.AUTH_SIGNED_IN, request, totp=device is not None)
        return Response(_session_payload(request))


class LogoutView(APIView):
    """POST api/v1/auth/logout -> 204, session gone.

    Closed to a token for want of anything to do: there is no session behind one
    to end, and answering 204 would record a sign-out that did not happen. A
    script that wants its credential to stop working revokes the token.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        # Before the session is flushed, which is what takes the account off the
        # request: an event recorded after it would have nobody's name on it.
        recorder.record(kinds.AUTH_SIGNED_OUT, request)
        # Read before the session is flushed, which is what makes the key
        # unavailable a line later.
        sessions.forget(request.session.session_key)
        auth_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class SessionView(APIView):
    """GET api/v1/auth/session -> who you are, plus what the SPA needs to boot.

    Open to anonymous callers: it is how the login page learns the panel name
    and the theme, and how every client gets its CSRF cookie. It reveals
    nothing that the login page does not already show.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response(_session_payload(request))


class AccountView(APIView):
    """POST api/v1/settings/account -> the session payload, with the new name in it.

    The username and the password are changed through one endpoint because they
    are one credential: the form asks for the current password once and the
    admin fills in whichever half they came to replace. Either may be left out;
    a request that asks for neither is refused by the serializer rather than
    answered with a 200 that changed nothing.

    Nothing is written until both halves are known to be acceptable, so a new
    password that Django's validators refuse cannot leave the account renamed.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        serializer = CredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        username = serializer.validated_data["username"]
        password = serializer.validated_data["new"]

        if not user.check_password(serializer.validated_data["current"]):
            raise ValidationError(
                {"current": BAD_CURRENT},
                "Nothing was changed: that is not your current password.",
            )

        was = user.get_username()
        renamed = bool(username) and username != was
        fields: list[str] = []

        if renamed:
            _check_name_free(user, username)
            setattr(user, user.USERNAME_FIELD, username)
            fields.append(user.USERNAME_FIELD)
        if password:
            # AUTH_PASSWORD_VALIDATORS is empty, so this accepts whatever the
            # owner picked; the call stays because that setting is where a
            # deployment puts rules back, and it is the only thing that would
            # then have to change. Run after the rename has been put on the
            # instance, so a validator that compares a password against the
            # account name sees the name it is really going to be used with, and
            # before anything is saved, so a refusal leaves the account
            # untouched.
            validate_password(password, user)
            user.set_password(password)
            fields.append("password")

        user.save(update_fields=fields)

        # Every other browser is signed out for either change, because either one
        # replaces what those browsers signed in with. A password change has to
        # do it - that is the whole point of changing it after a laptop goes
        # missing - and a rename that left them running would leave sessions
        # behind under a name that no longer exists.
        key = request.session.session_key
        sessions.revoke_others(user, key)
        if password:
            # Rotates this session's key and re-signs it against the new hash, so
            # the browser that made the change stays signed in while every other
            # cookie is now stale. The row describing this session is moved onto
            # the new key rather than left orphaned, or the session list would
            # forget when this browser signed in.
            update_session_auth_hash(request, user)
            sessions.rekey(key, request.session.session_key)

        if renamed:
            log.info("the account %s was renamed to %s", was, username)
            recorder.record(kinds.AUTH_USERNAME_CHANGED, request, name=was, target=username)
        if password:
            log.info("password changed for %s", user.get_username())
            recorder.record(kinds.AUTH_PASSWORD_CHANGED, request, target=user.get_username())

        return Response(_session_payload(request))


class LoginSessionsView(APIView):
    """GET api/v1/settings/sessions -> every browser signed in to this account.

    Only this account's own sessions, though the panel has one account: an
    endpoint that answers "whose sessions?" with "all of them" is one account
    away from being wrong, and the filter costs nothing.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def get(self, request: Request) -> Response:
        rows = sessions.for_user(request.user, request.session.session_key)
        return Response(LoginSessionListSerializer({"sessions": rows}).data)


class LoginSessionView(APIView):
    """DELETE api/v1/settings/sessions/<id> -> 204, and that browser is signed out.

    Immediately, not at its next expiry: the session itself is deleted, so the
    cookie the other browser still holds names nothing.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def delete(self, request: Request, ident: uuid.UUID) -> Response:
        ended = sessions.revoke(request.user, ident, request.session.session_key)
        browser, platform = sessions.describe_agent(ended.user_agent)
        # Which browser this was and where it was, because "another session" is
        # the description the admin was trying to get rid of when they pressed
        # the button: the row they picked it from is deleted a line above, and
        # the list is the only place it was ever written down. Both halves can be
        # empty - an unrecognised agent, an address behind a proxy the panel does
        # not trust - and the browser's sentence has a wording for each of them.
        recorder.record(
            kinds.AUTH_SESSION_REVOKED,
            request,
            target=ended.ip,
            browser=browser,
            platform=platform,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginSessionsRevokeOthersView(APIView):
    """POST api/v1/settings/sessions/revoke-others -> how many were ended.

    The one thing to do after a laptop goes missing, without having to work out
    which row it is. This session survives, so the admin is still signed in to
    finish the job - changing the password is usually the next thing they do,
    and that ends other sessions too.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        ended = sessions.revoke_others(request.user, request.session.session_key)
        # Only when something actually ended, by the same rule as a client sweep
        # that removed nothing: the button is not offered when there is nothing
        # to end, so a zero here is a stale page or a session that expired on its
        # own between the list and the click - and "signed out 0 other browsers"
        # is a line about neither.
        if ended:
            recorder.record(kinds.AUTH_SESSIONS_REVOKED, request, count=ended)
        return Response(RevokedSessionsSerializer({"ended": ended}).data)


class TotpEnableView(APIView):
    """POST api/v1/settings/2fa/enable, called twice.

    Without a code it creates an unconfirmed device and returns the secret to
    scan. With a code it confirms that device. The device stays unconfirmed
    until a real code arrives, so an enrolment that is abandoned half way
    through cannot lock anybody out.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        serializer = TotpEnableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"]
        user = request.user

        if code:
            return Response(_confirm_device(request, user, code))
        return Response(_start_enrolment(user))


class TotpDisableView(APIView):
    """POST api/v1/settings/2fa/disable -> removes every TOTP device."""

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        serializer = TotpDisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user

        if not user.check_password(serializer.validated_data["password"]):
            raise ValidationError(
                {"password": BAD_CURRENT},
                "Two-factor authentication was not changed: the password did not match.",
            )

        removed = TOTPDevice.objects.filter(user=user).count()
        TOTPDevice.objects.filter(user=user).delete()
        # The session still names the device that just stopped existing; leaving
        # the key behind would make every later request look up a missing row.
        request.session.pop(DEVICE_ID_SESSION_KEY, None)
        log.info("two-factor disabled for %s (%d device(s) removed)", user.get_username(), removed)
        recorder.record(kinds.AUTH_TOTP_DISABLED, request, target=user.get_username())
        return Response(TotpStatusSerializer({"enabled": False}).data)


class ApiTokensView(APIView):
    """GET api/v1/settings/tokens -> the tokens; POST -> one more, with its secret.

    The two halves of the one thing an admin does here: look at what can reach
    this panel without a browser, and issue something that can.

    The POST response is the only place in the whole API a token's secret
    appears. It is returned rather than shown again later because it is not kept
    - the row holds a hash - so a token that was not copied out of this response
    is replaced rather than recovered, and the UI says so at the moment it shows
    it.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def get(self, request: Request) -> Response:
        rows = tokens.for_user(request.user)
        return Response(ApiTokenListSerializer({"tokens": rows}).data)

    def post(self, request: Request) -> Response:
        serializer = ApiTokenCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        lifetime = serializer.validated_data["expires_in"]
        renew = serializer.validated_data["renew_on_use"]

        # No name is a request to be given one, which is the panel's own form
        # with its suggestion cleared as well as a script that never cared.
        # Checked before anything is generated either way, so the ordinary
        # duplicate is a sentence about the name rather than an integrity error
        # caught later.
        if name:
            tokens.check_name_free(request.user, name)
        else:
            name = tokens.free_name(request.user)
        if renew and not lifetime:
            raise ValidationError(
                {"renewOnUse": tokens.RENEW_NEEDS_EXPIRY}, tokens.RENEW_NEEDS_EXPIRY
            )

        token, secret = tokens.issue(request.user, name=name, lifetime=lifetime, renew_on_use=renew)
        log.info("API token %s created by %s", name, request.user.get_username())
        # The window rather than the date it produced: read a year later, "expires
        # 2026-09-14" says nothing about what was chosen, and the date is on the
        # row anyway for as long as the token exists. Zero is the panel's word for
        # "never" here, as it is in the request that asked for it.
        recorder.record(
            kinds.AUTH_TOKEN_CREATED,
            request,
            target=name,
            seconds=lifetime or 0,
            renew=bool(token.renew_on_use),
        )
        return Response(
            ApiTokenIssuedSerializer({"token": tokens.describe(token), "secret": secret}).data,
            status=status.HTTP_201_CREATED,
        )


class ApiTokenView(APIView):
    """PUT api/v1/settings/tokens/<id> to change one, DELETE to revoke it.

    Renaming and the renewal switch are the whole of what PUT accepts; see
    apps.accounts.tokens.update for why the secret and the expiry are not on
    that list.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def put(self, request: Request, ident: uuid.UUID) -> Response:
        serializer = ApiTokenUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token, was, changed = tokens.update(
            request.user,
            ident,
            name=serializer.validated_data.get("name"),
            renew_on_use=serializer.validated_data["renew_on_use"],
        )

        # Nothing recorded for a request that asked for the values the token
        # already had, by the same rule as a sweep that removed nothing: an
        # event saying something changed, on a day nothing did, is worse than no
        # event at all.
        if changed:
            log.info("API token %s was changed", token.name)
            recorder.record(
                kinds.AUTH_TOKEN_UPDATED,
                request,
                target=token.name,
                # The old name, so a rename reads as one rather than as a change
                # to a token nobody remembers by that name. "" when it kept its
                # name, and the presence of one is what the browser's sentence
                # branches on - see sentenceContext in EventLog.tsx.
                name=was,
                renew=token.renew_on_use,
            )
        return Response(ApiTokenSerializer(tokens.describe(token)).data)

    def delete(self, request: Request, ident: uuid.UUID) -> Response:
        token = tokens.revoke(request.user, ident)
        recorder.record(kinds.AUTH_TOKEN_REVOKED, request, target=token.name)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _error(detail: str, code: int, errors: dict[str, str] | None = None) -> Response:
    """The body awgui.exceptions builds, for the statuses DRF cannot raise cleanly.

    Raising AuthenticationFailed would arrive as a 403, because DRF downgrades
    401 whenever the authenticator offers no WWW-Authenticate challenge and
    SessionAuthentication never does. The SPA needs the difference: 401 means
    "show the login form", 403 means "you are blocked".
    """
    return Response({"detail": detail, "errors": dict(errors or {})}, status=code)


def _refused(
    request: Request, username: str, message: str, errors: dict[str, str] | None = None
) -> Response:
    """The reply to a sign-in that did not work, and the row that records it.

    Both refusals go through here - a wrong password and a wrong code - because
    from the outside they are one thing: somebody tried to get in and did not.
    The distinction the log does draw is between an attempt and a knock at a
    locked door, which is what `_record_refusal` is about.
    """
    locked = _lockout_response(request)
    _record_refusal(request, username, locked=locked is not None)
    return locked or _error(message, status.HTTP_401_UNAUTHORIZED, errors)


def _record_refusal(request: Request, username: str, *, locked: bool) -> None:
    """Write down a refused sign-in, up to the attempt that shuts the address out.

    This is the one path in the panel an outsider can drive as often as they
    like, so it is the one place where "record what happened" has to be bounded
    by something. django-axes counts the failures per address and leaves the
    running total on the request, and that is the counter to bound it by: every
    attempt below the limit is a line, the one that reaches the limit is the
    lockout, and anything past it is somebody knocking at a door that is already
    locked and writes nothing at all.

    Bounded, and not merely slowed down, which is the part that matters.
    AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT is on by default, so a flood
    keeps extending its own cool-off and the count never falls back inside the
    limit while it continues - which means the whole flood costs this table the
    handful of lines that led up to it and nothing more. An address that goes
    quiet long enough for the count to expire and comes back is a new lockout and
    gets a new line, which is what it is.

    What is deliberately lost is how many times a blocked address tried again.
    django-axes keeps that itself, and the journal beside this log has a line for
    every one of them.
    """
    failures = int(getattr(request, "axes_failures_since_start", 0) or 0)
    # A limit of zero would mean axes never locks out, in which case nothing here
    # bounds the count and one line is all this may safely write.
    limit = max(get_failure_limit(request, {"username": username}), 1)
    if failures <= 0 or failures > limit:
        return

    kind = kinds.AUTH_LOCKED_OUT if locked else kinds.AUTH_SIGN_IN_FAILED
    # The name is what was typed, not an account: it belongs in the detail, where
    # it reads as a claim, rather than in `actor`, which is a fact about who did
    # something and here is nobody.
    #
    # The two counters go with it, because one refused sign-in means nothing on
    # its own and a run of them is the whole signal. Which attempt this was and
    # how many the address is allowed turns a page of identical lines into a
    # countdown, and it is the same pair that decides whether this row is a
    # refusal or the lockout - so the log can be read for how close somebody got
    # without going to the journal for it.
    recorder.record(kind, request, name=username, tries=failures, limit=limit)


def _lockout_response(request: Request) -> Response | None:
    """The 403 for a client axes has blocked, or None when it has not.

    ``errors["lockout"]`` carries the cool-off in seconds so the login form can
    count down rather than invite another attempt that cannot succeed.
    """
    if not getattr(request, "axes_locked_out", False):
        return None

    cool_off = get_cool_off(request)
    if cool_off is None:
        # AXES_COOLOFF_TIME unset means the block never expires on its own.
        return _error(
            "This address is blocked after too many failed sign-in attempts. Clear it on the "
            "server with 'sudo awg-panel manage axes_reset'.",
            status.HTTP_403_FORBIDDEN,
            {"lockout": "0"},
        )

    seconds = max(int(cool_off.total_seconds()), 0)
    return _error(
        f"Too many failed sign-in attempts from this address. Try again in {_humanise(seconds)}.",
        status.HTTP_403_FORBIDDEN,
        {"lockout": str(seconds)},
    )


def _humanise(seconds: int) -> str:
    """A duration written the way a person would say it."""
    if seconds < 60:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours = round(minutes / 60)
    return "1 hour" if hours == 1 else f"{hours} hours"


def _session_payload(request: Request) -> dict:
    user = request.user
    authenticated = bool(user and user.is_authenticated)
    payload = {
        "authenticated": authenticated,
        "username": user.get_username() if authenticated else "",
        # Whether this account is protected by TOTP, not whether the caller
        # still owes a code: the login endpoint answers that with totp_required.
        # Left false for anonymous callers, so the login page cannot be used to
        # find out whether the admin has a second factor.
        "otp_required": authenticated and _has_confirmed_device(user),
        "version": settings.PANEL_VERSION,
        "theme": panel_setting("theme", "system"),
        "language": panel_setting("language", "en"),
        "base_path": settings.BASE_PATH,
        "mock": settings.PANEL_MOCK,
        # What authenticated this request, when that was a token rather than a
        # browser. See CallingTokenSerializer for why a script is allowed to ask
        # about the credential it is already holding.
        "token": _calling_token(request),
    }
    return SessionSerializer(payload).data


def _calling_token(request: Request) -> dict | None:
    """The token behind this request, or None for a session and for nobody."""
    token = getattr(request, "auth", None)
    if not isinstance(token, ApiToken):
        return None
    return {
        "name": token.name,
        "expires_at": token.expires_at,
        "renew_on_use": token.renew_on_use,
    }


def _has_confirmed_device(user: AbstractBaseUser) -> bool:
    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def _check_name_free(user: AbstractBaseUser, username: str) -> None:
    """Refuse a name another account already answers to.

    Case-insensitively, though the column is not: two accounts called "admin"
    and "Admin" would be a login form nobody could reason about, and the panel
    is normally installed with a single account anyway. The account being
    renamed is excluded, so changing only the capitalisation of your own name
    is allowed.
    """
    user_model = get_user_model()
    field = user_model.USERNAME_FIELD
    taken = user_model.objects.exclude(pk=user.pk).filter(**{f"{field}__iexact": username}).exists()
    if taken:
        raise ValidationError(
            {"username": "Another account is already called that."},
            f"Nothing was changed: there is already an account called {username}.",
        )


def _start_enrolment(user: AbstractBaseUser) -> dict:
    """Create the unconfirmed device and return what the operator has to scan."""
    if _has_confirmed_device(user):
        raise Conflict(
            "Two-factor authentication is already on for this account. Turn it off first if "
            "you want to enrol a different device."
        )

    # A half-finished enrolment is worthless and its secret may be sitting in
    # somebody's screenshot; each visit to this page starts a fresh one.
    TOTPDevice.objects.filter(user=user, confirmed=False).delete()
    device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)

    uri = _provisioning_uri(device)
    return TotpEnrolmentSerializer(
        {"secret": _secret(device), "provisioning_uri": uri, "qr": _qr_data_uri(uri)}
    ).data


def _confirm_device(request: Request, user: AbstractBaseUser, code: str) -> dict:
    """Confirm the pending device, or explain why the code was refused."""
    device = TOTPDevice.objects.filter(user=user, confirmed=False).order_by("-id").first()
    if device is None:
        raise Conflict(
            "There is no two-factor setup waiting to be confirmed. Start again to get a new "
            "QR code."
        )

    # django_otp backs off exponentially after a wrong code, and verify_token
    # answers a throttled attempt exactly like a wrong one. Saying "that code
    # did not match" to somebody who just typed the right one would send them
    # hunting for a fault in their phone's clock.
    wait = _throttle_wait(device)
    if wait:
        message = _throttle_message(wait)
        raise ValidationError({"code": message}, message)

    if not device.verify_token(code):
        raise ValidationError({"code": BAD_TOTP_ENROL}, BAD_TOTP_ENROL)

    device.confirmed = True
    device.save(update_fields=["confirmed"])
    TOTPDevice.objects.filter(user=user, confirmed=False).delete()

    # The session that just proved possession of the device counts as verified;
    # without this the operator would be asked for a code on the next request.
    otp_login(request, device)
    log.info("two-factor enabled for %s", user.get_username())
    recorder.record(kinds.AUTH_TOTP_ENABLED, request, target=user.get_username())
    return TotpStatusSerializer({"enabled": True}).data


def _throttle_message(wait: int) -> str:
    return f"Too many wrong codes. Wait {_humanise(wait)} and enter the next code your app shows."


def _throttle_wait(device: TOTPDevice) -> int:
    """Seconds until this device will look at a code again, or 0 if it will now."""
    allowed, details = device.verify_is_allowed()
    if allowed:
        return 0
    locked_until = (details or {}).get("locked_until")
    if locked_until is None:
        return 1
    return max(int((locked_until - timezone.now()).total_seconds() + 0.5), 1)


def _secret(device: TOTPDevice) -> str:
    """The shared secret in base32, for people whose camera will not cooperate.

    Padding is stripped: authenticator apps accept it either way, and a trailing
    "=" in a field somebody is retyping by hand is one more thing to get wrong.
    """
    return base64.b32encode(device.bin_key).decode("ascii").rstrip("=")


def _provisioning_uri(device: TOTPDevice) -> str:
    """otpauth:// URI labelled with the panel name.

    django_otp builds one of these itself, and its issuer would be right now
    that the name is a constant. It is still assembled here for the encoding:
    django_otp form-encodes the query, so "AWG Panel" reaches the authenticator
    as "AWG+Panel", which some apps display literally.
    """
    issuer = settings.PANEL_NAME
    label = f"{issuer}:{device.user.get_username()}"
    params = urlencode(
        {
            "secret": _secret(device),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": device.digits,
            "period": device.step,
        },
        # Percent-encoding, not the form encoding urlencode defaults to.
        quote_via=quote,
    )
    return f"otpauth://totp/{quote(label, safe='')}?{params}"


def _qr_data_uri(text: str) -> str:
    """The URI as an inline PNG.

    A data: URI rather than an endpoint of its own: the image carries the TOTP
    secret, and this way it never becomes a URL that can be re-fetched, logged
    by a proxy or left in browser history. The CSP allows img-src data: for it.
    """
    buffer = io.BytesIO()
    # Error correction M and no Micro QR, which is what this produced before
    # segno replaced qrcode: an enrolment code is scanned once, by whatever
    # authenticator the operator already uses, and is not the place to find out
    # that one of them does not read Micro QR.
    segno.make(text, error="m", micro=False).save(
        buffer, kind="png", scale=_QR_BOX, border=_QR_BORDER
    )
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
