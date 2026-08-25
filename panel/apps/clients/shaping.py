"""Decide which clients have a ceiling, and keep the kernel holding exactly those.

awg.shaper is the mechanism and says so: it attaches the structure, gives one
address a ceiling, takes it away and reads back what is in force, and it
deliberately does not know what a client is or where a number came from. This is
the half it left out. Everything here is policy - which peers are eligible, what
the settings allow, when to attach at all, and what to do when the kernel and the
database disagree.

The numbers live in ClientMeta beside the quota, for the same reason the quota
does: a WireGuard config has nowhere to write a rate. What is *applied* is never
stored, because awg.shaper reads it back out of the kernel, and the difference
between the two is the entire point - a tunnel that came up before the panel did
holds no ceilings at all while the database still says what every client should
have. A stored "applied" flag would agree with the database and be wrong, which
is the one failure a reconcile pass cannot see.

Three callers, on three clocks, and all three go through the same two functions:

* a request that changed a client applies that one client, so an operator sees
  the effect while they are still looking at the screen;
* `manage shape`, from the PostUp hook, applies all of them, because tc rules do
  not survive the interface going down and a boot is the ordinary way to lose
  every one of them;
* the collector's reconcile pass compares what the kernel holds against what the
  database says and issues the difference, which is what catches a hook that did
  not fire, a config too old to carry one, and a hand-run `tc qdisc del`.

**Nothing is attached until something needs to be.** A WireGuard interface has no
qdisc at all by default - the kernel is told not to give it one - so putting an
HTB root on it replaces a lock-free path with a single-locked one for every
client on the server, shaped or not. A server where nobody has set a ceiling
therefore gets exactly the tunnel it had before this existed, and the structure
goes up when the first ceiling is set and comes down when the last one is
cleared.

**A peer that is switched off is not shaped.** Its key is not on the interface,
so it cannot send anything a class would meter, and a class it does not need is
around twelve kilobytes of kernel memory per direction - which is nothing for one
client and fifty megabytes for a server that has just expired three thousand.
Switching it back on is picked up by the request that did it, and by the
reconcile at the latest.

**Ceilings are clamped here rather than refused.** The API refuses a number above
what awg.shaper can hang under its root, which is where an operator can see the
message. By the time a pass is running, the same disagreement means something
else - a database restored from a machine running a later version, a row written
by hand - and raising on it would leave the reconcile failing on the same client
every minute forever, with nothing else in the pass getting done either.

**There is no link rate.** There was, and it was the switch: a capacity in
megabits, with zero meaning off. It bought nothing. HTB caps a class at its own
ceiling whatever its parent is rated at, so the figure decided only whether the
API would accept a client's number - and it could not be discovered, so it was a
guess at the server's own uplink that was wrong on most servers and, when the
guess was low, capped the entire tunnel. See awg.shaper.ROOT_BPS. What is left in
its place is a plain switch, and two numbers that are policy rather than
capacity: what a newly created client is given, and what the button on the
settings page writes over everybody with.
"""

import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from django.db.models import Q

from apps.panel import settings_store
from awg import paths, store, sysinfo
from awg import shaper as shaper_mod
from awg.errors import AwgError, ValidationError

from .models import ClientMeta

log = logging.getLogger(__name__)

# What the settings are said in. An operator quotes a limit in megabits and tc
# takes bits, and this is the one place the two meet.
MBIT = 1_000_000

# The most a client's own ceiling may be set to. It is awg.shaper's root rate,
# because a class cannot be given a ceiling the tree it hangs in cannot express -
# and the two being the same number is what keeps the API from accepting one the
# shaper then refuses.
MAX_BPS = shaper_mod.ROOT_BPS


