"""Who gets a ceiling, when it is applied, and what the reconcile pass does about drift.

tests/test_shaper.py owns the other half of this - what the commands look like -
and deliberately knows nothing about clients. Everything here is the decision
layer: the settings that switch shaping on, the peers that are eligible, the
attach that must not happen until somebody needs it, and the pass that compares
the kernel against the database.

`tc` is faked throughout for the same reason it is there. What is asserted is
which commands would have been issued, and above all *how many* - the difference
between one batch and one process per client is the difference between a bring-up
that takes a second and one that takes two minutes, and it is invisible in any
test that only checks the result.

AWG_MOCK is on for the whole suite, so every test that wants real behaviour has
to clear it. That is the right default: a developer running these must not have
their own machine's qdiscs rewritten because a fixture changed.
"""

import pytest

from apps.clients import shaping
from apps.clients.models import ClientMeta
from apps.panel import settings_store, views
from awg import shaper as shaper_mod
from awg import store

pytestmark = pytest.mark.django_db

# Above anything a server would really be given, and the number both the API and
# awg.shaper measure a ceiling against now that there is no link rate to.
MAX_BPS = shaping.MAX_BPS


class FakeTc:
    """Records every tc invocation and answers the reads out of `state`.

    `state` maps a command prefix to what tc would print, longest prefix first,
    the same way tests/test_shaper.py does it - the two suites drive the same
    module and a second convention for faking it would be one more thing to keep
    in step.
    """

    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.state = state or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.inputs.append(kwargs.get("input") or "")
        line = " ".join(cmd)
        best = ""
        for prefix in self.state:
            if line.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        return _Proc(0, self.state.get(best, ""))

    @property
    def lines(self) -> list[str]:
        """Every command that reached tc, batched ones expanded into their lines."""
        out: list[str] = []
        for call, payload in zip(self.calls, self.inputs, strict=True):
            if "-batch" in call:
                out.extend(line for line in payload.splitlines() if line)
            else:
                out.append(" ".join(call[1:]))
        return out

    def matching(self, *words: str) -> list[str]:
        return [line for line in self.lines if all(word in line for word in words)]


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# A tunnel carrying the whole download structure: the root qdisc and the filter
# that hashes into the v4 table. Both, because "attached" is a question about
# every piece and not about the root alone - a fixture that answered only the
# root would put every test here on a server the shaper considers half built.
ATTACHED = {
    "tc qdisc show dev awg0 root": "qdisc htb 1: root refcnt 2 default 2",
    "tc filter show dev awg0 parent 1: protocol ip": "filter pref 1 u32 fh 800: link 10:",
}

# ...and the same server shaping upload as well, which is two more qdiscs and a
# second hash: an ingress on the tunnel to mark from and an HTB root on the WAN
# to filter the marks on.
ATTACHED_UPLOAD = {
    **ATTACHED,
    "tc qdisc show dev awg0 ingress": "qdisc ingress ffff: parent ffff:fff1",
    "tc filter show dev awg0 parent ffff: protocol ip": "filter pref 1 u32 fh 800: link 20:",
    "tc qdisc show dev ens3 root": "qdisc htb 1: root refcnt 2 default 2",
}


@pytest.fixture
def tc(monkeypatch):
    """A host that can shape: tc on PATH, no AWG_MOCK, and nothing attached yet."""
    fake = FakeTc()
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper_mod.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(shaper_mod.subprocess, "run", fake)
    return fake


def shaping_on(upload: bool = False) -> None:
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "1" if upload else "0"})


def peer(public_key: str, ip: str, *, enabled: bool = True) -> store.PeerScan:
    return store.PeerScan(
        name=f"c{ip.rsplit('.', 1)[-1]}",
        public_key=public_key,
        ip=ip,
        ip6="",
        enabled=enabled,
        created=None,
    )


def meta(public_key: str, down: int = 0, up: int = 0) -> ClientMeta:
    return ClientMeta.objects.create(public_key=public_key, down_bps=down, up_bps=up)


def plan_for(**over) -> shaping.Plan:
    scan = store.ServerScan(
        peers=[],
        subnet_cidr=over.pop("subnet", "10.13.13.0/24"),
        free_ips=0,
        has_ipv6=False,
        subnet6_cidr=over.pop("subnet6", ""),
        mtu=1400,
    )
    return shaping.plan_from(scan)


