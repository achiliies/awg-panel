"""Fold the kernel's counters into traffic.db before the interface goes away.

The kernel counts per peer from the moment an interface comes up and forgets the
lot when it goes down. traffic.db is what makes "how much has this client used"
survive that, and it can only be kept honest if something reads the counters
while they still exist - so the server config carries a PreDown hook pointing
here, and an orderly `awg-quick down` therefore never loses the last stretch.

It used to be `awg-client traffic sync`. It moved here when the CLI stopped
writing anything under /etc/amnezia/amneziawg: the algorithm is unchanged and so
is the file, but there is now exactly one program that maintains it.

The collector does this too, every ten seconds, and the two cannot tread on each
other: both fold through awg.traffic under the config lock. What this catches is
the stretch between the collector's last flush and the interface going down,
which is up to ten seconds of every client's traffic, every restart.

It folds with `ending=True`, which the collector never does: the interface is
about to be destroyed, so the raw counters recorded here are the last that will
ever be read from it. On a kernel that publishes traffic.epoch's two files that
tells awg.traffic nothing it will not work out for itself, and it is ignored.
Where it cannot, this is the only warning the counters are about to restart -
without one, the next bring-up compares fresh counters against these stale ones,
and a peer busy enough to pass its own pre-restart figure before the collector
next looks has the difference quietly taken off its total.

Running this by hand on a live interface is therefore safe on any machine that
publishes an epoch, which is the machines this runs on: the claim is only acted
on where nothing is able to check it.

A failure here must not stop the interface coming down. The hook is written with
that in mind, and so is this: an unreadable interface or an unwritable file is
reported and exits non-zero, but by then `awg-quick` is already committed to the
teardown, and the counters it could not save are ones the next bring-up will
count from zero anyway.
"""

from django.core.management.base import BaseCommand, CommandError

from awg import paths, traffic
from awg.controller import get_controller
from awg.errors import AwgError


class Command(BaseCommand):
    help = "Record the running interface's transfer counters in traffic.db."

    def handle(self, *args: object, **options: object) -> None:
        try:
            dump = get_controller().show_dump()
        except AwgError as exc:
            raise CommandError(f"cannot read {paths.iface()}: {exc}") from exc

        if dump is None:
            # The interface is already gone, or was never up. Nothing has been
            # lost that this could have saved: the counters went with it, and
            # what is in the file is still the last good reading.
            self.stdout.write("interface not up")
            return

        try:
            deltas = traffic.sync(dump.transfers(), ending=True)
        except (AwgError, OSError) as exc:
            raise CommandError(f"cannot update {paths.traffic_db()}: {exc}") from exc

        moved = sum(1 for rx, tx in deltas.values() if rx or tx)
        self.stdout.write(f"recorded {len(deltas)} peer(s), {moved} with new traffic")
