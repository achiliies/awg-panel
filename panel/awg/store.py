"""The only door between the web layer and /etc/amnezia/amneziawg.

This is the only implementation of these operations, and it did not start that
way: there was a shell CLI writing the same files, and every rule below was
written so the two could not corrupt each other. The rules stayed when it went,
because the reasons turned out not to be about a second program at all.

Every mutation re-reads the files inside config_lock() rather than trusting
anything it read before - not because a CLI may have written in between, but
because the panel is several processes and threads, and because the files are
ordinary files that root can edit with a text editor. Every write goes through
paths.atomic_write - not so an unlocked reader in bash sees a whole file, but
because `awg-quick` reads the server config at boot taking no lock at all, and a
half-written one there is a tunnel that does not come up.

Nothing in this module keeps state between calls except the endpoint-detection
cache, which exists only to avoid a three second stall on a box with no metadata
service.

Private keys pass through add_client, resync_all and client_conf_text. They are
never logged, never returned in a view object and never put on a command line.
"""

import contextlib
import ipaddress
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import clientsenv, keys, names, subnet, subnet6, traffic, validate
from .conf import (
    Peer,
    ServerConf,
    clone_conf,
    no_line_break,
    parse_client_conf,
    parse_conf,
    render_conf,
    strip_conf,
)
from .controller import BaseController, get_controller, mock_enabled
from .errors import NAME_IN_USE, AwgError, Conflict, NotConfigured, NotFound, ValidationError
from .lock import config_lock
from .paths import (
    atomic_write,
    client_dir,
    ensure_dir,
    env_file,
    iface,
    server_conf,
    traffic_db,
    write_batch,
)

log = logging.getLogger(__name__)


class Unset:
    """The absence of an argument, for a value whose own absence means something.

    A client's DNS and AllowedIPs can each be left alone, replaced, or cleared
    back to the server default, and the last two are both spelled with a string -
    one of them the empty one. None cannot carry three states, so it carried two
    and the clear was silently read as "leave alone". This type is the third.

    Only worth having where the empty string is itself a value the caller may
    mean. Everywhere else in here None is still the right way to say "nothing
    given", and stays.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "UNSET"


UNSET = Unset()

# `date -u +%Y-%m-%dT%H:%M:%SZ` and `date +%Y%m%d%H%M%S` in the bash tools.
CREATED_FMT = "%Y-%m-%dT%H:%M:%SZ"
BACKUP_FMT = "%Y%m%d%H%M%S"

# The tunnel's own hooks, defined in validate because the checks there have to
# recognise them too - see the note beside them. install.sh writes them into a
# new config and repairs an old one on upgrade; _ensure_hooks below re-asserts
# them on every write from here, because losing either is silent and neither is
# recoverable after the fact.
PREDOWN_HOOK = validate.PREDOWN_HOOK
PREDOWN_MARKER = validate.PREDOWN_MARKER
POSTUP_HOOK = validate.POSTUP_HOOK
POSTUP_MARKER = validate.POSTUP_MARKER
SHAPE_HOOK = validate.SHAPE_HOOK
SHAPE_MARKER = validate.SHAPE_MARKER
UNSHAPE_HOOK = validate.UNSHAPE_HOOK
UNSHAPE_MARKER = validate.UNSHAPE_MARKER

# What the same two hooks looked like when bin/awg-client still owned this
# configuration. A line carrying one of these is rewritten rather than left
# alone: the commands behind them no longer exist, so the config would go on
# calling a tool that cannot answer - losing counters on every restart and
# re-admitting revoked clients at every boot, both without a word.
LEGACY_HOOKS = {
    "awg-client traffic sync": PREDOWN_HOOK,
    "awg-client enforce": POSTUP_HOOK,
}

# The name becomes a file name and a config comment, so nothing exotic is allowed
# through. Kept exactly as strict as it was when a shell script had to quote it:
# loosening it now would issue names that every server installed before this one
# would refuse, and the name is what a client's config file is called.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# Server settings that live in the [Interface] section rather than clients.env.
_CONF_NETWORK = ("ListenPort", "Address", "MTU")

# Changing any of these means every client config has to be regenerated and
# re-imported: they all appear inside the client's own [Interface]/[Peer].
_CLIENT_VISIBLE = (
    "DNS",
    "EndpointHost",
    "EndpointPort",
    "AllowedIPs",
    "PersistentKeepalive",
    "MTU",
)

# snake_case field names the API uses -> the catalog key validate.py knows.
_FIELD_TO_SPEC = {
    "listen_port": "ListenPort",
    "address": "Address",
    "mtu": "MTU",
    "dns": "DNS",
    "endpoint_host": "EndpointHost",
    "endpoint_port": "EndpointPort",
    "allowed_ips_default": "AllowedIPs",
    "keepalive": "PersistentKeepalive",
    "post_up": "PostUp",
    "post_down": "PostDown",
    "pre_down": "PreDown",
}

# Ways of naming the tunnel network on a save. Both are spellings of the
# server's Address, so they are pulled out of the normal field mapping and
# turned into one. "subnet_base" is the pre-CIDR name and still works, since a
# script written against the old API has no reason to have been changed.
_SUBNET_FIELDS = ("subnet_cidr", "subnet_base")

# Catalog key -> the clients.env variable that carries it to clients.
_SPEC_TO_ENV = {
    "DNS": "CLIENT_DNS",
    "EndpointHost": "ENDPOINT_HOST",
    "EndpointPort": "ENDPOINT_PORT",
    "AllowedIPs": "CLIENT_ALLOWED_IPS",
    "PersistentKeepalive": "KEEPALIVE",
    "MTU": "CLIENT_MTU",
}

# EC2 instance metadata, and only these two requests. Everything else - a
# hostname, a route's source address, whatever a cloud's own agent thinks - is a
# guess we are not entitled to make: this value goes into every client config
# issued from here, and a wrong one is a fleet that cannot connect.
_IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
_IMDS_IPV4_URL = "http://169.254.169.254/latest/meta-data/public-ipv4"
_IMDS_TIMEOUT = 3.0
_DETECT_FAIL_TTL = 60.0
# How much of that TTL a warm-up refuses to rely on. Comfortably more than the
# lock's own 10 second budget, so a probe cannot fall back inside it.
_DETECT_WARM_MARGIN = 15.0

# (expires_at, address). A success is kept for the life of the process; a
# failure only briefly, so setting ENDPOINT_HOST takes effect without a restart
# while a non-EC2 box does not pay six seconds of timeouts per client added.
_detected: tuple[float, str | None] | None = None
# gunicorn serves requests on threads, so two of them can miss the cache at the
# same moment and each pay the full probe.
_detect_lock = threading.Lock()

# What bootstrap_if_missing() writes. 203.0.113.0/24 is TEST-NET-3: an endpoint
# nobody can mistake for a real server.
DEMO_ENDPOINT = "203.0.113.10"
DEMO_SUBNET = "10.13.0.0/20"
DEMO_SERVER_IP = "10.13.0.1"
DEMO_PORT = "41234"
DEMO_MTU = "1372"
DEMO_WAN = "eth0"
DEMO_CLIENTS = ("phone", "laptop", "tablet")


@dataclass
class ClientView:
    """One peer, as the panel shows it.

    `ip` is the address the server routes to this client (the peer's AllowedIPs
    in the server config, without its /32). `allowed_ips` is the other
    direction: what the client is told to send through the tunnel, read from its
    own config file, which is what an admin means by "split tunnel".
    """

    name: str | None
    public_key: str
    preshared_key: str | None
    ip: str
    # The IPv6 address the server routes to this client, or "" when it routes
    # none. Read back out of the peer entry rather than derived from `ip`,
    # because what matters to anyone looking at it is what the server actually
    # does - a client whose peer has no v6 route is a client still leaking, and
    # showing it the address it would have had would hide exactly that.
    ip6: str
    allowed_ips: str
    enabled: bool
    disabled_at: str | None
    created: str | None
    has_conf_file: bool


@dataclass
class PeerScan:
    """One peer as the server config describes it, and no further.

    A ClientView minus everything that needs another file opened: no
    `allowed_ips`, which lives in the client's own config, and no
    `has_conf_file`, which is a stat of it. Both are the caller's to fetch for
    the peers it actually cares about - see scan_server.
    """

    name: str | None
    public_key: str
    ip: str
    ip6: str
    enabled: bool
    created: str | None


@dataclass
class ServerScan:
    """One read of the server config: its peers and the few facts about the server itself.

    Those facts come back with the peers rather than from calls of their own
    because each of them is another parse of the same file, and they have to
    describe the same moment as the peer list: a subnet change landing between
    two reads would report clients against a network they were never allocated
    from.

    `subnet6_cidr` and `mtu` are here for the same reason and for one caller -
    the shaping pass, which needs the tunnel's v6 prefix to shape both families
    into one ceiling and its MTU to size the leaf queues. Both were already
    being read to answer `has_ipv6`; handing them back costs nothing and saves
    the collector a second parse of thirty thousand lines once a minute.
    """

    peers: list[PeerScan]
    subnet_cidr: str
    free_ips: int
    has_ipv6: bool
    subnet6_cidr: str = ""
    mtu: int = 0


@dataclass(frozen=True)
class ConfChange:
    """Which peers a config write added or removed, for a reader keeping a copy.

    Handed to the observers registered with on_config_write, and the whole of
    what lets apps.clients.index update a row instead of reading the server
    again. Peers are named by public key because that is what a copy of this
    table is keyed by; a name is a column and can be edited.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    # A peer that stayed but whose entry or client file was rewritten - renamed,
    # disabled, re-addressed. Separate from `added` because the row already
    # exists and its metadata row must not be opened a second time.
    updated: tuple[str, ...] = ()


# Set by the app layer at startup. Lists rather than single callbacks because
# nothing here should care how many readers keep a copy, and empty in every CLI
# and test that imports this module without Django loaded.
_observers: list = []
_starters: list = []
_finishers: list = []


def on_config_write(observer, starter=None, finisher=None) -> None:
    """Register callables to be told when a mutation starts, what it changed, and that it ended.

    `observer(conf, change)` is called once a mutation's files are written, with
    the config as it was written and the ConfChange describing it. Not every
    mutation calls it - see _announce.

    `starter()` is called at the top of those same mutations, after the lock is
    taken and before anything is written, so a reader can see how the files
    looked before this change. It needs that to tell its own change apart from
    somebody else's: a reader holding a copy made two edits ago would otherwise
    apply this delta to it and call the result current, quietly dropping
    whatever happened in between.

    `finisher()` closes that off, and is the reason the three are registered
    together. What a starter records is only about the mutation that is running,
    so it must not outlive it - see _mutating.

    All three run inside config_lock, on the thread doing the work, which is the
    only window in which "the files are what I think they are" holds.
    """
    _observers.append(observer)
    if starter is not None:
        _starters.append(starter)
    if finisher is not None:
        _finishers.append(finisher)


