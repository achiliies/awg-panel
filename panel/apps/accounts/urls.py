"""Routes under the panel's api/v1/ prefix.

The routes under settings/ live here because they belong to this app, whatever
the UI calls the page they appear on: the account is the pair of credentials the
login handshake above them checks, two-factor enrolment is part of that
handshake, a session list is the other half of the sessions those logins create,
and a token is the credential for a caller that never makes one. None of them is
a panel setting stored in a row.
"""

from django.urls import path

from .views import (
    AccountView,
    ApiTokensView,
    ApiTokenView,
    LoginSessionsRevokeOthersView,
    LoginSessionsView,
    LoginSessionView,
    LoginView,
    LogoutView,
    SessionView,
    TotpDisableView,
    TotpEnableView,
)

urlpatterns = [
    path("auth/login", LoginView.as_view(), name="auth-login"),
    path("auth/logout", LogoutView.as_view(), name="auth-logout"),
    path("auth/session", SessionView.as_view(), name="auth-session"),
    path("settings/account", AccountView.as_view(), name="settings-account"),
    path("settings/2fa/enable", TotpEnableView.as_view(), name="totp-enable"),
    path("settings/2fa/disable", TotpDisableView.as_view(), name="totp-disable"),
    path("settings/sessions", LoginSessionsView.as_view(), name="login-sessions"),
    # Before the pattern below it for readability only: "revoke-others" is not a
    # UUID, so the converter would never have matched it anyway.
    path(
        "settings/sessions/revoke-others",
        LoginSessionsRevokeOthersView.as_view(),
        name="login-sessions-revoke-others",
    ),
    path("settings/sessions/<uuid:ident>", LoginSessionView.as_view(), name="login-session"),
    path("settings/tokens", ApiTokensView.as_view(), name="api-tokens"),
    path("settings/tokens/<uuid:ident>", ApiTokenView.as_view(), name="api-token"),
]
