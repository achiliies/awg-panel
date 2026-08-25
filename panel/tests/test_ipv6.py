"""The tunnel's IPv6 half, from the store outward.

tests/subnet6.sh already holds lib/subnet6.sh and awg/subnet6.py to the same
string for 1200 spellings of a prefix. What is left to check is the layer above:
that the store puts those addresses in all three of the places a client needs
them, that it puts none of them anywhere when the server carries no IPv6, and
that the client list can say which clients are still routing only IPv4.

The three places matter separately, and a config can look right while missing
any one of them:

  * ::/0 in the client's AllowedIPs, or the route is never created;
  * an IPv6 address on the client's interface, or several clients decline to
    install that route at all and the config reads as correct while leaking;
  * a /128 on the peer, or the server has no return path and the client's IPv6
    goes out and never comes back.
"""

import ipaddress

import pytest

from apps.clients.merge import leaks_ipv6
from awg import clientsenv, store, subnet6
from awg.errors import ValidationError

PREFIX = "fd7a:1e5f:22::/64"


@pytest.fixture
def dual_stack(server_conf, clients_env):
    """Turn the fixture server into a dual-stack one, the way the installer does."""
    text = server_conf.read_text(encoding="utf-8")
    address = next(line for line in text.splitlines() if line.startswith("Address"))
    server_conf.write_text(
        text.replace(address, f"{address}, fd7a:1e5f:22::1/64"), encoding="utf-8"
    )
    clientsenv.update_env({"SUBNET6_CIDR": PREFIX, "SUBNET6_MODE": "blackhole"})
    return server_conf


# --------------------------------------------------------------- the prefix


def test_the_server_address_is_the_authority_for_both_families(dual_stack):
    """One Address line, and each side reads its own half out of it."""
    assert str(store.subnet6_network()) == PREFIX
    assert store.subnet_network().version == 4


def test_a_server_without_a_prefix_reports_none(server_conf):
    """Not a default, not an error: a fact the callers have to handle."""
    assert store.subnet6_network() is None


def test_a_prefix_only_in_clients_env_is_still_read(server_conf, clients_env):
    """The mirror stands in when the config's Address has no v6 half yet."""
    clientsenv.update_env({"SUBNET6_CIDR": PREFIX})
    assert str(store.subnet6_network()) == PREFIX


# ----------------------------------------------------------- creating clients


def test_a_new_client_gets_all_three_halves(dual_stack):
    view = store.add_client("dual")
    network = store.subnet6_network()

    # The peer: what the server routes back.
    assert view.ip6 == subnet6.host_addr(
        network, subnet6.offset_of(store.subnet_network(), view.ip)
    )
    conf = store.client_conf_text("dual")
    # The interface: what the client is numbered as.
    assert f"Address = {view.ip}/32, {view.ip6}/128" in conf
    # The routes: what it sends through the tunnel.
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf


def test_the_two_addresses_are_the_same_client(dual_stack):
    """The v6 address is the v4 one's offset, so they cannot come apart."""
    network4, network6 = store.subnet_network(), store.subnet6_network()
    for name in ("one", "two", "three"):
        view = store.add_client(name)
        offset = int(ipaddress.IPv4Address(view.ip)) - int(network4.network_address)
        assert ipaddress.IPv6Address(view.ip6) == network6.network_address + offset


def test_a_server_without_ipv6_writes_none_of_it(server_conf):
    """No prefix means no address invented and no ::/0 promised."""
    view = store.add_client("v4only")
    assert view.ip6 == ""
    conf = store.client_conf_text("v4only")
    assert f"Address = {view.ip}/32\n" in conf
    assert "AllowedIPs = 0.0.0.0/0\n" in conf
    assert "::" not in conf


def test_a_split_default_route_is_a_full_tunnel(dual_stack):
    """0.0.0.0/1 + 128.0.0.0/1 covers everything without saying "0.0.0.0/0".

    Clients write it to override a system default route rather than replace it.
    Reading it as a split tunnel is what let a server go on leaking after it had
    supposedly been fixed.
    """
    store.add_client("halves", allowed_ips="0.0.0.0/1, 128.0.0.0/1")
    conf = store.client_conf_text("halves")
    assert "AllowedIPs = 0.0.0.0/1, 128.0.0.0/1, ::/0" in conf


def test_resync_upgrades_a_split_default_route(dual_stack):
    """The migration path for the clients already holding one."""
    store.add_client("halves")
    # Put the config back the way a client that predates this would have it.
    conf = store.client_conf_text("halves").replace(
        "AllowedIPs = 0.0.0.0/0, ::/0", "AllowedIPs = 0.0.0.0/1, 128.0.0.0/1"
    )
    store._write_client_conf("halves", conf)

    store.resync_all()

    assert "AllowedIPs = 0.0.0.0/1, 128.0.0.0/1, ::/0" in store.client_conf_text("halves")