@contextlib.contextmanager
def _mutating():
    """Hold the config lock for one mutation, and bracket it for the observers.

    Every mutation that goes on to announce runs inside this, and the bracket is
    what makes announcing safe rather than the announcement itself. A starter
    records how the files looked before this change; an observer applies the
    change against that record. Between them sits everything that can go wrong -
    a name already taken, a peer that is already in the state being asked for, a
    write that raises - and any of those leaves the mutation without announcing.

    Left behind, that record would be read by the next mutation on this thread
    as its own starting point. It would be a stamp taken before somebody else's
    change, so it would agree with what the reader has stored, and the reader
    would fold this change in and mark itself level with files it never read -
    dropping whatever happened in between, silently and for good. Clearing it in
    a finally is what makes "a mutation that announces without a starter is only
    slower" true, which is what the whole arrangement rests on.
    """
    with config_lock():
        _begin()
        try:
            yield
        finally:
            _end()


def _begin() -> None:
    """Tell the observers a mutation is about to write, while the files are untouched.

    Called by _mutating, after config_lock is taken and before the first write. A
    mutation that skips this and announces anyway is not broken: the observer
    finds it was never told when the change began, which it treats the same as
    being unable to place the change, and re-reads the files.
    """
    for starter in _starters:
        try:
            starter()
        except Exception:  # noqa: BLE001 - a bookkeeping failure must not fail the write
            log.exception("a config-write observer failed to note the start of a change")


def _end() -> None:
    """Tell the observers the mutation is over, whatever became of it."""
    for finisher in _finishers:
        try:
            finisher()
        except Exception:  # noqa: BLE001 - as above: bookkeeping, not the write
            log.exception("a config-write observer failed to note the end of a change")


@dataclass
class ServerView:
    iface: str
    address: str
    # The whole tunnel network, prefix included ("10.13.13.0/24"). The UI needs
    # the prefix: it is what the split-tunnel AllowedIPs value has to be, and
    # three octets could only ever describe a /24.
    subnet_cidr: str
    subnet_capacity: int
    # The tunnel's IPv6 network and what this server does with it. Both empty
    # when the tunnel carries no IPv6 at all, which is a server that has not
    # been through the upgrade yet and whose clients are still leaking.
    subnet6_cidr: str
    subnet6_mode: str
    listen_port: int
    mtu: int
    public_key: str
    params: dict[str, str]
    dns: str
    endpoint_host: str
    endpoint_port: str
    keepalive: str
    allowed_ips_default: str
    post_up: list[str] = field(default_factory=list)
    post_down: list[str] = field(default_factory=list)
    pre_down: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- server


def read_server() -> ServerView:
    """The server as it is on disk right now. The private key never leaves here."""
    conf = _read_conf()
    env = clientsenv.read_env()
    section = conf.interface
    private = section.get("PrivateKey") or ""
    params = {
        key: value
        for key in (*validate.AWG_PARAMS, *validate.SERVER_ONLY_PARAMS)
        if (value := section.get(key)) is not None
    }
    network = _subnet(conf, env)
    return ServerView(
        iface=iface(),
        address=_address_line(conf),
        subnet_cidr=str(network),
        subnet_capacity=subnet.capacity(network),
        subnet6_cidr=(subnet6.cidr(network6) if (network6 := _subnet6(conf, env)) else ""),
        subnet6_mode=(env.get("SUBNET6_MODE", "") if network6 else ""),
        listen_port=_int(section.get("ListenPort")),
        mtu=_int(section.get("MTU")),
        public_key=keys.pubkey(private) if keys.is_key(private) else "",
        params=params,
        dns=env.get("CLIENT_DNS", ""),
        endpoint_host=env.get("ENDPOINT_HOST", ""),
        endpoint_port=env.get("ENDPOINT_PORT", ""),
        keepalive=env.get("KEEPALIVE", ""),
        allowed_ips_default=env.get("CLIENT_ALLOWED_IPS", ""),
        post_up=section.all("PostUp"),
        post_down=section.all("PostDown"),
        pre_down=section.all("PreDown"),
    )


def save_server(changes: dict) -> dict:
    """Validate and persist server settings, then report what applying them costs.

    `changes` accepts the snake_case field names of ServerView (listen_port, mtu,
    address, subnet_cidr, dns, endpoint_host, endpoint_port,
    allowed_ips_default, keepalive, post_up, post_down, pre_down) and any
    obfuscation parameter under its catalog name ("Jc", "S1", "I1", ...). A key
    this module does not know is ignored, as validate_params ignores one, so a
    caller may hand over a whole settings object. A value of None means "not
    provided"; an empty string clears the parameter.

    Validation runs over the merged before-and-after picture, not just the
    changed keys, because the cross-field rules (Jmin < Jmax, non-overlapping
    header ranges) need both sides. That means a pre-existing invalid value
    blocks the save; it is the safe direction, since a half-valid obfuscation
    set fails handshakes with nothing in any log.

    Nothing is applied to the running interface here - that drops every session,
    so the caller decides when - but the client config files are regenerated
    immediately when a client-visible value changed. Deferring that would leave
    the server saying one thing and every issued config another, with nothing
    anywhere recording that a rebuild was owed.

    Returns {"needs_restart", "must_reimport", "warnings", "resynced"}.
    """
    # resync_all runs re-entrantly inside the lock below, so its own warm-up
    # would be too late to be worth anything.
    _warm_endpoint()
    with config_lock():
        conf = _read_conf()
        env = clientsenv.read_env()
        current = _current_values(conf, env)
        proposed, origin = _normalise(changes, current.get("Address", ""))

        merged = {**current, **proposed}
        errors = validate.validate_params(merged, features=get_controller().features())
        if errors:
            raise ValidationError(
                {origin.get(key, key): message for key, message in errors.items()}
            )
        _refuse_moving_the_prefix6(current.get("Address", ""), merged.get("Address", ""), origin)

        changed = {key for key, value in proposed.items() if current.get(key, "") != value}
        obfuscation = changed.intersection(validate.AWG_PARAMS)
        # Deliberately not folded into `obfuscation` above. It is an [Interface]
        # value like the rest, so the interface has to come back for it - but it
        # appears in no client config, which makes telling the fleet to
        # re-import a lie about a change no peer can read.
        server_only = changed.intersection(validate.SERVER_ONLY_PARAMS)
        needs_restart = bool(obfuscation or server_only or changed.intersection(_CONF_NETWORK))
        must_reimport = bool(obfuscation or changed.intersection(_CLIENT_VISIBLE))
        # A client's Endpoint is "<host>:<ListenPort>" unless clients.env pins
        # EndpointPort, which it does not by default. Moving the port therefore
        # invalidates every issued config, and saying otherwise would leave a
        # fleet of clients quietly unable to connect with the panel reporting
        # success.
        if "ListenPort" in changed and not merged.get("EndpointPort", "").strip():
            must_reimport = True

        warnings = validate.warnings_for(merged)
        warnings.extend(_subnet_warnings(merged, conf))
        if not changed:
            return {
                "needs_restart": False,
                "must_reimport": False,
                "warnings": warnings,
                "resynced": 0,
            }

        if (
            changed.intersection(_CONF_NETWORK)
            or obfuscation
            or server_only
            or changed.intersection(validate.HOOK_PARAMS)
        ):
            backup_conf()
            _apply_to_conf(conf, proposed, changed)
            warnings.extend(
                _retarget_nat(conf, current.get("Address", ""), merged.get("Address", ""))
            )
            _write_conf(conf)

        env_changes = _env_changes(proposed, changed, env, current.get("Address", ""))
        clientsenv.update_env(env_changes)
        # Moving the tunnel moves a split tunnel with it, and that only reaches
        # the devices through a rebuild - which a changed Address does not ask
        # for on its own, since the addresses themselves do not move.
        moved_scope = _moved_scope(current.get("Address", ""), merged.get("Address", ""))
        must_reimport = must_reimport or "CLIENT_ALLOWED_IPS" in env_changes

        resynced = 0
        if must_reimport:
            try:
                # Passed explicitly because a resync otherwise keeps each
                # client's own DNS, and a new default would then reach nobody
                # who had ever been given one.
                resynced = resync_all(
                    dns=proposed["DNS"] if "DNS" in changed else None, move_scope=moved_scope
                )
            except AwgError as exc:
                # The settings are already on disk and correct; only the client
                # files are stale. Saying so beats undoing a good save.
                warnings.append(
                    f"Settings saved, but the client configs could not be regenerated: {exc} "
                    "Fix that and run 'awg-panel manage resync', or the clients will keep the "
                    "old settings."
                )

    return {
        "needs_restart": needs_restart,
        "must_reimport": must_reimport,
        "warnings": warnings,
        "resynced": resynced,
    }


# ------------------------------------------------------------------ clients


def list_clients() -> list[ClientView]:
    """Every peer in the server config, disabled ones included, in file order.

    Peers without a "# Client" comment are kept with name None rather than
    dropped: the panel shows them as unnamed, and their address is allocated
    either way. Hiding them would hand the same address out twice.
    """
    conf = _read_conf()
    env = clientsenv.read_env()
    return [_view(peer, env) for peer in conf.peers if peer.public_key]


def get_client(name: str) -> ClientView:
    """One client by name."""
    conf = _read_conf()
    return _view(_require_peer(conf, name), clientsenv.read_env())


def scan_server() -> ServerScan:
    """Everything the server config alone says, without opening a single client file.

    The difference from list_clients() is the whole reason this exists.
    ClientView carries `allowed_ips`, which is not in the server config at all -
    it is a line in the client's own clients/<name>.conf - so building one costs
    an open and a parse per peer, and building the list costs one per client on
    the server. That is the right trade for a caller that wants a handful of
    clients and the wrong one for apps.clients.index, which wants every peer and
    already knows which of their files have changed since it last looked.

    So this stops at the config: the peer entries, the subnet they were allocated
    from, and whether the tunnel carries IPv6 at all. What each client sends
    through the tunnel is left for the caller to read for the clients it decides
    it needs.

    Unnamed peers are included. They cannot be listed - the API addresses a
    client by name - but they are holding addresses, and `free` would overcount
    by however many of them there are if they were dropped here.
    """
    return scan_of(_read_conf(), clientsenv.read_env())


