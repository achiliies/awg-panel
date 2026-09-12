"""Request-pipeline pieces that are specific to how the panel is deployed.

Six concerns, kept apart because they fire at different points: which peer's
forwarded headers are worth believing at all, the headers every response
carries, the rule that nothing outside the secret base path exists, the refusal
to answer in the clear once HTTPS is on, the largest body a route will be
handed, and the session lifetime the operator chose in the UI.
"""

import importlib
import ipaddress
import logging
import os
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
    JsonResponse,
)

log = logging.getLogger(__name__)

# 'unsafe-inline' for styles only: Tailwind and Radix both set inline style
# attributes for positioning, and a static build carries no style nonce.
# Scripts get no such licence. The two inline scripts the shell needs - the
# per-request bootstrap object and the pre-paint theme switch - are served with
# a fresh nonce instead, which SpaView stamps onto them and this middleware
# announces. Without that they would be dropped silently by every browser and
# the SPA would come up not knowing its own base path.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

# Attribute SpaView sets on the request when it stamped a nonce into the HTML.
NONCE_ATTR = "csp_nonce"


def content_security_policy(nonce: str = "") -> str:
    """The policy, widened to trust exactly the inline scripts we just wrote."""
    if not nonce:
        return CONTENT_SECURITY_POLICY
    return CONTENT_SECURITY_POLICY.replace(
        "script-src 'self'", f"script-src 'self' 'nonce-{nonce}'"
    )


HSTS_VALUE = "max-age=31536000; includeSubDomains"

# Set on the bare 404 that BasePathMiddleware returns; SecurityHeadersMiddleware
# leaves those responses completely empty.
BARE_RESPONSE_ATTR = "awg_bare_response"

# Where the panel app might keep its settings accessors. The exact module is not
# this package's business, and this middleware has to keep working before the
# first migration has created the table it reads.
_SETTING_MODULES = (
    "apps.panel.settings_store",
    "apps.panel.store",
    "apps.panel.defaults",
    "apps.panel.models",
)

_resolved: dict[str, Any] = {}


# The peers whose X-Forwarded-* headers mean anything, read the way gunicorn
# reads the same key: a comma-separated list of addresses, or "*" for any. Also
# accepts a network in CIDR form, which gunicorn does not, because a proxy on a
# container bridge gets a different address on every restart.
#
# Default 127.0.0.1, matching gunicorn's own default for this key. A proxy on
# the same host is the deployment docs/PANEL.md describes, and the panel and the
# server disagreeing about who is trusted would be worse than either answer.
FORWARDED_ALLOW_IPS = "AWG_PANEL_FORWARDED_ALLOW_IPS"

_ANY_PEER = "*"


@lru_cache(maxsize=8)
def _parse_peers(raw: str) -> tuple[str, ...] | None:
    """Split the configured list once per distinct value; None means any peer.

    Cached on the string rather than on the process, so a test that sets the
    variable gets the value it set. The list is a handful of entries read on
    every request, and re-splitting it each time to save a dictionary lookup
    would be the wrong trade in the other direction.
    """
    entries = tuple(item.strip() for item in raw.split(",") if item.strip())
    return None if _ANY_PEER in entries else entries


def _trusted_peers() -> tuple[str, ...] | None:
    return _parse_peers(os.environ.get(FORWARDED_ALLOW_IPS) or "127.0.0.1")


def peer_is_trusted(remote_addr: str | None) -> bool:
    """Whether headers from this peer may be believed.

    The forwarded headers are a claim the peer makes about a request it is
    passing on. From nginx on loopback that claim is worth something; from
    anyone who reached the port some other way it is worth exactly nothing, and
    every one of them can be typed by hand into curl.

    An address that will not parse is not trusted. That is not a real peer -
    gunicorn writes REMOTE_ADDR from the socket it accepted - and a fallback
    that trusted the unparseable would be a fallback in the wrong direction.
    """
    trusted = _trusted_peers()
    if trusted is None:
        return True
    if not remote_addr:
        return False
    try:
        peer = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if peer in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            # A hostname, or a typo. Neither is an address this can compare
            # against, and guessing at what was meant is not this function's
            # job - but a whole panel must not fail over one bad list entry.
            log.warning("ignoring unparseable entry in %s: %r", FORWARDED_ALLOW_IPS, entry)
    return False


