"""Read and write the AmneziaWG server config without disturbing a byte.

This file is not the panel's private format. `awg-quick` reads it at boot, the
installer writes it with here-docs, awg-menu reads it with awk, and an admin
edits it with vi. A parse/render cycle therefore has to hand back the original text unchanged -
blank lines, comments, key spelling and repeated PostUp/PostDown lines
included - otherwise every panel write would show up as churn in a file the
bash tools also own.

The splitting rules mirror those awk programs exactly: a line is split on the
FIRST '=' only (base64 values are '='-padded), and whitespace around the key
and the value is stripped.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import ValidationError

# Keys awg-quick consumes itself. They configure the host (addresses, routes,
# hooks), not the kernel interface, and `awg setconf` rejects them.
_STRIP_DROP = frozenset(
    {
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
)

# Peer settings that get their own dataclass field; everything else survives in
# Peer.extra. Matched case-insensitively, rendered in canonical spelling.
_PEER_FIELDS = {
    "publickey": "public_key",
    "presharedkey": "preshared_key",
    "allowedips": "allowed_ips",
    "endpoint": "endpoint",
    "persistentkeepalive": "persistent_keepalive",
}

# Peer metadata the bash tools carry as comments. Case-sensitive, because the
# awk in peer_list is: a differently-cased marker would be invisible to it.
_META_FIELDS = {"Client": "name", "Created": "created", "Disabled": "disabled_at"}

# A section header, with the trailing comment awg-quick allows. It cuts every
# line at the first "#" before looking for a bracket, and the C parser in `awg`
# strips the comment and the whitespace before comparing against [Interface] /
# [Peer] - so "[Peer] # phone" opens a peer section for both of them, and for
# the /^\[Peer\]/ awk in lib/conf.sh. Reading it as an ordinary line instead
# folded the block into the peer above and dropped one of the two on the next
# write. "#" is excluded from the bracket body so "[Peer # x]" stays a
# non-header, which is what `awg` does with it (it strips the comment, sees an
# unterminated "[Peer", and refuses the file).
_SECTION_RE = re.compile(r"\s*\[([^\]#]*)\]\s*(?:#.*)?$")
# awg-quick's own test for "this line ends the section above": cut at the first
# '#', trim, and see whether a bracket opens what is left. A line that looks
# like a section but names nothing this understands - "[Bogus]", or a mangled
# "[Peer #2]" - still closes the peer above and is kept verbatim. Letting it
# fall through as an ordinary line instead merges the block that follows into
# the previous peer, and the next write then drops one of the two.
_SECTION_START_RE = re.compile(r"\s*\[")
_META_RE = re.compile(r"\s*#\s*(Client|Created|Disabled)\s*=(.*)$")


def _uncommented(line: str) -> str:
    """The part of a line awg-quick reads: everything before the first "#".

    A comment runs to the end of the line wherever it starts, so
    "Address = 10.9.0.1/24 # main" is an address and nothing else. This was
    applied to section headers from the beginning and never to values, which
    left the panel showing a CIDR with a comment stuck to it and lib/conf.sh
    handing one to parse_cidr.

    Only the value is cut. The source line is kept whole in the _Item beside it
    and is what renders back, so a comment survives until somebody changes the
    setting it sits on - at which point there is no line left for it to belong
    to.
    """
    return line.split("#", 1)[0]


def no_line_break(field: str, value: str) -> str:
    """Return `value`, or refuse it if it would not stay on the line it is written to.

    Every format this package writes is one setting per line, so a value holding
    a newline is not a long value: it is however many further settings the rest
    of it parses as. `DNS = 1.1.1.1\\nPostUp = curl …` is a well-formed config
    with a PostUp in it, and `wg-quick` runs a PostUp as root on whichever
    machine imports the file.

    The rule already existed for clients.env, where update_env has refused a
    line break since it was written, and did not for the client configs the
    store renders - although the values reaching them come from the same API and
    the same operator. This is that rule, in one place, for both.

    Returning the value rather than only raising lets a caller write
    `no_line_break("dns", dns)` at the point of use, so the check cannot be left
    behind by an edit that moves the interpolation somewhere else.
    """
    if "\n" in value or "\r" in value:
        raise ValidationError({field: "must not contain a line break"})
    return value


@dataclass
class _Item:
    """One line of a section: a setting, a blank line, a comment or garbage."""

    kind: str  # "kv" | "blank" | "comment" | "raw"
    key: str = ""
    value: str = ""
    text: str | None = None  # verbatim source line, dropped once the value changes


class OrderedMulti:
    """The ordered contents of one config section.

    A section is a stream, not a dict: PostUp/PostDown repeat and their order is
    load-bearing, and blank lines and comments have to survive a parse/render
    cycle untouched. Lookups ignore case, as the kernel tools do, but the
    spelling and spacing found in the file are kept until a value is changed.
    """

    def __init__(self, items: Iterable[tuple[str, str]] | None = None) -> None:
        self._items: list[_Item] = []
        for key, value in items or ():
            self.append(key, value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and bool(self._indexes(key))

    def _indexes(self, key: str) -> list[int]:
        folded = key.casefold()
        return [
            i
            for i, item in enumerate(self._items)
            if item.kind == "kv" and item.key.casefold() == folded
        ]

    def _insert_after_last_kv(self, item: _Item) -> None:
        # A new key belongs after the last real setting, not after the blank
        # line that separates the section from the first [Peer].
        for i in range(len(self._items) - 1, -1, -1):
            if self._items[i].kind == "kv":
                self._items.insert(i + 1, item)
                return
        self._items.append(item)

    def get(self, key: str, default: str | None = None) -> str | None:
        """First value for key, or default. Matches awk's iface_get."""
        found = self._indexes(key)
        return self._items[found[0]].value if found else default

    def all(self, key: str) -> list[str]:
        """Every value for key, in file order."""
        return [self._items[i].value for i in self._indexes(key)]

    def set(self, key: str, value: str) -> None:
        """Leave the section with exactly one occurrence of key, holding value.

        Every occurrence, in the first one's position - the rule delete() already
        follows, and for the same reason. A hand-edited config can carry a
        parameter twice, and this used to rewrite the first and walk away from
        the rest: the panel then displayed the value it had written while the
        interface came up with a later line that had never been touched. Which
        of the two won depended on the parameter, and neither the file nor the
        panel said anything was wrong.

        Collapsing rather than rewriting in place is what makes get() honest
        afterwards: it answers with the first occurrence, and after this there is
        only one for it to answer with. A key whose repeats are meaningful -
        PostUp and PostDown - is not written through here at all; it goes through
        set_all, which keeps them.
        """
        self.set_all(key, [value])

    def set_all(self, key: str, values: list[str]) -> None:
        """Replace every occurrence with values, keeping the first position."""
        found = self._indexes(key)
        if not values:
            self.delete(key)
            return
        if not found:
            for value in values:
                self._insert_after_last_kv(_Item("kv", key, value))
            return
        first = found[0]
        head = self._items[first]
        head.value = values[0]
        head.text = None
        for index in reversed(found[1:]):
            del self._items[index]
        for offset, value in enumerate(values[1:], start=1):
            self._items.insert(first + offset, _Item("kv", head.key, value))

    def delete(self, key: str) -> None:
        """Drop every occurrence of key.

        Every occurrence, not the first: a hand-edited config can carry a
        parameter twice, and leaving the second one behind would clear a setting
        in the panel and leave the interface still coming up with it.
        """
        for index in reversed(self._indexes(key)):
            del self._items[index]

    def keys(self) -> list[str]:
        """Distinct keys in first-appearance order, in their file spelling."""
        out: list[str] = []
        seen: set[str] = set()
        for item in self._items:
            if item.kind != "kv":
                continue
            folded = item.key.casefold()
            if folded not in seen:
                seen.add(folded)
                out.append(item.key)
        return out

    def items(self) -> list[tuple[str, str]]:
        """Every key/value pair in file order, repeats included."""
        return [(item.key, item.value) for item in self._items if item.kind == "kv"]

    def copy(self) -> "OrderedMulti":
        """An independent section: the blank lines and comments too, not only the settings.

        Rebuilding this from items() would be a section that renders differently
        from the one it was copied from, because everything that is not a setting
        would be gone. So the items are copied as they are, and each one is a new
        object - `set` edits an item's value in place, and a shared item is one
        section's edit landing in another's.
        """
        clone = OrderedMulti()
        clone._items = [_Item(i.kind, i.key, i.value, i.text) for i in self._items]
        return clone

    def append(self, key: str, value: str, text: str | None = None) -> None:
        """Add a setting at the end of the section, keeping its source line."""
        self._items.append(_Item("kv", key, value, text))

    def add_blank(self, text: str = "") -> None:
        self._items.append(_Item("blank", text=text))

    def add_comment(self, text: str) -> None:
        self._items.append(_Item("comment", text=text))

    def add_raw(self, text: str) -> None:
        self._items.append(_Item("raw", text=text))

    def lines(self) -> list[str]:
        """Render the section body, without its [Interface] header."""
        out: list[str] = []
        for item in self._items:
            if item.kind == "kv" and item.text is None:
                out.append(f"{item.key} = {item.value}")
            else:
                out.append(item.text or "")
        return out