def test_a_split_tunnel_keeps_the_routes_it_was_given(dual_stack):
    """Only an exact 0.0.0.0/0 is a full tunnel that has to grow ::/0."""
    store.add_client("split", allowed_ips="10.13.13.0/24")
    conf = store.client_conf_text("split")
    assert "AllowedIPs = 10.13.13.0/24\n" in conf
    # It still gets an address, because it is still on a dual-stack tunnel.
    assert "/128" in conf


# ------------------------------------------------------------------ migration


def test_resync_carries_a_pre_ipv6_server_across(server_conf, clients_env):
    """The upgrade path: clients created before the prefix existed."""
    before = [store.add_client(name) for name in ("old1", "old2")]
    assert all(view.ip6 == "" for view in before)

    text = server_conf.read_text(encoding="utf-8")
    address = next(line for line in text.splitlines() if line.startswith("Address"))
    server_conf.write_text(
        text.replace(address, f"{address}, fd7a:1e5f:22::1/64"), encoding="utf-8"
    )
    clientsenv.update_env({"SUBNET6_CIDR": PREFIX, "SUBNET6_MODE": "blackhole"})

    store.resync_all()

    for view in store.list_clients():
        # Every peer in the pool is routed IPv6, including the fixture's own
        # client1, which has no config file of its own.
        assert view.ip6, f"{view.name} was left without an IPv6 address"
        if not view.has_conf_file:
            continue
        conf = store.client_conf_text(view.name)
        assert f"{view.ip6}/128" in conf
        assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf


def test_resync_is_repeatable(dual_stack):
    """A peer that already has an address must not collect a second one."""
    created = store.add_client("once")
    store.resync_all()
    store.resync_all()

    after = next(view for view in store.list_clients() if view.name == "once")
    assert after.ip6 == created.ip6
    # One address on the peer and one on the interface, however many passes.
    assert store.client_conf_text("once").count("/128") == 1
    assert dual_stack.read_text(encoding="utf-8").count(f"{created.ip6}/128") == 1


def test_a_peer_outside_the_pool_is_left_alone(dual_stack, server_conf):
    """An invented offset would land on a real client's address."""
    store.add_client("inside")
    server_conf.write_text(
        server_conf.read_text(encoding="utf-8")
        + "\n[Peer]\n# Client = stray\nPublicKey = "
        + "1" * 42
        + "=\nAllowedIPs = 192.0.2.9/32\n",
        encoding="utf-8",
    )
    store.resync_all()
    stray = next(view for view in store.list_clients() if view.name == "stray")
    assert stray.ip6 == ""


# -------------------------------------------------------------- saving settings


def test_a_dual_stack_server_can_save_settings_that_are_not_the_address(dual_stack):
    """A save is checked against the whole configuration, not only what changed.

    So an address line the validator could not read stopped every save the
    panel could make - the obfuscation page included - on a server whose
    address was exactly what the installer had written there.
    """
    result = store.save_server({"S1": "120"})

    assert result["needs_restart"] is True
    text = dual_stack.read_text(encoding="utf-8")
    assert "S1 = 120" in text
    assert "Address = 10.13.13.1/24, fd7a:1e5f:22::1/64" in text


def test_saving_a_dual_stack_address_back_unchanged_is_a_no_op(dual_stack):
    """The page reads the line, shows it, and hands the same line back."""
    before = dual_stack.read_text(encoding="utf-8")

    result = store.save_server({"address": "10.13.13.1/24, fd7a:1e5f:22::1/64"})

    assert result["needs_restart"] is False
    assert dual_stack.read_text(encoding="utf-8") == before


def test_the_ipv4_half_can_still_be_moved(dual_stack):
    """Moving the pool is what this field has always been for; the prefix rides along."""
    store.save_server({"address": "10.20.0.1/20, fd7a:1e5f:22::1/64"})

    assert str(store.subnet_network()) == "10.20.0.0/20"
    assert str(store.subnet6_network()) == PREFIX


