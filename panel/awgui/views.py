"""The pages Django itself serves: the SPA shell, health, and the error bodies.

Everything else is DRF inside ``apps/``. These exist because they either run
before authentication (health), or have to keep working when the rest of the
panel does not (a missing frontend build, an unhandled exception).
"""

import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template import TemplateSyntaxError, engines
from django.template.backends.django import Template
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .middleware import NONCE_ATTR

log = logging.getLogger(__name__)

# Compiled index.html, keyed by (mtime, size) so a redeploy is picked up without
# a restart while a steady state costs one stat() per request.
_index_cache: dict[str, Any] = {}

_BOOTSTRAP_MARKER = "{{ awg_bootstrap }}"
_BUILD_HINT = "make -C panel build"


def _bootstrap_json(payload: dict[str, Any]) -> str:
    """Serialise the bootstrap object so it cannot break out of its <script>.

    The base path is operator-controlled and reaches this by way of a file the
    panel does not parse, so "</script>" in it must not end the element.
    ensure_ascii also escapes U+2028 and U+2029, which JavaScript treats as line
    terminators; the three HTML-significant characters are escaped here. The
    result is still valid JSON.
    """
    text = json.dumps(payload, ensure_ascii=True)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(raw, escaped)
    return text


def _prepare_source(source: str) -> str:
    """Insert the injection point and, under a base path, a <base> element.

    Vite builds with `base: "./"`, so index.html references its bundles
    relatively. That resolves correctly for /ab12/ and /ab12/clients but not
    for a two-segment client route, where "./assets/x.js" would resolve one
    directory too deep and the panel would come back blank on a hard refresh.
    A <base> element pins it once and costs nothing.
    """
    lowered = source.lower()

    head_open = lowered.find("<head")
    if settings.BASE_PATH != "/" and "<base " not in lowered and head_open != -1:
        head_end = source.find(">", head_open)
        if head_end != -1:
            insert_at = head_end + 1
            source = f'{source[:insert_at]}\n    <base href="{settings.BASE_PATH}" />{source[insert_at:]}'
            lowered = source.lower()

    head_close = lowered.rfind("</head>")
    if head_close == -1:
        return _BOOTSTRAP_MARKER + source
    return f"{source[:head_close]}    {_BOOTSTRAP_MARKER}\n  {source[head_close:]}"


def _load_index(path: Path) -> Template | None:
    """Compile the built index.html, or None if it is not valid template syntax."""
    stat = path.stat()
    key = f"{stat.st_mtime_ns}:{stat.st_size}"
    if not settings.DEBUG and _index_cache.get("key") == key:
        return _index_cache["template"]

    source = _prepare_source(path.read_text(encoding="utf-8"))
    try:
        template = engines["django"].from_string(source)
    except TemplateSyntaxError:
        # The built index.html is ours, but a stray "{%" from some future
        # frontend dependency must not take the whole panel down; the caller
        # substitutes the marker by hand instead.
        log.warning("frontend index.html is not valid Django template syntax; serving it raw")
        template = None

    _index_cache["key"] = key
    _index_cache["template"] = template
    _index_cache["source"] = source
    return template


# Any <script> that is not loading a file, so the built bundle's own inline
# blocks are covered as well as the one injected below.
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\ssrc\s*=)([^>]*)>", re.IGNORECASE)


def _stamp_nonce(html: str, nonce: str) -> str:
    """Give every inline script the nonce the CSP header is about to announce.

    Both of them have to be inline: the bootstrap object is built per request,
    and the theme switch has to run before the first paint or a dark-mode user
    gets a white flash on every load. Under `script-src 'self'` a browser drops
    them without a word, and the SPA then comes up with no base path and 404s
    on every call, which is why this is not optional.
    """
    return _INLINE_SCRIPT_RE.sub(lambda m: f'<script nonce="{nonce}"{m.group(1)}>', html)


