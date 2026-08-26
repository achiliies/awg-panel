"""The server endpoints: what they must never say, and what a save costs.

Two things are being defended here. The first is the server private key, which
is in awg0.conf, is read on every one of these requests, and must not appear in
any response - so the test looks for the key itself in the raw body rather than
for a field name somebody has to remember to exclude.

The second is contract 11a. An obfuscation change is not hot: it takes an
interface restart and then a re-render of every client config, because a
half-applied junk parameter fails the handshake silently and there is nothing in
any log to find. So a save that touches one has to report needsRestart and
mustReimport, has to perform that restart rather than leave the tunnel running
settings it no longer has on disk, and the client configs have to already carry
the new values by the time the response is written - the admin is being told to
re-import files that must therefore be correct. A DNS change is the other half
of the same rule: nothing on the server changes at all, so nothing needs a
restart, but every client file does change.
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.events import kinds
from apps.events.models import Event
from awg import firewall, keys, ports, store, validate
from awg.clientsenv import read_env
from awg.conf import parse_client_conf, parse_conf
from awg.controller import get_controller, reset_controller

pytestmark = pytest.mark.django_db


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


def interface_value(server_conf: Path, key: str) -> str:
    """One [Interface] key, read off disk rather than out of the response."""
    return parse_conf(server_conf.read_text(encoding="utf-8")).interface.get(key) or ""


def client_conf(conf_dir: Path, name: str) -> dict[str, str]:
    """clients/<name>.conf as a flat map, the way the Amnezia app reads it."""
    return parse_client_conf((conf_dir / "clients" / f"{name}.conf").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_controller():
    """One mock interface per test.

    get_controller() caches its instance for the life of the process, and the
    mock remembers what it has been told; a syncconf from one test would
    otherwise still be in effect in the next.
    """
    reset_controller()
    yield
    reset_controller()


@pytest.fixture
def api(server_conf) -> APIClient:
    """A signed-in client against the fixture server config.

    force_login rather than the login form: the sign-in handshake belongs to
    test_api_auth.py, and an Argon2 hash per test buys nothing here.
    """
    client = APIClient()
    client.force_login(get_user_model().objects.create_user("admin"))
    return client


# ---------------------------------------------------------------------- read


def test_get_server_never_carries_the_private_key(api, server_conf):
    private = interface_value(server_conf, "PrivateKey")
    assert keys.is_key(private)

    response = api.get(api_url("server"))

    assert response.status_code == 200
    assert private not in response.content.decode("utf-8")
    body = response.json()
    assert not [name for name in body if "private" in name.lower()]
    assert "PrivateKey" not in body["params"]
    # The public key is derived for display, so the UI never needs the other one.
    assert body["publicKey"] == keys.pubkey(private)
    assert body["listenPort"] == 41234
    assert body["subnetCidr"] == "10.13.13.0/24"
    assert body["subnetCapacity"] == 253
    assert body["params"]["Jc"] == "4"
    # Three firewall rules, plus the tunnel's own two hooks - re-revoking the
    # disabled peers and re-applying the bandwidth ceilings - which the config
    # carries and the settings page therefore shows.
    assert len(body["postUp"]) == 5


def test_params_catalog_explains_every_parameter(api):
    """The Server Config page is built from this, help text included."""
    response = api.get(api_url("server/params"))

    assert response.status_code == 200
    rows = response.json()
    assert {row["key"] for row in rows} == set(validate.PARAMS)

    for row in rows:
        where = row["key"]
        assert row["group"], where
        assert row["kind"], where
        assert row["label"].strip(), where
        # help_short sits under the field, help_long is the popover. Neither may
        # be a placeholder, and neither may be a copy of the other.
        assert row["helpShort"].strip(), where
        assert row["helpLong"].strip(), where
        assert row["helpLong"] != row["helpShort"], where
        assert row["helpLong"] != row["label"], where
        assert isinstance(row["mustMatchClient"], bool), where
        assert isinstance(row["importerSafe"], bool), where
        # Every parameter is supported by the mock, which reports a current
        # module; the UI greys out the ones a real old module cannot do.
        assert row["supported"] is True, where


# --------------------------------------------------------------------- write


def test_a_bad_parameter_is_rejected_with_the_field_named(api, server_conf, server_conf_text):
    response = api.put(api_url("server"), {"params": {"S1": "eighty-six"}}, format="json")

    assert response.status_code == 400
    body = response.json()
    assert "S1" in body["errors"]
    assert body["detail"]
    # Nothing was written, so a rejected save cannot leave the tunnel in a state
    # halfway between two obfuscation sets.
    assert server_conf.read_text(encoding="utf-8") == server_conf_text


@pytest.mark.parametrize("key", ["RandomTrailers", "DisableCookies"])
def test_a_switch_written_as_a_number_does_not_jam_every_other_save(api, server_conf, key):
    """A save validates the whole merged config, not the fields in the payload,
    so one value the panel calls malformed stops every unrelated save with an
    error on a field the admin never touched - and the only way out is an editor
    on the server, which is where the value came from.

    The tools take a number for a switch: parse_bool reads on and off as words
    and then anything that parses as one, zero being off. So `= 0` is a config
    they accept, the interface is up on it, and the panel refusing it was the
    panel's own rule. Both switches, because the rule is on the kind and a third
    one would inherit it.
    """
    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(text.replace("Jc = 4", f"Jc = 4\n{key} = 0"), encoding="utf-8")

    response = api.put(api_url("server"), {"params": {"Jmin": "50"}}, format="json")

    assert response.status_code == 200, response.content
    assert interface_value(server_conf, "Jmin") == "50"
    # Read as off, so it says nothing about a protection that is not switched off.
    assert not [text for text in response.json()["warnings"] if key in text]


def test_a_server_only_change_restarts_but_asks_nobody_to_reimport(api, server_conf, conf_dir):
    """DisableCookies is an [Interface] value, so the tunnel has to come back for
    it - but it appears in no client config, so telling the fleet to re-import
    would be a lie about a change no peer can read.

    That split is why it is not folded in with the obfuscation keys: those two
    answers used to be the same intersection, and a setting that restarts
    without costing a re-import had no way to say so.
    """
    store.add_client("phone")
    before = client_conf(conf_dir, "phone")

    response = api.put(api_url("server"), {"params": {"DisableCookies": "on"}}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is True
    assert body["mustReimport"] is False
    assert body["applied"] is True

    assert interface_value(server_conf, "DisableCookies") == "on"
    # The switch costs the flood protection, and the save is the only place an
    # admin is in a position to hear that.
    assert any("cookie challenge" in text for text in body["warnings"]), body["warnings"]
    # Nothing the client holds moved, which is what mustReimport just promised.
    assert client_conf(conf_dir, "phone") == before


def test_clearing_a_server_only_switch_removes_the_line(api, server_conf):
    """Empty takes the line out rather than writing it blank, the same as every
    other [Interface] parameter: awg-quick reads a blank value as malformed."""
    api.put(api_url("server"), {"params": {"DisableCookies": "on"}}, format="json")
    assert interface_value(server_conf, "DisableCookies") == "on"

    response = api.put(api_url("server"), {"params": {"DisableCookies": ""}}, format="json")

    assert response.status_code == 200, response.content
    # The line itself, not the parsed value: a blank `DisableCookies =` reads
    # back as "" through the helper exactly like an absent one, and blank is
    # the thing awg-quick refuses.
    assert "DisableCookies" not in server_conf.read_text(encoding="utf-8")


def test_an_obfuscation_change_needs_a_restart_and_a_reimport(api, server_conf, conf_dir):
    store.add_client("phone")
    before = client_conf(conf_dir, "phone")

    response = api.put(api_url("server"), {"params": {"Jc": "9"}}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is True
    assert body["mustReimport"] is True
    # Never applied hot - but the save does the down/up itself, inside the
    # request. A setting that is on disk and not in force is the state this
    # endpoint must never leave behind.
    assert body["applied"] is True

    assert interface_value(server_conf, "Jc") == "9"
    # A timestamped copy beside the config, in the name install.sh uses.
    assert list(conf_dir.glob("awg0.conf.bak-*"))

    # The admin is being told to re-import, so the files they will export have
    # to be right already.
    after = client_conf(conf_dir, "phone")
    assert after["Jc"] == "9"
    assert after["PrivateKey"] == before["PrivateKey"]
    assert after["Address"] == before["Address"]


def test_an_obfuscation_change_saves_on_a_dual_stack_server(api, server_conf):
    """The address line is checked on every save, whatever the save is about.

    A server the installer gave IPv6 to has two families on one Address line,
    and reading only the first spelling of that field - a lone IPv4 address -
    turned every save into a 400 about an address the request had not mentioned.
    """
    text = server_conf.read_text(encoding="utf-8")
    address = next(line for line in text.splitlines() if line.startswith("Address"))
    server_conf.write_text(
        text.replace(address, f"{address}, fd7a:1e5f:22::1/64"), encoding="utf-8"
    )

    response = api.put(api_url("server"), {"params": {"Jc": "9"}}, format="json")

    assert response.status_code == 200, response.content
    assert interface_value(server_conf, "Jc") == "9"
    assert interface_value(server_conf, "Address") == "10.13.13.1/24, fd7a:1e5f:22::1/64"


def test_changing_only_the_dns_is_a_client_side_change(api, server_conf, conf_dir):
    store.add_client("phone")
    before = server_conf.read_text(encoding="utf-8")

    response = api.put(api_url("server"), {"dns": "9.9.9.9"}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is False
    assert body["mustReimport"] is True
    assert body["applied"] is True

    # DNS is a client-side setting: the server config is not touched at all.
    assert server_conf.read_text(encoding="utf-8") == before
    assert read_env()["CLIENT_DNS"] == "9.9.9.9"
    assert client_conf(conf_dir, "phone")["DNS"] == "9.9.9.9"
    assert api.get(api_url("server")).json()["dns"] == "9.9.9.9"


def test_a_restart_that_cannot_happen_is_reported_rather_than_hidden(api, server_conf):
    """A down interface is not a failed save: the config on disk is still right."""
    get_controller().down()

    response = api.put(api_url("server"), {"params": {"Jc": "9"}}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["needsRestart"] is True
    assert body["applied"] is False
    # The UI shows the restart button off the back of this sentence, so it has
    # to say what happened rather than only that something did.
    assert any("down" in text for text in body["warnings"]), body["warnings"]
    assert interface_value(server_conf, "Jc") == "9"


def test_moving_the_listen_port_moves_the_firewall_rule(api, server_conf, monkeypatch):
    """The installer opens the first port and this has to move it; a port change
    that leaves ufw on the old one is a save that reports success and takes the
    server off the internet."""
    moved: list[tuple] = []
    monkeypatch.setattr(
        firewall,
        "move_port",
        lambda new, old=0, proto="udp": moved.append((new, old, proto)) or "ufw",
    )

    response = api.put(api_url("server"), {"listenPort": "443"}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert moved == [(443, 41234, "udp")]
    assert interface_value(server_conf, "ListenPort") == "443"
    # Whatever the panel cannot reach is the admin's next job, so it is said out
    # loud rather than left to be discovered when nobody can connect.
    note = [text for text in body["warnings"] if "ufw" in text]
    assert note and "security group" in note[0], body["warnings"]
    # Moving the port invalidates every issued config, because a client's
    # Endpoint carries it.
    assert body["mustReimport"] is True
    assert body["applied"] is True


def test_a_port_that_did_not_move_leaves_the_firewall_alone(api, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        firewall, "move_port", lambda new, old=0, proto="udp": calls.append((new, old)) or "ufw"
    )

    response = api.put(api_url("server"), {"listenPort": "41234"}, format="json")

    assert response.status_code == 200, response.content
    assert calls == []


def test_a_listen_port_something_else_holds_is_named(api, server_conf, monkeypatch):
    """awg-quick fails with a sentence about an address, and the journal it
    fails into does not say which process was in the way. Refusing the save
    names the holding process and leaves the config on disk untouched."""
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": True)
    monkeypatch.setattr(ports, "holder", lambda port, proto="udp": "systemd-resolve")

    response = api.put(api_url("server"), {"listenPort": "53"}, format="json")

    assert response.status_code == 400, response.content
    body = response.json()
    assert "listenPort" in body["errors"]
    error = body["errors"]["listenPort"]
    assert "systemd-resolve" in error and "53" in error, error
    assert interface_value(server_conf, "ListenPort") == "41234"


def test_a_conflict_without_a_named_process_is_refused(api, server_conf, monkeypatch):
    """An occupied port is refused even when the holding process name cannot be read."""
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": True)
    monkeypatch.setattr(ports, "holder", lambda port, proto="udp": "")

    response = api.put(api_url("server"), {"listenPort": "443"}, format="json")

    assert response.status_code == 400, response.content
    error = response.json()["errors"]["listenPort"]
    assert "Another service" in error or "another service" in error, error
    assert interface_value(server_conf, "ListenPort") == "41234"


def test_a_free_listen_port_says_nothing_about_conflicts(api, monkeypatch):
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": False)

    response = api.put(api_url("server"), {"listenPort": "443"}, format="json")

    assert response.status_code == 200, response.content
    assert not [text for text in response.json()["warnings"] if "UDP port" in text]


def test_an_unchanged_listen_port_is_accepted_when_busy(api, server_conf, monkeypatch):
    """The tunnel holds its own port. Moving other settings while keeping the port
    must succeed even when the port is reported as busy."""
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": True)

    response = api.put(
        api_url("server"), {"listenPort": "41234", "params": {"Jc": "9"}}, format="json"
    )

    assert response.status_code == 200, response.content
    assert interface_value(server_conf, "ListenPort") == "41234"
    assert interface_value(server_conf, "Jc") == "9"


def test_a_junk_listen_port_gets_store_validation_error(api, server_conf, monkeypatch):
    """A value that is not a valid port must not be masked by the port check."""
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": True)

    response = api.put(api_url("server"), {"listenPort": "banana"}, format="json")

    assert response.status_code == 400, response.content
    body = response.json()
    assert "listenPort" in body["errors"]
    assert "whole number" in body["errors"]["listenPort"]
    assert interface_value(server_conf, "ListenPort") == "41234"


def test_a_port_that_did_not_move_is_never_probed(api, monkeypatch):
    """The tunnel holds its own port. Asking would report that the server is
    running, in the words of something being in the way."""
    asked: list[int] = []
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": asked.append(port) or True)

    response = api.put(api_url("server"), {"listenPort": "41234"}, format="json")

    assert response.status_code == 200, response.content
    assert asked == []


# --------------------------------------------------------------- reconfigure


def test_reconfigure_previews_a_valid_set_without_saving(api, server_conf, server_conf_text):
    before = api.get(api_url("server")).json()["params"]["Jc"]
    response = api.post(api_url("server/reconfigure"), {}, format="json")

    assert response.status_code == 200, response.content
    params = response.json()["params"]
    assert params["Jc"] and params["H1"] and params["I1"]
    # The generated set has to pass the same rules a hand-typed one does, or the
    # Reconfigure button hands the admin a config that will not save.
    assert validate.validate_params(params, features=get_controller().features()) == {}

    # A preview, not a save: nothing is written until the admin says so.
    assert server_conf.read_text(encoding="utf-8") == server_conf_text
    assert api.get(api_url("server")).json()["params"]["Jc"] == before


def test_reconfigure_leaves_the_unused_imitation_slots_blank(api, server_conf):
    """The decoy session is three to five packets, so a slot can legitimately
    come back empty - and it has to survive serialisation as an empty string,
    because that is how the save removes the line."""
    for _ in range(12):
        params = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"]
        filled = [bool(params[f"I{n}"]) for n in range(1, 6)]
        assert 3 <= sum(filled) <= 5
        assert filled == sorted(filled, reverse=True), filled


def test_reconfigure_fits_s4_to_the_configured_mtu(api, server_conf):
    """S4 is added to every data packet, so it is drawn against the room the
    interface's own MTU leaves rather than against an assumed one."""
    api.put(api_url("server"), {"mtu": 1420}, format="json")
    for _ in range(12):
        params = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"]
        assert int(params["S4"]) <= 1440 - 1420


