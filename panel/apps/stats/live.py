"""The live blob: one file, written by the collector, served by the API.

The dashboard polls every two seconds. If that poll ran `awg show dump` the
panel would fork a process per browser tab per two seconds, and a page left open
overnight would be a load generator. So the collector - which is already polling
the interface for traffic accounting - writes what it saw into one small JSON
file, and the web side does nothing but hand that file back.

Two rules follow from that split, and both are the point of this module:

* The writer must never let a reader see half a file. ``awg.paths.atomic_write``
  writes a sibling temp file and renames it, so a reader gets either the whole
  previous blob or the whole new one.
* The reader must never fail. The collector may not be running yet, may have
  been stopped, or may never have been installed (a web-only container), and
  none of those is an error worth a 500 on the dashboard. A missing, empty,
  truncated or non-JSON file all read back as the same well-formed "no data
  yet" blob, whose ``ts`` of 0 is what tells the UI to say so.

Keys here are camelCase, unlike everywhere else in the Python: this dict is the
API response, served verbatim without a serializer, because half of it is keyed
by peer public key and a camelCasing pass would rewrite those keys as if they
were field names.
"""

import json
import logging
from pathlib import Path
from typing import Any

from awg.paths import atomic_write, live_state_file, server_conf

log = logging.getLogger(__name__)

# 0640 rather than 0600: the collector writes it as root, and the group bit is
# what would let a web process running as a service account read it without ever
# being able to rewrite it. Nothing in here is secret - no key material reaches
# the blob - but peer public keys and endpoints are not for every local user.
FILE_MODE = 0o640


def empty_system() -> dict[str, Any]:
    """The system block of a blob with nothing in it yet."""
    return {
        "cpu": 0.0,
        "memUsed": 0,
        "memTotal": 0,
        # Bytes held by the tunnel and by the panel itself. Unlike the two
        # above, which are mebibytes: see awg.sysinfo for why.
        "memCore": 0,
        "memPanel": 0,
        # Swap in bytes, and zero for a total on a machine that has none - which
        # the UI reads as "no swap here" rather than as an empty swap device.
        "swapUsed": 0,
        "swapTotal": 0,
        "uptime": 0,
        "load": [0.0, 0.0, 0.0],
        # Bytes per second the disks were reading and writing over the poll that
        # wrote this, and the share of it the busiest one spent working. Rates,
        # unlike the two WAN figures below, which are counters since boot.
        "diskRead": 0,
        "diskWrite": 0,
        "diskBusy": 0.0,
        # How full the root filesystem is, in bytes. Used and free are both as
        # the kernel gives them, so they do not add up to total: a filesystem
        # keeps a reserve only root may write into.
        "diskUsed": 0,
        "diskFree": 0,
        "diskTotal": 0,
        "wanRx": 0,
        "wanTx": 0,
    }


def empty_blob() -> dict[str, Any]:
    """A complete blob that says "the collector has not reported anything".

    ``ts`` is 0 rather than the current time on purpose: the UI decides whether
    live data is trustworthy by how old the blob is, and stamping "now" on an
    empty one would claim the interface is up and idle.
    """
    return {
        "ts": 0,
        "ifaceUp": False,
        # Unix seconds since which the tunnel has been up, and the index of the
        # netdev that claim is about. Both 0 while it is down. The kernel keeps
        # no creation time for an interface, so the collector observes this and
        # carries it across its own restarts through this file; the index is
        # what tells a tunnel that stayed up from one that was restarted in the
        # meantime. See the collector for what the figure is worth.
        "ifaceSince": 0,
        "ifaceIndex": 0,
        "online": 0,
        "total": 0,
        "totalRateRx": 0,
        "totalRateTx": 0,
        "peers": {},
        "system": empty_system(),
    }


