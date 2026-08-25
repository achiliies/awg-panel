"""The server config parser, against a capture of what install.sh actually writes.

fixtures/server.conf is not a hand-simplified sample: it is one real installer
run (10.13.13.0/24, port 41234, eth0) with the two peers a client add
appends afterwards. Everything here is about the one property the whole
compatibility story rests on - the panel may rewrite this file only where it
means to, because `awg-quick`, awg-menu and an admin's editor all read it.
"""

import difflib

import pytest

from awg import conf

# What `awg-quick strip awg0` produces for fixtures/server.conf: the host-side
# keys are gone (awg setconf rejects them), the comments are gone, and the
# disabled peer is gone with them - a revoked key must never reach the kernel.
EXPECTED_STRIP = """\
[Interface]
ListenPort = 41234
PrivateKey = eEm+CjQ0q0o3MCTDexoRJMv3A8wz9PVZVJbqj1yfXUU=
Jc = 4
Jmin = 40
Jmax = 70
S1 = 86
S2 = 574
S3 = 64
S4 = 16
H1 = 5-500000000
H2 = 500000001-1000000000
H3 = 1000000001-1500000000
H4 = 1500000001-2000000000
I1 = <r 2><b 0x85800001000100000000056170706c6503636f6d0000010001c00c0001000100002b960004e4569a55>
I2 = <b 0x1b00062d><r 44>
I3 = <r 2><b 0x818000010001000000000377777706676f6f676c6503636f6d0000010001c00c0001000100005a3e00044b23500c>
I4 = <b 0x000100002112a442><r 12>
I5 = <b 0xc00000000108><r 8><b 0x0000><r 32>
[Peer]
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ=
PresharedKey = a5PQ57ToVX91TckGUHDDDTfdQQu4UIFOB7Ew0kLPpm0=
AllowedIPs = 10.13.13.2/32
"""

ENABLED_PEER = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="
DISABLED_PEER = "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4="


def _diff(before: str, after: str) -> list[str]:
    """Changed lines only, as unified diff bodies ('-old', '+new')."""
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("---", "+++"))
    ]


def test_round_trip_is_byte_identical(server_conf_text: str) -> None:
    """The installer's output survives a parse/render cycle unchanged.

    Anything less and every panel write would show up as churn in a file the
    admin also edits over SSH.
    """
    assert conf.render_conf(conf.parse_conf(server_conf_text)) == server_conf_text


def test_base64_values_keep_their_padding(server_conf_text: str) -> None:
    """Values are split on the first '=' only: a base64 value is '='-padded."""
    parsed = conf.parse_conf(server_conf_text)
    assert parsed.interface.get("PrivateKey") == "eEm+CjQ0q0o3MCTDexoRJMv3A8wz9PVZVJbqj1yfXUU="
    assert parsed.peers[0].public_key == ENABLED_PEER
    assert parsed.peers[0].preshared_key.endswith("=")
    assert parsed.peers[1].public_key == DISABLED_PEER


def test_a_value_may_contain_equals_signs() -> None:
    """A hook is a shell command; splitting it twice would corrupt the firewall rules."""
    text = "[Interface]\nPostUp = sysctl -w net.ipv4.ip_forward=1\n"
    parsed = conf.parse_conf(text)
    assert parsed.interface.get("PostUp") == "sysctl -w net.ipv4.ip_forward=1"
    assert conf.render_conf(parsed) == text


