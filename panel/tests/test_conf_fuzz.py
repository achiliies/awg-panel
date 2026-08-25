"""Property tests for the config parser.

awg0.conf is read by `awg-quick` and awg-menu and gets hand-edited on
real servers, so the parser meets input nobody designed for: CRLF endings from a
Windows editor, a peer whose keys are in an unusual order, a stray section, a
value containing '='. None of that may raise, lose a peer, or hand the kernel a
key that is supposed to be revoked - if it did, the panel would quietly break a
VPN that was working a minute earlier.

Deliberately self-contained: no fixtures, so these keep running even if the
shared conftest changes.
"""

import random
import string

import pytest

from awg import conf

B64 = string.ascii_letters + string.digits + "+/"


def _key(rng: random.Random) -> str:
    """A plausible 44-character base64 key, padding included."""
    return "".join(rng.choice(B64) for _ in range(43)) + "="


def _random_conf(rng: random.Random) -> str:
    """A config assembled from the shapes real servers actually contain."""
    lines = ["[Interface]"]
    for key, value in [
        ("Address", f"10.{rng.randrange(256)}.13.1/24"),
        ("ListenPort", str(rng.randrange(1024, 65535))),
        ("PrivateKey", _key(rng)),
        ("MTU", str(rng.choice([1280, 1400, 1420]))),
    ]:
        lines.append(f"{key} = {value}")
    if rng.random() < 0.7:
        lines.append("")
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4"):
        if rng.random() < 0.8:
            lines.append(f"{key} = {rng.randrange(0, 600)}")
    for key in ("H1", "H2", "H3", "H4"):
        if rng.random() < 0.8:
            low = rng.randrange(5, 10**9)
            lines.append(f"{key} = {low}-{low + rng.randrange(1, 10**8)}")
    for key in ("I1", "I2", "I3", "I4", "I5"):
        if rng.random() < 0.5:
            blob = "".join(rng.choice("0123456789abcdef") for _ in range(rng.randrange(4, 40) * 2))
            lines.append(f"{key} = <r {rng.randrange(1, 60)}><b 0x{blob}>")
    if rng.random() < 0.5:
        lines.append("PreDown = /usr/local/bin/awg-panel manage trafficsync || true")
    for _ in range(rng.randrange(0, 4)):
        lines.append("PostUp = iptables -A FORWARD -i %i -j ACCEPT")
        lines.append("PostDown = iptables -D FORWARD -i %i -j ACCEPT")

    for index in range(rng.randrange(0, 8)):
        lines.append("")
        lines.append("[Peer]")
        # An unnamed peer is what an operator gets when they paste a block in by
        # hand. It still owns an address, so it must survive every rewrite.
        if rng.random() < 0.8:
            lines.append(f"# Client = peer{index}")
        if rng.random() < 0.6:
            lines.append("# Created = 2026-08-04T18:00:00Z")
        if rng.random() < 0.2:
            lines.append("# Disabled = 2026-08-05T09:00:00Z")
        if rng.random() < 0.15:
            lines.append("# a note somebody left here")
        block = [f"PublicKey = {_key(rng)}"]
        if rng.random() < 0.9:
            block.append(f"PresharedKey = {_key(rng)}")
        block.append(f"AllowedIPs = 10.13.13.{index + 2}/32")
        if rng.random() < 0.2:
            block.append(f"Endpoint = 198.51.100.{index + 1}:{rng.randrange(1024, 65535)}")
        if rng.random() < 0.3:
            block.append("PersistentKeepalive = 25")
        lines.extend(block)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("seed", range(200))
def test_round_trip_is_lossless(seed: int) -> None:
    text = _random_conf(random.Random(seed))
    assert conf.render_conf(conf.parse_conf(text)) == text


