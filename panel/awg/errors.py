"""Error types shared by the whole panel.

Every one of these carries a message that is shown to the user as-is, so the
text must be an actionable sentence, never a bare code. The DRF exception
handler maps them to HTTP status codes.
"""

import re
from collections.abc import Mapping, Sequence

# A base64 X25519 key or PSK: 43 payload chars plus the '=' pad. Command lines
# are echoed back in error messages, and a key must never reach a log or an
# API response.
_KEYLIKE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


class AwgError(Exception):
    """Base class for every expected failure. str() is user-facing."""


class LockTimeout(AwgError):
    """The config lock was held by another writer for longer than we waited."""


class NotFound(AwgError):
    """A client, peer or file the caller named does not exist."""


class NotConfigured(AwgError):
    """There is no server config to work with.

    Distinct from every other failure because it is not a fault: the panel can
    be installed with --standalone before the VPN, AWG_IFACE can name an
    interface that was never created, and a fresh container has an empty
    /etc/amnezia. All three are states the UI should explain, not report as a
    crash, so this maps to 503 rather than 500 and is logged without a
    traceback.
    """


# The `code` a Conflict about a name in use carries, and the only one there is.
#
# Two very different refusals share the 409: the name is taken, and the address
# pool is full. They read alike to anything branching on the status, and the
# panel does branch - a name it suggested and the server would not take is
# replaced with another on the spot, which would be a nonsense answer to a
# server that has simply run out of addresses. The wording says which, but
# wording is for people, so the machine-readable half is this.
NAME_IN_USE = "name_in_use"


class Conflict(AwgError):
    """The request collides with existing state: duplicate name, IPs exhausted.

    ``code`` is empty for most of those and NAME_IN_USE for the one the panel
    acts on differently; awgui.exceptions puts it in the response body beside
    the sentence, exactly as it does for a panel with no server config.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ValidationError(AwgError):
    """One or more input fields are invalid.

    ``errors`` maps field name to a human message; it is serialised into the
    API response so the UI can attach each message to its own field.
    """

    def __init__(
        self,
        errors: Mapping[str, str] | str | None = None,
        message: str | None = None,
    ) -> None:
        if isinstance(errors, str):
            message, errors = errors, None
        self.errors: dict[str, str] = dict(errors or {})
        if message is None:
            message = _join_errors(self.errors) or "invalid input"
        super().__init__(message)


class ToolError(AwgError):
    """A subprocess (awg, awg-quick, ip, systemctl) failed."""

    def __init__(
        self,
        cmd: Sequence[str] | str,
        stderr: str = "",
        returncode: int | None = None,
        message: str | None = None,
    ) -> None:
        self.cmd: list[str] = [cmd] if isinstance(cmd, str) else list(cmd)
        self.stderr: str = (stderr or "").strip()
        self.returncode: int | None = returncode
        if message is None:
            shown = " ".join("<redacted>" if _KEYLIKE.match(a) else a for a in self.cmd)
            message = f"{shown} failed"
            if returncode is not None:
                message += f" (exit {returncode})"
            if self.stderr:
                message += f": {self.stderr.splitlines()[0]}"
        super().__init__(message)


def _join_errors(errors: Mapping[str, str]) -> str:
    return "; ".join(f"{field}: {msg}" for field, msg in errors.items())
