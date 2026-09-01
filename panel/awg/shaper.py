"""Per-client bandwidth ceilings, enforced in the kernel, keyed off the tunnel IP.

AmneziaWG has no rate limiting of its own: the module encrypts and forwards, and
every byte it forwards moves as fast as the link allows. A ceiling therefore has
to come from the one place in Linux that can hold a packet back without leaving
kernel space - `tc`. This module is the whole of that mechanism and nothing
above it. It attaches the shaping structure to an interface, gives one client a
ceiling, takes it away again, and reads back what is actually in force. It does
not know what a client is, when a ceiling should change, or where the numbers
come from; that is the panel's, and is deliberately not here.

Nothing in it is stored. A client's place in the structure is *derived* from its
tunnel address, and what is currently applied is *read back from the kernel*, so
there is no table to keep in step with the config and no state that can be stale
after a restart, a restore or a hand edit. It is the same bargain subnet.py
makes with addresses, for the same reason: two records of one fact eventually
disagree, and the one that disagrees silently is the one that hurts.

Both directions are shaped, and they are shaped in different places, because a
client's upload is not visible where its download is:

* **Download** (server to client) is plaintext on the tunnel's egress, still
  carrying the client's address as its destination. An HTB class per client and
  a `u32` filter on that address is the whole of it.

* **Upload** (client to the internet) has left the tunnel and been through
  MASQUERADE by the time it reaches the WAN's egress, so its source address is
  the server's and there is nothing left to classify on. It is marked instead,
  on the tunnel's *ingress* where the address is still the client's, and the
  mark is what the WAN's egress filters on - `skb->mark` survives routing and
  NAT. The obvious alternative, redirecting ingress to an IFB device, is not
  used: IFB is single-queue and is exactly the bottleneck this is meant to
  avoid.

The lookup on both sides is a `u32` hash table keyed on the address's last
octet, not a flat list of filters. A flat list is checked linearly for every
packet, which is fine for a demo and indefensible on a server whose subnet holds
four thousand clients; the hash makes it a bucket lookup and a handful of
compares regardless of how many clients exist.

**Both address families are shaped, into the same class.** A ceiling that only
counted IPv4 would not be a ceiling on this project: every client here is handed
a v6 address derived from the very offset its class id comes from, and in the
`native` and `nat` modes it carries real v6 traffic. Shaping one family and not
the other would leave a "10 Mbit client" with 10 Mbit of IPv4 and an unmetered
v6 path beside it - and Happy Eyeballs means most of their traffic would take
the unmetered one without them ever choosing to. subnet6.py makes the same
argument about routing ("Routing IPv4 while the client has working IPv6 does not
fail - it leaks"), and it is just as true of shaping. So each family gets its
own hash table at its own priority, and both point at the one HTB class, which
is what makes the ceiling a total across both rather than one each.

A server with IPv6 genuinely off passes no v6 network and is shaped on v4 alone.
That is the only case in which half of this is skipped, and it is the caller's
statement rather than a guess made here: this module cannot see which `--ipv6`
mode the server was installed in, so a caller that has one and does not pass it
gets exactly the leak described above.

**This module owns `skb->mark` for forwarded tunnel traffic.** The marking
action sets the whole word rather than a masked field, because `skbedit`'s mask
form is not in every iproute2 this project runs on. Nothing else here marks
those packets - install.sh writes `nat` and `filter` rules only, never `mangle` -
so the assumption holds today, and it is written down because it is the thing
that would break if it ever stopped holding.

**Anything that touches more than one client goes through `batched()`.** One
ceiling is four to nine `tc` commands, and each of those is a fork, an exec and
a netlink round trip - about three milliseconds, which is nothing for the one
client an admin just edited. It is not nothing for every client on the server:
four thousand of them is upwards of thirty thousand forks and the better part of
two minutes, and the place that has to do exactly that is the PostUp hook, where
the two minutes are spent with `awg-quick up` blocked and the tunnel still down.
`tc -batch` reads the same commands from stdin in one process over one netlink
socket, which takes each one from milliseconds to tens of microseconds and turns
that two minutes into a second or so. So the per-client entry points below are
unchanged and still run immediately; wrapping a loop of them in `batched()`
collects what they would have run and sends it once.
"""

import contextlib
import ipaddress
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from . import subnet as subnet_mod
from . import subnet6 as subnet6_mod
from .controller import mock_enabled
from .errors import ToolError, ValidationError

log = logging.getLogger(__name__)

TC = "tc"

# tc is a netlink round trip, not a daemon. Anything that has not answered in
# this long has not merely been slow, and a caller holding the config lock must
# not wait on it.
TIMEOUT = 10

# A whole batch's ceiling, and it cannot be the single-command one: giving four
# thousand clients a ceiling is around thirty thousand commands, and even at
# netlink speed that is seconds rather than milliseconds. Scaled by how much was
# asked for, so a batch of nine still fails fast, and capped so that a `tc` that
# has wedged cannot hold a bring-up open indefinitely.
BATCH_TIMEOUT = 120
BATCH_PER_TIMEOUT = 200

# The root qdisc's handle, and the two classes every attached interface carries
# whether or not a single client has a ceiling: 1:1 is the link itself and 1:2
# takes everything unclassified. HTB's `default` points at the minor, so traffic
# for a client with no ceiling is not stalled looking for a class that is not
# there - it lands in 1:2 and is limited only by the link.
ROOT = "1:"
ROOT_MAJOR = 1
ROOT_CLASS = "1:1"
DEFAULT_CLASS_ID = 2

# Client minors start above the two reserved ones. 0x10 rather than 3 to leave
# room for anything else this structure grows, and because a minor that is
# obviously not a client's is easier to recognise in `tc class show`.
CLASS_BASE = 0x10
MAX_MINOR = 0xFFFF

HASH_BUCKETS = 256

# The ingress qdisc's handle is fixed by the kernel.
INGRESS = "ffff:"

# u32 node ids are 12 bits and the kernel hands out its own from 0x800 up.
# Starting there keeps a derived id clear of a hand-added filter's, and leaves
# the low half of the space free for one.
NODE_BASE = 0x800

# Bytes, and only a default: the leaf qdiscs want a quantum no smaller than a
# packet, and the tunnel's real MTU is what the caller should pass.
#
# Not validate.DEFAULT_MTU, which shares the name and is a different number and
# a different job - that one is the MTU a new server is built with, drawn
# against the per-packet budget. This is a floor under a tc quantum, so it is
# only ever wrong by being too small, and one above the tunnel default is the
# safe direction to be wrong in. It does not move when that one does.
DEFAULT_MTU = 1400

