"""The client endpoints, checked against the files they are supposed to write.

The panel is a second front-end over /etc/amnezia/amneziawg, not a database with
a config exporter, so these tests assert on the config files rather than on the
JSON the API echoed back. A handler that returned a perfect response and wrote a
peer block `awg-quick` cannot parse would pass any test that only read the
response body, and the failure would surface days later as a tunnel that will
not come up at boot.

The other half is what must never leave the server. A private key or a preshared
key appears in exactly two places - the config download and the QR image - and
nowhere else, so the list response is checked against the real key material on
disk rather than against a list of field names somebody has to remember to
update.
"""

import io
import json
import re
import zipfile
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.clients import merge, shaping
from apps.clients.models import ClientMeta
from apps.clients.views import _qr_png
from apps.panel import settings_store
from apps.stats.models import ClientDaily, DailyTotal, utc_day
from awg import clientsenv, keys, lock, names, paths, traffic
from awg.conf import parse_client_conf, parse_conf, strip_conf
from awg.controller import reset_controller
from awg.errors import LockTimeout
from awg.paths import live_state_file
from awg.traffic import Counters

pytestmark = pytest.mark.django_db

# The peer block a client add appends, as a pattern: a blank line, the header,
# the two metadata comments in this order, then the three keys in this order.
# Same expression as tests/test_store.py, because the format is the contract
# between the two front-ends and both layers have to keep it.
PEER_BLOCK_RE = re.compile(
    r"\n\[Peer\]\n"
    r"# Client = (?P<name>[^\n]+)\n"
    r"# Created = (?P<created>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\n"
    r"PublicKey = (?P<public>[A-Za-z0-9+/]{43}=)\n"
    r"PresharedKey = (?P<psk>[A-Za-z0-9+/]{43}=)\n"
    r"AllowedIPs = (?P<ip>[\d.]+)/32\n$"
)

DISABLED_MARKER_RE = re.compile(
    r"^# Disabled = \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.MULTILINE
)

# The preshared key of the fixture's named peer. It is in awg0.conf and must not
# reach any response, whether or not the panel created that client.
FIXTURE_PSK = "a5PQ57ToVX91TckGUHDDDTfdQQu4UIFOB7Ew0kLPpm0="

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def api_url(path: str) -> str:
    """An API path, base path included. The panel may be mounted under a secret prefix."""
    return f"{settings.BASE_PATH}api/v1/{path}"


def create(client: APIClient, name: str, **fields: object) -> dict:
    """POST one client and hand back the row the API answered with."""
    response = client.post(api_url("clients"), {"name": name, **fields}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def client_secrets(conf_dir, name: str) -> dict[str, str]:
    """The key material in clients/<name>.conf, read straight off disk."""
    text = (conf_dir / "clients" / f"{name}.conf").read_text(encoding="utf-8")
    return parse_client_conf(text)


def peer_named(server_conf, name: str):
    for peer in parse_conf(server_conf.read_text(encoding="utf-8")).peers:
        if peer.name == name:
            return peer
    raise AssertionError(f"no peer called {name!r} in {server_conf}")


@pytest.fixture(autouse=True)
def fresh_controller():
    """One mock interface per test.

    get_controller() caches its instance for the life of the process, and the
    mock remembers which keys it has been told to drop. Left alone, a peer
    disabled in one test would still be missing from the interface in the next.
    """
    reset_controller()
    yield
    reset_controller()


@pytest.fixture
def api(server_conf) -> APIClient:
    """A signed-in API client against the fixture server config.

    force_login rather than a POST to auth/login: what is under test here is the
    client endpoints, and driving the real login form would also drag in axes,
    the TOTP handshake and an Argon2 hash per test, all of which belong to the
    auth tests. The account therefore needs no password at all.
    """
    user = get_user_model().objects.create_user("admin")
    client = APIClient()
    client.force_login(user)
    return client


# ------------------------------------------------------------------- create


def test_create_appends_the_peer_block_bash_would_write(api, server_conf, server_conf_text):
    """The bash tool appends with a here-doc; nothing above the new block moves."""
    created = create(api, "phone")

    text = server_conf.read_text(encoding="utf-8")
    assert text.startswith(server_conf_text)

    match = PEER_BLOCK_RE.search(text[len(server_conf_text) :])
    assert match, text[len(server_conf_text) :]
    assert match.group("name") == "phone"
    assert match.group("public") == created["publicKey"]
    # The fixture holds .2 and .3, so the allocator's answer is .4 - the same one
    # the allocator must keep picking as long as the fixture is unchanged.
    assert match.group("ip") == "10.13.13.4"
    assert created["ip"] == "10.13.13.4"


def test_create_writes_a_client_conf_the_peer_matches(api, server_conf, conf_dir):
    """The private key on disk has to derive to the public key in the peer block.

    If it does not, the config is for a peer the server will not accept, and the
    only symptom is a handshake that never completes.
    """
    created = create(api, "phone")

    secrets = client_secrets(conf_dir, "phone")
    assert keys.pubkey(secrets["PrivateKey"]) == created["publicKey"]
    assert secrets["PresharedKey"] == peer_named(server_conf, "phone").preshared_key
    assert (conf_dir / "clients" / "phone.conf").stat().st_mode & 0o777 == 0o600


def test_creating_the_same_name_twice_is_a_conflict(api):
    create(api, "phone")
    response = api.post(api_url("clients"), {"name": "phone"}, format="json")
    assert response.status_code == 409
    assert "phone" in response.json()["detail"]
    # And it says which 409 it is. The other one is an exhausted address pool,
    # which reads the same to anything branching on the status - and the panel
    # does branch: a name it suggested and the server refused is replaced with
    # another, which would be a nonsense answer to a server that is simply full.
    assert response.json()["code"] == "name_in_use"


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}])
def test_a_client_added_without_a_name_is_given_one(api, server_conf, conf_dir, body):
    """The name is the identifier, so the server draws one rather than doing without.

    Asserted on the config rather than on the reply, like everything else here:
    the name is what the peer's "# Client" comment says and what the client's
    own file is called, and a name that existed only in the JSON would be a
    client nothing could address afterwards.
    """
    response = api.post(api_url("clients"), body, format="json")

    assert response.status_code == 201, response.content
    name = response.json()["name"]
    assert len(name) == names.LENGTH
    assert set(name) <= set(names.ALPHABET)
    assert peer_named(server_conf, name).name == name
    assert (conf_dir / "clients" / f"{name}.conf").is_file()


def test_a_drawn_name_does_not_land_on_a_client_this_server_already_has(api, monkeypatch):
    """A name is an identifier, and "unlikely" is not the same as looked at.

    Forced, because forty-five bits will not produce the collision on its own
    and the branch that survives it has to be exercised by something.
    """
    create(api, "aaaaaaaaa")
    draws = iter(["aaaaaaaaa", "bbbbbbbbb"])
    monkeypatch.setattr(names, "random_name", lambda *args, **kwargs: next(draws))

    row = api.post(api_url("clients"), {}, format="json").json()

    assert row["name"] == "bbbbbbbbb"
    assert row["ip"] == "10.13.13.5"  # a client of its own, not a rewrite of the first


def test_a_drawn_name_does_not_land_on_a_config_file_left_behind(api, conf_dir, monkeypatch):
    """The file is where the private key goes, so a stray one is a name in use.

    A client removed while the config directory was unwritable, or a file an
    admin dropped in by hand, is a name with no peer behind it. Nothing else
    would notice it, and writing over it would destroy the only copy of a key.
    """
    stray = conf_dir / "clients" / "ccccccccc.conf"
    stray.write_text("# put here by hand\n", encoding="utf-8")
    draws = iter(["ccccccccc", "ddddddddd"])
    monkeypatch.setattr(names, "random_name", lambda *args, **kwargs: next(draws))

    row = api.post(api_url("clients"), {}, format="json").json()

    assert row["name"] == "ddddddddd"
    assert stray.read_text(encoding="utf-8") == "# put here by hand\n"


# --------------------------------------------------------------- downloads


