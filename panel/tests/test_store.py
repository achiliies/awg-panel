"""The store, checked against the exact bytes it is supposed to write.

The golden cases compare against fixtures captured from a real server rather
than against the store's own idea of the format, which is the only way a format
test means anything: these files are read by `awg-quick` at boot and by an admin
over SSH, and a rendering that has quietly drifted still round-trips through the
code that drifted with it.

The rest guard the things that go wrong quietly: an address handed out twice, a
disabled key that still reaches the kernel, a resync that overwrites the only
copy of a private key, and the PreDown hook - whose loss nobody notices until
the all-time traffic figures reset.
"""

import fcntl
import os
import re
import time
from pathlib import Path

import pytest

from awg import clientsenv, keys, store, traffic
from awg.conf import parse_conf, render_conf, strip_conf
from awg.errors import NAME_IN_USE, Conflict, NotFound, ValidationError
from awg.paths import lock_file
from awg.traffic import Counters

FIXTURES = Path(__file__).parent / "fixtures"

PREDOWN_LINE = "PreDown = /usr/local/bin/awg-panel manage trafficsync || true"
POSTUP_LINE = "PostUp = /usr/local/bin/awg-panel manage enforce || true"
SHAPE_LINE = "PostUp = /usr/local/bin/awg-panel manage shape || true"
UNSHAPE_LINE = "PostDown = /usr/local/bin/awg-panel manage shape --detach || true"

# The same two hooks as bin/awg-client owned them, which is what every server
# installed before the panel became the only writer still has in its config.
LEGACY_PREDOWN_LINE = "PreDown = /usr/local/bin/awg-client traffic sync"
LEGACY_POSTUP_LINE = "PostUp = /usr/local/bin/awg-client enforce || true"

# The peer block a client add appends, as a pattern: a blank line, the header,
# the two metadata comments in this order, then the three keys in this order.
PEER_BLOCK_RE = re.compile(
    r"\n\[Peer\]\n"
    r"# Client = (?P<name>[^\n]+)\n"
    r"# Created = (?P<created>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\n"
    r"PublicKey = (?P<public>[A-Za-z0-9+/]{43}=)\n"
    r"PresharedKey = (?P<psk>[A-Za-z0-9+/]{43}=)\n"
    r"AllowedIPs = (?P<ip>[\d.]+)/32\n$"
)


def conf_text(path) -> str:
    return path.read_text(encoding="utf-8")


def peer_named(path, name: str):
    for peer in parse_conf(conf_text(path)).peers:
        if peer.name == name:
            return peer
    raise AssertionError(f"no peer called {name!r} in {path}")


def fill_subnet(path, hosts, third: int = 13) -> None:
    """Append bare peer blocks holding the given host addresses.

    Written as text rather than through add_client: the allocator only ever
    looks at AllowedIPs, and 253 key generations would buy nothing.
    """
    blocks = [
        f"\n[Peer]\n# Client = filler{third}x{host}\nPublicKey = {host:043d}=\n"
        f"AllowedIPs = 10.13.{third}.{host}/32\n"
        for host in hosts
    ]
    path.write_text(conf_text(path) + "".join(blocks), encoding="utf-8")


@pytest.fixture
def golden_keys(monkeypatch, golden_client):
    """Pin the key material add_client generates, so the output is comparable."""
    monkeypatch.setattr(keys, "genkey", lambda: golden_client.private_key)
    monkeypatch.setattr(keys, "genpsk", lambda: golden_client.preshared_key)
    return golden_client


# ------------------------------------------------------------ golden format


def test_add_client_writes_the_golden_client_conf(server_conf, conf_dir, golden_keys):
    """Byte-for-byte the config a client on this fixture server must be given."""
    store.add_client(golden_keys.name)

    written = conf_dir / "clients" / f"{golden_keys.name}.conf"
    assert written.read_text(encoding="utf-8") == golden_keys.text
    assert written.stat().st_mode & 0o777 == 0o600


def test_add_client_appends_the_golden_peer_block(server_conf, server_conf_text, golden_keys):
    """Appended, not rewritten: nothing above the new block moves."""
    store.add_client(golden_keys.name)

    text = conf_text(server_conf)
    assert text.startswith(server_conf_text)

    match = PEER_BLOCK_RE.search(text[len(server_conf_text) :])
    assert match, text[len(server_conf_text) :]
    assert match.group("name") == golden_keys.name
    assert match.group("public") == keys.pubkey(golden_keys.private_key)
    assert match.group("psk") == golden_keys.preshared_key
    assert match.group("ip") == golden_keys.ip


def test_add_client_returns_a_view_without_the_private_key(server_conf, golden_keys):
    view = store.add_client(golden_keys.name)

    assert view.name == golden_keys.name
    assert view.ip == golden_keys.ip
    assert view.public_key == keys.pubkey(golden_keys.private_key)
    assert view.enabled is True
    assert view.has_conf_file is True
    assert golden_keys.private_key not in repr(view)


def test_added_peer_reaches_the_stripped_config(server_conf, golden_keys):
    store.add_client(golden_keys.name)
    stripped = strip_conf(parse_conf(conf_text(server_conf)))
    assert keys.pubkey(golden_keys.private_key) in stripped


def test_duplicate_name_is_a_conflict(server_conf):
    store.add_client("phone")
    with pytest.raises(Conflict):
        store.add_client("phone")


def test_the_two_conflicts_are_told_apart_by_a_code(server_conf):
    """Both refusals are 409s and only one of them is worth retrying.

    A caller - the panel's own add form among them - answers a name already in
    use by choosing another name. Answering an exhausted address pool that way
    is a retry loop that never ends, and the two are the same status with
    different sentences, so the difference is written down as a code.
    """
    store.add_client("phone")
    with pytest.raises(Conflict) as taken:
        store.add_client("phone")

    fill_subnet(server_conf, range(5, 255))
    with pytest.raises(Conflict) as full:
        store.add_client("one-too-many")

    assert taken.value.code == NAME_IN_USE
    assert full.value.code == ""


def test_a_client_can_be_added_without_a_name(server_conf):
    """The store draws one, and it is a name the store would itself accept."""
    view = store.add_client()

    assert store.NAME_RE.match(view.name)
    assert store.get_client(view.name).public_key == view.public_key


@pytest.mark.parametrize("name", ["", ".hidden", "has space", "a" * 33, "-leading", "sl/ash"])
def test_invalid_names_are_rejected(server_conf, name):
    with pytest.raises(ValidationError):
        store.add_client(name)


# --------------------------------------------------------- ip allocation


