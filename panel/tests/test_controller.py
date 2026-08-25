"""The tunnel layer: parsing `awg show dump`, and behaving when there is no awg.

Two obligations are tested here. First, the dump parser has to survive the exact
output the real tool produces, "(none)" placeholders and an interface with no
peers included - the collector runs this every two seconds and a parse error
there stops all accounting. Second, every read-only method has to answer rather
than raise when the binary is missing: the status page exists precisely to tell
an admin that AmneziaWG is not installed, and it cannot do that if reading the
status is what crashes.

The mock is tested as a component in its own right because the whole panel runs
on it under AWG_MOCK=1, and a mock that disagrees with the config on disk would
send every developer chasing a bug that is not in the product.
"""

import shutil
import time
from unittest import mock

import pytest

from awg import conf as conf_mod
from awg import controller as controller_mod
from awg import keys, lock, store
from awg.controller import AwgController, Dump, parse_dump
from awg.errors import ToolError
from awg.mock import MockController

# `awg show awg0 dump` for the fixture server: the interface line, then an idle
# peer whose endpoint and handshake are still unset, then a connected one with
# no preshared key. Built from fields rather than written as one blob because
# the separator is a tab and a stray space would make the test lie.
INTERFACE_ROW = [
    "eEm+CjQ0q0o3MCTDexoRJMv3A8wz9PVZVJbqj1yfXUU=",  # private key
    "BBKNFzBhlBZLbRu6o5YaxGYfYy9H3XQJ0VcKG/FBPkY=",  # public key
    "41234",  # listen port
    "off",  # fwmark
]
IDLE_PEER_ROW = [
    "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ=",
    "a5PQ57ToVX91TckGUHDDDTfdQQu4UIFOB7Ew0kLPpm0=",
    "(none)",  # never connected, so no endpoint
    "10.13.13.2/32",
    "0",
    "0",
    "0",
    "off",
]
LIVE_PEER_ROW = [
    "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4=",
    "(none)",  # no preshared key configured
    "198.51.100.7:51820",
    "10.13.13.3/32",
    "1754332800",
    "918273",
    "6553600",
    "25",
]
REAL_DUMP = "".join("\t".join(row) + "\n" for row in (INTERFACE_ROW, IDLE_PEER_ROW, LIVE_PEER_ROW))


@pytest.fixture
def no_awg(monkeypatch, tmp_path):
    """A machine with none of it: no `awg`, `modinfo` or `systemctl`, no module.

    An empty PATH takes care of the binaries, but not of the module: the
    version of a *loaded* module is read straight out of sysfs, which is a
    filesystem path and not a subprocess, so PATH has no bearing on it. Left
    alone, these tests read the module of whatever machine is running them -
    which is nothing on a CI runner and a real version on any server the panel
    is installed on, so the suite passed exactly where nobody was looking and
    failed where somebody was.

    Pointing SYSFS_MODULE at an empty tree is what makes "nothing is installed"
    true rather than merely intended. module_loaded() also consults
    /proc/modules and is not covered by this, which is why nothing here claims
    it returns False.
    """
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(controller_mod, "SYSFS_MODULE", tmp_path / "empty-sys")
    assert shutil.which("awg") is None
    assert controller_mod._sysfs_module_version() is None


# ---------------------------------------------------------- dump parsing


def test_parse_dump_reads_the_interface_line():
    dump = parse_dump(REAL_DUMP)

    assert dump.private_key == "eEm+CjQ0q0o3MCTDexoRJMv3A8wz9PVZVJbqj1yfXUU="
    assert dump.public_key == "BBKNFzBhlBZLbRu6o5YaxGYfYy9H3XQJ0VcKG/FBPkY="
    assert dump.listen_port == 41234
    assert dump.fwmark == "off"


def test_parse_dump_reads_every_peer_field():
    peer = parse_dump(REAL_DUMP).peers[1]

    assert peer.public_key == "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4="
    assert peer.endpoint == "198.51.100.7:51820"
    assert peer.allowed_ips == "10.13.13.3/32"
    assert peer.latest_handshake == 1754332800
    assert peer.rx == 918273
    assert peer.tx == 6553600
    assert peer.keepalive == "25"