@dataclass(frozen=True)
class Plan:
    """What this server is configured to shape, and with what.

    Read once per pass rather than per client: it is three settings and one
    parse of the server config, and every client in the pass is measured against
    the same copy of it.
    """

    # The tunnel, its networks and its MTU, straight out of the server config.
    network: ipaddress.IPv4Network
    network6: ipaddress.IPv6Network | None
    iface: str
    mtu: int
    # Whether the operator has switched bandwidth limits on at all.
    enabled: bool
    # The interface upload is shaped on, or "" when upload shaping is off.
    wan: str
    # Why this tunnel's own subnet cannot be shaped, or "" when it can.
    #
    # An HTB class id is sixteen bits and a client's is derived from its offset
    # into the subnet, so a network wide enough to push the last address past
    # that ceiling has no class to give it - awg.shaper.supports says so, and
    # refuses to build at all. The panel accepts anything from /16 to /30, so a
    # /16 tunnel is a perfectly ordinary server that this feature cannot serve.
    #
    # It is a property of the plan rather than something each caller re-derives
    # because the answer decides `on`: a server whose subnet cannot be shaped is
    # a server where shaping is off, not one where every pass raises.
    unsupported: str = ""

    @property
    def on(self) -> bool:
        """Whether anything should be shaped on this server at all."""
        return self.enabled and not self.unsupported

    def shaper(self) -> shaper_mod.Shaper:
        """The mechanism, configured from this plan. Only valid while `on`.

        Every caller checks that first, which is what makes the arguments below
        safe to pass through unexamined: a plan that is on has a subnet the
        shaper will accept, which is the only thing it can refuse to build for.

        Taking the structure *down* deliberately does not come through here. It
        is needed most in the case where this cannot be built - a subnet too wide
        to shape - so it goes to awg.shaper's own module functions, which ask for
        no subnet at all.
        """
        return shaper_mod.Shaper(
            self.network,
            iface=self.iface,
            wan=self.wan,
            network6=self.network6,
            mtu=self.mtu or shaper_mod.DEFAULT_MTU,
        )


def unavailable() -> str:
    """Why this host cannot shape anything at all, or "" when it can.

    Asked before anything else, and separate from whether shaping is switched
    on: a container without iproute2 and a developer's laptop under AWG_MOCK
    both have to be able to store a client's ceiling and say plainly that
    nothing is enforcing it, rather than failing the save that set it.
    """
    return shaper_mod.available()


def plan_from(scan: store.ServerScan) -> Plan:
    """The plan, given a scan of the server config the caller already has.

    Takes the scan rather than reading the config, because both of the callers
    that run over every client are holding one: the collector's reconcile pass
    reads it for the peer list, and re-parsing thirty thousand lines to learn
    the subnet again is most of what a pass would cost.
    """
    values = settings_store.all_settings()
    enabled = settings_store.get_bool("shaperOn")
    iface = paths.iface()
    wan = ""
    if enabled and settings_store.get_bool("shaperUpload"):
        wan = wan_iface(values)
        if not wan:
            log.warning(
                "upload shaping is on but no WAN interface is set and none holds the default "
                "route; upload ceilings cannot be applied"
            )
        elif wan == iface:
            # The tunnel is not a WAN, whatever /proc/net/route says. Shaping
            # both directions on one device would write each client's upload
            # rate over its own download class, and then read the one back as
            # the other - a pass that disagrees with itself and rewrites every
            # client on the server, every minute, for ever. A box whose default
            # route is its own tunnel is a chained VPN rather than a server, and
            # download shaping is the half that still means something on it.
            log.warning(
                "%s holds the default route, so it is the tunnel rather than a WAN; upload "
                "ceilings cannot be applied. Name the real uplink in the outgoing interface "
                "field, beside the upload switch on the Server page.",
                wan,
            )
            wan = ""
    network = _network(scan.subnet_cidr)
    return Plan(
        network=network,
        network6=_network6(scan.subnet6_cidr),
        iface=iface,
        mtu=scan.mtu,
        enabled=enabled,
        wan=wan,
        unsupported=shaper_mod.supports(network),
    )


def current_plan() -> Plan:
    """The plan, reading the server config for it. For a caller that has no scan."""
    return plan_from(store.scan_server())