@pytest.mark.parametrize("profile", ["standard", "dpi", "fast", "random"])
def test_reconfigure_accepts_every_profile(api, server_conf, profile):
    response = api.post(api_url("server/reconfigure"), {"profile": profile}, format="json")

    assert response.status_code == 200, response.content
    body = response.json()
    assert validate.validate_params(body["params"], features=get_controller().features()) == {}
    assert body["warnings"] == []


def test_reconfigure_rejects_a_profile_it_does_not_have(api, server_conf):
    """A typo has to come back as an error rather than as a silent standard
    draw, or a caller asking for the thorough profile gets the cheap one and is
    told it succeeded."""
    response = api.post(api_url("server/reconfigure"), {"profile": "paranoid"}, format="json")
    assert response.status_code == 400, response.content


def test_reconfigure_draws_s4_against_the_saved_mtu_not_a_requested_one(api, server_conf):
    """The MTU is not the caller's to state. Whatever it asks for, the save is
    measured against the config, so drawing against anything else would produce
    a set the budget check then refuses."""
    api.put(api_url("server"), {"mtu": 1420}, format="json")
    for _ in range(12):
        body = {"profile": "dpi", "mtu": 1280}
        params = api.post(api_url("server/reconfigure"), body, format="json").json()["params"]
        assert int(params["S4"]) <= 1440 - 1420