def scan_of(conf: ServerConf, env: dict[str, str]) -> ServerScan:
    """The same scan, over a config the caller has already parsed.

    For a writer that is holding the config it just wrote: parsing thirty
    thousand lines again to learn what it already knows is most of what a
    rebuild costs, and it is the whole of what the fast path in
    apps.clients.index exists to avoid.
    """
    network = _subnet(conf, env)
    peers = [
        PeerScan(
            name=peer.name or None,
            public_key=peer.public_key,
            ip=_peer_ip(peer),
            ip6=_peer_ip6(peer),
            enabled=peer.disabled_at is None,
            created=peer.created,
        )
        for peer in conf.peers
        if peer.public_key
    ]
    # The allocator's own notion of a taken address, not a second one built from
    # PeerScan.ip. _used_hosts reads every AllowedIPs entry a peer carries; this
    # used to read one address per peer, so a peer holding a bare address or
    # listing its v6 entry first counted for nothing and the panel offered a
    # free address that _next_ip would not have handed out. Two answers to "how
    # many are left" is one more than there should be.
    used = sum(
        1
        for value in _used_hosts(conf, network)
        if subnet.first_host(network) <= value <= subnet.last_host(network)
    )
    network6 = _subnet6(conf, env)
    return ServerScan(
        peers=peers,
        subnet_cidr=str(network),
        free_ips=subnet.capacity(network) - used,
        has_ipv6=network6 is not None,
        subnet6_cidr=str(network6) if network6 is not None else "",
        mtu=_int(conf.interface.get("MTU")),
    )


def client_conf_stat(name: str | None) -> tuple[int, int]:
    """(mtime_ns, size) for a client's own config, or (0, 0) if there is none.

    Two numbers instead of the file, so a caller holding a previous reading can
    tell whether re-reading it would tell it anything new. Missing reads as
    (0, 0), which no real file can return, so a config that appears later is
    never mistaken for one that was already seen.
    """
    if not name:
        return (0, 0)
    try:
        info = _client_path(name).stat()
    except OSError:
        return (0, 0)
    return (info.st_mtime_ns, info.st_size)


def client_allowed_ips(name: str | None, env: dict[str, str] | None = None) -> str:
    """What this client sends through the tunnel, or the server default if it says nothing.

    The same value _view puts on a ClientView, for a caller reading one client's
    file at a time rather than all of them.
    """
    values = _client_values(name)
    defaults = env if env is not None else clientsenv.read_env()
    return values.get("AllowedIPs") or defaults.get("CLIENT_ALLOWED_IPS", "")


def _check_client_values(allowed_ips: str | None, dns: str | None) -> None:
    """Refuse the two operator-supplied config values before anything is written.

    _emit_client_conf refuses them too, and that is where the rule belongs: it is
    the one place a client config is rendered, so nothing can route round it. But
    by the time it runs, add_client has already appended the peer and written the
    server config - deliberately, so an interrupted add leaves a reserved address
    rather than a config naming an address the server will reissue. A refusal is
    not an interruption, and it should not leave that state behind either: the
    request was bad before any of it started, and nothing should have moved.

    So the check happens twice on purpose. Here it is about answering the caller
    with the config untouched; there it is about the invariant holding for every
    path that reaches the renderer.
    """
    if dns is not None:
        no_line_break("dns", dns)
    if allowed_ips is not None:
        no_line_break("allowed_ips", allowed_ips)


def _supplied(value: str | Unset) -> str | None:
    """The argument as a string, or None when the caller did not pass one."""
    return None if isinstance(value, Unset) else value


def _resolve_client_value(supplied: str | Unset, existing: str | None, default: str) -> str:
    """Which of the three candidates a client config should be rendered with.

    Not passed: what the file already says, falling back to the server default
    for a file that says nothing. Passed empty: the server default, which is the
    whole point of being able to pass empty. Passed a value: that value.
    """
    if isinstance(supplied, Unset):
        return existing or default
    return supplied or default


def add_client(
    name: str | None = None, *, allowed_ips: str | None = None, dns: str | None = None
) -> ClientView:
    """Create a client: keys, preshared key, next free address, peer entry, config.

    A name of None means the server picks one - see `_free_name` for what it
    picks and why the choice is made in here rather than by the caller. The name
    the client ended up with is on the view that comes back either way, so
    nothing downstream has to know which of the two happened.

    The obfuscation values in the client's config are copied out of the live
    server config rather than reconstructed. They have to match this server's
    exactly - they are the profile, and a client whose numbers differ by one
    does not hand-shake - so there is no second place they could be derived from.
    """
    if name is not None:
        validate_name(name)
    _check_client_values(allowed_ips, dns)
    _warm_endpoint()
    with _mutating():
        conf = _read_conf()
        env = clientsenv.read_env()
        if name is None:
            name = _free_name(conf)
        elif _find_peer(conf, name) is not None:
            raise Conflict(
                f"a client called '{name}' already exists. Pick another name.", NAME_IN_USE
            )

        address = _next_ip(conf, env)
        address6 = _client_ip6(conf, env, address)
        private = keys.genkey()
        preshared = keys.genpsk()
        peer = Peer(
            public_key=keys.pubkey(private),
            preshared_key=preshared,
            allowed_ips=_peer_allowed(address, address6),
            name=name,
            created=time.strftime(CREATED_FMT, time.gmtime()),
        )
        # Server side first, then the client file, then the live interface. The
        # order is what an interrupted run leaves behind: a peer entry with no
        # config file is a reserved address and a client who never got anything,
        # which resync or a delete can sort out. The other way round leaves a
        # config file naming an address the server will hand to somebody else.
        conf.peers.append(peer)
        _write_conf(conf)
        _write_client_conf(
            name,
            _emit_client_conf(
                conf,
                env,
                private_key=private,
                address=address,
                address6=address6,
                dns=dns or env.get("CLIENT_DNS", ""),
                allowed_ips=_allowed_with_v6(
                    allowed_ips or env.get("CLIENT_ALLOWED_IPS", ""), _subnet6(conf, env)
                ),
                preshared_key=preshared,
            ),
        )
        _announce(conf, ConfChange(added=(peer.public_key,)))
        apply_live(conf=conf)
        return _view(peer, env)


def remove_client(name: str) -> None:
    """Delete the peer, its config file and its traffic history, then apply live."""
    with _mutating():
        conf = _read_conf()
        peer = _require_peer(conf, name)
        conf.peers.remove(peer)
        _write_conf(conf)
        _client_path(name).unlink(missing_ok=True)
        _forget_traffic(peer.public_key)
        _announce(conf, ConfChange(removed=(peer.public_key,)))
        apply_live(conf=conf)


def remove_clients(names: Iterable[str]) -> list[str]:
    """Delete several clients in one pass, and report which ones were there.

    The same work remove_client does, with the parts that cost something done
    once instead of once per client: the server config is parsed and rewritten
    a single time, traffic.db is read and rewritten a single time, and the
    kernel is handed the result once at the end. Removing thirty expired
    clients one call at a time is thirty config rewrites and thirty syncconf
    runs, which on a wide subnet is seconds of work and thirty windows in which
    a reader sees a config that is halfway through the operation.

    A name with no peer behind it is skipped rather than raised over. The
    caller listed those names from a config read earlier, and anything having
    removed one in between - another worker, the collector, an admin with an
    editor - is not an error but the same outcome by another route. Raising
    would abandon the rest of a batch over a peer that is already gone.

    What comes back is what this call actually removed, in the
    order asked for, so the caller can report a count it can stand behind and
    clean up the rows that go with it.
    """
    with _mutating():
        conf = _read_conf()
        removed: list[str] = []
        keys_gone: list[str] = []
        for name in names:
            peer = _find_peer(conf, name)
            if peer is None:
                continue
            conf.peers.remove(peer)
            removed.append(name)
            keys_gone.append(peer.public_key)
        if not removed:
            return []

        _write_conf(conf)
        for name in removed:
            _client_path(name).unlink(missing_ok=True)
        _forget_traffic(*keys_gone)
        _announce(conf, ConfChange(removed=tuple(keys_gone)))
        apply_live(conf=conf)
    return removed


def rename_client(old: str, new: str) -> None:
    """Change the display name only. Keys, address and sessions are untouched."""
    validate_name(new)
    with _mutating():
        conf = _read_conf()
        peer = _require_peer(conf, old)
        if old != new and _find_peer(conf, new) is not None:
            raise Conflict(
                f"a client called '{new}' already exists. Pick another name.", NAME_IN_USE
            )
        peer.name = new
        _write_conf(conf)
        source = _client_path(old)
        if source.is_file():
            os.replace(source, _client_path(new))
        # After the file moves, not before: the row's AllowedIPs is read from the
        # path the new name gives, and that path does not exist until now.
        _announce(conf, ConfChange(updated=(peer.public_key,)))
    # No syncconf: the name is a comment, so the kernel's view has not changed.


def set_client_enabled(name: str, enabled: bool, reason: str = "manual") -> None:
    """Add or remove the "# Disabled" marker, then re-apply the live interface.

    The peer keeps its entry either way, so its address stays reserved and its
    config file survives; enforcement is that strip_conf leaves disabled peers
    out of what the kernel is given. `reason` is recorded by the caller's
    database, not here - the config file only ever gains comment lines the bash
    tools already understand.

    Disabling something already disabled keeps the original timestamp: the
    collector re-asserts enforcement every minute, and a moving timestamp would
    rewrite the server config every time it did.
    """
    with _mutating():
        conf = _read_conf()
        peer = _require_peer(conf, name)
        if enabled:
            if peer.disabled_at is None:
                return
            peer.disabled_at = None
        else:
            if peer.disabled_at is not None:
                return
            peer.disabled_at = time.strftime(CREATED_FMT, time.gmtime())
        _write_conf(conf)
        _announce(conf, ConfChange(updated=(peer.public_key,)))
        apply_live(conf=conf)