def wan_iface(values: Mapping[str, str]) -> str:
    """Which interface upload shaping is - or was - applied on, for one set of settings.

    Named rather than inlined because the answer is needed about two different
    sets of them. A plan asks it of the settings as they stand; a save that
    switches upload shaping off, or moves it to another interface, has to ask it
    of the settings as they *were*, because that is the only record of where the
    structure it is about to orphan actually is. The kernel cannot be asked: an
    HTB root on a WAN carries no mark saying who put it there, and this module
    builds on one it finds rather than replacing it, so a root discovered there
    is as likely to be the operator's own.
    """
    return values.get("shaperWanIface", "").strip() or (sysinfo.default_iface() or "")


def wanted_from(
    peers: list[store.PeerScan], meta: dict[str, ClientMeta], plan: Plan
) -> dict[str, tuple[int, int]]:
    """Which addresses should be shaped and how fast: {address: (down, up)}.

    Built from the two things the caller already has rather than from a query of
    its own, so the collector's pass adds no database work at all.

    Only enabled peers with a number on them, only addresses inside the pool
    awg.shaper derives class ids from, and every rate clamped to the link.
    Everything else is skipped rather than reported: a peer outside the subnet
    is one an admin addressed by hand, and it has no place in the structure this
    builds.
    """
    if not plan.on:
        return {}
    found: dict[str, tuple[int, int]] = {}
    for peer in peers:
        row = meta.get(peer.public_key)
        if row is None or not peer.enabled or not peer.ip:
            continue
        down = min(max(row.down_bps, 0), MAX_BPS)
        up = min(max(row.up_bps, 0), MAX_BPS) if plan.wan else 0
        if not down and not up:
            continue
        try:
            shaper_mod.minor(plan.network, peer.ip)
        except ValidationError:
            log.debug("%s is not an address %s can shape; skipping it", peer.ip, plan.network)
            continue
        found[peer.ip] = (down, up)
    return found


def wanted() -> tuple[Plan, dict[str, tuple[int, int]]]:
    """The same, reading both sources. For `manage shape`, which holds neither."""
    scan = store.scan_server()
    plan = plan_from(scan)
    if not plan.on:
        return plan, {}
    keys = {peer.public_key for peer in scan.peers}
    rows = ClientMeta.objects.filter(public_key__in=keys).filter(
        Q(down_bps__gt=0) | Q(up_bps__gt=0)
    )
    return plan, wanted_from(scan.peers, {row.public_key: row for row in rows}, plan)


# --------------------------------------------------------------------- one client


def apply_client(address: str, down_bps: int, up_bps: int, *, enabled: bool = True) -> str:
    """Give one client its ceiling now, and say why it could not rather than raising.

    For the request that changed it. A save has already landed in the database by
    the time this runs, and the ceiling is re-asserted by the next reconcile
    whatever happens here, so a tc that failed is a line in the log and a note in
    the reply - not a 500 for an edit that was in fact stored.

    A client with no ceiling, or one that is switched off, is cleared instead:
    both are states in which it should hold no class, and clearing one that has
    none is quiet by design.
    """
    down = down_bps if enabled else 0
    up = up_bps if enabled else 0
    try:
        plan = current_plan()
    except AwgError as exc:
        return f"the server configuration could not be read: {exc}"
    return _apply_client(plan, address, down, up)


def clear_client(address: str) -> str:
    """Take one client out of the structure. For a removal, and for a bulk one."""
    return clear_clients([address])


def clear_clients(addresses: list[str]) -> str:
    """The same for a set of them, in one batch rather than one process each."""
    if not addresses:
        return ""
    reason = unavailable()
    if reason:
        return reason
    try:
        plan = current_plan()
        if not plan.on:
            # Nothing on this server holds a class, so there is nothing to take
            # one out of. A plan that is off has already had its structure
            # removed by whichever pass turned it off.
            return ""
        shaper = plan.shaper()
        with shaper.batched():
            for address in addresses:
                _quietly(shaper.clear_limit, address)
        _detach_if_idle(plan, shaper)
    except (AwgError, OSError) as exc:
        log.warning("could not clear the ceiling of %d client(s): %s", len(addresses), exc)
        return str(exc)
    return ""


