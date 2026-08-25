"""Rebuild every client config from the server's current settings.

An upgrade that changes something every client can see - the endpoint, the DNS,
the tunnel's own address family - has to re-issue the config files, or the
devices go on using values the server no longer has. install.sh does that at the
end of such an upgrade, and it used to do it by calling `awg-client resync`.

The work itself is store.resync_all, which is the same code the panel's settings
page runs when a save changes a client-visible value. This is only a way to
reach it from a shell, for the installer and for an admin whose panel is down.

Each client keeps its own keys, address, DNS and split tunnel unless `--dns`
overrides it, and a client whose stored private key does not derive to the
public key in its peer entry is skipped rather than rewritten - that mismatch
means the file was hand-edited or corrupted, and rewriting it would destroy the
only copy of a key that might still be recoverable.
"""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from awg import store
from awg.errors import AwgError, NotConfigured


class Command(BaseCommand):
    help = "Re-issue every client config file from the server's current settings."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dns",
            default=None,
            help="Write this DNS into every client, replacing whatever each one had.",
        )

    def handle(self, *args: object, **options: object) -> None:
        try:
            written = store.resync_all(dns=options["dns"])
        except NotConfigured as exc:
            raise CommandError(f"there is no server configuration to resync: {exc}") from exc
        # OSError too, for the same reason as addclient: a command an admin
        # reaches for when the panel is down should answer in a sentence.
        except (AwgError, OSError) as exc:
            raise CommandError(f"could not re-issue the client configs: {exc}") from exc

        self.stdout.write(f"re-issued {written} client config(s)")
