"""Swapping the host in a client config on its way out of the panel.

The layer has one job and one rule: change the host in the Endpoint line, change
nothing else. The rule is what the assertions here are about, because a config
is imported by an app that will not say what it disliked - it simply fails to
connect - so anything this rewrites and should not have is a fault that surfaces
on somebody's phone.

No Django: the module reads a certificate and a mapping of settings, and nothing
else, which is what lets it be checked this cheaply.
"""

import pytest

from apps.panel import endpoint

CONFIG = """\
[Interface]
PrivateKey = qCOkQo0ROacEtytL5yOJl5jX88NlRppfkcn1fo2wJ3g=
Address = 10.13.13.4/32
DNS = 8.8.8.8, 8.8.4.4
MTU = 1400

Jc = 4
S1 = 86

[Peer]
PublicKey = 0ZoQvCkV/RTLnAmRZHtjPAqmDGrmU8lHFLHRoAeOwlA=
PresharedKey = elstKOnuCKHgq/aWZnUvVHhujFKUOwhOrxEMJSHVwMw=
Endpoint = 203.0.113.10:41234
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""


# ------------------------------------------------------------------- rewrite


def test_the_host_changes_and_the_port_stays():
    out = endpoint.rewrite(CONFIG, "vpn.example.com")
    assert "Endpoint = vpn.example.com:41234" in out


def test_nothing_else_in_the_config_moves():
    """One line differs. Everything else is key material and obfuscation."""
    before = CONFIG.splitlines()
    after = endpoint.rewrite(CONFIG, "vpn.example.com").splitlines()
    assert len(before) == len(after)
    differing = [old for old, new in zip(before, after, strict=True) if old != new]
    assert differing == ["Endpoint = 203.0.113.10:41234"]


def test_no_host_is_a_pass_through():
    """The switch being off must cost the download nothing at all, byte for byte."""
    assert endpoint.rewrite(CONFIG, "") is CONFIG


def test_an_ipv6_endpoint_keeps_its_brackets_and_its_port():
    """Every colon but the last belongs to the address."""
    text = "[Peer]\nEndpoint = [2001:db8::1]:51820\n"
    assert endpoint.rewrite(text, "vpn.example.com") == "[Peer]\nEndpoint = vpn.example.com:51820\n"


def test_an_endpoint_with_no_port_is_left_alone():
    """Without a port there is no way to tell which half is the host."""
    text = "[Peer]\nEndpoint = 203.0.113.10\n"
    assert endpoint.rewrite(text, "vpn.example.com") == text


def test_a_line_that_is_not_an_endpoint_is_left_alone():
    """AllowedIPs and Address carry colons and slashes too, and neither is a host."""
    text = "Address = 10.13.13.4/32\nAllowedIPs = 0.0.0.0/0\nDNS = 8.8.8.8\n"
    assert endpoint.rewrite(text, "vpn.example.com") == text


def test_the_spacing_of_the_line_survives():
    text = "Endpoint=203.0.113.10:41234\n"
    assert endpoint.rewrite(text, "vpn.example.com") == "Endpoint=vpn.example.com:41234\n"


def test_every_endpoint_in_the_text_is_swapped():
    """The export archive is one call per file, but a config could carry two peers."""
    text = "Endpoint = 203.0.113.10:41234\nEndpoint = 203.0.113.11:41234\n"
    out = endpoint.rewrite(text, "vpn.example.com")
    assert out.count("vpn.example.com:41234") == 2


# ---------------------------------------------------------------- what to use


def test_the_mode_defaults_to_leaving_the_config_alone():
    """An install that never opened the setting must download what it always did."""
    assert endpoint.wanted_host({}) == ""


def test_domain_mode_takes_the_name_off_the_certificate(tmp_path, make_certificate):
    cert, _, _ = make_certificate(tmp_path, "vpn.example.com")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(cert)}
    assert endpoint.wanted_host(values) == "vpn.example.com"


def test_a_pinned_host_wins_over_the_certificate(tmp_path, make_certificate):
    cert, _, _ = make_certificate(tmp_path, "panel.example.com")
    values = {
        "configEndpointMode": "domain",
        "configEndpointHost": "vpn.example.com",
        "tlsCertPath": str(cert),
    }
    assert endpoint.wanted_host(values) == "vpn.example.com"


def test_a_wildcard_is_not_an_address_anyone_can_dial(tmp_path, make_certificate):
    """A browser matches *.example.com; a client cannot connect to it."""
    cert, _, _ = make_certificate(tmp_path, "*.example.com", "vpn.example.com")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(cert)}
    assert endpoint.wanted_host(values) == "vpn.example.com"


def test_a_certificate_of_wildcards_only_leaves_the_config_alone(tmp_path, make_certificate):
    cert, _, _ = make_certificate(tmp_path, "*.example.com")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(cert)}
    assert endpoint.wanted_host(values) == ""


def test_an_address_certificate_has_no_name_to_swap_in(tmp_path, make_certificate):
    """Let's Encrypt issues for addresses, and such a certificate names no name.

    The address on it is the address the config already carries, so substituting
    it swaps a host for itself. Answering "" keeps the download byte-identical
    and keeps the page from calling an address a domain and asking for one.
    """
    cert, _, _ = make_certificate(tmp_path, "198.51.100.7")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(cert)}
    assert endpoint.wanted_host(values) == ""


def test_a_name_beside_an_address_is_still_handed_out(tmp_path, make_certificate):
    """One certificate can carry both, and only one half is dialable as a name."""
    cert, _, _ = make_certificate(tmp_path, "198.51.100.7", "vpn.example.com")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(cert)}
    assert endpoint.wanted_host(values) == "vpn.example.com"


def test_a_pinned_host_still_wins_over_an_address_certificate(tmp_path, make_certificate):
    """The certificate has no say in this: the operator typed the name to use."""
    cert, _, _ = make_certificate(tmp_path, "198.51.100.7")
    values = {
        "configEndpointMode": "domain",
        "configEndpointHost": "vpn.example.com",
        "tlsCertPath": str(cert),
    }
    assert endpoint.wanted_host(values) == "vpn.example.com"


@pytest.mark.parametrize("path", ["", "/nowhere/panel.crt"])
def test_domain_mode_without_a_readable_certificate_falls_back(path):
    """The certificate can be replaced or removed long after the setting was saved.

    Raising here would take out the panel's own downloads over a preference, so
    the config goes out with the address it already carries.
    """
    assert endpoint.wanted_host({"configEndpointMode": "domain", "tlsCertPath": path}) == ""


def test_an_unparsable_certificate_is_the_same_as_none(tmp_path):
    broken = tmp_path / "panel.crt"
    broken.write_text("-- not really a certificate --\n", encoding="utf-8")
    values = {"configEndpointMode": "domain", "tlsCertPath": str(broken)}
    assert endpoint.wanted_host(values) == ""


def test_ip_mode_ignores_a_pinned_host(tmp_path):
    """The host is remembered while the switch is off, not applied."""
    values = {"configEndpointMode": "ip", "configEndpointHost": "vpn.example.com"}
    assert endpoint.wanted_host(values) == ""