# What the root and default classes are rated at, and it is deliberately a
# constant rather than the interface's real capacity.
#
# It used to be asked for, and the number was worth almost nothing. HTB caps a
# class at its own `ceil` regardless of what its parent is rated at, so a client
# limited to 10 Mbit is limited to 10 Mbit whatever sits above it; the root's
# rate only decides how much the *unclassified* traffic in the default class may
# take, and the answer there is always "all of it". So the only thing the true
# capacity ever bought was an error message when somebody set a client's ceiling
# above it - and a ceiling above what the link can deliver is not a fault, it is
# simply one that never gets reached.
#
# Meanwhile it could not be discovered and had to be typed: a virtio NIC reports
# 10 Gbit while sitting behind a 200 Mbit allowance, so the setting was an
# operator's guess at their own uplink, wrong on most servers, and a number that
# is wrong in the low direction caps the whole tunnel. A constant above any link
# this will run on cannot make that mistake.
#
# 100 Gbit rather than something larger because tc derives a burst from the rate
# and the arithmetic stays sane here, and because a root that no real uplink
# reaches is all this has to be.
ROOT_BPS = 100_000_000_000

# The root class carries every client's traffic and is never a leaf, so its
# quantum decides nothing - but HTB derives one from the rate when none is given,
# clamps it, and says so on every attach: "sch_htb: quantum of class 10001 is
# big. Consider r2q change." At the rate above it would say it loudly. Stating
# one silences a warning about a number that does not matter.
ROOT_QUANTUM = 200_000

# A rate-limited class needs an AQM in front of it or it is a bufferbloat
# generator: HTB's own default leaf is a pfifo sized for an unshaped link, so a
# client capped at 5 Mbit would sit behind a queue measured in seconds. fq_codel
# also keeps one bulk transfer from adding delay to that same client's
# interactive traffic. The flow count is cut well below fq_codel's default
# because this is one client's share, not a whole interface's, and there may be
# hundreds of them.
CLIENT_QDISC = ("fq_codel", "limit", "1024", "flows", "128")

# The default class is the opposite case and takes fq_codel's own defaults: on
# the tunnel it carries every client without a ceiling, and on the WAN it
# carries the whole host. Cutting its flow count the way a client's is cut would
# be sizing the busiest class on the box for one client's share of traffic.
DEFAULT_QDISC = ("fq_codel",)


@dataclass(frozen=True)
class _Family:
    """One address family's place in the filter tree.

    The two families cannot share a filter chain - `protocol ip` and
    `protocol ipv6` are separate tcf_proto instances, each with its own hash
    table space - so each gets its own priority and its own table numbers. They
    converge again at the class: both `flowid` into the same minor, which is
    what makes a client's ceiling a total across both families rather than one
    allowance each.
    """

    proto: str  # what `protocol` is spelled as
    match: str  # what `match` is spelled as, which is not the same word
    prio: int
    src_at: int  # byte offset of the last four bytes of the source address
    dst_at: int  # ...and of the destination address
    down_ht: int
    mark_ht: int


# IPv4 puts its addresses at bytes 12 and 16. IPv6 puts its 16-byte addresses at
# 8 and 24, and the octet the hash keys on is the last of them - so the word to
# read is the final four bytes of each, at 20 and 36.
V4 = _Family("ip", "ip", 1, 12, 16, 0x10, 0x20)
V6 = _Family("ipv6", "ip6", 2, 20, 36, 0x11, 0x21)

# Kept as names because the check script and the tests reach for them.
DOWN_HT = V4.down_ht
MARK_HT = V4.mark_ht
SRC_AT = V4.src_at
DST_AT = V4.dst_at


class ShaperError(ToolError):
    """A tc command that was asked to change something did not."""


# `tc -batch` splits each line on whitespace and reads quotes and backslashes of
# its own, so a token carrying any of them would arrive as two arguments or as
# something else entirely. Nothing this module builds contains one - the rates
# are digits, the handles are hex, the addresses are addresses - and the only
# value that comes from outside is an interface name. Checked rather than
# escaped, because an interface name with a space in it is a mistake worth
# reporting and never a thing to accommodate.
_UNSAFE_TOKEN = re.compile(r"""[\s'"\\]""")


@dataclass
class _Queue:
    """The commands a batch has collected, in the order they were asked for.

    One list, and the order is the caller's own, because tc commands are not
    independent of each other and there is no rearrangement of them that is safe
    in general. A hash table has to exist before the filter that links to it and
    the qdisc it hangs off has to exist before either; a leaf qdisc has to be
    gone before the one replacing it is added; a class must outlive the filter
    aiming at it. Every one of those orderings is already decided, correctly, by
    the code that queues the commands - so keeping that order is the whole of
    what this has to do, and sending the batch is not the place to have opinions
    about it.

    It was two lists once, split by whether failing a command mattered, with the
    ones that could fail sent first. That worked as long as "can fail" meant
    "takes something away", and it stopped being true the moment something a
    build depended on was allowed to fail: creating a u32 hash table is refused
    when the table is already there, which is a normal outcome and not worth
    reporting, but hoisting it into the first half sent it before the root qdisc
    it hangs off existed - so it failed for real, the linking filter that needed
    it failed after it, and a batch that attached and then shaped anybody could
    not work at all.

    What is kept is the distinction that split was for: `check` still records
    whether anyone asked for this command to succeed, and _send uses it to
    decide what a failure was worth rather than when to run it.
    """

    items: list[tuple[list[str], bool]] = field(default_factory=list)

    def add(self, cmd: list[str], check: bool) -> None:
        self.items.append((cmd, check))


def supports(network: ipaddress.IPv4Network) -> str:
    """Why this subnet cannot be shaped, or "" when it can.

    An HTB minor is sixteen bits and a client's is derived from its offset into
    the subnet, so a network large enough to push the last address past that
    ceiling cannot be given one address per class. Every prefix from /30 to /17
    is comfortably inside it; a /16 overruns it by its last handful of
    addresses, which is reported here rather than discovered as a failing tc
    command on whichever client happened to be allocated one of them.
    """
    highest = subnet_mod.last_host(network) - int(network.network_address)
    if highest + CLASS_BASE > MAX_MINOR:
        roomy = subnet_mod.capacity(ipaddress.IPv4Network("0.0.0.0/17"))
        return (
            f"a /{network.prefixlen} is too large to shape: its last address needs class "
            f"{highest + CLASS_BASE:#x} and the kernel's ceiling is {MAX_MINOR:#x}. Use /17 or "
            f"narrower, which still holds {roomy} clients."
        )
    return ""