def test_second_client_gets_the_next_free_address(server_conf):
    """The fixture already holds .2 and .3, one of them unnamed and disabled -
    an address in use is an address in use whatever state its peer is in."""
    assert store.add_client("phone").ip == "10.13.13.4"
    assert store.add_client("laptop").ip == "10.13.13.5"
    assert store.next_ip() == "10.13.13.6"


def test_a_gap_left_by_remove_is_reused(server_conf):
    store.add_client("phone")  # .4
    store.add_client("laptop")  # .5

    store.remove_client("phone")

    assert store.next_ip() == "10.13.13.4"
    assert store.add_client("tablet").ip == "10.13.13.4"
    assert store.add_client("desktop").ip == "10.13.13.6"


def add_raw_peer(path, name: str, allowed_ips: str, host: int = 90) -> None:
    """Append one peer whose AllowedIPs reads exactly as given.

    Through text because that is the only way these spellings reach a server.
    The panel writes a /32 for every peer it creates, so an entry in any other
    form came from an admin's editor or from a tool that predates it - and those
    are the configs nobody is reading closely afterwards.
    """
    path.write_text(
        conf_text(path) + f"\n[Peer]\n# Client = {name}\nPublicKey = {host:043d}=\n"
        f"AllowedIPs = {allowed_ips}\n",
        encoding="utf-8",
    )


def test_a_bare_address_reserves_its_host(server_conf):
    """`AllowedIPs = 10.13.13.4` is what wg accepts and means the /32 by it.

    The allocator used to require the suffix before it would count an entry, so
    a bare address read as reserving nothing and .4 stayed in the pool with a
    peer already on it. The next client created got it, and two peers holding one
    address is a tunnel that works intermittently for both of them depending on
    which the kernel matched last, with nothing on the server saying so.
    """
    add_raw_peer(server_conf, "hand-edited", "10.13.13.4")

    assert store.next_ip() == "10.13.13.5"
    assert store.add_client("phone").ip == "10.13.13.5"


def test_a_bare_address_beside_a_suffixed_one_reserves_both(server_conf):
    add_raw_peer(server_conf, "hand-edited", "10.13.13.4/32, 10.13.13.5")

    assert store.next_ip() == "10.13.13.6"


def test_a_v6_address_beside_a_bare_v4_leaves_only_the_v4(server_conf):
    """A /128 has a prefix the reader rejects and colons as_int will not parse,
    so the dual-stack spelling of a hand-edited peer costs nothing extra."""
    add_raw_peer(server_conf, "dual", "10.13.13.4, fd7a:1e5f:22::4/128")

    assert store.next_ip() == "10.13.13.5"


def test_a_route_behind_a_peer_reserves_nothing(server_conf):
    """A network on a peer is a route to what is behind it, not an address this
    panel handed out. Counting its base address would be a guess at which of the
    254 was meant, and counting all of them would empty the pool of a server
    doing site-to-site - so it stays out of the question, as it always has."""
    add_raw_peer(server_conf, "branch-office", "192.168.50.0/24")

    assert store.next_ip() == "10.13.13.4"


def test_a_route_that_covers_the_tunnel_does_not_empty_the_pool(server_conf):
    """The same rule where it costs the most: a peer routed the whole tunnel
    network must not read as a claim on every address in it."""
    add_raw_peer(server_conf, "gateway", "10.13.13.0/24")

    assert store.next_ip() == "10.13.13.4"
    assert store.add_client("phone").ip == "10.13.13.4"


# ------------------------------------------------ what may go into a config


@pytest.mark.parametrize("break_char", ["\n", "\r"])
@pytest.mark.parametrize("field", ["dns", "allowed_ips"])
def test_a_line_break_never_reaches_a_client_config(server_conf, conf_dir, field, break_char):
    """One setting per line means a value with a newline in it is more settings.

    `DNS = 1.1.1.1\\nPostUp = curl ...` is a well-formed config carrying a
    PostUp, and wg-quick runs a PostUp as root on the machine that imports the
    file. clients.env has refused a line break since it was written; the client
    configs the store renders took the same values from the same API and did not.
    """
    payload = {field: f"1.1.1.1{break_char}PostUp = touch /tmp/pwned"}

    with pytest.raises(ValidationError) as caught:
        store.add_client("phone", **payload)

    assert field in caught.value.errors
    assert "line break" in str(caught.value)
    # And the refusal is not half a client: no peer, no file, no address spent.
    assert not (conf_dir / "clients" / "phone.conf").exists()
    assert "phone" not in conf_text(server_conf)
    assert store.next_ip() == "10.13.13.4"