def test_reconfigure_draws_the_whole_page_in_one_go(api, server_conf):
    """One button, both halves. They were two scopes while the advanced group
    was something an operator opted into; a draw that filled only one of them
    now leaves the page describing two different servers."""
    api.put(api_url("server"), {"mtu": 1300}, format="json")
    response = api.post(api_url("server/reconfigure"), {"profile": "standard"}, format="json")

    assert response.status_code == 200, response.content
    params = response.json()["params"]
    assert {"Jc", "S1", "H1", "I1"} <= set(params)
    assert set(validate.ADVANCED_PARAMS) <= set(params)
    assert params["HeaderProtectionKey"] and params["RekeyAfterTime"]
    assert (
        validate.validate_params({**params, "MTU": "1300"}, features=get_controller().features())
        == {}
    )


def test_reconfigure_leaves_random_trailers_off(api, server_conf):
    """The one member of the group drawn as empty. Everything else arrived in
    AmneziaWG 3.0, which is what a current client speaks; trailers arrived in
    3.1, and a peer without them drops an arriving handshake for being longer
    than it expects - with no error at either end."""
    for _ in range(6):
        params = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"]
        assert params["RandomTrailers"] == ""


def test_reconfigure_says_nothing_about_the_set_it_just_drew(api, server_conf):
    """A draw that arrives carrying advice about itself is a draw nobody trusts.
    The AmneziaWG 3.0 advisory used to fire on every one of these, because the
    generator sets exactly the parameters it named."""
    payload = api.post(api_url("server/reconfigure"), {}, format="json").json()
    assert payload["warnings"] == [], payload["warnings"]


