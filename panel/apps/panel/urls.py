"""Panel-wide routes, mounted under <base>api/v1/ by awgui.urls.

No trailing slashes: APPEND_SLASH is off and the frontend's paths are exact.
The account and two-factor routes under settings/ belong to apps.accounts, which
is included before this module, so nothing here shadows them.
"""

from django.urls import path

from .views import (
    BackupView,
    CertificateView,
    OpenApiView,
    RestoreView,
    SettingsView,
    UpdateApplyView,
    UpdateCheckView,
    UpdateStatusView,
)

urlpatterns = [
    path("settings", SettingsView.as_view(), name="settings"),
    path("settings/certificate", CertificateView.as_view(), name="settings-certificate"),
    path("backup", BackupView.as_view(), name="backup"),
    path("restore", RestoreView.as_view(), name="restore"),
    path("update/check", UpdateCheckView.as_view(), name="update-check"),
    path("update/apply", UpdateApplyView.as_view(), name="update-apply"),
    path("update/status", UpdateStatusView.as_view(), name="update-status"),
    # Named for the file a tool expects to download rather than for a resource,
    # because that is what it is: `openapi.json` is what an importer offers to
    # save it as and what a generator looks for beside a base URL.
    path("openapi.json", OpenApiView.as_view(), name="openapi"),
]
