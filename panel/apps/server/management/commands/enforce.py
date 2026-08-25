"""Re-revoke every disabled peer on the running interface.

`awg-quick up` loads the whole server config into the kernel, and a disabled
peer is still in that file on purpose - the entry is what reserves its address
and keeps its config file. So every bring-up re-admits clients that a quota, an
expiry or an admin took away, and it does so at boot, before anything else in
the panel is running.

This is what closes that window, and the config carries a PostUp hook pointing
at it for exactly that reason. It reads the config, and if any peer carries a
"# Disabled" marker it pushes the stripped configuration - which drops those
peers - onto the interface. Nothing else about the tunnel is touched.

It used to be `awg-client enforce`. It moved here when the CLI stopped being a
writer of the configuration: the check is the same one, over the same file, but
the panel is now the only thing that decides who is switched off, so the tool
that re-asserts it has to be on this side of the line.

Deliberately free of the database. It runs from a hook during `awg-quick up`,
which happens at boot before the panel's own services are started and on a box
where the panel may be mid-upgrade or broken outright - and none of that changes
what the config says about who is revoked. A peer is disabled because there is a
marker beside it in the file, and that is all this needs to read.
"""

from django.core.management.base import BaseCommand, CommandError

from awg import store
from awg.errors import AwgError, NotConfigured


class Command(BaseCommand):
    help = "Push the disabled peers' revocation back onto the running interface."

    def handle(self, *args: object, **options: object) -> None:
        try:
            disabled = [view for view in store.list_clients() if not view.enabled]
        except NotConfigured:
            # No tunnel on this box yet. Not a failure: the hook is wired into a
            # config that has to exist before it can fire, so this only happens
            # to somebody running the command by hand on a fresh install.
            self.stdout.write("no server configuration")
            return
        except AwgError as exc:
            raise CommandError(f"cannot read the server configuration: {exc}") from exc

        if not disabled:
            # The overwhelmingly common case, and worth returning early for: a
            # syncconf on a server with thousands of peers is not free, and at
            # boot it would be paid on every single bring-up for nothing.
            self.stdout.write("nothing disabled")
            return

        try:
            applied = store.apply_live()
        except AwgError as exc:
            raise CommandError(f"cannot apply the configuration: {exc}") from exc

        if not applied:
            # The interface is down, which is not an error worth failing a hook
            # over: whatever brings it up next loads the config through this same
            # path, and the marker is still in the file.
            self.stdout.write(f"interface down; {len(disabled)} peer(s) left revoked on disk")
            return
        self.stdout.write(f"revoked {len(disabled)} disabled peer(s)")