def test_parse_dump_turns_none_placeholders_into_empty_strings():
    """The tool prints "(none)" for an unset value; carrying that through would
    put the literal string in the UI and in the traffic database."""
    peers = parse_dump(REAL_DUMP).peers

    assert peers[0].endpoint == ""  # never connected
    assert peers[1].preshared_key == ""  # no PSK configured
    assert "(none)" not in str(peers)


def test_parse_dump_of_an_interface_with_no_peers():
    """A fresh server. The interface line is all there is, and it must not be
    mistaken for a peer."""
    line = "priv\tpub\t41234\toff\n"
    dump = parse_dump(line)

    assert dump.peers == []
    assert dump.listen_port == 41234
    assert dump.transfers() == {}


@pytest.mark.parametrize("text", ["", "\n", "   \n\n", "\t\t\t\n"])
def test_parse_dump_of_nothing_at_all(text):
    dump = parse_dump(text)
    assert isinstance(dump, Dump)
    assert dump.peers == []
    assert dump.listen_port == 0


def test_parse_dump_pads_short_lines_instead_of_failing():
    """A truncated dump from a tool version we do not know is still worth the
    peers it did manage to print."""
    dump = parse_dump("priv\tpub\t41234\toff\nsomekey\t(none)\t(none)\n")

    assert len(dump.peers) == 1
    assert dump.peers[0].public_key == "somekey"
    assert dump.peers[0].rx == 0
    assert dump.peers[0].keepalive == ""


def test_parse_dump_ignores_a_peer_line_with_no_key():
    assert parse_dump("priv\tpub\t1\toff\n\t\t\t\t\t\t\t\n").peers == []


def test_parse_dump_survives_unparseable_counters():
    """Never let one odd field stop the collector: a zero is a recoverable lie,
    an exception is not."""
    dump = parse_dump("priv\tpub\tnotaport\toff\nkey\tpsk\tep\tips\t-\tx\ty\toff\n")

    assert dump.listen_port == 0
    assert dump.peers[0].rx == 0
    assert dump.peers[0].tx == 0


def test_transfers_is_the_shape_traffic_accumulate_wants():
    assert parse_dump(REAL_DUMP).transfers() == {
        "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ=": (0, 0),
        "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4=": (918273, 6553600),
    }


def test_dump_repr_does_not_leak_the_private_key():
    dump = parse_dump(REAL_DUMP)
    assert dump.private_key not in repr(dump)
    assert dump.peers[0].preshared_key not in repr(dump.peers[0])


# ------------------------------------------------------- awg not installed


def test_available_is_false_without_the_binary(no_awg):
    assert AwgController().available() is False


def test_every_read_only_method_answers_without_awg(no_awg, monkeypatch):
    """A server between install.sh and the first bring-up, or a CI runner with no
    kernel module. Reading the status must not be what breaks."""
    monkeypatch.setenv("AWG_IFACE", "awg-does-not-exist")
    tunnel = AwgController()

    assert tunnel.available() is False
    assert tunnel.tools_version() is None
    assert tunnel.module_version() is None
    assert isinstance(tunnel.module_loaded(), bool)
    assert tunnel.iface_up() is False
    assert tunnel.show_dump() is None

    state = tunnel.service_state()
    assert state["unit"] == "awg-quick@awg-does-not-exist"
    assert state["active"] == "unknown"
    assert state["enabled"] == "unknown"


def test_features_without_awg_are_permissive_and_flagged_unknown(no_awg):
    """A false negative would make validate.py reject a parameter that works.
    Assume yes, and list what the assumption covers so the UI can say so."""
    features = AwgController().features()

    assert features["tools_version"] is None
    assert features["module_version"] is None
    for name in (
        "header_ranges",
        "imitation_packets",
        "header_protection_key",
        "content_padding",
        "timers",
    ):
        assert features[name] is True, name
    assert set(features["unknown"]) == {
        "header_ranges",
        "imitation_packets",
        "header_protection_key",
        "content_padding",
        "timers",
    }


def test_features_result_is_a_copy(no_awg):
    """It is cached for a minute; a caller that edited it would poison the next
    validation pass."""
    tunnel = AwgController()
    first = tunnel.features()
    first["unknown"].append("tampered")
    first["timers"] = False

    second = tunnel.features()
    assert "tampered" not in second["unknown"]
    assert second["timers"] is True