def test_config_download_carries_exactly_one_private_key(api, conf_dir):
    """One [Interface] with one PrivateKey: what the Amnezia app importer expects."""
    created = create(api, "phone")

    response = api.get(api_url("clients/phone/config"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    assert 'filename="phone.conf"' in response["Content-Disposition"]
    assert response["Cache-Control"] == "no-store"

    text = response.content.decode("utf-8")
    assert text == (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")

    private_lines = [line for line in text.splitlines() if line.startswith("PrivateKey")]
    assert len(private_lines) == 1

    values = parse_client_conf(text)
    assert keys.is_key(values["PrivateKey"])
    assert keys.pubkey(values["PrivateKey"]) == created["publicKey"]
    assert values["Address"] == "10.13.13.4/32"
    assert values["Endpoint"] == "203.0.113.10:41234"


def test_config_download_of_a_missing_client_is_a_404(api):
    assert api.get(api_url("clients/ghost/config")).status_code == 404


def test_qr_endpoint_returns_a_png(api):
    create(api, "phone")

    response = api.get(api_url("clients/phone/qr"))
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content.startswith(PNG_MAGIC)
    # The image is the private key in another form; a proxy or a disk cache
    # holding it would outlive the tab that asked for it.
    assert "no-store" in response["Cache-Control"]


# ----------------------------------------------------------------- the list


def test_list_never_carries_key_material(api, conf_dir):
    """The one rule the client endpoints cannot get wrong.

    Asserted against the real keys on disk rather than against field names: a
    serializer that gained a `presharedKey` field would still pass a check that
    only looked for the names somebody remembered to list.
    """
    created = create(api, "phone")
    secrets = client_secrets(conf_dir, "phone")

    response = api.get(api_url("clients"))
    assert response.status_code == 200
    raw = response.content.decode("utf-8")

    assert secrets["PrivateKey"] not in raw
    assert secrets["PresharedKey"] not in raw
    assert FIXTURE_PSK not in raw
    assert created["publicKey"] in raw  # the public key is the client's identity

    for row in response.json()["clients"]:
        assert "privateKey" not in row
        assert "presharedKey" not in row


def test_list_reports_the_subnet_and_skips_unnamed_peers(api):
    """The fixture's second peer has no "# Client" comment, so it has no identity
    to offer the API - but its address is still allocated."""
    create(api, "phone")

    body = api.get(api_url("clients")).json()
    # Oldest first: the fixture's client1 was created in 2026, "phone" a moment
    # ago. That is the API's order, not the config's.
    assert [row["name"] for row in body["clients"]] == ["client1", "phone"]
    assert body["subnetCidr"] == "10.13.13.0/24"
    # .2, .3 and .4 taken out of .2 through .254.
    assert body["freeIps"] == 250


def test_the_list_is_ordered_by_when_each_client_was_added(api):
    """Oldest first, whatever order the peers sit in the config file.

    The dates are RFC 3339 in UTC, so the ordering is done on the strings; this
    is the test that says the two orders really are the same thing.
    """
    for name in ("one", "two", "three"):
        create(api, name)

    rows = api.get(api_url("clients")).json()["clients"]
    dates = [row["createdAt"] for row in rows]

    assert dates == sorted(dates)
    # The fixture peer was created the day before the three added just now, so
    # it comes back ahead of all of them.
    assert [row["name"] for row in rows][0] == "client1"


def test_the_creation_date_is_stored_in_the_database_as_the_config_states_it(api, server_conf):
    """The panel keeps its own copy of a date that only a config comment holds.

    A comment does not survive a hand edit or a restore from a config written
    before it existed, and nothing can work the date out again afterwards - so
    the row carries the same instant, to the second, that the peer block does.
    """
    created = create(api, "phone")

    match = PEER_BLOCK_RE.search(server_conf.read_text(encoding="utf-8"))
    assert match, "the peer block should carry a '# Created' comment"
    assert created["createdAt"] == match.group("created")

    stored = ClientMeta.objects.get(public_key=created["publicKey"]).created_at
    assert stored is not None
    assert stored.utcoffset().total_seconds() == 0, "stored in UTC, not in local time"
    assert stored.strftime("%Y-%m-%dT%H:%M:%SZ") == match.group("created")


def test_the_stored_date_answers_once_the_config_comment_is_gone(api, server_conf):
    """Which is the whole reason for keeping a copy."""
    created = create(api, "phone")
    stored = ClientMeta.objects.get(public_key=created["publicKey"]).created_at

    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("# Created")) + "\n",
        encoding="utf-8",
    )

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["createdAt"] == stored.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_quota_and_expiry_are_stored_and_reflected(api):
    """Neither has anywhere to live in a WireGuard config, so both come from the
    database - keyed by public key, which is what survives a rename."""
    quota = 5 * 1024**3
    created = create(api, "phone", quotaBytes=quota, expiresAt="2027-01-01T00:00:00Z")

    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert meta.quota_bytes == quota
    assert meta.expires_at.isoformat() == "2027-01-01T00:00:00+00:00"
    assert meta.name == "phone"

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["quotaBytes"] == quota
    assert row["expiresAt"].startswith("2027-01-01T00:00:00")
    assert row["quotaPercent"] == 0


# ------------------------------------------------------- the bandwidth ceilings
#
# Stored beside the quota and for the same reason - a WireGuard config has
# nowhere to write a rate - and refused here rather than clamped, because this is
# the one moment there is somebody to tell. What the kernel then does with the
# number is tests/test_shaping.py's; every test below runs under AWG_MOCK, so no
# qdisc is touched at all and what is asserted is purely the API's half.


def shaping_on(upload: bool = False) -> None:
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "1" if upload else "0"})


def test_a_client_has_no_bandwidth_ceiling_unless_one_is_asked_for(api):
    row = create(api, "phone")
    assert row["downBps"] == 0
    assert row["upBps"] == 0
    assert ClientMeta.objects.get(public_key=row["publicKey"]).down_bps == 0


def test_a_ceiling_is_stored_and_comes_back_on_the_row(api):
    shaping_on(upload=True)
    row = create(api, "phone", downBps=10_000_000, upBps=2_000_000)

    meta = ClientMeta.objects.get(public_key=row["publicKey"])
    assert (meta.down_bps, meta.up_bps) == (10_000_000, 2_000_000)
    listed = next(
        entry for entry in api.get(api_url("clients")).json()["clients"] if entry["name"] == "phone"
    )
    assert (listed["downBps"], listed["upBps"]) == (10_000_000, 2_000_000)


def test_a_ceiling_can_be_changed_and_cleared_by_a_put(api):
    shaping_on()
    create(api, "phone", downBps=10_000_000)

    api.put(api_url("clients/phone"), {"downBps": 20_000_000}, format="json")
    assert ClientMeta.objects.get(name="phone").down_bps == 20_000_000

    # 0 is how "no limit" is spelled, and it is a different request from leaving
    # the key out - which is what makes an edit of somebody's note safe.
    api.put(api_url("clients/phone"), {"note": "hello"}, format="json")
    assert ClientMeta.objects.get(name="phone").down_bps == 20_000_000
    api.put(api_url("clients/phone"), {"downBps": 0}, format="json")
    assert ClientMeta.objects.get(name="phone").down_bps == 0


def test_a_ceiling_the_shaper_could_not_express_is_refused(api):
    shaping_on()
    response = api.post(
        api_url("clients"),
        {"name": "phone", "downBps": shaping.MAX_BPS * 2},
        format="json",
    )
    assert response.status_code == 400


def test_a_ceiling_on_a_server_with_limits_switched_off_is_refused(api):
    """Nothing would enforce it, and the message says where to switch them on."""
    response = api.post(api_url("clients"), {"name": "phone", "downBps": 10_000_000}, format="json")
    assert response.status_code == 400
    assert "switched off" in json.dumps(response.json())


def test_a_new_client_is_given_the_servers_default_limit(api):
    """Absent means "whatever this server gives new clients", which 0 does not."""
    shaping_on()
    settings_store.set_many({"shaperDefaultDownMbps": "20"})
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    assert ClientMeta.objects.get(name="phone").down_bps == 20 * shaping.MBIT


def test_a_new_client_that_asks_for_no_limit_gets_none(api):
    shaping_on()
    settings_store.set_many({"shaperDefaultDownMbps": "20"})
    api.post(api_url("clients"), {"name": "phone", "downBps": 0}, format="json")
    assert ClientMeta.objects.get(name="phone").down_bps == 0


