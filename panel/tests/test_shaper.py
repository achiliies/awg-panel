"""The shaping layer: what it puts in the kernel, and in which order.

Every test here fakes `tc` and asserts on the argument lists that would have
reached it, because that is the entire product of this module - it holds no
state of its own, and the only thing it can get wrong is the commands. Running
the real binary is not an option in a test suite: it needs root, it would rewrite
the developer's own qdiscs, and on a CI runner there is no tunnel to attach to.

Three properties are worth the ceremony they get below.

Ordering, because a class that appears after the filter aiming at it, or is
removed before it, is a window in which the kernel drops that client's traffic
instead of passing it - a bug that shows up as a brief unexplained outage and
never in a return code.

Derivation, because the ids are computed from the address rather than allocated,
which is only safe if two clients can never land on the same one and a restart
can never compute a different one than the run before it.

And agreement with the kernel's own hashing, which is the one of the three that
has already been got wrong once. A filter's bucket has to be the byte the kernel
will read under `hashkey mask 0x000000ff` - the address's own last octet, not
its offset into the pool. Those are equal on every network ending in .0 and
diverge on the /25 to /30 range subnet.py also accepts, so the cases below are
deliberately run over both shapes: a suite that only ever tested 10.13.0.0/20
passed the whole time the bucket was wrong.
"""

import ipaddress
import json

import pytest

from awg import shaper as shaper_mod
from awg import subnet as subnet_mod
from awg import subnet6 as subnet6_mod
from awg.errors import ValidationError
from awg.shaper import CLASS_BASE, Shaper, ShaperError, handle, handle6, minor, supports

NET = ipaddress.IPv4Network("10.13.0.0/20")
NET6 = ipaddress.IPv6Network("fd00:1:2:3::/64")
IFACE = "awg0"
WAN = "ens3"
LINK = 1_000_000_000

# Aligned and non-aligned, the two shapes the bucket derivation has to survive.
EVERY_SHAPE = ["10.13.0.0/20", "10.13.13.0/24", "10.13.13.128/25", "10.13.13.192/26"]