def test_mutating_calls_do_raise_without_awg(no_awg):
    """The read paths stay quiet; a caller that asked for a change has to be told
    it did not happen."""
    tunnel = AwgController()
    with pytest.raises(ToolError):
        tunnel.up()
    with pytest.raises(ToolError):
        tunnel.syncconf("[Interface]\n")
    with pytest.raises(ToolError):
        tunnel.remove_peer("BBKNFzBhlBZLbRu6o5YaxGYfYy9H3XQJ0VcKG/FBPkY=")
    with pytest.raises(ToolError):
        tunnel.service_start()


def test_stopping_a_tunnel_that_is_already_gone_is_quiet(no_awg):
    """Unlike the rest of them, because the caller got what they asked for.

    There is no interface here and nothing that could take one down, and a stop
    is a request for a state rather than for a command to be run. Raising would
    make the panel report a failure over precisely the situation the operator
    was aiming at."""
    AwgController().service_stop()


def test_get_controller_returns_the_mock_when_asked(monkeypatch):
    controller_mod.reset_controller()
    monkeypatch.setenv("AWG_MOCK", "1")
    assert isinstance(controller_mod.get_controller(), MockController)

    controller_mod.reset_controller()
    monkeypatch.setenv("AWG_MOCK", "0")
    assert isinstance(controller_mod.get_controller(), AwgController)
    controller_mod.reset_controller()


# ----------------------------------------------------------------- mock


def test_mock_dump_agrees_with_the_config_on_disk(server_conf):
    """The mock reads the peer list back from the same file the store writes, so
    a client added in the browser has to show up in the live view."""
    tunnel = MockController()
    dump = tunnel.show_dump()
    parsed = conf_mod.parse_conf(server_conf.read_text(encoding="utf-8"))

    assert dump is not None
    assert dump.listen_port == 41234
    assert dump.private_key == parsed.interface.get("PrivateKey")
    assert dump.public_key == keys.pubkey(dump.private_key)

    live = [peer for peer in parsed.peers if peer.disabled_at is None]
    assert [peer.public_key for peer in dump.peers] == [peer.public_key for peer in live]
    for peer, source in zip(dump.peers, live, strict=True):
        assert peer.allowed_ips == source.allowed_ips
        assert peer.preshared_key == source.preshared_key


def test_mock_leaves_disabled_peers_out_of_the_dump(server_conf):
    """A disabled key is one the kernel was never given, so it cannot appear in
    a dump of what the kernel holds."""
    parsed = conf_mod.parse_conf(server_conf.read_text(encoding="utf-8"))
    disabled = [peer.public_key for peer in parsed.peers if peer.disabled_at is not None]
    assert disabled, "the fixture is supposed to contain a disabled peer"

    dump = MockController().show_dump()
    assert not set(disabled) & {peer.public_key for peer in dump.peers}


def test_mock_sees_a_client_added_through_the_store(server_conf):
    store.add_client("laptop")

    dump = MockController().show_dump()
    added = store.get_client("laptop")
    assert added.public_key in {peer.public_key for peer in dump.peers}


def test_mock_dump_is_coherent(server_conf):
    """Every peer has to look like something the tool could have printed."""
    for peer in MockController().show_dump().peers:
        assert keys.is_key(peer.public_key)
        assert peer.rx >= 0 and peer.tx >= 0
        assert peer.latest_handshake >= 0
        assert peer.latest_handshake <= time.time() + 1
        # The tool prints an endpoint only for a peer it has heard from.
        if peer.latest_handshake == 0:
            assert peer.endpoint == ""
        else:
            host, _, port = peer.endpoint.rpartition(":")
            assert host and port.isdigit()


def test_mock_traffic_advances(server_conf):
    tunnel = MockController()
    before = tunnel.show_dump()

    # The mock bills wall-clock time between polls, and a test cannot afford to
    # sit and wait for it. Which peers are "online" is derived from their public
    # key, so it varies run to run; the behaviour under test is the counters
    # moving, not the split, hence forcing them all connected.
    for state in tunnel._peers.values():
        state.online = True
    tunnel._tick -= 5.0

    after = tunnel.show_dump()

    assert {peer.public_key for peer in after.peers} == {peer.public_key for peer in before.peers}
    for old, new in zip(before.peers, after.peers, strict=True):
        assert new.rx > old.rx
        assert new.tx > old.tx
        # From the server's side tx is the client's download, which is the big
        # number on any real link.
        assert new.tx - old.tx > new.rx - old.rx


