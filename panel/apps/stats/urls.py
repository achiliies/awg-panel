"""Stats routes, mounted under <base>api/v1/ by awgui.urls.

No trailing slashes: APPEND_SLASH is off and the frontend's paths are exact.
"""

from django.urls import path

from .views import LiveStatsView, StatsSummaryView, TrafficHistoryView, TrafficResetView

urlpatterns = [
    path("stats/live", LiveStatsView.as_view(), name="stats-live"),
    path("stats/summary", StatsSummaryView.as_view(), name="stats-summary"),
    path("stats/traffic", TrafficHistoryView.as_view(), name="stats-traffic"),
    # A path of its own rather than DELETE on the history above, because it is
    # not that resource being deleted: the series is one of four things this
    # clears, and a caller reading the route as "empty the chart" would be
    # surprised by what else went with it.
    path("stats/traffic/reset", TrafficResetView.as_view(), name="stats-traffic-reset"),
]