class FakeTc:
    """Stands in for subprocess.run, recording every command and answering shows.

    `output` maps a command prefix to what the real tc would print; the longest
    matching prefix wins, so a key for `protocol ip` cannot swallow the
    `protocol ipv6` command that happens to start with the same characters.
    Anything unmatched succeeds silently, which is what a mutation does. `fails`
    names commands that should come back non-zero, so the difference between a
    build step and a teardown step can be tested.
    """

    def __init__(self, output: dict[str, str] | None = None, fails: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        # What was fed to each call on stdin, positionally alongside `calls`. Only
        # a batch has any, and it is the whole of what a batch actually asked for.
        self.inputs: list[str] = []
        self.output = output or {}
        self.fails = fails

    def __call__(self, cmd, **kwargs):
        payload = kwargs.get("input") or ""
        self.calls.append(list(cmd))
        self.inputs.append(payload)
        line = " ".join(cmd)
        best = ""
        for prefix in self.output:
            if line.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        if best:
            return _Proc(0, self.output[best])
        # A batch carries its commands on stdin, so `fails` has to be looked for
        # there as well as in the arguments - a real tc refuses the line inside
        # the batch, not the invocation, and a marker that only ever matched the
        # arguments could not describe one client's command failing among many.
        if any(marker in line or marker in payload for marker in self.fails):
            return _Proc(2, "", "RTNETLINK answers: No such file or directory")
        return _Proc(0, "")

    def lines(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def matching(self, *words: str) -> list[str]:
        return [line for line in self.lines() if all(word in line for word in words)]

    def index_of(self, *words: str) -> int:
        """Where the first command containing all of `words` was run."""
        for position, line in enumerate(self.lines()):
            if all(word in line for word in words):
                return position
        raise AssertionError(f"no command matched {words}: {self.lines()}")


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _shapeable(monkeypatch, fake: FakeTc) -> FakeTc:
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper_mod.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(shaper_mod.subprocess, "run", fake)
    return fake


@pytest.fixture
def tc(monkeypatch):
    """A shapeable host: tc on PATH, no AWG_MOCK, and nothing already attached."""
    return _shapeable(monkeypatch, FakeTc())


def build(**kwargs) -> Shaper:
    options = {"iface": IFACE, "wan": WAN}
    options.update(kwargs)
    return Shaper(NET, **options)


def hosts(network: ipaddress.IPv4Network) -> list[str]:
    return [
        str(ipaddress.IPv4Address(value))
        for value in range(subnet_mod.first_host(network), subnet_mod.last_host(network) + 1)
    ]


# ------------------------------------------------------------------ derivation


def test_every_address_in_the_subnet_gets_its_own_class():
    """The ids are derived, so uniqueness is a property to prove, not to trust."""
    seen = {minor(NET, address) for address in hosts(NET)}
    assert len(seen) == subnet_mod.capacity(NET)
    assert max(seen) <= shaper_mod.MAX_MINOR


def test_an_id_is_the_same_one_the_run_before_it_computed():
    """Nothing is stored, so a restarted shaper has to land on the same numbers."""
    assert minor(NET, "10.13.5.9") == minor(NET, "10.13.5.9")
    assert minor(NET, "10.13.0.2") == 0x12


def test_the_reserved_minors_are_left_clear():
    """1:1 is the link and 1:2 takes unclassified traffic; no client may be either."""
    assert minor(NET, hosts(NET)[0]) > shaper_mod.DEFAULT_CLASS_ID


@pytest.mark.parametrize("cidr", EVERY_SHAPE)
def test_the_bucket_is_the_octet_the_kernel_will_actually_hash_on(cidr):
    """`hashkey mask 0x000000ff` reads the address's last byte off the wire.

    Not its offset into the pool. On 10.13.13.128/25 the client at .130 has
    offset 2, and a filter parked in bucket 2 sits where the kernel will never
    look: the packets miss it, fall through to the default class, and the
    ceiling is silently absent while every command that built it succeeded.
    """
    network = ipaddress.IPv4Network(cidr)
    for address in hosts(network):
        bucket = int(handle(network, address, 0x10).split(":")[1], 16)
        assert bucket == int(ipaddress.IPv4Address(address)) & 0xFF, address


@pytest.mark.parametrize("cidr", EVERY_SHAPE)
def test_no_two_clients_are_given_the_same_filter_slot(cidr):
    """Bucket and node together have to separate every client in the pool."""
    network = ipaddress.IPv4Network(cidr)
    slots = {handle(network, address, 0x10) for address in hosts(network)}
    assert len(slots) == subnet_mod.capacity(network)


def test_a_v6_filter_is_keyed_off_the_offset_the_address_is_built_from():
    """subnet6 puts a client at prefix::<offset>, so the octet and offset agree."""
    for address in ("10.13.0.2", "10.13.1.7", "10.13.3.200"):
        offset = minor(NET, address) - CLASS_BASE
        rendered = ipaddress.IPv6Address(subnet6_mod.host_addr(NET6, offset))
        bucket = int(handle6(offset, 0x11).split(":")[1], 16)
        assert bucket == int(rendered) & 0xFF


def test_a_subnet_too_wide_for_a_class_is_refused_with_the_reason():
    wide = ipaddress.IPv4Network("10.0.0.0/16")
    assert "too large to shape" in supports(wide)
    assert supports(NET) == ""
    with pytest.raises(ValidationError, match="too large"):
        Shaper(wide, iface=IFACE)


def test_the_refusal_quotes_a_capacity_that_matches_the_allocator():
    """A number in an error message an admin will act on has to be the real one."""
    roomy = subnet_mod.capacity(ipaddress.IPv4Network("0.0.0.0/17"))
    assert str(roomy) in supports(ipaddress.IPv4Network("10.0.0.0/16"))


def test_an_address_outside_the_pool_is_not_a_client():
    with pytest.raises(ValidationError, match="not a client address"):
        minor(NET, "192.168.1.1")
    # The server's own address is in the network but is not a client.
    with pytest.raises(ValidationError):
        minor(NET, "10.13.0.1")
    with pytest.raises(ValidationError, match="not an IPv4 address"):
        minor(NET, "not-an-ip")


# --------------------------------------------------------------------- attach


def test_attach_builds_a_root_a_link_class_and_a_default_class(tc):
    build().attach()
    assert tc.matching("qdisc replace", IFACE, "root handle 1: htb default 2")
    assert tc.matching("class replace", IFACE, "classid 1:1")
    assert tc.matching("class replace", IFACE, "classid 1:2")


def test_the_default_class_is_not_sized_for_one_clients_share(tc):
    """1:2 carries every unshaped client, and on the WAN the whole host."""
    build().attach()
    line = tc.matching("qdisc replace", IFACE, "parent 1:2")[0]
    assert "fq_codel" in line and "flows" not in line


def test_attach_hashes_instead_of_listing(tc):
    """A 256-bucket table and one linking filter; anything else is a linear scan."""
    build().attach()
    assert tc.matching("filter add", IFACE, "handle 10: u32 divisor 256")
    link = tc.matching("filter add", IFACE, "hashkey mask 0x000000ff at 16")
    assert link and "link 10:" in link[0] and str(NET) in link[0]


def test_the_linking_filter_names_no_hash_table_of_its_own(tc):
    """`ht 800::` is the *first* classifier's root on a parent, not "the root".

    The kernel numbers those roots as it hands them out, so naming one by id put
    the v6 filter - the second - into the v4 chain, where no IPv6 packet is ever
    offered to it. Naming none puts each filter in the root of the protocol and
    priority it was actually created under.
    """
    build(network6=NET6).attach()
    for line in tc.matching("filter add", IFACE, "hashkey"):
        assert " ht 800::" not in line, line


def test_an_interface_that_already_has_the_whole_structure_is_left_alone(monkeypatch):
    """Re-adding a root qdisc would take every existing client class down with it."""
    fake = _shapeable(
        monkeypatch,
        FakeTc(
            output={
                f"tc qdisc show dev {IFACE} root": "qdisc htb 1: root refcnt 2",
                f"tc filter show dev {IFACE} parent 1: protocol ip": (
                    "filter pref 1 u32 chain 0 fh 800::800 key ht 800 bkt 0 link 10:"
                ),
            }
        ),
    )
    build(wan="").attach()
    assert not fake.matching("qdisc replace", "root handle")
    assert not fake.matching("divisor 256")


def test_a_hash_table_that_outlived_its_chain_is_relinked_rather_than_re_added(monkeypatch):
    """The state that wedged every bring-up on a server this had ever half-run on.

    An explicitly created hash table belongs to the u32 block shared across the
    parent, not to one classifier, so it survives the chain being deleted and
    stops being listed with it. Asking after the table therefore answered "gone"
    about a table that was still there, and re-creating it failed with "Filter
    already exists" - for good, since nothing ever cleared it.
    """
    fake = _shapeable(
        monkeypatch,
        FakeTc(
            output={f"tc qdisc show dev {IFACE} root": "qdisc htb 1: root refcnt 2"},
            fails=("divisor 256",),
        ),
    )
    build(wan="").attach()

    # It tries, it is refused, and it carries on to the step that matters.
    assert fake.matching("filter add", IFACE, "divisor 256")
    assert fake.matching("filter add", IFACE, "link 10:")


def test_a_half_built_interface_is_repaired_rather_than_walked_past(monkeypatch):
    """attach() is what PostUp re-runs, so it has to finish a job that died."""
    fake = _shapeable(
        monkeypatch,
        FakeTc(output={f"tc qdisc show dev {IFACE} root": "qdisc htb 1: root refcnt 2"}),
    )
    build(wan="").attach()
    # The root is ours already and must not be rebuilt...
    assert not fake.matching("qdisc replace", "root handle")
    # ...but the hash table that never got made is.
    assert fake.matching("filter add", IFACE, "divisor 256")


# ------------------------------------------------------------ is it all there
#
# What `attached()` answers decides whether attach() runs at all, so it has to be
# a question about the whole structure. It was the tunnel's root qdisc alone,
# which is a different question on any server that shapes upload: the two qdiscs
# that half needs are elsewhere, and a server already shaping download has the
# root without them.


def _up(**over: str) -> dict[str, str]:
    """What tc prints for a server carrying the whole structure, both directions."""
    state = {
        f"tc qdisc show dev {IFACE} root": "qdisc htb 1: root refcnt 2",
        f"tc filter show dev {IFACE} parent 1: protocol ip": "filter pref 1 u32 fh 800: link 10:",
        f"tc qdisc show dev {IFACE} ingress": "qdisc ingress ffff: parent ffff:fff1",
        f"tc filter show dev {IFACE} parent ffff: protocol ip": (
            "filter pref 1 u32 fh 800: link 20:"
        ),
        f"tc qdisc show dev {WAN} root": "qdisc htb 1: root refcnt 2",
    }
    state.update(over)
    return state


def test_everything_in_place_is_attached(monkeypatch):
    _shapeable(monkeypatch, FakeTc(output=_up()))
    assert build().attached() is True


@pytest.mark.parametrize(
    "gone",
    [
        f"tc qdisc show dev {IFACE} root",
        f"tc filter show dev {IFACE} parent 1: protocol ip",
        f"tc qdisc show dev {IFACE} ingress",
        f"tc filter show dev {IFACE} parent ffff: protocol ip",
        f"tc qdisc show dev {WAN} root",
    ],
)
def test_any_piece_missing_is_not_attached(monkeypatch, gone):
    """Each of these on its own is a structure attach() has work to do on."""
    state = _up()
    del state[gone]
    _shapeable(monkeypatch, FakeTc(output=state))
    assert build().attached() is False


def test_the_wan_being_bare_is_the_state_switching_upload_on_arrives_in(monkeypatch):
    """A server shaping download, told to shape upload as well: root yes, WAN no."""
    state = _up()
    del state[f"tc qdisc show dev {WAN} root"]
    del state[f"tc qdisc show dev {IFACE} ingress"]
    del state[f"tc filter show dev {IFACE} parent ffff: protocol ip"]
    fake = _shapeable(monkeypatch, FakeTc(output=state))
    shaper = build()
    assert shaper.attached() is False
    shaper.attach()
    # The tunnel's root is ours already and is not rebuilt; the upload half is.
    assert not fake.matching("qdisc replace", IFACE, "root handle")
    assert fake.matching("qdisc replace", WAN, "root handle")
    assert fake.matching("qdisc add", IFACE, "ingress")


def test_a_download_only_shaper_asks_nothing_about_an_upload_it_does_not_shape(monkeypatch):
    fake = _shapeable(monkeypatch, FakeTc(output=_up()))
    assert build(wan="").attached() is True
    assert not fake.matching("show dev", WAN)
    assert not fake.matching("show dev", IFACE, "ingress")


def test_marking_is_only_built_when_there_is_a_wan_to_filter_on_it(tc):
    """A mark nothing reads is per-packet work for nothing."""
    build(wan="").attach()
    assert not tc.matching("ingress")


def test_upload_marking_rides_on_the_tunnels_ingress(tc):
    build().attach()
    assert tc.matching("qdisc add", IFACE, "handle ffff: ingress")
    link = tc.matching("filter add", IFACE, "parent ffff:", "hashkey mask 0x000000ff at 12")
    assert link and "link 20:" in link[0]


# ------------------------------------------------------------------ both families


def test_v6_gets_its_own_table_at_its_own_priority(tc):
    """protocol ip and protocol ipv6 are separate chains and cannot share one."""
    build(network6=NET6).attach()
    assert tc.matching("filter add", IFACE, "protocol ipv6", "handle 11: u32 divisor 256")
    link = tc.matching("filter add", IFACE, "protocol ipv6", "hashkey mask 0x000000ff at 36")
    assert link and "link 11:" in link[0] and str(NET6) in link[0]


def test_the_v6_ingress_hash_reads_the_last_word_of_the_source_address(tc):
    """An IPv6 source sits at byte 8; its last four bytes, which is the key, at 20."""
    build(network6=NET6).attach()
    assert tc.matching("filter add", IFACE, "parent ffff:", "protocol ipv6", "at 20")


def test_both_families_point_at_the_one_class(tc):
    """A ceiling is a total. Two classes would be two allowances."""
    build(network6=NET6).set_limit("10.13.0.2", down_bps=5_000_000)
    v4 = tc.matching("filter replace", IFACE, "protocol ip ", "match ip dst 10.13.0.2/32")
    v6 = tc.matching("filter replace", IFACE, "protocol ipv6", "match ip6 dst fd00:1:2:3::2/128")
    assert v4 and v6
    assert "flowid 1:12" in v4[0] and "flowid 1:12" in v6[0]
    assert len(tc.matching("class replace", IFACE, "classid 1:12")) == 1


def test_a_v6_upload_is_marked_with_the_same_id_as_a_v4_one(tc):
    build(network6=NET6).set_limit("10.13.0.2", up_bps=2_000_000)
    assert tc.matching("parent ffff:", "protocol ipv6", "match ip6 src", "skbedit mark 18")
    assert tc.matching("parent ffff:", "protocol ip ", "match ip src", "skbedit mark 18")
    # One fw filter serves both, because by then only the mark is being read.
    assert tc.matching("filter replace", WAN, "protocol all", "handle 18 fw")


def test_a_server_without_v6_is_shaped_on_v4_alone(tc):
    """--ipv6 off is a real mode; inventing a v6 network for it would be worse."""
    build().set_limit("10.13.0.2", down_bps=5_000_000)
    assert not tc.matching("ipv6")
    assert not tc.matching("ip6")


def test_clearing_takes_both_families_away(tc):
    build(network6=NET6).clear_limit("10.13.0.2")
    assert tc.matching("filter del", IFACE, "protocol ip ", "handle 10:2:800")
    assert tc.matching("filter del", IFACE, "protocol ipv6", "handle 11:2:800")


# ------------------------------------------------------------------ one client


def test_the_class_exists_before_the_filter_that_points_at_it(tc):
    """The reverse order classifies packets into a class that is not there yet."""
    build().set_limit("10.13.0.2", down_bps=5_000_000)
    assert tc.index_of("class replace", IFACE, "classid 1:12") < tc.index_of(
        "filter replace", IFACE, "match ip dst 10.13.0.2/32"
    )


def test_a_filter_is_removed_before_the_class_it_aims_at(tc):
    """A classifier aiming at a class that has gone drops the packets it aims."""
    build().clear_limit("10.13.0.2")
    assert tc.index_of("filter del", IFACE, "handle 10:2:800") < tc.index_of(
        "class del", IFACE, "classid 1:12"
    )


def test_a_ceiling_is_a_hard_cap_and_not_a_floor_to_borrow_from(tc):
    """rate and ceil both, so a quiet link does not hand the client more than it bought."""
    build().set_limit("10.13.0.2", down_bps=5_000_000)
    line = tc.matching("class replace", IFACE, "classid 1:12")[0]
    assert "rate 5000000bit" in line and "ceil 5000000bit" in line


def test_the_leaf_queue_is_removed_before_it_is_added_back(tc):
    """`replace` on a leaf that is already there changes it, and fq_codel refuses.

    The kernel builds a fresh qdisc only when the kind it is handed differs from
    the kind already hanging off that parent. Otherwise it reads the request as
    "change this one", and fq_codel will not have its flow count changed after
    setup - so every second call for a client came back EINVAL, which is to say
    raising or lowering somebody's speed failed and only the first setting they
    were ever given worked. Leaving the handle off does not change the kernel's
    mind, because it does not consult the handle. Deleting the leaf does.
    """
    build(wan="").set_limit("10.13.0.5", 10_000_000)
    assert not tc.matching("qdisc replace", IFACE, "parent 1:15")
    assert tc.index_of("qdisc del", IFACE, "parent 1:15") < tc.index_of(
        "qdisc add", IFACE, "parent 1:15", "fq_codel"
    )


def test_the_leaf_delete_goes_out_ahead_of_the_add_in_a_batch_too(tc):
    """A batch sends what it was given in the order it was given it."""
    shaper = build(wan="")
    with shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)
    ((_, lines),) = batches(tc)
    gone = next(
        n for n, line in enumerate(lines) if line.startswith("qdisc del dev awg0 parent 1:15")
    )
    back = next(
        n for n, line in enumerate(lines) if line.startswith("qdisc add dev awg0 parent 1:15")
    )
    assert gone < back


def test_a_shaped_class_gets_its_own_queue(tc):
    """HTB's default leaf is a pfifo sized for an unshaped link: seconds of delay."""
    build().set_limit("10.13.0.2", down_bps=5_000_000)
    assert tc.matching("qdisc add", IFACE, "parent 1:12", "fq_codel", "flows 128")


def test_upload_is_marked_on_the_tunnel_and_filtered_on_the_wan(tc):
    """The source address is gone by the WAN's egress, so the mark is what is left."""
    build().set_limit("10.13.0.2", up_bps=2_000_000)
    assert tc.matching("filter replace", IFACE, "parent ffff:", "action skbedit mark 18")
    assert tc.matching("filter replace", WAN, "handle 18 fw", "flowid 1:12")
    assert tc.matching("class replace", WAN, "classid 1:12", "rate 2000000bit")


def test_the_two_directions_are_independent(tc):
    """Capped downward and left alone upward is a thing an operator may want."""
    build().set_limit("10.13.0.2", down_bps=5_000_000)
    assert tc.matching("class replace", IFACE, "classid 1:12")
    # No upload ceiling was asked for, so the upload side is actively cleared.
    assert not tc.matching("class replace", WAN, "classid 1:12")
    assert tc.matching("class del", WAN, "classid 1:12")


def test_zero_in_both_directions_is_the_same_as_clearing(tc):
    build().set_limit("10.13.0.2", down_bps=0, up_bps=0)
    assert tc.matching("class del", IFACE, "classid 1:12")
    assert not tc.matching("class replace", IFACE, "classid 1:12")


# ------------------------------------------------------------------ validation


def test_an_upload_ceiling_with_nowhere_to_enforce_it_is_an_error(tc):
    """Silently dropping it is the exact failure this module keeps promising not to have."""
    with pytest.raises(ValidationError, match="needs a WAN interface"):
        build(wan="").set_limit("10.13.0.2", up_bps=2_000_000)
    # And nothing was half-applied on the way to finding out.
    assert not tc.matching("class replace")


def test_a_download_ceiling_still_works_without_a_wan(tc):
    build(wan="").set_limit("10.13.0.2", down_bps=5_000_000)
    assert tc.matching("class replace", IFACE, "classid 1:12")


def test_a_ceiling_above_what_the_tree_can_express_is_refused(tc):
    with pytest.raises(ValidationError, match="this can shape"):
        build().set_limit("10.13.0.2", down_bps=shaper_mod.ROOT_BPS * 2)


def test_a_ceiling_the_uplink_could_not_deliver_is_not_an_error(tc):
    """It is a limit that never gets reached, which is nobody's problem.

    There was a link rate here once and this was refused against it. The rate
    could not be discovered and had to be typed, so it was a guess at the
    server's own uplink - and refusing a number for being above a guess is worse
    than accepting one that will not be hit.
    """
    build().set_limit("10.13.0.2", down_bps=10_000_000_000)
    assert tc.matching("class replace", IFACE, "classid 1:12", "rate 10000000000bit")


def test_a_negative_or_fractional_ceiling_is_refused(tc):
    with pytest.raises(ValidationError):
        build().set_limit("10.13.0.2", down_bps=-1)
    with pytest.raises(ValidationError):
        build().set_limit("10.13.0.2", down_bps=True)


def test_the_root_is_rated_high_enough_to_constrain_nothing(tc):
    """Every class hangs under it, so it must never be the thing that caps one."""
    build().attach()
    line = tc.matching("class replace", IFACE, "classid 1:1 ")[0]
    assert f"rate {shaper_mod.ROOT_BPS}bit" in line and f"ceil {shaper_mod.ROOT_BPS}bit" in line
    # Stated rather than derived from the rate, which is what makes htb print
    # "quantum of class 10001 is big. Consider r2q change." on every attach.
    assert f"quantum {shaper_mod.ROOT_QUANTUM}" in line


def test_shaping_refuses_to_touch_a_developers_own_qdiscs(monkeypatch):
    """AWG_MOCK is a laptop. firewall.py makes the same promise about ufw."""
    monkeypatch.setenv("AWG_MOCK", "1")
    assert "AWG_MOCK" in shaper_mod.available()
    with pytest.raises(ShaperError, match="AWG_MOCK"):
        build().attach()


def test_a_host_without_iproute2_says_so_rather_than_failing_obscurely(monkeypatch):
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper_mod.shutil, "which", lambda name: None)
    assert "iproute2" in shaper_mod.available()