def normalise(blob: Any) -> dict[str, Any]:
    """Fill in whatever a blob is missing, so the API answer is always one shape.

    A blob written by an older collector, or one truncated by a full disk, is
    still worth what it does contain. The frontend has no optional branches for
    these fields; supplying them here is cheaper than a null check per field
    there.
    """
    if not isinstance(blob, dict):
        return empty_blob()

    out = empty_blob()
    for key, fallback in out.items():
        value = blob.get(key, fallback)
        # A wrong type is as bad as a missing key: "peers": null would make the
        # dashboard iterate over nothing and report every client as offline.
        if isinstance(value, type(fallback)) or (
            isinstance(fallback, (int, float)) and isinstance(value, (int, float))
        ):
            out[key] = value

    system = blob.get("system")
    if isinstance(system, dict):
        out["system"] = {**empty_system(), **system}
    peers = out["peers"]
    out["peers"] = {
        key: value
        for key, value in peers.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    return out


def read_live(path: Path | str | None = None) -> dict[str, Any]:
    """The collector's last report, or the empty blob. Never raises.

    Not locked: the file is replaced atomically, so a read sees one version or
    the other. A blob that fails to parse is logged at debug and treated as
    absent - something other than the collector wrote there, and failing a
    dashboard poll over it would hide the far more useful "no live data" state.
    """
    target = Path(path) if path is not None else live_state_file()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_blob()
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("cannot read live state at %s: %s", target, exc)
        return empty_blob()

    try:
        blob = json.loads(raw)
    except ValueError as exc:
        log.debug("live state at %s is not valid JSON: %s", target, exc)
        return empty_blob()
    return normalise(blob)


def conf_stamp() -> str:
    """A short token that changes whenever the server configuration is rewritten.

    The one thing the live endpoint reports that the collector did not observe.
    Everything else here is a reading taken two seconds ago; this is a fact about
    the file as it stands when the request is answered, and it is answered on
    that request because it is one stat() and the caller is already asking.

    It exists because the panel reads the tunnel on two clocks. Usage and rates
    come from this blob every two seconds; whether a client is switched on, and
    the status word beside it, come from the client list every thirty. A client
    that crosses its data limit is taken off the interface within one poll, and
    for the rest of that half minute the list goes on describing it as enabled
    and idle - a bar sitting full next to a row insisting nothing has happened.
    Comparing this token across two polls is how a reader learns to go and ask
    again, rather than waiting out a clock that knows nothing about the event.

    Only the server config, deliberately, and not the three files
    ClientIndex.Stamp watches: traffic.db is rewritten every ten seconds by the
    collector, so including it would mean a refetch on that timer for a file
    whose contents the blob is already carrying. What is left changes when the
    peer list does - a client added, removed, switched off by hand, edited over
    SSH, or switched off by enforcement - which is exactly the set of
    events that make the client list wrong.

    mtime alone would not do. A quota disable rewrites the file through
    ``atomic_write``, which renames a fresh inode over the old one, and two
    writes inside one filesystem timestamp tick are ordinary on a fast box. The
    size and the inode are what make those distinguishable, and they are the
    same three fields the client index has always stamped the config with.
    """
    try:
        info = server_conf().stat()
    except OSError:
        # No configuration yet, or one this process cannot see. Empty means "no
        # opinion" rather than a stamp of its own: a reader is told nothing
        # changed, which is true, instead of being sent to refetch a list that
        # is about a server that does not exist.
        return ""
    return f"{info.st_mtime_ns:x}-{info.st_size:x}-{info.st_ino:x}"


def write_live(blob: dict[str, Any], path: Path | str | None = None) -> None:
    """Replace the live file with `blob`, atomically and 0640.

    Raises OSError if the data directory is not writable; the collector treats
    that as one more bad cycle rather than a reason to stop polling.
    """
    target = Path(path) if path is not None else live_state_file()
    # separators: this is written every couple of seconds forever, and the
    # default ", " spacing is a few hundred wasted bytes per write on a box
    # whose disk may well be an SD card.
    atomic_write(target, json.dumps(blob, separators=(",", ":")), mode=FILE_MODE)