# ------------------------------------------------------------------ the settings


def test_shaping_is_off_until_it_is_switched_on(server_conf, clients_env):
    assert shaping.current_plan().on is False
    assert shaping.enabled() is False


def test_the_switch_is_the_whole_of_turning_it_on(server_conf, clients_env):
    shaping_on()
    assert shaping.enabled() is True
    assert shaping.current_plan().on is True


def test_upload_needs_both_the_master_switch_and_its_own(server_conf, clients_env):
    shaping_on(upload=True)
    assert shaping.upload_possible() is True
    settings_store.set_many({"shaperOn": "0"})
    # Upload shaping under a master switch that is off shapes nothing, so it is
    # not possible either - which is what stops the API offering a field that
    # could never be honoured.
    assert shaping.upload_possible() is False


def test_the_wan_falls_back_to_whichever_interface_holds_the_default_route(
    server_conf, clients_env, monkeypatch
):
    monkeypatch.setattr(shaping.sysinfo, "default_iface", lambda: "ens5")
    shaping_on(upload=True)
    assert shaping.current_plan().wan == "ens5"

    settings_store.set_many({"shaperWanIface": "eth9"})
    assert shaping.current_plan().wan == "eth9"


def test_a_plan_with_upload_off_carries_no_wan_at_all(server_conf, clients_env, monkeypatch):
    """Which is what makes an upload ceiling unenforceable rather than half-applied."""
    monkeypatch.setattr(shaping.sysinfo, "default_iface", lambda: "ens5")
    shaping_on(upload=False)
    assert shaping.current_plan().wan == ""


# --------------------------------------------------------------------- who is in


def test_a_client_with_no_ceiling_is_not_in_the_structure(server_conf, clients_env):
    shaping_on()
    plan = plan_for()
    peers = [peer("k1", "10.13.13.2")]
    assert shaping.wanted_from(peers, {"k1": meta("k1")}, plan) == {}


def test_a_switched_off_peer_is_not_shaped(server_conf, clients_env):
    """Its key is off the interface, so a class for it is kernel memory doing nothing."""
    shaping_on()
    plan = plan_for()
    peers = [peer("k1", "10.13.13.2", enabled=False)]
    assert shaping.wanted_from(peers, {"k1": meta("k1", down=5 * shaping.MBIT)}, plan) == {}


def test_a_peer_with_no_metadata_row_is_skipped(server_conf, clients_env):
    shaping_on()
    assert shaping.wanted_from([peer("k1", "10.13.13.2")], {}, plan_for()) == {}


def test_an_address_outside_the_pool_is_skipped_rather_than_raising(server_conf, clients_env):
    """A peer somebody addressed by hand has no place in a structure keyed by offset."""
    shaping_on()
    plan = plan_for()
    peers = [peer("k1", "192.0.2.9"), peer("k2", "10.13.13.3")]
    rows = {"k1": meta("k1", down=5 * shaping.MBIT), "k2": meta("k2", down=5 * shaping.MBIT)}
    assert shaping.wanted_from(peers, rows, plan) == {"10.13.13.3": (5 * shaping.MBIT, 0)}


def test_an_upload_ceiling_is_dropped_when_there_is_nowhere_to_enforce_it(server_conf, clients_env):
    shaping_on(upload=False)
    plan = plan_for()
    peers = [peer("k1", "10.13.13.2")]
    rows = {"k1": meta("k1", down=5 * shaping.MBIT, up=2 * shaping.MBIT)}
    assert shaping.wanted_from(peers, rows, plan) == {"10.13.13.2": (5 * shaping.MBIT, 0)}


def test_a_ceiling_the_tree_cannot_express_is_clamped_rather_than_raised(server_conf, clients_env):
    """The API refuses it where somebody can read the message; a pass has nobody to tell.

    A row this high is a database restored from a later version or written by
    hand, not something the panel can produce - and raising on it would fail the
    same client every minute forever, taking the rest of the pass with it.
    """
    shaping_on()
    plan = plan_for()
    peers = [peer("k1", "10.13.13.2")]
    rows = {"k1": meta("k1", down=MAX_BPS * 5)}
    assert shaping.wanted_from(peers, rows, plan) == {"10.13.13.2": (MAX_BPS, 0)}


