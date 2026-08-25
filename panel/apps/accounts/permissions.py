"""What a token may not do, however valid it is.

A token authenticates as the account, which is what makes it useful: a script
that can add a client can do everything the panel's client page can. The line
this draws is around the credentials themselves - the password, the second
factor, the signed-in browsers, and the tokens - and it is drawn for one reason.

A token is the credential an admin hands out and expects to be able to take
back. If holding one were enough to mint another, revoking the one that leaked
would be theatre: whoever had it could have issued a second, with no expiry,
before anybody noticed. And if it were enough to change the password, the answer
to a leaked token would be losing the account rather than deleting a row.

The line is drawn around what the credentials *are* rather than around the pages
they are edited on, which is why restoring a backup is on the wrong side of it
too: the archive carries db.sqlite3, and that file is where the password hash
and these tokens live.

What this is not is a wall, and that is worth saying here rather than leaving
somebody to discover it. The same db.sqlite3 leaves the server in every backup
*download*, which a token is deliberately allowed to make - and that file holds
the session table, whose keys are the awgsessionid cookie itself rather than a
hash of it, along with the TOTP secret. So a token that has fetched one archive
can put on a signed-in browser's cookie and is then past every rule below.

The rules still earn their place. They keep the credential surface out of the
ordinary reach of a script, so a token that leaks and is noticed cannot have
quietly minted a successor; and they make taking the account cost a backup
download, which is a row in the activity log under the token's own name. What
they are not is a boundary a leaked token can be revoked back across. A token
that has fetched an archive has had the account, and the answer to that is a new
password and an end to every session, not a deleted row. BackupView in
apps.panel.views says the same from the other side; docs/SECURITY.md says it to
the operator.

So the way in stays a person at the login form, for everything short of that one
archive. Everything a token can reach is what the panel *manages*; the one thing
it can reach that decides who may manage it is the copy of the database it is
allowed to carry away.
"""

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from .models import ApiToken

REFUSED = (
    "An API token cannot change the credentials that authorise it. Sign in to the panel to do this."
)


class NotAnApiToken(BasePermission):
    """Allow the request unless a token is what authenticated it.

    Applied to every endpoint under the Authentication tab, reads included. The
    listing is not sensitive in itself - it holds names and four characters of
    each secret - but a rule that is "tokens may not touch the credential
    surface" is one an operator can hold in their head, and one with a carve-out
    for reads is a rule somebody has to look up.
    """

    message = REFUSED

    def has_permission(self, request: Request, view: APIView) -> bool:
        return not isinstance(getattr(request, "auth", None), ApiToken)


# What a view carrying this rule declares. Spelled once, and here rather than in
# any one app's views, because it is not only this app's rule: POST restore
# replaces the database the password hash and these very tokens live in, so it
# belongs on this list as surely as anything under settings/account does.
#
# IsAuthenticated has to be named again: setting permission_classes replaces the
# defaults outright rather than adding to them, and a list holding only the rule
# above would be a route with no authentication at all.
CREDENTIAL_PERMISSIONS = [IsAuthenticated, NotAnApiToken]