class TrustedProxyMiddleware:
    """Drop forwarded headers that did not come from a proxy we were told about.

    AWG_PANEL_TRUST_PROXY switches on three things at once in awgui.settings:
    X-Forwarded-For decides the django-axes lockout bucket, X-Forwarded-Proto
    decides request.is_secure(), and X-Forwarded-Host decides get_host(). All
    three were then believed from whoever sent them, and the panel's default
    listen address is 0.0.0.0 - so on a deployment that turned the flag on and
    left the port reachable, anyone who could open a socket to it wrote all
    three by hand.

    The lockout is the one that matters. AXES_LOCKOUT_PARAMETERS is
    ["ip_address"], so a forged X-Forwarded-For is a fresh bucket per request
    and five attempts per address becomes unlimited attempts against the one
    account this panel has. X-Forwarded-Proto was the way past
    HttpsOnlyMiddleware that made reaching the login form possible at all.

    Stripping them here, before anything reads them, means every consumer
    downstream is correct without knowing this rule exists - Django's own
    USE_X_FORWARDED_HOST and SECURE_PROXY_SSL_HEADER included, since both read
    META and neither can be made conditional on the peer.

    Outermost in the stack for that reason: a middleware that runs after
    something has already read the header it was going to remove protects
    nothing.
    """

    HEADERS = (
        "HTTP_X_FORWARDED_FOR",
        "HTTP_X_FORWARDED_PROTO",
        "HTTP_X_FORWARDED_HOST",
        "HTTP_X_FORWARDED_PORT",
    )

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not peer_is_trusted(request.META.get("REMOTE_ADDR")):
            for header in self.HEADERS:
                request.META.pop(header, None)
        return self.get_response(request)


def client_ip_from_forwarded_for(request: HttpRequest) -> str | None:
    """The address django-axes counts a failed login against, behind one proxy.

    gunicorn sets REMOTE_ADDR from the socket it was handed, so with nginx in
    front every attempt in the world arrives from 127.0.0.1 and the whole
    internet shares a single lockout bucket: one attacker locks the admin out
    of their own server, and a real attacker's own failures are diluted by
    everybody else's. django-axes would normally resolve this through
    django-ipware, but that package is not a dependency here - so it is done
    explicitly, which also means the rule is visible rather than a proxy count.

    nginx's $proxy_add_x_forwarded_for appends the peer it actually saw, so the
    RIGHT-most element is the only one the proxy vouches for. Everything to its
    left was sent by the client and can say anything; taking the left-most, as
    a naive reading of the header does, hands the attacker their own bucket per
    request.

    Only wired up when AWG_PANEL_TRUST_PROXY says there really is a proxy. With
    no proxy the header is pure client input and honouring it would make the
    lockout a formality.
    """
    remote = request.META.get("REMOTE_ADDR")
    # The peer check again, though TrustedProxyMiddleware has already removed
    # the header from anyone who fails it. This function is named in a setting
    # and called by django-axes, not by the stack, so it is reachable in an
    # order this module does not control - and the cost of being wrong here is
    # the login lockout, which is the whole reason the header is read at all.
    if not peer_is_trusted(remote):
        return remote
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    for candidate in reversed(forwarded.split(",")):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            # axes writes this into a GenericIPAddressField, so anything that
            # is not an address would raise mid-login rather than be ignored.
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            # Do not keep walking left: a client that sends "1.2.3.4, junk"
            # would otherwise choose its own bucket.
            return remote
    return remote


def _lookup(key: str, default: Any, names: Iterable[str]) -> Any:
    """Call the first accessor named in `names` that any panel module exposes.

    Every failure - module missing, table missing, wrong arity - falls through
    to `default`. A panel that cannot read its own settings must still serve the
    page that lets you fix them.
    """
    cache_key = ",".join(names)
    cached = _resolved.get(cache_key)
    candidates = [cached] if cached else []
    if not candidates:
        for module_name in _SETTING_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            for attr in names:
                func = getattr(module, attr, None)
                if callable(func):
                    candidates.append(func)

    for func in candidates:
        try:
            value = func(key, default)
        except TypeError:
            try:
                value = func(key)
            except Exception:
                continue
        except Exception:
            continue
        if value is None:
            continue
        _resolved[cache_key] = func
        return value
    return default