def test_mock_traffic_never_moves_backwards(server_conf):
    """traffic.py reads a drop as a counter epoch restart and adds the whole
    value again, so a mock that jittered downwards would invent gigabytes."""
    tunnel = MockController()
    previous = {peer.public_key: (peer.rx, peer.tx) for peer in tunnel.show_dump().peers}
    for _ in range(5):
        tunnel._tick -= 1.0
        for peer in tunnel.show_dump().peers:
            old_rx, old_tx = previous[peer.public_key]
            assert peer.rx >= old_rx and peer.tx >= old_tx
            previous[peer.public_key] = (peer.rx, peer.tx)


def test_mock_restart_resets_the_counters(server_conf):
    """The kernel really does start again from zero, and traffic.db's epoch
    handling - a raw counter below the last one seen - has no other way to be
    exercised without a kernel module."""
    tunnel = MockController()
    before = {peer.public_key: (peer.rx, peer.tx) for peer in tunnel.show_dump().peers}
    assert any(rx > 0 for rx, _tx in before.values())

    tunnel.restart()
    assert tunnel.iface_up() is True

    for peer in tunnel.show_dump().peers:
        old_rx, old_tx = before[peer.public_key]
        # Not exactly zero: the dump that follows bills the microseconds since
        # the interface came back. What matters is that it went backwards.
        assert peer.rx < old_rx / 100
        assert peer.tx < old_tx / 100


def test_mock_dump_is_none_while_the_interface_is_down(server_conf):
    tunnel = MockController()
    tunnel.down()

    assert tunnel.iface_up() is False
    assert tunnel.show_dump() is None
    assert tunnel.service_state()["active"] == "inactive"

    tunnel.up()
    assert tunnel.show_dump() is not None


def test_mock_syncconf_on_a_down_interface_raises(server_conf):
    """The real tool fails here, so a store that forgets to check finds out in
    mock rather than in production."""
    tunnel = MockController()
    tunnel.down()
    with pytest.raises(ToolError):
        tunnel.syncconf("[Interface]\nListenPort = 41234\n")


def test_mock_syncconf_drops_the_peers_it_was_not_given(server_conf):
    """This is the enforcement path: strip_conf leaves a disabled peer out, and
    the interface has to forget it."""
    parsed = conf_mod.parse_conf(server_conf.read_text(encoding="utf-8"))
    keep = [peer for peer in parsed.peers if peer.disabled_at is None]
    survivor = keep[0].public_key

    tunnel = MockController()
    assert survivor in {peer.public_key for peer in tunnel.show_dump().peers}

    tunnel.syncconf("[Interface]\nListenPort = 41234\n")
    assert tunnel.show_dump().peers == []

    # And re-syncing the full config brings it back, so re-enabling a client is
    # the same code path as enabling it the first time.
    tunnel.syncconf(conf_mod.strip_conf(parsed))
    assert survivor in {peer.public_key for peer in tunnel.show_dump().peers}


def test_mock_reports_a_complete_feature_set(server_conf):
    """AWG_MOCK is how the panel is demoed, so nothing may be greyed out."""
    features = MockController().features()

    assert features["unknown"] == []
    for name in (
        "module_loaded",
        "header_ranges",
        "imitation_packets",
        "header_protection_key",
        "content_padding",
        "timers",
    ):
        assert features[name] is True, name
    assert features["tools_version"] and features["module_version"]


def test_mock_works_with_no_config_at_all(conf_dir):
    """First boot of a standalone demo: there is nothing on disk yet."""
    tunnel = MockController()
    dump = tunnel.show_dump()

    assert dump is not None
    assert dump.peers == []
    assert keys.is_key(dump.public_key)


def test_the_locked_mutators_finish_inside_the_config_lock_budget():
    """These three run with the config lock held, so a wedged `awg` is a lock
    nobody else can take. Left at the general 30s timeout it would hold the
    config lock for the whole of every other waiter's budget and then some, so
    the wedge would present as every panel operation failing at once rather than
    as the one that is stuck."""
    assert controller_mod.SYNC_TIMEOUT < lock.CONFIG_LOCK_SEC

    calls: list[int] = []
    ctl = AwgController("awg0")
    with mock.patch.object(
        controller_mod, "_run", side_effect=lambda *a, **kw: calls.append(kw.get("timeout"))
    ):
        ctl.syncconf("[Interface]\n")
        ctl.remove_peer("k" * 43 + "=")
        ctl.up()

    assert calls[:2] == [controller_mod.SYNC_TIMEOUT, controller_mod.SYNC_TIMEOUT]
    # up/down run the iptables hooks and are deliberately not under the lock;
    # cutting them short would leave a half-configured interface.
    assert calls[2] is None