def _apply_client(plan: Plan, address: str, down: int, up: int) -> str:
    reason = unavailable() or (plan.unsupported if (down or up) else "")
    if reason:
        return reason
    if not plan.on:
        # Nothing to apply and nothing to take away: a plan that is off has
        # already had its structure removed by whichever pass turned it off.
        return "" if not (down or up) else "bandwidth limits are switched off for this server."
    down = min(down, MAX_BPS)
    up = min(up, MAX_BPS) if plan.wan else 0
    try:
        shaper = plan.shaper()
        with shaper.batched():
            if down or up:
                # Only when something is missing. attach() is a dozen commands
                # and half of them are reads, which is a fair price at bring-up
                # and not one to pay on every edit. What decides that has to be
                # the whole structure and not the tunnel's root alone, which is
                # why attached() asks after every piece: the first upload ceiling
                # on a server already shaping download is exactly the case where
                # the tunnel's root is up and the two qdiscs that ceiling needs
                # are not.
                if not shaper.attached():
                    shaper.attach()
                shaper.set_limit(address, down, up)
            else:
                shaper.clear_limit(address)
        if not (down or up):
            _detach_if_idle(plan, shaper)
    except (AwgError, OSError) as exc:
        log.warning("could not apply the ceiling for %s: %s", address, exc)
        return str(exc)
    return ""


# ------------------------------------------------------------------- every client


def reconcile(plan: Plan, wanted: dict[str, tuple[int, int]], *, rebuild: bool = False) -> int:
    """Make the kernel hold exactly `wanted`, and say how many clients had to move.

    The difference and nothing more, which is what makes this affordable on the
    collector's clock. What is in force is read back out of the kernel - two
    commands, whatever the size of the server - and only the addresses whose
    numbers disagree are written. On a server where nothing has changed since the
    last pass, which is nearly every pass, that is two reads and a dictionary
    comparison.

    `rebuild` re-asserts every ceiling rather than the differing ones. The read
    back reports classes and their rates, which is the right question for "has
    somebody changed a number" and the wrong one for "is this ceiling actually
    being applied": a class at the correct rate with its filter missing reads as
    present and classifies nothing. So whenever the structure had to be built -
    which is the case where a filter can be missing - everything is written
    again. It is `replace` throughout, so re-asserting costs commands and changes
    nothing.
    """
    if unavailable():
        return 0
    if not plan.on or not wanted:
        # Either shaping was switched off, or the last ceiling was cleared, or
        # the tunnel has been moved to a subnet too wide to shape. The structure
        # stays on the interface until something takes it off, and this is the
        # pass that notices.
        #
        # Through the module functions rather than a Shaper, because the last of
        # those three is exactly a case where a Shaper cannot be built - and it
        # is the case where leaving the structure standing would be worst, since
        # every client in it is about to be numbered differently.
        if shaper_mod.attached_to(plan.iface):
            shaper_mod.detach_from(plan.iface, plan.wan)
            log.info(
                "nothing is shaped any more (%s); the tunnel's qdiscs have been removed",
                plan.unsupported or "no client has a limit",
            )
        return 0

    shaper = plan.shaper()
    # "Any piece of the structure is missing", not "the tunnel has no root": the
    # qdiscs the upload direction needs are two others, and a pass that read only
    # the tunnel's root walked past a server whose WAN had never been attached
    # because upload shaping was switched on after download shaping already was.
    fresh = not shaper.attached()
    # Two commands for the whole server, hoisted out of the comparison below on
    # purpose: asking per address would be one `tc class show` per client, which
    # is the cost this pass exists to avoid.
    downs = shaper.limits()
    ups = shaper.upload_limits() if plan.wan else {}
    live = {address: (down, ups.get(address, 0)) for address, down in downs.items()}
    live.update({address: (0, up) for address, up in ups.items() if address not in live})
    stale = [address for address in live if address not in wanted]
    moved = {
        address: pair
        for address, pair in wanted.items()
        if fresh or rebuild or live.get(address) != pair
    }
    if not fresh and not stale and not moved:
        return 0

    with shaper.batched():
        if fresh:
            shaper.attach()
        for address in stale:
            _quietly(shaper.clear_limit, address)
        for address, (down, up) in moved.items():
            _quietly(shaper.set_limit, address, down, up)

    if fresh:
        log.info("the shaping structure was not up; %d ceiling(s) re-applied", len(moved))
    elif stale or moved:
        log.info("%d ceiling(s) applied, %d removed", len(moved), len(stale))
    return len(moved) + len(stale)


