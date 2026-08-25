"""The whole URL map, mounted once under the panel's base path.

FORCE_SCRIPT_NAME is deliberately not used: it would make Django believe the
prefix was stripped by a front-end server, which is only true behind a reverse
proxy and never true for the gunicorn unit the installer sets up. Prefixing the
patterns here means the same map works both ways, and `reverse()` returns URLs
the browser can use unchanged.

Each app owns its own segment under api/v1/ - accounts serves auth/, clients
serves clients/, server serves server/, stats serves stats/, events serves
events and logs, and panel serves settings/, backup, restore and update/ - so
the paths in docs/API.md are exactly the paths registered here.
"""

from django.conf import settings
from django.urls import include, path, re_path

from .views import SpaView, health

API = "api/v1/"

# Anything that is not the API and not a static asset is a client-side route:
# the SPA has to come back for /clients or /server on a hard refresh, but a
# missing bundle must 404 as a missing bundle rather than as HTML with a
# JavaScript content type.
SPA_FALLBACK = r"^(?!api/)(?!assets/).*$"

panel_patterns = [
    # First, and outside every include: it is the one route that must answer
    # before authentication, migrations or the frontend build exist.
    path(f"{API}health", health, name="health"),
    # APPEND_SLASH is off, and this is the one URL people type by hand or paste
    # into a container healthcheck. Both spellings answer.
    path(f"{API}health/", health),
    path(f"{API}", include("apps.accounts.urls")),
    path(f"{API}", include("apps.panel.urls")),
    path(f"{API}", include("apps.server.urls")),
    path(f"{API}", include("apps.clients.urls")),
    path(f"{API}", include("apps.stats.urls")),
    path(f"{API}", include("apps.events.urls")),
    re_path(SPA_FALLBACK, SpaView.as_view(), name="spa"),
]

# BASE_PATH is "/" or "/x/y/"; path() prefixes want it without the leading
# slash. Static assets are served by WhiteNoise from STATIC_URL, which is
# derived from the same base path, so they need no pattern of their own.
urlpatterns = [
    path(settings.BASE_PATH.lstrip("/"), include(panel_patterns)),
]

handler404 = "awgui.views.not_found"
handler500 = "awgui.views.server_error"
