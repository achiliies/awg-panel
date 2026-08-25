"""Every event the panel can record, and how loud each of them is.

A kind is the whole of what an event *means*. Nothing here stores a sentence,
because a sentence written on the server is a sentence in one language, and the
panel ships two - so the row carries this constant plus a handful of named
values, and the browser turns the pair into a line of English or Russian at the
moment it draws it. That also means the wording of an old event improves when
the translation does, rather than being frozen at whatever the server thought
in the version that wrote it.

The strings are ``<category>.<what-happened>``, and the category half is load
bearing: it is what the filter above the list narrows by, with one chip per
category and no list of kinds anywhere in the UI. So a new kind joins an
existing category unless it is genuinely about something else, and adding one is
a constant here, a line in ``SEVERITY`` if it is not routine, and a string in
both translations.

Deliberately not a database enum or a set of choices on the column. A kind the
running code does not know about is a row from a newer version - after a
downgrade, or a backup restored from one - and the honest thing to do with it is
show it as its own name rather than refuse to read the table it is in.

No Django import, so this can be read by anything: the model uses it for its
severity default, the recorder to look one up, and the views to turn a category
chip into a query. Keeping it free of models is also what lets it be imported
from a package ``__init__`` if it ever needs to be, which the model cannot.
"""

# Signing in, and everything else about the account.
AUTH_SIGNED_IN = "auth.signed-in"
AUTH_SIGN_IN_FAILED = "auth.sign-in-failed"
AUTH_LOCKED_OUT = "auth.locked-out"
AUTH_SIGNED_OUT = "auth.signed-out"
AUTH_PASSWORD_CHANGED = "auth.password-changed"
AUTH_USERNAME_CHANGED = "auth.username-changed"
AUTH_TOTP_ENABLED = "auth.totp-enabled"
AUTH_TOTP_DISABLED = "auth.totp-disabled"
AUTH_SESSION_REVOKED = "auth.session-revoked"
AUTH_SESSIONS_REVOKED = "auth.sessions-revoked"
AUTH_TOKEN_CREATED = "auth.token-created"
AUTH_TOKEN_UPDATED = "auth.token-updated"
AUTH_TOKEN_REVOKED = "auth.token-revoked"

# Clients, as an admin changes them.
CLIENT_CREATED = "client.created"
CLIENT_UPDATED = "client.updated"
CLIENT_RENAMED = "client.renamed"
CLIENT_ENABLED = "client.enabled"
CLIENT_DISABLED = "client.disabled"
CLIENT_DELETED = "client.deleted"
CLIENT_BULK_DELETED = "client.bulk-deleted"
CLIENT_BULK_LIMITED = "client.bulk-limited"
CLIENT_KEYS_RESET = "client.keys-reset"
CLIENT_USAGE_RESET = "client.usage-reset"
# The one download worth a row of its own. A single config is fetched every time
# somebody opens a client's QR code and recording that would drown everything
# else; the archive is one deliberate act that puts every private key on the
# server into a file on somebody's laptop.
CLIENT_EXPORTED = "client.exported"

# Clients, as the collector changes them. The panel's own decisions, made
# without anybody pressing anything, and the only record of why a client that
# was working on Monday is dark on Tuesday.
CLIENT_QUOTA_REACHED = "client.quota-reached"
CLIENT_EXPIRED = "client.expired"
CLIENT_RESTORED = "client.restored"

# The tunnel and the machine it runs on.
SERVER_SAVED = "server.saved"
SERVER_RESTARTED = "server.restarted"
# An admin turning the tunnel off and on again, which is not the pair below.
# Those two are the collector noticing; these two are somebody deciding, and the
# difference is the whole reason to keep four kinds rather than two. A tunnel
# that is down because it was stopped on purpose and one that is down because it
# fell over look identical from the outside, and the question asked afterwards
# is always which of the two it was.
SERVER_STOPPED = "server.stopped"
SERVER_STARTED = "server.started"
SERVER_IFACE_DOWN = "server.iface-down"
SERVER_IFACE_UP = "server.iface-up"

# The panel itself, as opposed to what it manages.
PANEL_SETTINGS_SAVED = "panel.settings-saved"
PANEL_BACKUP_CREATED = "panel.backup-created"
PANEL_RESTORED = "panel.restored"
PANEL_UPDATE_STARTED = "panel.update-started"
# The one event that is about this table. Written immediately after the table is
# emptied, so the ledger is never silently blank: what an operator finds after a
# clear is a log with one line in it, naming who cleared it and how much went.
PANEL_EVENTS_CLEARED = "panel.events-cleared"
# The other thing an admin can empty, and the reason this row is worth as much
# as that one: every traffic figure on the server goes back to zero at once, and
# afterwards there is nothing in the numbers themselves to say that it happened
# rather than that the server has been quiet since it was installed.
PANEL_TRAFFIC_CLEARED = "panel.traffic-cleared"

# The categories, in the order the filter offers them, which is roughly the
# order of how often somebody is looking for one.
CATEGORIES = ("client", "server", "panel", "auth")

INFO = "info"
WARNING = "warning"
SEVERITIES = (INFO, WARNING)

# Everything not named here is INFO, which is most of it.
#
# The line between the two is not "how bad is this" but "would somebody scanning
# for the cause of a problem want it": something destroyed, something
# interrupted, or something that was tried and refused. A password change is a
# significant act and is still routine; a client deleted is routine and is still
# the one thing on this list that cannot be undone.
SEVERITY = {
    AUTH_SIGN_IN_FAILED: WARNING,
    AUTH_LOCKED_OUT: WARNING,
    CLIENT_DELETED: WARNING,
    CLIENT_BULK_DELETED: WARNING,
    CLIENT_QUOTA_REACHED: WARNING,
    CLIENT_EXPIRED: WARNING,
    SERVER_RESTARTED: WARNING,
    SERVER_STOPPED: WARNING,
    SERVER_IFACE_DOWN: WARNING,
    PANEL_RESTORED: WARNING,
    PANEL_UPDATE_STARTED: WARNING,
    PANEL_EVENTS_CLEARED: WARNING,
    PANEL_TRAFFIC_CLEARED: WARNING,
}


def severity_of(kind: str) -> str:
    """How loud this kind is. An unknown one is routine rather than alarming."""
    return SEVERITY.get(kind, INFO)


def category_of(kind: str) -> str:
    """The half of the kind the filter chips narrow by."""
    return kind.split(".", 1)[0]
