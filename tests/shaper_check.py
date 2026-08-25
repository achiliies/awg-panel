#!/usr/bin/env python3
"""Drive awg.shaper against real interfaces and ask the kernel what it kept.

Run by tests/shaper.sh inside a network namespace; not useful on its own, and
deliberately not a pytest file - it needs root and two interfaces, which is
exactly what the panel's test suite is built never to require.

The assertions are all of the same kind: do something through the module's own
API, then go behind it and read the kernel directly. Reading it back through the
module would only prove the module agrees with itself, and the failure this is
here to catch is the one where it agrees with itself perfectly and the kernel
was never listening.

What is read is the derived handle (`fh 10:2:800`) and the class it points at
(`flowid 1:12`), plus the hex word a u32 match actually compiles to. Not the
dotted address: `tc filter show` prints a u32 key as the 32-bit word and mask it
compares, so grepping the output for "10.13.0.2" finds nothing on any version
and would make this whole file a test that passes without testing.

Two subnets are run, and the second is the point. 10.13.13.128/25 is one of the
shapes where a client's offset into the pool and the last octet of its address
are different numbers, which is where the bucket derivation was wrong once
already.

The other thing this drives is *transitions*, and they were the blind spot for
longer. Every check here used to start either from a bare namespace or from a
structure that was fully built, so nothing ever exercised the move between the
two - and a server spends its life in exactly those moves: download shaping goes
on today and upload in a month's time, and it is that second switch that found
the tunnel's root already up and built none of the qdiscs the upload half needs.
"""

import ipaddress
import os
import re
import subprocess
import sys

from awg import shaper as shaper_mod
from awg.shaper import CLASS_BASE, Shaper, handle, handle6, minor

DOWN = 5_000_000
UP = 2_000_000

# (label, v4 network, v6 network or None, a client, another client)
SCENARIOS = [
    (
        "an aligned /20, dual stack",
        ipaddress.IPv4Network("10.13.0.0/20"),
        ipaddress.IPv6Network("fd00:1:2:3::/64"),
        "10.13.0.2",
        "10.13.1.7",
    ),
    (
        "a non-aligned /25, v4 only",
        ipaddress.IPv4Network("10.13.13.128/25"),
        None,
        "10.13.13.130",
        "10.13.13.200",
    ),
]

failures: list[str] = []


def check(claim: str, ok: bool, saw: str = "") -> None:
    """One assertion, and what the kernel actually said when it does not hold.

    Every check here is a substring of `tc`'s human-readable output, which is a
    display format rather than an interface: it varies with the iproute2 build
    and with the kernel underneath it. So a failure that only says which claim
    broke sends whoever reads it to guess at the string, on a machine that is
    usually not the one that failed - which is exactly the position this was in
    the first time it ran anywhere but its author's laptop.
    """
    print(f"  {'ok  ' if ok else 'FAIL'}  {claim}")
    if ok:
        return
    failures.append(claim)
    if saw:
        for line in saw.strip().splitlines() or ["(nothing)"]:
            print(f"          | {line}")


