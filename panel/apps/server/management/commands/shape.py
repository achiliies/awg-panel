"""Put every client's bandwidth ceiling back on the interface.

`awg-quick up` builds the tunnel device from scratch, and a tc structure belongs
to a device: every qdisc, class and filter on it goes when it does. So a boot, a
settings change that restarts the tunnel, or an operator running `awg-quick down`
and `up` leaves a server whose database still knows what every client is entitled
to and whose kernel is enforcing none of it. The config carries a PostUp hook
pointing here for exactly that reason, alongside the one that re-revokes disabled
peers.

Unlike that one, this needs the database. `manage enforce` is proudly free of it
because a peer is revoked by a marker in a file it can read for itself; a rate has
no such marker and no other home. That is a real difference at boot - the panel's
own services are not running yet and the SQLite file is on local disk being read
by a hook - so everything here is written to fail quietly and let the collector's
reconcile pass pick it up a minute later. The hook ends in `|| true` for the same
reason.

`--detach` is the other direction, wired into PostDown. The tunnel's own qdiscs
die with the interface and need no help, but the WAN's do not: shaping upload
means an HTB root on the interface this machine sends everything through, and
leaving it there while the tunnel is down is a queue on the path of the panel and
the operator's SSH session, metering traffic for clients that are not connected
to anything.
"""

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from apps.clients import shaping
from awg.errors import AwgError, NotConfigured


class Command(BaseCommand):
    help = "Re-apply every client's bandwidth ceiling to the running interface."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--detach",
            action="store_true",
            help="Remove the shaping structure instead, including the WAN's. For PostDown.",
        )

    def handle(self, *args: object, **options: object) -> None:
        reason = shaping.unavailable()
        if reason:
            # Not a failure. A container built without iproute2 and a developer's
            # laptop under AWG_MOCK both reach here, and neither is a server that
            # was supposed to be shaping anything.
            self.stdout.write(reason)
            return

        try:
            if options.get("detach"):
                self._detach()
                return
            self._apply()
        except NotConfigured:
            # No tunnel on this box yet. The hook lives in a config that has to
            # exist before it can fire, so this is only ever somebody running the
            # command by hand on a fresh install.
            self.stdout.write("no server configuration")
        except DatabaseError as exc:
            # The panel's own database, read from a hook during `awg-quick up`,
            # which at boot is before anything else in the panel has started. The
            # collector applies the same ceilings on its next pass, so this costs
            # a minute rather than the feature.
            raise CommandError(f"cannot read the client limits: {exc}") from exc
        except AwgError as exc:
            raise CommandError(f"cannot apply the bandwidth limits: {exc}") from exc

    def _apply(self) -> None:
        plan, wanted = shaping.wanted()
        if not plan.on:
            # Including the case where the tunnel's subnet is too wide to give
            # every client a class of its own, which is worth saying rather than
            # reporting as "off": nobody switched it off, and the reason is not
            # in the settings the operator would go and look at.
            self.stdout.write(plan.unsupported or "bandwidth limits are off")
            return
        # rebuild, because this is the case the flag is for: the structure has
        # almost certainly just been lost with the interface, and a class read
        # back at the right rate with its filter missing would look like a
        # ceiling that is already in force while classifying nothing.
        moved = shaping.reconcile(plan, wanted, rebuild=True)
        if not wanted:
            self.stdout.write("no client has a bandwidth limit")
            return
        self.stdout.write(f"applied {len(wanted)} bandwidth limit(s) ({moved} change(s))")

    def _detach(self) -> None:
        shaping.detach()
        self.stdout.write("shaping removed")
