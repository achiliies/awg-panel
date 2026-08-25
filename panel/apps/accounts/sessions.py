"""Recording, listing and ending the browsers that are signed in.

Everything the panel knows about a session that is not in the session itself
lives here: when it started, where from, what it looks like it is, and when it
was last used. Four callers between them keep that true - the login view opens
a row, the middleware keeps it current, the logout view closes it, and the
collector sweeps up what expired without anybody closing anything - and the API
reads it back.

Two rules run through the file.

The session table is the authority on what is still alive. A row here can outlive
its session in perfectly ordinary ways: changing the password deletes every other
session directly, `manage.py clearsessions` and the collector's own sweep reap
expired ones, and a database
restored from a backup can contain either without the other. So nothing is
listed without its session being found first, and what cannot be found is pruned
in passing rather than shown as a device that can still reach the panel.

Ending a session means deleting the session row. Not flagging ours, not waiting
for an expiry: the cookie in the other browser is worth exactly as much as the
row it names, so the row goes and the next request from it is anonymous.
"""

import ipaddress
import logging
import uuid
from datetime import datetime

from django.conf import settings as django_settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.sessions.models import Session
from django.http import HttpRequest
from django.utils import timezone

from awg.errors import NotFound, ValidationError
from awgui.middleware import client_ip_from_forwarded_for

from .models import LoginSession

log = logging.getLogger(__name__)

# How stale `last_seen_at` is allowed to get before a request writes it again.
# The dashboard polls every two seconds, so without a floor this table would be
# the busiest writer in the panel to record a column nobody reads to the second.
LAST_SEEN_RESOLUTION_SEC = 60

# Long user agents are bots and broken proxies; the column is 400 characters.
_USER_AGENT_MAX = 400

REVOKE_SELF = (
    "This is the session you are signed in with. Use Sign out to end it, so the panel knows to "
    "take you back to the login page."
)

SESSION_GONE = "That session has already ended."


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def remember(request: HttpRequest) -> None:
    """Open a row for the session the caller has just signed in to.

    Called after login(), which is when the session key is final: it is cycled
    on the way in, so recording it any earlier would name a session that no
    longer exists.
    """
    key = getattr(request.session, "session_key", None)
    user = getattr(request, "user", None)
    if not key or not (user and user.is_authenticated):
        return

    now = timezone.now()
    LoginSession.objects.update_or_create(
        session_key=key,
        defaults={
            "user": user,
            "created_at": now,
            "last_seen_at": now,
            "ip": client_ip(request),
            "user_agent": user_agent(request),
        },
    )


def touch(request: HttpRequest) -> None:
    """Keep the row for the session in flight current, and open one if it has none.

    A row can be missing for two honest reasons: the panel was upgraded while
    somebody was signed in, and a session was created by something that does not
    go through the login view. Both deserve to appear in the list, so this
    opens one - with `created_at` set to now, which for those is the first
    moment anything knew about the session rather than when it began.
    """
    key = getattr(request.session, "session_key", None)
    user = getattr(request, "user", None)
    if not key or not (user and user.is_authenticated):
        return

    now = timezone.now()
    row = LoginSession.objects.filter(session_key=key).first()
    if row is None:
        LoginSession.objects.create(
            session_key=key,
            user=user,
            created_at=now,
            last_seen_at=now,
            ip=client_ip(request),
            user_agent=user_agent(request),
        )
        return

    address = client_ip(request)
    stale = (now - row.last_seen_at).total_seconds() >= LAST_SEEN_RESOLUTION_SEC
    # The address is worth writing the moment it changes, whatever the clock
    # says: a session that moved from an office to a phone network is the one
    # thing in this row an admin might act on.
    if not stale and address == row.ip:
        return

    LoginSession.objects.filter(pk=row.pk).update(last_seen_at=now, ip=address)