def test_reconfigure_redraws_padding_the_key_can_be_carried_by(api, server_conf):
    """The key puts a floor under S1-S4, and an install from before that floor
    existed can be under it. It used to be a sentence telling the operator to go
    and redraw the other card; the other card is drawn by the same button now,
    so the short padding is simply replaced along with everything else."""
    api.put(api_url("server"), {"params": {"S2": "8"}}, format="json")
    payload = api.post(api_url("server/reconfigure"), {}, format="json").json()

    assert payload["params"]["HeaderProtectionKey"]
    assert int(payload["params"]["S2"]) >= validate.HEADER_NONCE
    assert payload["warnings"] == [], payload["warnings"]


@pytest.mark.parametrize("profile", ["standard", "dpi", "fast", "random"])
def test_reconfigure_draws_content_padding_as_a_range(api, server_conf, profile):
    """A single number is worse than an empty field: the kernel uses this
    instead of the 16-byte rounding it does anyway, so a constant addition
    replaces a length known to within 16 bytes with one known exactly."""
    body = {"profile": profile}
    for _ in range(12):
        value = api.post(api_url("server/reconfigure"), body, format="json").json()["params"][
            "ContentPaddingAddition"
        ]
        assert value == "" or "-" in value, value


@pytest.mark.parametrize("profile", ["standard", "dpi", "fast", "random"])
def test_anything_the_generator_draws_can_actually_be_saved(api, server_conf, profile):
    """The property that matters more than any single bound.

    The header protection key is refused unless the padding drawn beside it is
    long enough to carry its nonce, and the two come from different preset
    tables under one profile name. Every profile has to save, or the operator is
    left with a button that fills the form in and a Save that will not take it -
    or worse, one that takes it and an interface that does not come back.
    """
    drawn = api.post(api_url("server/reconfigure"), {"profile": profile}, format="json")
    assert drawn.status_code == 200, drawn.content
    saved = api.put(api_url("server"), {"params": drawn.json()["params"]}, format="json")
    assert saved.status_code == 200, (profile, saved.content)


