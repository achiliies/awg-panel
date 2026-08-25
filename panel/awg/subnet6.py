"""The tunnel's IPv6 network: the panel's half of lib/subnet6.sh.

Everything awg/subnet.py says about the IPv4 pool applies here too - one parser,
one set of derivations, and the server's own `Address` as the authority - with
one difference that shapes the whole module: **nothing is allocated**.

A client's IPv6 address is its IPv4 address's offset within the v4 pool written
into the v6 prefix, so the client at 10.13.0.5 out of 10.13.0.0/20 is
`<prefix>::5`. There is no second free-list and no second collision rule to
keep in step with the first. A client
cannot hold a v4 address and a v6 address belonging to two different clients,
because there is only ever one number and the other address is a rendering of
it.

Addresses are rendered here rather than by ipaddress.IPv6Address.compressed,
which is the surprising part and is deliberate. RFC 5952 compression takes the
longest run of zero groups and the leftmost of equals, and `2001:0:0:1::1:1170`
and `2001::1:0:0:1:1170` are that rule applied to the same address by two
implementations that broke the tie differently. Both are correct and they are
not the same string, which is all it takes for a config written by the CLI and
one written by the panel to stop comparing equal. So both sides instead use a
rule with no ties in it: drop the *trailing* zero groups of the prefix, join
what is left, and append `::` and the host part. Trivially the same in bash and
in Python, and tests/subnet6.sh holds the two to it - it runs this module and
lib/subnet6.sh over the same inputs and compares the strings, which is why it
sits in CI's shell job rather than the panel's.
"""

import hashlib
import ipaddress
import socket
from pathlib import Path

# What the server does with the prefix. See lib/subnet6.sh for what each one
# means and why the third exists.
MODES = ("native", "nat", "blackhole")

# The mode assumed when the configured one is missing or unreadable. Of the
# ways to be wrong about this, the one that leaks is the one not to default to.
DEFAULT_MODE = "blackhole"

# A tunnel is a /64 and nothing else: every client stack's idea of a link is
# built around 64 bits of host part, and a /64 is the smallest unit a provider
# routes.
PREFIX_LEN = 64

# Where subnet6_ula() takes its seed from, in the same order bash tries them.
MACHINE_ID_FILE = Path("/etc/machine-id")
ULA_SEED_PREFIX = "amneziawg-ula-"