def available() -> str:
    """Why shaping cannot run on this host, or "" when it can.

    Read-only and cheap enough to call on any path that wants to explain itself.
    A missing `tc` is the common answer on a container image built without
    iproute2; AWG_MOCK is the developer's laptop, where rewriting the machine's
    own qdiscs because a demo config changed would be as indefensible as
    firewall.py rewriting their ufw.
    """
    if mock_enabled():
        return "shaping is disabled under AWG_MOCK."
    if not shutil.which(TC):
        return "tc is not installed; the iproute2 package provides it."
    return ""


def attached_to(iface: str) -> bool:
    """Whether this interface already carries our root qdisc.

    A plain function rather than a method, because the question is about a
    network device and nothing else. A caller whose subnet is one this module
    refuses to shape - a /16, say - still has to be able to ask it, and cannot
    construct a Shaper to do so.
    """
    return "htb 1:" in (_capture([TC, "qdisc", "show", "dev", iface, "root"]) or "")


def detach_from(iface: str, wan: str = "") -> None:
    """Take the whole structure off, both interfaces. Also not about the subnet.

    Every client class and filter goes with the root qdisc, so they are not
    removed one by one. Failures are swallowed: detaching what is not there is
    the normal case on a half-attached interface, and there is no state left to
    be wrong about afterwards.

    Deliberately reachable without a Shaper. Tearing down is what a caller needs
    most in exactly the case where a Shaper cannot be built - a subnet too wide
    to shape - and requiring one would leave the structure standing precisely
    when it should come off.
    """
    _quiet([TC, "qdisc", "del", "dev", iface, "root"])
    detach_upload(iface, wan)


def detach_upload(iface: str, wan: str) -> None:
    """Take only the upload half off, and leave the download direction shaping.

    The two halves of this structure can be switched independently - upload
    shaping is its own setting, because it is the half that costs something on
    the host's own egress - so turning it off has to be able to remove its half
    without disturbing the ceilings still being applied on the tunnel.

    The tunnel's ingress qdisc is what makes it enough. Every upload ceiling is
    enforced by a mark set there and filtered on at the other end, so a WAN class
    with nothing marked for it meters no traffic at all: the packets fall through
    to the default class, which is rated at ROOT_BPS and constrains nothing. That
    matters because the WAN's root can be an operator's own - attach() builds on
    one it finds rather than replacing it - so this removes it only when the
    caller names the interface, and the caller is expected to name one only where
    the panel put the structure there itself.
    """
    _quiet([TC, "qdisc", "del", "dev", iface, "ingress"])
    if wan:
        _quiet([TC, "qdisc", "del", "dev", wan, "root"])


def minor(network: ipaddress.IPv4Network, address: str) -> int:
    """The HTB minor for a client address. Stable, derived, never allocated.

    The offset into the subnet is already unique per client and already stable
    for as long as the client keeps its address, which is the whole life of the
    client - so it is the id, shifted clear of the reserved minors. Nothing has
    to be written down, and a shaper that has just restarted derives exactly the
    ids the one before it used.
    """
    value = subnet_mod.as_int(address)
    if value is None:
        raise ValidationError(f"'{address}' is not an IPv4 address.")
    if not subnet_mod.contains_host(network, address):
        raise ValidationError(f"{address} is not a client address inside {network}.")
    result = value - int(network.network_address) + CLASS_BASE
    if result > MAX_MINOR:
        raise ValidationError(supports(network) or f"{address} cannot be given a class.")
    return result


def handle(network: ipaddress.IPv4Network, address: str, table: int) -> str:
    """The u32 filter handle for a client's IPv4 filter, in `table`.

    Split the same way the hash is, and that is the whole of the care needed
    here. The bucket has to be the address's *own* last octet, because the
    linking filter hands the kernel `hashkey mask 0x000000ff`, and what the
    kernel reads under that mask is the last byte of the address on the wire -
    not the client's offset into the pool.

    The two are the same number whenever the network address ends in .0, which
    every prefix of /24 and wider does by construction, and that is what made
    bucketing on the offset look right. From /25 to /30 - all of which
    subnet.py accepts - a network can start at .128 or .192, and then the two
    part company: a filter written into the offset's bucket sits somewhere the
    kernel will never look, so the packets fall through to the default class
    and the ceiling silently is not there. Nothing reports it, because every
    command succeeded.

    What is left of the offset separates the clients sharing a bucket. Between
    them the pair is unique in either case: below /24 the network is .0-aligned
    so bucket and node recover the offset exactly, and at /25 and narrower the
    pool is under 256 addresses and cannot cross a 256 boundary, so no two
    clients share a last octet at all.
    """
    offset = minor(network, address) - CLASS_BASE  # validates the address first
    return _slot(table, int(ipaddress.IPv4Address(address.strip())) & 0xFF, offset)


def handle6(offset: int, table: int) -> str:
    """The same, for a client's IPv6 filter, which is keyed off the offset itself.

    A client's v6 address is the tunnel prefix plus its offset in the IPv4 pool
    - subnet6.host_addr puts it there and offset_of reads it back - so the
    address's last octet simply *is* the offset's last byte, with no network
    base to add. The v4 subtlety above has no counterpart: the v6 network is a
    /64 and its host part always starts at zero.
    """
    return _slot(table, offset & 0xFF, offset)


def _slot(table: int, bucket: int, offset: int) -> str:
    return f"{table:x}:{bucket & 0xFF:x}:{NODE_BASE + (offset >> 8):x}"


# --------------------------------------------------------------------- shaper