def test_a_generated_key_cannot_be_saved_onto_padding_too_short_for_it(api, server_conf):
    """The failure this whole floor exists for. The kernel refuses the device
    configuration rather than the parameter, and an obfuscation save restarts
    the interface - so without this the panel takes the tunnel down to apply a
    combination that will not bring it back.

    The generator cannot produce this pairing any more - it draws the padding
    and the key together - so the key is saved on its own, which is what an API
    client sending half a preview does."""
    assert api.put(api_url("server"), {"params": {"S4": "8"}}, format="json").status_code == 200
    key = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"][
        "HeaderProtectionKey"
    ]

    refused = api.put(api_url("server"), {"params": {"HeaderProtectionKey": key}}, format="json")

    assert refused.status_code == 400, refused.content
    assert "S4" in refused.json()["errors"]


def test_saving_a_generated_advanced_set_writes_and_clearing_it_removes(api, server_conf):
    """The advanced group end to end. Clearing is no longer a button, but it is
    still what an empty field means, and it has to leave absent lines rather
    than blank ones - the same way an unused decoy slot does."""
    params = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"]
    assert api.put(api_url("server"), {"params": params}, format="json").status_code == 200

    written = server_conf.read_text(encoding="utf-8")
    assert f"HeaderProtectionKey = {params['HeaderProtectionKey']}\n" in written

    cleared = dict.fromkeys(validate.ADVANCED_PARAMS, "")
    assert api.put(api_url("server"), {"params": cleared}, format="json").status_code == 200

    written = server_conf.read_text(encoding="utf-8")
    for key in validate.ADVANCED_PARAMS:
        assert f"\n{key} " not in written, f"{key} was left in the config as a blank"