@dataclass
class Peer:
    public_key: str
    preshared_key: str | None = None
    allowed_ips: str = ""
    endpoint: str | None = None
    persistent_keepalive: int | None = None
    name: str | None = None  # from "# Client ="
    created: str | None = None  # from "# Created ="
    disabled_at: str | None = None  # from "# Disabled ="
    extra: list[tuple[str, str]] = field(default_factory=list)
    extra_comments: list[str] = field(default_factory=list)
    # The header line as it was written, so an annotated "[Peer] # phone"
    # survives a round trip instead of being normalised away under its author.
    header: str = "[Peer]"


@dataclass
class ServerConf:
    interface: OrderedMulti = field(default_factory=OrderedMulti)
    peers: list[Peer] = field(default_factory=list)
    interface_header: str = "[Interface]"


def clone_conf(conf: ServerConf) -> ServerConf:
    """An independent copy, for handing the same parse to more than one caller.

    Every mutation in awg.store edits this structure in place - a peer's
    "# Disabled" marker, a name, an interface setting, the peer list itself - so
    two callers sharing one object is two callers editing each other's work.
    Anything that keeps a parse around to hand out again has to copy it, and this
    is what "copy" has to mean for it to be safe: deep enough that no mutation
    can reach the original, shallow enough to be worth doing.

    Which comes to rebuilding the peers and the interface section, and sharing
    the strings. Strings are immutable, so the fields that are one are shared
    without risk; `extra` and `extra_comments` are lists and are copied; and the
    interface's items carry a mutable value, so they are rebuilt too - it is a
    dozen lines against thousands of peers, and leaving it shared would mean a
    port change reaching a cached config nobody else has read yet.

    Not copy.deepcopy, which does the same job by a general route and, measured
    on four thousand peers, takes rather longer than simply parsing the file
    again - which would make the whole idea of keeping the parse pointless.
    """
    return ServerConf(
        interface=conf.interface.copy(),
        interface_header=conf.interface_header,
        peers=[
            Peer(
                public_key=peer.public_key,
                preshared_key=peer.preshared_key,
                allowed_ips=peer.allowed_ips,
                endpoint=peer.endpoint,
                persistent_keepalive=peer.persistent_keepalive,
                name=peer.name,
                created=peer.created,
                disabled_at=peer.disabled_at,
                extra=list(peer.extra),
                extra_comments=list(peer.extra_comments),
                header=peer.header,
            )
            for peer in conf.peers
        ],
    )


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the trailing newline ends the last line, it is not one
    return lines


