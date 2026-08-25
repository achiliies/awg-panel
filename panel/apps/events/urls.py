"""Event and journal routes, mounted under <base>api/v1/ by awgui.urls.

No trailing slashes: APPEND_SLASH is off and the frontend's paths are exact.
"""

from django.urls import path

from .views import EventListView, JournalView

urlpatterns = [
    path("events", EventListView.as_view(), name="events"),
    # "logs" rather than "events/journal": the two are not one resource narrowed
    # two ways. One is a table this panel writes, the other is a pipe to
    # journalctl, and nesting them would promise a shape they do not share.
    path("logs", JournalView.as_view(), name="logs"),
]