def set_clients_enabled(names: Iterable[str], enabled: bool) -> list[str]:
    """Switch several clients at once, and report which ones actually moved.

    The same work set_client_enabled does, with the parts that cost something
    done once instead of once per client: one parse of the server config, one
    rewrite of it, one announcement and one syncconf however many peers are in
    the list.

    That difference is the whole point, and it is about a burst rather than
    about a busy server. Clients cross a data limit whenever they happen to,
    which is usually one at a time - but an admin who lowers a default quota, or
    a restore that brings back a config every peer has already outrun, puts the
    whole set over the line in the same instant. One call each would be one full
    rewrite of the server config each, sequentially, with the config lock held
    across all of them: every other worker and the collector wait for the last
    one. On four thousand peers that is not a pause anybody would attribute to a
    quota.

    A name with no peer behind it is skipped rather than raised over, and so is
    one already in the state being asked for. Both are the same situation as in
    remove_clients: the caller listed these names from a config it read earlier,
    and something else having got there first is not an error. What comes back
    is what this call changed, so the caller records reasons for exactly those.

    Nothing is written at all when that list is empty, which is the common case
    for the collector re-asserting a decision it has already applied - and it
    matters more than it looks, because a write here is a rewrite of the file
    the client index stamps, and stamping it every minute for no change would
    put the index's own rebuild on a timer.
    """
    with _mutating():
        conf = _read_conf()
        moved: list[str] = []
        keys_moved: list[str] = []
        stamp = None if enabled else time.strftime(CREATED_FMT, time.gmtime())
        # By name, once, rather than a scan of the peer list per name. Both are
        # the same work for the one client that usually crosses a limit, and the
        # difference only shows on the burst this call exists for: a thousand
        # names against four thousand peers is four million comparisons the
        # other way, which is measurably worse than everything else here put
        # together.
        by_name = _peers_by_name(conf)
        for name in names:
            peer = by_name.get(name)
            if peer is None or (peer.disabled_at is None) == enabled:
                continue
            # One timestamp for the whole batch: these peers were switched off by
            # the same decision at the same moment, and reading a different second
            # off the clock for each would suggest they were not.
            peer.disabled_at = stamp
            moved.append(name)
            keys_moved.append(peer.public_key)
        if not moved:
            return []

        _write_conf(conf)
        _announce(conf, ConfChange(updated=tuple(keys_moved)))
        apply_live(conf=conf)
    return moved


def update_client(
    name: str, *, allowed_ips: str | Unset = UNSET, dns: str | Unset = UNSET
) -> ClientView:
    """Rewrite one client's config with a new split-tunnel scope or DNS.

    Only the client file changes: both values are client-side, so the server has
    nothing to re-apply and no session is dropped. The device has to re-import.

    Each value has three states, and UNSET is what makes the third one sayable.
    An absent argument keeps whatever the client's file already carries; a value
    replaces it; and "" clears the client's own setting, which puts it back on
    the server default in clients.env rather than leaving it with no setting at
    all - the same thing a client added without one gets. Both used to arrive as
    None and only the first of the three could be expressed, so an admin who
    emptied the DNS box got a 200 and a config that still carried the old
    resolver.
    """
    _check_client_values(_supplied(allowed_ips), _supplied(dns))
    _warm_endpoint()
    with _mutating():
        conf = _read_conf()
        env = clientsenv.read_env()
        peer = _require_peer(conf, name)
        existing = _client_values(name)
        private = existing.get("PrivateKey", "")
        preshared = existing.get("PresharedKey", "")
        if not private or not preshared:
            raise NotFound(
                f"there is no usable config file for '{name}', so its private key is gone. "
                "Reset its keys to issue a new one."
            )
        _write_client_conf(
            name,
            _emit_client_conf(
                conf,
                env,
                private_key=private,
                address=_peer_ip(peer),
                address6=_client_ip6(conf, env, _peer_ip(peer)),
                dns=_resolve_client_value(dns, existing.get("DNS"), env.get("CLIENT_DNS", "")),
                allowed_ips=_allowed_with_v6(
                    _resolve_client_value(
                        allowed_ips,
                        existing.get("AllowedIPs"),
                        env.get("CLIENT_ALLOWED_IPS", ""),
                    ),
                    _subnet6(conf, env),
                ),
                preshared_key=preshared,
            ),
        )
        # The one mutation that never touches the server config, so nothing has
        # announced anything yet and this is the only notification it sends. A
        # crash before it still moves the clients directory's timestamp, which is
        # what the index stamps and re-reads on.
        _announce(conf, ConfChange(updated=(peer.public_key,)))
        return _view(peer, env)


def reset_client_keys(name: str) -> ClientView:
    """Issue a fresh keypair and preshared key, keeping the name, address and scope.

    The old key stops working the moment this is applied, which is the point:
    it is what an admin reaches for when a device is lost.
    """
    _warm_endpoint()
    with config_lock():
        conf = _read_conf()
        env = clientsenv.read_env()
        peer = _require_peer(conf, name)
        existing = _client_values(name)
        private = keys.genkey()
        preshared = keys.genpsk()
        old_public = peer.public_key
        peer.public_key = keys.pubkey(private)
        peer.preshared_key = preshared
        _write_conf(conf)
        _rekey_traffic(old_public, peer.public_key)
        _write_client_conf(
            name,
            _emit_client_conf(
                conf,
                env,
                private_key=private,
                address=_peer_ip(peer),
                address6=_client_ip6(conf, env, _peer_ip(peer)),
                dns=existing.get("DNS") or env.get("CLIENT_DNS", ""),
                allowed_ips=_allowed_with_v6(
                    existing.get("AllowedIPs") or env.get("CLIENT_ALLOWED_IPS", ""),
                    _subnet6(conf, env),
                ),
                preshared_key=preshared,
            ),
        )
        apply_live(conf=conf)
        return _view(peer, env)


def resync_all(dns: str | None = None, move_scope: tuple[str, str] | None = None) -> int:
    """Rebuild every client config from the server's current settings.

    Unnamed peers and peers with no config file are skipped, each client keeps
    its own keys, address, DNS and scope unless `dns` overrides it, and a client
    whose stored private key does not
    derive to the public key in its peer entry is skipped rather than rewritten -
    that mismatch means the file was hand-edited or corrupted, and rewriting it
    would destroy the only copy of a key that might still be recoverable.

    `move_scope` is (old network, new network) when the tunnel has been moved.
    A client whose AllowedIPs is exactly the old network is not expressing a
    preference, it is saying "the tunnel", and the tunnel moved - so it follows.
    Any other scope is a route the admin chose and is left alone.

    Returns the number of configs written.
    """
    # Before the loop, not inside it: a bad override would otherwise be found on
    # the first client and leave every client after it holding the old settings,
    # which is a half-resynced server nobody asked for.
    _check_client_values(None, dns)
    written = 0
    _warm_endpoint()
    with config_lock(), write_batch(client_dir()):
        conf = _read_conf()
        env = clientsenv.read_env()
        network6 = _subnet6(conf, env)
        # Before the configs are rebuilt, so the addresses written into them are
        # ones the server already routes.
        backfilled = _backfill_peer_ipv6(conf, env)
        if backfilled:
            _write_conf(conf)
        for peer in conf.peers:
            if not peer.public_key or not peer.name:
                continue
            path = _client_path(peer.name)
            if not path.is_file():
                continue
            current = _client_text(peer.name)
            values = parse_client_conf(current)
            private = values.get("PrivateKey", "")
            preshared = values.get("PresharedKey", "")
            if not private or not preshared:
                continue
            if not keys.is_key(private) or keys.pubkey(private) != peer.public_key:
                continue
            allowed = values.get("AllowedIPs") or env.get("CLIENT_ALLOWED_IPS", "")
            if move_scope is not None and allowed.strip() == move_scope[0]:
                allowed = move_scope[1]
            text = _emit_client_conf(
                conf,
                env,
                private_key=private,
                address=_peer_ip(peer),
                address6=_client_ip6(conf, env, _peer_ip(peer)),
                dns=dns or values.get("DNS") or env.get("CLIENT_DNS", ""),
                allowed_ips=_allowed_with_v6(allowed, network6),
                preshared_key=preshared,
            )
            # A file that already says this is not rewritten. The comparison is
            # free - the text was read a few lines up to get the keys out of it -
            # and what it buys is the whole cost of the write, which is an fsync
            # apiece and is what makes this the slowest thing the panel does.
            #
            # It matters because a resync is not always a change. Re-running the
            # installer, saving a settings page having edited nothing, or running
            # the command by hand to be sure all land here with nothing to do,
            # and each of those used to rewrite every client config on the server.
            # `written` still counts the files this brought up to date, which is
            # what a caller reporting "re-issued N configs" means by it.
            if text == current:
                continue
            _write_client_conf(peer.name, text, sync_dir=False)
            written += 1
    # Outside the lock, which apply_live takes for itself. Only when a peer
    # actually gained an address: the kernel has to know about it before the
    # client that was just handed one tries to use it.
    if backfilled:
        apply_live()
    return written


def _backfill_peer_ipv6(conf: ServerConf, env: dict[str, str]) -> int:
    """Route IPv6 to every peer that has none, in place. Returns how many changed.

    The server's half of what _emit_client_conf does for the client: both have
    to happen for a peer to carry IPv6 at all, and the two are written together
    for that reason rather than left to meet by accident. A peer that
    already has an IPv6 entry is skipped, so this is safe to run repeatedly,
    and so is one numbered outside the pool - an offset invented for a
    hand-numbered peer would land on a real client's address.
    """
    network6 = _subnet6(conf, env)
    if network6 is None:
        return 0
    network4 = _subnet(conf, env)
    changed = 0
    for peer in conf.peers:
        if ":" in peer.allowed_ips:
            continue
        address = _peer_ip(peer)
        offset = subnet6.offset_of(network4, address)
        if offset is None or offset <= 0:
            continue
        peer.allowed_ips = _peer_allowed(address, subnet6.host_addr(network6, offset))
        changed += 1
    return changed


def client_conf_text(name: str) -> str:
    """The client's config file verbatim.

    This is the only thing in the panel that hands out a client private key. The
    caller must put it in a download or a QR code and nowhere else - not a log,
    not an error message, not a JSON field beside other data.
    """
    path = _client_path(name)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFound(
            f"there is no config file for '{name}'. Reset its keys to issue a new one."
        ) from exc


def next_ip() -> str:
    """The address add_client would hand out next."""
    return _next_ip(_read_conf(), clientsenv.read_env())


def subnet_network() -> ipaddress.IPv4Network:
    """The tunnel network, read fresh. Callers outside this module get it from here.

    There is one answer to "which network is this server on", and it is the one
    the allocator uses; anything that reports capacity or decides whether an
    address is in range has to ask the same question of the same files.
    """
    return _subnet(_read_conf(), clientsenv.read_env())


def subnet6_network() -> ipaddress.IPv6Network | None:
    """The tunnel's IPv6 network, read fresh, or None when it carries none.

    None is a real answer here rather than a failure: it is what a server
    installed before this could carry IPv6 looks like, and what the client list
    needs in order to say which clients are still routing only IPv4.
    """
    return _subnet6(_read_conf(), clientsenv.read_env())


# ------------------------------------------------------------------ applying


