"""The tunnel network: one parser, one allocator, one capacity.

The server's own `Address` in <iface>.conf is the authority. It already carries
the prefix (`10.13.13.1/24`), so it says both where the server sits and how big
the address pool is, and every other spelling of the same fact - `SUBNET_CIDR`
in clients.env, the `-s <cidr>` in the MASQUERADE hooks, the split-tunnel hint
in the UI - is a mirror of it. Deriving them all from that one line is what
stops an allocator, a firewall rule and a client's routes from disagreeing about
which network the tunnel is.

Until this module existed the subnet was a three-octet string
(`SUBNET_BASE="10.13.13"`) with the /24 implied, which capped a server at 253
clients for no reason other than the string format. That spelling is still
read, so an existing clients.env keeps working, but a subnet is a CIDR
everywhere from here on and any prefix from /16 to /30 is allowed.

The layout inside a network never changes: the network address itself is not
usable, the server takes the first host, clients are handed out from the second
host upward with gaps reused, and the broadcast address is left alone. bin/awg-
client's next_ip() implements exactly this in awk and has to keep agreeing with
it.
"""

import ipaddress
import re

# /30 is the smallest network with room for the server and one client; below
# that there is no host range at all. /16 is the other end: 65533 client
# addresses is already more than one server has any business handing out, and
# past it a typo starts covering other people's traffic rather than being a
# mistake worth catching. It is also as wide as the per-client upload shaping
# is dimensioned for, so a wider tunnel would be one the panel cannot police.
MIN_PREFIX = 16
MAX_PREFIX = 30

DEFAULT_CIDR = "10.13.0.0/20"

# The pre-CIDR spelling: three octets with the /24 left implicit.
_LEGACY_BASE_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")


def parse(value: str) -> ipaddress.IPv4Network:
    """Read any spelling of the tunnel network into the network itself.

    Accepts what the config actually contains in each place it is written:
    the server's `Address` (`10.13.13.1/24`, host bits set), a plain network
    (`10.13.13.0/24`), a comma-separated Address list of which the first IPv4
    entry wins, and the legacy three-octet `SUBNET_BASE` (`10.13.13`).

    Raises ValueError with a sentence an admin can act on.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("the tunnel subnet is empty; expected something like 10.13.13.1/24.")

    # `Address = 10.13.13.1/24, fd00::1/64` is legal in a WireGuard config. The
    # allocator is IPv4-only, so the first IPv4 entry is the one that counts.
    for part in (piece.strip() for piece in raw.split(",")):
        if not part:
            continue
        if _LEGACY_BASE_RE.match(part):
            part = f"{part}.0/24"
        try:
            iface = ipaddress.ip_interface(part)
        except ValueError:
            continue
        if iface.version != 4:
            continue
        network = iface.network
        if network.prefixlen < MIN_PREFIX or network.prefixlen > MAX_PREFIX:
            raise ValueError(
                f"'{part}' is a /{network.prefixlen}. Use a prefix between "
                f"/{MIN_PREFIX} and /{MAX_PREFIX}: a /{MAX_PREFIX} is the smallest network with "
                "room for the server and a client, and anything wider than "
                f"/{MIN_PREFIX} is more addresses than a tunnel can use."
            )
        return network

    raise ValueError(
        f"'{raw}' is not an IPv4 network. Expected an address with a prefix, e.g. 10.13.13.1/24."
    )


def parse_or_none(value: str) -> ipaddress.IPv4Network | None:
    """parse(), but a value that cannot be read is simply absent.

    Used where a fallback exists and a bad value must not take the panel down -
    a hand-edited clients.env, say, when the server config is readable and
    authoritative anyway.
    """
    try:
        return parse(value)
    except ValueError:
        return None


def server_ip(network: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    """The server's own address: the first host in the network."""
    return network.network_address + 1


def first_host(network: ipaddress.IPv4Network) -> int:
    """Lowest address a client may get, as an integer."""
    return int(network.network_address) + 2


def last_host(network: ipaddress.IPv4Network) -> int:
    """Highest address a client may get, as an integer. Broadcast is excluded."""
    return int(network.broadcast_address) - 1


def capacity(network: ipaddress.IPv4Network) -> int:
    """How many clients fit: everything but the network, the server and broadcast."""
    return max(0, last_host(network) - first_host(network) + 1)


def contains_host(network: ipaddress.IPv4Network, address: str) -> bool:
    """Is this a client address inside the pool? Server and broadcast are not."""
    try:
        value = int(ipaddress.IPv4Address(address.strip()))
    except ValueError:
        return False
    return first_host(network) <= value <= last_host(network)


def next_free(network: ipaddress.IPv4Network, used: set[int]) -> ipaddress.IPv4Address | None:
    """Lowest free host, gaps reused. None when the pool is full.

    The scan stops at the first free address, so it runs at most one step per
    address already handed out - it does not walk the whole network.
    """
    candidate = first_host(network)
    end = last_host(network)
    while candidate <= end and candidate in used:
        candidate += 1
    return ipaddress.IPv4Address(candidate) if candidate <= end else None


def as_int(address: str) -> int | None:
    """One IPv4 address as an integer, or None if it is not one."""
    try:
        return int(ipaddress.IPv4Address(address.strip()))
    except ValueError:
        return None