def parse(value: str) -> ipaddress.IPv6Network:
    """Read any spelling of the tunnel's IPv6 network into the network itself.

    Accepts what the config actually holds in each place it is written: a bare
    prefix (`2001:db8:1:2::/64`), the server's own Address with host bits set
    (`2001:db8:1:2::1/64`), an address with no prefix at all (read as a /64),
    and an Address list of which the first IPv6 entry wins - `Address =
    10.13.0.1/20, fd00::1/64` is a legal line and awg/subnet.py reads the other
    half of it.

    Raises ValueError with a sentence an admin can act on.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("the tunnel's IPv6 prefix is empty; expected something like fd00::/64.")

    for part in (piece.strip() for piece in raw.split(",")):
        if not part:
            continue
        candidate = part if "/" in part else f"{part}/{PREFIX_LEN}"
        try:
            iface = ipaddress.ip_interface(candidate)
        except ValueError:
            continue
        if iface.version != 6:
            continue
        network = iface.network
        if network.prefixlen != PREFIX_LEN:
            raise ValueError(
                f"'{part}' is a /{network.prefixlen}. The tunnel takes a /{PREFIX_LEN}: a shorter "
                "prefix is a block to carve one out of, not a link, and a longer one breaks the "
                "host part every client stack assumes."
            )
        if not usable(network):
            raise ValueError(
                f"'{part}' is not a usable tunnel prefix. Use a global unicast /64 (2000::/3), "
                "which is what a provider routes to you, or a unique-local one (fc00::/7) for a "
                "tunnel that does not carry IPv6 upstream."
            )
        return network

    raise ValueError(
        f"'{raw}' is not an IPv6 network. Expected a prefix with its length, e.g. fd00::/64."
    )


def parse_or_none(value: str) -> ipaddress.IPv6Network | None:
    """parse(), but a value that cannot be read is simply absent.

    Used everywhere a fallback exists and a bad value must not take the panel
    down - and, more to the point, must not silently turn into "no IPv6", which
    is the state that leaks. Callers that get None fall back to a ULA and the
    blackhole mode rather than to writing a client config with no v6 in it.
    """
    try:
        return parse(value)
    except ValueError:
        return None


def usable(network: ipaddress.IPv6Network) -> bool:
    """Is this a prefix a tunnel can actually be numbered out of?

    Global unicast is what a provider routes to a host; unique-local is what a
    server without upstream IPv6 hands out so that clients still have an address
    to hang the ::/0 route on. Everything else - link-local, multicast, the
    unspecified address, the documentation range - is a prefix that either
    cannot be routed or must not be squatted on, and accepting one produces a
    tunnel that comes up and carries nothing.
    """
    first = network.network_address
    if int(first) == 0:
        return False
    if first.is_link_local or first.is_multicast or first.is_loopback:
        return False
    if first.is_private and not _is_ula(first):
        # Catches the documentation prefix (2001:db8::/32) and friends, which
        # ipaddress reports as private but nobody may number a real tunnel from.
        return False
    return _is_ula(first) or _is_global_unicast(first)


def render_prefix(network: ipaddress.IPv6Network) -> str:
    """The network half of the /64, trailing zero groups dropped.

    `2001:db8:0:1::/64` renders as `2001:db8:0:1`, `fd00::/64` as `fd00`, and
    `fd00:1::/64` as `fd00:1`. An interior zero group stays: dropping it would
    let the `::` swallow it and land the address in a different network.
    """
    groups = [f"{group:x}" for group in _groups(network.network_address)[:4]]
    while len(groups) > 1 and groups[-1] == "0":
        groups.pop()
    return ":".join(groups)


def cidr(network: ipaddress.IPv6Network) -> str:
    """The tunnel network as text, for the MASQUERADE source and the summary."""
    return f"{render_prefix(network)}::/{PREFIX_LEN}"


def server_ip(network: ipaddress.IPv6Network) -> ipaddress.IPv6Address:
    """The server's own address: ::1 in the prefix, mirroring the v4 first host."""
    return network.network_address + 1


def server_addr(network: ipaddress.IPv6Network) -> str:
    """The server's own address with its prefix, as the Interface line wants it."""
    return f"{render_prefix(network)}::1/{PREFIX_LEN}"


def host_ip(network: ipaddress.IPv6Network, offset: int) -> ipaddress.IPv6Address:
    """The address at `offset` in the network, as an address object."""
    if offset <= 0:
        raise ValueError("a client's offset within the tunnel is at least 1.")
    return network.network_address + offset


def host_addr(network: ipaddress.IPv6Network, offset: int) -> str:
    """A client's address, spelled the way lib/subnet6.sh spells it.

    Offsets above 0xffff take two groups. The widest tunnel accepted is a /16,
    whose 65533 clients stop just short of that, so nothing reaches the second
    group today; it stays because what makes the address right is the
    arithmetic, not the ceiling of the moment, and the ceiling has moved before.
    """
    if offset <= 0:
        raise ValueError("a client's offset within the tunnel is at least 1.")
    prefix = render_prefix(network)
    if offset > 0xFFFF:
        return f"{prefix}::{offset >> 16:x}:{offset & 0xFFFF:x}"
    return f"{prefix}::{offset:x}"


def host_cidr(network: ipaddress.IPv6Network, offset: int) -> str:
    """A client's address with the /128 the server config's AllowedIPs needs."""
    return f"{host_addr(network, offset)}/128"


