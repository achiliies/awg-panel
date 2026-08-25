#!/usr/bin/env python3
"""The Python half of the server-config parity check.

lib/conf.sh reads /etc/amnezia/amneziawg/awg0.conf with awk; awg/conf.py reads
the same file for the panel, which is what writes it. The file says at the top
that the two have to agree, and for four kinds of line they did not: an
indented section header, a header with a comment after it, a key in a different
case, a value with a comment on the end, and a peer with two AllowedIPs lines.

None of those come out of the panel. All of them come out of an editor, and the
file is one an operator does open - install.sh says as much where it edits the
[Interface] address, and refuses to assume the file it is rewriting is one of
its own.

So this generates configs carrying exactly those lines, says what awg/conf.py
makes of each, and hands them to tests/conf.sh to put through the awk. Nothing
here is the definition of correct on its own: `awg-quick` is, and both sides are
written to match what it does - it cuts every line at the first "#", trims
before comparing a header, and compares keys without regard to case, and the
kernel unions the AllowedIPs lines of a peer.

  conf_check.py generate DIR   write the corpus and the expected answers
  conf_check.py compare  DIR   compare what the bash side produced

Exit 0 means the two implementations answered identically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "panel"))

from awg.conf import parse_conf  # noqa: E402

# The keys iface_get is asked for anywhere in the tree: bin/awg-menu wants the
# address, the port and the MTU, install.sh wants the same three, and
# bin/awg-uninstall wants the port. Asked for here in a spelling the file may
# not use, which is the point of half of these cases.
IFACE_KEYS = ("Address", "ListenPort", "MTU", "PrivateKey")

_KEYS = "\n".join(
    [
        "PrivateKey = 4KZ0oPBLQ2nGDPRFAP8+cQyLoZQXqYbFAWnCz1LWQ1g=",
        "Jc = 4",
        "Jmin = 40",
        "Jmax = 70",
    ]
)

CASES: dict[str, str] = {}

# What the installer writes, so that a corpus of awkward files still says
# whether the ordinary one is read the same way by both.
CASES["canonical"] = f"""[Interface]
Address = 10.9.0.1/24
ListenPort = 51820
MTU = 1420
{_KEYS}

[Peer]
# Client = laptop
# Created = 2026-01-04T11:02:19Z
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
AllowedIPs = 10.9.0.2/32

[Peer]
# Client = phone
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
AllowedIPs = 10.9.0.3/32
"""

# An editor that indents, which awg-quick trims before it compares and the awk
# used to require at column 0. The peer this opens was folded into the one
# above it, so the name on the status screen belonged to another client.
CASES["indented-headers"] = """[Interface]
  Address = 10.9.0.1/24
  ListenPort = 51820

  [Peer]
  # Client = laptop
  PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
  AllowedIPs = 10.9.0.2/32

	[Peer]
	# Client = tabbed
	PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
	AllowedIPs = 10.9.0.3/32
"""

# The annotation conf.py has always taken and the awk happened to take too,
# next to the one that is not a header at all: "[Peer # x]" leaves `awg` with
# an unterminated bracket, and both sides read it as an ordinary line that
# closes the peer above it.
CASES["annotated-headers"] = """[Interface] # the tunnel
Address = 10.9.0.1/24
ListenPort = 51820

[Peer] # laptop
# Client = laptop
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
AllowedIPs = 10.9.0.2/32