def test_a_huge_config_gets_proportionally_longer_to_sync():
    """The subnet is no longer capped at 253 clients, and a full /16 is a 10 MB
    syncconf. A flat budget sized for a /24 would abort it as if it had wedged,
    and the panel would stop being able to apply anything at all."""
    big = "[Interface]\n" + "x" * (10 * 1024 * 1024)
    assert controller_mod._sync_timeout("[Interface]\n") == controller_mod.SYNC_TIMEOUT
    assert controller_mod._sync_timeout(big) >= controller_mod.SYNC_TIMEOUT + 10


# ------------------------------------------------- which module is running


@pytest.fixture
def fake_sysfs(monkeypatch, tmp_path):
    """A writable stand-in for /sys/module, so both module questions are testable."""
    monkeypatch.setattr(controller_mod, "SYSFS_MODULE", tmp_path)

    def load(version: str | None) -> None:
        moddir = tmp_path / controller_mod.MODULE
        moddir.mkdir(exist_ok=True)
        if version is not None:
            (moddir / "version").write_text(version + "\n", encoding="utf-8")

    return load


def test_the_running_module_is_read_from_sysfs_not_modinfo(fake_sysfs, monkeypatch):
    """modinfo describes the .ko on disk; sysfs describes what the kernel loaded
    from it. An upgrade that could not unload the old module leaves those
    disagreeing, and every behaviour an admin is looking at is the loaded one."""
    fake_sysfs("3.0.20260805")
    monkeypatch.setattr(AwgController, "_module_version_on_disk", staticmethod(lambda: "9.9.9"))

    assert AwgController().module_version() == "3.0.20260805"


def test_the_installed_module_is_still_read_from_modinfo(fake_sysfs, monkeypatch):
    fake_sysfs("3.0.20260805")
    monkeypatch.setattr(AwgController, "_module_version_on_disk", staticmethod(lambda: "9.9.9"))

    assert AwgController().module_version_on_disk() == "9.9.9"


def test_nothing_loaded_falls_back_to_the_installed_version(fake_sysfs, monkeypatch):
    """A stopped tunnel still reports the version it would come up on, rather
    than reporting nothing at all."""
    monkeypatch.setattr(AwgController, "_module_version_on_disk", staticmethod(lambda: "3.0.1"))

    assert AwgController().module_version() == "3.0.1"


def test_a_module_declaring_no_version_falls_back_too(fake_sysfs, monkeypatch):
    """Loaded, but built without MODULE_VERSION: sysfs has the directory and no
    version file in it. "Cannot say" is not "nothing is installed"."""
    fake_sysfs(None)
    monkeypatch.setattr(AwgController, "_module_version_on_disk", staticmethod(lambda: "3.0.1"))

    assert AwgController().module_version() == "3.0.1"


def test_both_unknown_reports_none(fake_sysfs, monkeypatch):
    monkeypatch.setattr(AwgController, "_module_version_on_disk", staticmethod(lambda: None))

    assert AwgController().module_version() is None


def test_sysfs_version_is_stripped(fake_sysfs):
    fake_sysfs("  3.0.20260805  ")

    assert controller_mod._sysfs_module_version() == "3.0.20260805"


def test_module_loaded_reads_the_same_sysfs_root(fake_sysfs):
    """The two questions must not drift onto different paths."""
    assert AwgController().module_loaded() in (True, False)
    fake_sysfs("3.0.20260805")
    assert AwgController().module_loaded() is True


# ------------------------------------------------- stopping and starting


