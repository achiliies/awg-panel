"""Create a client from a shell prompt, for the installer and for recovery.

install.sh creates the first client at the end of a fresh install, so that the
admin has something to scan before they have logged into anything. That step
used to be `awg-client add`; it is this now, because adding a client writes the
server config and the panel is the only writer of it.

Not a general-purpose client manager, and deliberately so - there is no list,
no remove, no rename here. What this exists for is the two moments where the
panel's own API is not reachable: the installer, which runs before the web
service is up, and an admin who has to put a client back on a box whose panel
will not start. Everything else belongs in the panel.

The private key is generated in-process and written only to the client's config
file, exactly as the API does it; nothing about it reaches the command line, the
output or the log.
"""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from awg import paths, store
from awg.errors import AwgError, Conflict, NotConfigured, ValidationError


class Command(BaseCommand):
    help = "Create a client and write its config file. For the installer and for recovery."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "name",
            nargs="?",
            default=None,
            help=(
                "Client name: letters, digits, dot, dash, underscore. Left out, the server "
                "draws a random nine-character one, exactly as the panel does."
            ),
        )
        parser.add_argument(
            "--allowed-ips",
            default=None,
            help="What the client routes through the tunnel. Defaults to the server's setting.",
        )
        parser.add_argument(
            "--dns", default=None, help="DNS for this client. Defaults to the server's setting."
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print only the config file's path, for a script that wants to read it.",
        )

    def handle(self, *args: object, **options: object) -> None:
        name = str(options["name"]) if options["name"] else None
        try:
            view = store.add_client(
                name,
                allowed_ips=options["allowed_ips"],
                dns=options["dns"],
            )
        except NotConfigured as exc:
            raise CommandError(f"there is no server configuration to add: {exc}") from exc
        except (Conflict, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        # OSError as well as AwgError: this runs as root from the installer, but
        # by hand it is one sudo away from a traceback where a sentence belongs -
        # an unwritable /etc/amnezia or a full disk are the ordinary ways in.
        except (AwgError, OSError) as exc:
            raise CommandError(f"could not add the client: {exc}") from exc

        path = paths.client_dir() / f"{view.name}.conf"
        if options["quiet"]:
            self.stdout.write(str(path))
            return
        self.stdout.write(f"added {view.name} ({view.ip})")
        self.stdout.write(f"config written to {path}")
