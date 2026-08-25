"""X25519 key generation, checked against the tool that has to accept the output.

The panel derives keys in-process instead of shelling out to `awg genkey`, which
means nothing else verifies that the two agree. If they ever diverge the failure
is silent and total: a client config is issued, imported, and simply never
completes a handshake. So the shapes are pinned here, and where the real binary
is available the derivation is cross-checked against it.

No test in this file prints or asserts on a message containing key material.
"""

import base64
import shutil
import subprocess

import pytest

from awg import keys
from awg.errors import ValidationError

# A clamped private key and the public key `awg pubkey` derives from it. Pinned
# rather than generated: a golden vector catches a change in the derivation that
# a round-trip test would happily agree with.
PRIVATE = "SMHDaqUpBCB0LJvMCyWkm0aCzWTOMNM3ORNqRTaHY2Y="
PUBLIC = "DTOwbVLn1SHn0/x3itkaOtiuAW6VJ7IW9NhR077XEXo="

HAVE_AWG = shutil.which("awg") is not None


def raw(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


# -------------------------------------------------------------------- shapes


def test_genkey_shape():
    key = keys.genkey()

    assert len(key) == 44
    assert key.endswith("=")
    assert len(raw(key)) == 32
    assert keys.is_key(key)


def test_genkey_is_clamped_the_way_awg_genkey_clamps():
    """awg masks the scalar before encoding it, so every key it ever wrote has
    this shape. Ours must be indistinguishable from one written by the CLI."""
    for _ in range(20):
        scalar = raw(keys.genkey())
        assert scalar[0] & 0b111 == 0
        assert scalar[31] & 0b1100_0000 == 0b0100_0000


def test_genkey_is_not_repeating_itself():
    assert len({keys.genkey() for _ in range(64)}) == 64


def test_pubkey_shape():
    key = keys.pubkey(keys.genkey())

    assert len(key) == 44
    assert key.endswith("=")
    assert len(raw(key)) == 32


def test_genpsk_shape():
    psk = keys.genpsk()

    assert len(psk) == 44
    assert len(raw(psk)) == 32
    assert keys.is_key(psk)
    assert len({keys.genpsk() for _ in range(64)}) == 64


# -------------------------------------------------------------- determinism


def test_pubkey_matches_the_pinned_vector():
    assert keys.pubkey(PRIVATE) == PUBLIC


def test_pubkey_is_deterministic():
    """Peers are matched by public key everywhere - the traffic db, the client
    metadata table, resync's key check - so the derivation cannot drift."""
    first = keys.pubkey(PRIVATE)
    assert all(keys.pubkey(PRIVATE) == first for _ in range(10))


def test_distinct_private_keys_give_distinct_public_keys():
    private = [keys.genkey() for _ in range(32)]
    assert len({keys.pubkey(value) for value in private}) == 32


# ------------------------------------------------------------------ is_key


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "SMHDaqUpBCB0LJvMCyWkm0aCzWTOMNM3ORNqRTaHY2Y",  # padding stripped
        "SMHDaqUpBCB0LJvMCyWkm0aCzWTOMNM3ORNqRTaHY2Y==",  # 45 chars
        "aGVsbG8=",  # decodes to 5 bytes
        "!" * 44,
        "SMHDaqUpBCB0LJvMCyWkm0aCzWTOMNM3ORNqRTaHY2Y ",
        None,
        1234,
    ],
)
def test_is_key_rejects_anything_that_is_not_a_32_byte_key(value):
    assert keys.is_key(value) is False


def test_is_key_accepts_generated_material():
    assert keys.is_key(keys.genkey())
    assert keys.is_key(keys.genpsk())
    assert keys.is_key(keys.pubkey(PRIVATE))


def test_pubkey_rejects_junk_without_echoing_it():
    secret = "not-a-key-but-still-secret-looking-abcdefg"
    with pytest.raises(ValidationError) as caught:
        keys.pubkey(secret)
    assert secret not in str(caught.value)


# ------------------------------------------------- cross-check against awg


@pytest.mark.skipif(not HAVE_AWG, reason="the awg tools are not installed on this machine")
def test_pubkey_agrees_with_the_awg_binary():
    """The only test that proves the two implementations produce the same key.

    Everything else here is self-consistent, which is exactly what a wrong but
    stable derivation would also be.
    """
    for private in [PRIVATE, *(keys.genkey() for _ in range(8))]:
        proc = subprocess.run(
            ["awg", "pubkey"],
            input=private,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert proc.stdout.strip() == keys.pubkey(private)


@pytest.mark.skipif(not HAVE_AWG, reason="the awg tools are not installed on this machine")
def test_awg_accepts_a_key_generated_here():
    """`awg pubkey` re-encodes what it reads, so a key it hands back unchanged is
    one it parsed as a valid 32-byte scalar."""
    generated = keys.genkey()
    proc = subprocess.run(
        ["awg", "pubkey"],
        input=generated,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert keys.is_key(proc.stdout.strip())
    assert proc.stdout.strip() == keys.pubkey(generated)