# -------------------------------------------------------------------- failure


def test_a_failed_build_raises_because_the_caller_asked_for_a_change(monkeypatch):
    _shapeable(monkeypatch, FakeTc(fails=("class replace",)))
    with pytest.raises(ShaperError):
        build().set_limit("10.13.0.2", down_bps=5_000_000)


def test_a_failed_delete_is_not_news(monkeypatch):
    """Clearing a client that was never shaped ends in exactly this, every time."""
    _shapeable(monkeypatch, FakeTc(fails=("del",)))
    build().clear_limit("10.13.0.2")  # must not raise
    build().detach()


# --------------------------------------------------------------------- readback


def test_what_is_in_force_is_read_from_the_kernel_not_remembered(monkeypatch):
    """After a bring-up wipes the qdiscs, only the kernel knows what survived."""
    rows = [
        {"class": "htb", "handle": "1:1", "rate": LINK},
        {"class": "htb", "handle": "1:2", "rate": LINK},
        {"class": "htb", "handle": "1:12", "rate": 5_000_000},
        {"class": "htb", "handle": "1:1f", "rate": 9_000_000},
    ]
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": json.dumps(rows)}))
    assert build().limits() == {"10.13.0.2": 5_000_000, "10.13.0.15": 9_000_000}


def test_a_rate_spelled_with_units_reads_the_same_as_a_bare_number(monkeypatch):
    """Some iproute2 builds print "5Mbit" where others print 5000000."""
    rows = [
        {"class": "htb", "handle": "1:12", "rate": "5Mbit"},
        {"class": "htb", "handle": "1:13", "rate": "800Kbit"},
        # tc's "bps" is bytes per second, so this is 8 Mbit, not 1.
        {"class": "htb", "handle": "1:14", "rate": "1Mbps"},
    ]
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": json.dumps(rows)}))
    assert build().limits() == {
        "10.13.0.2": 5_000_000,
        "10.13.0.3": 800_000,
        "10.13.0.4": 8_000_000,
    }


