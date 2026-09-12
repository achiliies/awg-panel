"""Backup and restore, in the archive layout awg-menu used to write and read.

Two top-level directories, `amneziawg/` and `awg-panel/`. Backups are the
panel's alone now, but the layout is the format rather than a convenience: what
awg-menu wrote was `tar -czf out -C /etc/amnezia amneziawg -C /var/lib
awg-panel`, and it checked for `amneziawg/<iface>.conf` before restoring. Every
archive taken off a server before the move is still one of those, so the member
names are fixed here rather than derived from AWG_CONF_DIR.

The archive contains the server private key, every client private key and every
preshared key. It is written 0600, kept only for the length of the download, and
the caller has to have said so before offering it.
"""

import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import IO

from django.core.management import call_command
from django.db import DatabaseError, connection, connections

from awg import conf as conf_parser
from awg import keys, store, traffic
from awg.controller import get_controller
from awg.errors import AwgError, ToolError, ValidationError
from awg.lock import config_lock
from awg.paths import atomic_copy, client_dir, conf_dir, data_dir, iface, server_conf

from . import defaults, settings_store

log = logging.getLogger(__name__)

# Member names inside the archive. Not the directory names on disk: a test runs
# with AWG_CONF_DIR in a tmpdir and must still produce an archive that restores
# on a real server.
ARCHIVE_CONF = "amneziawg"
ARCHIVE_DATA = "awg-panel"
# Fixed for the same reason: awgui.settings names the file DATA_DIR/db.sqlite3,
# so that is what is in every archive, whatever DATA_DIR happens to be here.
ARCHIVE_DB = "db.sqlite3"

NAME_FMT = "awg-backup-%Y%m%d-%H%M%S"

# The largest archive a restore takes, checked against the upload's
# Content-Length by awgui.middleware.RequestBodyLimitMiddleware before any of it
# is read or spooled. Generous on purpose: refusing a real backup on the day it
# is needed would be far worse than accepting a big one. It is here so that the
# upload has a bound at all.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# The other process that writes both files a restore replaces. It has to be off
# for the whole of one: it rewrites traffic.db every ten seconds, and its SQLite
# connection is pinned to an inode that atomic_copy replaces - so left running
# it would keep reading the pre-restore database and write rows back into it
# that the restored one never sees.
COLLECTOR_UNIT = "awg-panel-collector.service"
SYSTEMCTL_TIMEOUT = 20

# SQLite keeps recent transactions in the -wal file until a checkpoint. Copying
# both while the panel is running can capture a pair that do not belong
# together, and SQLite trusts a -wal beside a database it does not match.
_SKIP_SUFFIXES = ("-wal", "-shm", ".tmp")

# Directories under data_dir() that no archive carries, in either direction.
# Both are bin/awg-update's, and both would make an archive that contained
# itself: the updater downloads the release bundle into update/work, writes its
# pre-update archives into backups/, and takes its backup between those two
# steps. Left in, every pre-update archive would carry the installer it had just
# fetched plus the last three archives whole, and the next one would carry those
# again - so how large a backup is would say how many times the server had been
# updated rather than what is on it.
#
# Skipped on the way back in as well, because an archive written before this
# still has them. Restoring update/ would put back a state file saying an update
# is running, on a server where none is, and the page an operator opens next
# reads that file.
_SKIP_DATA_TREES = ("backups", "update")


def archive_name(when: float | None = None) -> str:
    """The download's filename, in the spelling awg-menu established, in UTC.

    UTC is said outright rather than left to `localtime`. It comes out as UTC
    either way, because Django puts TIME_ZONE into the process's own TZ and
    that is fixed at "UTC" - but that is a fact about a settings module three
    imports away, and the day it changed, every archive an operator had sorted
    by name would silently stop being in the order it was taken in.
    """
    return time.strftime(NAME_FMT, time.gmtime(when)) + ".tar.gz"


