"""X25519 keys, byte-compatible with `awg genkey` / `awg pubkey` / `awg genpsk`.

The panel generates keys in-process instead of shelling out, so adding a client
works on a box where the tools are not on PATH yet (fresh install, --standalone,
AWG_MOCK) and never puts a private key on a command line where ps could read it.

Clamping: `awg genkey` masks the raw bytes (`k[0] &= 248; k[31] = (k[31] & 127)
| 64`) before base64-encoding them, so every key the CLI ever wrote has that
shape. cryptography does not clamp on import - OpenSSL clamps a copy of the
scalar inside the multiplication - which means an unclamped key derives the same
public key as its clamped form, and a key from either source is accepted by both
tools. We still clamp on generation so keys written by the panel and by the CLI
are indistinguishable on disk.

Nothing here logs, formats or raises a message containing key material.
"""

import base64
import binascii
import secrets

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .errors import ValidationError

KEY_BYTES = 32
KEY_B64_LEN = 44


def genkey() -> str:
    """Return a new base64 X25519 private key."""
    private = X25519PrivateKey.from_private_bytes(_clamp(secrets.token_bytes(KEY_BYTES)))
    raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return base64.b64encode(raw).decode("ascii")


def pubkey(private_b64: str) -> str:
    """Derive the base64 public key for a base64 private key."""
    private = X25519PrivateKey.from_private_bytes(_decode(private_b64, "private key"))
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def genpsk() -> str:
    """Return a new base64 32-byte preshared key."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def is_key(value: str) -> bool:
    """True if value is base64 for exactly 32 bytes, the shape awg accepts."""
    if not isinstance(value, str) or len(value) != KEY_B64_LEN or not value.endswith("="):
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == KEY_BYTES


def _clamp(raw: bytes) -> bytes:
    key = bytearray(raw)
    key[0] &= 248
    key[31] = (key[31] & 127) | 64
    return bytes(key)


def _decode(value: str, what: str) -> bytes:
    # The offending value is deliberately absent from the message: it is a key.
    if not is_key(value):
        raise ValidationError(f"{what} must be 44 base64 characters decoding to 32 bytes")
    return base64.b64decode(value, validate=True)
