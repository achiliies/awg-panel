#!/usr/bin/env python3
"""tests/subnet6_check.py - hold lib/subnet6.sh and awg/subnet6.py to one answer.

Two implementations still have to agree about this, even though the panel is
now the only thing that writes a client into the config. install.sh is what
plans the IPv6 side and writes the interface Address in the first place, in
bash, and the panel reads that back and allocates from it in Python.

They agree about something with more room to differ than it looks: an address
can be spelled several correct ways, and two implementations that both
"compress zeros" can produce different strings for the same address without
either being wrong. Let the installer's spelling and the panel's diverge and
`_used_hosts` misses a peer, which is how two clients end up holding one
address.

So this does not check that both sides are *valid*. It checks that they are
*identical*, over every spelling of a prefix that a config might hold, and that
what they produce actually lands where it claims to - verified independently
against the standard library rather than against either implementation.

    tests/subnet6_check.py generate <dir>   write the corpus for the bash side
    tests/subnet6_check.py verify <dir>     compare bash's answers with Python's
"""

import ipaddress
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "panel"))

from awg import subnet6

# Where a client can sit in the pool: the first one, a couple of ordinary ones,
# the group boundary, and offsets past it that need a second group. 4094 is the
# last host of the default /20; the ones past 65535 are wider than any tunnel
# the tools now accept, and stay here because both sides must still spell the
# same address for them.
OFFSETS = [1, 2, 5, 255, 256, 4094, 65535, 65536, 70000, 1000000]

# Values no spelling of which is a tunnel prefix. Both sides must refuse all of
# them: a prefix that only one side rejects is a prefix the two disagree about.
REJECT = [
    "",
    "::/64",
    "::1/64",
    "fd00::/48",
    "fd00::/63",
    "fd00::/128",
    "fd00:::1/64",
    "fd00::1::2/64",
    "zzzz::/64",
    "12345::/64",
    "1.2.3.4/24",
    "10.13.0.1/20",
    ":/64",
    "fd00:/64",
    ":::/64",
    "2001:db8::/64",  # documentation
    "2001:db8:1:2::/64",  # documentation
    "2001:1::/64",  # IETF protocol assignments, 2001::/23
    "2001:0:0:1::/64",  # same range
    "fe80::/64",  # link-local
    "ff02::/64",  # multicast
    "::1/128",
    "e000::/64",  # outside both 2000::/3 and fc00::/7
    "4000::/64",  # ditto: 2000::/3 stops at 3fff
    "not an address",
    "fd00::1/64 extra",
]


def _sample_networks(count: int) -> list[ipaddress.IPv6Network]:
    """Random /64s across both ranges a tunnel may be numbered from."""
    rng = random.Random(20260807)
    out: list[ipaddress.IPv6Network] = []
    while len(out) < count:
        if rng.random() < 0.5:
            first = rng.randrange(0x2000, 0x4000)  # global unicast
            if first == 0x2001:
                continue  # 2001::/23 and the documentation prefix live here
        else:
            first = rng.randrange(0xFD00, 0xFE00)  # unique-local
        # Deliberately weighted towards zero groups: that is where trailing-zero
        # trimming and "::" placement can differ between the two sides.
        rest = [rng.choice([0, 0, 0, 1, rng.randrange(0x10000)]) for _ in range(3)]
        groups = [first, *rest]
        address = ipaddress.IPv6Address(":".join(f"{g:x}" for g in groups) + "::")
        out.append(ipaddress.IPv6Network((address, 64)))
    return out


def _spellings(network: ipaddress.IPv6Network) -> list[str]:
    """The forms this prefix might legally appear in inside a config."""
    base = network.network_address
    return [
        str(network),  # fd00:1::/64
        f"{base.compressed}/64",  # the same, written out
        f"{base.exploded}/64",  # every group spelled, leading zeros and all
        f"{(base + 1).compressed}/64",  # the server's own Address, host bits set
        f"{(base + 4094).compressed}/64",  # a client's Address
        base.compressed,  # no prefix at all; read as a /64
        f"  {network}  ",  # whitespace, as an Address list leaves it
        f"10.13.0.1/20, {network}",  # the real dual-stack Address line
    ]


# Route lists, and whether adding ::/0 to each would close a leak. The point of
# the list is the spellings of "everything" that are not the string
# "0.0.0.0/0": matching the text rather than the coverage is what let a whole
# server go on leaking after it had supposedly been fixed.
ROUTE_LISTS = [
    "0.0.0.0/0",
    "0.0.0.0/1, 128.0.0.0/1",  # the split default route, and the one that bit
    "0.0.0.0/1,128.0.0.0/1",
    "128.0.0.0/1, 0.0.0.0/1",  # same, out of order
    "0.0.0.0/2, 64.0.0.0/2, 128.0.0.0/1",  # in three
    "0.0.0.0/2, 64.0.0.0/2, 128.0.0.0/2, 192.0.0.0/2",  # in four
    "  0.0.0.0/0  ",
    "0.0.0.0/0, 10.0.0.0/8",  # redundant, still everything
    "1.2.3.4/0",  # host bits set on a /0
    # Not full tunnels: something is deliberately not routed.
    "10.13.0.0/20",
    "0.0.0.0/1",
    "128.0.0.0/1",
    "0.0.0.0/2, 128.0.0.0/1",  # 64.0.0.0/2 missing
    "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16",
    "0.0.0.0/1, 128.0.0.1/1",  # still the two halves, host bits set
    # Already routing IPv6: somebody has decided, leave it be.
    "0.0.0.0/0, ::/0",
    "0.0.0.0/1, 128.0.0.0/1, ::/0",
    "::/0",
    "0.0.0.0/0, 2001:db8::/32",
    # Unreadable: leave alone rather than guess.
    "",
    "garbage",
    "0.0.0.0/33",
    "999.0.0.0/8",
    "0.0.0.0/0, nonsense",
]