def test_saving_a_reconfigure_removes_the_slots_it_left_empty(api, server_conf):
    """The whole round trip the button performs, on a config that starts with all
    five slots filled.

    An empty slot has to end up as an absent line rather than a blank value:
    awg-quick reads `I5 = ` as a malformed imitation packet and refuses to bring
    the interface up, so a save that wrote the empty string would take the tunnel
    down at the next restart and say nothing about why.
    """
    assert sum(f"\nI{n} = " in server_conf.read_text(encoding="utf-8") for n in range(1, 6)) == 5

    params = api.post(api_url("server/reconfigure"), {}, format="json").json()["params"]
    assert api.put(api_url("server"), {"params": params}, format="json").status_code == 200

    written = server_conf.read_text(encoding="utf-8")
    for slot in range(1, 6):
        key = f"I{slot}"
        if params[key]:
            assert f"{key} = {params[key]}\n" in written, f"{key} was not written"
        else:
            assert f"\n{key} " not in written, f"{key} was left in the config as a blank"

    # And the config the panel now reads back is the one that was generated.
    saved = api.get(api_url("server")).json()["params"]
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"):
        assert saved[key] == params[key], key


def _status_warnings(api) -> list[str]:
    response = api.get(api_url("server/status"))
    assert response.status_code == 200, response.content
    return response.json()["warnings"]


