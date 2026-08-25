"""clients.env - the client-facing defaults, in shell assignment syntax.

The syntax is what it is because bash used to source this file with a plain `.`.
Nothing does any more, and the format stays anyway: install.sh still writes it,
an admin still edits it over SSH, and a settings file whose spelling changed
under an upgrade is a settings file that reads as corrupted.

So reading it means unquoting bash words, not parsing INI: install.sh writes
`ENDPOINT_PORT=""            # blank = ListenPort from the server config`, so
every value is quoted and most lines carry an aligned trailing comment.

Writing it back has to be lossless. install.sh edits it with
`sed -i "s|^KEY=.*|KEY=\\"new\\"|"`, which throws the inline comment away; the
panel rewrites only the value and puts the original comment back at the same
column, so a file edited from the web looks like one edited by hand and the
admin does not lose the hints that explain each setting.

No variable expansion, command substitution or arithmetic is performed. A line
this module cannot read as a simple assignment is copied through untouched and
contributes no value, so a hand-written oddity can never be silently rewritten
into something bash would read differently.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .conf import no_line_break
from .errors import AwgError, ValidationError
from .lock import config_lock
from .paths import atomic_write, env_file

# What a missing or partial file means. These only ever get used on a server
# whose clients.env was deleted or truncated, and what they have to be is the
# value install.sh would have written into the line that is missing - a fallback
# that quietly differs from it hands the next client something no server was
# ever set up with. CLIENT_DNS said 1.1.1.1 and CLIENT_MTU said 1420 against an
# installer writing 8.8.8.8 and 1400, and the comment here asserted they agreed,
# which is how it went unnoticed.
#
# CLIENT_ALLOWED_IPS is the one that cannot simply mirror it: the installer adds
# ::/0 when the tunnel carries IPv6, and nothing readable from this file says
# whether it does. It pairs with SUBNET6_MODE below, which defaults to "no IPv6"
# for the same reason, so the two agree about a truncated file.
#
# SUBNET_CIDR and SUBNET_BASE both default to blank rather than to a network:
# the tunnel subnet is the server's own Address, and these two only mirror it.
# A default here would win over an older file's customised SUBNET_BASE and
# quietly move the allocator, which is the one thing this file must never do.
DEFAULTS: dict[str, str] = {
    "ENDPOINT_HOST": "",
    "ENDPOINT_PORT": "",
    "CLIENT_DNS": "8.8.8.8, 8.8.4.4",
    "CLIENT_MTU": "1400",
    "CLIENT_ALLOWED_IPS": "0.0.0.0/0",
    "SUBNET_CIDR": "",
    "SUBNET_BASE": "",
    # Blank for the same reason as SUBNET_CIDR - the server's own Address is the
    # authority and this only mirrors it - and with the same consequence as
    # there: blank means "this tunnel carries no IPv6", which is exactly what a
    # server installed before it could looks like until it is migrated.
    "SUBNET6_CIDR": "",
    "SUBNET6_MODE": "",
    "KEEPALIVE": "25",
}

# Header install.sh writes; repeated here for the case where the panel has to
# create the file from nothing (standalone install, or an admin deleted it).
# Only ever written into a new file - an upgraded server keeps whatever header
# it already had, and nothing here reads one.
HEADER = "# AmneziaWG client defaults. Shell syntax - every value must be QUOTED.\n"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")

# Characters bash still interprets inside double quotes.
_ESCAPE_IN_DQ = ("\\", '"', "$", "`")


@dataclass
class _Line:
    """One source line, plus what was recognised in it."""

    text: str
    key: str | None = None
    value: str = ""
    prefix: str = ""  # indent + optional "export " + "KEY="
    trailer: str = ""  # everything after the value, verbatim
    comment: str = ""  # trailing "# ..." if the trailer is only padding + comment
    comment_col: int = -1  # column the '#' started in, so alignment survives


def read_env(path: Path | str | None = None) -> dict[str, str]:
    """Return the settings with DEFAULTS filled in for anything the file omits.

    Not taken under config_lock(): every panel request reads this file and
    writers replace it atomically, so a reader gets the old file or the new one
    and never half of either. Locking here would put a read behind every write.
    """
    values = dict(DEFAULTS)
    for line in _read_text(_target(path)).splitlines():
        parsed = _parse_line(line)
        # A repeated key is what bash would end up with: the last one wins.
        if parsed.key is not None:
            values[parsed.key] = parsed.value
    return values


def update_env(changes: dict[str, str], path: Path | str | None = None) -> None:
    """Set the given keys in place, preserving comments, blanks and unknown keys.

    Keys already present are rewritten wherever they appear - a duplicate left
    behind further down the file would win when bash sourced it. New keys are
    appended in `KEY="value"` form. The write is atomic and 0600, under the same
    lock the bash tools take.
    """
    if not changes:
        return  # nothing to do; do not rewrite the file or take the lock
    for key, value in changes.items():
        if not _KEY_RE.match(key):
            raise ValidationError({key: "not a valid shell variable name"})
        no_line_break(key, value)

    target = _target(path)
    with config_lock():
        original = _read_text(target)
        lines = [_parse_line(line) for line in original.splitlines()]

        seen: set[str] = set()
        for parsed in lines:
            if parsed.key in changes:
                seen.add(parsed.key)
                parsed.text = _render(parsed, changes[parsed.key])

        out = [parsed.text for parsed in lines]
        if not out and not original:
            out.append(HEADER.rstrip("\n"))
        for key, value in changes.items():
            if key not in seen:
                out.append(f"{key}={quote(value)}")

        atomic_write(target, "\n".join(out) + "\n", mode=0o600)


def quote(value: str) -> str:
    """Render a value as a bash double-quoted word."""
    escaped = value
    for char in _ESCAPE_IN_DQ:
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def _target(path: Path | str | None) -> Path:
    return Path(path) if path is not None else env_file()


def _read_text(target: Path) -> str:
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as exc:
        raise AwgError(
            f"{target} is not valid UTF-8; fix or remove it, then retry "
            "(without it every client-facing default falls back to the panel's own)."
        ) from exc


def _parse_line(line: str) -> _Line:
    match = _ASSIGN_RE.match(line)
    if not match:
        return _Line(text=line)

    scanned = _scan_word(line, match.end())
    if scanned is None:
        return _Line(text=line)
    value, end = scanned

    trailer = line[end:]
    stripped = trailer.strip()
    # Anything else after the value (`FOO=bar baz`) means the line does more
    # than assign, so it is left exactly as it is.
    if stripped and not stripped.startswith("#"):
        return _Line(text=line)

    comment = ""
    comment_col = -1
    if stripped:
        comment_col = line.index("#", end)
        comment = line[comment_col:]

    return _Line(
        text=line,
        key=match.group("key"),
        value=value,
        prefix=line[: match.end()],
        trailer=trailer,
        comment=comment,
        comment_col=comment_col,
    )


def _scan_word(text: str, start: int) -> tuple[str, int] | None:
    """Unquote one bash word. Returns (value, index after it), or None if unreadable."""
    out: list[str] = []
    i, n = start, len(text)
    while i < n:
        char = text[i]
        if char in " \t":
            break
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in _ESCAPE_IN_DQ:
                    out.append(text[i + 1])
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i >= n:
                return None  # unterminated quote: the value spans lines
            i += 1
        elif char == "'":
            i += 1
            while i < n and text[i] != "'":
                out.append(text[i])
                i += 1
            if i >= n:
                return None
            i += 1
        elif char == "\\":
            if i + 1 >= n:
                return None  # trailing backslash: line continuation
            out.append(text[i + 1])
            i += 2
        else:
            # '#' only starts a comment at the start of a word, so mid-word it
            # is data: FOO=a#b assigns "a#b".
            out.append(char)
            i += 1
    return "".join(out), i


def _render(parsed: _Line, value: str) -> str:
    body = parsed.prefix + quote(value)
    if not parsed.comment:
        return body + parsed.trailer
    # Keep the comment in its original column; a longer value just pushes it
    # right by one space rather than running into it.
    return body + " " * max(parsed.comment_col - len(body), 1) + parsed.comment