def detach() -> str:
    """Take the whole structure off both interfaces, without asking first.

    For PostDown, and the one place that does not consult `attached()`. That
    check reads the tunnel's root qdisc, and by the time PostDown runs the tunnel
    device has already been deleted - so the answer is "nothing is attached"
    while the WAN's own root, which nothing deleted, is still there metering
    traffic for clients that are no longer connected to anything.

    Failures are the shaper's to swallow: removing what is not there is the
    normal case on either interface, and there is no state left to be wrong
    about afterwards.

    It does not build a Shaper either, and for the same reason it does not ask:
    a server whose subnet is too wide to shape cannot have one built, and it is
    a server whose structure most needs taking down.
    """
    reason = unavailable()
    if reason:
        return reason
    try:
        plan = current_plan()
    except AwgError as exc:
        log.warning("could not read the server configuration to remove the shaping: %s", exc)
        return str(exc)
    shaper_mod.detach_from(plan.iface, plan.wan)
    return ""


def detach_upload(wan: str) -> str:
    """Take the upload half off `wan`, and leave whatever is shaping download alone.

    For the settings save that switches upload shaping off or moves it to
    another interface. That save is the last moment anything knows where the
    upload structure is: `wan` comes from the settings as they were before it,
    and once they are written there is no way left to find out - the interface
    is not in the new settings, and an HTB root on a WAN says nothing about who
    put it there.

    Without this the switch does not switch anything off. The WAN keeps its
    class and `fw` filter per client and the tunnel keeps marking, so every
    client goes on being held to an upload ceiling the panel no longer shows and
    has no way to remove; and the HTB root stays on the interface this machine
    sends everything through, which is the cost the setting exists to make
    deliberate. The tunnel's own root is untouched, because download shaping is
    a separate switch and is very often still on.

    Which is also why the tunnel is refused as the interface to clear, the same
    way plan_from refuses it as one to shape. The two have to agree: what the
    plan would not build, this must not delete. On a box whose default route is
    its own tunnel the settings answer "where is the upload structure" with the
    tunnel's name, no upload structure was ever put there - and the root qdisc
    that is there is the download half, with every client's class hanging off it.
    """
    reason = unavailable()
    if reason:
        return reason
    iface = paths.iface()
    if wan == iface:
        wan = ""
    try:
        shaper_mod.detach_upload(iface, wan)
    except OSError as exc:  # pragma: no cover - the shaper swallows its own
        log.warning("could not remove the upload shaping from %s: %s", wan, exc)
        return str(exc)
    log.info(
        "the upload half of the shaping has been removed%s; the tunnel is no longer marking",
        f" from {wan}" if wan else "",
    )
    return ""


def _detach_if_idle(plan: Plan, shaper: shaper_mod.Shaper) -> None:
    """Take the structure off once the last ceiling has gone.

    Because an HTB root on a WireGuard interface is not free even when every
    class under it is empty: the kernel gives these interfaces no qdisc at all by
    default, so what this removes is a lock on the path of every packet to every
    client on the server.

    Asked of the database rather than of the kernel, and with the row that was
    just written already committed, so it is the question "does anybody still
    want a ceiling" rather than "is any class left".

    The kernel is still asked one thing, and only to keep the line below out of
    the log on a server that never had a structure. It is asked with the same
    all-or-nothing question every other caller uses, so a structure that is
    half there is left standing here - which is the reconcile pass's to take
    down, on the same branch that handles a server where the last ceiling went
    while nothing was watching.
    """
    if _any_ceiling() or not shaper.attached():
        return
    shaper.detach()
    log.info("the last bandwidth ceiling was cleared; the tunnel's qdiscs have been removed")


def _any_ceiling() -> bool:
    return ClientMeta.objects.filter(Q(down_bps__gt=0) | Q(up_bps__gt=0)).exists()