def test_the_human_listing_is_read_when_minus_j_is_ignored(monkeypatch):
    """iproute2 6.1 accepts `-j` on `class show`, ignores it, and exits zero.

    Which is what Ubuntu 24.04 and Debian 12 ship. Taking only the JSON made
    limits() answer "nothing is shaped" there, and the reconcile pass believes
    it: it finds every ceiling missing and rewrites all of them on every cycle,
    for ever, without anything reporting a fault.
    """
    listing = (
        "class htb 1:1 root rate 1Gbit ceil 1Gbit burst 1600b cburst 1600b\n"
        "class htb 1:2 parent 1:1 leaf 2: prio 0 rate 1Gbit ceil 1Gbit burst 1600b cburst 1600b\n"
        "class htb 1:12 parent 1:1 leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst 1600b\n"
        "class htb 1:1f parent 1:1 leaf 8002: prio 0 rate 800Kbit ceil 800Kbit burst 1600b\n"
    )
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": listing}))
    assert build().limits() == {"10.13.0.2": 5_000_000, "10.13.0.15": 800_000}


def test_a_leaf_qdiscs_own_classes_are_not_read_as_clients(monkeypatch):
    """An fq_codel with traffic in it reports a class per busy flow, `8001:12` upwards.

    Those minors run straight through the range a client's is drawn from, so the
    major is what says whether a row is ours.
    """
    listing = (
        "class htb 1:12 parent 1:1 leaf 8001: prio 0 rate 5Mbit ceil 5Mbit\n"
        "class fq_codel 8001:1f parent 8001:\n"
        "class fq_codel 2:3ff parent 2:\n"
    )
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": listing}))
    assert build().limits() == {"10.13.0.2": 5_000_000}