def test_repeated_hooks_keep_their_order(server_conf_text: str) -> None:
    """PostDown undoes PostUp in reverse; a reordered rule set leaks or blocks traffic."""
    section = conf.parse_conf(server_conf_text).interface
    assert section.all("PostUp") == [
        # The tunnel's own hooks, kept ahead of the firewall rules by awg.store.
        "/usr/local/bin/awg-panel manage enforce || true",
        "/usr/local/bin/awg-panel manage shape || true",
        "iptables -t nat -A POSTROUTING -s 10.13.13.0/24 -o eth0 -j MASQUERADE",
        "iptables -A FORWARD -i %i -j ACCEPT",
        "iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]
    assert section.all("PostDown") == [
        "iptables -t nat -D POSTROUTING -s 10.13.13.0/24 -o eth0 -j MASQUERADE",
        "iptables -D FORWARD -i %i -j ACCEPT",
        "iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        # ...and last on the way down, where awg.store keeps this one.
        "/usr/local/bin/awg-panel manage shape --detach || true",
    ]
    assert section.all("PreDown") == ["/usr/local/bin/awg-panel manage trafficsync || true"]


def test_unknown_keys_and_comments_survive() -> None:
    """A newer awg, or an admin's note, must not be deleted by a panel save."""
    text = (
        "[Interface]\n"
        "# provisioned by hand on 2026-08-04\n"
        "ListenPort = 41234\n"
        "SomeFutureOption = 7\n"
        "\n"
        "[Peer]\n"
        "# Client = client1\n"
        "# renewed after the laptop was reimaged\n"
        "PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ=\n"
        "AllowedIPs = 10.13.13.2/32\n"
        "SomeFuturePeerOption = yes\n"
    )
    parsed = conf.parse_conf(text)
    assert parsed.interface.get("SomeFutureOption") == "7"
    assert parsed.peers[0].extra == [("SomeFuturePeerOption", "yes")]
    assert parsed.peers[0].extra_comments == ["# renewed after the laptop was reimaged"]
    assert conf.render_conf(parsed) == text


def test_unnamed_peer_survives_a_rewrite(server_conf_text: str) -> None:
    """The second peer has no '# Client =' line. The panel shows it as (unnamed)
    and its address is still allocated, so dropping it would hand that address out
    twice."""
    parsed = conf.parse_conf(server_conf_text)
    assert parsed.peers[1].name is None
    assert parsed.peers[1].allowed_ips == "10.13.13.3/32"
    assert parsed.peers[1].disabled_at == "2026-08-05T09:30:00Z"
    assert conf.render_conf(parsed) == server_conf_text


def test_renaming_a_peer_touches_one_line(server_conf_text: str) -> None:
    parsed = conf.parse_conf(server_conf_text)
    parsed.peers[0].name = "laptop"
    assert _diff(server_conf_text, conf.render_conf(parsed)) == [
        "-# Client = client1",
        "+# Client = laptop",
    ]


def test_disabling_a_peer_adds_one_line(server_conf_text: str) -> None:
    """Disable is a comment, so the bash tools keep reading the file the same way."""
    parsed = conf.parse_conf(server_conf_text)
    parsed.peers[0].disabled_at = "2026-08-06T10:00:00Z"
    assert _diff(server_conf_text, conf.render_conf(parsed)) == [
        "+# Disabled = 2026-08-06T10:00:00Z",
    ]


def test_removing_a_peer_leaves_the_rest_untouched(server_conf_text: str) -> None:
    parsed = conf.parse_conf(server_conf_text)
    del parsed.peers[0]
    rendered = conf.render_conf(parsed)
    assert ENABLED_PEER not in rendered
    # Only the removed block is gone; the interface and the other peer are as they were.
    assert [line for line in _diff(server_conf_text, rendered) if line.startswith("+")] == []
    assert rendered == server_conf_text.replace(
        "\n[Peer]\n"
        "# Client = client1\n"
        "# Created = 2026-08-04T18:00:00Z\n"
        f"PublicKey = {ENABLED_PEER}\n"
        "PresharedKey = a5PQ57ToVX91TckGUHDDDTfdQQu4UIFOB7Ew0kLPpm0=\n"
        "AllowedIPs = 10.13.13.2/32\n",
        "",
    )


def test_changing_an_interface_value_touches_one_line(server_conf_text: str) -> None:
    """Editing MTU must not reflow the imitation packets or the blank lines around them."""
    parsed = conf.parse_conf(server_conf_text)
    parsed.interface.set("MTU", "1380")
    assert _diff(server_conf_text, conf.render_conf(parsed)) == ["-MTU = 1400", "+MTU = 1380"]


def test_changing_a_duplicated_interface_value_leaves_one_line(server_conf_text: str) -> None:
    """A hand-edited config can carry a parameter twice, and wg reads the last one.

    Rewriting only the first left the second behind: the panel showed the value
    it had written, the interface came up with a value nobody had asked for, and
    nothing anywhere said the two disagreed.
    """
    doubled = server_conf_text.replace("MTU = 1400", "MTU = 1400\nMTU = 1280", 1)
    parsed = conf.parse_conf(doubled)
    parsed.interface.set("MTU", "1380")
    rendered = conf.render_conf(parsed)

    assert conf.parse_conf(rendered).interface.all("MTU") == ["1380"]
    assert _diff(doubled, rendered) == ["-MTU = 1400", "-MTU = 1280", "+MTU = 1380"]


def test_setting_a_key_keeps_the_first_position(server_conf_text: str) -> None:
    """The collapse must not move the setting to the end of the section."""
    doubled = server_conf_text.replace("MTU = 1400", "MTU = 1400\nMTU = 1280", 1)
    parsed = conf.parse_conf(doubled)
    parsed.interface.set("MTU", "1380")
    keys = [key for key, _ in parsed.interface.items()]
    original = [key for key, _ in conf.parse_conf(server_conf_text).interface.items()]
    assert keys == original


def test_strip_is_what_awg_setconf_accepts(server_conf_text: str) -> None:
    assert conf.strip_conf(conf.parse_conf(server_conf_text)) == EXPECTED_STRIP


def test_strip_excludes_disabled_peers(server_conf_text: str) -> None:
    stripped = conf.strip_conf(conf.parse_conf(server_conf_text))
    assert ENABLED_PEER in stripped
    assert DISABLED_PEER not in stripped


def test_strip_excludes_named_keys(server_conf_text: str) -> None:
    """The caller can revoke more than the config knows about, e.g. an over-quota client."""
    stripped = conf.strip_conf(conf.parse_conf(server_conf_text), [ENABLED_PEER])
    assert ENABLED_PEER not in stripped
    assert "[Peer]" not in stripped


def test_strip_drops_wg_quick_only_keys(server_conf_text: str) -> None:
    stripped = conf.strip_conf(conf.parse_conf(server_conf_text))
    for key in ("Address", "MTU", "PreDown", "PostUp", "PostDown"):
        assert f"{key} = " not in stripped
    assert "#" not in stripped


# -------------------------------------------------- annotated section headers


ANNOTATED = """[Interface]  # the server
Address = 10.13.13.1/24
PrivateKey = uJ5MJ2mFcnKfKAWZ2vhLXDSAFXCyJXFxwvJKq3RvSVA=

[Peer]
# Client = laptop
PublicKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
AllowedIPs = 10.13.13.2/32

[Peer] # phone
# Client = phone
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
AllowedIPs = 10.13.13.3/32
"""


def test_a_commented_peer_header_still_opens_a_peer() -> None:
    """`awg-quick` cuts every line at the first '#' before looking for a bracket,
    and the /^\\[Peer\\]/ awk in lib/conf.sh matches on the prefix, so both of
    them read "[Peer] # phone" as a section. Reading it as an ordinary line
    folded the block into the peer above it, and the next write dropped one of
    the two clients - silently, and with its address still reserved."""
    parsed = conf.parse_conf(ANNOTATED)

    assert [peer.name for peer in parsed.peers] == ["laptop", "phone"]
    assert parsed.peers[0].allowed_ips == "10.13.13.2/32"
    assert parsed.peers[1].allowed_ips == "10.13.13.3/32"


def test_an_annotated_header_survives_the_round_trip() -> None:
    """Recognising the line is only half of it: normalising the comment away
    would rewrite a hand-edited config under its author on the next save."""
    assert conf.render_conf(conf.parse_conf(ANNOTATED)) == ANNOTATED


def test_the_kernel_is_given_canonical_headers() -> None:
    """`awg setconf` has no use for the annotation, so strip does not pass it on."""
    stripped = conf.strip_conf(conf.parse_conf(ANNOTATED))

    assert stripped.count("[Peer]") == 2
    assert "# phone" not in stripped
    assert stripped.startswith("[Interface]\n")


@pytest.mark.parametrize(
    "header", ["[Peer]#phone", "[Peer]   # phone", "  [Peer] # phone", "[Peer]\t# phone"]
)
def test_the_spacing_around_the_annotation_does_not_matter(header: str) -> None:
    """awg-quick cuts at the first '#' and then trims whitespace, so all of these
    are the same line to it and have to be the same line here."""
    parsed = conf.parse_conf(ANNOTATED.replace("[Peer] # phone", header))

    assert [peer.name for peer in parsed.peers] == ["laptop", "phone"]