def offset_of(network4: ipaddress.IPv4Network, address4: str | ipaddress.IPv4Address) -> int | None:
    """A client's offset within the IPv4 pool - the number both families share.

    None when the address is not in the pool at all, which is a peer somebody
    numbered by hand outside the tunnel network. It keeps its IPv4 address and
    gets no v6 one; inventing an offset for it would collide with a real client.
    """
    try:
        value = ipaddress.IPv4Address(str(address4).strip())
    except ValueError:
        return None
    if value not in network4:
        return None
    return int(value) - int(network4.network_address)


def needs_ipv6(allowed_ips: str) -> bool:
    """Would adding ::/0 to this route list close a leak?

    True when the list routes the whole of IPv4 and none of IPv6 - a client
    that asked for everything and is getting half of it.

    The question is deliberately about *coverage* rather than about the text.
    "0.0.0.0/0" is only one way to spell a full tunnel; "0.0.0.0/1,
    128.0.0.0/1" is the split-default-route form that clients and generators
    write to override a system default route without replacing it, and it
    covers exactly the same address space. Matching the string instead of the
    coverage classified the second as a split tunnel somebody had chosen, left
    it alone, and let every client using it go on leaking - which is precisely
    the failure this module exists to stop, reintroduced one layer up.

    Any IPv6 entry at all means the answer is no. Whoever wrote a v6 route into
    this list has already decided what the client does with IPv6, and a rule
    that appended ::/0 beside a deliberate v6 split tunnel would be overriding
    a real choice rather than filling in a missing one.
    """
    entries = [entry.strip() for entry in (allowed_ips or "").split(",") if entry.strip()]
    if not entries:
        return False
    if any(":" in entry for entry in entries):
        return False

    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.IPv4Network(entry, strict=False))
        except ValueError:
            # Something no parser here recognises. Leaving an unreadable route
            # list alone is the only safe move: rewriting it would be guessing
            # at what it meant.
            return False

    merged = list(ipaddress.collapse_addresses(networks))
    return len(merged) == 1 and merged[0] == ipaddress.IPv4Network("0.0.0.0/0")


def with_ipv6(allowed_ips: str) -> str:
    """The same route list, routing IPv6 too when that would close a leak."""
    if not needs_ipv6(allowed_ips):
        return allowed_ips
    return f"{allowed_ips.strip()}, ::/0"


def mode_valid(mode: str) -> bool:
    """Is this one of the three modes?"""
    return mode in MODES


def ula(seed: str | None = None) -> ipaddress.IPv6Network:
    """A stable unique-local /64 for this server.

    Derived from the machine ID rather than drawn at random, so that re-running
    the installer lands on the same network instead of silently renumbering
    every client. Hashed rather than used raw, because the machine ID
    identifies the host and this value ends up in config files that get mailed
    around.

    RFC 4193 asks for a random 40-bit global ID, which is what this is: the
    machine ID is unique per host and the hash spreads it, so a collision
    between two servers needs the same 40 bits out of 2^40.

    Byte-for-byte the same value as subnet6_ula() in lib/subnet6.sh, including
    the order the seed is looked for in.
    """
    if seed is None:
        seed = _host_seed()
    digest = hashlib.sha256(f"{ULA_SEED_PREFIX}{seed}".encode()).hexdigest()[:10]
    return ipaddress.IPv6Network(f"fd{digest[0:2]}:{digest[2:6]}:{digest[6:10]}::/{PREFIX_LEN}")


def _host_seed() -> str:
    try:
        value = MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    try:
        return socket.gethostname() or "awg"
    except OSError:
        return "awg"


def _groups(address: ipaddress.IPv6Address) -> list[int]:
    value = int(address)
    return [(value >> shift) & 0xFFFF for shift in range(112, -1, -16)]


def _is_ula(address: ipaddress.IPv6Address) -> bool:
    return int(address) >> 121 == 0x7E  # fc00::/7


def _is_global_unicast(address: ipaddress.IPv6Address) -> bool:
    return int(address) >> 125 == 0x1  # 2000::/3