def test_a_tc_that_refuses_minus_j_outright_is_asked_again_without_it(monkeypatch):
    """Older still: the flag is unknown, so the whole command fails and prints nothing."""
    fake = _shapeable(
        monkeypatch,
        FakeTc(
            output={f"tc class show dev {IFACE}": "class htb 1:12 parent 1:1 rate 5Mbit\n"},
            fails=("-j",),
        ),
    )
    assert build().limits() == {"10.13.0.2": 5_000_000}
    assert fake.lines()[:2] == [f"tc -j class show dev {IFACE}", f"tc class show dev {IFACE}"]


def test_output_that_is_neither_shape_is_no_limits_rather_than_a_crash(monkeypatch):
    """Whatever a future release prints, asking what is in force must not raise."""
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": "who knows\n"}))
    assert build().limits() == {}


def test_a_class_outside_the_client_range_is_not_reported_as_a_client(monkeypatch):
    """1:1 and 1:2 are ours and are not anybody's ceiling."""
    rows = [{"class": "htb", "handle": "1:1", "rate": LINK}]
    _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": json.dumps(rows)}))
    assert build().limits() == {}


def test_detach_takes_the_whole_structure_rather_than_one_client_at_a_time(tc):
    build().detach()
    assert tc.matching("qdisc del", IFACE, "root")
    assert tc.matching("qdisc del", IFACE, "ingress")
    assert tc.matching("qdisc del", WAN, "root")