@pytest.mark.parametrize("seed", range(200))
def test_reparse_is_stable(seed: int) -> None:
    """Parsing what we rendered gives the same peers back.

    Byte equality is the strong property; this is the one that still has to hold
    for inputs where it does not, such as a file with no blank line before a
    peer block.
    """
    text = _random_conf(random.Random(seed))
    first = conf.parse_conf(text)
    second = conf.parse_conf(conf.render_conf(first))
    assert [p.public_key for p in first.peers] == [p.public_key for p in second.peers]
    assert [p.name for p in first.peers] == [p.name for p in second.peers]
    assert [p.allowed_ips for p in first.peers] == [p.allowed_ips for p in second.peers]
    assert [p.disabled_at for p in first.peers] == [p.disabled_at for p in second.peers]
    assert first.interface.get("PrivateKey") == second.interface.get("PrivateKey")
    assert first.interface.all("PostUp") == second.interface.all("PostUp")


@pytest.mark.parametrize("seed", range(100))
def test_disabled_peers_never_reach_the_kernel(seed: int) -> None:
    text = _random_conf(random.Random(seed))
    parsed = conf.parse_conf(text)
    stripped = conf.strip_conf(parsed)
    for peer in parsed.peers:
        if peer.disabled_at is not None:
            assert peer.public_key not in stripped
        else:
            assert peer.public_key in stripped


@pytest.mark.parametrize("seed", range(100))
def test_strip_drops_only_wg_quick_keys(seed: int) -> None:
    """`awg setconf` rejects a key it does not know, so the filter has to be exact."""
    stripped = conf.strip_conf(conf.parse_conf(_random_conf(random.Random(seed))))
    for line in stripped.splitlines():
        if not line or line.startswith("["):
            continue
        key = line.split("=", 1)[0].strip().casefold()
        assert key not in {
            "address",
            "dns",
            "mtu",
            "table",
            "preup",
            "postup",
            "predown",
            "postdown",
            "saveconfig",
        }
        assert not line.lstrip().startswith("#")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "[Interface]\n",
        "[Peer]\nPublicKey = abc\n",
        "no sections at all\n",
        "[Interface]\n= novalue\n",
        "[Interface]\nBrokenLineWithoutEquals\n",
        "[Interface]\nPrivateKey=nospaces\n",
        "[Interface]\n   PrivateKey   =   padded   \n",
        "[Interface]\n[Interface]\nListenPort = 1\n",
        "[Unknown]\nfoo = bar\n",
        "[Interface]\nPrivateKey = aGk=\n\n[Peer]\n# Client = x\n",
        "[Interface]\r\nListenPort = 51820\r\n",
        "[Interface]\nDNS = 1.1.1.1, 1.0.0.1\n",
        "[Interface]\nPostUp = echo a=b=c\n",
    ],
)
def test_degenerate_input_does_not_raise(text: str) -> None:
    parsed = conf.parse_conf(text)
    rendered = conf.render_conf(parsed)
    conf.parse_conf(rendered)  # and the result is parseable again
    conf.strip_conf(parsed)


def test_value_containing_equals_survives() -> None:
    """awk splits on the first '=' only; base64 padding and shell hooks depend on it."""
    text = (
        "[Interface]\n"
        "PrivateKey = QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=\n"
        "PostUp = sh -c 'x=1; y=2'\n"
        "\n"
        "[Peer]\n"
        "# Client = a\n"
        "PublicKey = MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3BxcnN0dXY=\n"
        "AllowedIPs = 10.13.13.2/32\n"
    )
    parsed = conf.parse_conf(text)
    assert parsed.interface.get("PrivateKey") == "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY="
    assert parsed.interface.get("PostUp") == "sh -c 'x=1; y=2'"
    assert parsed.peers[0].public_key.endswith("=")
    assert conf.render_conf(parsed) == text


def test_unnamed_peer_keeps_its_address() -> None:
    """A hand-pasted peer has no '# Client =' line. Losing it would free an address
    the panel would then hand to somebody else."""
    text = (
        "[Interface]\n"
        "ListenPort = 51820\n"
        "\n"
        "[Peer]\n"
        "PublicKey = MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3BxcnN0dXY=\n"
        "AllowedIPs = 10.13.13.7/32\n"
    )
    parsed = conf.parse_conf(text)
    assert len(parsed.peers) == 1
    assert parsed.peers[0].name is None
    assert parsed.peers[0].allowed_ips == "10.13.13.7/32"
    assert conf.render_conf(parsed) == text