def test_nothing_is_wanted_while_shaping_is_off(server_conf, clients_env):
    plan = plan_for()
    peers = [peer("k1", "10.13.13.2")]
    assert shaping.wanted_from(peers, {"k1": meta("k1", down=5 * shaping.MBIT)}, plan) == {}


# -------------------------------------------------------------------- one client


def test_setting_a_ceiling_attaches_the_structure_first(server_conf, clients_env, tc):
    shaping_on()
    assert shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 0) == ""
    assert tc.matching("qdisc replace dev awg0 root", "htb")
    assert tc.matching("class replace dev awg0", "rate 5000000bit")


def test_a_second_ceiling_does_not_rebuild_the_structure(server_conf, clients_env, tc):
    """attach() is a dozen commands, and paying for it on every edit is not free."""
    tc.state = ATTACHED
    shaping_on()
    shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 0)
    assert not tc.matching("qdisc replace dev awg0 root")


def test_a_client_switched_off_has_its_ceiling_taken_away(server_conf, clients_env, tc):
    tc.state = ATTACHED
    shaping_on()
    shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 0, enabled=False)
    assert tc.matching("class del dev awg0", "1:12")
    assert not tc.matching("class replace dev awg0", "rate 5000000bit")


def test_nothing_is_attached_while_no_client_has_a_ceiling(server_conf, clients_env, tc):
    """A WireGuard interface has no qdisc by default, and that is worth keeping."""
    shaping_on()
    assert shaping.apply_client("10.13.13.2", 0, 0) == ""
    assert not tc.matching("qdisc replace dev awg0 root")


def test_the_structure_comes_off_when_the_last_ceiling_goes(server_conf, clients_env, tc):
    tc.state = ATTACHED
    shaping_on()
    shaping.clear_client("10.13.13.2")
    assert tc.matching("qdisc del dev awg0 root")


def test_the_structure_stays_while_somebody_else_still_wants_one(server_conf, clients_env, tc):
    tc.state = ATTACHED
    shaping_on()
    meta("other", down=5 * shaping.MBIT)
    shaping.clear_client("10.13.13.2")
    assert not tc.matching("qdisc del dev awg0 root")


def test_a_ceiling_on_a_host_without_tc_is_stored_and_said_to_be_unenforced(
    server_conf, clients_env, monkeypatch
):
    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper_mod.shutil, "which", lambda name: None)
    shaping_on()
    assert "iproute2" in shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 0)


def test_a_ceiling_under_awg_mock_touches_no_qdisc(server_conf, clients_env):
    """A developer's laptop is not a server, and its own queueing is not ours to rewrite."""
    shaping_on()
    assert "AWG_MOCK" in shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 0)


def test_clearing_a_whole_sweep_is_one_batch_not_one_process_each(server_conf, clients_env, tc):
    tc.state = ATTACHED
    shaping_on()
    meta("other", down=5 * shaping.MBIT)
    addresses = [f"10.13.13.{n}" for n in range(2, 40)]
    shaping.clear_clients(addresses)

    # One invocation for the removals, plus the two reads that decide whether the
    # structure can come off. Never one per client.
    assert sum(1 for call in tc.calls if "-batch" in call) == 1
    assert len(tc.matching("class del dev awg0")) == len(addresses)


# ------------------------------------------------------------------- reconciling


def classes(rows: dict[str, int]) -> str:
    """`tc -j class show` output for a set of {address: rate}, in the fixture's subnet."""
    import json

    return json.dumps(
        [
            {
                "class": "htb",
                "handle": f"1:{int(address.rsplit('.', 1)[-1]) + shaper_mod.CLASS_BASE:x}",
                "rate": rate,
            }
            for address, rate in rows.items()
        ]
    )


def test_a_pass_that_finds_the_kernel_level_writes_nothing(server_conf, clients_env, tc):
    shaping_on()
    tc.state = {
        **ATTACHED,
        "tc -j class show dev awg0": classes({"10.13.13.2": 5 * shaping.MBIT}),
    }
    plan = plan_for()
    moved = shaping.reconcile(plan, {"10.13.13.2": (5 * shaping.MBIT, 0)})
    assert moved == 0
    assert not tc.matching("class replace")


