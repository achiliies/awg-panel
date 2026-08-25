"""Client routes, mounted under <base>api/v1/ by awgui.urls.

No trailing slashes: APPEND_SLASH is off and the frontend's paths are exact.

Order matters, for the same reason each time. "export.zip", "remove-expired"
and "bulk-remove" are all legal client names, so the collection routes have to
come before the <name> patterns or a request for any of them would be read as a
request for a client of that name. The consequence is that a client actually
called one of them is unreachable by name; naming one that is a choice nobody
makes by accident.
"""

from django.urls import path

from .views import (
    ClientBulkLimitView,
    ClientBulkRemoveView,
    ClientConfigView,
    ClientDetailView,
    ClientExportView,
    ClientListView,
    ClientQrView,
    ClientRemoveExpiredView,
    ClientResetKeysView,
    ClientResetUsageView,
    ClientTrafficView,
)

urlpatterns = [
    path("clients", ClientListView.as_view(), name="clients"),
    path("clients/export.zip", ClientExportView.as_view(), name="clients-export"),
    path(
        "clients/remove-expired",
        ClientRemoveExpiredView.as_view(),
        name="clients-remove-expired",
    ),
    path("clients/bulk-remove", ClientBulkRemoveView.as_view(), name="clients-bulk-remove"),
    path("clients/bulk-limit", ClientBulkLimitView.as_view(), name="clients-bulk-limit"),
    path("clients/<str:name>", ClientDetailView.as_view(), name="client"),
    path("clients/<str:name>/config", ClientConfigView.as_view(), name="client-config"),
    path("clients/<str:name>/qr", ClientQrView.as_view(), name="client-qr"),
    path("clients/<str:name>/traffic", ClientTrafficView.as_view(), name="client-traffic"),
    path("clients/<str:name>/reset-keys", ClientResetKeysView.as_view(), name="client-reset-keys"),
    path(
        "clients/<str:name>/reset-usage",
        ClientResetUsageView.as_view(),
        name="client-reset-usage",
    ),
]