class SpaView(View):
    """Serve the built single-page app with its bootstrap object injected."""

    http_method_names = ["get", "head"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        index = settings.FRONTEND_DIST / "index.html"
        if not index.is_file():
            return _missing_build_response()

        payload = {
            "basePath": settings.BASE_PATH,
            "version": settings.PANEL_VERSION,
        }
        # Safe because _bootstrap_json escapes every character that could close
        # the element or start a tag.
        script = mark_safe(f"<script>window.__AWG__={_bootstrap_json(payload)};</script>")

        try:
            template = _load_index(index)
            if template is not None:
                html = template.render({"awg_bootstrap": script}, request)
            else:
                html = _index_cache["source"].replace(_BOOTSTRAP_MARKER, str(script))
        except OSError as exc:
            log.error("cannot read %s: %s", index, exc)
            return _missing_build_response()

        # Fresh per response, so a nonce cannot be replayed from a cached page.
        nonce = secrets.token_urlsafe(16)
        setattr(request, NONCE_ATTR, nonce)
        html = _stamp_nonce(html, nonce)

        response = HttpResponse(html, content_type="text/html; charset=utf-8")
        # The shell carries the base path and the panel name; both change from
        # the settings page, and a cached copy would point the browser at the
        # old address with no way to notice.
        response.headers["Cache-Control"] = "no-store"
        return response


def health(request: HttpRequest) -> JsonResponse:
    """Liveness, without authentication. Used by Docker and by the settings page."""
    response = JsonResponse({"ok": True, "version": settings.PANEL_VERSION})
    response.headers["Cache-Control"] = "no-store"
    return response


@csrf_exempt
def csrf_failure(request: HttpRequest, reason: str = "", template_name: str = "") -> HttpResponse:
    """Answer a rejected CSRF check in the same JSON shape as every other error."""
    log.info("CSRF check failed for %s: %s", request.path_info, reason)
    return JsonResponse(
        {
            "detail": (
                "This request was rejected because its security token was missing or stale. "
                "Reload the page and try again."
            ),
            "errors": {},
        },
        status=403,
    )


def not_found(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """404 inside the base path: JSON for the API, a plain page for anything else."""
    if _is_api(request):
        return JsonResponse(
            {"detail": "There is no such endpoint in this version of the panel.", "errors": {}},
            status=404,
        )
    return _plain_page("Not found", "There is nothing at this address.", status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    """500 handler. Never a traceback, even when DEBUG was left on by accident."""
    if _is_api(request):
        return JsonResponse(
            {
                "detail": (
                    "The panel hit an unexpected error. Your VPN keeps running; "
                    "run 'journalctl -u awg-panel-web -n 50' to see what happened."
                ),
                "errors": {},
            },
            status=500,
        )
    return _plain_page(
        "Something went wrong",
        "The panel hit an unexpected error. Your VPN keeps running. Run "
        "'journalctl -u awg-panel-web -n 50' on the server to see what happened.",
        status=500,
    )


def _is_api(request: HttpRequest) -> bool:
    return request.path_info.startswith(f"{settings.BASE_PATH}api/")


def _missing_build_response() -> HttpResponse:
    return _plain_page(
        "The panel's web interface has not been built",
        f"The API is running, but there is no index.html in {settings.FRONTEND_DIST}, so there "
        "is no page to serve. Build it on the server, or copy in a release bundle.",
        status=503,
        command=_BUILD_HINT,
        note=(
            "A release archive ships a prebuilt frontend/dist; install-panel.sh uses it when "
            "present and only falls back to building from source."
        ),
    )


def _plain_page(
    title: str,
    body: str,
    *,
    status: int,
    command: str = "",
    note: str = "",
) -> HttpResponse:
    """A small self-contained HTML page. No template file, no static files, no JS.

    These are exactly the situations where the frontend build or a template
    loader may be the thing that is broken, so this page depends on neither.
    """
    command_block = f"<pre><code>{escape(command)}</code></pre>" if command else ""
    note_block = f'<p class="note">{escape(note)}</p>' if note else ""
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex, nofollow">
<title>{escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --fg:#18181b; --muted:#52525b; --bg:#fafafa;
         --card:#ffffff; --line:#e4e4e7; --code:#f4f4f5; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fg:#f4f4f5; --muted:#a1a1aa; --bg:#09090b; --card:#18181b;
           --line:#27272a; --code:#27272a; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        padding:1.5rem; background:var(--bg); color:var(--fg);
        font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ width:100%; max-width:34rem; background:var(--card); border:1px solid var(--line);
        border-radius:12px; padding:1.75rem; }}
h1 {{ margin:0 0 .5rem; font-size:1.125rem; font-weight:600; letter-spacing:-.01em; }}
p {{ margin:0 0 1rem; color:var(--muted); }}
p.note {{ margin-bottom:0; font-size:.875rem; }}
pre {{ margin:0 0 1rem; padding:.75rem 1rem; background:var(--code); border-radius:8px;
       overflow-x:auto; }}
code {{ font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color:var(--fg); }}
</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p>{escape(body)}</p>
{command_block}
{note_block}
</main>
</body>
</html>
"""
    response = HttpResponse(html, status=status, content_type="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response