def test_a_changed_ceiling_is_the_only_one_written(server_conf, clients_env, tc):
    shaping_on()
    tc.state = {
        **ATTACHED,
        "tc -j class show dev awg0": classes(
            {"10.13.13.2": 5 * shaping.MBIT, "10.13.13.3": 5 * shaping.MBIT}
        ),
    }
    shaping.reconcile(
        plan_for(),
        {"10.13.13.2": (5 * shaping.MBIT, 0), "10.13.13.3": (9 * shaping.MBIT, 0)},
    )
    assert tc.matching("class replace dev awg0", "rate 9000000bit")
    assert not tc.matching("class replace dev awg0", "rate 5000000bit")


def test_a_ceiling_the_database_no_longer_wants_is_removed(server_conf, clients_env, tc):
    shaping_on()
    tc.state = {
        **ATTACHED,
        "tc -j class show dev awg0": classes({"10.13.13.9": 5 * shaping.MBIT}),
    }
    shaping.reconcile(plan_for(), {"10.13.13.2": (5 * shaping.MBIT, 0)})
    assert tc.matching("class del dev awg0", "1:19")


def test_a_tunnel_that_came_up_without_its_qdiscs_gets_all_of_them_back(
    server_conf, clients_env, tc
):
    """The boot case: the interface is up, the structure is gone, the database still knows."""
    shaping_on()
    wanted = {f"10.13.13.{n}": (5 * shaping.MBIT, 0) for n in range(2, 12)}
    assert shaping.reconcile(plan_for(), wanted) == len(wanted)
    assert tc.matching("qdisc replace dev awg0 root", "htb")
    assert len(tc.matching("class replace dev awg0", "rate 5000000bit")) == len(wanted)


def test_a_rebuilt_structure_re_asserts_every_filter_not_just_the_classes(
    server_conf, clients_env, tc
):
    """A class at the right rate with its filter missing reads as present and shapes nothing."""
    shaping_on()
    wanted = {"10.13.13.2": (5 * shaping.MBIT, 0)}
    shaping.reconcile(plan_for(), wanted)
    assert tc.matching("filter replace dev awg0", "match ip dst 10.13.13.2/32")


def test_a_pass_with_nothing_wanted_takes_the_structure_down(server_conf, clients_env, tc):
    shaping_on()
    tc.state = ATTACHED
    shaping.reconcile(plan_for(), {})
    assert tc.matching("qdisc del dev awg0 root")


def test_a_pass_with_shaping_switched_off_takes_it_down_too(server_conf, clients_env, tc):
    tc.state = ATTACHED
    shaping.reconcile(plan_for(), {})
    assert tc.matching("qdisc del dev awg0 root")


def test_a_pass_over_a_bare_interface_with_nothing_to_do_touches_nothing(
    server_conf, clients_env, tc
):
    shaping.reconcile(plan_for(), {})
    assert not tc.matching("qdisc del")


def test_a_whole_server_is_re_applied_in_one_batch(server_conf, clients_env, tc):
    """The number that decides whether a bring-up takes a second or two minutes."""
    shaping_on()
    wanted = {f"10.13.13.{n}": (5 * shaping.MBIT, 0) for n in range(2, 200)}
    shaping.reconcile(plan_for(), wanted)
    assert sum(1 for call in tc.calls if "-batch" in call) <= 2


def test_the_upload_side_is_read_once_and_not_once_per_client(server_conf, clients_env, tc):
    shaping_on(upload=True)
    settings_store.set_many({"shaperWanIface": "ens3"})
    tc.state = {
        **ATTACHED_UPLOAD,
        "tc -j class show dev awg0": classes(
            {f"10.13.13.{n}": 5 * shaping.MBIT for n in range(2, 60)}
        ),
        "tc -j class show dev ens3": classes(
            {f"10.13.13.{n}": 2 * shaping.MBIT for n in range(2, 60)}
        ),
    }
    plan = plan_for()
    wanted = {f"10.13.13.{n}": (5 * shaping.MBIT, 2 * shaping.MBIT) for n in range(2, 60)}
    assert shaping.reconcile(plan, wanted) == 0
    assert len([call for call in tc.calls if "-j" in call and "ens3" in call]) == 1


# --------------------------------------------------- switching upload on and off
#
# The two halves of this are separate switches, and each of them is a transition
# between two states rather than a state of its own. Everything above drives a
# server that is already in one; these are the moves between them, which is where
# both of the bugs below lived - invisible to a test that starts from a bare
# interface and to one that starts from a fully attached one.


