"""The second way a request can prove who it is: `Authorization: Bearer <token>`.

The panel's own SPA never uses this. It exists for everything else - a
deployment script, a monitoring check, a cron job that adds a client for a new
laptop - and its whole job is to let those things authenticate without being
handed the password an admin signs in with.

Two consequences of being an authenticator rather than a session are worth
stating plainly, because both are deliberate.

There is no CSRF check on a token request, and there must not be. CSRF exists
because a browser attaches cookies to a request it was tricked into making; a
browser attaches no `Authorization` header it was not asked to, and cannot be
made to send one cross-origin without a preflight this panel answers no CORS
headers to. The check that matters here is that the header carries a secret,
which is the whole of the authentication.

And a refused token is refused silently. Nothing is written to the event log for
one, unlike a refused sign-in: this path can be driven as often as an outsider
likes, and the sign-in path is only allowed its rows because django-axes bounds
them. An audit trail an anonymous caller can grow at will is not an audit trail.
What *is* recorded is the successful use, in the token's own row.
"""

from django.http import HttpRequest
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from . import tokens
from .models import ApiToken

# One sentence for every way of failing, on purpose: which of them it was is not
# something an unauthenticated caller is owed. It still says where a working one
# comes from, because the overwhelmingly likely reader is the admin who has just
# pasted the wrong thing into their own script.
BAD_TOKEN = (
    "That API token is not valid, has expired, or has been revoked. Issue a new one on the "
    "panel's API page."
)

MALFORMED = 'Send the token as a single "Authorization: Bearer <token>" header.'


class ApiTokenAuthentication(BaseAuthentication):
    """Authenticate a request from its bearer token, or leave it to the session.

    Returning None rather than raising for a request with no bearer header is
    what lets this sit in front of SessionAuthentication: the browser's cookie is
    still the panel's own way in, and a request that carries neither is simply
    anonymous.
    """

    keyword = "Bearer"

    def authenticate(self, request: Request) -> tuple[object, ApiToken] | None:
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise AuthenticationFailed(MALFORMED)

        try:
            presented = header[1].decode("utf-8")
        except UnicodeDecodeError:
            # A token this panel issued is base64url, so this is not one of ours.
            raise AuthenticationFailed(BAD_TOKEN) from None

        token = tokens.authenticate(presented)
        if token is None:
            raise AuthenticationFailed(BAD_TOKEN)

        # Before the view runs, so a token that renews on use is renewed by the
        # request it is being used for rather than by the next one.
        tokens.used(token, request)
        return token.user, token

    def authenticate_header(self, request: HttpRequest) -> str:
        """The WWW-Authenticate challenge, which is what makes a refusal a 401.

        DRF downgrades 401 to 403 when no authenticator offers a challenge, and
        SessionAuthentication never does - so without this a script would get
        the status the SPA uses to mean "you are signed in and may not do this"
        for a token that was simply wrong.
        """
        return self.keyword
