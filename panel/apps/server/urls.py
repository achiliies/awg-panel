"""Server routes, mounted under <base>api/v1/ by awgui.urls.

No trailing slashes: APPEND_SLASH is off and the SPA's paths are exact.
"""

from django.urls import path

from .views import (
    ReconfigureObfuscationView,
    ServerParamsView,
    ServerRestartView,
    ServerStartView,
    ServerStatusView,
    ServerStopView,
    ServerView,
)

urlpatterns = [
    path("server", ServerView.as_view(), name="server"),
    path("server/params", ServerParamsView.as_view(), name="server-params"),
    path("server/reconfigure", ReconfigureObfuscationView.as_view(), name="server-reconfigure"),
    path("server/restart", ServerRestartView.as_view(), name="server-restart"),
    path("server/start", ServerStartView.as_view(), name="server-start"),
    path("server/status", ServerStatusView.as_view(), name="server-status"),
    path("server/stop", ServerStopView.as_view(), name="server-stop"),
]
