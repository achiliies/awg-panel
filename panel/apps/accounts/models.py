"""The two ways in, as rows: a browser's session, and a token that is not one.

django.contrib.sessions stores three things per signed-in browser: an opaque
key, an encoded blob and an expiry. Whose session it is can only be recovered by
decoding the blob, and when it started, where it was signed in from and when it
was last used are not recorded at all. That is enough to authenticate a request
and not enough for the question an admin actually asks - "what else is signed in
to my panel, and can I end it from here?"

So one row is kept alongside each session. The session stays the source of
truth: a row whose session has expired or been deleted describes nothing, and
apps.accounts.sessions prunes it rather than showing it.

The primary key is a random UUID rather than the session key, because this is
the identifier the API hands to a browser. A session key in a listing is the
cookie value of somebody else's browser, and anything that can read that page -
an extension, a proxy log, a screenshot - would be one copy away from being
signed in as them. The key is stored, because ending a session means deleting
the row django.contrib.sessions keeps under exactly that name, but it never
leaves the server.

An API token is the same shape of thing for a caller that has no browser to keep
a cookie in, and the same rule decides what is stored: the secret itself is
never kept, only a hash of it, so a database that leaks - in a backup, in a
support archive - leaks nothing that can be presented at the door.
"""

import hashlib
import uuid
from datetime import datetime

from django.conf import settings
from django.db import models


class LoginSession(models.Model):
    """One browser signed in to the panel."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_sessions"
    )
    # django.contrib.sessions' own primary key, which is how a session is ended.
    # Unique because two rows for one session would revoke twice and list twice.
    session_key = models.CharField(max_length=40, unique=True)
    # When the password (and the code, when there is one) was accepted. Set from
    # the clock rather than with auto_now_add, because a row is also opened for
    # a session that was already running when the panel was upgraded to a
    # version that keeps these - and for that one this is a floor, not a fact.
    created_at = models.DateTimeField()
    # Moved on by the middleware at most once a minute; see sessions.touch().
    last_seen_at = models.DateTimeField()
    # Text rather than GenericIPAddressField: this is written on every request,
    # and behind an unusual proxy - or on a unix socket - the address can be
    # something that is not an address at all. An empty string says "not known",
    # which is a far better outcome than a 500 on every page.
    ip = models.CharField(max_length=45, blank=True, default="")
    # Truncated on write. It is shown to a human and parsed for a device name,
    # and neither needs more than this; anything longer is a bot's signature.
    user_agent = models.CharField(max_length=400, blank=True, default="")

    class Meta:
        # Most recently used first, which is the order the list is shown in
        # once the current session has been lifted to the top.
        ordering = ["-last_seen_at"]
        verbose_name = "login session"
        verbose_name_plural = "login sessions"

    def __str__(self) -> str:
        return f"{self.user_id}@{self.ip or 'unknown'}"


# What every actor written by a token-authenticated request begins with.
#
# The event log has one column for who did something, and an account name is
# what it usually holds. A token has to be distinguishable from an account in
# that column at a glance and beyond argument, so its name is written with this
# in front - and a colon is the one character apps.accounts.serializers refuses
# in a username, which is what makes "api:deploy" a thing no account can ever be
# called.
ACTOR_PREFIX = "api:"


class ApiToken(models.Model):
    """One credential for a caller that is not a browser.

    Everything an admin can do to this panel, a script can do too, and until now
    the only way to let it was to hand the script the password - which is the
    account's whole identity, cannot be handed to two scripts separately, and
    leaves a session in the list that says nothing about what created it. A token
    is the same access with the three properties a password does not have: it is
    named, so the activity log says which script did what; it can be given an
    expiry, so a forgotten one stops working on its own; and it can be revoked by
    itself, without changing what a person signs in with.

    The secret is not here. What is stored is a SHA-256 of it, and the reason
    that hash is not Argon2 - which is what guards the password next door - is
    that the two credentials are attacked differently. A password is something a
    person chose and a slow hash is what buys time against guessing it; a token
    is 256 bits from the system's random source, so there is no guessing to slow
    down, and the hash is instead on the path of every single API request, where
    Argon2 would be a self-inflicted denial of service.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    # What the admin calls it, and what the activity log shows. Unique per
    # account because the whole point is being able to tell one row from another
    # a year later - two tokens both called "backup script" would make the log
    # less readable rather than more.
    name = models.CharField(max_length=64)
    # SHA-256 of the whole presented string, prefix included, in lowercase hex.
    # Unique because a collision would mean one secret opening two rows, and
    # indexed because this is the lookup every token request makes.
    token_hash = models.CharField(max_length=64, unique=True)
    # The last few characters of the secret, so a row can be matched against the
    # copy in somebody's CI configuration. Safe to show: the secret has 256 bits
    # behind it, and four characters of a hash-checked value narrow nothing.
    hint = models.CharField(max_length=8, blank=True, default="")
    # From the clock rather than auto_now_add, for the same reason LoginSession
    # does it: the writing code owns the moment.
    created_at = models.DateTimeField()
    # Null means "does not expire", which is a deliberate choice an admin has to
    # make rather than the default the form offers.
    expires_at = models.DateTimeField(null=True, blank=True)
    # How long the token was given, kept beside the expiry it produced because
    # renewal needs the window and not just its end. Null exactly when
    # expires_at is.
    lifetime_sec = models.PositiveIntegerField(null=True, blank=True)
    # Off unless asked for. On, every use pushes expires_at out by lifetime_sec,
    # so a token in daily use never runs out and one whose script was
    # decommissioned dies on schedule - which is the opposite of the usual
    # trade, where the token that is still needed is the one that lapses.
    renew_on_use = models.BooleanField(default=False)
    # Null until the token is first presented. This is what tells an admin
    # whether a row is a live credential or a forgotten one.
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Text and blank-for-unknown, by the same reasoning as LoginSession.ip.
    last_used_ip = models.CharField(max_length=45, blank=True, default="")

    class Meta:
        # Newest first: the list is read after issuing one, and the row just
        # created is the one being looked for.
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_token_name_per_user")
        ]
        verbose_name = "API token"
        verbose_name_plural = "API tokens"

    def __str__(self) -> str:
        return self.name

    @property
    def actor_name(self) -> str:
        """What the event log writes in the actor column for this token."""
        return f"{ACTOR_PREFIX}{self.name}"

    def has_expired(self, now: datetime) -> bool:
        """Whether this token is past its expiry. A token without one never is."""
        return self.expires_at is not None and self.expires_at <= now


def digest(secret: str) -> str:
    """The stored form of a token, which is the only form the panel ever keeps.

    Plain SHA-256 with no salt, and that is correct here rather than a shortcut:
    the input is a value this panel generated from 32 random bytes, so there is
    no dictionary to precompute and nothing a per-row salt would defend against.
    An unsalted hash is also what makes the lookup a single indexed read instead
    of a scan over every row trying each one's salt.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