@pytest.fixture
def fake_systemd(monkeypatch, tmp_path):
    """A machine whose unit state and interface can be set independently.

    Scripts on disk rather than a patched `_run`, because what is under test is
    which program gets invoked with which arguments, and mocking the layer that
    chooses would leave nothing to prove. The fakes act on a file standing in
    for the interface - `systemctl stop` removes it only when systemd believes
    it is holding it up, `awg-quick down` always does - so that a stop which
    goes through the wrong tool leaves the same evidence here that it leaves on
    a server: a report of success over a tunnel that is still running.

    Returns a callable that sets the machine up and hands back the file every
    fake appends its own command line to, in order.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls.log"
    journal_file = tmp_path / "journal.txt"
    link = tmp_path / "iface"
    # Resolved before PATH is emptied, and spelled absolutely in the scripts:
    # they run with the same PATH the controller does, where the only thing on
    # it is this directory.
    cat = shutil.which("cat") or "/bin/cat"
    rm = shutil.which("rm") or "/bin/rm"
    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.setenv("AWG_IFACE", "awg9")
    # The one thing the controller answers without a subprocess. Backed by the
    # file the fakes create and remove, so it moves during a call the way a real
    # interface does rather than being frozen at whatever it was on entry.
    monkeypatch.setattr(AwgController, "iface_up", lambda self: link.exists())

    def configure(*, load_state="loaded", active_state="inactive", iface_up=True, rc=0, journal=""):
        journal_file.write_text(journal, encoding="utf-8")
        if iface_up:
            link.write_text("", encoding="utf-8")
        elif link.exists():
            link.unlink()
        running = active_state in ("active", "activating", "reloading", "deactivating")
        bodies = {
            "systemctl": (
                f'printf "systemctl %s\\n" "$*" >> {calls}\n'
                f'case "$1 $3" in\n'
                f'  "show LoadState") echo "{load_state}"; exit 0 ;;\n'
                f'  "show ActiveState") echo "{active_state}"; exit 0 ;;\n'
                f"esac\n"
                f'case "$1" in\n'
                f"  is-active) echo {active_state}; exit 0 ;;\n"
                f"  is-enabled) echo enabled; exit 0 ;;\n"
                f"  reset-failed) exit 0 ;;\n"
                # The heart of it: systemd runs ExecStop only for a unit it
                # believes is up, and exits 0 either way.
                f"  stop) [ {int(running)} -eq 1 ] && [ {rc} -eq 0 ] && {rm} -f {link}\n"
                f'        [ {rc} -eq 0 ] || echo "Job for $2 failed." >&2\n'
                f"        exit {rc} ;;\n"
                # And ExecStart only for one it believes is down.
                f"  start) [ {int(running)} -eq 1 ] || [ {rc} -ne 0 ] || : > {link}\n"
                f'         [ {rc} -eq 0 ] || echo "Job for $2 failed." >&2\n'
                f"         exit {rc} ;;\n"
                f"esac\n"
                f"exit 0\n"
            ),
            "journalctl": (f'printf "journalctl %s\\n" "$*" >> {calls}\n{cat} {journal_file}\n'),
            "awg-quick": (
                f'printf "awg-quick %s\\n" "$*" >> {calls}\n'
                f'case "$1" in up) : > {link} ;; down) {rm} -f {link} ;; esac\n'
                f"exit 0\n"
            ),
            "awg": f'printf "awg %s\\n" "$*" >> {calls}\nexit 0\n',
        }
        for name, body in bodies.items():
            script = binaries / name
            script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            script.chmod(0o755)
        return calls

    configure.link = link
    return configure


def _calls(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _acted(log):
    """The calls that change something, without the questions asked to decide."""
    return [line for line in _calls(log) if " show " not in line and "is-" not in line]


def test_a_stop_systemd_would_sleep_through_still_takes_the_tunnel_down(fake_systemd):
    """The state every install of this project is in, and the bug it caused.

    install.sh enables the unit and then brings the interface up with
    `awg-quick`, and every restart since goes the same way, so the unit is
    `enabled` and `inactive` over a tunnel that is running. `systemctl stop`
    against that exits zero having run nothing - which the panel reported as a
    stop, to an operator whose clients were all still connected.
    """
    log = fake_systemd(active_state="inactive", iface_up=True)

    AwgController().service_stop()

    assert _acted(log) == ["awg-quick down awg9"]
    assert not fake_systemd.link.exists()


def test_a_stop_goes_through_the_unit_when_systemd_is_holding_it_up(fake_systemd):
    """Then systemd's own ExecStop is the path: it runs the same hooks under the
    unit's timeout, and leaves systemd's view of it true afterwards."""
    log = fake_systemd(active_state="active", iface_up=True)

    AwgController().service_stop()

    assert _acted(log) == ["systemctl stop awg-quick@awg9"]
    assert not fake_systemd.link.exists()


def test_stopping_twice_is_not_an_error(fake_systemd):
    """Idempotent, because what was asked for is a state and it is already the
    one in force. Nothing is run at all: `awg-quick down` on a missing interface
    fails, and would turn a second press into a failure report."""
    log = fake_systemd(active_state="inactive", iface_up=False)

    AwgController().service_stop()

    assert _acted(log) == []


def test_a_start_goes_through_the_unit_so_that_a_later_stop_can(fake_systemd):
    """`awg-quick up` would bring the same interface up and leave systemd
    believing it is inactive - which is the state that made the stop above do
    nothing."""
    log = fake_systemd(active_state="inactive", iface_up=False)

    AwgController().service_start()

    assert _acted(log) == ["systemctl start awg-quick@awg9"]
    assert fake_systemd.link.exists()


def test_a_start_finishes_the_job_when_systemd_thinks_it_is_already_up(fake_systemd):
    """RemainAfterExit keeps the unit `active` after an `awg-quick down` behind
    its back, and `systemctl start` on an active unit exits zero without running
    ExecStart. The interface is what was asked for, so awg-quick supplies it."""
    log = fake_systemd(active_state="active", iface_up=False)

    AwgController().service_start()

    assert _acted(log) == ["systemctl start awg-quick@awg9", "awg-quick up awg9"]
    assert fake_systemd.link.exists()


def test_starting_a_tunnel_that_is_already_up_runs_nothing(fake_systemd):
    """`systemctl start` would hand `awg-quick up` a name the kernel already has
    and fail over a tunnel that is working."""
    log = fake_systemd(active_state="active", iface_up=True)

    AwgController().service_start()

    assert _calls(log) == []


def test_a_failed_unit_is_cleared_before_it_is_started(fake_systemd):
    """Where ExecStop ran against an interface somebody had already taken away.
    Nothing else clears that, and the start it blocks is the one an operator
    reaches for precisely because the tunnel is down."""
    log = fake_systemd(active_state="failed", iface_up=False)

    AwgController().service_start()

    assert "systemctl reset-failed awg-quick@awg9" in _acted(log)


def test_without_a_unit_it_falls_back_to_awg_quick(fake_systemd):
    """An AmneziaWG somebody set up by hand. There is nothing for systemd to
    believe, so nothing its view can contradict - and the tunnel still has to
    stop."""
    log = fake_systemd(load_state="not-found", active_state="inactive", iface_up=True)
    tunnel = AwgController()

    tunnel.service_stop()
    tunnel.service_start()

    assert [line for line in _calls(log) if line.startswith("awg-quick")] == [
        "awg-quick down awg9",
        "awg-quick up awg9",
    ]


def test_a_failed_start_quotes_what_the_unit_logged(fake_systemd):
    """systemctl says only that the job failed and tells the reader to go and
    look in the journal. An operator in a browser cannot follow that."""
    fake_systemd(
        active_state="inactive",
        iface_up=False,
        rc=1,
        journal="[#] ip link add awg9 type amneziawg\nLine unrecognized: `Jc=4'\n",
    )

    with pytest.raises(ToolError) as caught:
        AwgController().service_start()

    assert "Line unrecognized" in caught.value.stderr


def test_a_failure_the_journal_cannot_explain_keeps_systemctl_s_own_words(fake_systemd):
    """No journald, or a unit that logged nothing. Saying nothing at all would be
    worse than repeating the instruction to go and look."""
    fake_systemd(active_state="active", iface_up=True, rc=1, journal="")

    with pytest.raises(ToolError) as caught:
        AwgController().service_stop()

    assert "Job for" in caught.value.stderr


def test_the_journal_is_matched_on_the_unit_rather_than_asked_for_by_name(fake_systemd):
    """`-u` also matches what PID 1 logs *about* the unit, and those messages
    always come last: "Failed to start ..." would be the line quoted back, which
    is the one thing the reader already knows."""
    log = fake_systemd(active_state="inactive", iface_up=False, rc=1, journal="something broke")

    with pytest.raises(ToolError):
        AwgController().service_start()

    asked = [line for line in _calls(log) if line.startswith("journalctl")]
    assert asked and "_SYSTEMD_UNIT=awg-quick@awg9.service" in asked[0]
    assert " -u " not in asked[0]