def test_a_module_installed_but_not_yet_loaded_is_said_out_loud(api, monkeypatch):
    """install.sh cannot unload a module the interface is holding open, so it
    installs the new one and says the swap happens at the next reboot. Until
    then modinfo reports the fix and the kernel is still running the bug - and
    this page is where someone looks to find out why nothing changed."""
    controller = get_controller()
    features = dict(controller.features())
    features["module_version"] = "3.0.20260805"
    features["module_version_on_disk"] = "3.1.20260812"
    monkeypatch.setattr(controller, "features", lambda: dict(features))

    notes = [text for text in _status_warnings(api) if "amneziawg" in text]

    assert notes, _status_warnings(api)
    assert "3.0.20260805" in notes[0] and "3.1.20260812" in notes[0]
    assert "reboot" in notes[0]


def test_a_disabled_cookie_reply_keeps_saying_so_on_the_status_page(api):
    """The save is where an admin first hears what the switch costs, but it is
    not the last word: a server not answering a flood is a live condition, so
    the sentence stands on server/status for as long as the switch is on.
    read_server carries the server-only params for exactly this, and nothing
    else would notice if it stopped.

    Both endpoints saying it is deliberate and is checked here, because for one
    moment they say it at once - the PUT answers, and the status query behind
    the notice at the top of the page catches up - and the Server page had been
    rendering both, one paragraph about cookie replies printed out twice. The
    page drops the line from the save's block once the standing one carries it;
    what neither end may do is stop saying it.
    """
    assert not [text for text in _status_warnings(api) if "DisableCookies" in text]

    saved = api.put(api_url("server"), {"params": {"DisableCookies": "on"}}, format="json")
    assert saved.status_code == 200, saved.content
    assert [text for text in saved.json()["warnings"] if "DisableCookies" in text]

    standing = [text for text in _status_warnings(api) if "DisableCookies" in text]
    assert standing, _status_warnings(api)
    assert "cookie challenge" in standing[0]
    # Word for word the same sentence, which is what lets the page recognise it
    # as one thing said twice rather than two things that happen to overlap.
    assert standing == [text for text in saved.json()["warnings"] if "DisableCookies" in text]

    api.put(api_url("server"), {"params": {"DisableCookies": ""}}, format="json")
    assert not [text for text in _status_warnings(api) if "DisableCookies" in text]


def test_matching_module_versions_say_nothing(api, monkeypatch):
    """The normal case is every install, so it must not carry a warning."""
    controller = get_controller()
    features = dict(controller.features())
    features["module_version"] = "3.1.20260812"
    features["module_version_on_disk"] = "3.1.20260812"
    monkeypatch.setattr(controller, "features", lambda: dict(features))

    assert not [text for text in _status_warnings(api) if "reboot" in text]