def apply_live(controller: BaseController | None = None, conf: ServerConf | None = None) -> bool:
    """Push the on-disk config onto the running interface.

    Returns False when the interface is down, which is not an error: the change
    is already on disk, and the next bring-up loads it. Callers say so rather
    than failing an operation that succeeded.

    `conf` is what the caller just wrote, for the mutations that call this at the
    end of their own work. Every one of them is holding the config it rendered a
    moment earlier, and reading it back to hand to the kernel was a second full
    parse of a file already in memory - on four thousand clients the largest
    single cost in adding one. It must be the config *after* _write_conf, which
    is what _ensure_hooks may have altered on the way past; passing the object
    that was written is what guarantees that.

    Left out, this reads the file, which is right for every caller that is not
    mid-mutation: the collector re-asserting enforcement and the server settings
    page have nothing in hand and must see whatever is on disk now.
    """
    tunnel = controller or get_controller()
    with config_lock():
        if not tunnel.iface_up():
            return False
        # strip_conf already drops disabled peers, so a key that is meant to be
        # revoked cannot reach the kernel through this path.
        tunnel.syncconf(strip_conf(conf if conf is not None else _read_conf()))
    return True


def restart_iface(controller: BaseController | None = None) -> None:
    """Full down/up. Drops every session, so only for port, address, MTU and obfuscation.

    `awg-quick up` loads the whole config into the kernel, and disabled peers
    are still in that file on purpose - it is what reserves their address. So
    the bring-up re-admits every client that quota, expiry or an admin took
    away. install.sh writes a PostUp hook that undoes this for every path
    including boot, but a config from an older install may not carry it yet, so
    re-assert here too rather than trust it.
    """
    ctl = controller or get_controller()
    ctl.restart()
    apply_live(ctl)


def stop_iface(controller: BaseController | None = None) -> None:
    """Take the tunnel down and leave it down. Every session drops and none come back.

    Deliberate, unlike the down half of a restart: nothing here puts it back,
    and nothing else will until somebody starts it again or the machine reboots
    and systemd brings the enabled unit up. Leaving the unit enabled is the
    choice - a stop that also survived a reboot would be a second, quieter piece
    of state with nothing on the dashboard to show it, and an admin who wants
    that has `systemctl disable` and knows it.
    """
    (controller or get_controller()).service_stop()


def start_iface(controller: BaseController | None = None) -> None:
    """Bring the tunnel back up, with the revocations that were in force before it.

    apply_live for the same reason restart_iface has it: `awg-quick up` loads the
    whole config, disabled peers included, so the bring-up re-admits every client
    quota, expiry or an admin took away. The PostUp hook install.sh writes undoes
    that, but a config from an older install may not carry it, and a tunnel that
    was deliberately stopped is exactly where a stale peer would go unnoticed
    longest.
    """
    ctl = controller or get_controller()
    ctl.service_start()
    apply_live(ctl)


def backup_conf() -> Path:
    """Copy the server config to <iface>.conf.bak-YYYYmmddHHMMSS beside itself.

    Same name install.sh uses, and the same UTC it stamps it in, so the two
    tools' backups sort together. A second backup within the same second
    overwrites the first, exactly as `cp -a` does there; the file has not
    changed in between.
    """
    source = server_conf()
    if not source.is_file():
        raise AwgError(f"there is no server config at {source} to back up.")
    target = source.with_name(f"{source.name}.bak-{time.strftime(BACKUP_FMT, time.gmtime())}")
    with config_lock():
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
    return target


