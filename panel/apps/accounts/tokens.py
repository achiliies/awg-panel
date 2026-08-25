"""Issuing, checking, listing and ending API tokens.

Everything about a token that is not a column lives here, and three rules run
through the file.

The secret exists once. It is generated in `issue`, returned to the caller that
asked for it, and never reconstructible afterwards - the row holds a hash, so a
token that was not written down is replaced rather than looked up. That is the
whole reason the create endpoint has a response shape of its own and the list
does not.

A token is checked on every request it makes, so nothing on that path may cost
more than a single indexed read. The hash is unsalted for exactly that reason
(see models.digest), the expiry is a comparison against a column rather than a
second query, and the bookkeeping a use leaves behind is rate-limited the same
way the session list's "last active" is - a script polling the dashboard twice a
second must not make this table the busiest writer in the panel.

An expired token is kept, not swept. It is the answer to "why did my deployment
start failing on Tuesday", and a row that vanished at the moment it stopped
working would leave the admin with nothing to read. It stops authenticating the
instant it lapses either way, which is the part that matters; deleting it is
something a person decides to do.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import DatabaseError, IntegrityError
from django.http import HttpRequest
from django.utils import timezone

from awg import names
from awg.errors import NAME_IN_USE, Conflict, NotFound

from .models import ApiToken, digest
from .sessions import client_ip

log = logging.getLogger(__name__)

# What every token this panel issues begins with.
#
# Not decoration: it is what lets a secret leaked into a repository be spotted
# for what it is - by the operator reading a diff, and by the scanners that hunt
# for exactly this shape of thing - and it is what lets an obviously foreign
# bearer token be refused without a database query at all.
PREFIX = "awgp_"

# 32 bytes, urlsafe-base64'd into 43 characters. There is no upgrade path for a
# token's length short of reissuing every one of them, so this is chosen well
# past anything that could be brute-forced rather than at the point where it
# merely is not.
_SECRET_BYTES = 32

# How much of the secret is kept in the clear so a row can be recognised.
_HINT_CHARS = 4

# The shortest and longest lives a token may be given, in seconds.
#
# The floor is not about security - a five-minute token is a perfectly sensible
# thing to want - but about the renewal below being able to keep up: a lifetime
# shorter than the resolution at which uses are written would let a token in
# constant use lapse anyway, which is the one behaviour "renew on use" exists to
# rule out. The ceiling is ten years, which is not a policy so much as a refusal
# to store a date that means nothing.
MIN_LIFETIME_SEC = 300
MAX_LIFETIME_SEC = 3650 * 86400

# How stale `last_used_at` may get before a request writes it again, and by how
# much a renewal must move the expiry to be worth a write. The same number as
# the session list's, for the same reason: this column is read to the minute.
LAST_USED_RESOLUTION_SEC = 60

NAME_TAKEN = (
    "There is already a token called that. Names are how the activity log tells them apart."
)

TOKEN_GONE = "That token has already been revoked."

RENEW_NEEDS_EXPIRY = (
    "A token that never expires has nothing to renew. Give it an expiry, or leave renewal off."
)


# --------------------------------------------------------------------------
# Issuing
# --------------------------------------------------------------------------


def issue(
    user: AbstractBaseUser,
    *,
    name: str,
    lifetime: int | None,
    renew_on_use: bool,
    now: datetime | None = None,
) -> tuple[ApiToken, str]:
    """Create a token and return it with its secret, which is seen only here.

    The secret is generated, hashed, stored and handed back in one call because
    there is nowhere else it could be handed back from: nothing in the panel can
    recover it afterwards, by construction.
    """
    moment = now or timezone.now()
    secret = f"{PREFIX}{secrets.token_urlsafe(_SECRET_BYTES)}"
    expires = moment + timedelta(seconds=lifetime) if lifetime else None

    try:
        token = ApiToken.objects.create(
            user=user,
            name=name,
            token_hash=digest(secret),
            hint=secret[-_HINT_CHARS:],
            created_at=moment,
            expires_at=expires,
            lifetime_sec=lifetime or None,
            renew_on_use=bool(renew_on_use) and bool(lifetime),
        )
    except IntegrityError as exc:
        # The unique constraint on (user, name). Checked in the view as well, so
        # this is the two-requests-at-once case rather than the ordinary one -
        # and it has to answer the same way, not with a 500.
        raise Conflict(NAME_TAKEN, NAME_IN_USE) from exc

    log.info("API token %s issued for %s", name, user.get_username())
    return token, secret


# --------------------------------------------------------------------------
# Presenting
# --------------------------------------------------------------------------


def authenticate(secret: str, now: datetime | None = None) -> ApiToken | None:
    """The token this secret names, or None if it names nothing usable.

    None covers every way of failing on purpose - not one of ours, not in the
    table, expired, belonging to a disabled account - because the caller has no
    business learning which. A token that is refused is refused; telling an
    unauthenticated caller that their string was a real token that lapsed
    yesterday is a fact about this server they have not earned.
    """
    presented = (secret or "").strip()
    # Nothing that could have come from this panel is missing the prefix, so an
    # `Authorization: Bearer` header meant for something else costs no query.
    if not presented.startswith(PREFIX):
        return None

    token = ApiToken.objects.filter(token_hash=digest(presented)).select_related("user").first()
    if token is None:
        return None
    if token.has_expired(now or timezone.now()):
        return None
    if not getattr(token.user, "is_active", True):
        return None
    return token


def used(token: ApiToken, request: HttpRequest, now: datetime | None = None) -> None:
    """Record that this token was just used, and renew it if it is set to renew.

    Never raises. This runs on the authentication path of every API request a
    script makes, and a database that is momentarily locked - or an upgrade
    whose migrations have not run yet - must not turn a working call into a 500
    over a column nobody reads to the second.
    """
    moment = now or timezone.now()
    address = client_ip(request)
    fields: dict[str, object] = {}

    stale = (
        token.last_used_at is None
        or (moment - token.last_used_at).total_seconds() >= LAST_USED_RESOLUTION_SEC
    )
    if stale:
        fields["last_used_at"] = moment
    # The address is worth writing the moment it changes, by the same rule the
    # session list applies: a credential that started being presented from
    # somewhere else is the one thing here an admin might act on.
    if address != token.last_used_ip:
        fields["last_used_at"] = moment
        fields["last_used_ip"] = address

    if token.renew_on_use and token.lifetime_sec:
        renewed = moment + timedelta(seconds=token.lifetime_sec)
        # Only when it moves the expiry by more than the resolution above, so a
        # busy script rewrites this row once a minute rather than once a
        # request. The floor on a lifetime is what makes that safe: the window
        # is always far longer than the gap between two writes, so a token in
        # continuous use cannot lapse between them.
        moved = token.expires_at is None or (renewed - token.expires_at).total_seconds() >= (
            LAST_USED_RESOLUTION_SEC
        )
        if moved:
            fields["expires_at"] = renewed

    if not fields:
        return

    try:
        ApiToken.objects.filter(pk=token.pk).update(**fields)
    except DatabaseError:
        log.warning("could not record the use of API token %s", token.name, exc_info=True)
        return
    # Kept in step so anything later in this request - a listing, an event -
    # describes the row as it now stands rather than as it was read.
    for field, value in fields.items():
        setattr(token, field, value)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def for_user(user: AbstractBaseUser, now: datetime | None = None) -> list[dict]:
    """Every token this account holds, newest first, expired ones included."""
    moment = now or timezone.now()
    return [describe(token, moment) for token in ApiToken.objects.filter(user=user)]


def describe(token: ApiToken, now: datetime | None = None) -> dict:
    """One token as the API shows it. The secret is not in here and cannot be."""
    moment = now or timezone.now()
    return {
        "id": token.pk,
        "name": token.name,
        "hint": token.hint,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        # The window rather than what is left of it: it is what renewal restores
        # on each use, and what the UI says beside the switch.
        "expires_in": token.lifetime_sec,
        "renew_on_use": token.renew_on_use,
        "last_used_at": token.last_used_at,
        "last_used_ip": token.last_used_ip,
        # Sent rather than left to the browser to work out, because "has it run
        # out" is a question about the server's clock and not the laptop's.
        "expired": token.has_expired(moment),
    }


# --------------------------------------------------------------------------
# Changing and ending
# --------------------------------------------------------------------------


def update(
    user: AbstractBaseUser,
    ident: uuid.UUID,
    *,
    name: str | None = None,
    renew_on_use: bool | None = None,
) -> tuple[ApiToken, str, list[str]]:
    """Rename a token, or change whether using it renews it.

    Returns the token, the name it had before - "" when it was not renamed - and
    what actually moved. The caller needs all three: the event log says "X is now
    called Y", which needs the old name, and it should say nothing at all for a
    request that asked for the values the token already had.

    Deliberately the whole of what may be changed. The secret cannot be rotated
    in place - that is a new token and an old one to revoke, and pretending
    otherwise would leave two scripts believing they hold the same credential -
    and the expiry cannot be pushed out by hand, because a token whose end date
    can be moved whenever it approaches is one that does not really have one.
    """
    token = _own(user, ident)
    fields: list[str] = []
    was = ""

    if name is not None and name != token.name:
        _check_name_free(user, name, exclude=token.pk)
        was = token.name
        token.name = name
        fields.append("name")

    if renew_on_use is not None and renew_on_use != token.renew_on_use:
        if renew_on_use and not token.lifetime_sec:
            raise Conflict(RENEW_NEEDS_EXPIRY)
        token.renew_on_use = renew_on_use
        fields.append("renew_on_use")

    if fields:
        try:
            token.save(update_fields=fields)
        except IntegrityError as exc:
            raise Conflict(NAME_TAKEN, NAME_IN_USE) from exc
    return token, was, fields


def revoke(user: AbstractBaseUser, ident: uuid.UUID) -> ApiToken:
    """End a token. The next request presenting it is anonymous.

    Nothing survives it: the row is deleted rather than flagged, because a row
    kept "for the record" is a hash that still matches a secret somebody holds,
    and one forgotten check away from being honoured. What the token did is in
    the event log, which is where that record belongs.
    """
    token = _own(user, ident)
    ApiToken.objects.filter(pk=token.pk).delete()
    log.info("API token %s of %s was revoked", token.name, user.get_username())
    return token


def check_name_free(user: AbstractBaseUser, name: str) -> None:
    """Refuse a name this account already has a token under."""
    _check_name_free(user, name)


def free_name(user: AbstractBaseUser) -> str:
    """A random name this account is not already using, for a token issued without one.

    The same nine characters a client gets, drawn by awg.names, and checked
    against this account's rows by the same case-insensitive rule a typed name
    is checked by - so a generated name can no more shadow "A7kd3mx9p" than a
    typed one could.

    Checked and not merely drawn because the name is what the activity log is
    read by: two tokens sharing one name are two rows that cannot be told apart
    afterwards, whoever typed them. It is still the unique constraint that
    decides - two requests can pass this at the same moment - and `issue` turns
    that into the same 409 a duplicate has always produced.
    """
    taken = {
        value.casefold()
        for value in ApiToken.objects.filter(user=user).values_list("name", flat=True)
    }
    name = names.unique_name(lambda candidate: candidate in taken)
    if name is None:
        raise Conflict(
            "could not find an unused name to give this token. Create it with a name of your own."
        )
    return name


def _check_name_free(user: AbstractBaseUser, name: str, exclude: uuid.UUID | None = None) -> None:
    """The same case-insensitive rule the account name follows.

    The column is case-sensitive and the constraint on it therefore is too, so
    without this a panel could hold "Deploy" and "deploy" - which is two rows
    that read as one in a log, and the log is the reason names exist here.
    """
    rows = ApiToken.objects.filter(user=user, name__iexact=name)
    if exclude is not None:
        rows = rows.exclude(pk=exclude)
    if rows.exists():
        raise Conflict(NAME_TAKEN, NAME_IN_USE)


def _own(user: AbstractBaseUser, ident: uuid.UUID) -> ApiToken:
    """This account's token with that id, or a 404.

    404 rather than 403 for somebody else's, exactly as the session list does:
    the caller has no business learning that the id exists at all.
    """
    token = ApiToken.objects.filter(user=user, pk=ident).first()
    if token is None:
        raise NotFound(TOKEN_GONE)
    return token