@pytest.mark.parametrize("field", ["dns", "allowed_ips"])
def test_a_line_break_is_refused_on_update_too(server_conf, conf_dir, field):
    """The check is in the renderer rather than at the API edge, so every way in
    reaches it - add, update and resync all end at the same function."""
    store.add_client("phone")
    before = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        store.update_client("phone", **{field: "1.1.1.1\nPostUp = id"})

    assert (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8") == before


def test_an_ordinary_value_is_still_written(server_conf, conf_dir):
    store.add_client("phone", dns="9.9.9.9", allowed_ips="10.0.0.0/8")

    text = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")
    assert "DNS = 9.9.9.9" in text
    assert "AllowedIPs = 10.0.0.0/8" in text


def test_dot_one_is_never_handed_out(server_conf):
    """.1 is the server's own address; giving it to a client blackholes the
    gateway for everyone on the subnet."""
    parsed = parse_conf(conf_text(server_conf))
    parsed.peers.clear()
    server_conf.write_text(render_conf(parsed), encoding="utf-8")

    assert store.next_ip() == "10.13.13.2"
    assert store.add_client("first").ip == "10.13.13.2"

    # And it stays out of reach when everything else is taken.
    fill_subnet(server_conf, range(3, 255))
    with pytest.raises(Conflict):
        store.next_ip()


def test_exhaustion_raises_conflict(server_conf):
    fill_subnet(server_conf, range(4, 255))

    with pytest.raises(Conflict) as caught:
        store.next_ip()
    assert "10.13.13.0/24" in str(caught.value)

    with pytest.raises(Conflict):
        store.add_client("one-too-many")


def test_exhaustion_leaves_the_config_untouched(server_conf):
    fill_subnet(server_conf, range(4, 255))
    before = conf_text(server_conf)

    with pytest.raises(Conflict):
        store.add_client("one-too-many")

    assert conf_text(server_conf) == before


def test_allocator_follows_the_server_address_not_clients_env(server_conf, clients_env):
    """The server's own Address is the one thing both front-ends read, so it is
    the one that decides. A stale mirror in clients.env - and SUBNET_BASE on an
    installation that predates CIDR subnets is exactly that - must not move the
    allocator out of the network the interface actually routes."""
    clientsenv.update_env({"SUBNET_BASE": "10.99.99", "SUBNET_CIDR": "10.88.0.0/16"})
    assert store.next_ip() == "10.13.13.4"


def test_the_mirror_is_used_when_the_address_is_unreadable(server_conf, clients_env):
    """Only then. A config with no Address at all is not a server anybody can
    reach, but the panel still has to answer rather than throw."""
    parsed = parse_conf(conf_text(server_conf))
    parsed.interface.delete("Address")
    parsed.peers.clear()
    server_conf.write_text(render_conf(parsed), encoding="utf-8")

    clientsenv.update_env({"SUBNET_CIDR": "10.88.0.0/16"})
    assert store.next_ip() == "10.88.0.2"

    # And the pre-CIDR spelling below that, so an untouched clients.env from an
    # older install keeps allocating where it always did.
    clientsenv.update_env({"SUBNET_CIDR": "", "SUBNET_BASE": "10.99.99"})
    assert store.next_ip() == "10.99.99.2"


def test_a_wider_prefix_reaches_past_the_old_254_ceiling(server_conf, clients_env):
    """The whole point: a /24 stops at 253 clients, and nothing about the tools
    should have imposed that in the first place."""
    parsed = parse_conf(conf_text(server_conf))
    parsed.interface.set("Address", "10.13.0.1/16")
    parsed.peers.clear()
    server_conf.write_text(render_conf(parsed), encoding="utf-8")

    assert store.next_ip() == "10.13.0.2"
    fill_subnet(server_conf, range(2, 256), third=0)
    # .0.255 is an ordinary host inside a /16; only the network address, the
    # server and the broadcast address are out of bounds.
    assert store.next_ip() == "10.13.1.0"


# ------------------------------------------------------------------ rename


def test_rename_keeps_the_keys_and_moves_the_file(server_conf, conf_dir):
    created = store.add_client("phone")
    before = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")

    store.rename_client("phone", "work-phone")

    renamed = store.get_client("work-phone")
    assert renamed.public_key == created.public_key
    assert renamed.preshared_key == created.preshared_key
    assert renamed.ip == created.ip
    assert renamed.created == created.created

    assert "# Client = work-phone" in conf_text(server_conf)
    assert "# Client = phone\n" not in conf_text(server_conf)

    assert not (conf_dir / "clients" / "phone.conf").exists()
    assert (conf_dir / "clients" / "work-phone.conf").read_text(encoding="utf-8") == before


def test_rename_leaves_every_other_peer_alone(server_conf, server_conf_text):
    store.add_client("phone")
    store.rename_client("phone", "work-phone")

    text = conf_text(server_conf)
    # Everything the installer wrote, up to and including the last fixture peer.
    assert text.startswith(server_conf_text)


def test_rename_onto_an_existing_name_is_a_conflict(server_conf):
    store.add_client("phone")
    store.add_client("laptop")
    with pytest.raises(Conflict):
        store.rename_client("phone", "laptop")


def test_rename_of_a_missing_client(server_conf):
    with pytest.raises(NotFound):
        store.rename_client("ghost", "still-a-ghost")


def test_rename_to_the_same_name_is_allowed(server_conf):
    store.add_client("phone")
    store.rename_client("phone", "phone")
    assert store.get_client("phone").name == "phone"


# ----------------------------------------------------------------- disable


def test_disable_adds_the_marker_and_pulls_the_peer_out_of_the_strip(server_conf):
    view = store.add_client("phone")

    store.set_client_enabled("phone", False)

    peer = peer_named(server_conf, "phone")
    assert peer.disabled_at is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", peer.disabled_at)
    assert f"# Disabled = {peer.disabled_at}" in conf_text(server_conf)

    # The peer entry survives, so its address stays reserved and its config file
    # is still there to hand back when it is re-enabled.
    assert store.get_client("phone").enabled is False
    assert store.next_ip() == "10.13.13.5"
    assert store.client_conf_text("phone")

    assert view.public_key not in strip_conf(parse_conf(conf_text(server_conf)))


def test_enable_removes_the_marker_and_puts_the_peer_back(server_conf):
    view = store.add_client("phone")
    store.set_client_enabled("phone", False)

    store.set_client_enabled("phone", True)

    assert peer_named(server_conf, "phone").disabled_at is None
    # Only the fixture's own disabled peer still carries a marker.
    assert conf_text(server_conf).count("# Disabled = ") == 1
    assert store.get_client("phone").enabled is True
    assert view.public_key in strip_conf(parse_conf(conf_text(server_conf)))


def test_disabling_twice_keeps_the_original_timestamp(server_conf):
    """The collector re-asserts enforcement every minute; a moving timestamp
    would rewrite the server config every time it did."""
    store.add_client("phone")
    store.set_client_enabled("phone", False)
    first = conf_text(server_conf)

    store.set_client_enabled("phone", False)

    assert conf_text(server_conf) == first


def test_set_clients_enabled_switches_the_whole_list_in_one_write(server_conf, monkeypatch):
    """The point of the bulk path: one parse, one rewrite and one syncconf
    however many peers crossed a limit in the same instant."""
    phone = store.add_client("phone")
    laptop = store.add_client("laptop")
    keeper = store.add_client("tablet")
    writes = _count_conf_writes(monkeypatch)

    assert store.set_clients_enabled(["phone", "laptop"], False) == ["phone", "laptop"]

    assert len(writes) == 1, writes
    stripped = strip_conf(parse_conf(conf_text(server_conf)))
    assert phone.public_key not in stripped
    assert laptop.public_key not in stripped
    assert keeper.public_key in stripped
    # One decision, one moment: a different second per peer would suggest they
    # were switched off by different ones.
    assert (
        peer_named(server_conf, "phone").disabled_at
        == peer_named(server_conf, "laptop").disabled_at
    )


def test_set_clients_enabled_skips_what_is_already_in_that_state(server_conf, monkeypatch):
    """The collector re-asserts a decision it has already applied on every pass.
    Rewriting the config to say what it already says would move the timestamp the
    client index stamps, and put its rebuild on a timer."""
    store.add_client("phone")
    store.set_clients_enabled(["phone"], False)
    before = conf_text(server_conf)
    writes = _count_conf_writes(monkeypatch)

    assert store.set_clients_enabled(["phone", "ghost"], False) == []

    assert writes == []
    assert conf_text(server_conf) == before


def test_set_clients_enabled_puts_them_back(server_conf):
    phone = store.add_client("phone")
    laptop = store.add_client("laptop")
    store.set_clients_enabled(["phone", "laptop"], False)

    assert sorted(store.set_clients_enabled(["phone", "laptop"], True)) == ["laptop", "phone"]

    stripped = strip_conf(parse_conf(conf_text(server_conf)))
    assert phone.public_key in stripped
    assert laptop.public_key in stripped
    # Only the fixture's own disabled peer still carries a marker.
    assert conf_text(server_conf).count("# Disabled = ") == 1


def _count_conf_writes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every rewrite of the server config, which is what the batch saves."""
    writes: list[int] = []
    original = store._write_conf

    def record(conf):
        writes.append(len(conf.peers))
        return original(conf)

    monkeypatch.setattr(store, "_write_conf", record)
    return writes


def test_disabled_clients_are_still_listed(server_conf):
    """Hiding them would make an address look free when it is not."""
    store.add_client("phone")
    store.set_client_enabled("phone", False)

    names = {client.name for client in store.list_clients()}
    assert "phone" in names
    # None is the fixture's unnamed disabled peer, which the panel shows as
    # "(unnamed)" and which must never be dropped from the list either.
    assert {client.name for client in store.list_clients() if not client.enabled} == {"phone", None}


# ------------------------------------------------------------- save_server


def test_port_change_needs_a_restart(server_conf):
    result = store.save_server({"listen_port": 51820})

    assert result["needs_restart"] is True
    assert parse_conf(conf_text(server_conf)).interface.get("ListenPort") == "51820"


def test_s1_change_needs_a_restart_and_a_reimport(server_conf):
    """An obfuscation edit half-applied fails the handshake with nothing logged,
    so the save restarts the interface and resyncs every client."""
    store.add_client("phone")

    result = store.save_server({"S1": "120"})

    assert result["needs_restart"] is True
    assert result["must_reimport"] is True
    assert result["resynced"] == 1
    assert parse_conf(conf_text(server_conf)).interface.get("S1") == "120"
    assert "S1 = 120" in store.client_conf_text("phone")


def test_dns_change_is_client_side_only(server_conf):
    store.add_client("phone")
    before = conf_text(server_conf)

    result = store.save_server({"dns": "9.9.9.9"})

    assert result["needs_restart"] is False
    assert result["must_reimport"] is True
    assert conf_text(server_conf) == before  # the server does not care about DNS
    assert "DNS = 9.9.9.9" in store.client_conf_text("phone")
    assert clientsenv.read_env()["CLIENT_DNS"] == "9.9.9.9"


def test_saving_nothing_changes_nothing(server_conf):
    before = conf_text(server_conf)

    result = store.save_server({"listen_port": 41234, "mtu": 1400})

    assert result["needs_restart"] is False
    assert result["must_reimport"] is False
    assert conf_text(server_conf) == before


def test_invalid_parameters_are_rejected_before_anything_is_written(server_conf):
    before = conf_text(server_conf)

    with pytest.raises(ValidationError) as caught:
        store.save_server({"Jmin": "90"})  # Jmax is 70

    assert "Jmin" in str(caught.value) or "Jmax" in str(caught.value)
    assert conf_text(server_conf) == before


def test_save_server_backs_the_config_up_first(server_conf, conf_dir):
    store.save_server({"listen_port": 51820})
    assert list(conf_dir.glob("awg0.conf.bak-*"))


def test_save_server_reports_the_importer_warnings(server_conf):
    # Small enough to fit the fixture's MTU: the padding shares the per-packet
    # budget with S4, and a value that overruns it is refused outright, which is
    # a different test from this one.
    result = store.save_server({"ContentPaddingAddition": "16"})
    assert any("Amnezia app" in text for text in result["warnings"])


# --------------------------------------------------------- the tunnel subnet


def test_moving_the_subnet_derives_the_server_address(server_conf, clients_env):
    result = store.save_server({"subnet_cidr": "10.20.0.0/16"})

    conf = parse_conf(conf_text(server_conf))
    assert conf.interface.get("Address") == "10.20.0.1/16"
    assert result["needs_restart"] is True
    assert clientsenv.read_env()["SUBNET_CIDR"] == "10.20.0.0/16"


def test_moving_the_subnet_moves_the_masquerade_rule(server_conf, clients_env):
    """The `-s` in the NAT rule decides whose packets get translated on the way
    out. Left behind, every client hand-shakes and then reaches nothing."""
    result = store.save_server({"subnet_cidr": "10.20.0.0/16"})

    text = conf_text(server_conf)
    assert "POSTROUTING -s 10.20.0.0/16 -o" in text
    assert "10.13.13.0/24" not in text
    assert any("NAT rules" in warning for warning in result["warnings"])


def test_a_customised_hook_is_reported_rather_than_guessed_at(server_conf, clients_env):
    store.save_server({"post_up": ["iptables -A FORWARD -i %i -j ACCEPT"], "post_down": []})

    result = store.save_server({"subnet_cidr": "10.20.0.0/16"})

    assert any("No PostUp/PostDown rule sources" in warning for warning in result["warnings"])


def test_an_unrelated_source_in_a_hook_is_left_alone(server_conf, clients_env):
    """Only a `-s` naming the old tunnel network moves. An admin's own rule for
    some other source is theirs, and rewriting it would be a surprise."""
    store.save_server(
        {
            "post_up": [
                "iptables -t nat -A POSTROUTING -s 10.13.13.0/24 -o eth0 -j MASQUERADE",
                "iptables -t nat -A POSTROUTING -s 192.168.50.0/24 -o eth0 -j MASQUERADE",
            ]
        }
    )

    store.save_server({"subnet_cidr": "10.20.0.0/16"})

    # By content, not by position: the tunnel's own enforce hook is kept at the
    # head of PostUp, so an admin's rules are never the first entries here.
    hooks = [
        line
        for line in parse_conf(conf_text(server_conf)).interface.all("PostUp")
        if "iptables" in line
    ]
    assert "-s 10.20.0.0/16 -o eth0" in hooks[0]
    assert "-s 192.168.50.0/24 -o eth0" in hooks[1]


def test_the_legacy_field_name_still_moves_the_subnet(server_conf, clients_env):
    """`subnet_base` was the pre-CIDR spelling. A script written against it has
    no reason to have been changed, so it keeps working - and the three-octet
    value it carries can only ever have meant a /24."""
    store.save_server({"subnet_base": "10.20.30"})

    assert parse_conf(conf_text(server_conf)).interface.get("Address") == "10.20.30.1/24"


def test_the_stale_three_octet_mirror_is_emptied_not_left_lying(server_conf, clients_env):
    clientsenv.update_env({"SUBNET_BASE": "10.13.13"})

    store.save_server({"subnet_cidr": "10.20.0.0/16"})

    env = clientsenv.read_env()
    assert env["SUBNET_CIDR"] == "10.20.0.0/16"
    assert env["SUBNET_BASE"] == ""


def test_an_address_and_a_subnet_that_disagree_are_refused(server_conf, clients_env):
    with pytest.raises(ValidationError) as caught:
        store.save_server({"address": "10.20.0.1/16", "subnet_cidr": "10.30.0.0/16"})
    assert "subnet_cidr" in caught.value.errors


def test_an_address_and_a_subnet_that_agree_are_accepted(server_conf, clients_env):
    store.save_server({"address": "10.20.0.1/16", "subnet_cidr": "10.20.0.0/16"})

    assert parse_conf(conf_text(server_conf)).interface.get("Address") == "10.20.0.1/16"


@pytest.mark.parametrize("value", ["10.13.13.1/31", "10.0.0.1/7", "not-a-network", "10.13.13.1"])
def test_a_subnet_that_is_not_one_is_refused(server_conf, clients_env, value):
    with pytest.raises(ValidationError):
        store.save_server({"subnet_cidr": value})


def test_clients_left_outside_the_new_subnet_are_named(server_conf, clients_env):
    store.add_client("phone")

    result = store.save_server({"subnet_cidr": "10.20.0.0/16"})

    stranded = [w for w in result["warnings"] if "outside 10.20.0.0/16" in w]
    assert stranded and "phone" in stranded[0]


def test_a_wider_prefix_keeps_every_client_where_it_is(server_conf, clients_env):
    """Widening 10.13.13.0/24 to 10.13.0.0/16 does not move the network address,
    so nothing is stranded - it is the cheap way out of a full subnet."""
    store.add_client("phone")

    result = store.save_server({"subnet_cidr": "10.13.0.0/16"})

    assert not [w for w in result["warnings"] if "outside" in w]
    assert store.read_server().subnet_capacity == 65533


# ------------------------------------------------------------------ resync


def test_resync_rewrites_every_client_that_has_a_config(server_conf, conf_dir):
    store.add_client("phone")
    store.add_client("laptop")

    # A change every client can see, so there is something to write. client1 in
    # the fixture has a peer entry but no config file, and the second fixture
    # peer has no name at all: both are skipped.
    assert store.resync_all(dns="9.9.9.9") == 2

    for name in ("phone", "laptop"):
        assert "DNS = 9.9.9.9" in (conf_dir / "clients" / f"{name}.conf").read_text()


def test_resync_leaves_a_config_that_already_says_this_alone(server_conf, conf_dir):
    """A resync is not always a change, and the write is the expensive part.

    Re-running the installer, saving a settings page having edited nothing, or
    running the command by hand all arrive here with the files already correct.
    Rewriting them costs an fsync per client - the slowest thing the panel does
    on a large server - to produce the bytes that were already there."""
    store.add_client("phone")
    store.add_client("laptop")
    files = [conf_dir / "clients" / f"{name}.conf" for name in ("phone", "laptop")]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}

    assert store.resync_all() == 0

    for path in files:
        contents, mtime = before[path]
        assert path.read_bytes() == contents
        # Not merely equal by value: the file was never opened for writing.
        assert path.stat().st_mtime_ns == mtime


def test_resync_still_writes_the_clients_that_did_change(server_conf, conf_dir):
    """Skipping the unchanged ones must not skip the one that needs it."""
    store.add_client("phone")
    store.add_client("laptop")
    # The endpoint comes from the server, not from the client's own file, so a
    # wrong one here is exactly what a resync exists to correct. (DNS and
    # AllowedIPs would not do: a resync keeps each client's own.)
    stale = conf_dir / "clients" / "phone.conf"
    stale.write_text(
        re.sub(r"^Endpoint = .*$", "Endpoint = 198.51.100.9:1", stale.read_text(), flags=re.M),
        encoding="utf-8",
    )
    untouched = conf_dir / "clients" / "laptop.conf"
    before = untouched.stat().st_mtime_ns

    assert store.resync_all() == 1

    assert "198.51.100.9:1" not in stale.read_text(encoding="utf-8")
    assert untouched.stat().st_mtime_ns == before


def test_resync_skips_a_client_whose_private_key_does_not_match_its_peer(server_conf, conf_dir):
    """A mismatch means the file was hand-edited or corrupted. Rewriting it would
    destroy the only copy of a key that might still be recoverable, and would
    hand the user a config that cannot connect either way."""
    store.add_client("phone")
    store.add_client("laptop")

    stray = conf_dir / "clients" / "phone.conf"
    text = stray.read_text(encoding="utf-8")
    stray.write_text(
        re.sub(r"^PrivateKey = .*$", f"PrivateKey = {keys.genkey()}", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    before = stray.read_text(encoding="utf-8")

    assert store.resync_all(dns="9.9.9.9") == 1

    assert stray.read_text(encoding="utf-8") == before
    assert "DNS = 9.9.9.9" in store.client_conf_text("laptop")


def test_resync_keeps_each_clients_own_dns_and_scope(server_conf):
    store.add_client("phone", dns="1.0.0.1", allowed_ips="10.13.13.0/24")

    store.resync_all()

    text = store.client_conf_text("phone")
    assert "DNS = 1.0.0.1" in text
    assert "AllowedIPs = 10.13.13.0/24" in text


def test_resync_carries_a_new_default_dns_to_everybody(server_conf):
    store.add_client("phone", dns="1.0.0.1")
    store.resync_all(dns="9.9.9.9")
    assert "DNS = 9.9.9.9" in store.client_conf_text("phone")


# ------------------------------------------------------------- other paths


def test_reset_keys_issues_a_new_pair_and_keeps_the_address(server_conf):
    created = store.add_client("phone")

    rotated = store.reset_client_keys("phone")

    assert rotated.public_key != created.public_key
    assert rotated.preshared_key != created.preshared_key
    assert rotated.ip == created.ip
    assert rotated.public_key in strip_conf(parse_conf(conf_text(server_conf)))
    assert created.public_key not in conf_text(server_conf)


def test_remove_deletes_the_peer_and_its_config(server_conf, conf_dir):
    created = store.add_client("phone")

    store.remove_client("phone")

    assert created.public_key not in conf_text(server_conf)
    assert not (conf_dir / "clients" / "phone.conf").exists()
    with pytest.raises(NotFound):
        store.get_client("phone")


def test_remove_clients_takes_several_at_once_and_leaves_the_rest(server_conf, conf_dir):
    """The whole point of the bulk path: one config write for the lot of them,
    with everything outside the list untouched."""
    phone = store.add_client("phone")
    laptop = store.add_client("laptop")
    keeper = store.add_client("tablet")
    traffic.write_db(
        {
            phone.public_key: Counters(1, 2, 1, 2),
            keeper.public_key: Counters(3, 4, 3, 4),
        }
    )

    assert store.remove_clients(["phone", "laptop"]) == ["phone", "laptop"]

    text = conf_text(server_conf)
    assert phone.public_key not in text
    assert laptop.public_key not in text
    assert keeper.public_key in text
    assert not (conf_dir / "clients" / "phone.conf").exists()
    assert not (conf_dir / "clients" / "laptop.conf").exists()
    assert (conf_dir / "clients" / "tablet.conf").exists()
    # Both rows dropped in the one rewrite. Popping only the first is what a
    # short-circuiting any() over the keys would have done.
    assert traffic.read_db() == {keeper.public_key: Counters(3, 4, 3, 4)}


def test_remove_clients_skips_a_name_that_is_already_gone(server_conf):
    """A sweep names clients it listed a moment ago, and something may have
    taken one in between. That is the outcome asked for, not an error."""
    store.add_client("phone")
    before = conf_text(server_conf)

    assert store.remove_clients(["ghost"]) == []
    assert conf_text(server_conf) == before

    assert store.remove_clients(["ghost", "phone"]) == ["phone"]
    assert "# Client = phone" not in conf_text(server_conf)


def test_update_client_rewrites_only_the_client_file(server_conf):
    store.add_client("phone")
    before = conf_text(server_conf)

    store.update_client("phone", allowed_ips="10.13.13.0/24")

    assert conf_text(server_conf) == before
    assert "AllowedIPs = 10.13.13.0/24" in store.client_conf_text("phone")


@pytest.mark.parametrize(
    ("field", "line", "default"),
    [
        ("dns", "DNS", "8.8.8.8, 8.8.4.4"),
        ("allowed_ips", "AllowedIPs", "0.0.0.0/0"),
    ],
)
def test_an_empty_value_clears_the_clients_own_setting(server_conf, field, line, default):
    """Empty means "take the server's default", and it has to be sayable.

    Both fields used to arrive as None whether the caller had left them out or
    emptied them, so a client given a resolver of its own could never be put
    back on the server's: the request answered 200 and kept the old value.
    """
    store.add_client("phone", **{field: "10.10.10.10" if field == "dns" else "10.0.0.0/8"})
    assert f"{line} = " in store.client_conf_text("phone")

    store.update_client("phone", **{field: ""})

    assert f"{line} = {default}" in store.client_conf_text("phone")


def test_an_absent_value_still_leaves_the_clients_own_setting(server_conf):
    """The other half of the same distinction: nothing said, nothing changed."""
    store.add_client("phone", dns="10.10.10.10", allowed_ips="10.0.0.0/8")

    store.update_client("phone", dns="10.20.20.20")

    text = store.client_conf_text("phone")
    assert "DNS = 10.20.20.20" in text
    assert "AllowedIPs = 10.0.0.0/8" in text


def test_a_peer_listing_ipv6_first_keeps_its_ipv4_address(server_conf, conf_dir):
    """AllowedIPs has no promised order, and a hand-edited config may use another.

    Reading the first entry gave the client its own /128 as an IPv4 address, so
    the config it re-imported had no v4 address, no v6 address derived from one,
    and a peer that could reach nothing.
    """
    store.add_client("phone")
    address = store.get_client("phone").ip
    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(
        text.replace(f"AllowedIPs = {address}/32", f"AllowedIPs = fd00::99/128, {address}/32"),
        encoding="utf-8",
    )

    store.update_client("phone", dns="9.9.9.9")

    assert f"Address = {address}/32" in store.client_conf_text("phone")
    assert store.get_client("phone").ip == address


def test_a_peer_listing_ipv6_first_is_still_counted_against_the_pool(server_conf):
    """The count the panel shows and the address the allocator hands out agree."""
    store.add_client("phone")
    address = store.get_client("phone").ip
    before = store.scan_server().free_ips
    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(
        text.replace(f"AllowedIPs = {address}/32", f"AllowedIPs = fd00::99/128, {address}/32"),
        encoding="utf-8",
    )

    assert store.scan_server().free_ips == before
    assert store.next_ip() != address


def test_unnamed_fixture_peer_is_preserved(server_conf, server_conf_text):
    """A hand-pasted peer with no "# Client" line owns an address; every mutation
    has to leave it exactly where it was."""
    store.add_client("phone")
    store.rename_client("phone", "work-phone")
    store.set_client_enabled("work-phone", False)
    store.remove_client("work-phone")

    assert conf_text(server_conf) == server_conf_text


# ---------------------------------------------------------- the PreDown hook


MUTATIONS = {
    "add": lambda: store.add_client("second"),
    "remove": lambda: store.remove_client("phone"),
    "rename": lambda: store.rename_client("phone", "work-phone"),
    "disable": lambda: store.set_client_enabled("phone", False),
    "enable": lambda: (
        store.set_client_enabled("phone", False),
        store.set_client_enabled("phone", True),
    ),
    "reset_keys": lambda: store.reset_client_keys("phone"),
    "update": lambda: store.update_client("phone", dns="9.9.9.9"),
    "save_port": lambda: store.save_server({"listen_port": 51820}),
    "save_obfuscation": lambda: store.save_server({"Jc": "8"}),
    "save_dns": lambda: store.save_server({"dns": "9.9.9.9"}),
    "save_hooks": lambda: store.save_server({"post_up": ["iptables -A FORWARD -i %i -j ACCEPT"]}),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_every_tunnel_hook_survives_every_mutation(server_conf, mutation):
    """Lose the PreDown and the kernel's counters vanish on the next orderly
    restart; lose one PostUp and every revoked client is back on the interface at
    the next boot, or every bandwidth ceiling stops being enforced. None of them
    says anything when it happens."""
    store.add_client("phone")
    MUTATIONS[mutation]()

    text = conf_text(server_conf)
    assert PREDOWN_LINE in text
    assert POSTUP_LINE in text
    assert SHAPE_LINE in text
    assert UNSHAPE_LINE in text
    interface = parse_conf(text).interface
    assert interface.all("PreDown") == ["/usr/local/bin/awg-panel manage trafficsync || true"]
    # Both ahead of the firewall rules and in this order: neither depends on
    # routing, and nothing is served by making a revocation wait behind it.
    assert interface.all("PostUp")[:2] == [
        "/usr/local/bin/awg-panel manage enforce || true",
        "/usr/local/bin/awg-panel manage shape || true",
    ]
    # ...and the teardown last, for the same reason it goes first on the way up.
    assert interface.all("PostDown")[-1] == (
        "/usr/local/bin/awg-panel manage shape --detach || true"
    )


@pytest.mark.parametrize(
    "slot,line",
    [
        ("pre_down", PREDOWN_LINE),
        ("post_up", POSTUP_LINE),
        ("post_up", SHAPE_LINE),
        ("post_down", UNSHAPE_LINE),
    ],
)
def test_clearing_a_tunnel_hook_puts_it_back(server_conf, slot, line):
    """None of them is the admin's to delete from here. A config without the
    PreDown silently loses traffic history, one without the enforce hook silently
    un-revokes, and one without the shaping pair silently stops shaping."""
    store.save_server({slot: []})
    assert line in conf_text(server_conf)


def test_a_config_without_the_hooks_gains_them_on_the_next_write(server_conf):
    """Upgrading from a version that predates traffic accounting or bandwidth
    ceilings, which is what install.sh also does when it finds an older config."""
    text = conf_text(server_conf)
    for line in (PREDOWN_LINE, POSTUP_LINE, SHAPE_LINE, UNSHAPE_LINE):
        text = text.replace(line + "\n", "")
    server_conf.write_text(text, encoding="utf-8")
    assert not any(line in text for line in (PREDOWN_LINE, POSTUP_LINE, SHAPE_LINE, UNSHAPE_LINE))

    store.add_client("phone")

    written = conf_text(server_conf)
    assert all(line in written for line in (PREDOWN_LINE, POSTUP_LINE, SHAPE_LINE, UNSHAPE_LINE))


def test_the_hooks_land_in_one_order_whichever_of_them_the_config_had(server_conf):
    """Because a file that had half of them must end up matching one that had none.

    They are taken out and put back rather than added where missing, and this is
    what that is for: written the other way, the hook that was already there
    stayed where it stood and the new one went in front of it, so an upgraded
    config and a fresh one disagreed about the order of two lines forever.
    """
    text = conf_text(server_conf).replace(POSTUP_LINE + "\n", "")
    server_conf.write_text(text, encoding="utf-8")
    store.add_client("phone")

    interface = parse_conf(conf_text(server_conf)).interface
    assert interface.all("PostUp")[:2] == [
        "/usr/local/bin/awg-panel manage enforce || true",
        "/usr/local/bin/awg-panel manage shape || true",
    ]


def test_hooks_naming_the_retired_cli_are_repointed_at_the_panel(conf_dir, clients_env):
    """Every server installed before the panel became the only writer has these
    two lines calling the retired CLI, and those commands are gone. Left alone
    they would fail on every bring-up: counters lost at each restart, revoked
    clients re-admitted at each boot, and nothing said about either."""
    legacy = (FIXTURES / "server-legacy-hooks.conf").read_text(encoding="utf-8")
    target = conf_dir / "awg0.conf"
    target.write_text(legacy, encoding="utf-8")
    assert LEGACY_PREDOWN_LINE in legacy and LEGACY_POSTUP_LINE in legacy

    store.add_client("phone")

    text = conf_text(target)
    assert LEGACY_PREDOWN_LINE not in text
    assert LEGACY_POSTUP_LINE not in text
    assert PREDOWN_LINE in text
    assert POSTUP_LINE in text
    # Rewritten where they stood, not appended beside the originals: two hooks
    # would both run, and the stale one would log a failure forever.
    interface = parse_conf(text).interface
    assert interface.all("PreDown") == ["/usr/local/bin/awg-panel manage trafficsync || true"]
    assert interface.all("PostUp")[0] == "/usr/local/bin/awg-panel manage enforce || true"
    assert sum("enforce" in line for line in interface.all("PostUp")) == 1


# ------------------------------------------ what the lock may and may not cover


def _lock_is_free() -> bool:
    """True if nothing holds the flock right now, seen from a second fd.

    The depth counter alone would not catch it: the point is whether another
    process waiting on the flock would get in.
    """
    handle = os.open(lock_file(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(handle)


def test_restart_does_not_hold_the_config_lock(server_conf):
    """awg-quick's own PreDown hook is `awg-panel manage trafficsync`, which takes
    this same flock. Holding it across the restart deadlocks the tunnel against
    its own hook - so this stays outside, and a future reader tidying the lock
    scope has a test that says why."""
    seen = []

    class Watcher:
        def restart(self) -> None:
            seen.append(_lock_is_free())

        def iface_up(self) -> bool:
            return False

    store.restart_iface(Watcher())

    assert seen == [True]


def test_the_endpoint_probe_happens_before_the_lock_is_taken(server_conf, clients_env, monkeypatch):
    """With no ENDPOINT_HOST the panel asks the EC2 metadata service, and on a
    box that black-holes that address it is six seconds of timeouts. Six
    seconds inside the lock is six seconds of every other worker and the
    collector queued behind it for no reason anyone could explain."""
    clientsenv.update_env({"ENDPOINT_HOST": ""})
    monkeypatch.setattr(store, "_detected", None)
    probed: list[bool] = []
    monkeypatch.setattr(
        store, "_http", lambda *_args, **_kwargs: probed.append(_lock_is_free()) or "198.51.100.7"
    )

    store.add_client("phone")

    assert probed and all(probed), probed


def test_moving_the_subnet_moves_a_split_tunnel_with_it(server_conf, clients_env):
    """A split tunnel is spelled as the tunnel network, so a client whose scope
    is exactly that is not stating a preference - it is saying "the tunnel", and
    the tunnel moved. Left behind, every one of them routes a range the server
    is no longer on and cannot reach the address it was just given."""
    clientsenv.update_env({"CLIENT_ALLOWED_IPS": "10.13.13.0/24"})
    store.add_client("phone")

    result = store.save_server({"subnet_cidr": "10.20.0.0/16"})

    assert clientsenv.read_env()["CLIENT_ALLOWED_IPS"] == "10.20.0.0/16"
    assert result["must_reimport"] is True
    assert result["resynced"] >= 1
    assert "AllowedIPs = 10.20.0.0/16" in store.client_conf_text("phone")


def test_a_scope_the_admin_chose_is_not_rewritten(server_conf, clients_env):
    """Only an exact match for the old network follows. Anything else is a route
    somebody decided on, and moving it would be the panel overruling them."""
    clientsenv.update_env({"CLIENT_ALLOWED_IPS": "0.0.0.0/0"})
    store.add_client("phone")
    store.update_client("phone", allowed_ips="192.168.50.0/24")

    store.save_server({"subnet_cidr": "10.20.0.0/16"})

    assert clientsenv.read_env()["CLIENT_ALLOWED_IPS"] == "0.0.0.0/0"
    assert "AllowedIPs = 192.168.50.0/24" in store.client_conf_text("phone")


def test_a_cached_failure_about_to_expire_is_refreshed_before_the_lock(
    server_conf, clients_env, monkeypatch
):
    """A warm-up that only checks "is it cached" defers, not settles: a failure
    expiring a second from now sails through and then expires while the request
    is queued on the flock, putting the whole probe back inside it."""
    clientsenv.update_env({"ENDPOINT_HOST": ""})
    monkeypatch.setattr(store, "_detected", (time.monotonic() + 1.0, None))
    probed: list[bool] = []
    monkeypatch.setattr(
        store, "_http", lambda *_a, **_kw: probed.append(_lock_is_free()) or "198.51.100.7"
    )

    store.add_client("phone")

    assert probed and all(probed), probed


# ------------------------------------------------------- the remembered parse


def _held():
    return getattr(store._parsed, "conf", None)


def _forget() -> None:
    """Start from cold. The parse is per thread and outlives one test's tmpdir."""
    store._parsed.conf = None


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every parse of the server config, which is what is being avoided."""
    parses: list[int] = []
    original = store.parse_conf

    def record(text):
        parses.append(len(text))
        return original(text)

    monkeypatch.setattr(store, "parse_conf", record)
    return parses


def test_a_second_mutation_does_not_parse_the_file_again(server_conf, monkeypatch):
    """The saving. Provisioning over the API is this five hundred times over, and
    every one of them was re-reading a file the last one had just written."""
    _forget()
    store.add_client("phone")
    parses = _count_parses(monkeypatch)

    store.set_client_enabled("phone", False)
    store.set_client_enabled("phone", True)

    assert parses == [], parses


def test_a_write_by_anything_else_is_read_from_disk(server_conf, monkeypatch):
    """The whole reason the parse is checked against a stat rather than trusted.

    Another worker, or an admin with an editor, writes this file while this
    process holds a parse of it, and a remembered parse that survived that would
    be the panel rewriting the config from a version that no longer exists -
    dropping whatever had been added.
    """
    _forget()
    store.add_client("phone")
    # As an edit outside this process would leave it: a peer it never parsed.
    server_conf.write_text(
        conf_text(server_conf) + "\n[Peer]\n# Client = added-elsewhere\n"
        f"PublicKey = {keys.pubkey(keys.genkey())}\nAllowedIPs = 10.13.13.9/32\n",
        encoding="utf-8",
    )
    parses = _count_parses(monkeypatch)

    store.set_client_enabled("phone", False)

    assert len(parses) == 1, parses
    assert "added-elsewhere" in conf_text(server_conf)
    assert {client.name for client in store.list_clients()} >= {"phone", "added-elsewhere"}


def test_what_is_remembered_is_what_is_on_disk(server_conf, conf_dir):
    """The invariant the whole thing rests on, over every mutation there is.

    _write_conf keeps the object it rendered rather than a copy of it, which is
    only sound while nothing edits that object afterwards. Every mutation today
    ends with the write and then reads - _announce, apply_live, the client
    files - but that is a property of the code rather than of the type, and an
    edit that added a mutation after the write would leave the remembered parse
    describing a config that was never written. Nothing else would notice.
    """
    _forget()

    def still_matches(what: str) -> None:
        held = _held()
        assert held is not None, f"{what}: nothing was remembered"
        assert render_conf(held[1]) == conf_text(server_conf), f"{what}: drifted from the file"
        assert held[0] == store._conf_stamp(server_conf), f"{what}: the stamp is not the file's"

    store.add_client("phone")
    still_matches("add_client")
    store.set_client_enabled("phone", False)
    still_matches("set_client_enabled")
    store.set_clients_enabled(["phone"], True)
    still_matches("set_clients_enabled")
    store.rename_client("phone", "work-phone")
    still_matches("rename_client")
    store.reset_client_keys("work-phone")
    still_matches("reset_client_keys")
    store.save_server({"listen_port": 51820})
    still_matches("save_server")
    store.remove_client("work-phone")
    still_matches("remove_client")
    store.add_client("second")
    store.remove_clients(["second"])
    still_matches("remove_clients")


def test_the_parse_handed_back_is_the_callers_own(server_conf):
    """Two callers editing one object is two callers editing each other's work,
    and a mutation that then failed would leave its edits in the next one's
    starting point."""
    _forget()
    store.add_client("phone")
    held = _held()
    assert held is not None

    mine = store._read_conf()
    mine.peers[-1].name = "scribbled"
    mine.peers.clear()
    mine.interface.set("ListenPort", "9999")

    assert held[1].peers[-1].name == "phone"
    assert held[1].interface.get("ListenPort") == "41234"
    assert render_conf(held[1]) == conf_text(server_conf)


def test_a_failed_mutation_leaves_nothing_behind(server_conf):
    """It edits the config in place before it writes, so what it leaves has to be
    the last thing that reached the file, not the last thing that was attempted."""
    _forget()
    store.add_client("phone")
    before = conf_text(server_conf)

    with pytest.raises(NotFound):
        store.set_client_enabled("ghost", False)

    assert conf_text(server_conf) == before
    held = _held()
    assert held is not None
    assert render_conf(held[1]) == before


def test_looking_up_many_names_agrees_with_looking_up_one(server_conf):
    """Which peer an operation lands on must not depend on how many were asked
    for. A hand edit is the only way to get two peers with one name, and both
    paths have to answer with the same one."""
    _forget()
    first = store.add_client("phone")
    store.add_client("laptop")
    server_conf.write_text(
        conf_text(server_conf) + "\n[Peer]\n# Client = phone\n"
        f"PublicKey = {keys.pubkey(keys.genkey())}\nAllowedIPs = 10.13.13.20/32\n",
        encoding="utf-8",
    )
    conf = store._read_conf()

    assert store._peers_by_name(conf)["phone"] is store._find_peer(conf, "phone")
    assert store._peers_by_name(conf)["phone"].public_key == first.public_key