# ---------------------------------------------------------------------- batching
#
# The cost these are about does not show up in any single command. One ceiling is
# four to nine forks and nobody notices; every client on a four thousand client
# server is thirty thousand of them, in a PostUp hook, with the tunnel down for
# the two minutes they take. So what is asserted here is the shape of what
# reaches tc - one invocation rather than one per command - that the order the
# commands were queued in is the order they are sent in, and that a failure still
# means what it meant when each of them ran on its own.


def batches(fake: FakeTc) -> list[tuple[str, list[str]]]:
    """Every batch that was sent: how tc was invoked, and the lines it was fed."""
    return [
        (" ".join(call), [line for line in payload.splitlines() if line])
        for call, payload in zip(fake.calls, fake.inputs, strict=True)
        if "-batch" in call
    ]


def test_a_batch_is_one_invocation_however_many_clients(tc):
    shaper = build(network6=NET6)
    with shaper.batched():
        for host in hosts(NET)[:50]:
            shaper.set_limit(host, 10_000_000)

    # One call, not five hundred.
    assert len(tc.calls) == 1
    assert sum(len(lines) for _, lines in batches(tc)) == 50 * 10


def test_a_batch_keeps_the_order_the_commands_were_queued_in(tc):
    """Which is the whole of what makes a batch mean the same as running them.

    A hash table has to exist before the filter that links to it, and the qdisc
    both hang off has to exist before either. Creating that table is allowed to
    fail, because it is already there whenever the chain alone was lost - and
    when "allowed to fail" was what decided the sending order, it went out ahead
    of the root qdisc, failed for real, and took the linking filter with it. So a
    batch that attached and then shaped anybody could not work at all.
    """
    shaper = build(wan="")
    with shaper.batched():
        shaper.attach()

    ((_, lines),) = batches(tc)
    root = next(n for n, line in enumerate(lines) if line.startswith("qdisc replace dev awg0 root"))
    table = next(n for n, line in enumerate(lines) if "u32 divisor 256" in line)
    link = next(n for n, line in enumerate(lines) if "link 10:" in line)
    assert root < table < link


