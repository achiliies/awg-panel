"""Write a backup archive to a path, for something that is not a browser.

The panel has always been able to hand a backup to whoever is signed in;
bin/awg-update needs the same archive written to a file instead, because it
takes one immediately before it replaces the code that would otherwise have
been asked for it.

Through the same engine rather than a tar written in the updater, and that is
the whole point of this file existing. apps.panel.backup folds the live traffic
counters in, checkpoints the SQLite WAL back into the database - the archive
deliberately carries no -wal, so anything left in the log would be missing from
the restore - and reads the config tree under the lock the panel writes it with.
An archive taken any other way can be a database caught mid-transaction, or a
-wal beside a database it does not belong to, which is exactly the archive
nobody finds out about until they need it.
"""

import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from awg.errors import AwgError

# Not `from .. import backup`: two levels up is apps.panel.management, and this
# module is itself called backup, so the engine is three up by its own name.
from ...backup import create_archive


class Command(BaseCommand):
    help = "Write a full backup (config tree and panel database) to a file."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--output",
            required=True,
            help="Where to write the .tar.gz. Its directory must already exist.",
        )

    def handle(self, *args: object, **options: object) -> None:
        target = Path(str(options["output"])).expanduser()
        if not target.parent.is_dir():
            raise CommandError(f"{target.parent} is not a directory.")

        try:
            written = create_archive()
        except AwgError as exc:
            raise CommandError(str(exc)) from exc

        # Moved into place rather than written there: create_archive() builds
        # the archive in a temp file, so a run interrupted halfway leaves no
        # half-archive at the path somebody is about to trust. shutil.move
        # rather than os.replace, because the temp directory and /var/lib are
        # often different filesystems and rename does not cross one.
        try:
            shutil.move(str(written), str(target))
        except OSError as exc:
            written.unlink(missing_ok=True)
            raise CommandError(f"the backup could not be moved to {target}: {exc}") from exc
        # The archive holds the server key, every client key and the password
        # hash. create_archive() writes 0600 and the move can carry a different
        # mode across a filesystem boundary, so it is said again here.
        target.chmod(0o600)

        self.stdout.write(str(target))
