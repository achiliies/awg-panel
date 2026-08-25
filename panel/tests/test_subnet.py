"""The tunnel network: parsing, capacity and allocation.

lib/subnet.sh implements the same rules in bash and awk (parse_cidr), for the
installer's own allocation. The two used to be held together by tests/compat.sh
and tests/crosscheck.sh, which went with the CLI they compared against: there is
no second allocator left, and the installer hands the first client to the panel
rather than placing it itself. What is asserted here is the arithmetic itself,
including the edges nobody reaches by hand: the smallest network that still
works, the last address before broadcast, and the pre-CIDR spelling an upgraded
server still has on disk.
"""

import ipaddress

import pytest

from awg import subnet


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The server's Address, which is what every reader actually has.
        ("10.13.13.1/24", "10.13.13.0/24"),
        ("10.13.0.1/16", "10.13.0.0/16"),
        ("172.16.0.1/16", "172.16.0.0/16"),
        # A plain network, which is what clients.env carries.
        ("10.13.13.0/24", "10.13.13.0/24"),
        # The pre-CIDR SUBNET_BASE spelling: three octets, /24 implied.
        ("10.13.13", "10.13.13.0/24"),
        # A WireGuard Address may list several; the allocator is IPv4-only, so
        # the first IPv4 entry wins wherever it sits.
        ("10.13.13.1/24, fd00::1/64", "10.13.13.0/24"),
        ("fd00::1/64, 10.13.13.1/24", "10.13.13.0/24"),
        # Whitespace survives a hand-edited config.
        ("  10.13.13.1/24  ", "10.13.13.0/24"),
    ],
)
def test_every_spelling_of_a_network_reads_the_same(value: str, expected: str) -> None:
    assert str(subnet.parse(value)) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-an-address",
        "10.13.13.1",  # no prefix and four octets: too ambiguous to guess at
        "999.1.1.1/24",
        "010.1.1.1/24",  # leading zeros; lib/subnet.sh rejects these too
        "fd00::1/64",
        "10.13.13.1/31",  # no host range at all
        "10.13.13.1/32",
        "10.0.0.1/15",  # wider than the tools will go
        "10.0.0.1/8",  # /16 is the ceiling: 65533 clients is already the most a
        # server hands out, and the shaper is dimensioned for no more
    ],
)
def test_what_is_not_a_tunnel_network(value: str) -> None:
    with pytest.raises(ValueError):
        subnet.parse(value)
    assert subnet.parse_or_none(value) is None


def test_the_prefix_bounds_are_explained_not_just_refused() -> None:
    with pytest.raises(ValueError) as caught:
        subnet.parse("10.13.13.1/31")
    message = str(caught.value)
    assert "/31" in message and "/30" in message and "/16" in message


@pytest.mark.parametrize(
    ("cidr", "server", "first", "last", "capacity"),
    [
        ("10.13.13.0/24", "10.13.13.1", "10.13.13.2", "10.13.13.254", 253),
        ("10.13.0.0/16", "10.13.0.1", "10.13.0.2", "10.13.255.254", 65533),
        # The ceiling: nothing wider than a /16 is accepted.
        ("10.13.0.0/17", "10.13.0.1", "10.13.0.2", "10.13.127.254", 32765),
        # The floor: network, server, one client, broadcast.
        ("10.13.13.0/30", "10.13.13.1", "10.13.13.2", "10.13.13.2", 1),
    ],
)
def test_the_layout_inside_a_network(
    cidr: str, server: str, first: str, last: str, capacity: int
) -> None:
    network = subnet.parse(cidr)
    assert str(subnet.server_ip(network)) == server
    assert str(ipaddress.IPv4Address(subnet.first_host(network))) == first
    assert str(ipaddress.IPv4Address(subnet.last_host(network))) == last
    assert subnet.capacity(network) == capacity


def test_the_pool_excludes_the_network_the_server_and_broadcast() -> None:
    network = subnet.parse("10.13.13.0/24")
    assert not subnet.contains_host(network, "10.13.13.0")
    assert not subnet.contains_host(network, "10.13.13.1")
    assert not subnet.contains_host(network, "10.13.13.255")
    assert subnet.contains_host(network, "10.13.13.2")
    assert subnet.contains_host(network, "10.13.13.254")
    # A client stranded by a subnet change is outside the pool, not in it.
    assert not subnet.contains_host(network, "10.99.99.5")
    assert not subnet.contains_host(network, "nonsense")


def test_a_wide_network_makes_the_255th_address_ordinary() -> None:
    """In a /24 that address is broadcast. In a /16 it is somebody's phone, and
    the old three-octet allocator could never have handed it out."""
    network = subnet.parse("10.13.0.0/16")
    assert subnet.contains_host(network, "10.13.0.255")
    assert subnet.contains_host(network, "10.13.1.0")
    assert not subnet.contains_host(network, "10.13.255.255")


def test_allocation_reuses_gaps_and_stops_at_the_top() -> None:
    network = subnet.parse("10.13.13.0/30")
    assert str(subnet.next_free(network, set())) == "10.13.13.2"
    assert subnet.next_free(network, {subnet.as_int("10.13.13.2")}) is None


def test_allocation_steps_over_the_old_ceiling() -> None:
    network = subnet.parse("10.13.0.0/16")
    used = {int(ipaddress.IPv4Address(f"10.13.0.{host}")) for host in range(2, 255)}
    assert str(subnet.next_free(network, used)) == "10.13.0.255"
    used.add(subnet.as_int("10.13.0.255"))
    assert str(subnet.next_free(network, used)) == "10.13.1.0"


def test_the_scan_costs_one_step_per_address_handed_out() -> None:
    """Not one per address in the network: a /16 must not walk 65 thousand
    slots to answer, or adding a client on a wide subnet would hang."""
    network = subnet.parse("10.0.0.0/16")
    used = {int(ipaddress.IPv4Address(f"10.0.0.{host}")) for host in range(2, 100)}
    assert str(subnet.next_free(network, used)) == "10.0.0.100"