def test_moving_the_prefix_is_refused_rather_than_half_done(dual_stack):
    """The prefix is in four places and the installer is what writes all four.

    Moved here it would move in one of them, and every client would keep an
    address out of a prefix the server had stopped answering for - a tunnel that
    hand-shakes and then carries no IPv6, with nothing anywhere saying why.
    """
    before = dual_stack.read_text(encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        store.save_server({"address": "10.13.13.1/24, fd7a:1e5f:23::1/64"})

    assert "installer" in str(caught.value)
    assert dual_stack.read_text(encoding="utf-8") == before


def test_dropping_the_prefix_from_the_address_is_refused_too(dual_stack):
    """Taking IPv6 off a tunnel leaves hooks and a mode behind that still claim it."""
    with pytest.raises(ValidationError) as caught:
        store.save_server({"address": "10.13.13.1/24"})

    assert PREFIX in str(caught.value)
    assert str(store.subnet6_network()) == PREFIX


def test_adding_a_prefix_to_a_tunnel_without_one_is_refused(server_conf):
    """The other half of the same rule: the hooks have to be written with it."""
    with pytest.raises(ValidationError) as caught:
        store.save_server({"address": "10.13.13.1/24, fd7a:1e5f:22::1/64"})

    assert "installer" in str(caught.value)
    assert store.subnet6_network() is None


def test_moving_the_pool_by_subnet_keeps_the_prefix(dual_stack):
    """subnet_cidr names an IPv4 network and has no way of saying the other half.

    Deriving the whole address line from it alone dropped the v6 address off a
    dual-stack server as a side effect of moving its v4 pool, and left the
    ip6tables hooks and the clients.env mirror behind still describing it.
    """
    store.save_server({"subnet_cidr": "10.20.0.0/16"})

    assert store.read_server().address == "10.20.0.1/16, fd7a:1e5f:22::1/64"
    assert str(store.subnet6_network()) == PREFIX


def test_an_address_and_a_subnet_that_agree_on_the_pool_are_accepted(dual_stack):
    """Sending both is only a contradiction when they disagree about the network."""
    store.save_server(
        {"address": "10.20.0.1/16, fd7a:1e5f:22::1/64", "subnet_cidr": "10.20.0.0/16"}
    )

    assert store.read_server().address == "10.20.0.1/16, fd7a:1e5f:22::1/64"


def test_an_address_and_a_subnet_that_disagree_are_still_refused(dual_stack):
    with pytest.raises(ValidationError):
        store.save_server(
            {"address": "10.20.0.1/16, fd7a:1e5f:22::1/64", "subnet_cidr": "10.30.0.0/16"}
        )


# ------------------------------------------------------------- the server API


def test_the_server_payload_carries_the_ipv6_fields(dual_stack):
    """ServerView held these for a while before anything served them.

    A field on the dataclass that no serializer lists reaches nothing: the API
    answered without it, the UI could not show it, and the only sign was its
    absence.
    """
    from apps.server.serializers import ServerSerializer

    data = ServerSerializer(store.read_server()).data
    assert data["subnet6Cidr"] == PREFIX
    assert data["subnet6Mode"] == "blackhole"


def test_the_server_payload_says_nothing_when_there_is_no_ipv6(server_conf):
    from apps.server.serializers import ServerSerializer

    data = ServerSerializer(store.read_server()).data
    assert data["subnet6Cidr"] == ""
    assert data["subnet6Mode"] == ""


# ------------------------------------------------------- who is still leaking


@pytest.mark.parametrize(
    ("allowed", "has_v6", "expected"),
    [
        ("0.0.0.0/0", True, True),  # the leak: everything asked for, IPv4 delivered
        ("0.0.0.0/0, ::/0", True, False),  # fixed
        ("0.0.0.0/0,::/0", True, False),  # spacing is not meaning
        ("0.0.0.0/0", False, False),  # the server has no IPv6; not the client's doing
        ("10.13.13.0/24", True, False),  # a split tunnel routes what it was told to
        ("", True, False),  # nothing to say about a config we cannot read
        ("::/0", True, False),  # v6-only is odd, but it is not leaking v6
        # The split default route. Every one of these covers the whole of IPv4
        # without ever spelling "0.0.0.0/0", and a rule that matched the string
        # called them split tunnels and left them leaking.
        ("0.0.0.0/1, 128.0.0.0/1", True, True),
        ("0.0.0.0/1,128.0.0.0/1", True, True),
        ("128.0.0.0/1, 0.0.0.0/1", True, True),  # order is not meaning either
        ("0.0.0.0/2, 64.0.0.0/2, 128.0.0.0/1", True, True),
        ("0.0.0.0/1, 128.0.0.0/1, ::/0", True, False),  # already routing v6
        ("0.0.0.0/2, 128.0.0.0/1", True, False),  # 64.0.0.0/2 genuinely missing
        ("0.0.0.0/1", True, False),  # half of IPv4 is a real split tunnel
    ],
)
def test_leaks_ipv6(allowed, has_v6, expected):
    assert leaks_ipv6(allowed, has_v6) is expected