def _feed_interface(section: OrderedMulti, line: str) -> None:
    stripped = line.strip()
    if not stripped:
        section.add_blank(line)
        return
    if stripped.startswith("#"):
        section.add_comment(line)
        return
    key, sep, value = _uncommented(line).partition("=")
    if not sep or not key.strip():
        section.add_raw(line)
        return
    section.append(key.strip(), value.strip(), line)


def _feed_peer(peer: Peer, line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return  # blank lines inside a peer block carry nothing worth keeping
    if stripped.startswith("#"):
        meta = _META_RE.match(line)
        if meta:
            setattr(peer, _META_FIELDS[meta.group(1)], meta.group(2).strip())
        else:
            peer.extra_comments.append(line)
        return
    key, sep, value = _uncommented(line).partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not key:
        # Malformed, but never silently dropped: it renders back verbatim.
        peer.extra_comments.append(line)
        return
    attr = _PEER_FIELDS.get(key.casefold())
    if attr == "persistent_keepalive":
        try:
            peer.persistent_keepalive = int(value)
        except ValueError:
            peer.extra.append((key, value))  # "off" and friends stay as text
        return
    if attr == "allowed_ips":
        # The kernel takes the union of every AllowedIPs line in a peer, so a
        # second one adds to the first rather than replacing it. Overwriting
        # understated what a client may reach, and the panel allocates out of
        # what it thinks is unused - so the address on the line that was thrown
        # away was an address it would hand to somebody else.
        #
        # They render back as the one comma-separated line that means the same
        # thing to `awg`, which is the single place this file does not give
        # back the bytes it was handed. A peer written by the panel has one
        # AllowedIPs line and is untouched; a hand-edited one with two is
        # tidied the first time something rewrites the file, which is better
        # than the alternative of losing one of them.
        peer.allowed_ips = f"{peer.allowed_ips}, {value}" if peer.allowed_ips else value
        return
    if attr:
        setattr(peer, attr, value)
        return
    peer.extra.append((key, value))


def parse_conf(text: str) -> ServerConf:
    """Parse a server config, keeping unknown keys, comments and order."""
    conf = ServerConf()
    peer: Peer | None = None
    seen_interface = False
    for line in _split_lines(text):
        header = _SECTION_RE.match(line)
        if header:
            name = header.group(1).strip().casefold()
            if name == "peer":
                peer = Peer(public_key="", header=line.rstrip("\r\n"))
                conf.peers.append(peer)
                continue
            if name == "interface" and not seen_interface:
                seen_interface = True
                conf.interface_header = line.rstrip("\r\n")
                peer = None
                continue
            # A repeated [Interface] or a section nothing understands: keep the
            # header verbatim so the file still reads the way it was written.
            peer = None
            conf.interface.add_raw(line)
            continue
        if _SECTION_START_RE.match(line.split("#", 1)[0]):
            peer = None
            conf.interface.add_raw(line)
            continue
        if peer is None:
            _feed_interface(conf.interface, line)
        else:
            _feed_peer(peer, line)
    return conf


def _peer_lines(peer: Peer) -> list[str]:
    out = [peer.header or "[Peer]"]
    if peer.name is not None:
        out.append(f"# Client = {peer.name}")
    if peer.created is not None:
        out.append(f"# Created = {peer.created}")
    if peer.disabled_at is not None:
        out.append(f"# Disabled = {peer.disabled_at}")
    # Comments in a peer block are metadata written above the keys; keeping
    # them there is what makes the round trip exact for existing files.
    out.extend(peer.extra_comments)
    if peer.public_key:
        out.append(f"PublicKey = {peer.public_key}")
    if peer.preshared_key:
        out.append(f"PresharedKey = {peer.preshared_key}")
    if peer.allowed_ips:
        out.append(f"AllowedIPs = {peer.allowed_ips}")
    if peer.endpoint:
        out.append(f"Endpoint = {peer.endpoint}")
    if peer.persistent_keepalive is not None:
        out.append(f"PersistentKeepalive = {peer.persistent_keepalive}")
    out.extend(f"{key} = {value}" for key, value in peer.extra)
    return out


def render_conf(conf: ServerConf) -> str:
    """Render a ServerConf in the layout install.sh writes."""
    lines: list[str] = [conf.interface_header or "[Interface]"]
    lines.extend(conf.interface.lines())
    for peer in conf.peers:
        # Exactly one blank line before each [Peer]; the interface section may
        # already end with the one the installer wrote.
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(_peer_lines(peer))
    return "\n".join(lines) + "\n"


def parse_client_conf(text: str) -> dict[str, str]:
    """Flat key/value view of a client config, first occurrence winning.

    Scans the whole file regardless of section and stops at the first match,
    which is how every other reader of a client config treats it: the sections
    are there for the tools, not as a namespace.
    """
    out: dict[str, str] = {}
    for line in _split_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        # A trailing comment is not part of the value here either, and this
        # file is the one most likely to carry one: a client config that comes
        # back to be imported has been through somebody's editor.
        key, sep, value = _uncommented(line).partition("=")
        key = key.strip()
        if sep and key and key not in out:
            out[key] = value.strip()
    return out


def strip_conf(conf: ServerConf, exclude_pubkeys: Iterable[str] = ()) -> str:
    """Render what `awg setconf`/`syncconf` accepts, as `awg-quick strip` does.

    Peers in exclude_pubkeys are left out, and so are peers carrying a
    "# Disabled" marker: a disabled key must never reach the kernel, whoever
    calls this.
    """
    excluded = set(exclude_pubkeys)
    out: list[str] = ["[Interface]"]
    for key, value in conf.interface.items():
        if key.casefold() not in _STRIP_DROP:
            out.append(f"{key} = {value}")
    for peer in conf.peers:
        if not peer.public_key or peer.disabled_at is not None:
            continue
        if peer.public_key in excluded:
            continue
        out.append("[Peer]")
        out.append(f"PublicKey = {peer.public_key}")
        if peer.preshared_key:
            out.append(f"PresharedKey = {peer.preshared_key}")
        if peer.allowed_ips:
            out.append(f"AllowedIPs = {peer.allowed_ips}")
        if peer.endpoint:
            out.append(f"Endpoint = {peer.endpoint}")
        if peer.persistent_keepalive is not None:
            out.append(f"PersistentKeepalive = {peer.persistent_keepalive}")
    return "\n".join(out) + "\n"