def generate(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    accept = []
    for network in _sample_networks(150):
        for spelling in _spellings(network):
            accept.append(f"{spelling}\t{network}")
    (target / "accept.tsv").write_text("\n".join(accept) + "\n", encoding="utf-8")
    (target / "reject.txt").write_text("\n".join(REJECT) + "\n", encoding="utf-8")
    (target / "offsets.txt").write_text("\n".join(str(n) for n in OFFSETS) + "\n", encoding="utf-8")
    (target / "routes.txt").write_text("\n".join(ROUTE_LISTS) + "\n", encoding="utf-8")
    print(
        f"{len(accept)} spellings, {len(REJECT)} rejects, {len(OFFSETS)} offsets, "
        f"{len(ROUTE_LISTS)} route lists"
    )


def verify(target: Path) -> int:
    problems: list[str] = []

    rows = (target / "accepted.tsv").read_text(encoding="utf-8").splitlines()
    seen = 0
    for row in rows:
        if not row.strip():
            continue
        seen += 1
        fields = row.split("\t")
        spelling, expected, bash_cidr, bash_server = (
            fields[0],
            fields[1],
            fields[2],
            fields[3],
        )
        bash_hosts = fields[4:]

        if bash_cidr == "PARSE_FAIL":
            problems.append(f"bash refused {spelling!r}, which is a legal spelling of {expected}")
            continue

        network = subnet6.parse(spelling)
        if str(network) != expected:
            problems.append(f"python read {spelling!r} as {network}, not {expected}")
            continue
        if subnet6.cidr(network) != bash_cidr:
            problems.append(f"{spelling!r}: bash cidr {bash_cidr}, python {subnet6.cidr(network)}")
        if subnet6.server_addr(network) != bash_server:
            problems.append(
                f"{spelling!r}: bash server {bash_server}, python {subnet6.server_addr(network)}"
            )

        # Counted before they are paired. bash_hosts is however many columns the
        # shell happened to write, and zip() would quietly stop at the shorter
        # of the two - so a run where the shell emitted nine hosts for ten
        # offsets would compare nine and report nothing about the tenth, which
        # is a missing answer reported as a passing one.
        if len(bash_hosts) != len(OFFSETS):
            problems.append(
                f"{spelling!r}: bash wrote {len(bash_hosts)} hosts for {len(OFFSETS)} offsets"
            )
            continue

        for offset, got in zip(OFFSETS, bash_hosts, strict=True):
            want = subnet6.host_addr(network, offset)
            if got != want:
                problems.append(f"{spelling!r} offset {offset}: bash {got}, python {want}")
                continue
            # Independent of both: does the string land where it claims to?
            address = ipaddress.IPv6Address(got)
            if address not in network:
                problems.append(f"{spelling!r} offset {offset}: {got} is outside {network}")
            elif int(address) - int(network.network_address) != offset:
                real = int(address) - int(network.network_address)
                problems.append(f"{spelling!r} offset {offset}: {got} is really +{real}")

    refused = (target / "rejected.tsv").read_text(encoding="utf-8").splitlines()
    for row in refused:
        if not row.strip():
            continue
        value, verdict = row.split("\t", 1)
        python_took = subnet6.parse_or_none(value) is not None
        bash_took = verdict != "REJECTED"
        if bash_took or python_took:
            who = "both" if bash_took and python_took else ("bash" if bash_took else "python")
            problems.append(f"{who} accepted {value!r}, which is not a usable tunnel prefix")

    routes = (target / "routes-out.tsv").read_text(encoding="utf-8").splitlines()
    for row in routes:
        value, verdict, rendered = row.split("\t", 2)
        bash_needs = verdict == "NEEDS"
        want_needs = subnet6.needs_ipv6(value)
        if bash_needs != want_needs:
            problems.append(
                f"{value!r}: bash says {'needs' if bash_needs else 'leave'}, "
                f"python says {'needs' if want_needs else 'leave'}"
            )
        want_rendered = subnet6.with_ipv6(value)
        if rendered != want_rendered:
            problems.append(f"{value!r}: bash wrote {rendered!r}, python {want_rendered!r}")

    for problem in problems[:40]:
        print(f"  FAIL {problem}")
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more")
    print(
        f"\n{seen} spellings, {len(refused)} rejects and {len(routes)} route lists checked, "
        f"{len(problems)} problems"
    )
    return 1 if problems else 0


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("generate", "verify"):
        print(__doc__)
        return 2
    target = Path(sys.argv[2])
    if sys.argv[1] == "generate":
        generate(target)
        return 0
    return verify(target)


if __name__ == "__main__":
    raise SystemExit(main())