def panel_setting(key: str, default: str = "") -> str:
    """A panel setting as text, or `default` if it cannot be read."""
    return str(_lookup(key, default, ("get_setting", "get_str", "get")))


def panel_setting_int(key: str, default: int) -> int:
    """A panel setting as an integer, or `default` if it cannot be read."""
    try:
        return int(_lookup(key, default, ("get_int", "get_setting", "get")))
    except (TypeError, ValueError):
        return default


class SecurityHeadersMiddleware:
    """Attach the panel's security headers to every response it produces."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if getattr(response, BARE_RESPONSE_ATTR, False):
            return response

        headers = response.headers
        headers.setdefault(
            "Content-Security-Policy",
            content_security_policy(getattr(request, NONCE_ATTR, "")),
        )
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        # Only with TLS terminated by us or by a proxy we were told about.
        # Sending HSTS over plain HTTP is ignored by browsers, but sending it
        # from a panel reached by IP on port 2097 would pin the whole host.
        if settings.PANEL_TLS:
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response


class BasePathMiddleware:
    """Everything outside the secret base path returns an empty 404.

    The base path is not authentication - the login form is - but it does keep
    the panel out of the logs of every scanner that walks port 2097. That only
    holds if a wrong path is indistinguishable from a server with nothing on it,
    so the response has no body, no redirect and no header worth fingerprinting.
    Nothing is exempt: health checks and static assets live under the base path
    like everything else.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.base = settings.BASE_PATH
        self.base_no_slash = self.base.rstrip("/")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if path.startswith(self.base):
            return self.get_response(request)

        # The one concession: an operator who typed the base path without its
        # trailing slash already knows the secret, so redirecting them leaks
        # nothing and saves a support question.
        if self.base_no_slash and path == self.base_no_slash:
            return HttpResponsePermanentRedirect(self.base)

        return self._bare_404()

    @staticmethod
    def _bare_404() -> HttpResponse:
        response = HttpResponse(b"", status=404, content_type="text/plain")
        setattr(response, BARE_RESPONSE_ATTR, True)
        return response


class HttpsOnlyMiddleware:
    """Once HTTPS is on, nothing is answered over plain HTTP.

    The switch on the settings page is a promise about the wire, and only half
    of it was being kept. Where gunicorn holds the certificate itself the
    promise held by accident: that socket answers a cleartext request with a TLS
    alert and no HTTP at all. Behind a reverse proxy it did not hold. There
    AWG_PANEL_TRUST_PROXY makes the panel consider itself encrypted - Secure
    cookies, HSTS, an https:// address on every screen - while gunicorn is still
    listening in the clear on AWG_PANEL_LISTEN. Anything reaching that socket
    without passing the proxy, or through a proxy handing on
    X-Forwarded-Proto: http, was served the login form in full, and would have
    read the admin's password out of the POST that came back.

    So the scheme is checked rather than inferred from the deployment.
    `is_secure()` is the whole test and it is right in both shapes: gunicorn
    sets wsgi.url_scheme from its own socket, and SECURE_PROXY_SSL_HEADER - set
    in the same branch that forces PANEL_TLS on - is what makes it read the
    proxy's header when there is a proxy worth believing.

    A reader is sent to the same URL over HTTPS, which is what an operator who
    typed http:// by hand is asking for. Anything that could carry a credential
    or change something is refused instead: a browser drops the body on the way
    through a redirect, so following one would silently lose the request, and a
    secret already put on the wire in the clear is not made safe by answering
    politely.

    That redirect is temporary on purpose. A permanent one is cached by the
    browser well past the day HTTPS is switched off again, and the address the
    panel hands back at that point is precisely the one that would still be
    pinned - HSTS never applies to a bare IP, which is the only reason that
    escape route works at all.

    Standing after BasePathMiddleware is deliberate too. A scanner walking port
    2097 in the clear still gets the same empty 404 as every other panel, and
    only somebody who already knows the secret path is told there is anything
    here to redirect.
    """

    # Everything else either carries something or changes something.
    READ_METHODS = frozenset({"GET", "HEAD"})

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not settings.PANEL_TLS or request.is_secure():
            return self.get_response(request)

        if request.method in self.READ_METHODS:
            return HttpResponseRedirect(f"https://{request.get_host()}{request.get_full_path()}")

        # Worth a line in the journal: on a working deployment this cannot
        # happen, so it is either a proxy that stopped forwarding the scheme -
        # which otherwise presents as "saving anything in the panel is broken" -
        # or somebody talking to the port directly. The path is left out; it is
        # the secret.
        log.warning("refused a %s that arrived over plain HTTP", request.method)
        return HttpResponse(
            b"This panel only answers over HTTPS.\n", status=400, content_type="text/plain"
        )