def test_a_batch_sends_the_commands_without_the_binary_in_front_of_them(tc):
    shaper = build(wan="")
    with shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)

    ((_, lines),) = batches(tc)
    assert lines[0].startswith("class replace dev ")
    assert not any(line.startswith("tc ") for line in lines)


def test_a_batch_does_not_abandon_what_comes_after_a_refusal(tc):
    """One client that cannot be shaped must not be four thousand that are not.

    Without -force, tc stops at the first command it could not run - so a single
    refusal partway through a full re-apply left every client after it in the
    list with no ceiling at all, and nothing about the failure said which ones.
    """
    shaper = build()
    with shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)

    assert [argv for argv, _ in batches(tc)] == [f"{shaper_mod.TC} -force -batch -"]


def test_a_batch_still_raises_when_something_was_refused(tc):
    """Stepping over a failure is not the same as reporting success."""
    tc.fails = ("class replace",)
    shaper = build(wan="")
    with pytest.raises(ShaperError), shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)


def test_a_refused_build_in_a_batch_is_still_reported(tc):
    """A ceiling that did not land raises, as it does when the command runs alone."""
    tc.fails = ("-batch",)
    shaper = build(wan="")
    with pytest.raises(ShaperError), shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)


def test_a_refused_removal_in_a_batch_is_not_news(tc):
    """As when it ran alone: clearing a client that was never shaped fails, and should."""
    tc.fails = ("-force",)
    shaper = build()
    with shaper.batched():
        shaper.clear_limit("10.13.0.5")