def test_one_request_gives_every_client_the_same_limit(api):
    shaping_on()
    for name in ("phone", "laptop", "tablet"):
        api.post(api_url("clients"), {"name": name}, format="json")
    response = api.post(
        api_url("clients/bulk-limit"), {"downBps": 20_000_000, "upBps": 0}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["changed"] == ClientMeta.objects.count()
    assert set(ClientMeta.objects.values_list("down_bps", flat=True)) == {20_000_000}


def test_a_bulk_limit_that_names_only_one_direction_is_refused(api):
    """It overwrites everybody, so half a body must not read as "clear the rest"."""
    shaping_on()
    response = api.post(api_url("clients/bulk-limit"), {"downBps": 20_000_000}, format="json")
    assert response.status_code == 400


def test_an_upload_ceiling_on_a_server_that_shapes_no_upload_is_refused(api):
    shaping_on(upload=False)
    response = api.post(api_url("clients"), {"name": "phone", "upBps": 2_000_000}, format="json")
    assert response.status_code == 400
    assert "upload" in json.dumps(response.json()).lower()


@pytest.mark.parametrize("field", ["dns", "allowedIps"])
def test_a_line_break_in_a_config_value_is_a_400(api, conf_dir, field):
    """A newline does not make a long DNS setting, it makes a second setting.

    `DNS = 1.1.1.1\\nPostUp = curl ...` is a valid config carrying a PostUp, and
    wg-quick runs a PostUp as root on the device that imports it. The refusal
    comes out of the store rather than the serializer, so it covers the CLI and
    anything else that creates a client - and it still reaches the caller as an
    ordinary field error.
    """
    response = api.post(
        api_url("clients"),
        {"name": "phone", field: "1.1.1.1\nPostUp = touch /tmp/pwned"},
        format="json",
    )

    assert response.status_code == 400
    assert "line break" in json.dumps(response.json())
    assert not (conf_dir / "clients" / "phone.conf").exists()


def test_a_line_break_cannot_be_smuggled_through_an_update(api, conf_dir):
    api.post(api_url("clients"), {"name": "phone"}, format="json")
    before = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")

    response = api.put(api_url("clients/phone"), {"dns": "1.1.1.1\nPostUp = id"}, format="json")

    assert response.status_code == 400
    assert (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8") == before


def test_a_ceiling_of_zero_is_accepted_whatever_the_server_is_set_to(api):
    """Because it is not a limit: it is the absence of one, and every client has it."""
    response = api.post(
        api_url("clients"), {"name": "phone", "downBps": 0, "upBps": 0}, format="json"
    )
    assert response.status_code == 201


def test_a_ceiling_survives_a_rename(api):
    """Keyed by public key, like everything else the config file cannot hold."""
    shaping_on()
    created = create(api, "phone", downBps=10_000_000)
    api.put(api_url("clients/phone"), {"name": "work-phone"}, format="json")
    assert ClientMeta.objects.get(public_key=created["publicKey"]).down_bps == 10_000_000


def test_an_expiry_is_honoured_to_the_minute_and_not_to_the_day(api):
    """An expiry is a moment, not a date, and the panel lets one be set for six
    this evening. So the same afternoon has to be able to fall on either side of
    it: a client whose moment is still to come is live, and one whose moment
    passed an hour ago has lapsed on a date that is still today.

    Asked of the sweep, because that is the one place on the server where a date
    is ruled on by itself. The status word deliberately says nothing about an
    expiry, and the list hands the browser the timestamp rather than a verdict on
    it.
    """
    now = timezone.now()
    later = create(api, "phone", expiresAt=(now + timedelta(hours=2)).isoformat())
    passed = create(api, "laptop", expiresAt=(now - timedelta(hours=1)).isoformat())

    # To the minute in the database too: a value rounded to the day would put
    # both of these on the same side of it.
    assert ClientMeta.objects.get(public_key=later["publicKey"]).expires_at.minute == now.minute
    assert ClientMeta.objects.get(public_key=passed["publicKey"]).expires_at.minute == now.minute

    assert api.post(api_url("clients/remove-expired")).json() == {
        "removed": ["laptop"],
        "count": 1,
    }


def test_last_seen_and_endpoint_outlive_the_interface_they_came_from(api):
    """Both are kernel state, and the kernel drops them when the tunnel goes
    down - a reboot, a settings change, a restore. The stored copy is what the
    list falls back to, and it must not make a peer look connected."""
    created = create(api, "phone")
    seen = int(timezone.now().timestamp()) - 600
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(
        last_handshake=seen, last_endpoint="198.51.100.7:51820"
    )

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["lastHandshake"] == seen
    # With no live blob to read a receive clock out of, the stored handshake is
    # the only evidence this peer was ever there, and it is what "last seen"
    # falls back to rather than reading as never.
    assert row["lastSeen"] == seen
    assert row["endpoint"] == "198.51.100.7:51820"
    # Ten minutes ago is not now, and it is not "a while ago" either: nothing has
    # been heard from this client since, so it is off the tunnel.
    assert row["online"] is False
    assert row["status"] == "offline"


def test_a_row_says_what_reset_usage_took_off_its_totals(api, conf_dir):
    """The page folds the collector's blob over these rows every two seconds,
    and the blob's totals are the peer's whole life: the collector is not told
    that a counter was cleared, because what was cleared is recorded here and
    nowhere it can see. So the figure is sent with the row, and subtracting it
    from the blob has to land exactly on what this endpoint would have said."""
    created = create(api, "phone")
    key = created["publicKey"]
    (conf_dir / "traffic.db").write_text(f"{key} 9000 4000 9000 4000\n", encoding="utf-8")
    ClientMeta.objects.filter(public_key=key).update(offset_rx=1000, offset_tx=500)

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )

    assert (row["offsetRx"], row["offsetTx"]) == (1000, 500)
    # Which is the arithmetic the browser does against the blob's 9000 and 4000.
    assert (row["rxBytes"], row["txBytes"]) == (9000 - 1000, 4000 - 500)


def write_live(public_key: str, **peer: object) -> None:
    """Put one peer into a live blob stamped now, so the API reads it as current."""
    now = int(timezone.now().timestamp())
    live_state_file().write_text(
        json.dumps({"ts": now, "ifaceUp": True, "peers": {public_key: peer}}),
        encoding="utf-8",
    )


def test_a_client_that_stopped_sending_drops_offline_without_waiting_for_the_handshake(api):
    """The regression, as a timeline: switching a client off used to leave it
    "online" for the whole handshake threshold and then "idle" for a day.

    Every config this project issues carries PersistentKeepalive, so a connected
    client's receive counter moves every 25 seconds whether or not anybody is
    using it. Silence on that counter is what says it has gone, and it says so
    long before the kernel's rekey clock does - which is the only reason the
    answer arrives in about a minute rather than three.
    """
    created = create(api, "phone")
    now = int(timezone.now().timestamp())

    def status_with(handshake: int, last_rx: int) -> dict:
        write_live(created["publicKey"], handshake=handshake, lastRx=last_rx, rateRx=0, rateTx=0)
        return next(
            row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
        )

    # Still sending: connected, however long ago it last rekeyed.
    assert status_with(now - 600, now - 5)["status"] == "online"

    # Quiet for four keepalives, with a handshake the 180 s threshold would call
    # fresh. This is the case that used to read "online" for two more minutes.
    just_dropped = status_with(now - 30, now - 100)
    assert just_dropped["online"] is False
    assert just_dropped["status"] == "idle"

    # ...and past the grace it is simply gone, which is the word that never used
    # to arrive until the following day.
    assert status_with(now - 600, now - 600)["status"] == "offline"


def test_a_client_still_sending_keepalives_reads_online(api):
    """The other half: silence is only meaningful because packets are not.

    A client that has said nothing for a couple of seconds is simply between
    keepalives, and calling that a disconnection would make every row flicker.
    """
    created = create(api, "phone")
    now = int(timezone.now().timestamp())
    # A handshake old enough that the threshold alone would call this peer gone.
    write_live(created["publicKey"], handshake=now - 600, lastRx=now - 5, rateRx=128, rateTx=128)

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["online"] is True
    assert row["status"] == "online"


def test_last_seen_is_the_last_packet_rather_than_the_last_rekey(api):
    """The regression: "Last seen" counting up to two minutes on a client that
    was transferring at that moment.

    WireGuard rekeys about every two minutes and nothing about that is visible
    to the client, so the handshake spends most of its life minutes stale while
    the peer behind it was heard from seconds ago. A row drawn from the
    handshake said "online" and "2m ago" at once, out of the same request, for
    no better reason than that the lamp had been moved onto the receive counter
    and the column had not.
    """
    created = create(api, "phone")
    now = int(timezone.now().timestamp())
    write_live(created["publicKey"], handshake=now - 110, lastRx=now - 3, rateRx=4096, rateTx=4096)

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["lastSeen"] == now - 3
    # The kernel's own figure is still reported and still old. It is a true
    # answer to a different question, and it is what the sort orders on.
    assert row["lastHandshake"] == now - 110
    assert row["online"] is True


def test_last_seen_falls_back_to_the_handshake_with_no_receive_clock(api):
    """Two cases arrive here looking identical: a blob written by a collector too
    old to report `lastRx` at all, and a peer the running collector has not yet
    seen send anything since it started. Neither is a client that has never been
    heard from, and the handshake is what says so."""
    created = create(api, "phone")
    now = int(timezone.now().timestamp())
    write_live(created["publicKey"], handshake=now - 30, rateRx=0, rateTx=0)

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["lastSeen"] == now - 30


def test_the_liveness_window_is_never_tighter_than_the_collector_looks():
    """`lastRx` is only as current as the last poll, so a server that looks once
    a minute cannot call a client silent after fifty seconds - it would be
    reporting its own polling interval as a disconnection."""
    from apps.clients.merge import liveness_window

    # The default: two keepalives, and the poll is nowhere near the binding term.
    assert liveness_window("25", poll=2) == 50
    # A slow poll takes over before it can manufacture silence.
    assert liveness_window("25", poll=60) == 120
    # A keepalive small enough to flap is floored instead.
    assert liveness_window("1", poll=2) == 30
    # No keepalive is no signal, whatever the poll.
    assert liveness_window("", poll=2) == 0
    assert liveness_window("0", poll=2) == 0


def test_a_server_with_no_keepalive_falls_back_to_the_handshake(api):
    """With no keepalive there is no liveness signal: a quiet client and a
    departed one are indistinguishable, so the handshake clock is all there is
    and nothing is inferred from silence."""
    created = create(api, "phone")
    clientsenv.update_env({"KEEPALIVE": ""})
    now = int(timezone.now().timestamp())
    write_live(created["publicKey"], handshake=now - 60, lastRx=now - 100, rateRx=0, rateTx=0)

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    assert row["online"] is True


def test_a_config_that_lost_its_peers_does_not_take_the_metadata_with_it(api, server_conf):
    """The list prunes rows whose peer is gone, and must not do it on an empty config.

    parse_conf reports a truncated or half-restored config as a config with no
    peers rather than as an error, so without the guard one dashboard poll would
    delete every quota, expiry and offset on the server with nothing to restore
    them from.
    """
    created = create(api, "phone", quotaBytes=4096, note="spare")
    ClientMeta.objects.update(updated_at=timezone.now() - timedelta(hours=1))

    server_conf.write_text("[Interface]\nListenPort = 41234\n", encoding="utf-8")

    assert api.get(api_url("clients")).json()["clients"] == []
    assert ClientMeta.objects.get(public_key=created["publicKey"]).quota_bytes == 4096


def test_quota_and_expiry_survive_a_put(api):
    created = create(api, "phone")

    response = api.put(
        api_url("clients/phone"),
        {"quotaBytes": 1024, "expiresAt": "2026-12-31T23:00:00Z", "note": "office laptop"},
        format="json",
    )
    assert response.status_code == 200

    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert (meta.quota_bytes, meta.note) == (1024, "office laptop")
    assert meta.expires_at.isoformat() == "2026-12-31T23:00:00+00:00"
    assert response.json()["quotaBytes"] == 1024


# ------------------------------------------------------------------ paging
#
# The list is one page now. What these check is that the narrowing happens
# where the counts are still whole: a page is only readable next to a total it
# is a page of, and every count the UI puts a number on - the sweep button, the
# "N clients found" line - is counted over the server rather than over the
# twenty rows that came back.


def test_the_list_answers_one_page_and_says_how_many_there_are(api):
    """Default page size, and the two totals that make a page mean something."""
    for index in range(4):
        create(api, f"c{index}")

    body = api.get(api_url("clients"), {"pageSize": 2}).json()

    assert [row["name"] for row in body["clients"]] == ["client1", "c0"]
    assert (body["page"], body["pageSize"]) == (1, 2)
    # Five named clients: the fixture's, plus the four just made.
    assert (body["total"], body["totalAll"]) == (5, 5)


def test_the_second_page_carries_on_where_the_first_stopped(api):
    for index in range(4):
        create(api, f"c{index}")

    first = api.get(api_url("clients"), {"pageSize": 2, "page": 1}).json()
    second = api.get(api_url("clients"), {"pageSize": 2, "page": 2}).json()

    assert [row["name"] for row in second["clients"]] == ["c1", "c2"]
    assert second["page"] == 2
    # No row on both pages, which is the property a paged list actually needs.
    assert not {row["name"] for row in first["clients"]} & {
        row["name"] for row in second["clients"]
    }


def test_a_page_past_the_end_answers_the_last_one_with_rows(api):
    """Deleting the only client on the last page is the ordinary way to get here.

    An empty table would leave the operator with no sign that the rest of the
    list is still where it was, so the request is answered with the page that
    now holds the end of it.
    """
    create(api, "phone")

    body = api.get(api_url("clients"), {"pageSize": 1, "page": 99}).json()

    assert body["page"] == 2
    assert [row["name"] for row in body["clients"]] == ["phone"]


def test_a_page_size_of_zero_asks_for_every_row(api):
    """The panel's "All", which cannot name a size because it does not know the count.

    Capping it at MAX_PAGE_SIZE was what this replaced: an operator with more
    clients than that picked All and got a pager under it, which is the one
    answer the word rules out.
    """
    for index in range(4):
        create(api, f"c{index}")

    body = api.get(api_url("clients"), {"pageSize": 0}).json()

    assert len(body["clients"]) == body["total"] == 5
    # Echoed rather than reported as the row count: it is what the page was cut
    # by, and it was not cut.
    assert (body["page"], body["pageSize"]) == (1, 0)


def test_an_unpaged_request_narrows_like_any_other(api):
    """Asking for every row means every row the search and the filter left."""
    create(api, "alice-phone")
    create(api, "bob-laptop")

    body = api.get(api_url("clients"), {"pageSize": 0, "q": "laptop"}).json()

    assert [row["name"] for row in body["clients"]] == ["bob-laptop"]
    assert body["total"] == 1


def test_an_unpaged_request_ignores_the_page_it_was_asked_for(api):
    """`?pageSize=0&page=4` is a bookmark of a view since asked for whole.

    There is no page four of one page, and nothing here rejects a stale query
    string - so it is answered with the only page there is rather than with an
    empty table.
    """
    create(api, "phone")

    body = api.get(api_url("clients"), {"pageSize": 0, "page": 4}).json()

    assert body["page"] == 1
    assert len(body["clients"]) == 2


def test_a_page_size_over_the_cap_is_not_a_way_to_ask_for_everything(api):
    """Above MAX_PAGE_SIZE the query string is rejected, and a rejected query
    string is answered with the default page - the search and the filter
    discarded along with the size. Which is why "all" is zero and not a big
    number: this is what a big number does."""
    create(api, "alice-phone")
    create(api, "bob-laptop")

    body = api.get(api_url("clients"), {"pageSize": 100000, "q": "laptop"}).json()

    assert body["pageSize"] == 50
    assert body["total"] == 3


def test_the_search_looks_where_an_admin_would_have_looked(api):
    """Name, address, note, email and endpoint - the five fields the box searched
    when the browser did this, matched as substrings."""
    create(api, "alice-phone", note="office laptop", email="a@example.com")
    create(api, "bob-laptop")

    def names(**params) -> list[str]:
        return [row["name"] for row in api.get(api_url("clients"), params).json()["clients"]]

    assert names(q="phone") == ["alice-phone"]
    assert names(q="OFFICE") == ["alice-phone"]  # case-insensitive
    assert names(q="a@example") == ["alice-phone"]
    assert names(q="laptop") == ["alice-phone", "bob-laptop"]  # note and name alike
    assert names(q="nothing-matches-this") == []


def test_the_search_narrows_the_total_but_not_the_counts_over_the_server(api):
    """`total` is what the search found; `totalAll` is what the server holds.

    Both are on the wire because they answer different questions - one sizes the
    pagination, the other is the number of clients there are - and a UI that had
    only the first would report a searched server as having shrunk.
    """
    create(api, "alice")
    create(api, "bob")

    body = api.get(api_url("clients"), {"q": "alice"}).json()

    assert [row["name"] for row in body["clients"]] == ["alice"]
    assert body["total"] == 1
    assert body["totalAll"] == 3


def test_the_status_filter_selects_by_the_word_the_table_shows(api, server_conf):
    """A client the status column calls disabled is one "disabled" returns."""
    create(api, "on")
    create(api, "off")
    api.put(api_url("clients/off"), {"enabled": False}, format="json")

    def names(status: str) -> list[str]:
        params = {"status": status, "pageSize": 500}
        return [row["name"] for row in api.get(api_url("clients"), params).json()["clients"]]

    assert names("disabled") == ["off"]
    assert "off" not in names("active")
    assert "on" in names("active")
    assert set(names("all")) == {"client1", "on", "off"}


def test_the_expired_filter_reads_the_date_and_not_the_status_word(api):
    """The two are separate questions, and a lapsed client an admin switched back
    on answers yes to "expired" while reading as online."""
    create(api, "lapsed", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    create(api, "current", expiresAt=(timezone.now() + timedelta(days=30)).isoformat())

    body = api.get(api_url("clients"), {"status": "expired"}).json()

    assert [row["name"] for row in body["clients"]] == ["lapsed"]


def test_the_expired_count_is_over_every_client_not_the_page(api):
    """The sweep deletes the expired clients a filter is hiding as readily as the
    ones on screen, so a button counting only the visible ones would understate it."""
    for index in range(3):
        create(api, f"old{index}", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    create(api, "current")

    body = api.get(api_url("clients"), {"pageSize": 1, "q": "current"}).json()

    assert len(body["clients"]) == 1
    assert body["expiredCount"] == 3


def test_a_client_kept_on_past_its_date_is_not_counted_as_sweepable(api):
    """One short of every expired client, because the sweep refuses that one too."""
    create(api, "lapsed", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/lapsed"), {"enabled": True}, format="json")

    body = api.get(api_url("clients")).json()

    assert body["expiredCount"] == 0
    # Still expired, still listed under the filter that reads the date.
    assert [
        row["name"] for row in api.get(api_url("clients"), {"status": "expired"}).json()["clients"]
    ] == ["lapsed"]


def test_sorting_by_name_orders_the_whole_list_and_not_the_page(api):
    """The sort happens before the cut, so page one holds the first rows of the
    sorted list - not the first rows of the config, re-ordered among themselves."""
    for name in ("delta", "alpha", "charlie", "bravo"):
        create(api, name)

    body = api.get(api_url("clients"), {"sort": "name", "pageSize": 2}).json()

    assert [row["name"] for row in body["clients"]] == ["alpha", "bravo"]


def test_sorting_by_address_reads_the_pool_in_order(api):
    """Text order would put .10 before .2, which is the whole point of the column."""
    for index in range(9):
        create(api, f"c{index}")

    body = api.get(api_url("clients"), {"sort": "ip", "pageSize": 500}).json()
    last_octets = [int(row["ip"].split("/")[0].split(".")[-1]) for row in body["clients"]]

    assert last_octets == sorted(last_octets)


def test_the_direction_reverses_the_order(api):
    for name in ("delta", "alpha", "charlie"):
        create(api, name)

    ascending = api.get(api_url("clients"), {"sort": "name", "pageSize": 500}).json()
    descending = api.get(
        api_url("clients"), {"sort": "name", "direction": "desc", "pageSize": 500}
    ).json()

    assert [row["name"] for row in descending["clients"]] == list(
        reversed([row["name"] for row in ascending["clients"]])
    )


def test_a_query_string_this_version_does_not_understand_answers_the_default_page(api):
    """A stale bookmark is answered with the client list, not with a 400 about a
    filter word that was removed two versions ago."""
    create(api, "phone")

    response = api.get(api_url("clients"), {"status": "banana", "sort": "sideways", "page": "-3"})

    assert response.status_code == 200
    assert [row["name"] for row in response.json()["clients"]] == ["client1", "phone"]


def test_a_client_deep_in_the_list_is_still_addressable_by_name(api):
    """The single-client endpoint searches every row, not the first page.

    A server that has had a client for a year holds it well past page one, and
    reusing the paged list here would 404 exactly the clients an operator is
    most likely to be asking about.
    """
    for index in range(60):
        create(api, f"c{index}")

    response = api.get(api_url("clients/c55"))

    assert response.status_code == 200
    assert response.json()["name"] == "c55"


# --------------------------------------------------------------------- dns
#
# Every other field on a row is a read per request - one config parse, one
# traffic file, one live blob, however many clients there are. `dns` is the
# exception: it lives in the client's own config file, so a row that carried it
# cost the server an open and a parse per client, on a list polled every thirty
# seconds by every open tab, for a value the table has no column for. It is
# served on a single client instead, which is where the edit form reads it.


def test_a_list_row_does_not_carry_the_dns_and_a_single_client_does(api):
    create(api, "phone", dns="1.0.0.1")

    row = next(
        row for row in api.get(api_url("clients")).json()["clients"] if row["name"] == "phone"
    )
    one = api.get(api_url("clients/phone")).json()

    # Absent, not blank. "" is a client that was asked and has no DNS line of
    # its own, and an edit form that could not tell the two apart would show an
    # empty box for a client that has one.
    assert "dns" not in row
    assert one["dns"] == "1.0.0.1"


def test_the_list_does_not_read_a_config_file_per_client(api, monkeypatch):
    """The whole point of the field moving. This counts the reads rather than
    timing them, because the cost is one open per client and that is a thing a
    test can state exactly."""
    for index in range(20):
        create(api, f"c{index}")

    reads: list[str | None] = []
    original = merge._client_dns

    def counted(name: str | None) -> str:
        reads.append(name)
        return original(name)

    monkeypatch.setattr(merge, "_client_dns", counted)

    api.get(api_url("clients"))
    assert reads == []

    api.get(api_url("clients/c7"))
    assert reads == ["c7"]


def test_the_dns_is_the_line_in_the_file_and_not_what_the_panel_would_default_to(api, conf_dir):
    """A client created without one is still issued the server's, written into
    its own config - so that is what comes back, because the file is what the
    device is holding. Blank is reserved for a config with no DNS line at all,
    which is a hand edit or a server that sets none."""
    create(api, "phone")
    assert api.get(api_url("clients/phone")).json()["dns"] == "8.8.8.8, 8.8.4.4"

    conf = conf_dir / "clients" / "phone.conf"
    conf.write_text(
        "\n".join(line for line in conf.read_text().splitlines() if not line.startswith("DNS")),
        encoding="utf-8",
    )

    assert api.get(api_url("clients/phone")).json()["dns"] == ""


def test_emptying_a_field_puts_the_client_back_on_the_server_default(api, conf_dir):
    """The panel's DNS box can be emptied, and emptying it has to mean something.

    An absent field means "leave it alone" and an empty one means "drop my own
    setting", which is the client back on the server default - the same thing a
    client created without one is given. Both used to be collapsed into the
    first, so the box could be emptied, saved, answered 200, and reopened with
    the old resolver still in it.
    """
    create(api, "phone", dns="10.10.10.10", allowedIps="10.0.0.0/8")

    response = api.put(api_url("clients/phone"), {"dns": ""}, format="json")

    assert response.status_code == 200
    text = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")
    assert "DNS = 8.8.8.8, 8.8.4.4" in text
    # Untouched, because nothing was said about it.
    assert "AllowedIPs = 10.0.0.0/8" in text


# --------------------------------------------------------- enable / disable


def test_disabling_writes_the_marker_and_revokes_the_key(api, server_conf):
    """The marker is a comment the bash tools ignore; the revocation is that the
    stripped config handed to `awg syncconf` no longer mentions the peer."""
    created = create(api, "phone")

    response = api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["disabledReason"] == "manual"

    text = server_conf.read_text(encoding="utf-8")
    assert DISABLED_MARKER_RE.search(text)
    assert peer_named(server_conf, "phone").disabled_at is not None
    # The peer entry itself stays, so the address remains allocated.
    assert created["publicKey"] in text
    assert created["publicKey"] not in strip_conf(parse_conf(text))


def test_enabling_removes_the_marker_and_the_reason(api, server_conf):
    created = create(api, "phone")
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")

    response = api.put(api_url("clients/phone"), {"enabled": True}, format="json")
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["disabledReason"] == ""

    text = server_conf.read_text(encoding="utf-8")
    assert peer_named(server_conf, "phone").disabled_at is None
    assert created["publicKey"] in strip_conf(parse_conf(text))
    assert ClientMeta.objects.get(public_key=created["publicKey"]).disabled_reason == ""


# ------------------------------------------- switched off and expired, separately


def test_the_status_word_never_answers_the_expiry_question(api):
    """Four clients covering both answers to both questions, and no word doing
    double duty for two of them.

    Whether a client is switched off and whether its date has gone are
    independent facts, and the row states them in two fields that never borrow
    from each other. `status` is only ever about the first, so "expired" is not
    among the words it can produce for any of these four - not even for the one
    the collector switched off precisely because of its date. `expiresAt` carries
    the second, on all four, including the two that are off.

    `disabledReason` is where the two meet, and the only place: it says why a
    dark client is dark, which is a fact about the switch and not about the date.
    """
    now = timezone.now()
    create(api, "live", expiresAt=(now + timedelta(days=7)).isoformat())
    create(api, "lapsed", expiresAt=(now - timedelta(days=1)).isoformat())
    create(api, "off", expiresAt=(now + timedelta(days=7)).isoformat())
    swept = create(api, "off-and-lapsed", expiresAt=(now - timedelta(days=1)).isoformat())
    api.put(api_url("clients/off"), {"enabled": False}, format="json")
    api.put(api_url("clients/off-and-lapsed"), {"enabled": False}, format="json")
    # The state the collector leaves behind when a date is what switched a client
    # off, rather than an admin.
    ClientMeta.objects.filter(public_key=swept["publicKey"]).update(disabled_reason="expired")

    rows = {row["name"]: row for row in api.get(api_url("clients")).json()["clients"]}

    # Nothing about a date, however long ago it went.
    assert rows["live"]["status"] == "offline"
    assert rows["lapsed"]["status"] == "offline"
    assert rows["off"]["status"] == "disabled"
    assert rows["off-and-lapsed"]["status"] == "disabled"

    # And the date on every one of them, said once, in the field that is for it.
    mine = ["live", "lapsed", "off", "off-and-lapsed"]
    assert all(rows[name]["expiresAt"] is not None for name in mine)
    assert all(rows[name]["expiryOverridden"] is False for name in mine)

    assert rows["live"]["disabledReason"] == ""
    assert rows["lapsed"]["disabledReason"] == ""
    assert rows["off"]["disabledReason"] == "manual"
    assert rows["off-and-lapsed"]["disabledReason"] == "expired"


def test_switching_a_lapsed_client_on_is_recorded_against_the_date_it_overrules(api, server_conf):
    """The switch has to still mean something a minute later.

    Until this was written down it did not: the collector re-read the date on its
    next pass and put the client straight back off, so a passed date and a dark
    client were the same fact and there was no way to give somebody a few more
    days without editing the date they had run out on. What is stored is the date
    itself, so the reprieve is tied to the decision that was actually made. The
    collector's half of this is in test_collector.
    """
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")

    row = api.put(api_url("clients/phone"), {"enabled": True}, format="json").json()
    assert row["enabled"] is True
    assert row["disabledReason"] == ""
    assert row["expiryOverridden"] is True
    # Still lapsed, and the row still says so: the date did not move, and this is
    # not a way of pretending it did.
    assert row["expiresAt"] is not None
    assert row["status"] == "offline"

    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert meta.expiry_override_at == meta.expires_at
    assert created["publicKey"] in strip_conf(parse_conf(server_conf.read_text(encoding="utf-8")))


def test_switching_on_a_client_whose_date_is_still_to_come_records_nothing(api):
    """There is nothing to overrule yet, and a date stored now would be waiting
    to forgive that expiry the moment it arrived."""
    created = create(api, "phone", expiresAt=(timezone.now() + timedelta(days=7)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")

    row = api.put(api_url("clients/phone"), {"enabled": True}, format="json").json()
    assert row["expiryOverridden"] is False
    assert ClientMeta.objects.get(public_key=created["publicKey"]).expiry_override_at is None


def test_moving_the_date_takes_the_reprieve_with_it(api):
    """A reprieve is a decision about one date. Any other date is one nobody has
    been asked about, whether it is further off or further back, and enforcement
    is in force again for it."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    api.put(api_url("clients/phone"), {"enabled": True}, format="json")

    moved = (timezone.now() - timedelta(hours=2)).isoformat()
    row = api.put(api_url("clients/phone"), {"expiresAt": moved}, format="json").json()
    assert row["expiryOverridden"] is False
    assert ClientMeta.objects.get(public_key=created["publicKey"]).expiry_override_at is None

    # And a date taken away leaves nothing behind that could forgive a later one.
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    api.put(api_url("clients/phone"), {"enabled": True}, format="json")
    cleared = api.put(api_url("clients/phone"), {"expiresAt": None}, format="json").json()
    assert cleared["expiresAt"] is None
    assert cleared["expiryOverridden"] is False
    assert ClientMeta.objects.get(public_key=created["publicKey"]).expiry_override_at is None


def test_switching_a_kept_client_off_again_ends_the_reprieve(api):
    """The admin is now saying the opposite of what they said before. Left
    behind, the reprieve would quietly survive into the next time anything - the
    panel, or an admin deleting the marker by hand - switched this client on."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    api.put(api_url("clients/phone"), {"enabled": True}, format="json")

    row = api.put(api_url("clients/phone"), {"enabled": False}, format="json").json()
    assert row["expiryOverridden"] is False
    assert row["disabledReason"] == "manual"
    assert ClientMeta.objects.get(public_key=created["publicKey"]).expiry_override_at is None


def test_an_edit_that_mentions_neither_leaves_the_reprieve_alone(api):
    """A note, an address, a quota: none of them is a decision about the expiry.
    An admin correcting somebody's email must not switch their client off as a
    side effect."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    api.put(api_url("clients/phone"), {"enabled": True}, format="json")

    row = api.put(
        api_url("clients/phone"),
        {"note": "paid for another week", "quotaBytes": 1024**3},
        format="json",
    ).json()
    assert row["expiryOverridden"] is True
    assert row["note"] == "paid for another week"
    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert meta.expiry_override_at == meta.expires_at


def test_a_put_that_repeats_the_same_date_is_not_a_change_of_mind(api):
    """The reprieve ends when the date moves, and a date that was sent again
    unchanged has not moved. The panel's own edit form only sends the fields it
    saw change, but nothing on the wire enforces that, and a client sending its
    whole row back must not switch itself off by saying nothing new."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    row = api.put(api_url("clients/phone"), {"enabled": True}, format="json").json()

    same = api.put(api_url("clients/phone"), {"expiresAt": row["expiresAt"]}, format="json").json()
    assert same["expiryOverridden"] is True
    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert meta.expiry_override_at == meta.expires_at


def test_a_rename_leaves_the_reprieve_alone(api):
    """The row is keyed by public key, which a rename does not touch - and the
    handler writes the name into the same statement that could clear this."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    api.put(api_url("clients/phone"), {"enabled": True}, format="json")

    row = api.put(api_url("clients/phone"), {"name": "work-phone"}, format="json").json()
    assert row["name"] == "work-phone"
    assert row["expiryOverridden"] is True
    assert ClientMeta.objects.get(public_key=created["publicKey"]).expiry_override_at is not None


# ------------------------------------------------------------------ rename


def test_rename_keeps_the_metadata_row(api, server_conf, conf_dir):
    """ClientMeta is keyed by public key precisely so this holds - and so that a
    rename made over SSH with an editor keeps the quota too."""
    created = create(api, "phone", quotaBytes=4096, email="a@example.org", note="spare")

    response = api.put(api_url("clients/phone"), {"name": "work-phone"}, format="json")
    assert response.status_code == 200
    assert response.json()["name"] == "work-phone"
    assert response.json()["publicKey"] == created["publicKey"]

    # One row for this client, not two: the rename moved the name it carries
    # rather than opening a second row under the new one. Counted by key rather
    # than over the table, because the table also holds a row for every other
    # peer in the config - the index opens one per peer, and the collector always
    # did for any peer it saw a handshake from.
    assert ClientMeta.objects.filter(public_key=created["publicKey"]).count() == 1
    assert not ClientMeta.objects.filter(name="phone").exists()
    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    assert meta.name == "work-phone"
    assert (meta.quota_bytes, meta.email, meta.note) == (4096, "a@example.org", "spare")

    assert "# Client = work-phone" in server_conf.read_text(encoding="utf-8")
    assert (conf_dir / "clients" / "work-phone.conf").is_file()
    assert not (conf_dir / "clients" / "phone.conf").exists()


def test_rename_onto_an_existing_name_changes_nothing(api, server_conf):
    create(api, "phone")
    create(api, "laptop")
    before = server_conf.read_text(encoding="utf-8")

    response = api.put(api_url("clients/phone"), {"name": "laptop"}, format="json")
    assert response.status_code == 409
    assert server_conf.read_text(encoding="utf-8") == before


# -------------------------------------------------------------- reset keys


def test_reset_keys_carries_everything_keyed_by_the_old_key(api, server_conf):
    """A rotation is the same client with new keys, so what it owns follows it.

    Three stores hang off the public key - the config, traffic.db and the
    metadata row - and a rotation that moves only some of them leaves the client
    reading as brand new, or leaves rows nothing will ever read again. Traffic
    history used to be a fourth and no longer is: it is one server-wide total
    per day, which a rekey cannot disturb.
    """
    created = create(api, "phone", quotaBytes=4096, note="spare")
    old_key = created["publicKey"]
    traffic.write_db({old_key: Counters(10, 20, 10, 20)})

    response = api.post(api_url("clients/phone/reset-keys"), {}, format="json")
    assert response.status_code == 200
    new_key = response.json()["publicKey"]
    assert new_key != old_key
    assert peer_named(server_conf, "phone").public_key == new_key

    assert ClientMeta.objects.get(public_key=new_key).quota_bytes == 4096
    assert not ClientMeta.objects.filter(public_key=old_key).exists()
    # The kernel starts the new peer at zero, so the raw counters reset while the
    # all-time totals carry over.
    assert traffic.read_db()[new_key].cum_rx == 10


# ------------------------------------------------------------- reset usage


def test_reset_usage_takes_the_clients_history_along_with_its_counters(api, conf_dir):
    """One button, both figures, because to whoever presses it there is one.

    The offsets are what the list subtracts from traffic.db and the rows are
    what the history chart draws, and a client handed on to somebody else is
    meant to start from nothing. A chart still showing last month's evenings
    would be the half of the reset that did not happen.
    """
    created = create(api, "phone")
    key = created["publicKey"]
    (conf_dir / "traffic.db").write_text(f"{key} 9000 4000 9000 4000\n", encoding="utf-8")
    meta = ClientMeta.objects.get(public_key=key)
    today = utc_day(timezone.now())
    ClientDaily.objects.create(meta=meta, day=today, rx=10, tx=20)
    ClientDaily.objects.create(meta=meta, day=today - timedelta(days=200), rx=30, tx=40)

    response = api.post(api_url("clients/phone/reset-usage"), {}, format="json")

    assert response.status_code == 204
    meta.refresh_from_db()
    assert (meta.offset_rx, meta.offset_tx) == (9000, 4000)
    assert ClientDaily.objects.filter(meta=meta).count() == 0
    # traffic.db is still the one thing a reset cannot destroy: it is the only
    # record of what the peer has ever moved, and the panel subtracts from it.
    assert traffic.read_db()[key].cum_rx == 9000
    # Stamped for the collector, which is holding today's figure in memory in
    # another process and would otherwise write it straight back.
    assert meta.history_reset_at is not None


def test_reset_usage_leaves_the_server_and_the_other_clients_out_of_it(api, server_conf):
    """The narrowest thing the button can be, and the reason it is safe to press.

    Nothing here is a claim about what the server carried: those bytes crossed
    the wire, whoever has since had their figures cleared, so DailyTotal is not
    touched and the dashboard reads the same after this as before it.
    """
    create(api, "phone")
    keeper = create(api, "laptop")
    other = ClientMeta.objects.get(public_key=keeper["publicKey"])
    today = utc_day(timezone.now())
    ClientDaily.objects.create(meta=other, day=today, rx=700, tx=800)
    DailyTotal.objects.create(day=today, rx=710, tx=820)

    api.post(api_url("clients/phone/reset-usage"), {}, format="json")

    assert ClientDaily.objects.filter(meta=other).count() == 1
    total = DailyTotal.objects.get(day=today)
    assert (total.rx, total.tx) == (710, 820)
    assert ClientMeta.objects.get(pk=other.pk).history_reset_at is None


def test_the_history_chart_reads_empty_the_moment_the_reset_returns(api):
    """The endpoint the dialog draws from, asked straight after the button it
    now carries: the deletion happens in the request rather than being left to
    the collector's next pass, so the chart the operator is looking at goes flat
    as soon as it refetches."""
    created = create(api, "phone")
    meta = ClientMeta.objects.get(public_key=created["publicKey"])
    ClientDaily.objects.create(meta=meta, day=utc_day(timezone.now()), rx=10, tx=20)

    api.post(api_url("clients/phone/reset-usage"), {}, format="json")
    body = api.get(api_url("clients/phone/traffic")).json()

    assert all(point["rx"] == 0 and point["tx"] == 0 for point in body["daily"])
    assert all(point["rx"] == 0 and point["tx"] == 0 for point in body["monthly"])


# ------------------------------------------------------------------ delete


def test_delete_removes_the_peer_its_file_its_row_and_its_counters(api, server_conf, conf_dir):
    """Every trace of the client on disk, plus the row only the panel has."""
    created = create(api, "phone", quotaBytes=1024)
    public_key = created["publicKey"]
    traffic.write_db({public_key: Counters(10, 20, 10, 20)})
    assert public_key in traffic.read_db()

    assert api.delete(api_url("clients/phone")).status_code == 204

    text = server_conf.read_text(encoding="utf-8")
    assert public_key not in text
    assert "# Client = phone" not in text
    assert not (conf_dir / "clients" / "phone.conf").exists()
    assert not ClientMeta.objects.filter(public_key=public_key).exists()
    assert public_key not in traffic.read_db()

    assert api.get(api_url("clients/phone")).status_code == 404


def test_delete_leaves_the_other_clients_alone(api, server_conf):
    create(api, "phone")
    keeper = create(api, "laptop")
    traffic.write_db({keeper["publicKey"]: Counters(1, 2, 3, 4)})

    api.delete(api_url("clients/phone"))

    assert peer_named(server_conf, "laptop").public_key == keeper["publicKey"]
    assert ClientMeta.objects.filter(public_key=keeper["publicKey"]).exists()
    assert traffic.read_db()[keeper["publicKey"]] == Counters(1, 2, 3, 4)


# -------------------------------------------------------- remove the expired


def test_remove_expired_takes_the_past_dates_and_nothing_else(api, server_conf, conf_dir):
    """One sweep of everything whose moment has gone, leaving a client that
    expires later and a client that never expires exactly where they were."""
    now = timezone.now()
    gone = create(api, "old-phone", expiresAt=(now - timedelta(days=2)).isoformat())
    later = create(api, "laptop", expiresAt=(now + timedelta(days=30)).isoformat())
    forever = create(api, "router")
    traffic.write_db(
        {gone["publicKey"]: Counters(10, 20, 10, 20), later["publicKey"]: Counters(1, 2, 1, 2)}
    )

    response = api.post(api_url("clients/remove-expired"))
    assert response.status_code == 200
    assert response.json() == {"removed": ["old-phone"], "count": 1}

    text = server_conf.read_text(encoding="utf-8")
    assert gone["publicKey"] not in text
    assert not (conf_dir / "clients" / "old-phone.conf").exists()
    assert not ClientMeta.objects.filter(public_key=gone["publicKey"]).exists()
    assert gone["publicKey"] not in traffic.read_db()

    assert peer_named(server_conf, "laptop").public_key == later["publicKey"]
    assert peer_named(server_conf, "router").public_key == forever["publicKey"]
    assert ClientMeta.objects.filter(public_key=later["publicKey"]).exists()
    assert traffic.read_db()[later["publicKey"]] == Counters(1, 2, 1, 2)


def test_a_client_switched_off_by_hand_says_so_and_is_still_swept_when_it_lapses(api, server_conf):
    """The status word says how this client was switched off; the sweep goes by
    its date. They describe the same client and they do not have to agree.

    A client an admin disabled by hand reports "disabled" for as long as it is
    off, whatever its date does afterwards, because that word is the answer to
    "why did this stop working" and the date has a column of its own. The sweep
    reads the date and takes this client regardless.

    Which is why nothing in the browser may count expired clients by that word.
    Doing so counted none of these, so the button that removes them stayed hidden
    and the expired filter came back empty, on a list that was drawing a red
    Expired chip on the very rows in question.
    """
    gone = create(api, "old-phone", expiresAt=(timezone.now() - timedelta(days=2)).isoformat())
    api.put(api_url("clients/old-phone"), {"enabled": False}, format="json")

    row = next(r for r in api.get(api_url("clients")).json()["clients"] if r["name"] == "old-phone")
    assert row["status"] == "disabled"
    assert row["disabledReason"] == "manual"
    assert row["expiresAt"] is not None

    assert api.post(api_url("clients/remove-expired")).json() == {
        "removed": ["old-phone"],
        "count": 1,
    }
    assert gone["publicKey"] not in server_conf.read_text(encoding="utf-8")


def test_a_client_stopped_by_its_data_limit_says_so_even_once_it_lapses(api, server_conf):
    """Quota is a reason the admin can act on by raising the limit, so it stays
    the word on a client whose date has also gone."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(disabled_reason="quota")

    row = next(r for r in api.get(api_url("clients")).json()["clients"] if r["name"] == "phone")
    assert row["status"] == "quota"


def test_remove_expired_goes_by_the_date_and_not_by_the_marker(api, server_conf):
    """`disabled_reason` outlives the reason for it: the collector writes
    "expired" when it switches a client off and only clears it on its next pass,
    so an admin who has just moved the date forward has a client that still
    carries the marker. Deleting that client would be deleting one whose expiry
    is in the future."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(hours=1)).isoformat())
    api.put(
        api_url("clients/phone"),
        {"expiresAt": (timezone.now() + timedelta(days=7)).isoformat()},
        format="json",
    )
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(disabled_reason="expired")

    assert api.post(api_url("clients/remove-expired")).json() == {"removed": [], "count": 0}
    assert peer_named(server_conf, "phone").public_key == created["publicKey"]
    assert ClientMeta.objects.filter(public_key=created["publicKey"]).exists()


def test_the_sweep_leaves_a_lapsed_client_that_was_switched_back_on(api, server_conf):
    """The sweep and the collector have to honour the same decision.

    Keeping a client working in one of them while the other deletes it outright
    would be the worse half of both behaviours - and it is the same admin, the
    same client and the same date in each case. The client is still expired,
    still shown as expired, and still deletable on its own, which is where
    removing one somebody is deliberately keeping ought to happen.
    """
    lapsed = (timezone.now() - timedelta(days=1)).isoformat()
    kept = create(api, "kept", expiresAt=lapsed)
    gone = create(api, "gone", expiresAt=lapsed)
    api.put(api_url("clients/kept"), {"enabled": False}, format="json")
    api.put(api_url("clients/kept"), {"enabled": True}, format="json")

    assert api.post(api_url("clients/remove-expired")).json() == {"removed": ["gone"], "count": 1}

    assert peer_named(server_conf, "kept").public_key == kept["publicKey"]
    assert ClientMeta.objects.filter(public_key=kept["publicKey"]).exists()
    assert gone["publicKey"] not in server_conf.read_text(encoding="utf-8")


def test_remove_expired_with_nothing_expired_leaves_the_config_alone(api, server_conf):
    create(api, "phone", expiresAt=(timezone.now() + timedelta(days=1)).isoformat())
    before = server_conf.read_text(encoding="utf-8")

    assert api.post(api_url("clients/remove-expired")).json() == {"removed": [], "count": 0}
    assert server_conf.read_text(encoding="utf-8") == before


# -------------------------------------------------------------- remove in bulk


def bulk(client: APIClient, **options: bool) -> dict:
    """POST the bulk removal and hand back its answer."""
    response = client.post(api_url("clients/bulk-remove"), options, format="json")
    assert response.status_code == 200, response.content
    return response.json()


def names_on_server(client: APIClient) -> list[str]:
    """Every client still on the server, in the order it lists them."""
    body = client.get(api_url("clients"), {"pageSize": 500}).json()
    return [row["name"] for row in body["clients"]]


@pytest.fixture
def mixed(api):
    """One client of every kind a bulk removal can be asked about.

    Named for what they are rather than for devices, because every assertion
    below is about which categories went and which stayed:

    * `lapsed`   - its date has passed, and nothing else is wrong with it.
    * `spent`    - over its data limit and still switched on, which is the state
                   between crossing the limit and the collector's next pass.
    * `switched` - switched off by hand, with a date that has not passed.
    * `keeper`   - working, inside its limit, no date at all.
    """
    lapsed = create(api, "lapsed", expiresAt=(timezone.now() - timedelta(days=2)).isoformat())
    spent = create(api, "spent", quotaBytes=1000)
    switched = create(api, "switched")
    keeper = create(api, "keeper")
    traffic.write_db({spent["publicKey"]: Counters(600, 600, 600, 600)})
    api.put(api_url("clients/switched"), {"enabled": False}, format="json")
    return {"lapsed": lapsed, "spent": spent, "switched": switched, "keeper": keeper}


def test_bulk_remove_takes_only_the_expired_when_that_is_all_it_is_asked_for(api, mixed):
    """The `expired` half on its own is the old sweep, and reads the date the
    same way: the client that is merely switched off keeps its place."""
    assert bulk(api, expired=True) == {"removed": ["lapsed"], "count": 1}
    assert names_on_server(api) == ["client1", "spent", "switched", "keeper"]


def test_bulk_remove_takes_every_stopped_client_however_it_was_stopped(api, mixed):
    """The word the table uses covers both ways a client stops: an admin
    switching it off, and its own data limit running out.

    The second is the one worth a test. A client that has crossed its limit is
    still switched on until the collector reaches it, so a sweep reading only
    the config's "# Disabled" marker would leave behind exactly the row the
    operator was looking at - the one the table had already stopped calling
    online - while reporting success.
    """
    assert bulk(api, disabled=True) == {"removed": ["spent", "switched"], "count": 2}
    assert names_on_server(api) == ["client1", "lapsed", "keeper"]


def test_bulk_remove_with_both_options_takes_the_union_and_counts_each_client_once(api, mixed):
    """A client can be expired and switched off at once, which is the ordinary
    state of one the collector has enforced against - so the two selections
    overlap and the answer must not count that client twice."""
    api.put(api_url("clients/lapsed"), {"enabled": False}, format="json")

    assert bulk(api, expired=True, disabled=True) == {
        "removed": ["lapsed", "spent", "switched"],
        "count": 3,
    }
    assert names_on_server(api) == ["client1", "keeper"]


def test_bulk_remove_deletes_the_files_and_rows_that_belong_to_the_clients_it_takes(
    api, mixed, server_conf, conf_dir
):
    """The same removal the single delete performs: peer, config file, metadata
    row and traffic counters, for every client in the sweep."""
    assert bulk(api, expired=True, disabled=True)["count"] == 3

    text = server_conf.read_text(encoding="utf-8")
    for kind in ("lapsed", "spent", "switched"):
        assert mixed[kind]["publicKey"] not in text
        assert not (conf_dir / "clients" / f"{kind}.conf").exists()
        assert not ClientMeta.objects.filter(public_key=mixed[kind]["publicKey"]).exists()
        assert mixed[kind]["publicKey"] not in traffic.read_db()

    assert peer_named(server_conf, "keeper").public_key == mixed["keeper"]["publicKey"]
    assert ClientMeta.objects.filter(public_key=mixed["keeper"]["publicKey"]).exists()


def test_bulk_remove_keeps_a_lapsed_client_that_was_switched_back_on(api, server_conf):
    """The same reprieve `clients/remove-expired` honours, for the same reason:
    an admin who switched a lapsed client back on has answered for that date, and
    a sweep of the expired ones must not undo it behind their back.

    Only under `expired`, though. Nothing about that decision makes the client
    disabled, so a sweep of the stopped clients has no opinion on it either way -
    it is switched on and inside its limit.
    """
    kept = create(api, "kept", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/kept"), {"enabled": False}, format="json")
    api.put(api_url("clients/kept"), {"enabled": True}, format="json")

    assert bulk(api, expired=True, disabled=True) == {"removed": [], "count": 0}
    assert peer_named(server_conf, "kept").public_key == kept["publicKey"]


def test_bulk_remove_with_no_option_chosen_is_refused_rather_than_answered_with_nothing(api, mixed):
    """Removing nothing on purpose and a flag that never arrived are different
    mistakes, and a 200 saying nothing was removed answers both the same way."""
    response = api.post(api_url("clients/bulk-remove"), {}, format="json")

    assert response.status_code == 400
    assert "expired" in response.json()["detail"]
    assert names_on_server(api) == ["client1", "lapsed", "spent", "switched", "keeper"]


def test_bulk_remove_leaves_the_config_alone_when_nothing_matches(api, server_conf):
    create(api, "phone", expiresAt=(timezone.now() + timedelta(days=1)).isoformat())
    before = server_conf.read_text(encoding="utf-8")

    assert bulk(api, expired=True, disabled=True) == {"removed": [], "count": 0}
    assert server_conf.read_text(encoding="utf-8") == before


def test_the_counts_on_the_list_are_what_the_bulk_removal_actually_takes(api, mixed):
    """The number on the button and the number of clients that go have to be the
    same number, and they are worked out in two different places: the counts are
    a query over the client index, the sweep is a pass over the config file
    holding the lock. This is the test that holds the two together.

    The union is the interesting one. `expiredCount` and `disabledCount` overlap
    once the collector has switched a lapsed client off, so a panel that added
    them up would promise to remove more clients than the server has.
    """
    api.put(api_url("clients/lapsed"), {"enabled": False}, format="json")
    body = api.get(api_url("clients")).json()

    assert (body["expiredCount"], body["disabledCount"]) == (1, 3)
    assert body["expiredOrDisabledCount"] == 3

    assert bulk(api, expired=True)["count"] == body["expiredCount"]
    assert bulk(api, disabled=True)["count"] == body["disabledCount"] - 1  # the lapsed one has gone


def test_the_disabled_count_is_over_every_client_and_not_the_page(api, mixed):
    """Counted over the server like the expired one beside it: the sweep removes
    the stopped clients a search is hiding as readily as the ones on screen."""
    body = api.get(api_url("clients"), {"pageSize": 1, "q": "keeper"}).json()

    assert len(body["clients"]) == 1
    assert (body["disabledCount"], body["expiredOrDisabledCount"]) == (2, 3)


# ------------------------------------------------------------------ export


def test_export_zip_holds_one_entry_per_client(api, conf_dir):
    """One config per client that has one.

    The fixture's `client1` is a peer with no rendered config file - the state a
    server is left in when a client was removed by hand - and it is skipped
    rather than exported as an empty file.
    """
    for name in ("phone", "laptop", "tablet"):
        create(api, name)

    response = api.get(api_url("clients/export.zip"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["laptop.conf", "phone.conf", "tablet.conf"]
    assert archive.testzip() is None
    for name in ("phone", "laptop", "tablet"):
        expected = (conf_dir / "clients" / f"{name}.conf").read_text(encoding="utf-8")
        assert archive.read(f"{name}.conf").decode("utf-8") == expected


def test_export_zip_with_nothing_to_export_says_so(api):
    """The fixture's two peers have no config files, so there is nothing to zip."""
    response = api.get(api_url("clients/export.zip"))
    assert response.status_code == 404
    assert "resync" in response.json()["detail"]


# ---------------------------------------------- the address configs go out with


@pytest.fixture
def domain_configs(tmp_path, make_certificate):
    """Set the panel to hand out its certificate's domain. Yields the name to expect.

    The three ways a config leaves the panel are asserted against this one
    setting, because the whole point of doing the substitution on the way out is
    that a phone that scans and a laptop that downloads cannot end up on
    different addresses.
    """
    cert, key, _ = make_certificate(tmp_path, "vpn.example.com")
    settings_store.set_many(
        {
            "tlsCertPath": str(cert),
            "tlsKeyPath": str(key),
            "configEndpointMode": "domain",
        }
    )
    yield "vpn.example.com"
    # The store keeps values for a few seconds so a 2 s dashboard poll does not
    # hit SQLite each time, and that cache outlives the per-test rollback.
    settings_store.invalidate()


def test_the_download_names_the_certificate_domain(api, domain_configs):
    create(api, "phone")

    text = api.get(api_url("clients/phone/config")).content.decode("utf-8")

    values = parse_client_conf(text)
    # Only the host: the port is the one the server actually listens on and is
    # no business of the panel's certificate.
    assert values["Endpoint"] == f"{domain_configs}:41234"


def test_the_file_on_disk_keeps_saying_what_the_server_configured(api, conf_dir, domain_configs):
    """The substitution is a view of the file, not an edit to it.

    An admin reads these files over SSH and sees what the server was configured
    with; a panel setting quietly rewriting them would make the file and the
    server disagree about its own address, and the next resync would put the IP
    back with no explanation.
    """
    create(api, "phone")
    api.get(api_url("clients/phone/config"))

    on_disk = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")
    assert parse_client_conf(on_disk)["Endpoint"] == "203.0.113.10:41234"


def test_the_qr_code_is_the_config_the_download_hands_over(api, domain_configs):
    """Rendered from the same text, so the two cannot drift apart."""
    create(api, "phone")
    text = api.get(api_url("clients/phone/config")).content.decode("utf-8")

    response = api.get(api_url("clients/phone/qr"))

    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)
    assert response.content == _qr_png(text)


def test_the_export_archive_carries_the_domain_too(api, domain_configs):
    for name in ("phone", "laptop"):
        create(api, name)

    response = api.get(api_url("clients/export.zip"))

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    for name in ("phone", "laptop"):
        values = parse_client_conf(archive.read(f"{name}.conf").decode("utf-8"))
        assert values["Endpoint"] == f"{domain_configs}:41234"


def test_a_pinned_host_is_handed_out_instead_of_the_certificate_name(api, domain_configs):
    """A certificate for the panel is not always the name the tunnel answers on."""
    settings_store.set_many({"configEndpointHost": "gateway.example.com"})
    create(api, "phone")

    text = api.get(api_url("clients/phone/config")).content.decode("utf-8")

    assert parse_client_conf(text)["Endpoint"] == "gateway.example.com:41234"


def test_a_certificate_that_went_away_does_not_take_the_download_with_it(api, domain_configs):
    """Renewals and reverse proxies both end with the path pointing at nothing.

    Refusing to render a config at that point would take the panel's own
    downloads out over a preference, so the address the file already carries is
    what goes over the wire.
    """
    settings_store.set_many({"tlsCertPath": "", "tlsKeyPath": ""})
    create(api, "phone")

    text = api.get(api_url("clients/phone/config")).content.decode("utf-8")

    assert parse_client_conf(text)["Endpoint"] == "203.0.113.10:41234"


def test_a_multi_field_edit_takes_the_lock_once(api, server_conf, monkeypatch):
    """Each store call takes the lock for itself, so an edit that changes several
    things used to be several windows with gaps between them. Anything landing
    in a gap left half the edit applied and answered the admin with a 404 about
    a name that no longer existed."""
    create(api, "phone")
    acquisitions = _count_config_lock(monkeypatch)

    response = api.put(
        api_url("clients/phone"),
        {"name": "work-phone", "enabled": False, "allowedIps": "10.13.13.0/24", "note": "x"},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert len(acquisitions) == 1, acquisitions
    body = response.json()
    assert body["name"] == "work-phone"
    assert body["enabled"] is False
    assert body["allowedIps"] == "10.13.13.0/24"


def test_a_delete_takes_the_lock_once(api, server_conf, monkeypatch):
    create(api, "phone")
    acquisitions = _count_config_lock(monkeypatch)

    assert api.delete(api_url("clients/phone")).status_code == 204
    assert len(acquisitions) == 1, acquisitions


def test_a_busy_config_lock_is_a_503_the_caller_may_simply_repeat(api, server_conf, monkeypatch):
    """The one failure a mutation is allowed to answer by trying again.

    The lock is taken before anything is written, so a request that waits out
    its budget there has touched no file - which is what lets the browser repeat
    it without asking whether the operation is idempotent, and `reset-keys` is
    the one that would otherwise issue a second keypair.

    What tells the two 503s apart is the absence of a code. A missing VPN
    install answers with the same status and `not_configured`, and waiting will
    not produce a server config; adding a code here would quietly stop the
    repeat, so this asserts there is none.
    """

    def busy(fd, path, timeout):
        raise LockTimeout(f"timed out after {timeout:g}s waiting for the lock at {path}")

    monkeypatch.setattr(lock, "_acquire", busy)

    response = api.post(api_url("clients"), {"name": "phone"}, format="json")

    assert response.status_code == 503, response.content
    assert response["Retry-After"] == "5"
    assert "code" not in response.json()
    assert not (paths.client_dir() / "phone.conf").exists()


def _count_config_lock(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every acquisition of the config lock, and only that one.

    Counted by path, because the flock machinery is shared: the client index
    takes a lock of its own while it rebuilds, on a file in the run directory,
    and counting acquisitions of any lock would make these tests about a
    coincidence rather than about the config lock being taken once per edit.
    """
    acquisitions: list[str] = []
    original = lock._acquire  # only reached when the lock is not already held

    def record(fd, path, timeout):
        if path == paths.lock_file():
            acquisitions.append(str(path))
        return original(fd, path, timeout)

    monkeypatch.setattr(lock, "_acquire", record)
    return acquisitions


def _enforced_off(api, name: str, reason: str, used: int) -> dict:
    """A client the collector has switched off, with `used` bytes against its name."""
    created = create(api, name, quotaBytes=used)
    api.put(api_url(f"clients/{name}"), {"enabled": False}, format="json")
    traffic.write_db({created["publicKey"]: Counters(used, 0, used, 0)})
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(disabled_reason=reason)
    return created


def test_raising_a_quota_switches_the_client_back_on(api, server_conf):
    """The collector would have done this within a minute, and a minute is long
    enough that the admin who just raised the limit reads it as a broken button."""
    _enforced_off(api, "phone", "quota", 1000)

    body = api.put(api_url("clients/phone"), {"quotaBytes": 5000}, format="json").json()

    assert body["enabled"] is True
    assert body["disabledReason"] == ""
    assert peer_named(server_conf, "phone").disabled_at is None


def test_a_quota_raised_but_not_past_the_usage_leaves_the_client_off(api, server_conf):
    """Halfway is still over: the verdict is about the limit the client will have,
    not about the admin having touched it."""
    _enforced_off(api, "phone", "quota", 1000)

    body = api.put(api_url("clients/phone"), {"quotaBytes": 900}, format="json").json()

    assert body["enabled"] is False
    assert peer_named(server_conf, "phone").disabled_at is not None


def test_moving_an_expiry_forward_switches_the_client_back_on(api, server_conf):
    """The same rule, applied to the other half of the verdict."""
    created = create(api, "phone", expiresAt=(timezone.now() - timedelta(days=1)).isoformat())
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(disabled_reason="expired")

    body = api.put(
        api_url("clients/phone"),
        {"expiresAt": (timezone.now() + timedelta(days=7)).isoformat()},
        format="json",
    ).json()

    assert body["enabled"] is True
    assert peer_named(server_conf, "phone").disabled_at is None


def test_raising_a_quota_leaves_a_client_that_has_also_lapsed_switched_off(api):
    """Expiry is the first half of the verdict, so a client past its date stays off
    however much quota it is given."""
    created = create(
        api,
        "phone",
        quotaBytes=1000,
        expiresAt=(timezone.now() - timedelta(days=1)).isoformat(),
    )
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")
    traffic.write_db({created["publicKey"]: Counters(1000, 0, 1000, 0)})
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(
        disabled_reason="quota", expiry_override_at=None
    )

    assert (
        api.put(api_url("clients/phone"), {"quotaBytes": 5000}, format="json").json()["enabled"]
        is False
    )


def test_raising_a_quota_leaves_a_client_switched_off_by_hand_alone(api):
    """Only what the panel enforced can be reversed by the panel. A peer disabled
    by hand is somebody's decision, and quota is not what is keeping it off."""
    _enforced_off(api, "phone", "manual", 1000)

    assert (
        api.put(api_url("clients/phone"), {"quotaBytes": 5000}, format="json").json()["enabled"]
        is False
    )


def test_an_explicit_switch_still_wins_over_the_verdict(api):
    """A request that says `enabled` is the admin answering the question directly,
    and it must not be second-guessed by the limits it arrives beside."""
    _enforced_off(api, "phone", "quota", 1000)

    body = api.put(
        api_url("clients/phone"),
        {"enabled": False, "quotaBytes": 5000},
        format="json",
    ).json()

    assert body["enabled"] is False


def _spent(api, name: str, used: int) -> dict:
    """A client that is on, has no limit, and has already transferred `used` bytes."""
    created = create(api, name)
    traffic.write_db({created["publicKey"]: Counters(used, 0, used, 0)})
    return created


def test_lowering_a_quota_below_the_usage_switches_the_client_off(api, server_conf):
    """The other direction of the same verdict, and the one that used to wait.

    An admin who sets a limit a client has already outrun is switching that
    client off, and until the collector's next pass they were the only person
    who did not know it: the status column reads the usage, so the panel drew
    the client as stopped while the kernel went on carrying its key.
    """
    _spent(api, "phone", 10_000)

    body = api.put(api_url("clients/phone"), {"quotaBytes": 4000}, format="json").json()

    assert body["enabled"] is False
    assert body["disabledReason"] == "quota"
    assert peer_named(server_conf, "phone").disabled_at is not None


def test_a_client_stopped_by_a_lowered_quota_comes_back_when_it_is_raised(api, server_conf):
    """The reason has to be the verdict rather than "manual", or this edit stops
    working: only a client the panel enforced against is eligible to be freed,
    and a lowered limit recorded as somebody's decision would be a client that
    could never be given its quota back."""
    _spent(api, "phone", 10_000)
    api.put(api_url("clients/phone"), {"quotaBytes": 4000}, format="json")

    body = api.put(api_url("clients/phone"), {"quotaBytes": 20_000}, format="json").json()

    assert body["enabled"] is True
    assert body["disabledReason"] == ""
    assert peer_named(server_conf, "phone").disabled_at is None


def test_lowering_a_quota_the_client_is_still_inside_leaves_it_on(api, server_conf):
    """Lowering a limit is not switching a client off. The verdict is about where
    the usage falls against the new number, not about the number having moved."""
    _spent(api, "phone", 1000)

    body = api.put(api_url("clients/phone"), {"quotaBytes": 4000}, format="json").json()

    assert body["enabled"] is True
    assert peer_named(server_conf, "phone").disabled_at is None


def test_moving_an_expiry_into_the_past_switches_the_client_off(api, server_conf):
    """The expiry half of the same direction. Moving the date is a new decision,
    so whatever override the client carried is cleared and the date is enforced."""
    create(api, "phone")

    body = api.put(
        api_url("clients/phone"),
        {"expiresAt": (timezone.now() - timedelta(days=1)).isoformat()},
        format="json",
    ).json()

    assert body["enabled"] is False
    assert body["disabledReason"] == "expired"
    assert peer_named(server_conf, "phone").disabled_at is not None


def test_an_explicit_switch_on_still_wins_over_a_lowered_quota(api, server_conf):
    """The one combination left for the collector, and deliberately so.

    `enabled` in the request is the admin answering directly, and that is read
    where it arrives rather than second-guessed here - in this direction as in
    the other. The collector still switches the client off on its next pass,
    because a quota is a measurement and nothing has been written down that
    forgives it; what this proves is only that the request does not.
    """
    _spent(api, "phone", 10_000)
    api.put(api_url("clients/phone"), {"enabled": False}, format="json")

    body = api.put(
        api_url("clients/phone"),
        {"enabled": True, "quotaBytes": 4000},
        format="json",
    ).json()

    assert body["enabled"] is True
    assert peer_named(server_conf, "phone").disabled_at is None


def test_an_edit_that_touches_neither_limit_switches_nothing_off(api, server_conf):
    """A client already over a limit the collector has not reached yet must not be
    switched off by an edit to its note. The verdict is applied when its own
    inputs change; everything else is the collector's to notice."""
    created = create(api, "phone", quotaBytes=4000)
    traffic.write_db({created["publicKey"]: Counters(10_000, 0, 10_000, 0)})

    body = api.put(api_url("clients/phone"), {"note": "desk"}, format="json").json()

    assert body["enabled"] is True
    assert peer_named(server_conf, "phone").disabled_at is None