def create_archive() -> Path:
    """Write the backup to a fresh 0600 temp file and return its path.

    The caller owns the file and must delete it (or unlink it after opening,
    which is what the download view does).
    """
    _sync_traffic()
    _checkpoint_database()

    handle, name = tempfile.mkstemp(prefix="awg-backup-", suffix=".tar.gz")
    os.close(handle)
    target = Path(name)
    os.chmod(target, 0o600)

    try:
        with tarfile.open(target, "w:gz") as tar:
            # The config lock only guards /etc/amnezia/amneziawg; taking it for
            # that half means the archive cannot catch another worker halfway
            # through adding a client.
            with config_lock():
                tar.add(conf_dir(), arcname=ARCHIVE_CONF, filter=_skip_volatile)
            if data_dir().is_dir():
                tar.add(data_dir(), arcname=ARCHIVE_DATA, filter=_skip_volatile)
    except (OSError, tarfile.TarError) as exc:
        target.unlink(missing_ok=True)
        raise AwgError(f"the backup could not be written: {exc}") from exc
    return target


def restore_archive(upload: IO[bytes]) -> dict:
    """Replace the configuration, and the panel's own data, from an uploaded archive.

    The archive is validated before anything on disk is touched: an admin who
    picked the wrong file gets an error, not a stopped tunnel. After that the
    order is stop the interface, extract, bring it back up, so a client whose
    keys changed cannot stay connected on the old ones.

    The panel's own database is a second, harder half. Replacing the file under
    a running service leaves every worker holding a handle to the inode that was
    replaced, so they go on reading the pre-restore database and writing into a
    file nothing will ever open again. That is why this ends in a restart rather
    than in advice to run one.
    """
    staging = Path(tempfile.mkdtemp(prefix="awg-restore-"))
    try:
        source = _spool(upload, staging / "upload.tar.gz")
        with tarfile.open(source, "r:gz") as tar:
            members = _checked_members(tar)
            restored = sorted({name.split("/")[0] for name, _ in members})
            extract_dir = staging / "extract"
            extract_dir.mkdir(mode=0o700)
            _extract(tar, [member for _, member in members], extract_dir)
        # Still nothing on disk touched. Both of these are faults that survive
        # every check above - the archive's shape is right and its contents are
        # not - and both of them are unrecoverable once the tunnel is down and
        # the file has been written over.
        _check_config(extract_dir / ARCHIVE_CONF / f"{iface()}.conf")
        _check_database(extract_dir / ARCHIVE_DATA / ARCHIVE_DB)

        tunnel = get_controller()
        notes: list[str] = []
        collector_was_running = _collector("is-active")
        if collector_was_running:
            # Before the interface goes down, so the collector's own shutdown
            # flush lands first and the PreDown hook catches the final delta
            # after it.
            if not _collector("stop"):
                notes.append(
                    "The traffic collector could not be stopped, so it may have written over "
                    "part of the restored data. Run 'sudo awg-panel restart' and check the "
                    "client list."
                )
        try:
            tunnel.down()
        except ToolError as exc:
            # Already down, or awg-quick is not installed. Neither stops a
            # restore; the config on disk is what matters.
            log.info("could not stop the interface before restoring: %s", exc)

        with config_lock():
            _install_tree(extract_dir / ARCHIVE_CONF, conf_dir(), notes)
            _drop_orphan_client_confs(notes)
            if ARCHIVE_DATA in restored:
                _install_tree(extract_dir / ARCHIVE_DATA, data_dir(), notes)
                _reset_database_connections()

        if ARCHIVE_DATA in restored:
            # Outside the config lock: both touch the panel's own database only,
            # and a migrate on a large history is not a thing to hold every
            # other writer of the config behind.
            _migrate_database(notes)
            _pin_web_settings(notes)
        settings_store.invalidate()

        try:
            store.restart_iface(tunnel)
        except AwgError as exc:
            notes.append(
                f"The configuration was restored, but the tunnel did not come back up: {exc} "
                f"Check 'systemctl status awg-quick@{iface()}'."
            )

        if collector_was_running:
            _collector("start")

        restarting = False
        if ARCHIVE_DATA in restored:
            restarting = settings_store.request_web_restart()
            note = (
                "The panel database was replaced, so accounts, quotas and history are as of the "
                "backup. "
            )
            if restarting:
                note += (
                    "The panel is restarting to pick it up, and you will have to sign in again "
                    "with the password that was set when the backup was taken."
                )
            else:
                # No wrapper to call, which is a dev checkout or a container. On
                # a real install it is a restore that only half happened, and
                # saying which half is the whole of the difference.
                note += (
                    "Run 'sudo awg-panel restart' to pick it up. Until then the panel is still "
                    "answering out of the database that was just replaced, and anything changed "
                    "here is written into a file that no longer has a name."
                )
            notes.append(note)

        return {
            "restored": restored,
            "clients": _client_count(),
            "iface": iface(),
            "restarting": restarting,
            "detail": " ".join(["Backup restored.", *notes]),
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------- internals


def _sync_traffic() -> None:
    """Fold the kernel's live counters into traffic.db first, as PreDown does.

    Without it a backup taken minutes after the last flush loses that much of
    every client's all-time total, and nobody would ever notice.
    """
    try:
        dump = get_controller().show_dump()
        if dump is not None:
            traffic.sync(dump.transfers())
    except (AwgError, OSError) as exc:
        log.info("could not refresh traffic counters before the backup: %s", exc)


def _checkpoint_database() -> None:
    """Fold the SQLite write-ahead log back into the database file.

    The archive deliberately leaves the -wal and -shm files out, so anything
    still only in the log would be missing from the restored panel.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as exc:
        # Broad on purpose: a database that will not checkpoint is a reason to
        # take a slightly stale backup, never a reason to refuse one.
        log.info("could not checkpoint the database before the backup: %s", exc)


def _skip_volatile(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if _is_skipped_tree(info.name):
        # Returning None for the directory itself is enough: tarfile.add stops
        # there rather than walking into it.
        return None
    name = Path(info.name).name
    if name.endswith(_SKIP_SUFFIXES) or name.startswith("."):
        # Leading dot covers .lock and the atomic-write temp files, which are
        # meaningless outside the machine that made them.
        return None
    return info


def _is_skipped_tree(name: str) -> bool:
    """Is this member inside one of the data directories no archive carries?"""
    parts = Path(name).parts
    return len(parts) > 1 and parts[0] == ARCHIVE_DATA and parts[1] in _SKIP_DATA_TREES


def _spool(upload: IO[bytes], target: Path) -> Path:
    """Copy the upload to a 0600 file we can seek in."""
    chunks = getattr(upload, "chunks", None)
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "wb") as out:
        if callable(chunks):
            for chunk in chunks():
                out.write(chunk)
        else:
            shutil.copyfileobj(upload, out)
    return target


def _checked_members(tar: tarfile.TarFile) -> list[tuple[str, tarfile.TarInfo]]:
    """Validate the archive and return the members worth extracting.

    Anything that is not a plain file or directory under one of the two known
    top-level names is dropped: an archive is an untrusted upload, and a
    symlink or an absolute path in one is how an extract turns into a write
    anywhere on the box.
    """
    try:
        names = tar.getnames()
    except tarfile.TarError as exc:
        raise ValidationError(
            "That file is not a readable .tar.gz archive. Pick a backup file the panel wrote."
        ) from exc

    expected = f"{ARCHIVE_CONF}/{iface()}.conf"
    if expected not in names:
        raise ValidationError(
            f"That archive does not contain {expected}, so it is not a backup of this server "
            f"(wrong file, or a backup of an interface not called '{iface()}'). Nothing has "
            "been changed."
        )

    members: list[tuple[str, tarfile.TarInfo]] = []
    for member in tar.getmembers():
        name = _safe_name(member)
        if name is None:
            log.warning("skipping unsafe archive member %r", member.name)
            continue
        if _is_skipped_tree(name):
            # An archive taken before those trees were left out. Not unsafe, so
            # not warned about - just nothing this server wants back.
            continue
        members.append((name, member))
    return members


def _safe_name(member: tarfile.TarInfo) -> str | None:
    """The member's path if it is safe to extract, else None."""
    if not (member.isfile() or member.isdir()):
        return None
    name = member.name.replace("\\", "/").strip("/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or parts[0] not in (ARCHIVE_CONF, ARCHIVE_DATA):
        return None
    if any(part == ".." for part in parts) or os.path.isabs(member.name):
        return None
    return "/".join(parts)


def _check_config(source: Path) -> None:
    """Refuse an archive whose interface config could not run.

    _checked_members proves that `amneziawg/<iface>.conf` is a name in the
    archive. Nothing proved it was a config. A file that is the right name and
    the wrong contents - a truncated download, half a file from a full disk,
    the wrong archive renamed - passed every check and was found out by
    `awg-quick up`, which by then was being run against the config that had
    already replaced the working one, with the tunnel already down. The restore
    returned success, the note said the tunnel had not come back, and there was
    nothing left to go back to: _install_tree overwrites, so the previous
    server config is the file this one was written over.

    So it is parsed here instead, while the only copy of it is still in the
    staging directory and the interface is still up.

    Deliberately shallow. The three things checked are the three that stop the
    tunnel coming up at all, and everything else an old backup might be missing
    - a peer without a name, an interface without the obfuscation parameters,
    settings this version writes and that one did not - is a backup doing what
    a backup is for. The same reasoning _check_database gives about schema
    versions applies here.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(
            f"The interface configuration in that archive could not be read ({exc}). The file "
            "is probably a truncated or corrupted download - fetch the backup again. Nothing "
            "has been changed."
        ) from exc

    parsed = conf_parser.parse_conf(text)

    # The server's own key. Without a usable one awg-quick brings up an
    # interface that can authenticate nobody, and the panel cannot build a
    # single client config from it.
    if not keys.is_key((parsed.interface.get("PrivateKey") or "").strip()):
        raise ValidationError(
            f"The {source.name} in that archive has no usable PrivateKey, so it is not an "
            "interface configuration this panel can start. The file is probably truncated or "
            "corrupted - fetch the backup again. Nothing has been changed."
        )

    # Every peer's key, because awg-quick refuses the whole file over one bad
    # one: a config that loses a single peer's PublicKey does not come up
    # short a client, it does not come up.
    for index, peer in enumerate(parsed.peers, start=1):
        if not keys.is_key((peer.public_key or "").strip()):
            label = peer.name or f"number {index}"
            raise ValidationError(
                f"Peer {label} in that archive's {source.name} has no usable PublicKey. "
                "awg-quick refuses the whole configuration over one bad key, so restoring this "
                "would leave the tunnel down. Nothing has been changed."
            )


def _check_database(source: Path) -> None:
    """Refuse an archive whose panel database SQLite cannot open.

    Every other check here is about the shape of the archive, and all of them
    pass on a file that is the right size, in the right place, and not a
    database: a truncated download, a text file somebody renamed, an archive
    written while the disk was full. Installing that replaces the only copy of
    the accounts, quotas and history with something nothing can read, and there
    is nothing left to go back to - the previous database is the file it was
    written over.

    Only that it opens and can be queried. Whether its schema is this version's
    is the migration's question, and an older one is a backup doing exactly what
    a backup is for.
    """
    # Absent is a real answer: awg-menu wrote backups before the panel existed
    # and they carry no database at all. Present but not a regular file is not -
    # a directory called db.sqlite3, or a link with nothing behind it, is a file
    # SQLite cannot open, which is the one thing this function exists to refuse.
    # `is_file()` alone answered False to both and let the second through, into
    # an _install_tree that would go on to put a directory where the panel's
    # database belongs.
    if not source.exists() and not source.is_symlink():
        return
    if not source.is_file():
        raise ValidationError(
            "The panel database in that archive is not a file. The archive is malformed - "
            "fetch the backup again. Nothing has been changed."
        )
    try:
        # Read-only, so a file that turns out to be a database is not given a
        # -wal beside it in the staging directory on the way past.
        db = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        try:
            db.execute("PRAGMA schema_version;").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ValidationError(
            f"The panel database in that archive is not a database SQLite can open ({exc}). "
            "The file is probably a truncated or corrupted download - fetch the backup again. "
            "Nothing has been changed."
        ) from exc


def _extract(tar: tarfile.TarFile, members: list[tarfile.TarInfo], target: Path) -> None:
    try:
        # The members are already filtered, but the data filter is a second,
        # better-tested pair of eyes on the same class of trick.
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=target, members=members, filter="data")
        else:
            tar.extractall(path=target, members=members)
    except (tarfile.TarError, OSError) as exc:
        raise ValidationError(
            f"The archive could not be unpacked: {exc} Nothing has been changed."
        ) from exc


def _install_tree(source: Path, target: Path, notes: list[str]) -> None:
    """Copy an extracted tree over its destination, then lock the permissions down.

    Overlay, not replace, which is what `tar -x` over a live directory does and
    therefore what a restore has always done here. A file the backup does not
    mention survives; every file it does mention is overwritten.

    File by file through atomic_copy rather than in one shutil.copytree, because
    the readers of this directory do not take the lock this runs under:
    `awg-quick` reads awg0.conf at boot, and an admin can read it at any time.
    A plain copy truncates the target and fills it, so either of them can catch
    a config with half its peers in it - and awg-quick's half is the one that
    decides who gets on the VPN.

    The server config goes last for the same reason. While the rest of the tree
    lands, a reader sees the old awg0.conf against the new clients.env, which is
    a pairing that at least worked; the switch to the new one is a single
    rename.
    """
    if not source.is_dir():
        return
    try:
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        files: list[tuple[Path, Path]] = []
        for entry in sorted(source.rglob("*")):
            destination = target / entry.relative_to(source)
            if entry.is_dir():
                destination.mkdir(mode=0o700, exist_ok=True)
            elif entry.is_file():
                files.append((entry, destination))
        files.sort(key=lambda pair: pair[1].name == f"{iface()}.conf")
        for entry, destination in files:
            # SQLite's sidecars describe the file they were written beside, so
            # one left over from before the restore would be replayed onto the
            # database that just arrived.
            if destination.suffix in (".sqlite3", ".db"):
                for suffix in ("-wal", "-shm"):
                    Path(f"{destination}{suffix}").unlink(missing_ok=True)
            atomic_copy(entry, destination)
    except OSError as exc:
        raise AwgError(
            f"the backup could not be written to {target}: {exc} The configuration may be "
            "half restored; check it before starting the tunnel."
        ) from exc
    _harden(target, notes)


def _drop_orphan_client_confs(notes: list[str]) -> None:
    """Delete clients/<name>.conf for peers the restored server config does not have.

    _install_tree overlays rather than replaces, which is what makes a
    half-finished restore recoverable, but it means a client added since the
    backup keeps its file after its peer entry is gone. The file is dead - the
    server no longer has the key, so nothing in it can connect - and what is
    left is a private key nobody is tracking, in a directory the name check
    still reads: creating a client under that name would refuse, or worse write
    over the file, which is the one way to destroy a key that exists nowhere
    else. Neither is a state to leave a restore in.

    Named peers only, and only when the restored config parses into some. A
    config that came back unreadable is not evidence that every client on the
    disk is stale, and deleting them on the strength of it would turn a bad
    restore into an unrecoverable one.
    """
    directory = client_dir()
    if not directory.is_dir():
        return
    try:
        parsed = conf_parser.parse_conf(server_conf().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("not pruning client configs: the restored server config did not parse: %s", exc)
        return
    keep = {peer.name for peer in parsed.peers if peer.name}
    if not keep:
        return

    dropped: list[str] = []
    for entry in sorted(directory.glob("*.conf")):
        if entry.is_file() and entry.stem not in keep:
            try:
                entry.unlink()
            except OSError as exc:
                notes.append(f"{entry} is not in the backup and could not be removed ({exc}).")
                continue
            dropped.append(entry.stem)
    if dropped:
        log.warning("removed %d client config(s) not in the backup: %s", len(dropped), dropped)
        notes.append(
            f"{len(dropped)} client config file(s) for peers the backup does not have were "
            f"removed ({', '.join(dropped[:5])}{', ...' if len(dropped) > 5 else ''}); those "
            "devices no longer have a peer on the server and cannot connect."
        )


def _migrate_database(notes: list[str]) -> None:
    """Bring the restored database up to this version's schema.

    A backup is most useful when it is old, and an old one carries the schema of
    the panel that wrote it. Nothing else runs migrate: the installer does it on
    upgrade, and a restore is the one other way a database of a different age
    arrives. Without this the panel comes back up against tables that are
    missing a column and answers 500 to everything, which reads as a restore
    that destroyed the server.
    """
    try:
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:
        # Broad: migrate reaches arbitrary migration code, and a failure here
        # must still leave a restore that reports what happened rather than a
        # 500 over a configuration that is already back in place.
        log.warning("could not migrate the restored database: %s", exc)
        notes.append(
            f"The restored database could not be brought up to this version's schema ({exc}). "
            "Run 'sudo awg-panel migrate' and check the logs; parts of the panel will not work "
            "until it succeeds."
        )


def _pin_web_settings(notes: list[str]) -> None:
    """Keep the panel at the address it is answering on, whatever the backup says.

    The five settings systemd reads - listen address, port, base path and the
    TLS pair - are the one part of a restore that cannot simply be applied. They
    live in two places that a restore only touches one of: the database, which
    arrives from the archive, and /etc/awg-panel.env, which the service was
    started from and which is not in the archive at all. Left alone the two
    disagree, and the disagreement is silent and total - the Settings page reads
    the database and reports HTTPS on, on a service that is serving plain HTTP
    and has no certificate loaded, at a port and a secret path that may be
    another machine's.

    Writing the archive's values into the env file and restarting into them is
    the other way to make them agree, and it is worse. The certificate and key
    are files on disk that no backup contains, so an archive restored onto a
    rebuilt server names two files that are not there and gunicorn refuses to
    start on them; the base path and port move the panel to an address the admin
    is not on and may never have written down. A restore is the moment an admin
    has least appetite for the only UI they have moving somewhere it might not
    come back from.

    So the running values win, and the database is corrected to match them. The
    settings are not lost so much as not applied: the Settings page is one save
    away from turning HTTPS back on, and it says what it will cost first.
    """
    running = _running_web_settings()
    if running is None:
        return  # no env file: a dev checkout or a container, and no address to protect
    try:
        stored = dict(settings_store.all_settings())
    except DatabaseError as exc:
        log.info("cannot read the restored settings: %s", exc)
        return

    differs = {key: value for key, value in running.items() if stored.get(key, "") != value}
    if not differs:
        return
    try:
        settings_store.set_many(running)
    except DatabaseError as exc:
        notes.append(
            f"The restored panel settings could not be reconciled with the running service "
            f"({exc}), so the Settings page may report an address the panel is not serving on."
        )
        return

    was_tls = bool(stored.get("tlsCertPath") and stored.get("tlsKeyPath"))
    now_tls = bool(running.get("tlsCertPath") and running.get("tlsKeyPath"))
    named = sorted({_WEB_SETTING_NAMES.get(key, key) for key in differs})
    line = (
        f"The backup carries a different {_and_list(named)}; the panel has been left at the "
        "address it is answering on rather than moved to one this restore cannot check is "
        "reachable."
    )
    if was_tls and not now_tls:
        line += (
            " That includes HTTPS, which stays off: no backup contains the certificate and key "
            "themselves, so turning it on here could leave the panel unable to start."
        )
    notes.append(line + " Set them again on the Settings page, which restarts the panel for you.")


# What the Settings page calls each of them. The note this feeds is read by
# somebody deciding whether they have lost anything, not by somebody grepping.
_WEB_SETTING_NAMES = {
    "webListen": "listen address",
    "webPort": "port",
    "webBasePath": "secret path",
    "tlsCertPath": "TLS certificate",
    "tlsKeyPath": "TLS certificate",
}


def _and_list(items: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(items) < 2:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _running_web_settings() -> dict[str, str] | None:
    """The five env-backed settings as the running service was started with.

    Read from /etc/awg-panel.env rather than from os.environ: it is the file
    systemd renders the next start from, so it is what "where the panel is"
    means past the restart this restore is about to schedule. A variable the
    file does not assign has no running value to defend and is left to the
    archive. None when there is no file to read at all.
    """
    assigned = settings_store.read_panel_env()
    if assigned is None:
        return None
    return {
        key: assigned[name] for key, name in defaults.ENV_KEYS.items() if name in assigned
    } or None


def _harden(target: Path, notes: list[str]) -> None:
    """0700 directories, 0600 files: the archive's own modes are not to be trusted."""
    try:
        os.chmod(target, 0o700)
        for root, dirs, files in os.walk(target):
            for name in dirs:
                os.chmod(Path(root) / name, 0o700)
            for name in files:
                os.chmod(Path(root) / name, 0o600)
    except OSError as exc:
        notes.append(
            f"Permissions under {target} could not be tightened ({exc}); check them by hand, "
            "the files hold private keys."
        )


def _collector(action: str) -> bool:
    """Run one systemctl verb against the collector. False if it did not work.

    "is-active" doubles as the probe. A dev checkout or a container has no
    systemd and no collector, and there False is the right answer to every
    question here rather than an error.
    """
    try:
        result = subprocess.run(
            ["systemctl", action, COLLECTOR_UNIT],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("systemctl %s %s failed: %s", action, COLLECTOR_UNIT, exc)
        return False
    return result.returncode == 0


def _reset_database_connections() -> None:
    """Drop the handles pointing at the database file we just replaced.

    The file has a new inode; a connection opened before the restore would keep
    reading the old one until the process ended.
    """
    connections.close_all()


def _client_count() -> int:
    try:
        return len(store.list_clients())
    except AwgError:
        return 0