def test_only_the_lines_somebody_was_relying_on_are_worth_raising_over(monkeypatch):
    """`-force` names every line it refused, and that is what tells the two apart.

    A batch mixes commands nobody minds failing - a removal on a client that was
    never shaped, a hash table that is already there - with the ones a caller
    asked for and has to be told about. Sending them as two separate invocations
    used to be how they were told apart; keeping the queued order costs that, so
    tc's own account of which lines it would not run is read instead.
    """
    refuse: list[str] = []

    class Refusing(FakeTc):
        def __call__(self, cmd, **kwargs):
            proc = super().__call__(cmd, **kwargs)
            if "-batch" not in cmd:
                return proc
            lines = (kwargs.get("input") or "").splitlines()
            at = next(n for n, line in enumerate(lines) if refuse[0] in line)
            return _Proc(1, "", f"RTNETLINK answers: File exists\nCommand failed -:{at + 1}")

    _shapeable(monkeypatch, Refusing())
    shaper = build(wan="")

    # A hash table that was already there. Refused, and nothing was relying on it.
    refuse[:] = ["u32 divisor 256"]
    with shaper.batched():
        shaper.attach()

    # The class a ceiling needs. Refused, and the caller has to hear about it -
    # by the command itself, because tc names a line of a file nobody can look at.
    refuse[:] = ["class replace"]
    with pytest.raises(ShaperError, match=r"tc class replace dev awg0 .* classid 1:15"):
        with shaper.batched():
            shaper.set_limit("10.13.0.5", 10_000_000)


def test_a_batch_that_tc_will_not_attribute_is_raised_on_regardless(tc):
    """ "Something failed and I cannot say what" is not evidence that it was harmless."""
    tc.fails = ("-batch",)  # a non-zero exit with no "Command failed" line in it
    shaper = build(wan="")
    with pytest.raises(ShaperError), shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)


def test_a_batch_that_could_not_be_worked_out_sends_nothing(tc):
    """Half a decision is not a state anybody asked for."""
    shaper = build(wan="")
    with pytest.raises(ValidationError), shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)
        shaper.set_limit("10.13.0.6", 0, 5_000_000)  # no wan to enforce it on
    assert tc.calls == []


def test_nesting_leaves_the_flush_to_the_outermost_block(tc):
    shaper = build(wan="")
    with shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000)
        with shaper.batched():
            shaper.set_limit("10.13.0.6", 10_000_000)
        assert tc.calls == []
    assert len(tc.calls) == 1


def test_a_read_inside_a_batch_still_goes_to_the_kernel(monkeypatch):
    """It has to return something, and nothing queued has happened yet."""
    fake = _shapeable(monkeypatch, FakeTc(output={f"tc -j class show dev {IFACE}": "[]"}))
    shaper = build(wan="")
    with shaper.batched():
        assert shaper.limits() == {}
        shaper.set_limit("10.13.0.5", 10_000_000)
    assert " ".join(fake.calls[0]) == f"tc -j class show dev {IFACE}"


def test_attaching_inside_a_batch_builds_the_whole_structure(tc):
    """The bring-up path, which is the reason any of this exists."""
    shaper = build(network6=NET6)
    with shaper.batched():
        shaper.attach()

    lines = [line for _, payload in batches(tc) for line in payload]
    assert any(line.startswith(f"qdisc replace dev {IFACE} root handle 1: htb") for line in lines)
    assert any(f"handle {shaper_mod.V6.down_ht:x}: u32 divisor 256" in line for line in lines)
    assert any(f"dev {WAN} root handle 1: htb" in line for line in lines)


def test_a_batch_refuses_a_token_tc_would_split(tc):
    """An interface name with a space in it is a mistake, not a thing to escape around."""
    shaper = build(wan="eth0 evil")
    with pytest.raises(ShaperError, match="space or a quote"), shaper.batched():
        shaper.set_limit("10.13.0.5", 10_000_000, 5_000_000)


def test_a_failed_batch_names_the_command_and_not_a_line_of_a_file():
    """tc says "Command failed -:2", which is a line of stdin nobody can look at."""
    lines = ["class add dev awg0 parent 1:1 classid 1:15 htb rate 1bit", "qdisc add dev awg0 x"]
    said = shaper_mod._batch_stderr(
        "RTNETLINK answers: Invalid argument\nCommand failed -:2", lines, [1]
    )
    # In front of tc's own words, because a ToolError shows the first line only.
    assert said.splitlines()[0] == ("tc qdisc add dev awg0 x: RTNETLINK answers: Invalid argument")
    # A failure nothing was blamed for is passed through as it stands.
    assert shaper_mod._batch_stderr("no such device", lines) == "no such device"


def test_the_lines_tc_refused_are_read_out_of_what_it_said():
    """None, not an empty set, when it would not say - the two are different answers."""
    assert shaper_mod._refused_lines("Command failed -:1\nCommand failed -:17") == {0, 16}
    assert shaper_mod._refused_lines("RTNETLINK answers: No such file") is None
    assert shaper_mod._refused_lines("") is None