# The class attribute a view sets to take a larger body than Django's
# DATA_UPLOAD_MAX_MEMORY_SIZE. Only restore does.
MAX_BODY_ATTR = "max_request_body"

_MEBIBYTE = 1024 * 1024


class RequestBodyLimitMiddleware:
    """Refuse a body larger than its route takes, before anything has read it.

    DATA_UPLOAD_MAX_MEMORY_SIZE reads like that limit and is not one. Django
    holds request.body and the plain fields of a form to it - and DRF 3.17.2
    parses JSON and forms from request.body, so those are held to it as well -
    but a file part is never counted. In a request past 2.5 MB it is streamed to
    a temporary file instead, at whatever size the sender chose.

    The login view is where that told. It is open to anyone, and the CSRF check
    it runs reads request.POST before comparing a token, so a multipart POST
    carrying only the cookie GET auth/session hands out was written to the
    service's /tmp in full, and only then answered 403. That /tmp is memory
    wherever it is a tmpfs, which Debian 13 makes it by default.

    Content-Length is the whole test. Nothing has read the body when this runs,
    so a refusal costs neither memory nor disk; gunicorn discards whatever the
    client goes on sending instead of keeping it. A body sent with no length at
    all is read by Django as empty, and there is nothing to bound.

    In process_view rather than __call__, because the limit belongs to the
    route: restore takes an archive, and everything else takes a form's worth of
    JSON. That is still ahead of every read. CsrfViewMiddleware checks in
    process_view too and stands later in the list, and the csrf_protect on the
    login view runs inside the view itself.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponse],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponse | None:
        # DRF's as_view() records the class as `cls`, Django's as `view_class`.
        view_class = getattr(view_func, "cls", None) or getattr(view_func, "view_class", None)
        limit = getattr(view_class, MAX_BODY_ATTR, settings.DATA_UPLOAD_MAX_MEMORY_SIZE)
        if limit is None:
            return None
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            # Django reads a body whose length it cannot parse as empty.
            return None
        if length <= limit:
            return None

        # The path is left out, as it is from HttpsOnlyMiddleware's line: it is
        # the secret.
        log.info("refused a %s body of %d bytes; the route takes %d", request.method, length, limit)
        megabytes = f"{limit / _MEBIBYTE:.1f}".removesuffix(".0")
        return JsonResponse(
            {"detail": f"This route takes a request body of at most {megabytes} MB.", "errors": {}},
            status=413,
        )


class SessionAgeMiddleware:
    """Apply the sessionMaxAge panel setting to the session in flight.

    SESSION_COOKIE_AGE is read once at import, so a value changed in the UI
    would need a service restart to matter. Setting the expiry per request also
    makes it a rolling window: an admin watching the dashboard stays logged in,
    one who walked away is signed out sessionMaxAge seconds after their last
    request.
    """

    DEFAULT_MAX_AGE = 86400

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session = getattr(request, "session", None)
        user = getattr(request, "user", None)
        if session is not None and user is not None and user.is_authenticated:
            max_age = panel_setting_int("sessionMaxAge", self.DEFAULT_MAX_AGE)
            if max_age > 0 and session.get("_session_expiry") != max_age:
                # Comparing first keeps the 2 s dashboard poll from rewriting
                # the session row on every request.
                session.set_expiry(max_age)
        return self.get_response(request)