class Shaper:
    """The tc structure on one tunnel interface, and optionally one WAN interface.

    Constructed per operation rather than kept around: it holds configuration,
    not state, and everything it reports it reads back from the kernel when
    asked. `wan` is what makes upload ceilings possible; without it only the
    download direction is shaped, which is a legitimate way to run - it leaves
    the host's own egress qdisc alone, and download is the direction an operator
    usually means.

    There is no link rate to give. Every class hangs under a root rated at
    ROOT_BPS, which is above any link this runs on and therefore constrains
    nothing; a client is held to its own ceiling, which is what a ceiling means.
    See that constant for why the interface's real capacity turned out to be a
    number nobody could supply and nothing needed.

    **Shaping the WAN shapes the whole host.** An HTB root there is on the path
    of every packet the machine sends, not only the tunnel's - the panel's own
    replies and the operator's SSH session included - and on a multiqueue NIC it
    replaces `mq` with a single-locked root, which costs throughput a busy server
    will notice. That is the price of capping upload at all, since the client's
    address has been translated away by the time its traffic gets there, and it
    is why `wan` is optional rather than assumed.

    `network6` is the tunnel's IPv6 /64, and leaving it out means v6 traffic is
    not shaped - see the note at the top of this module about what that costs on
    a dual-stack server. A caller that has one should pass it.
    """

    def __init__(
        self,
        network: ipaddress.IPv4Network,
        *,
        iface: str,
        wan: str = "",
        network6: ipaddress.IPv6Network | None = None,
        mtu: int = DEFAULT_MTU,
    ) -> None:
        reason = supports(network)
        if reason:
            raise ValidationError(reason)
        self.network = network
        self.network6 = network6
        self.iface = iface
        self.wan = wan
        self.mtu = max(int(mtu), 68)
        # What batched() is collecting, or None when every command goes straight
        # to its own tc. Held per shaper rather than globally because a shaper is
        # built per operation and never shared across threads.
        self._queue: _Queue | None = None

    @property
    def families(self) -> tuple[_Family, ...]:
        """The families this shaper covers - v6 only when it was given a network."""
        return (V4, V6) if self.network6 is not None else (V4,)

    # ----------------------------------------------------------- the structure

    def attach(self) -> None:
        """Put the root structure in place. Safe to run again; safe to run at boot.

        Every piece is checked for and built if it is missing, rather than the
        whole thing being skipped on the strength of the root qdisc alone. That
        distinction is the difference between recovering and wedging: attach()
        exists to be re-run from the PostUp hook, tc rules do not survive the
        interface going down, and a run that got the root up and then failed -
        a missing module, a tc killed mid-way - would otherwise be a state every
        later attach walked straight past, leaving the hash tables absent and
        every set_limit failing on a bare RTNETLINK error.

        What is never rebuilt is a root qdisc that is already ours: re-adding it
        would take every client class with it.

        An HTB root that is *not* ours is indistinguishable from one that is -
        HTB is HTB, and nothing in it records who put it there - so one found on
        the WAN is built upon rather than replaced, and its 1:1 and 1:2 are
        re-stated to this shaper's numbers. On the tunnel the question does not
        arise, since this project created the interface. On a WAN an operator
        was already shaping by hand, it does: they should pass no `wan` and cap
        download only.
        """
        self._require_available()
        self._attach_shaping(self.iface)
        self._attach_marking()
        if self.wan:
            self._attach_shaping(self.wan)

    def attached(self) -> bool:
        """Whether every piece attach() builds is already there.

        The question every caller asks with this is "is the structure up", and
        the answer decides whether attach() runs at all - so it has to be asked
        of the whole structure and not of the one piece that is easiest to read.

        It used to be the tunnel's root qdisc alone, and that is a different
        question on any server that shapes upload. The upload half lives on two
        other qdiscs - an ingress on the tunnel to mark with, an HTB root on the
        WAN to filter on - and neither exists until attach() puts it there. A
        server already shaping download has the tunnel's root, so switching
        upload on, or setting the first upload ceiling, found "attached" true and
        skipped the build: `tc` was then asked for a class under a root that was
        not on the WAN and a filter under an ingress that was not on the tunnel,
        both refused, and the ceiling the panel went on displaying was enforced
        by nothing until the next bring-up - which is the one path where the
        tunnel's root is missing and attach() therefore ran.

        The hash tables are asked after for the same reason, one step further in.
        attach() is careful to rebuild a piece that went missing rather than walk
        past it, precisely so that a run which died half way can be repaired; a
        caller that decides whether to call it from the root qdisc alone puts
        that state straight back, and a chain whose table is gone takes every
        filter written into it with no error anywhere.

        Cheap enough to keep asking. It is a read per piece, short-circuited, so
        a server shaping download only pays two or three and one shaping upload
        pays at most eight - once per edit, and once per reconcile pass.
        """
        if not self._has_root(self.iface):
            return False
        if not all(
            self._has_link(self.iface, ROOT, family, family.down_ht) for family in self.families
        ):
            return False
        if not self.wan:
            return True
        if not self._has_ingress() or not self._has_root(self.wan):
            return False
        return all(
            self._has_link(self.iface, INGRESS, family, family.mark_ht) for family in self.families
        )

    def detach(self) -> None:
        """Take the whole structure off again, both interfaces."""
        detach_from(self.iface, self.wan)

    # -------------------------------------------------------------- one client

    def set_limit(self, address: str, down_bps: int = 0, up_bps: int = 0) -> None:
        """Give one client a ceiling in each direction. 0 means unlimited.

        The two directions are independent: a client may be capped downward and
        left alone upward, and asking for neither is the same as clearing it.
        The class is created before the filter that points at it, in both
        directions, because the reverse order is a window in which packets are
        classified into a class that does not exist yet.

        Both address families are filtered into that one class, so the ceiling
        is what the client gets in total and not what it gets twice over.
        """
        self._require_available()
        down_bps, up_bps = self._check_rate(down_bps, "down"), self._check_rate(up_bps, "up")
        if up_bps and not self.wan:
            # Quietly dropping this would be the failure this module keeps
            # promising not to have: a ceiling the caller was told nothing about
            # and the kernel was never asked for.
            raise ValidationError(
                {"up": "an upload ceiling needs a WAN interface to shape it on; none was given."}
            )
        if not down_bps and not up_bps:
            self.clear_limit(address)
            return

        client = minor(self.network, address)
        if down_bps:
            self._add_class(self.iface, client, down_bps)
            for family, target, slot in self._targets(address, "down"):
                self._run(
                    [TC, "filter", "replace", "dev", self.iface, "parent", ROOT]
                    + ["protocol", family.proto, "prio", str(family.prio)]
                    + ["handle", slot, "u32", "ht", _bucket(slot)]
                    + ["match", family.match, "dst", target]
                    + ["flowid", f"1:{client:x}"]
                )
        else:
            self._drop_down(address, client)

        if up_bps:
            self._mark(address)
            self._add_class(self.wan, client, up_bps)
            # `protocol all` rather than one filter per family: this matches on
            # the mark, which the ingress side has already set from whichever
            # family the packet arrived in, so the address type no longer says
            # anything the classifier needs.
            self._run(
                [TC, "filter", "replace", "dev", self.wan, "parent", ROOT]
                + ["protocol", "all", "prio", "1", "handle", str(client), "fw"]
                + ["flowid", f"1:{client:x}"]
            )
        else:
            self._drop_up(address, client)

    def clear_limit(self, address: str) -> None:
        """Return one client to the unshaped default class, in both directions."""
        self._require_available()
        client = minor(self.network, address)
        self._drop_down(address, client)
        self._drop_up(address, client)

    def limits(self) -> dict[str, int]:
        """What the tunnel is actually enforcing: client address to download bits/sec.

        Read out of the kernel, not out of anything this process remembers, so
        it is the answer to "what is in force" rather than "what was asked for" -
        which are different questions after a bring-up that wiped the qdiscs, and
        telling them apart is the entire job of a reconcile pass.
        """
        return self._read_classes(self.iface)

    def upload_limits(self) -> dict[str, int]:
        """The same, for the upload direction. Empty when no WAN was given."""
        return self._read_classes(self.wan) if self.wan else {}

    # ------------------------------------------------------------- internals

    def _targets(self, address: str, side: str) -> list[tuple[_Family, str, str]]:
        """Per family: what to match on, and where that filter goes.

        One place decides both, because they have to agree - a filter written
        into a bucket the hash never reaches is invisible and silent, and the two
        halves being derived side by side is what keeps that from drifting apart
        again.
        """
        offset = minor(self.network, address) - CLASS_BASE
        table = "down_ht" if side == "down" else "mark_ht"
        found: list[tuple[_Family, str, str]] = [
            (V4, f"{address}/32", handle(self.network, address, getattr(V4, table)))
        ]
        if self.network6 is not None:
            found.append(
                (
                    V6,
                    subnet6_mod.host_cidr(self.network6, offset),
                    handle6(offset, getattr(V6, table)),
                )
            )
        return found

    def _attach_shaping(self, dev: str) -> None:
        """Root HTB, the link class, the default class and the egress hashes.

        Each step asks whether it is already there. `replace` on the root would
        be shorter and would wipe every client class on an interface that was
        already set up, so the check guards that one; the classes below it use
        `replace` because re-asserting them costs nothing and repairs a class
        somebody edited by hand.

        Both classes are rated at ROOT_BPS, which is to say at nothing: the root
        is there to hang clients under, and the default class carries everything
        that has no ceiling and must not be slowed for having none.
        """
        rate = f"{ROOT_BPS}bit"
        if not self._has_root(dev):
            # replace, not add: the root here is whatever the kernel installed by
            # default - fq or pfifo_fast - and `add` on a device that already has
            # one is refused. Guarded above, so this can never land on ours.
            self._run(
                [TC, "qdisc", "replace", "dev", dev, "root", "handle", ROOT]
                + ["htb", "default", f"{DEFAULT_CLASS_ID:x}"]
            )
        self._run(
            [TC, "class", "replace", "dev", dev, "parent", ROOT, "classid", ROOT_CLASS]
            + ["htb", "rate", rate, "ceil", rate, "quantum", str(ROOT_QUANTUM)]
        )
        self._run(
            [TC, "class", "replace", "dev", dev, "parent", ROOT_CLASS]
            + ["classid", f"1:{DEFAULT_CLASS_ID:x}"]
            + ["htb", "rate", rate, "ceil", rate, "quantum", str(self.mtu)]
        )
        self._run(
            [TC, "qdisc", "replace", "dev", dev, "parent", f"1:{DEFAULT_CLASS_ID:x}"]
            + ["handle", f"{DEFAULT_CLASS_ID:x}:", *DEFAULT_QDISC]
        )
        if dev == self.iface:
            for family in self.families:
                self._attach_hash(dev, ROOT, family, family.down_ht, "dst", family.dst_at)

    def _attach_marking(self) -> None:
        """The ingress qdisc and the hashes that mark a client's upload.

        Only worth putting up when there is a WAN to shape at the other end: a
        mark nothing filters on is per-packet work for nothing.
        """
        if not self.wan:
            return
        if not self._has_ingress():
            self._run([TC, "qdisc", "add", "dev", self.iface, "handle", INGRESS, "ingress"])
        for family in self.families:
            self._attach_hash(self.iface, INGRESS, family, family.mark_ht, "src", family.src_at)

    def _attach_hash(
        self, dev: str, parent: str, family: _Family, table: int, side: str, at: int
    ) -> None:
        """One 256-bucket hash table, plus the filter that routes traffic into it.

        The linking filter is what makes this a hash lookup instead of a list
        walk: it matches the tunnel subnet once, takes the address's last octet
        as the key and jumps straight to the bucket, so a packet is compared
        against the handful of clients sharing that octet and no others.

        **The linking filter names no hash table of its own.** It used to say
        `ht 800::`, meaning "put this in the root table", and that is true only
        of the first u32 classifier on a parent. The kernel numbers those roots
        as it allocates them - 800: for the first, 801: for the second - so on a
        dual-stack tunnel the v6 filter, which is the second, was placed by id
        into the *v4* chain. Every command succeeded, `tc filter show` showed a
        filter carrying the v6 prefix and the v6 hash offset, and no IPv6 packet
        ever reached it, because a `protocol ip` classifier is not given any: the
        v6 chain held two empty tables and every v6 client ran unshaped, which is
        precisely the leak the note at the top of this module is about. Naming no
        table puts the filter in whichever root belongs to the protocol and
        priority it was actually created under, which is what was meant.

        What decides whether to build is the linking filter rather than the
        table, and that is not a detail either. An explicitly created hash table
        belongs to the u32 block shared by every classifier on the parent, not to
        one classifier - so deleting the v4 chain leaves table 10: allocated and
        invisible, and the next attach was told it was missing, tried to create
        it, and failed with "Filter already exists" for good. Asking after the
        filter instead means the repair does the one thing that is actually
        needed, and the table creation is allowed to fail for exactly that
        reason: the link that follows is the step that has to succeed, and it
        cannot unless the table it points at is really there.

        Which makes these two commands a pair that has to stay in this order and
        in this position - after the qdisc they hang off, before the filter that
        needs the table. That the first of them may fail says nothing about when
        it should run, and a batch that took the two apart on that basis could
        not attach at all.
        """
        network = self.network if family is V4 else self.network6
        if self._has_link(dev, parent, family, table):
            return
        self._run(
            [TC, "filter", "add", "dev", dev, "parent", parent]
            + ["protocol", family.proto, "prio", str(family.prio)]
            + ["handle", f"{table:x}:", "u32", "divisor", str(HASH_BUCKETS)],
            check=False,
        )
        self._run(
            [TC, "filter", "add", "dev", dev, "parent", parent]
            + ["protocol", family.proto, "prio", str(family.prio), "u32"]
            + ["match", family.match, side, str(network)]
            + ["hashkey", "mask", "0x000000ff", "at", str(at)]
            + ["link", f"{table:x}:"]
        )

    def _add_class(self, dev: str, client: int, bps: int) -> None:
        """One HTB class and its leaf AQM. `replace` on the class, del-then-add on the leaf.

        rate and ceil are set to the same number on purpose. HTB would otherwise
        let the class borrow up to ceil whenever the link is quiet, and a ceiling
        an operator has written down as "10 Mbit" that delivers thirty at 3am is
        not a ceiling they can explain to anybody.

        **The leaf qdisc is removed and added back rather than replaced**, and
        that is what makes changing a ceiling work at all. `tc qdisc replace` is
        not a replace when something is already hanging off that parent: the
        kernel builds a fresh qdisc only if the kind it was handed differs from
        the kind that is there, and otherwise reads the request as "change the
        existing one in place". fq_codel will not have its flow count changed
        after setup, so every second call for a client came back "RTNETLINK
        answers: Invalid argument" - raising or lowering somebody's speed failed,
        and only the first setting they were ever given worked.

        Leaving the handle off does not help, though it looks as though it
        should: the kernel's choice between changing and grafting does not
        consult the handle at all, only the kind and the exclusive flag. What it
        does skip past is a qdisc with *no* handle, which is what HTB puts back
        when a leaf is deleted - so removing first and adding after leaves the
        kernel building the fq_codel afresh, which is what `replace` was being
        asked for all along.

        The removal is not checked, because a client being given a ceiling for
        the first time has nothing to delete and that is the common case rather
        than a fault. It still has to happen before the add, in a batch as much
        as on its own, which is why a batch keeps the order these were queued in
        rather than sorting the commands that may fail to the front.

        What it costs is the packets sitting in that client's queue at the moment
        its ceiling changes - a few, on an operation an operator performs
        deliberately, and none at all for the clients a pass leaves alone, since
        only the ones whose numbers moved are written.
        """
        rate = f"{bps}bit"
        classid = f"1:{client:x}"
        self._run(
            [TC, "class", "replace", "dev", dev, "parent", ROOT_CLASS, "classid", classid]
            + ["htb", "rate", rate, "ceil", rate, "quantum", str(self.mtu)]
        )
        self._run([TC, "qdisc", "del", "dev", dev, "parent", classid], check=False)
        self._run([TC, "qdisc", "add", "dev", dev, "parent", classid, *CLIENT_QDISC])

    def _mark(self, address: str) -> None:
        """Stamp a client's upload with its own id, on the tunnel's ingress."""
        client = minor(self.network, address)
        for family, target, slot in self._targets(address, "up"):
            self._run(
                [TC, "filter", "replace", "dev", self.iface, "parent", INGRESS]
                + ["protocol", family.proto, "prio", str(family.prio)]
                + ["handle", slot, "u32", "ht", _bucket(slot)]
                + ["match", family.match, "src", target]
                + ["action", "skbedit", "mark", str(client)]
            )

    def _drop_down(self, address: str, client: int) -> None:
        """Remove the download filters, leaf and class, in that order.

        Filter first, always: a class removed while a filter still points at it
        is a classifier aiming at nothing, and the packets it aims are dropped
        rather than falling through to the default.
        """
        for family, _, slot in self._targets(address, "down"):
            self._del_filter(self.iface, ROOT, slot, "u32", family.proto, family.prio)
        self._del_class(self.iface, client)

    def _drop_up(self, address: str, client: int) -> None:
        if not self.wan:
            return
        for family, _, slot in self._targets(address, "up"):
            self._del_filter(self.iface, INGRESS, slot, "u32", family.proto, family.prio)
        self._del_filter(self.wan, ROOT, str(client), "fw", "all", 1)
        self._del_class(self.wan, client)

    def _del_filter(
        self, dev: str, parent: str, node: str, kind: str, proto: str, prio: int
    ) -> None:
        self._run(
            [TC, "filter", "del", "dev", dev, "parent", parent]
            + ["protocol", proto, "prio", str(prio), "handle", node, kind],
            check=False,
        )

    def _del_class(self, dev: str, client: int) -> None:
        self._run([TC, "qdisc", "del", "dev", dev, "parent", f"1:{client:x}"], check=False)
        self._run([TC, "class", "del", "dev", dev, "classid", f"1:{client:x}"], check=False)

    def _read_classes(self, dev: str) -> dict[str, int]:
        """Every client class on `dev`, back as {address: rate}.

        `-j` is asked for first because JSON is an interface, while the human
        listing is a display format that has changed between iproute2 releases.
        It is not always answered. `tc class show` was given JSON output long
        after `qdisc` and `filter` were, and on a build that predates that -
        6.1.0, which is what Ubuntu 24.04 and Debian 12 ship - the flag is
        accepted, silently ignored, and the human listing comes back with an exit
        status of zero. Nothing distinguishes it from a build that has no classes
        except that the output is prose.

        So both shapes are read. Taking only the JSON made limits() answer
        "nothing is shaped" on all of those hosts, which is worse than it sounds:
        the reconcile pass believes it, concludes every ceiling has been lost,
        and rewrites all of them on every cycle for ever without reporting a
        fault. One command answers both, because the text *is* what that command
        printed; the second is run only for a tc old enough to refuse `-j`
        outright, which unlike the above is a non-zero exit and no output at all.

        Only classes under our own root are ours. A leaf fq_codel reports a class
        per active flow - `8001:12` and the like - and the minor alone cannot tell
        one of those from client 0x12, so the major is checked too. Today those
        rows carry no rate and would be dropped a line later anyway; that is a
        property of how fq_codel dumps itself rather than anything this module
        arranged, and it is not what the question should rest on.

        Read back through the text shape a rate is only good to the kilobit:
        tc prints "5Mbit" for anything from 5000000 to 5000999 bit/s. A ceiling
        set to a whole number of kbit - which the panel's own Mbit-based form can
        only produce - survives the round trip exactly; one set to 1234567 bit/s
        through the API reads back as 1234000 and is rewritten once per reconcile
        pass, for ever. That is one client's four commands on a timer rather than
        the whole server's, and there is no other rate iproute2 will report.
        """
        out = self._capture([TC, "-j", "class", "show", "dev", dev])
        if out is None:
            out = self._capture([TC, "class", "show", "dev", dev])
        if not out:
            return {}
        rows = list(_classes_in(out))
        if not rows:
            log.debug("no class on %s was read out of: %s", dev, out.strip()[:200])

        found: dict[str, int] = {}
        base = int(self.network.network_address)
        last = int(self.network.broadcast_address)
        for client, rate in rows:
            value = base + client - CLASS_BASE
            if client < CLASS_BASE or value > last:
                continue
            address = str(ipaddress.IPv4Address(value))
            if subnet_mod.contains_host(self.network, address):
                found[address] = rate
        return found

    def _has_root(self, dev: str) -> bool:
        out = self._capture([TC, "qdisc", "show", "dev", dev, "root"]) or ""
        return "htb 1:" in out

    def _has_ingress(self) -> bool:
        out = self._capture([TC, "qdisc", "show", "dev", self.iface, "ingress"]) or ""
        return "ingress" in out

    def _has_link(self, dev: str, parent: str, family: _Family, table: int) -> bool:
        """Whether traffic on this chain is already being hashed into `table`.

        Asked per family and per chain rather than once for the interface,
        because the two families are built by separate commands and a run that
        died between them leaves exactly one of them standing.

        What is looked for is the *link*, not the table. A hash table is owned by
        the u32 block shared across a parent rather than by one classifier, so it
        outlives the chain it was created from and stops being listed with it -
        which made "is the table there" answer no about a table that was very
        much there. The link is a property of this chain and disappears with it,
        which is the question worth asking, and `link 10:` is a string this
        module chose rather than one iproute2 happens to format.
        """
        out = (
            self._capture(
                [TC, "filter", "show", "dev", dev, "parent", parent, "protocol", family.proto]
            )
            or ""
        )
        return f"link {table:x}:" in out

    def _check_rate(self, bps: int, side: str) -> int:
        """A ceiling is a whole number of bits per second, and fits in the tree.

        The upper bound is the root's rate rather than the link's, because there
        is no link rate any more and a ceiling above what the uplink can deliver
        was never a fault - it is one that never gets reached. What is left is
        the arithmetic: a rate above the root would be a class HTB can never let
        run at its own ceiling, which is a number that means nothing.
        """
        if not isinstance(bps, int) or isinstance(bps, bool) or bps < 0:
            raise ValidationError({side: "a ceiling is a whole number of bits per second, or 0."})
        if bps > ROOT_BPS:
            raise ValidationError(
                {side: f"a ceiling of {bps} bit/s is above the {ROOT_BPS} bit/s this can shape."}
            )
        return bps

    def _require_available(self) -> None:
        reason = available()
        if reason:
            raise ShaperError(TC, message=reason)

    # ---------------------------------------------------------------- running

    @contextlib.contextmanager
    def batched(self) -> Iterator[None]:
        """Collect this block's commands and send them all to `tc -batch` at the end.

        For the callers that touch every client at once - the bring-up hook, a
        full re-assert after the qdiscs were found missing, a bulk removal.
        Wrapping a loop of ordinary set_limit and clear_limit calls is the whole
        of the interface: nothing inside the block changes, and what would have
        been thirty thousand forks becomes one process fed thirty thousand lines.

        **Reads inside a batch see the kernel as it was before it.** `attached`,
        `limits` and the internal checks still run their own tc immediately,
        because they have to return something, and nothing queued has happened
        yet. That is right for the one place it comes up - attach() asks whether
        each piece is already there, and inside a batch the answer is about the
        state the batch is being built against - and it is a trap anywhere else,
        so a caller must not read back a value it has queued a change to.

        `detach` is immediate for the same reason: it is three commands that go
        through the module functions below, which take no subnet and so are
        reachable when a Shaper is not. Taking the structure down in the middle
        of building it is incoherent in any case, so nothing is lost by it.

        Nothing is sent if the block raises. A batch is one decision about what
        the kernel should hold, and half of a decision that could not be worked
        out is not a state anybody asked for.

        Nesting is allowed and does nothing: the outermost block owns the flush,
        so a helper that batches internally can be called from inside a larger
        batch without sending its commands early.
        """
        if self._queue is not None:
            yield
            return
        self._queue = _Queue()
        try:
            yield
        except BaseException:
            self._queue = None
            raise
        queue, self._queue = self._queue, None
        self._send(queue.items)

    def _send(self, items: Sequence[tuple[list[str], bool]]) -> None:
        """Hand a whole batch to `tc -batch` on stdin, in one go and in order.

        It runs with `-force`, which carries on past a command that failed
        instead of abandoning everything after it. Some of what is in here is
        expected to fail: clearing a client that never had a ceiling fails on
        every line, and creating a hash table that is already there is refused.
        For the rest it is the difference between one client that cannot be
        shaped and four thousand that are not - without it, a single refusal
        partway through a full re-apply left every client after it in the list
        with no ceiling at all.

        Stepping over a failure is not the same as reporting success. tc exits
        non-zero if anything in the batch was refused, and with `-force` it also
        says which lines, so the two are told apart afterwards rather than by
        having been sent separately: a batch raises when a command somebody
        asked to succeed is among the ones it names, and is quiet when they were
        all commands nobody was relying on.

        A non-zero exit tc will not attribute is raised on whatever it contained.
        "Something failed and I cannot say what" is not evidence that the failure
        was harmless, and treating it as such is how a ceiling goes missing with
        nothing in the log.
        """
        if not items:
            return
        lines = [_batch_line(cmd) for cmd, _ in items]
        wanted = [at for at, (_, check) in enumerate(items) if check]
        argv = [TC, "-force", "-batch", "-"]
        timeout = max(TIMEOUT, min(BATCH_TIMEOUT, len(lines) // BATCH_PER_TIMEOUT))
        try:
            proc = subprocess.run(
                argv,
                input="".join(f"{line}\n" for line in lines),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if wanted:
                raise ShaperError(
                    argv, message=f"{len(lines)} tc command(s) could not be run: {exc}"
                ) from exc
            log.info("%d tc command(s) could not be run: %s", len(lines), exc)
            return
        if proc.returncode == 0:
            return
        refused = _refused_lines(proc.stderr)
        blame = sorted(set(wanted) & refused) if refused is not None else wanted
        if not blame:
            log.debug("tc refused %d command(s) nobody was relying on", len(refused or ()))
            return
        raise ShaperError(
            argv, stderr=_batch_stderr(proc.stderr, lines, blame), returncode=proc.returncode
        )

    def _run(self, cmd: list[str], check: bool = True) -> None:
        """Run one tc command. `check` says whether failing it is worth raising over.

        The split matters. A caller that asked for a ceiling has to be told when
        it did not happen, so the commands that build the structure raise. The
        ones that take it apart do not: deleting a filter that is already gone
        is how every clear on an unshaped client ends, and it is not news.

        Inside batched() nothing is run at all: the command is queued under the
        same flag, which is what decides which of the two invocations it ends up
        in and therefore whether failing it is reported.
        """
        if self._queue is not None:
            self._queue.add(cmd, check)
            return
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            if check:
                raise ShaperError(cmd, message=f"{' '.join(cmd)} could not be run: {exc}") from exc
            log.info("%s could not be run: %s", " ".join(cmd), exc)
            return
        if proc.returncode != 0:
            if check:
                raise ShaperError(cmd, stderr=proc.stderr, returncode=proc.returncode)
            log.debug("%s exited %d", " ".join(cmd), proc.returncode)

    def _capture(self, cmd: list[str]) -> str | None:
        return _capture(cmd)


def _capture(cmd: list[str]) -> str | None:
    """stdout of a successful command, or None. Reads never raise."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("%s could not be run: %s", " ".join(cmd), exc)
        return None
    return proc.stdout if proc.returncode == 0 else None


def _quiet(cmd: list[str]) -> None:
    """Run one command and say nothing about it failing. For teardown only."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("%s could not be run: %s", " ".join(cmd), exc)
        return
    if proc.returncode != 0:
        log.debug("%s exited %d", " ".join(cmd), proc.returncode)


def _batch_line(cmd: list[str]) -> str:
    """One queued command as `tc -batch` wants it: the arguments, without the binary."""
    args = cmd[1:] if cmd and cmd[0] == TC else list(cmd)
    for token in args:
        if _UNSAFE_TOKEN.search(token):
            raise ShaperError(
                cmd,
                message=(
                    f"'{token}' cannot be sent to tc in a batch: it contains a space or a quote, "
                    "which tc would read as the end of the argument. Check the interface names."
                ),
            )
    return " ".join(args)


# "Command failed -:317", one per line tc would not run. The name is the batch
# file's, and for a batch read from stdin it is the literal "-".
_BATCH_FAILURE = re.compile(r"Command failed [^:]*:(\d+)")


def _refused_lines(stderr: str) -> set[int] | None:
    """Which commands tc says it would not run, as positions in what was sent.

    None rather than an empty set when tc named none of them, because "it exited
    non-zero and would not say which line" and "nothing was refused" are
    different answers and only one of them is good news.
    """
    found = {int(match.group(1)) - 1 for match in _BATCH_FAILURE.finditer(stderr or "")}
    return found or None


def _batch_stderr(stderr: str, lines: list[str], blame: Sequence[int] = ()) -> str:
    """tc's own complaint, with the commands it is about put in front of it.

    A failed batch says "Command failed -:317", which names a line of a file that
    only ever existed on this process's stdin - so on its own it tells whoever
    reads the log nothing at all. The lines are still here, so they are quoted.

    Only the ones somebody was relying on, and only the first few. A batch that
    failed for a reason worth raising over will usually also have stepped over a
    dozen removals that were never going to work, and burying the one command
    that matters among them is how a message ends up unread.

    They go in front because a ToolError's message is the first line of this and
    nothing after it, and what tc puts on that line is whichever complaint came
    first - which on a real kernel is as likely to be a warning about the htb
    quantum as anything to do with the failure. All of it is still carried on the
    exception, where a log can have the rest.
    """
    text = (stderr or "").strip()
    named = "; ".join(f"tc {lines[at]}" for at in list(blame)[:3] if 0 <= at < len(lines))
    if not named:
        return text
    return f"{named}: {text}" if text else named


def _bucket(node: str) -> str:
    """The hash table and bucket a full u32 handle sits in: "10:1f:800" -> "10:1f:"."""
    table, slot, _ = node.split(":")
    return f"{table}:{slot}:"


def _minor_of(value: object) -> int | None:
    """The minor half of a "1:1f" class handle, as an int, when the major is ours.

    Both halves are read, and the major is what decides whether the row is one of
    ours at all. Every class this module makes hangs under handle 1:, while a
    leaf qdisc reports classes of its own under the handle the kernel gave it -
    an fq_codel with traffic in it lists one per busy flow, `8001:1` upwards - and
    those minors run straight through the range a client's is drawn from.
    """
    if not isinstance(value, str) or ":" not in value:
        return None
    major, _, rest = value.partition(":")
    try:
        if int(major or "0", 16) != ROOT_MAJOR:
            return None
        return int(rest or "0", 16)
    except ValueError:
        return None


# One line of `tc class show`, which is what a tc that ignores `-j` answers with:
#
#     class htb 1:12 parent 1:1 leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst ...
#
# The handle is the third word whatever the qdisc kind. The rate is found by its
# own name rather than by counting, because what sits around it - `ceil`, `prio`,
# `leaf`, the burst figures - differs by kind and has moved between releases. A
# class with no rate at all is a leaf qdisc's own, and matches nothing here.
_CLASS_LINE = re.compile(
    r"^\s*class\s+\S+\s+([0-9a-f]+:[0-9a-f]*)\b.*?\brate\s+(\S+)", re.IGNORECASE
)


def _classes_in(listing: str) -> Iterator[tuple[int, int]]:
    """Every class in a listing as (minor, bits per second), in whichever shape it came.

    JSON from a tc that has it, and the human listing from one that does not -
    see _read_classes for why one command can come back as either. A row without
    both a handle and a rate is passed over rather than guessed at: it is a class
    belonging to some other qdisc, and inventing a ceiling nobody set is worse
    than reporting none.
    """
    try:
        rows = json.loads(listing)
    except (ValueError, TypeError):
        rows = None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            client, rate = _minor_of(row.get("handle")), _rate_of(row.get("rate"))
            if client is not None and rate is not None:
                yield client, rate
        return
    for line in listing.splitlines():
        found = _CLASS_LINE.match(line)
        if not found:
            continue
        client, rate = _minor_of(found.group(1)), _rate_of(found.group(2))
        if client is not None and rate is not None:
            yield client, rate


# "5Mbit", "800Kbit", "1000bit", or the bare number most builds emit. tc's
# multipliers are decimal for bit rates, and a "bps" suffix means *bytes* per
# second - its oldest and worst-named unit, and the one a naive parser reads as
# bits and reports at an eighth of the truth.
_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(bit|bps)?\s*$", re.IGNORECASE)
_SCALE = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000, "T": 1_000_000_000_000}


def _rate_of(value: object) -> int | None:
    """One class's rate in bits per second, however this iproute2 spelled it.

    `tc -j` prints a bare number on the builds this project has been run on, and
    a unit-suffixed string on others - it depends on how the release was
    compiled, not on anything that can be asked for on the command line. Only
    accepting the number would make limits() answer "nothing is shaped" on those
    hosts, which is worse than it sounds: a reconcile pass believes it and
    reapplies every ceiling on every cycle, forever, without ever reporting a
    fault.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    found = _RATE_RE.match(value)
    if not found:
        return None
    number, unit, kind = found.groups()
    scale = _SCALE[unit.upper()] * (8 if (kind or "").lower() == "bps" else 1)
    return int(float(number) * scale)