def validate_name(name: str) -> None:
    """Reject a name that cannot safely be a file name or a config comment."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValidationError(
            {
                "name": (
                    "Use letters, digits, dots, underscores and hyphens only, starting with a "
                    "letter or digit, up to 32 characters."
                )
            }
        )


def bootstrap_if_missing() -> None:
    """With AWG_MOCK=1 and no config present, write a demo server and a few clients.

    This is what makes `AWG_MOCK=1 manage.py runserver` show a populated panel on
    a laptop with no kernel module. It is a no-op on a real server: without the
    mock controller the panel must show the truth, which is that the VPN is not
    installed yet.
    """
    if not mock_enabled() or server_conf().exists():
        return
    with config_lock():
        if server_conf().exists():
            return  # the web and collector processes both call this at startup
        atomic_write(server_conf(), _demo_conf_text(), mode=0o600)
        if not env_file().exists():
            atomic_write(env_file(), _demo_env_text(), mode=0o600)
        for name in DEMO_CLIENTS:
            add_client(name)


# ------------------------------------------------------------------ internals


# The last config this thread wrote, and the (mtime_ns, size, inode) of the file
# it wrote it to. Kept so that a mutation which follows one of ours does not
# parse a file we already hold the parse of - see _read_conf.
#
# Thread-local, which is what makes it safe rather than merely fast. The panel
# serves requests on a thread pool, and a ServerConf handed to two threads is two
# threads editing each other's work; per thread there is only ever one caller.
# It costs a cold entry per thread, paid once.
_parsed = threading.local()


def _conf_stamp(path: Path) -> tuple[int, int, int]:
    """(mtime_ns, size, inode) of the server config, or zeroes if it is not there.

    Zeroes never match a stored stamp, because a file that does not exist is not
    a file we can be holding the parse of.
    """
    try:
        info = path.stat()
    except OSError:
        return (0, 0, 0)
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def _read_conf() -> ServerConf:
    """The server config, parsed. From memory when this thread wrote it last.

    Parsing is the largest single cost in changing anything: on four thousand
    clients it is about twenty-four milliseconds of a hundred, and every mutation
    pays it to read back a file the previous mutation just wrote. Provisioning
    over the API is that case five hundred times in a row.

    So a mutation leaves its parse behind (see _write_conf) and this hands it
    back, having first checked with a stat that the file is still the one that
    parse came from. Anything else having written - another worker, the
    collector, a hand edit over SSH, a restore - moves the stamp and is read
    from disk exactly as before. The stat is what makes a cache of a file this
    process does not exclusively own safe to have, and it is three numbers
    against twenty-four milliseconds.

    What comes back is always a copy. Callers edit this structure in place, so
    handing out the stored object would let one mutation's edits show up in the
    next one's starting point - including a mutation that failed and was never
    written. Copying is a third of the price of parsing, which is where the
    saving comes from; it is not free, and that is the honest cost of the idea.
    """
    path = server_conf()
    held = getattr(_parsed, "conf", None)
    if held is not None:
        stamp, conf = held
        if stamp != (0, 0, 0) and stamp == _conf_stamp(path):
            return clone_conf(conf)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotConfigured(
            f"no server config at {path}. Either AmneziaWG is not installed on this machine, "
            f"or AWG_IFACE/AWG_CONF_DIR point somewhere else."
        ) from exc
    except OSError as exc:
        raise AwgError(f"cannot read {path}: {exc}") from exc
    # Not stored. A read that nothing wrote is a read of somebody else's file,
    # and storing it would mean paying for a copy on the way out of every one of
    # them to save a parse on a repeat that may never come. Only a write knows
    # its parse is worth keeping.
    return parse_conf(text)


def _write_conf(conf: ServerConf) -> None:
    """Render and replace the server config, and keep the parse behind for the next read.

    The stat is taken after the write and describes the file this config was
    rendered into, so the next _read_conf can tell "still ours" from "somebody
    else has written since". `conf` itself is stored rather than a copy of it:
    this is the last thing in every mutation that changes the configuration -
    only _announce and apply_live follow, and neither writes to it - and
    _read_conf copies on the way out, so no caller can ever reach this object.
    """
    _ensure_hooks(conf)
    path = server_conf()
    atomic_write(path, render_conf(conf), mode=0o600)
    _parsed.conf = (_conf_stamp(path), conf)


def _announce(conf: ServerConf, change: ConfChange) -> None:
    """Tell the observers what a finished mutation changed, once its files are written.

    Called last, after every file the change touches is on disk - for add_client
    that is the client's own config, which the row's AllowedIPs is read from, so
    announcing at _write_conf time would record the peer as having no file.

    Being last is also what makes an interrupted mutation safe. An observer
    keeping a copy of these files records how they looked at the moment it read
    them, and only this call gives it the chance to; so a crash after the config
    is written - or between the config and the client file - leaves its record
    describing files older than the ones on disk, which is the state it already
    treats as "read them again". Nothing is announced up front for the same
    reason: a mutation that never reaches this point needs no undoing.

    A mutation that cannot describe what it did - save_server moving the subnet
    under every peer, resync_all rewriting all of them - simply does not call
    this. Its writes still move the files, and an observer that noticed the files
    move is an observer that re-reads them. So forgetting this call costs a
    re-read and can never cost accuracy, which is what makes it safe to add to
    the mutations worth speeding up and leave off the rest.
    """
    for observer in _observers:
        try:
            observer(conf, change)
        except Exception:  # noqa: BLE001
            # The config is already on disk. Raising here would report a mutation
            # as failed that in fact succeeded, and an admin would repeat it. An
            # observer's contract is that its own failures leave its copy looking
            # unread rather than current, so stepping over one costs a re-read.
            log.exception("a config-write observer failed; its copy will be rebuilt")


def _ensure_hooks(conf: ServerConf) -> None:
    """Keep the tunnel's own hooks in the config, whatever else we are writing.

    Losing the PreDown loses every client's all-time totals on the next restart;
    losing either PostUp puts every revoked client back on the interface at the
    next boot, or leaves every bandwidth ceiling unenforced until the collector's
    next pass. None of them says anything when it happens, and none can be worked
    out afterwards from what is left, so they are re-asserted on every write
    rather than trusted to whoever wrote the file last.

    A hook naming the old CLI is rewritten in place rather than left beside its
    replacement. Two lines would both run, and the stale one would fail on every
    bring-up: harmless with "|| true" after it, but it would put an error in the
    journal on a healthy server forever, which is how a real one stops being read.
    """
    for slot, markers, ours, first in (
        ("PreDown", (PREDOWN_MARKER,), (PREDOWN_HOOK,), False),
        # In this order and at this position, whatever order the file had them
        # in. Both go ahead of the firewall rules, which is where install.sh
        # puts them and where they belong - neither depends on routing, and
        # nothing is served by making a revocation queue behind it. Between the
        # two, revoking comes before shaping, though only for the reading: the
        # shaping pass takes its list of who is switched off from the config
        # rather than from the interface, so it is right either way round.
        ("PostUp", (POSTUP_MARKER, SHAPE_MARKER), (POSTUP_HOOK, SHAPE_HOOK), True),
        # The mirror image, and last for the same reason: it depends on nothing
        # above it. Matched on the plain shaping marker rather than the whole
        # `--detach` invocation, so a config carrying the wrong form of the line
        # here is corrected to this one instead of gaining a second.
        ("PostDown", (SHAPE_MARKER,), (UNSHAPE_HOOK,), False),
    ):
        lines = [_retarget_hook(line) for line in conf.interface.all(slot)]
        # Ours are taken out and put back rather than added when missing, so the
        # result does not depend on which of them the file already had. Written
        # the other way, a config that already carried one hook and not the other
        # ended up with them in a different order from a config that carried
        # neither - and then a mutation of either one produced a file that a
        # golden comparison against the other could not match.
        others = [line for line in lines if not any(marker in line for marker in markers)]
        conf.interface.set_all(slot, [*ours, *others] if first else [*others, *ours])


def _retarget_hook(line: str) -> str:
    """One hook line, pointed at the panel if it still names the retired CLI."""
    for legacy, replacement in LEGACY_HOOKS.items():
        if legacy in line:
            return replacement
    return line


def _find_peer(conf: ServerConf, name: str) -> Peer | None:
    for peer in conf.peers:
        if peer.name == name and peer.public_key:
            return peer
    return None


def _peers_by_name(conf: ServerConf) -> dict[str, Peer]:
    """Every named peer, by name, for a caller with a list of them to look up.

    The first entry wins where a hand edit has given two peers the same name,
    which is what _find_peer answers for the same config: this has to agree with
    it, or which peer an operation lands on would depend on whether the caller
    happened to be looking up one client or several.
    """
    found: dict[str, Peer] = {}
    for peer in conf.peers:
        if peer.name and peer.public_key:
            found.setdefault(peer.name, peer)
    return found


def _require_peer(conf: ServerConf, name: str) -> Peer:
    peer = _find_peer(conf, name)
    if peer is None:
        raise NotFound(f"there is no client called '{name}'.")
    return peer


def _peer_ip6(peer: Peer) -> str:
    """The IPv6 address the server routes to this peer, or "" if it routes none."""
    for entry in peer.allowed_ips.split(","):
        entry = entry.strip()
        if ":" in entry:
            return entry.split("/")[0]
    return ""


def _peer_ip(peer: Peer) -> str:
    """The IPv4 address the server routes to this peer, or "" if it routes none.

    The first entry that is not an IPv6 one, rather than the first entry: the
    panel writes v4 first - see _peer_allowed - but a hand-edited config is
    under no such obligation, and one that lists its /128 ahead of its /32 had
    the v6 address read back as the peer's tunnel address. That address then
    went into the client config's own Address line, where it is wrong twice
    over: the client numbers its interface v6-only, and _client_ip6 can find no
    v4 offset to derive the v6 half from, so the peer loses the address it had
    as well as the one it was owed.

    Written as the mirror of _peer_ip6 above, because the two answer the same
    question about one peer and the file promises nothing about the order.
    """
    for entry in peer.allowed_ips.split(","):
        entry = entry.strip()
        if entry and ":" not in entry:
            return entry.split("/")[0]
    return ""


def _view(peer: Peer, env: dict[str, str]) -> ClientView:
    name = peer.name or None
    path = _client_path(name) if name else None
    values = _client_values(name) if name else {}
    return ClientView(
        name=name,
        public_key=peer.public_key,
        preshared_key=peer.preshared_key,
        ip=_peer_ip(peer),
        ip6=_peer_ip6(peer),
        allowed_ips=values.get("AllowedIPs") or env.get("CLIENT_ALLOWED_IPS", ""),
        enabled=peer.disabled_at is None,
        disabled_at=peer.disabled_at,
        created=peer.created,
        has_conf_file=bool(path and path.is_file()),
    )


def _client_path(name: str) -> Path:
    return client_dir() / f"{name}.conf"


def _client_values(name: str | None) -> dict[str, str]:
    """Flat view of a client config, or {} when there is none to read."""
    return parse_client_conf(_client_text(name))


def _client_text(name: str | None) -> str:
    """The raw bytes of a client config as text, or "" when there is none to read."""
    if not name:
        return ""
    try:
        return _client_path(name).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return ""


def _write_client_conf(name: str, text: str, sync_dir: bool = True) -> None:
    # install -d -m 700: the directory holds every client's private key.
    #
    # sync_dir is False only for a caller inside paths.write_batch, which syncs
    # the directory once when it is done rather than once per file.
    ensure_dir(client_dir())
    atomic_write(_client_path(name), text, mode=0o600, sync_dir=sync_dir)


def _address_line(conf: ServerConf) -> str:
    """The interface's address list: every Address line, joined as one value.

    wg-quick accepts the tunnel's addresses either way round - `Address = <v4>,
    <v6>` on one line, or a line each - and the panel has only ever written the
    first form. Reading only the first line therefore worked on configs the
    panel wrote and lost half of every config written by hand or by an older
    installer: a dual-stack server with its prefix on the second line read as
    having no IPv6 at all, which is a client config with no v6 address and a
    ::/0 route that leaks.

    Joining them costs nothing on the common single-line config and gives every
    reader below the same value it would have got had the file been written
    here. The write side collapses to one line - see OrderedMulti.set - so a
    config that goes through a settings save comes out in the panel's own
    spelling with nothing dropped.
    """
    return ", ".join(value for value in conf.interface.all("Address") if value.strip())


def _subnet(conf: ServerConf, env: dict[str, str]) -> ipaddress.IPv4Network:
    """The network the allocator works in.

    The server's own Address is the authority, because it carries the prefix and
    because it is the same line the kernel routes from: an allocator working in
    a different network than the interface hands out addresses that never reach
    anybody. SUBNET_CIDR in clients.env - and the older three-octet SUBNET_BASE -
    are mirrors of it, consulted only when the config has no readable Address,
    which on a working server it always has.
    """
    for candidate in (
        _address_line(conf),
        env.get("SUBNET_CIDR", ""),
        env.get("SUBNET_BASE", ""),
    ):
        network = subnet.parse_or_none(candidate)
        if network is not None:
            return network
    return subnet.parse(subnet.DEFAULT_CIDR)


def _subnet6(conf: ServerConf, env: dict[str, str]) -> ipaddress.IPv6Network | None:
    """The tunnel's IPv6 network, or None when it carries none.

    Read the same way and in the same order as _subnet: the server's own
    Address first, SUBNET6_CIDR in clients.env only as a mirror. Unlike the
    IPv4 side there is no default to fall back on, and that is deliberate - a
    server that was never configured with a prefix has none, and inventing one
    would put an address in every client config that nothing on this machine
    routes.
    """
    for candidate in (_address_line(conf), env.get("SUBNET6_CIDR", "")):
        network = subnet6.parse_or_none(candidate)
        if network is not None:
            return network
    return None


def _client_ip6(conf: ServerConf, env: dict[str, str], address: str) -> str | None:
    """A client's IPv6 address, derived from its IPv4 one, or None.

    None for a tunnel with no IPv6 and for a peer numbered outside the pool -
    the second because an offset invented for a hand-numbered peer would land
    on some other client's address, and two clients sharing one is a tunnel
    that delivers somebody's traffic to somebody else.
    """
    network6 = _subnet6(conf, env)
    if network6 is None:
        return None
    offset = subnet6.offset_of(_subnet(conf, env), address)
    if offset is None or offset <= 0:
        return None
    return subnet6.host_addr(network6, offset)


def _allowed_with_v6(value: str, network6: ipaddress.IPv6Network | None) -> str:
    """A full tunnel stays a full tunnel once the server carries IPv6.

    A client told to route 0.0.0.0/0 was told to route everything, and once the
    server carries IPv6 "everything" includes it. Left alone, that client sends
    its IPv6 outside the tunnel - which is a leak, and a silent one, because
    every IPv4 test of it passes.

    Only an exact 0.0.0.0/0 is touched, because anything else is a split-tunnel
    route list somebody chose.
    """
    if network6 is None:
        return value
    return subnet6.with_ipv6(value)


def _peer_allowed(address: str, address6: str | None) -> str:
    """What the server routes to one client.

    The /128 belongs here as well as in the client's own Address: cryptokey
    routing is symmetric, so without it the client can send IPv6 and never
    hear anything back.
    """
    if address6 is None:
        return f"{address}/32"
    return f"{address}/32, {address6}/128"


def _reserved_host(entry: str) -> int | None:
    """The one IPv4 address an AllowedIPs entry reserves, or None if it reserves no single host.

    A bare address counts. `AllowedIPs = 10.13.0.2` is what wg accepts and means
    exactly what `10.13.0.2/32` means; this used to require the suffix, so an
    entry without it read as reserving nothing and the address stayed in the
    pool. The panel always writes the /32 - see _peer_allowed - so the configs
    that carry a bare address are the hand-edited ones and the ones an older tool
    wrote, which are also the configs nobody is auditing. What followed was the
    next client being handed an address a peer already had: two peers with one
    address is a tunnel that half works for both of them, intermittently,
    depending on which of the two the kernel matched last, and there is no error
    anywhere on the server that says so.

    A prefix wider than a single host still counts for nothing, and that is a
    separate decision rather than the same one carried further. `10.13.0.0/24` on
    a peer is a route to a network behind it, not an address this panel handed
    out. Reading it as a claim on 254 addresses would empty the pool on a server
    doing site-to-site; reading it as a claim on its base address alone would be
    a guess at which of the 254 was meant. Neither is an answer the allocator has
    any business inventing, so such an entry is left out of the question
    entirely, exactly as before.

    IPv6 falls out for free: a /128 has a prefix this rejects, and an address
    with colons in it is not an IPv4 address for as_int to parse.
    """
    address, sep, prefix = entry.strip().partition("/")
    if sep and prefix.strip() != "32":
        return None
    return subnet.as_int(address)


def _used_hosts(conf: ServerConf, network: ipaddress.IPv4Network) -> set[int]:
    """Every address already spoken for, as integers.

    `network` is not read. Peers outside it need no filtering here because
    subnet.next_free only ever scans between first_host and last_host, so an
    address from some other network cannot be handed out whether it is in this
    set or not. The parameter stays because it is what the caller has and what a
    reader expects to pass, and because filtering here would be a second place
    for the bounds to be wrong.
    """
    used: set[int] = set()
    for peer in conf.peers:
        for entry in peer.allowed_ips.split(","):
            value = _reserved_host(entry)
            if value is not None:
                used.add(value)
    return used


def _free_name(conf: ServerConf) -> str:
    """A random name this server is not already using, for a client added without one.

    Drawn by awg.names and checked twice over, because a client's name is an
    identifier: it is the key every per-client route resolves through, and it is
    the name of the file the private key is about to be written into.

    So a name is refused if a peer answers to it, which is the collision that
    matters, and refused as well if `clients/<name>.conf` exists with no peer
    behind it - a file left over from a client removed while the config was
    unwritable, or one an admin dropped in by hand. Nothing else in here would
    notice that file, and the write below would go straight over it, taking with
    it the one copy of a private key.

    Compared case-insensitively, unlike the exact match a typed name gets. A
    generated name is lower case throughout, so the only thing this rules out is
    a generated name landing beside a hand-typed "Ab3Kd9Xm2" that differs from it
    in case alone - two names that read as one in a list, and on a case-folding
    file system are one file.

    Called with the config lock held and the config already read, so the answer
    is still true when the peer is appended a few lines later.
    """
    taken = {peer.name.casefold() for peer in conf.peers if peer.name}

    def used(candidate: str) -> bool:
        return candidate in taken or _client_path(candidate).exists()

    name = names.unique_name(used)
    if name is None:
        raise Conflict(
            "could not find an unused name to give this client. Add it with a name of your own."
        )
    return name


def _next_ip(conf: ServerConf, env: dict[str, str]) -> str:
    """Lowest free host in the tunnel network, gaps reused, the server's own address reserved."""
    network = _subnet(conf, env)
    address = subnet.next_free(network, _used_hosts(conf, network))
    if address is None:
        raise Conflict(
            f"there are no free addresses left in {network} ({subnet.capacity(network)} "
            "clients). Remove a client, or move the server to a larger subnet."
        )
    return str(address)


def _forget_traffic(*public_keys: str) -> None:
    """Drop these peers' rows, so a deleted client's usage does not outlive it.

    Variadic so that removing a hundred clients reads and rewrites the file once
    rather than a hundred times. The file is small, but the collector and the
    PreDown hook both write it under the same lock, and every rewrite is a
    window one of them waits out.
    """
    wanted = [key for key in public_keys if key]
    if not wanted or not traffic_db().exists():
        return
    db = traffic.read_db()
    # A list rather than any(): the pop has to happen for every key, and a test
    # that stopped at the first row it found would leave the rest in the file.
    dropped = [key for key in wanted if db.pop(key, None) is not None]
    if dropped:
        traffic.write_db(db)


def _rekey_traffic(old_public: str, new_public: str) -> None:
    """Carry all-time totals across a key rotation.

    It is the same client to the person reading the dashboard, so its history
    follows the name, not the key. The raw counters are reset because the kernel
    starts a brand new peer at zero.
    """
    if not old_public or old_public == new_public or not traffic_db().exists():
        return
    db = traffic.read_db()
    row = db.pop(old_public, None)
    if row is None:
        return
    row.last_rx = 0
    row.last_tx = 0
    db[new_public] = row
    traffic.write_db(db)


# --------------------------------------------------------- client rendering


def _emit_client_conf(
    conf: ServerConf,
    env: dict[str, str],
    *,
    private_key: str,
    address: str,
    dns: str,
    allowed_ips: str,
    preshared_key: str,
    address6: str | None = None,
) -> str:
    """Render the .conf file a client imports.

    Every obfuscation value is copied from the live server config, which is the
    reason the two ends can never drift: a mismatch there fails the handshake
    with no error on either side. A parameter that is empty or exactly "0" is
    omitted rather than written out, because that is what the tools read as
    "off" - and a config carrying `S4 = 0` is not the same as one without it.

    The two values an operator supplies are refused here if they carry a line
    break, rather than at the API edge that happens to be sending them today.
    This is the one place a client config is built - update_client, resync_all
    and add_client all end here - so a rule that lives here covers the CLI and
    anything added later, and cannot be walked round by a second caller. See
    conf.no_line_break for what one would otherwise write into the file.
    """
    dns = no_line_break("dns", dns)
    allowed_ips = no_line_break("allowed_ips", allowed_ips)
    private = conf.interface.get("PrivateKey") or ""
    if not keys.is_key(private):
        raise AwgError(
            f"the server config at {server_conf()} has no usable PrivateKey, so no client "
            "config can be built from it."
        )
    # /128 beside the /32, and it matters as much as the ::/0 below. Several
    # clients - iOS most reliably - will not install a default route for an
    # address family the interface holds no address in, so a config with ::/0
    # and no v6 address imports without complaint and leaks exactly as before.
    interface_address = f"{address}/32" if address6 is None else f"{address}/32, {address6}/128"
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {interface_address}",
        f"DNS = {dns}",
        f"MTU = {env.get('CLIENT_MTU', '')}",
        "",
    ]
    for key in validate.AWG_PARAMS:
        value = (conf.interface.get(key) or "").strip()
        # validate's own reading of "would this be written at all", rather than
        # a second copy of it here: a switch says it is off in words, and a
        # hand-written `RandomTrailers = off` copied into every client config
        # is a line that means nothing on either end.
        if validate.is_set(key, value):
            lines.append(f"{key} = {value}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {keys.pubkey(private)}",
        f"PresharedKey = {preshared_key}",
        f"Endpoint = {_resolve_endpoint(conf, env)}",
        f"AllowedIPs = {allowed_ips}",
        f"PersistentKeepalive = {env.get('KEEPALIVE', '')}",
    ]
    return "\n".join(lines) + "\n"


def _resolve_endpoint(conf: ServerConf, env: dict[str, str]) -> str:
    """host:port a client should dial: clients.env if it says, else this server."""
    host = (env.get("ENDPOINT_HOST") or "").strip() or _detect_public_ip()
    if not host:
        raise AwgError(
            "this server's public address cannot be detected automatically. Set the endpoint "
            f"host in Server settings (ENDPOINT_HOST in {env_file()}) and try again."
        )
    port = (env.get("ENDPOINT_PORT") or "").strip() or (conf.interface.get("ListenPort") or "")
    port = port.strip()
    if not port:
        raise AwgError(
            "the server config has no ListenPort and no endpoint port is set, so a client would "
            "have no port to connect to."
        )
    return f"{host}:{port}"


def _warm_endpoint() -> None:
    """Resolve the public address before taking the lock, not while holding it.

    With ENDPOINT_HOST blank, rendering a client config asks the EC2 metadata
    service, and on a box that black-holes 169.254.169.254 that is six seconds
    of timeouts. Six seconds inside the critical section is six seconds where
    every other worker and the collector are queued on the config lock.

    The result is cached for the life of the process, so by the time the locked
    path asks there is nothing left to do. read_env is unlocked by design, so
    this costs one file read; if ENDPOINT_HOST is set in the gap, the only
    price is one probe nobody needed.
    """
    with contextlib.suppress(AwgError, OSError):
        if not (clientsenv.read_env().get("ENDPOINT_HOST") or "").strip():
            # With a margin, because the point is that nothing probes once the
            # lock is held. A cached failure that expires a second from now
            # would sail through this and then expire while the request is
            # queued on the flock, putting the whole six seconds back inside
            # the critical section.
            _detect_public_ip(margin=_DETECT_WARM_MARGIN)


def _detect_public_ip(margin: float = 0.0) -> str | None:
    """Ask the EC2 metadata service, which is the only guess worth making.

    `margin` treats a cached failure that is about to expire as already expired,
    so a caller that wants the answer settled before it takes a lock gets it
    settled rather than deferred by a second.
    """
    global _detected
    with _detect_lock:
        now = time.monotonic()
        if _detected is not None and (_detected[1] is not None or now + margin < _detected[0]):
            return _detected[1]

        token = _http("PUT", _IMDS_TOKEN_URL, {"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        headers = {"X-aws-ec2-metadata-token": token} if token else {}
        address = _http("GET", _IMDS_IPV4_URL, headers)
        if address:
            try:
                ipaddress.ip_address(address)
            except ValueError:
                address = None
        _detected = (now + _DETECT_FAIL_TTL, address)
        return address


def _http(method: str, url: str, headers: dict[str, str]) -> str | None:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_IMDS_TIMEOUT) as response:
            return response.read(256).decode("ascii", "replace").strip() or None
    except (urllib.error.URLError, OSError, ValueError):
        # No metadata service, no route to link-local, a proxy in the way: all
        # of them mean the same thing here, and none of them is an error.
        return None


# ------------------------------------------------------- server settings I/O


def _current_values(conf: ServerConf, env: dict[str, str]) -> dict[str, str]:
    """Everything validate.py knows about, as it stands now."""
    values: dict[str, str] = {}
    for key in (*validate.AWG_PARAMS, *validate.SERVER_ONLY_PARAMS, *_CONF_NETWORK):
        # Address through _address_line, so a config carrying its IPv6 prefix on
        # a second Address line is compared and rewritten with that prefix still
        # in it. Reading the first line alone made _keep_prefix6 believe there
        # was no prefix to keep, and a settings save that touched the subnet
        # would then write a v4-only address over a dual-stack interface.
        found = (_address_line(conf) or None) if key == "Address" else conf.interface.get(key)
        if found is not None:
            values[key] = found
    for spec_key, env_key in _SPEC_TO_ENV.items():
        if spec_key != "MTU":  # MTU is the server's, read above; CLIENT_MTU tracks it
            values[spec_key] = env.get(env_key, "")
    for key in validate.HOOK_PARAMS:
        values[key] = "\n".join(conf.interface.all(key))
    return values


def _normalise(changes: dict, address: str = "") -> tuple[dict[str, str], dict[str, str]]:
    """Map incoming field names onto catalog keys, remembering where each came from.

    The second dict is catalog key -> the name the caller used, so a validation
    message lands on the field the admin actually typed in.

    `address` is the server's current address line, needed only by the subnet
    fields: they name an IPv4 network, and a dual-stack address line says more
    than they can.
    """
    proposed: dict[str, str] = {}
    origin: dict[str, str] = {}
    for name, raw in changes.items():
        if raw is None or name in _SUBNET_FIELDS:
            continue
        key = _FIELD_TO_SPEC.get(name, name)
        if key not in validate.PARAMS:
            continue  # not a setting; a caller may pass a whole object through
        proposed[key] = _flatten(raw)
        origin[key] = name

    for field_name in _SUBNET_FIELDS:
        raw = changes.get(field_name)
        if raw is None or not str(raw).strip():
            continue
        # Either spelling says the same thing: which network the tunnel is on.
        # The server's address is derived from it rather than asked for
        # separately, because the two disagreeing is a server nobody can route
        # through, and there is exactly one right answer for where the server
        # sits inside its own subnet.
        try:
            network = subnet.parse(str(raw))
        except ValueError as exc:
            raise ValidationError({field_name: str(exc)}) from exc
        derived = f"{subnet.server_ip(network)}/{network.prefixlen}"
        if "Address" in proposed:
            # What the two fields both describe is the IPv4 network, so that is
            # what has to agree. An address line carrying an IPv6 half is not
            # disagreeing with a subnet field that has no way of mentioning one.
            if subnet.parse_or_none(proposed["Address"]) != network:
                raise ValidationError(
                    {
                        field_name: (
                            f"This does not match the address {proposed['Address']}. Change one "
                            "of them, not both."
                        )
                    }
                )
            continue
        proposed["Address"] = _keep_prefix6(derived, address)
        origin.setdefault("Address", field_name)

    # An empty network value means "leave it alone": there is no such thing as a
    # server with no port or no address, and clearing one would be unrecoverable.
    for key in _CONF_NETWORK:
        if key in proposed and not proposed[key]:
            del proposed[key]
            origin.pop(key, None)
    return proposed, origin


def _flatten(value: object) -> str:
    """Hook lists arrive as lists; everything else as a scalar."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _masquerade_rewrite(lines: list[str], old: str, new: str) -> list[str]:
    """Point the NAT rules at the new tunnel network.

    install.sh writes `iptables -t nat -{A,D} POSTROUTING -s <cidr> -o <wan> -j
    MASQUERADE`, and that `-s` is what decides whose packets get translated on
    the way out. Move the subnet without moving it and every client gets an
    address that cannot reach the internet - the tunnel comes up, the handshake
    succeeds, and nothing routes.

    Only a `-s` whose value is the old network is touched, so an admin's own
    rules for other sources survive. A hook rewritten past recognition simply
    does not match, and _subnet_warnings says so rather than this guessing.
    """
    pattern = re.compile(rf"(-s[ \t]+|--source[ \t]+){re.escape(old)}(?=[ \t]|$)")
    return [pattern.sub(rf"\g<1>{new}", line) for line in lines]


def _keep_prefix6(address: str, current: str) -> str:
    """Put back the IPv6 half that a subnet field had no way of saying.

    `subnet_cidr` and `subnet_base` name an IPv4 network and nothing else, so an
    address line derived from one alone is an address line with no IPv6 in it.
    Writing that to a dual-stack server would take the tunnel's IPv6 off it as a
    side effect of moving its v4 pool, silently, leaving the ip6tables hooks and
    the clients.env mirror behind still describing a prefix the interface no
    longer holds.
    """
    for part in (piece.strip() for piece in current.split(",")):
        if part and subnet6.parse_or_none(part) is not None:
            return f"{address}, {part}"
    return address


def _refuse_moving_the_prefix6(before: str, after: str, origin: dict[str, str]) -> None:
    """The tunnel's IPv6 prefix is the installer's to set, and not an edit here.

    `subnet6Cidr` is read-only over the API, and the address line must not be a
    way around that. The prefix is written in four places - the interface
    address, the ip6tables hooks, the mirror in clients.env and the route on
    every peer - and the installer is the one thing that writes all four
    together. Moving it here would move one of them, and what that produces is a
    tunnel that hand-shakes and then carries no IPv6 at all, on clients still
    numbered out of a prefix the server has stopped answering for.

    Adding and removing are refused for the same reason and not out of caution:
    both need the hooks and the mode written or unwritten with the address.
    """
    old = subnet6.parse_or_none(before)
    new = subnet6.parse_or_none(after)
    if old == new:
        return
    if old is None:
        detail = (
            "This tunnel carries no IPv6, and giving it some is more than an address: the "
            "ip6tables hooks and the mode in clients.env have to be written with it. Re-run the "
            "installer, which writes them together."
        )
    elif new is None:
        detail = (
            f"This tunnel's IPv6 prefix is {subnet6.cidr(old)}. Dropping it from the address here "
            "would leave the ip6tables hooks, clients.env and every peer's route still saying it, "
            "so keep this half of the line; re-run the installer to take IPv6 off the tunnel."
        )
    else:
        detail = (
            f"This tunnel's IPv6 prefix is {subnet6.cidr(old)}, and the panel does not move it: "
            "the prefix is written in the ip6tables hooks, in clients.env and in every peer's "
            "route as well as here, and only the installer writes all of them together. Leave "
            "this half of the line alone and re-run the installer to move the prefix."
        )
    raise ValidationError({origin.get("Address", "address"): detail})


def _moved_scope(before: str, after: str) -> tuple[str, str] | None:
    """(old network, new network) when the tunnel moved, else None."""
    old = subnet.parse_or_none(before)
    new = subnet.parse_or_none(after)
    if old is None or new is None or old == new:
        return None
    return str(old), str(new)


def _retarget_nat(conf: ServerConf, before: str, after: str) -> list[str]:
    """Move the NAT rules with the subnet, or say why they could not be moved."""
    old = subnet.parse_or_none(before)
    new = subnet.parse_or_none(after)
    if old is None or new is None or old == new:
        return []

    moved = 0
    for key in ("PostUp", "PostDown"):
        lines = conf.interface.all(key)
        rewritten = _masquerade_rewrite(lines, str(old), str(new))
        if rewritten != lines:
            conf.interface.set_all(key, rewritten)
            moved += sum(1 for a, b in zip(lines, rewritten, strict=True) if a != b)

    if moved:
        return [
            f"The NAT rules in PostUp/PostDown were moved from {old} to {new}. They only run "
            "when the interface starts, so restart the tunnel before expecting traffic to "
            "leave the box."
        ]
    return [
        f"No PostUp/PostDown rule sources traffic from {old}, so nothing was moved to {new}. "
        "Check the MASQUERADE rule by hand: without one that matches the new subnet, clients "
        "will connect and hand-shake but reach nothing."
    ]


def _apply_to_conf(conf: ServerConf, proposed: dict[str, str], changed: set[str]) -> None:
    for key in (*_CONF_NETWORK, *validate.AWG_PARAMS, *validate.SERVER_ONLY_PARAMS):
        if key not in changed:
            continue
        value = proposed[key]
        if not value and key in (*validate.AWG_PARAMS, *validate.SERVER_ONLY_PARAMS):
            # Empty removes the line rather than writing it blank: awg-quick
            # reads an empty value as malformed and refuses the whole config.
            conf.interface.delete(key)
        else:
            conf.interface.set(key, value)
    for key in validate.HOOK_PARAMS:
        if key not in changed:
            continue
        # An admin who deletes one of the tunnel's own hooks here gets it back,
        # but that is _ensure_hooks' job and not this one's: every path out of
        # here goes through _write_conf, which calls it. Re-asserting the PreDown
        # a second time in this loop is how the PostUp came to be missing from
        # it - two places doing the same job, and only one of them updated.
        conf.interface.set_all(key, [line for line in proposed[key].splitlines() if line.strip()])


def _env_changes(
    proposed: dict[str, str], changed: set[str], env: dict[str, str], current_address: str = ""
) -> dict[str, str]:
    """The client-facing half of a settings save, in clients.env spelling."""
    out: dict[str, str] = {}
    for spec_key, env_key in _SPEC_TO_ENV.items():
        if spec_key in changed:
            out[env_key] = proposed[spec_key]
    if "Address" in changed:
        # The mirror of the server's address. Nothing allocates from it - that
        # is the Address itself - but _subnet falls back to it when the config
        # has no readable Address, and letting a stale value sit here is how
        # something later allocates out of the wrong network.
        network = subnet.parse_or_none(proposed["Address"])
        if network is not None:
            out["SUBNET_CIDR"] = str(network)
            # A split tunnel is spelled as the tunnel network, so moving the
            # network has to move it too. Left behind, every client issued from
            # then on routes the old range and cannot reach the server it was
            # just given an address on. Only an exact match is moved: anything
            # else is a route the admin chose, and not ours to rewrite.
            if "AllowedIPs" not in changed:
                before = subnet.parse_or_none(env.get("CLIENT_ALLOWED_IPS", ""))
                old_network = subnet.parse_or_none(current_address)
                if before is not None and old_network is not None and before == old_network:
                    out["CLIENT_ALLOWED_IPS"] = str(network)
            # Three octets cannot describe anything but a /24, so the old
            # spelling is emptied rather than left holding a half-truth about a
            # /16. Only touched when the file already carries it: a blank line
            # appended to a file that never had one is just noise.
            if env.get("SUBNET_BASE"):
                out["SUBNET_BASE"] = ""
    return out


def _subnet_warnings(merged: dict[str, str], conf: ServerConf) -> list[str]:
    network = subnet.parse_or_none(merged.get("Address", ""))
    if network is None:
        return []
    stranded = [
        peer.name or "(unnamed)"
        for peer in conf.peers
        if peer.public_key
        and not subnet.contains_host(network, peer.allowed_ips.split(",")[0].strip().split("/")[0])
    ]
    if not stranded:
        return []
    return [
        f"{len(stranded)} existing client(s) still have addresses outside {network} "
        f"({', '.join(stranded[:5])}{', ...' if len(stranded) > 5 else ''}). They will not route "
        "until they are removed and re-added."
    ]


# ------------------------------------------------------------------- demo


def _demo_conf_text() -> str:
    params = validate.randomize(mtu=DEMO_MTU)
    lines = [
        "[Interface]",
        f"Address = {DEMO_SERVER_IP}/20",
        f"ListenPort = {DEMO_PORT}",
        f"PrivateKey = {keys.genkey()}",
        f"MTU = {DEMO_MTU}",
        "",
    ]
    lines += [f"{key} = {params[key]}" for key in validate.AWG_PARAMS if params.get(key)]
    lines += [
        "",
        f"PreDown = {PREDOWN_HOOK}",
        "",
        # Both hooks, in install.sh's order, so the settings page in a demo shows
        # what it shows on a real server rather than one line short of it.
        f"PostUp = {POSTUP_HOOK}",
        f"PostUp = iptables -t nat -A POSTROUTING -s {DEMO_SUBNET} -o {DEMO_WAN} -j MASQUERADE",
        "PostUp = iptables -A FORWARD -i %i -j ACCEPT",
        "PostUp = iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        f"PostDown = iptables -t nat -D POSTROUTING -s {DEMO_SUBNET} -o {DEMO_WAN} -j MASQUERADE",
        "PostDown = iptables -D FORWARD -i %i -j ACCEPT",
        "PostDown = iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]
    return "\n".join(lines) + "\n"


def _demo_env_text() -> str:
    # Same layout install.sh writes, aligned comments included, so the settings
    # page looks the same in a demo as on a real server.
    return (
        f"{clientsenv.HEADER}"
        f'ENDPOINT_HOST="{DEMO_ENDPOINT}"     # blank = auto-detect\n'
        'ENDPOINT_PORT=""                # blank = ListenPort from the server config\n'
        'CLIENT_DNS="8.8.8.8, 8.8.4.4"\n'
        f'CLIENT_MTU="{DEMO_MTU}"\n'
        f'CLIENT_ALLOWED_IPS="0.0.0.0/0"  # "{DEMO_SUBNET}" for split tunnel\n'
        f'SUBNET_CIDR="{DEMO_SUBNET}"\n'
        'KEEPALIVE="25"\n'
    )


def _int(value: str | None) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return 0