def test_switching_upload_on_later_still_builds_the_wan(server_conf, clients_env, tc):
    """The tunnel is already shaping download, so its root is up and the WAN's is not.

    "Is the structure attached" used to be answered by the tunnel's root alone,
    which is true here and says nothing about the two qdiscs an upload ceiling
    needs. Nothing was built, so tc was asked for a class under a root that was
    not on the WAN and a filter under an ingress that was not on the tunnel, and
    every upload ceiling on the server was enforced by nothing until the next
    bring-up.
    """
    shaping_on(upload=True)
    settings_store.set_many({"shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED)
    shaping.reconcile(
        plan_for(), {"10.13.13.2": (5 * shaping.MBIT, 2 * shaping.MBIT)}, rebuild=True
    )
    assert tc.matching("qdisc replace dev ens3 root", "htb")
    assert tc.matching("qdisc add dev awg0", "ingress")
    assert tc.matching("filter add dev awg0 parent ffff:", "divisor 256")


def test_the_first_upload_ceiling_builds_what_it_needs(server_conf, clients_env, tc):
    """The same hole, reached through the request that sets one client's limit."""
    shaping_on(upload=True)
    settings_store.set_many({"shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED)
    assert shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 2 * shaping.MBIT) == ""
    assert tc.matching("qdisc replace dev ens3 root", "htb")
    assert tc.matching("qdisc add dev awg0", "ingress")


def test_a_fully_attached_server_is_still_left_alone(server_conf, clients_env, tc):
    """The other half of the same question: asking after every piece must not rebuild."""
    shaping_on(upload=True)
    settings_store.set_many({"shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED_UPLOAD)
    shaping.apply_client("10.13.13.2", 5 * shaping.MBIT, 2 * shaping.MBIT)
    assert not tc.matching("qdisc replace dev ens3 root")
    assert not tc.matching("qdisc add dev awg0", "ingress")


# The first client in fixtures/server.conf, so that `wanted()` is not empty and
# the reconcile half of a save leaves the structure up. Without one, every save
# below would be a server where nothing is shaped at all - which takes the whole
# structure down for its own reasons and proves nothing about the WAN.
SHAPED_KEY = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="


def test_switching_upload_off_stops_the_upload_ceilings(server_conf, clients_env, tc):
    """Off has to mean off. The WAN keeps a class per client until something removes it.

    Taking the tunnel's ingress qdisc with it is what actually ends the
    enforcement: nothing is marked any more, so the WAN's classes meter nothing
    and the traffic falls through to a default class that constrains nothing.
    """
    meta(SHAPED_KEY, down=5 * shaping.MBIT)
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "0", "shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED_UPLOAD)
    views._reshape(
        {"shaperUpload": "0"},
        {"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": "ens3"},
    )
    assert tc.matching("qdisc del dev ens3 root")
    assert tc.matching("qdisc del dev awg0 ingress")
    # The download half is a separate switch and is still on.
    assert not tc.matching("qdisc del dev awg0 root")


def test_switching_everything_off_takes_the_wan_with_it(server_conf, clients_env, tc):
    """`shaperOn` off leaves the plan with no WAN to detach, so the save has to carry it."""
    meta(SHAPED_KEY, down=5 * shaping.MBIT)
    settings_store.set_many({"shaperOn": "0", "shaperUpload": "1", "shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED_UPLOAD)
    views._reshape(
        {"shaperOn": "0"},
        {"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": "ens3"},
    )
    assert tc.matching("qdisc del dev ens3 root")
    assert tc.matching("qdisc del dev awg0 root")


def test_moving_the_wan_clears_the_interface_it_came_from(server_conf, clients_env, tc):
    meta(SHAPED_KEY, down=5 * shaping.MBIT)
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": "ens4"})
    tc.state = dict(ATTACHED_UPLOAD)
    views._reshape(
        {"shaperWanIface": "ens4"},
        {"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": "ens3"},
    )
    assert tc.matching("qdisc del dev ens3 root")
    assert not tc.matching("qdisc del dev ens4 root")


def test_a_server_that_never_shaped_upload_deletes_nobodys_qdisc(server_conf, clients_env, tc):
    """An HTB root on a WAN is as likely to be the operator's own, so it is left alone."""
    meta(SHAPED_KEY, down=5 * shaping.MBIT)
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "0", "shaperWanIface": "ens3"})
    tc.state = dict(ATTACHED)
    views._reshape({"shaperOn": "1"}, {"shaperOn": "0", "shaperUpload": "0"})
    assert not tc.matching("qdisc del dev ens3")


def test_the_tunnel_is_not_taken_for_a_wan(server_conf, clients_env, tc, monkeypatch):
    """Shaping both directions on one device writes each rate over the other.

    A box whose default route is its own tunnel would otherwise have every
    client's upload rate replace its download class, and read one back as the
    other - a pass that disagrees with itself and rewrites the whole server
    every minute, for ever.
    """
    shaping_on(upload=True)
    monkeypatch.setattr(shaping.sysinfo, "default_iface", lambda: "awg0")
    assert plan_for().wan == ""


def test_the_tunnel_is_not_taken_for_a_wan_on_the_way_out_either(
    server_conf, clients_env, tc, monkeypatch
):
    """The same box, one save later - and the removal has to agree with the plan.

    Where the upload structure went is answered from the settings as they were,
    and on a server whose default route is its own tunnel that answer is the
    tunnel. Nothing was ever built there, because the plan refuses it; but the
    removal asked the same question without the same refusal, and the interface
    it named has a root qdisc which is the download half. Every client's class
    hangs off it.
    """
    meta(SHAPED_KEY, down=5 * shaping.MBIT)
    monkeypatch.setattr(shaping.sysinfo, "default_iface", lambda: "awg0")
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "0", "shaperWanIface": ""})
    tc.state = dict(ATTACHED)
    views._reshape(
        {"shaperUpload": "0"},
        {"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": ""},
    )
    assert not tc.matching("qdisc del dev awg0 root")


# ------------------------------------------------------- when the subnet moves
#
# The tunnel's network is not fixed. It is chosen at install and can be changed
# afterwards on the server config page, and a client's place in the shaping
# structure is derived from its offset into it - so moving the subnet renumbers
# every class on the interface without a single tc command being run. What these
# pin is that each of the four shapes that move produces something coherent
# rather than a pass that raises once a minute forever.


def test_a_wider_subnet_renumbers_every_class_and_the_pass_catches_up(server_conf, clients_env, tc):
    """10.13.13.2 is offset 2 in a /24 and offset 3330 in the /20 above it.

    Both are addresses the server still routes, so the client keeps working and
    keeps its limit - under a different class id. The old class decodes to an
    address nobody wants, which is exactly what the stale branch removes.
    """
    shaping_on()
    narrow, wide = plan_for(subnet="10.13.13.0/24"), plan_for(subnet="10.13.0.0/20")
    assert shaper_mod.minor(narrow.network, "10.13.13.2") == 0x12
    assert shaper_mod.minor(wide.network, "10.13.13.2") == 0xD12

    # The kernel still holds what the /24 built: class 0x12.
    tc.state = {**ATTACHED, "tc -j class show dev awg0": classes({"10.13.13.2": 5 * shaping.MBIT})}
    shaping.reconcile(wide, {"10.13.13.2": (5 * shaping.MBIT, 0)})

    assert tc.matching("class replace dev awg0", "1:d12")
    assert tc.matching("class del dev awg0", "1:12")


def test_a_client_stranded_outside_the_new_subnet_is_not_shaped(server_conf, clients_env):
    """It has no offset into the pool, so it has no class - and no route either.

    A client left outside the tunnel's network does not work at all; the store
    warns about it on the save that moved the subnet. Skipping it here is the
    same verdict, not a second one.
    """
    shaping_on()
    plan = plan_for(subnet="10.20.0.0/24")
    rows = {"k1": meta("k1", down=5 * shaping.MBIT)}
    assert shaping.wanted_from([peer("k1", "10.13.13.2")], rows, plan) == {}


def test_a_subnet_too_wide_to_shape_switches_shaping_off_rather_than_failing(
    server_conf, clients_env
):
    """An HTB class id is 16 bits, and a /16 has more addresses than that.

    The panel accepts /16 to /30, so this is an ordinary server rather than a
    misconfiguration - and the way it has to read is "off", because the
    alternative is a Shaper that refuses to exist and a pass that raises on
    every cycle for as long as the server runs.
    """
    shaping_on()
    plan = plan_for(subnet="10.13.0.0/16")
    assert plan.unsupported
    assert plan.on is False
    assert "/16" in plan.unsupported


def test_a_pass_on_an_unshapeable_subnet_takes_the_structure_down(server_conf, clients_env, tc):
    """And it must do it without building a Shaper, which is the thing it cannot do."""
    shaping_on()
    tc.state = ATTACHED
    plan = plan_for(subnet="10.13.0.0/16")

    assert shaping.reconcile(plan, {"10.13.13.2": (5 * shaping.MBIT, 0)}) == 0

    assert tc.matching("qdisc del dev awg0 root")
    assert not tc.matching("class replace")


def test_a_limit_cannot_be_set_at_all_on_an_unshapeable_subnet(server_conf, clients_env, tc):
    shaping_on()
    reason = shaping._apply_client(
        plan_for(subnet="10.13.0.0/16"), "10.13.13.2", 5 * shaping.MBIT, 0
    )
    assert "/16" in reason
    assert not tc.matching("class replace")


def test_clearing_still_works_on_an_unshapeable_subnet(server_conf, clients_env, tc):
    """Removing a client must never fail because of a setting it has nothing to do with."""
    shaping_on()
    assert shaping.detach() == ""
    assert tc.matching("qdisc del dev awg0 root")


def test_a_narrower_subnet_still_derives_the_bucket_from_the_address(server_conf, clients_env):
    """The /25 shape, where the offset into the pool and the last octet differ."""
    shaping_on()
    plan = plan_for(subnet="10.13.13.128/25")
    rows = {"k1": meta("k1", down=5 * shaping.MBIT)}
    assert shaping.wanted_from([peer("k1", "10.13.13.130")], rows, plan) == {
        "10.13.13.130": (5 * shaping.MBIT, 0)
    }
    assert shaper_mod.handle(plan.network, "10.13.13.130", shaper_mod.V4.down_ht) == "10:82:800"


def test_a_new_client_is_given_the_default_when_one_is_set(server_conf, clients_env):
    shaping_on(upload=True)
    settings_store.set_many({"shaperDefaultDownMbps": "20", "shaperDefaultUpMbps": "5"})
    assert shaping.default_limits() == (20 * shaping.MBIT, 5 * shaping.MBIT)


def test_there_is_no_default_while_shaping_is_off(server_conf, clients_env):
    """A number nothing would enforce is not a default worth writing against a name."""
    settings_store.set_many({"shaperDefaultDownMbps": "20"})
    assert shaping.default_limits() == (0, 0)


def test_the_upload_default_is_dropped_where_upload_is_not_shaped(server_conf, clients_env):
    shaping_on(upload=False)
    settings_store.set_many({"shaperDefaultDownMbps": "20", "shaperDefaultUpMbps": "5"})
    assert shaping.default_limits() == (20 * shaping.MBIT, 0)


def test_applying_to_everybody_overwrites_what_each_client_had(server_conf, clients_env, tc):
    shaping_on()
    meta("k1", down=500 * shaping.MBIT)
    meta("k2", up=50 * shaping.MBIT)
    meta("k3")
    changed, reason = shaping.apply_to_all(20 * shaping.MBIT, 0)
    assert (changed, reason) == (3, "")
    assert {row.down_bps for row in ClientMeta.objects.all()} == {20 * shaping.MBIT}
    assert {row.up_bps for row in ClientMeta.objects.all()} == {0}


def test_applying_the_limit_everybody_already_has_writes_nothing(server_conf, clients_env, tc):
    """So a second press answers 0 rather than the size of the server."""
    shaping_on()
    meta("k1", down=20 * shaping.MBIT)
    meta("k2", down=20 * shaping.MBIT)
    assert shaping.apply_to_all(20 * shaping.MBIT, 0)[0] == 0


def test_applying_to_everybody_on_a_host_without_tc_says_so(server_conf, clients_env):
    """The one write-everything path must not be the one that reports success it lacks.

    The reconcile pass answers a host it cannot shape by doing nothing and
    saying nothing, which is right on a timer and wrong in a reply: setting the
    same limit on a single client has always said plainly that nothing is
    enforcing it, and the button that writes four thousand of them said the
    opposite.
    """
    shaping_on()
    meta("k1")
    changed, reason = shaping.apply_to_all(20 * shaping.MBIT, 0)
    assert changed == 1
    assert reason == shaping.unavailable() != ""