def rekey(old_key: str | None, new_key: str | None) -> None:
    """Follow a session whose key was cycled under it.

    Changing the password rotates the key of the browser that made the change,
    which would otherwise leave this row naming a session that no longer exists
    and the session itself with no row. The list would still show the browser -
    the middleware opens a row for a session it does not recognise - but with
    `created_at` set to the moment of the password change rather than to when
    that browser actually signed in.
    """
    if not old_key or not new_key or old_key == new_key:
        return
    LoginSession.objects.filter(session_key=old_key).update(session_key=new_key)


def forget(session_key: str | None) -> None:
    """Drop the row for a session that is being signed out."""
    if session_key:
        LoginSession.objects.filter(session_key=session_key).delete()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def for_user(user: AbstractBaseUser, current_key: str | None) -> list[dict]:
    """Every live session of this account, the caller's own first.

    The caller's own leads the list because it is the one they need to
    recognise: everything below it is somebody else's browser until proven
    otherwise, and that is the whole point of reading this page.
    """
    live = _live_sessions()
    rows = LoginSession.objects.filter(user=user)

    out: list[dict] = []
    stale: list[uuid.UUID] = []
    for row in rows:
        expires = live.get(row.session_key)
        if expires is None:
            stale.append(row.pk)
            continue
        out.append(_describe(row, expires, current=row.session_key == current_key))

    if stale:
        LoginSession.objects.filter(pk__in=stale).delete()

    # The queryset already arrives newest-used first, and sorting in Python is
    # stable, so lifting the current session to the top is the only sort this
    # list needs.
    out.sort(key=lambda item: item["current"], reverse=True)
    return out


def _describe(row: LoginSession, expires: datetime, *, current: bool) -> dict:
    browser, platform = describe_agent(row.user_agent)
    return {
        "id": row.pk,
        "current": current,
        "created_at": row.created_at,
        "last_seen_at": row.last_seen_at,
        "expires_at": expires,
        "ip": row.ip,
        "browser": browser,
        "platform": platform,
        "user_agent": row.user_agent,
    }


def _live_sessions() -> dict[str, datetime]:
    """Session key -> expiry, for every session that has not run out.

    Expiry is checked here rather than trusted from the row's own age: the panel
    applies its sessionMaxAge setting to each session as it is used, so two
    sessions of the same account can legitimately die at different times.
    """
    rows = Session.objects.filter(expire_date__gt=timezone.now()).values_list(
        "session_key", "expire_date"
    )
    return dict(rows)


# --------------------------------------------------------------------------
# Ending
# --------------------------------------------------------------------------


def revoke(user: AbstractBaseUser, ident: uuid.UUID, current_key: str | None) -> LoginSession:
    """End one of this account's other sessions, and hand back the row that went.

    Refusing to end the caller's own is not paternalism about a destructive
    action - it is that the session cookie in this browser would keep being sent
    to a session that no longer exists, and every page would answer 401 with no
    explanation. Signing out is the operation that does this properly, and it is
    one click away in the top bar.

    The row is returned rather than dropped because it is the only description of
    what was ended that will exist a second later: the caller writes an event
    saying which browser and which address this was, and by then the row is gone.
    It is the in-memory copy of a deleted row, so it is worth reading and not
    worth saving.
    """
    row = LoginSession.objects.filter(user=user, pk=ident).first()
    if row is None:
        raise NotFound(SESSION_GONE)
    if current_key and row.session_key == current_key:
        raise ValidationError(REVOKE_SELF)

    _end([row.session_key])
    LoginSession.objects.filter(pk=row.pk).delete()
    log.info("session %s of %s was signed out from the panel", row.pk, user.get_username())
    return row


def revoke_others(user: AbstractBaseUser, current_key: str | None) -> int:
    """End every session of this account except the caller's own. Returns how many."""
    rows = list(LoginSession.objects.filter(user=user).exclude(session_key=current_key))
    if not rows:
        return 0

    _end([row.session_key for row in rows])
    LoginSession.objects.filter(pk__in=[row.pk for row in rows]).delete()
    log.info("%d other session(s) of %s were signed out", len(rows), user.get_username())
    return len(rows)