def tc(*args: str) -> str:
    """Read the kernel's own account of things, never the module's."""
    proc = subprocess.run(["tc", *args], capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def dump(label: str, *args: str) -> None:
    """Print a tc listing verbatim, when asked. Off unless SHAPER_CHECK_DUMP is set.

    Because the checks below compare against substrings of a display format, and
    the useful thing to see when one of them is wrong on a machine that is not
    this one is the whole line rather than the fragment somebody guessed at.
    """
    if not os.environ.get("SHAPER_CHECK_DUMP"):
        return
    print(f"  -- {label}: tc {' '.join(args)}")
    for line in (tc(*args).strip() or "(no output)").splitlines():
        print(f"     | {line}")


def word4(address: str) -> str:
    """The 32-bit word a u32 `match ip dst <a>/32` compiles to, as tc prints it."""
    return f"{int(ipaddress.IPv4Address(address)):08x}"


def hashes_at(text: str, offset: int) -> bool:
    """Whether this listing hashes on the last octet at `offset`.

    The mask is written back without the "0x" the command line takes - iproute2
    prints `hash mask 000000ff at 16` for what was asked for as `mask
    0x000000ff` - and both spellings are accepted here rather than one, because
    which of them appears is a property of the build rather than of anything
    this project decides.
    """
    return re.search(rf"hash mask (?:0x)?0*ff at {offset}\b", text) is not None


def run(label, net, net6, client, other, tun, wan) -> None:
    print(f"\n=== {label} ===")
    shaper = Shaper(net, iface=tun, wan=wan, network6=net6)
    ident = minor(net, client)
    offset = ident - CLASS_BASE

    print("attach")
    shaper.attach()
    check("the tunnel carries an htb root", "htb" in tc("qdisc", "show", "dev", tun, "root"))
    check("the tunnel carries an ingress qdisc", "ingress" in tc("qdisc", "show", "dev", tun))
    check("the WAN carries an htb root", "htb" in tc("qdisc", "show", "dev", wan, "root"))
    check("the link class is there", "1:1 " in tc("class", "show", "dev", tun))
    check("the default class is there", "1:2 " in tc("class", "show", "dev", tun))

    dump("egress v4", "filter", "show", "dev", tun, "parent", "1:", "protocol", "ip")
    dump("ingress v4", "filter", "show", "dev", tun, "parent", "ffff:", "protocol", "ip")
    if net6 is not None:
        dump("egress v6", "filter", "show", "dev", tun, "parent", "1:", "protocol", "ipv6")

    egress4 = tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip")
    check("a v4 hash table exists on egress", "ht divisor 256" in egress4, egress4)
    check("the v4 link filter hashes on the last octet", hashes_at(egress4, 16), egress4)
    ingress4 = tc("filter", "show", "dev", tun, "parent", "ffff:", "protocol", "ip")
    check("the v4 ingress hash reads the source octet", hashes_at(ingress4, 12), ingress4)

    if net6 is not None:
        egress6 = tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ipv6")
        check("a v6 hash table exists on egress", "ht divisor 256" in egress6, egress6)
        check("the v6 link filter reads the last word", hashes_at(egress6, 36), egress6)
        ingress6 = tc("filter", "show", "dev", tun, "parent", "ffff:", "protocol", "ipv6")
        check("the v6 ingress hash reads the source word", hashes_at(ingress6, 20), ingress6)

        # The one that would have caught this in the first place. Both filters
        # named `ht 800::`, which is the *first* classifier's root table on the
        # parent - so the v6 one was placed by id into the v4 chain, where no
        # IPv6 packet is ever offered to it. Every command succeeded and every
        # v6 client ran unshaped. A chain must hash on its own family's offsets
        # and on nothing else.
        check(
            "the v4 chain does not carry the v6 filter",
            not hashes_at(egress4, 36) and f"link {shaper_mod.V6.down_ht:x}:" not in egress4,
            egress4,
        )
        check(
            "the v4 ingress chain does not carry the v6 one either",
            not hashes_at(ingress4, 20) and f"link {shaper_mod.V6.mark_ht:x}:" not in ingress4,
            ingress4,
        )

    print("attach is idempotent")
    before = tc("class", "show", "dev", tun)
    shaper.attach()
    check("a second attach changed nothing", tc("class", "show", "dev", tun) == before)

    print("attach repairs rather than walks past")
    # Take one hash table away behind the module's back, the way a run that died
    # half way would have left it, and check the next attach puts it back.
    subprocess.run(
        ["tc", "filter", "del", "dev", tun, "parent", "1:", "protocol", "ip", "prio", "1"],
        capture_output=True,
        check=False,
    )
    dump(
        "egress v4 after the delete", "filter", "show", "dev", tun, "parent", "1:", "protocol", "ip"
    )
    shaper.attach()
    check(
        "the missing v4 hash table was rebuilt",
        "ht divisor 256" in tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip"),
    )

    print("upload can be switched on after download, and off again")
    # The transition, which is the shape everything above is blind to: each check
    # so far starts from a bare interface or a whole structure, and what went
    # wrong on a real server was the move between two states. Shaping download is
    # its own switch, so a server sits in that half for weeks before anybody asks
    # for the other - and then the two qdiscs the upload half needs are the only
    # things missing while the tunnel's root, which is what "is it attached" used
    # to mean, is very much there.
    down_only = Shaper(net, iface=tun, wan="", network6=net6)
    check("everything is up, so the shaper says so", shaper.attached())
    shaper_mod.detach_upload(tun, wan)
    check("the upload half is off the kernel", "htb" not in tc("qdisc", "show", "dev", wan, "root"))
    check(
        "...and it takes the tunnel's marking with it",
        "ingress" not in tc("qdisc", "show", "dev", tun),
    )
    check("a download-only shaper is still attached", down_only.attached())
    check("one that shapes upload is not", not shaper.attached())
    shaper.attach()
    check("attach put the WAN's root back", "htb" in tc("qdisc", "show", "dev", wan, "root"))
    check("and the tunnel's ingress", "ingress" in tc("qdisc", "show", "dev", tun))
    check(
        "and the hash the marking filters go in",
        "ht divisor 256" in tc("filter", "show", "dev", tun, "parent", "ffff:", "protocol", "ip"),
    )

    print("one client, both directions")
    shaper.set_limit(client, down_bps=DOWN, up_bps=UP)
    classes = tc("class", "show", "dev", tun)
    check(f"class 1:{ident:x} exists", f"1:{ident:x} " in classes)
    check("it is capped, not merely guaranteed", "ceil 5Mbit" in classes)
    check("it has its own queue", "fq_codel" in tc("qdisc", "show", "dev", tun))

    down4 = tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip")
    slot4 = handle(net, client, shaper_mod.V4.down_ht)
    check(f"the v4 filter sits in its derived handle {slot4}", f"fh {slot4}" in down4)
    check("it points at the client's class", f"flowid 1:{ident:x}" in down4)
    check("and it compares the client's own address", word4(client) in down4)

    if net6 is not None:
        down6 = tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ipv6")
        slot6 = handle6(offset, shaper_mod.V6.down_ht)
        check(f"the v6 filter sits in its derived handle {slot6}", f"fh {slot6}" in down6)
        check("both families point at the one class", f"flowid 1:{ident:x}" in down6)

    mark4 = tc("filter", "show", "dev", tun, "parent", "ffff:", "protocol", "ip")
    check("the upload is marked on ingress", "skbedit" in mark4)
    check(
        "the mark filter sits in its derived handle",
        f"fh {handle(net, client, shaper_mod.V4.mark_ht)}" in mark4,
    )
    wan_filter = tc("filter", "show", "dev", wan, "parent", "1:")
    check("the WAN filters on the mark", "fw" in wan_filter)
    check(f"the WAN has class 1:{ident:x}", f"1:{ident:x} " in tc("class", "show", "dev", wan))

    print("readback")
    # Both spellings, because limits() reads the first and falls back to the
    # second, and a build that answers neither is one where the reconcile pass
    # believes nothing is shaped and re-applies every ceiling on every cycle.
    dump("classes as json", "-j", "class", "show", "dev", tun)
    dump("classes as text", "class", "show", "dev", tun)
    check("the module reports what the kernel holds", shaper.limits().get(client) == DOWN)
    check("and the same upward", shaper.upload_limits().get(client) == UP)

    print("a second client")
    shaper.set_limit(other, down_bps=DOWN * 2)
    live = shaper.limits()
    check("both clients are in force", live.get(client) == DOWN and live.get(other) == DOWN * 2)

    print("changing a ceiling is one call, not a remove and re-add")
    shaper.set_limit(client, down_bps=DOWN * 3)
    check("the new ceiling took", shaper.limits().get(client) == DOWN * 3)
    check("the upload ceiling was withdrawn with it", client not in shaper.upload_limits())

    print("clear")
    shaper.clear_limit(client)
    check("the client is gone from the tunnel", client not in shaper.limits())
    check(f"class 1:{ident:x} is gone", f"1:{ident:x} " not in tc("class", "show", "dev", tun))
    check("the other client is untouched", shaper.limits().get(other) == DOWN * 2)
    check(
        "no filter is left aiming at the class",
        f"fh {slot4}" not in tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip"),
    )

    print("clearing a client that was never shaped is quiet")
    spare = str(ipaddress.IPv4Address(int(net.network_address) + 3))
    shaper.clear_limit(spare)
    check("nothing raised", True)

    print("detach")
    shaper.detach()
    check("the tunnel root is gone", "htb" not in tc("qdisc", "show", "dev", tun, "root"))
    check("the ingress qdisc is gone", "ingress" not in tc("qdisc", "show", "dev", tun))
    check("the WAN root is gone", "htb" not in tc("qdisc", "show", "dev", wan, "root"))

    # Everything above drove one command per call, which is what an admin editing
    # one client gets. The bring-up hook cannot afford that - four thousand
    # clients is thirty thousand forks - so it sends the same commands to
    # `tc -batch` instead, and the only thing worth checking is that the kernel
    # ends up holding the same thing. It is a different parser on the tc side
    # (its own line splitter rather than the shell's argv) and a single netlink
    # socket for the lot, so agreeing in a unit test proves nothing about here.
    print("the same work as one batch")
    group = [str(ipaddress.IPv4Address(int(net.network_address) + n)) for n in range(2, 12)]
    with shaper.batched():
        shaper.attach()
        for address in group:
            shaper.set_limit(address, down_bps=DOWN, up_bps=UP)

    check("the batch built the root it needed", "htb" in tc("qdisc", "show", "dev", tun, "root"))
    check(
        "and the hash table under it",
        "ht divisor 256" in tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip"),
    )
    live = shaper.limits()
    up = shaper.upload_limits()
    check(f"all {len(group)} ceilings are in force", all(live.get(a) == DOWN for a in group))
    check("and all of them upward too", all(up.get(a) == UP for a in group))
    check(
        "every filter landed in its own derived handle",
        all(
            f"fh {handle(net, address, shaper_mod.V4.down_ht)}"
            in tc("filter", "show", "dev", tun, "parent", "1:", "protocol", "ip")
            for address in group
        ),
    )

    print("and cleared as one batch")
    with shaper.batched():
        for address in group:
            shaper.clear_limit(address)
    check("nothing is left in force", shaper.limits() == {})
    check("nor upward", shaper.upload_limits() == {})

    shaper.detach()


def main(tun: str, wan: str) -> int:
    for label, net, net6, client, other in SCENARIOS:
        run(label, net, net6, client, other, tun, wan)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("every check passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: shaper_check.py <tunnel-iface> <wan-iface>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