def _quietly(call, *args) -> None:
    """Run one client's change without letting it end the pass.

    A ceiling that will not apply is one client's problem, and a pass that raised
    on it would leave every client after it in the list unshaped as well. Inside
    a batch this can only be a rejected input, since the commands themselves are
    not run until the block ends.
    """
    try:
        call(*args)
    except (AwgError, OSError) as exc:
        log.warning("%s(%s) could not be applied: %s", call.__name__, args[0], exc)


# ------------------------------------------------------------------- the settings


def enabled() -> bool:
    """Whether bandwidth limits are switched on for this server.

    For the API, which has to know whether a limit it is being handed will do
    anything, and which must not read the server config to answer a question one
    setting already settles.
    """
    return settings_store.get_bool("shaperOn")


def default_limits() -> tuple[int, int]:
    """What a newly created client is given when the request names no limit.

    In bits per second, and (0, 0) - no limit - on every server that has not set
    one. The upload half is dropped when this server shapes no upload, because a
    number that cannot be enforced is not a default worth writing against a
    client's name.
    """
    if not enabled():
        return 0, 0
    down = _int(settings_store.get("shaperDefaultDownMbps")) * MBIT
    up = _int(settings_store.get("shaperDefaultUpMbps")) * MBIT if upload_possible() else 0
    return min(max(down, 0), MAX_BPS), min(max(up, 0), MAX_BPS)


def apply_to_all(down_bps: int, up_bps: int) -> tuple[int, str]:
    """Give every client the same limit, overwriting whatever each of them had.

    Returns how many rows were written and why the kernel could not be brought
    into line, which is "" when it was. The two are separate answers: the numbers
    are the panel's record and are stored whether or not anything is enforcing
    them, exactly as one client's are, so a host without iproute2 still saves and
    still says plainly that nothing is applying it.

    Destructive on purpose and by request only. Every hand-set limit on the
    server is gone afterwards and there is nothing to undo it with, which is why
    nothing calls this except the button that asks first.

    One write and one pass, not one of each per client: the update is a single
    statement, and reconcile sends its commands to `tc -batch` in one go. On a
    four thousand client server that is the difference between a second and the
    better part of an hour.
    """
    down = min(max(int(down_bps), 0), MAX_BPS)
    up = min(max(int(up_bps), 0), MAX_BPS)
    written = ClientMeta.objects.exclude(down_bps=down, up_bps=up).update(down_bps=down, up_bps=up)
    # Asked before the pass rather than left to it. reconcile() answers a host it
    # cannot shape by doing nothing and saying nothing, which is right for a
    # timer and wrong for a reply: without this, the one path that writes every
    # client on the server was also the one that reported "applied" on a box with
    # no iproute2, while setting the same limit on a single client said plainly
    # that nothing was enforcing it.
    reason = unavailable()
    if reason:
        return written, reason
    plan, want = wanted()
    try:
        reconcile(plan, want, rebuild=True)
    except (AwgError, OSError) as exc:
        log.warning("the new limit was stored but could not be applied: %s", exc)
        return written, str(exc)
    return written, ""


def subnet_problem() -> str:
    """Why this tunnel's own subnet cannot be shaped, or "" when it can.

    Separate from `enabled` because it costs a parse of the server config, and
    the two callers pay it only when it matters: a request that is actually
    setting a ceiling, and a settings save that is actually switching shaping
    on. Neither is on a path anything polls, and asking on every client create
    would put a full config parse in front of provisioning.

    An unreadable config answers "" rather than guessing. Whatever is wrong with
    it, a message about class id arithmetic is not the one to show.
    """
    try:
        return shaper_mod.supports(_network(store.scan_server().subnet_cidr))
    except AwgError as exc:
        log.debug("cannot read the server config to check the subnet: %s", exc)
        return ""


def upload_possible() -> bool:
    """Whether an upload ceiling has anywhere to be enforced on this server."""
    return enabled() and settings_store.get_bool("shaperUpload")


# ------------------------------------------------------------------------ parsing


def _network(cidr: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return ipaddress.IPv4Network("10.0.0.0/24")


def _network6(cidr: str) -> ipaddress.IPv6Network | None:
    if not cidr:
        return None
    try:
        return ipaddress.IPv6Network(cidr, strict=False)
    except ValueError:
        return None


def _int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