def _end(session_keys: list[str]) -> None:
    """Delete the sessions themselves, which is what actually revokes them."""
    Session.objects.filter(session_key__in=session_keys).delete()


def sweep_expired() -> int:
    """Delete the sessions that have run out, and every row left describing one.

    Nothing else ever does. Signing out is the rare way for a session to end -
    an admin closes the tab and comes back next week, and the row they left
    behind expires where it sits - and no read path revisits an expired session,
    so both tables grow by a row per login for the life of the install. Neither
    is large enough to be a performance problem on a panel with one account; a
    table that only ever grows is a thing to have a story about anyway, and this
    is cheaper than the story.

    `for_user` already prunes the second of those, but only for the account
    whose page somebody has opened, which on a panel nobody visits that page of
    is never.

    Swept by the same rule that list applies: a LoginSession whose key names no
    *live* session describes a browser that cannot reach the panel, which is
    exactly what `for_user` refuses to show. Written once so the two cannot come
    to different conclusions about which rows are still worth keeping - and by
    the same reasoning, the sessions go first, so a row orphaned by this call is
    taken in the same pass rather than left for the next one.

    Returns how many LoginSession rows went. The session rows behind them are
    Django's own bookkeeping and are never shown to anybody, so their count is
    not worth reporting.
    """
    now = timezone.now()
    Session.objects.filter(expire_date__lt=now).delete()
    removed, _ = LoginSession.objects.exclude(
        session_key__in=Session.objects.filter(expire_date__gt=now).values("session_key")
    ).delete()
    return removed


# --------------------------------------------------------------------------
# What the request says about the browser
# --------------------------------------------------------------------------


def client_ip(request: HttpRequest) -> str:
    """The address this request came from, or "" when it cannot be trusted or read.

    The same rule as the login lockout, deliberately: X-Forwarded-For is honoured
    only when the operator has declared a proxy, because otherwise it is a header
    the client writes itself, and a session list that reports whatever address an
    attacker typed is worse than one that reports the proxy's.
    """
    if getattr(django_settings, "AXES_CLIENT_IP_CALLABLE", None):
        raw = client_ip_from_forwarded_for(request)
    else:
        raw = request.META.get("REMOTE_ADDR")
    try:
        return str(ipaddress.ip_address((raw or "").strip()))
    except ValueError:
        return ""


def user_agent(request: HttpRequest) -> str:
    return request.META.get("HTTP_USER_AGENT", "")[:_USER_AGENT_MAX]


# Longest-lived first, because nearly every one of these strings claims to be
# several of the others: Edge says Chrome and Safari, Chrome says Safari, and
# every Chromium browser says Mozilla. The first match wins, so the order here
# is the rule.
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("Opera", "Opera"),
    ("Vivaldi", "Vivaldi"),
    ("YaBrowser", "Yandex Browser"),
    ("Brave", "Brave"),
    ("SamsungBrowser", "Samsung Internet"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chromium/", "Chromium"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
    # Not browsers. Somebody scripting against the API is a legitimate session
    # and has to be recognisable in the list rather than shown as "unknown".
    ("curl/", "curl"),
    ("Wget", "Wget"),
    ("HTTPie", "HTTPie"),
    ("python-requests", "python-requests"),
    ("PostmanRuntime", "Postman"),
)

_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("Windows NT", "Windows"),
    # Before "Linux": every Android user agent says both.
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
    ("FreeBSD", "FreeBSD"),
)


def describe_agent(agent: str) -> tuple[str, str]:
    """(browser, platform) from a user agent string; either may be "".

    A guess, and named as one wherever it is shown. The point is not to identify
    the device - nothing in an HTTP header can - but to let an admin tell one
    row from another well enough to know which of them is the laptop they left
    at the office.
    """
    text = agent or ""
    browser = next((name for token, name in _BROWSERS if token in text), "")
    platform = next((name for token, name in _PLATFORMS if token in text), "")
    return browser, platform