[Peer # phone]
# Client = phone
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
AllowedIPs = 10.9.0.3/32
"""

# The C parser in `awg` compares keys with strcasecmp and conf.py folds them.
# A peer spelled this way works, and the awk counted it as a peer with no key
# at all - which is to say it did not count it.
CASES["mixed-case-keys"] = """[Interface]
address = 10.9.0.1/24
listenport = 51820
mtu = 1420

[Peer]
# Client = laptop
publickey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
allowedips = 10.9.0.2/32

[Peer]
# Client = phone
PUBLICKEY = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
ALLOWEDIPS = 10.9.0.3/32
"""

# awg-quick cuts every line at the first "#". Read whole, the address is a
# string no parse_cidr takes and the allowed IPs are a string no allocator can
# subtract from what is free.
CASES["inline-comments"] = """[Interface]
Address = 10.9.0.1/24   # the server itself
ListenPort = 51820  # opened in the firewall by install.sh
MTU = 1420# no space before it

[Peer]
# Client = laptop
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=   # rotated 2026-01
AllowedIPs = 10.9.0.2/32  # wired
"""

# The kernel takes the union. Keeping the last line understated what the client
# may reach, and the panel hands out what it believes is unused.
CASES["repeated-allowedips"] = """[Interface]
Address = 10.9.0.1/24
ListenPort = 51820

[Peer]
# Client = laptop
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
AllowedIPs = 10.9.0.2/32
AllowedIPs = 10.9.0.66/32
AllowedIPs = 192.168.8.0/24
"""

# A section neither side knows still closes the peer above it, and everything
# under it belongs to nobody.
CASES["unknown-section"] = """[Interface]
Address = 10.9.0.1/24
ListenPort = 51820

[Peer]
# Client = laptop
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
AllowedIPs = 10.9.0.2/32

[Bogus]
PublicKey = 6Uu8lQlHCDdLFSQ4kZTRIpBqLDQjqVIzJ7CZv2mBcXk=
AllowedIPs = 10.9.0.9/32

[Peer]
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
AllowedIPs = 10.9.0.3/32
"""

# A peer with no "# Client" line, which is what a peer added by hand looks
# like, and the empty first field split_peer exists to keep in place.
CASES["unnamed-peer"] = """[Interface]
Address = 10.9.0.1/24

[Peer]
PublicKey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
AllowedIPs = 10.9.0.2/32

[Peer]
# Client = phone
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
AllowedIPs = 10.9.0.3/32
"""

# A peer with no key at all is not a peer: neither side lists it.
CASES["keyless-peer"] = """[Interface]
Address = 10.9.0.1/24

[Peer]
# Client = half-written
AllowedIPs = 10.9.0.4/32

[Peer]
# Client = phone
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=
AllowedIPs = 10.9.0.3/32
"""

# Every one of the above in one file, because they turned up together in the
# first place: a config somebody has been editing for a year.
CASES["everything"] = """[Interface] # server
  address = 10.9.0.1/24  # the server itself
  ListenPort=51820
	MTU  =  1420

  [Peer]  # laptop
  # Client = laptop
  publickey = Bn3ihGqLHQ8b3sdz0rk9BuLR/GdxTBmYqZjKQPVIzT4=
  AllowedIPs = 10.9.0.2/32   # wired
  allowedips = 192.168.8.0/24

[Peer # not a header]
[Peer]
PublicKey = mQ9YQZ0mDMSNzGO2wPB6bnCyTVR7lFDCLuUbBGKgVXE=   # phone
AllowedIPs=10.9.0.3/32
PersistentKeepalive = 25
"""


def expected(text: str) -> tuple[list[str], list[str]]:
    """What awg/conf.py makes of one config: its peers, and the keys iface_get reads."""
    conf = parse_conf(text)
    peers = [
        "\t".join((peer.name or "", peer.public_key, peer.allowed_ips))
        for peer in conf.peers
        if peer.public_key
    ]
    iface = [f"{key}\t{conf.interface.get(key) or ''}" for key in IFACE_KEYS]
    return peers, iface


def generate(dest: Path) -> int:
    (dest / "cases").mkdir(parents=True, exist_ok=True)
    (dest / "want").mkdir(parents=True, exist_ok=True)
    for name, text in CASES.items():
        (dest / "cases" / f"{name}.conf").write_text(text)
        peers, iface = expected(text)
        (dest / "want" / f"{name}.peers").write_text("".join(f"{line}\n" for line in peers))
        (dest / "want" / f"{name}.iface").write_text("".join(f"{line}\n" for line in iface))
    (dest / "names.txt").write_text("".join(f"{name}\n" for name in CASES))
    (dest / "keys.txt").write_text("".join(f"{key}\n" for key in IFACE_KEYS))
    print(f"  {len(CASES)} configs")
    return 0


def compare(dest: Path) -> int:
    failed = 0
    for name in CASES:
        for kind in ("peers", "iface"):
            want = (dest / "want" / f"{name}.{kind}").read_text().splitlines()
            got_file = dest / "got" / f"{name}.{kind}"
            if not got_file.exists():
                print(f"  FAIL {name} ({kind}): the bash side produced nothing")
                failed += 1
                continue
            got = got_file.read_text().splitlines()
            if want == got:
                print(f"  ok   {name} ({kind})")
                continue
            failed += 1
            print(f"  FAIL {name} ({kind}):")
            for i in range(max(len(want), len(got))):
                w = want[i] if i < len(want) else "<nothing>"
                g = got[i] if i < len(got) else "<nothing>"
                if w != g:
                    print(f"         conf.py:    {w!r}")
                    print(f"         lib/conf.sh: {g!r}")
    print(f"\n{len(CASES) * 2 - failed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("generate", "compare"):
        print(__doc__)
        return 2
    dest = Path(argv[2])
    return generate(dest) if argv[1] == "generate" else compare(dest)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