def test_an_unknown_version_on_either_side_says_nothing(api, monkeypatch):
    """modinfo missing, or a module with no MODULE_VERSION. Two unknowns are not
    a mismatch, and guessing one would warn on every server that has neither."""
    controller = get_controller()
    for running, installed in (("3.1.20260812", None), (None, "3.1.20260812"), (None, None)):
        features = dict(controller.features())
        features["module_version"] = running
        features["module_version_on_disk"] = installed
        monkeypatch.setattr(controller, "features", lambda f=features: dict(f))

        assert not [text for text in _status_warnings(api) if "reboot" in text], (
            running,
            installed,
        )


# --------------------------------------------------- stopping and starting
#
# The point of separating these from the restart above is that a restart is an
# operation on a tunnel that is meant to be running - it ends with every client
# back on and nothing changed - while a stop ends with a server that serves
# nobody until somebody says otherwise. Everything below is a consequence of
# that difference.


def test_stopping_takes_the_interface_down_and_leaves_it_down(api):
    tunnel = get_controller()
    assert tunnel.iface_up() is True

    response = api.post(api_url("server/stop"))

    assert response.status_code == 204, response.content
    assert tunnel.iface_up() is False


def test_starting_brings_it_back(api):
    tunnel = get_controller()
    tunnel.down()

    response = api.post(api_url("server/start"))

    assert response.status_code == 204, response.content
    assert tunnel.iface_up() is True


@pytest.mark.parametrize("action", ["stop", "start"])
def test_both_are_idempotent(api, action):
    """systemd's own stop and start are, and an admin who presses the button
    twice because the first press was slow must not be told they did something
    wrong: the tunnel is in the state they asked for either way."""
    assert api.post(api_url(f"server/{action}")).status_code == 204
    assert api.post(api_url(f"server/{action}")).status_code == 204


def test_a_start_does_not_re_admit_a_client_that_was_switched_off(api, server_conf):
    """`awg-quick up` loads the whole config, and a disabled peer is still in it
    on purpose - that is what reserves its address. So a bring-up hands the
    tunnel back to every client quota, expiry or an admin took away, and the
    start has to undo that before anybody can reach the internet through one."""
    created = api.post(api_url("clients"), {"name": "cutoff"}, format="json")
    assert created.status_code == 201, created.content
    public_key = created.json()["publicKey"]
    assert api.put(api_url("clients/cutoff"), {"enabled": False}, format="json").status_code == 200

    api.post(api_url("server/stop"))
    api.post(api_url("server/start"))

    dump = get_controller().show_dump()
    assert dump is not None
    assert public_key not in {peer.public_key for peer in dump.peers}


def test_stopping_and_starting_are_each_their_own_event(api):
    """Four server kinds rather than two. "The interface is down" and "somebody
    took the interface down" are the same observation and different answers, and
    the question asked in front of the log afterwards is always which."""
    api.post(api_url("server/stop"))
    api.post(api_url("server/start"))

    recorded = [row.kind for row in Event.objects.order_by("id")]

    assert kinds.SERVER_STOPPED in recorded
    assert kinds.SERVER_STARTED in recorded
    assert kinds.SERVER_RESTARTED not in recorded


def test_a_stop_is_loud_in_the_log_and_a_start_is_not(api):
    """The line between the two severities is not how bad it is but whether
    somebody scanning for the cause of a problem wants it. A tunnel that stopped
    serving is that; one that came back is the end of the story."""
    api.post(api_url("server/stop"))
    api.post(api_url("server/start"))

    assert Event.objects.get(kind=kinds.SERVER_STOPPED).severity == kinds.WARNING
    assert Event.objects.get(kind=kinds.SERVER_STARTED).severity == kinds.INFO
