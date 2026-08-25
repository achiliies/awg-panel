"""The copy of the config the client list reads, and what keeps it honest.

Everything the panel shows about a client now comes out of ClientIndex rather
than off the disk, so the only question that matters about this table is whether
it can ever quietly disagree with the files. Almost every test here is a version
of that question: change a file the way one of its real writers changes it, ask
the panel something, and check the answer moved.

The writers are not interchangeable and that is the point of doing each
separately. The panel replaces the server config through a temp file and a
rename, which lands a new inode. An edit made outside it can append in place
with `cat >>`, which keeps the inode and moves the size. A client config is replaced
by rename on both sides. Each of those trips a different part of the stamp, and a
scheme that caught two of the three would look completely correct until the day
somebody used the CLI.
"""

import base64
import itertools
import json
import os
import threading
import time
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.clients import index, merge
from apps.clients.models import ClientIndex, ClientIndexState, ClientMeta
from awg import lock, store
from awg.errors import NotConfigured
from awg.paths import index_lock_file, live_state_file
from awg.traffic import Counters

pytestmark = pytest.mark.django_db

# The named peer in tests/fixtures/server.conf, and the unnamed disabled one
# beside it. The second is why several counts here are "one" rather than "two":
# it holds an address and has no name, so it can never be a row.
FIXTURE_CLIENT = "client1"
FIXTURE_KEY = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="


def api_url(path: str) -> str:
    return f"{settings.BASE_PATH}api/v1/{path}"


@pytest.fixture
def api(server_conf) -> APIClient:
    """A signed-in API client against the fixture server config."""
    user = get_user_model().objects.create_user("admin")
    client = APIClient()
    client.force_login(user)
    return client


def names(payload: dict) -> list[str]:
    return [row["name"] for row in payload["clients"]]


def listing(api: APIClient, **params: object) -> dict:
    response = api.get(api_url("clients"), params)
    assert response.status_code == 200, response.content
    return response.json()


def add(api: APIClient, name: str, **fields: object) -> dict:
    """Create a client through the panel, which writes its config file too."""
    response = api.post(api_url("clients"), {"name": name, **fields}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def rows() -> dict[str, ClientIndex]:
    """The index as it stands, by client name, without refreshing it."""
    return {row.name: row for row in ClientIndex.objects.select_related("meta")}


def snapshot() -> dict[str, tuple]:
    """Every indexed value, by public key, for comparing two builds of the table."""
    return {
        row[0]: row[1:]
        for row in ClientIndex.objects.values_list("meta__public_key", *index.FIELDS)
    }


# The fixture's clients were created in August 2026, and the list is ordered
# oldest first, so a peer appended by a test has to carry a later date to land
# where an appended peer belongs - at the bottom, in the order it was added.
_added = itertools.count(1)


def append_peer(server_conf, name: str, key: str, ip: str, created: str | None = None) -> None:
    """Add a peer the way an append outside the panel does: in place, same inode.

    Not through the store, deliberately. The panel rewrites the whole file and
    lands a new inode; bash appends and keeps it. This is the writer that a stamp
    watching only the inode would miss entirely, so the tests that care about
    being told the truth about the CLI have to write the file the way it does.
    """
    stamped = created or f"2026-09-01T00:00:{next(_added):02d}Z"
    with open(server_conf, "a", encoding="utf-8") as handle:
        handle.write(
            f"\n[Peer]\n# Client = {name}\n# Created = {stamped}\n"
            f"PublicKey = {key}\nAllowedIPs = {ip}/32\n"
        )


def key_for(seed: int) -> str:
    """A distinct, well-formed public key. The value is never parsed, only matched."""
    return base64.b64encode(seed.to_bytes(4, "big") + bytes(28)).decode()


# ----------------------------------------------------------- noticing a change


def test_first_read_builds_the_index(api, server_conf):
    """Nothing is built by the migration: the files are the source and they are already there."""
    assert not ClientIndex.objects.exists()

    assert names(listing(api)) == [FIXTURE_CLIENT]

    assert ClientIndex.objects.count() == 1
    assert ClientIndexState.objects.get().conf_mtime_ns == os.stat(server_conf).st_mtime_ns


def test_a_peer_appended_outside_the_panel_shows_up(api, server_conf):
    """`cat >>` does not change the inode, but it does move the size."""
    assert names(listing(api)) == [FIXTURE_CLIENT]
    before = os.stat(server_conf).st_ino

    append_peer(server_conf, "from-ssh", key_for(1), "10.13.13.9")

    assert names(listing(api)) == [FIXTURE_CLIENT, "from-ssh"]
    assert os.stat(server_conf).st_ino == before, "the append should not have replaced the file"


def test_a_peer_removed_outside_the_panel_disappears(api, server_conf):
    """The other half: a rewrite through a temp file and a rename, so the inode moves."""
    append_peer(server_conf, "from-ssh", key_for(1), "10.13.13.9")
    assert "from-ssh" in names(listing(api))

    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(text.split("\n[Peer]\n# Client = from-ssh")[0] + "\n", encoding="utf-8")

    assert names(listing(api)) == [FIXTURE_CLIENT]
    assert not ClientIndex.objects.filter(name="from-ssh").exists()


def test_a_disable_marker_added_by_hand_is_noticed(api, server_conf):
    """A hand edit that changes the file's length and nothing else."""
    assert listing(api)["clients"][0]["enabled"] is True

    text = server_conf.read_text(encoding="utf-8")
    marked = text.replace(
        f"# Client = {FIXTURE_CLIENT}\n",
        f"# Client = {FIXTURE_CLIENT}\n# Disabled = 2026-08-05T09:30:00Z\n",
    )
    server_conf.write_text(marked, encoding="utf-8")

    row = listing(api)["clients"][0]
    assert row["enabled"] is False
    assert row["status"] == "disabled"


def test_a_rewritten_client_config_is_noticed(api, server_conf, conf_dir):
    """AllowedIPs lives in the client's own file, which the server config knows nothing about.

    The server config is untouched by this edit - both values it changes are
    client-side - so the only thing that can catch it is the clients directory's
    own timestamp, which moves because every writer of these files on both sides
    replaces them by rename rather than writing through them.
    """
    add(api, "phone", allowedIps="0.0.0.0/0")
    listing(api)
    before = os.stat(server_conf)

    store.update_client("phone", allowed_ips="10.13.13.0/24")

    after = os.stat(server_conf)
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
    ), "this test is pointless unless awg0.conf was left alone"

    row = next(r for r in listing(api)["clients"] if r["name"] == "phone")
    assert row["allowedIps"] == "10.13.13.0/24"


def test_nothing_is_rebuilt_when_nothing_moved(api, server_conf, monkeypatch):
    """The whole point: a request between two edits must not read the config at all."""
    listing(api)

    def fail(*args, **kwargs):
        raise AssertionError("the config was re-read when no file had changed")

    monkeypatch.setattr(store, "scan_server", fail)
    assert names(listing(api)) == [FIXTURE_CLIENT]


def test_traffic_moving_refreshes_counters_without_a_full_rebuild(
    api, server_conf, conf_dir, monkeypatch
):
    """traffic.db is rewritten every ten seconds. That must not cost a config parse.

    The cheap half of staying current. If this ever regressed to a full rebuild
    the panel would parse the whole server config six times a minute forever,
    which is more work than the per-request reading the index replaced.
    """
    listing(api)
    (conf_dir / "traffic.db").write_text(f"{FIXTURE_KEY} 4096 2048 4096 2048\n", encoding="utf-8")

    def fail():
        raise AssertionError("a traffic flush must not trigger a config parse")

    monkeypatch.setattr(store, "scan_server", fail)
    row = listing(api)["clients"][0]

    assert (row["rxBytes"], row["txBytes"]) == (4096, 2048)


# --------------------------------------------------------------- staying right


def test_an_incremental_rebuild_matches_one_from_scratch(api, server_conf, conf_dir):
    """The rebuild only writes rows that differ. It must reach the same table anyway.

    This is the test that earns that optimisation. Everything is recomputed from
    the files every time; only the writing is skipped for rows that already
    match, and if the comparison were wrong the table would silently keep an old
    value. So the same sequence of edits is replayed against an emptied table and
    the two results are compared column by column.
    """
    add(api, "second")
    listing(api)
    append_peer(server_conf, "third", key_for(2), "10.13.13.20")
    listing(api)
    store.set_client_enabled(FIXTURE_CLIENT, False)
    listing(api)
    store.update_client("second", allowed_ips="10.13.13.0/24")
    listing(api)
    store.rename_client("second", "renamed")
    listing(api)

    incremental = snapshot()

    ClientIndex.objects.all().delete()
    ClientIndexState.objects.all().delete()
    index.current()

    assert snapshot() == incremental


def test_the_index_can_be_dropped_at_any_moment(api, server_conf):
    """Both tables are disposable. Losing them costs one rebuild and nothing else."""
    first = listing(api)

    ClientIndex.objects.all().delete()
    ClientIndexState.objects.all().delete()

    assert listing(api) == first


def test_an_edit_that_changes_neither_size_nor_inode_is_still_caught(api, server_conf):
    """The case the modification time is in the stamp for.

    A hand edit that renames a client to another name of the same length, written
    in place, moves neither the size nor the inode. Only the nanosecond
    modification time separates the two files, which is why all three are
    compared rather than whichever one seemed sufficient.
    """
    listing(api)
    before = os.stat(server_conf)
    text = server_conf.read_text(encoding="utf-8")
    replaced = text.replace(f"# Client = {FIXTURE_CLIENT}", "# Client = restore")
    assert len(replaced) == len(text), "this test is pointless unless the size is unchanged"
    server_conf.write_text(replaced, encoding="utf-8")

    after = os.stat(server_conf)
    assert (after.st_size, after.st_ino) == (before.st_size, before.st_ino)
    assert names(listing(api)) == ["restore"]


def test_a_change_that_keeps_the_timestamp_is_caught_by_the_size(api, server_conf):
    """The case the size is in the stamp for.

    Nanosecond modification times settle almost everything on their own, but they
    are not guaranteed: a filesystem with one-second resolution cannot tell two
    edits in the same second apart, and adding two clients in a second is an
    ordinary thing for a script to do. Here that is forced by putting the old
    timestamp back on a file that grew, which leaves the size as the only
    difference there is.
    """
    listing(api)
    original = os.stat(server_conf)

    append_peer(server_conf, "sneaky", key_for(70), "10.13.13.80")
    os.utime(server_conf, ns=(original.st_atime_ns, original.st_mtime_ns))

    after = os.stat(server_conf)
    assert (after.st_mtime_ns, after.st_ino) == (original.st_mtime_ns, original.st_ino)
    assert after.st_size != original.st_size

    assert "sneaky" in names(listing(api))


def test_a_restore_that_preserves_timestamps_is_still_caught(api, server_conf):
    """The case the inode is in the stamp for.

    `tar x` and `rsync -a` put a file back with the modification time it had when
    it was archived, which can be older than the one the index was built from -
    so the timestamp moves backwards, or does not move at all, while the contents
    change completely. What no restore can preserve is the inode: putting a file
    back means creating one.
    """
    listing(api)
    original = os.stat(server_conf)

    text = server_conf.read_text(encoding="utf-8")
    restored = server_conf.with_suffix(".restored")
    # Same length as the name it replaces, so neither the size nor the timestamp
    # can be what catches this. The inode is the only thing left.
    restored.write_text(
        text.replace(f"# Client = {FIXTURE_CLIENT}", "# Client = backup1"), encoding="utf-8"
    )
    os.replace(restored, server_conf)
    # What tar does last: put the archived timestamps back on what it extracted.
    os.utime(server_conf, ns=(original.st_atime_ns, original.st_mtime_ns))

    after = os.stat(server_conf)
    assert (after.st_mtime_ns, after.st_size) == (original.st_mtime_ns, original.st_size)
    assert after.st_ino != original.st_ino

    assert names(listing(api)) == ["backup1"]


def test_invalidate_forces_the_next_read_to_rebuild(api, server_conf):
    listing(api)
    ClientIndex.objects.all().delete()

    index.invalidate()

    assert names(listing(api)) == [FIXTURE_CLIENT]


# ------------------------------------------------------------- what it answers


def test_the_page_is_the_only_thing_built(api, server_conf):
    """Counts are over the whole server; rows are only the page asked for."""
    for i in range(6):
        append_peer(server_conf, f"peer{i}", key_for(10 + i), f"10.13.13.{30 + i}")

    payload = listing(api, pageSize=2, page=2)

    assert len(payload["clients"]) == 2
    assert payload["total"] == 7
    assert payload["totalAll"] == 7
    assert payload["page"] == 2


def test_a_page_costs_the_same_however_many_clients_there_are(api, server_conf):
    """The claim the whole change rests on, asserted rather than believed.

    A `select_related` quietly dropped from the page query would add one query
    per row on the page and nothing else would notice, so the count is taken
    against two servers an order of magnitude apart and required to be identical.
    """
    listing(api)
    with CaptureQueriesContext(connection) as small:
        listing(api)

    for i in range(40):
        append_peer(server_conf, f"bulk{i}", key_for(200 + i), f"10.13.{i}.5")
    listing(api)

    with CaptureQueriesContext(connection) as large:
        payload = listing(api)

    assert payload["totalAll"] == 41
    assert len(large) == len(small), f"{len(small)} query(s) for 1 client, {len(large)} for 41"


def test_search_reaches_the_fields_only_the_database_has(api, server_conf):
    """Note and email are ClientMeta's, joined to rather than copied into the index."""
    add(api, "phone", note="spare handset", email="owner@example.org")

    assert names(listing(api, q="spare")) == ["phone"]
    assert names(listing(api, q="handset")) == ["phone"]
    assert names(listing(api, q="owner@example")) == ["phone"]
    assert names(listing(api, q="nothing here")) == []


def test_an_edited_note_shows_without_any_file_changing(api, server_conf):
    """ClientMeta is joined to, never copied, so no stamp has to move for this."""
    add(api, "phone")
    listing(api)

    ClientMeta.objects.filter(name="phone").update(note="second thoughts")

    assert names(listing(api, q="second thoughts")) == ["phone"]


def test_sorting_by_name_is_case_insensitive_and_stable(api, server_conf):
    for name, seed, ip in (("Zulu", 20, "10.13.13.40"), ("alpha", 21, "10.13.13.41")):
        append_peer(server_conf, name, key_for(seed), ip)

    assert names(listing(api, sort="name")) == ["alpha", FIXTURE_CLIENT, "Zulu"]
    assert names(listing(api, sort="name", direction="desc")) == ["Zulu", FIXTURE_CLIENT, "alpha"]


def test_sorting_by_address_is_numeric(api, server_conf):
    append_peer(server_conf, "ten", key_for(30), "10.13.13.10")
    append_peer(server_conf, "two", key_for(31), "10.13.13.2")

    assert names(listing(api, sort="ip")) == [FIXTURE_CLIENT, "two", "ten"]


def test_clients_that_tie_are_ordered_by_when_they_were_added(api, server_conf):
    """Every sort but "created" ties on creation order, and that has to be the real one.

    Ties are the common case, not the corner: sort by usage on a server where
    most clients have never sent a byte and nearly all of them are equal. Without
    a tie-break the database is free to return them in whatever order it finds
    them, which changes between polls and makes the table appear to shuffle
    itself while nobody is touching it.

    The two peers here are appended in the opposite order to their dates, so
    "found in the table" and "added first" cannot be the same answer.
    """
    append_peer(
        server_conf, "later-added", key_for(60), "10.13.13.70", created="2026-09-01T00:00:00Z"
    )
    append_peer(
        server_conf, "older-client", key_for(61), "10.13.13.71", created="2026-01-01T00:00:00Z"
    )

    # Nobody has ever handshaken and nobody has used a byte, so all three tie on
    # both of these and creation order is the whole of the answer.
    expected = ["older-client", FIXTURE_CLIENT, "later-added"]
    assert names(listing(api, sort="usage")) == expected
    assert names(listing(api, sort="lastHandshake")) == expected


def test_reversing_the_default_order_reverses_it_completely(api, server_conf):
    """Descending by creation reverses ties too, because that order *is* the tie.

    Two clients added in the same second are ordered by where they sit in the
    config. Descending has to turn that round as well - it is one list read
    backwards, not a list sorted by a key that half of it shares.
    """
    same = "2026-09-01T00:00:00Z"
    append_peer(server_conf, "first-in-file", key_for(62), "10.13.13.72", created=same)
    append_peer(server_conf, "second-in-file", key_for(63), "10.13.13.73", created=same)

    assert names(listing(api)) == [FIXTURE_CLIENT, "first-in-file", "second-in-file"]
    assert names(listing(api, direction="desc")) == [
        "second-in-file",
        "first-in-file",
        FIXTURE_CLIENT,
    ]


def test_sorting_by_usage_takes_the_offsets_off_first(api, server_conf, conf_dir):
    """Usage is the counters less what a cleared counter recorded, and offsets are ClientMeta's."""
    append_peer(server_conf, "heavy", key_for(40), "10.13.13.50")
    (conf_dir / "traffic.db").write_text(
        f"{FIXTURE_KEY} 100 100 100 100\n{key_for(40)} 900 900 900 900\n", encoding="utf-8"
    )
    assert names(listing(api, sort="usage", direction="desc")) == ["heavy", FIXTURE_CLIENT]

    # Clear the heavy client's counter: its usage becomes zero and the order
    # flips. The offset is ClientMeta's and no file moved, so this also shows
    # that usage is computed against the live row rather than baked into it.
    ClientMeta.objects.filter(public_key=key_for(40)).update(offset_rx=900, offset_tx=900)

    assert names(listing(api, sort="usage", direction="desc")) == [FIXTURE_CLIENT, "heavy"]


def test_the_quota_filter_and_the_status_word_agree(api, server_conf, conf_dir):
    """A client over its limit reads "quota" and is returned by "disabled", enforced or not.

    The word on the row and the filter that returns it are computed in two
    different places now - one in Python for the page, one in SQL for the
    selection - so that they agree is a thing worth asserting rather than
    assuming.
    """
    created = add(api, "capped", quotaBytes=100)
    (conf_dir / "traffic.db").write_text(f"{created['publicKey']} 200 0 200 0\n", encoding="utf-8")

    row = next(r for r in listing(api)["clients"] if r["name"] == "capped")
    assert row["status"] == "quota"
    assert "capped" in names(listing(api, status="disabled"))
    assert "capped" not in names(listing(api, status="active"))


def test_the_expired_filter_reads_the_date_and_not_the_marker(api, server_conf):
    """And the sweep count leaves alone the one an admin has switched back on."""
    created = add(api, "lapsed")
    gone = timezone.now() - timedelta(days=1)
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(expires_at=gone)

    payload = listing(api, status="expired")
    assert names(payload) == ["lapsed"]
    assert payload["expiredCount"] == 1

    # Overruled for exactly that date: still expired, no longer sweepable.
    ClientMeta.objects.filter(public_key=created["publicKey"]).update(expiry_override_at=gone)
    payload = listing(api, status="expired")
    assert names(payload) == ["lapsed"]
    assert payload["expiredCount"] == 0


def test_the_online_filter_reads_the_live_dump(api, server_conf):
    """The one filter no column can answer, and the one place the blob is still consulted."""
    append_peer(server_conf, "quiet", key_for(50), "10.13.13.60")
    now = int(time.time())
    live_state_file().write_text(
        json.dumps(
            {
                "ts": now,
                "ifaceUp": True,
                "peers": {
                    FIXTURE_KEY: {"handshake": now - 5, "lastRx": now - 5, "endpoint": "1.2.3.4:1"}
                },
            }
        ),
        encoding="utf-8",
    )

    assert names(listing(api, status="online")) == [FIXTURE_CLIENT]
    assert names(listing(api, status="offline")) == ["quiet"]


def test_the_connectivity_filters_are_only_ever_about_a_working_client(api, server_conf, conf_dir):
    """Neither word is said about a client that is switched off or over its limit.

    The status column calls those "disabled" and "quota", and the two filters
    that read the dump have to agree with it or they contradict the table they
    narrow. "offline" is the one that shows it: a disabled peer is never in the
    dump, because the kernel is never given it, so a filter that only asks the
    dump answers "offline" for every client an admin has switched off.
    """
    disabled = add(api, "off")
    capped = add(api, "capped", quotaBytes=100)
    (conf_dir / "traffic.db").write_text(f"{capped['publicKey']} 200 0 200 0\n", encoding="utf-8")
    store.set_client_enabled("off", False)

    now = int(time.time())
    # Both of them connected, which the over-quota one genuinely can be: the
    # collector has not switched it off yet, and the disabled one is here to show
    # that being in the dump is not what decides this either.
    live_state_file().write_text(
        json.dumps(
            {
                "ts": now,
                "ifaceUp": True,
                "peers": {
                    key: {"handshake": now - 5, "lastRx": now - 5, "endpoint": "1.2.3.4:1"}
                    for key in (FIXTURE_KEY, disabled["publicKey"], capped["publicKey"])
                },
            }
        ),
        encoding="utf-8",
    )

    listed = {row["name"]: row["status"] for row in listing(api)["clients"]}
    assert (listed["off"], listed["capped"]) == ("disabled", "quota")

    assert names(listing(api, status="online")) == [FIXTURE_CLIENT]
    assert names(listing(api, status="offline")) == []
    assert set(names(listing(api, status="disabled"))) == {"off", "capped"}


def test_the_online_card_counts_what_the_table_would_list(api, server_conf, conf_dir):
    """The card and the filter answer the same question and must answer it once.

    Counting the live dump on its own counts peers the client list cannot show.
    A peer with no "# Client" comment has no row - it cannot be addressed by
    name, so nothing lists it - and the kernel serves it all the same, so a
    server with one of those could report more clients online than it has.
    """
    capped = add(api, "capped", quotaBytes=100)
    (conf_dir / "traffic.db").write_text(f"{capped['publicKey']} 200 0 200 0\n", encoding="utf-8")
    with open(server_conf, "a", encoding="utf-8") as handle:
        handle.write(f"\n[Peer]\nPublicKey = {key_for(60)}\nAllowedIPs = 10.13.13.70/32\n")

    now = int(time.time())
    live_state_file().write_text(
        json.dumps(
            {
                "ts": now,
                "ifaceUp": True,
                "peers": {
                    key: {"handshake": now - 5, "lastRx": now - 5, "endpoint": "1.2.3.4:1"}
                    for key in (FIXTURE_KEY, capped["publicKey"], key_for(60))
                },
            }
        ),
        encoding="utf-8",
    )

    figures = merge.totals()

    # Three peers in the dump, all of them connected; one client to count.
    assert figures["online_clients"] == 1
    assert figures["online_clients"] <= figures["total_clients"]
    assert names(listing(api, status="online")) == [FIXTURE_CLIENT]


def test_the_online_card_does_not_sort_what_it_only_counts(api):
    """The keys behind that card are a set, and ordering them buys nothing.

    ClientIndex is ordered by created_key and position by default, so a query
    built from it inherits that unless it is taken off - and SQLite cannot serve
    a three-term order out of the one-column index, so what the inherited
    ordering cost was a scan of every client on the server into a temp b-tree,
    to produce keys whose only use is a dictionary lookup each.

    Asserted against the query rather than against a clock, for the reason
    test_benchmark.py gives for asserting nothing at all: a threshold here would
    either be loose enough never to fire or tight enough to fail on a busy
    machine, where the shape of the query is neither.
    """
    assert "ORDER BY" not in str(merge.working_keys().query)


# ------------------------------------------------------------------- the edges


def test_no_server_config_is_still_a_503(api, server_conf):
    """A missing install is a state the UI explains, not an empty client list."""
    listing(api)
    server_conf.unlink()

    response = api.get(api_url("clients"))
    assert response.status_code == 503


def test_a_truncated_config_does_not_prune_metadata(api, server_conf):
    """The one case where "the config says so" must not be acted on.

    A half-finished restore leaves a config with no peers in it, and that is
    indistinguishable from every client having been deleted. Emptying the index
    is free - it rebuilds - but the quotas and notes beside it would be gone for
    good, so a peerless config is the one thing a prune refuses to act on.
    """
    key = add(api, "phone", quotaBytes=999)["publicKey"]
    ClientMeta.objects.filter(public_key=key).update(updated_at=timezone.now() - timedelta(hours=1))
    listing(api)

    server_conf.write_text("[Interface]\nAddress = 10.13.13.1/24\n", encoding="utf-8")
    assert listing(api)["clients"] == []

    assert ClientMeta.objects.filter(public_key=key).exists()
    assert ClientMeta.objects.get(public_key=key).quota_bytes == 999


def test_metadata_for_a_departed_peer_survives_the_grace_period(api, server_conf):
    """A key rotation writes the new key before the row follows it, and must not lose the quota."""
    key = add(api, "phone", quotaBytes=512)["publicKey"]
    listing(api)

    text = server_conf.read_text(encoding="utf-8")
    server_conf.write_text(text.split("\n[Peer]\n# Client = phone")[0] + "\n", encoding="utf-8")
    listing(api)

    # Touched a moment ago, so still inside the window.
    assert ClientMeta.objects.filter(public_key=key).exists()

    ClientMeta.objects.filter(public_key=key).update(
        updated_at=timezone.now() - timedelta(seconds=index.PRUNE_GRACE_SEC + 60)
    )
    server_conf.write_text(
        server_conf.read_text(encoding="utf-8") + "\n# nudge the stamp\n", encoding="utf-8"
    )
    listing(api)

    assert not ClientMeta.objects.filter(public_key=key).exists()


def test_a_missing_client_config_is_reported_not_guessed(api, server_conf, conf_dir):
    """has_conf_file is what says the private key is gone, and it has to survive a rebuild."""
    add(api, "phone")
    listing(api)
    assert rows()["phone"].has_conf_file is True

    (conf_dir / "clients" / "phone.conf").unlink()
    listing(api)

    assert rows()["phone"].has_conf_file is False


def test_a_client_config_that_appears_later_is_picked_up(api, server_conf, conf_dir):
    """A missing file stamps as zero, which must never match and be treated as cached."""
    add(api, "phone", allowedIps="10.13.13.0/24")
    saved = (conf_dir / "clients" / "phone.conf").read_text(encoding="utf-8")
    (conf_dir / "clients" / "phone.conf").unlink()
    listing(api)

    (conf_dir / "clients" / "phone.conf").write_text(saved, encoding="utf-8")
    listing(api)

    assert rows()["phone"].allowed_ips == "10.13.13.0/24"
    assert rows()["phone"].has_conf_file is True


def test_a_public_key_appearing_twice_does_not_empty_the_list(api, server_conf):
    """A malformed config must cost one client, not all of them.

    Two peer entries with one public key is a hand edit or a botched merge. The
    index opens one metadata row per key and hangs one row off it, so a row per
    peer would be two rows against one parent - which the database refuses,
    failing the rebuild and leaving the panel showing an empty server until
    somebody worked out why. The first entry wins and the rest are logged.
    """
    listing(api)
    append_peer(server_conf, "twin", FIXTURE_KEY, "10.13.13.99")
    # A good peer in the same edit. Without it this test passes on a rebuild that
    # blew up and left the previous rows in place, which looks identical from the
    # outside: the duplicate is missing either way.
    append_peer(server_conf, "innocent", key_for(80), "10.13.13.98")

    payload = listing(api)

    assert names(payload) == [FIXTURE_CLIENT, "innocent"]
    assert payload["totalAll"] == 2


def test_an_unnamed_peer_holds_an_address_without_being_a_row(api, server_conf):
    """The fixture's second peer has no "# Client" comment: not listable, still an occupant."""
    payload = listing(api)

    assert names(payload) == [FIXTURE_CLIENT]
    assert payload["totalAll"] == 1
    # 10.13.13.0/24 minus network, broadcast and the server's own .1, less the
    # two peers in the fixture - one of which is the unnamed one.
    assert payload["freeIps"] == 251


def test_rebuilding_opens_a_metadata_row_for_every_peer(api, server_conf):
    """The relation is what makes one query able to filter on quota and sort by handshake."""
    listing(api)

    assert ClientMeta.objects.filter(public_key=FIXTURE_KEY).exists()
    meta = ClientMeta.objects.get(public_key=FIXTURE_KEY)
    assert (meta.quota_bytes, meta.expires_at, meta.note) == (0, None, "")


def test_a_traffic_file_of_nonsense_reads_as_no_usage(api, server_conf, conf_dir):
    """Unparseable rows are dropped by the reader, which leaves a client with none."""
    listing(api)
    (conf_dir / "traffic.db").write_text("not a counter file at all\n", encoding="utf-8")

    row = listing(api)["clients"][0]

    assert (row["rxBytes"], row["txBytes"]) == (0, 0)


def test_a_traffic_file_that_cannot_be_read_keeps_the_last_figures(
    api, server_conf, conf_dir, monkeypatch
):
    """A disk that will not answer must not be reported as "nobody has used anything".

    An empty traffic.db and an unreadable one are different facts. The first
    means every client really is at zero; the second means nothing is known this
    moment, and writing zeroes over every usage figure on the server would turn
    a failed read into a false statement - one that would then be sorted on,
    counted in the dashboard totals, and compared against quotas.
    """
    (conf_dir / "traffic.db").write_text(f"{FIXTURE_KEY} 4096 2048 4096 2048\n", encoding="utf-8")
    row = listing(api)["clients"][0]
    assert (row["rxBytes"], row["txBytes"]) == (4096, 2048)

    # Switched by hand rather than with monkeypatch.undo(), which would also undo
    # the environment the autouse fixtures set and send the panel looking for the
    # real /etc/amnezia.
    denied = {"now": True}
    readable = index.traffic.read_db

    def sometimes(*args, **kwargs):
        if denied["now"]:
            raise PermissionError(13, "Permission denied")
        return readable(*args, **kwargs)

    monkeypatch.setattr(index.traffic, "read_db", sometimes)
    (conf_dir / "traffic.db").write_text(f"{FIXTURE_KEY} 9999 9999 9999 9999\n", encoding="utf-8")

    row = listing(api)["clients"][0]
    assert (row["rxBytes"], row["txBytes"]) == (4096, 2048)

    # A config change while it is still unreadable takes the other path - a full
    # rebuild rather than a counter refresh - and has to keep the figures too.
    append_peer(server_conf, "newcomer", key_for(90), "10.13.13.90")
    page = listing(api)["clients"]
    assert names({"clients": page}) == [FIXTURE_CLIENT, "newcomer"]
    assert (page[0]["rxBytes"], page[0]["txBytes"]) == (4096, 2048)

    # And the stamp was left where it was, so the reading is retried on the very
    # next request rather than waiting for the file to move again.
    denied["now"] = False
    row = listing(api)["clients"][0]
    assert (row["rxBytes"], row["txBytes"]) == (9999, 9999)


def test_an_index_that_has_never_been_built_does_not_answer_with_no_clients(
    api, server_conf, monkeypatch
):
    """Falling back to the last good answer is right. Falling back to nothing is not.

    A rebuild that fails when there is something to fall back on serves slightly
    old rows, because a poll answered with a 500 is worse. But before the first
    successful build there is no old answer - only an empty table - and serving
    that would tell an operator their server has no clients on it, which is a
    different and far more alarming thing to be wrong about.
    """

    def broken(*args, **kwargs):
        raise DatabaseError("no such table: clients_clientindex")

    monkeypatch.setattr(index, "rebuild", broken)

    # It travels rather than being swallowed, which the panel turns into a 500 and
    # the test client re-raises. The assertion is that the request does not come
    # back 200 with an empty list.
    with pytest.raises(DatabaseError):
        api.get(api_url("clients"))
    assert not ClientIndex.objects.exists()


def test_a_failed_rebuild_keeps_serving_what_was_built_before(api, server_conf, monkeypatch):
    """The other side of it: once there is an answer, an old one beats none."""
    assert names(listing(api)) == [FIXTURE_CLIENT]

    def broken(*args, **kwargs):
        raise DatabaseError("database is locked")

    monkeypatch.setattr(index, "rebuild", broken)
    append_peer(server_conf, "unseen", key_for(100), "10.13.13.100")

    assert names(listing(api)) == [FIXTURE_CLIENT]


def test_not_configured_travels_through_current(server_conf):
    """current() swallows read failures but never this one - see its docstring."""
    server_conf.unlink()

    with pytest.raises(NotConfigured):
        index.current()


# ------------------------------------------------- folding a change in directly


@pytest.fixture
def settled(server_conf, monkeypatch):
    """An index built and level with the files, past the once-per-process rebuild.

    Both halves matter. Nothing can be folded into an index that was never
    built, and the first read in a process rebuilds unconditionally - see
    index._verified - so a test asserting that no rebuild happened has to get
    both out of the way before it starts measuring.

    Set through monkeypatch so it goes back to what it was afterwards: the flag
    is process-wide by design, and a test that left it standing would decide
    whether the next one rebuilds.
    """
    index.current()
    monkeypatch.setattr(index, "_verified", True)
    return server_conf


@pytest.fixture
def no_rebuild(monkeypatch):
    """Fail the test if anything reads the config files again.

    The assertion that matters for every test in this section. A fast path that
    quietly fell back would still produce a correct client list - that is the
    whole design - so correctness alone cannot tell the two apart, and only
    forbidding the rebuild outright shows which one ran.
    """

    def rebuilt(*args, **kwargs):
        raise AssertionError("the config was read again; the change was not folded in")

    monkeypatch.setattr(index, "rebuild", rebuilt)


def test_adding_a_client_writes_its_row_without_reading_the_config(settled, no_rebuild):
    """The case this exists for: provisioning over the API, one client per request."""
    view = store.add_client("second")

    assert index.Stamp.stored(index.current()) == index.Stamp.observe()
    row = rows()["second"]
    assert (row.enabled, row.has_conf_file, row.cum_rx) == (True, True, 0)
    assert row.ip == view.ip


def test_removing_a_client_drops_its_row_and_closes_the_gap_in_position(settled, no_rebuild):
    """Positions are indices into the config, so a removal shifts everything after it.

    Left alone they would still sort correctly - the column is only ever a
    tiebreaker - but they would no longer be what a rebuild writes, and two
    paths that disagree about a column are exactly what the tests in this file
    exist to catch.
    """
    for name in ("a", "b", "c"):
        store.add_client(name)
    index.current()

    store.remove_clients(["a", "c"])
    index.current()

    assert set(rows()) == {FIXTURE_CLIENT, "b"}
    # The fixture's unnamed peer sits at 1 and holds its place, so "b" is 2.
    assert rows()["b"].position == 2


def test_renaming_disabling_and_rescoping_all_fold_in(settled, no_rebuild):
    """The three single-peer edits, none of which needs the config read back."""
    store.add_client("second")
    index.current()

    store.set_client_enabled("second", False)
    store.update_client("second", allowed_ips="10.13.13.0/24")
    store.rename_client("second", "renamed")
    index.current()

    row = rows()["renamed"]
    assert row.enabled is False
    assert row.allowed_ips.startswith("10.13.13.0/24")
    assert row.name_key == "renamed"


def test_what_is_folded_in_is_what_a_rebuild_would_have_written(api, server_conf, conf_dir):
    """The test that makes a second writer of this table safe to have.

    Two paths now write ClientIndex - the rebuild, which reads the files, and
    absorb, which is told what changed - and a column they disagree about is a
    row that is wrong while the stamp says the index is current, so nothing ever
    re-reads the file that would correct it. That is the one failure this design
    has no defence against, so it is asserted directly: run every mutation that
    folds in, then throw the table away, rebuild it from the files alone, and
    require the two to be equal column for column.

    Compared over index.FIELDS rather than a list written out here, so a column
    added later is covered by this test on the day it is added rather than on
    the day somebody remembers to extend it.
    """
    add(api, "second")
    add(api, "third")
    add(api, "fourth")
    store.set_client_enabled("second", False)
    store.update_client("third", allowed_ips="10.13.13.0/24")
    store.rename_client("third", "renamed")
    store.remove_client("fourth")
    store.remove_clients(["second"])
    listing(api)

    folded = snapshot()

    ClientIndex.objects.all().delete()
    ClientIndexState.objects.all().delete()
    index.current()

    assert snapshot() == folded


def test_a_change_too_big_to_fold_is_rebuilt_instead(settled, monkeypatch):
    """Past a point the incremental path is the slower one - see ABSORB_MAX_PEERS.

    Each removal costs a statement of its own to close the gap it left, and the
    keys travel in an IN clause against a parameter limit, so a bulk delete is
    handed back to the rebuild rather than being taken apart.
    """
    monkeypatch.setattr(index, "ABSORB_MAX_PEERS", 2)
    for name in ("a", "b", "c"):
        store.add_client(name)
    index.current()

    store.remove_clients(["a", "b", "c"])

    assert index.Stamp.stored(index._state()) != index.Stamp.observe()
    index.current()
    assert set(rows()) == {FIXTURE_CLIENT}


def test_a_change_that_adds_and_removes_at_once_is_rebuilt_instead(settled):
    """Nothing in awg.store announces one, and the three helpers assume nothing does.

    Positions are read out of the config as it stands while a removal renumbers
    the rows still stored, so the two would shift against each other. Refused
    rather than half-supported: it is a rebuild either way, and an ordering that
    is only sometimes right is the kind of wrong this table cannot notice.

    Called directly, because no mutation produces such a change - which is also
    why the stamp is not asserted on here. absorb leaves it alone and relies on
    the caller's own write having moved the files past it; there is no write in
    front of this one, so what is checked is the part that is this function's
    own doing: that it wrote nothing.
    """
    view = store.add_client("second")
    index.current()
    before = snapshot()

    index.absorb(
        store._read_conf(),
        store.ConfChange(added=(view.public_key,), removed=(FIXTURE_KEY,)),
    )

    assert snapshot() == before


def test_peers_added_in_one_change_are_numbered_as_a_rebuild_would_number_them(settled):
    """A change naming two new peers must not depend on the order it names them in.

    Inserting a row shifts every row at or after it, so two inserts applied in
    the order they happen to arrive shift each other: the later peer, taken
    first, is moved on again by the earlier one and ends up below the row it was
    supposed to displace. Nothing sorts against `position` - it is only ever a
    tiebreaker - so the wrong answer is a table that quietly disagrees with what
    a rebuild writes, which is the one thing the two writers may not do.

    Called directly, like the mixed change above: no mutation announces two adds
    today, and this is here so the day one does it is slow at worst.
    """
    head, rest = settled.read_text(encoding="utf-8").split("\n[Peer]", 1)
    added = ""
    for seed, name in ((91, "first"), (92, "second")):
        added += (
            f"\n[Peer]\n# Client = {name}\n# Created = 2026-09-01T00:00:0{seed - 90}Z\n"
            f"PublicKey = {key_for(seed)}\nAllowedIPs = 10.13.13.{seed}/32\n"
        )

    index.begin()
    # In front of the peers that are already there, so both inserts have
    # something to displace, and announced highest position first.
    settled.write_text(head + added + "\n[Peer]" + rest, encoding="utf-8")
    index.absorb(
        store._read_conf(),
        store.ConfChange(added=(key_for(92), key_for(91))),
    )
    folded = snapshot()
    assert {row.name for row in ClientIndex.objects.all()} == {"first", "second", FIXTURE_CLIENT}

    ClientIndex.objects.all().delete()
    ClientIndexState.objects.all().delete()
    index.current()

    assert folded == snapshot()


def test_a_mutation_that_announces_nothing_is_still_noticed(api, server_conf):
    """save_server moves every peer's address at once and describes none of it.

    So it announces nothing, and is caught the way a hand edit is: it
    wrote the file, the stamp moved, the next read rebuilds. That is what makes
    the fast path safe to add one mutation at a time - the ones left off it are
    not broken, only slower.
    """
    assert names(listing(api)) == [FIXTURE_CLIENT]

    store.save_server({"mtu": 1380})

    assert index.Stamp.stored(index._state()) != index.Stamp.observe()
    assert names(listing(api)) == [FIXTURE_CLIENT]


def test_a_fast_path_that_fails_leaves_the_index_stale_rather_than_wrong(settled, monkeypatch):
    """Its failures have to cost a rebuild, never accuracy.

    Anything it cannot do - a lock it cannot take, a row it cannot find, a
    database that will not answer - has to leave the stamp describing files older
    than the ones on disk, because that is the state the next read repairs. The
    failure is injected at the point where half the work is already done, so
    what is being checked is that the transaction took it back with it.
    """

    def broken(*args, **kwargs):
        raise DatabaseError("database is locked")

    monkeypatch.setattr(index, "_absorb_added", broken)
    store.add_client("second")

    assert not ClientIndex.objects.filter(name="second").exists()
    assert index.Stamp.stored(index._state()) != index.Stamp.observe()

    index.current()
    assert set(rows()) == {FIXTURE_CLIENT, "second"}


def test_a_fold_waits_for_the_index_lock_inside_the_config_lock_budget(settled, monkeypatch):
    """The one place the index lock is waited for by somebody holding the other one.

    Two budgets, and what separates them is what the waiter is holding rather
    than what it is waiting for. A fold waits for the index lock from inside the
    config lock, so every second it spends is a second the next mutation is
    queued behind it - it has to give up quickly, and giving up costs only the
    rebuild it was trying to avoid. A request waiting for the same index lock
    holds nothing at all, so it can afford to wait for the rebuild to finish and
    be served by it.

    So the fold's budget is the short one, well inside both. The chain these
    used to be asserted in - absorb < config < request - is not the invariant:
    the config lock's budget answers to a different question entirely, which is
    how long a settings save on a large server may take.
    """
    assert index.ABSORB_LOCK_TIMEOUT_SEC < index.LOCK_TIMEOUT_SEC
    assert index.ABSORB_LOCK_TIMEOUT_SEC < lock.CONFIG_LOCK_SEC

    # Shortened so the test does not spend the real budget waiting; what is under
    # test is that absorb gives up and the mutation does not care.
    monkeypatch.setattr(index, "ABSORB_LOCK_TIMEOUT_SEC", 0.05)
    taken, finished = threading.Event(), threading.Event()

    def rebuilding_elsewhere() -> None:
        with lock.file_lock(index_lock_file(), timeout=5.0):
            taken.set()
            finished.wait(10.0)

    holder = threading.Thread(target=rebuilding_elsewhere)
    holder.start()
    try:
        assert taken.wait(5.0)
        store.add_client("second")
    finally:
        finished.set()
        holder.join(10.0)

    # The write went through and the fold did not, which leaves the stamps to it.
    assert not ClientIndex.objects.filter(name="second").exists()
    index.current()
    assert set(rows()) == {FIXTURE_CLIENT, "second"}


def test_a_fold_that_failed_still_leaves_something_to_fall_back_on(api, settled, monkeypatch):
    """Why absorb leaves the stamp alone instead of zeroing it when it gives up.

    An all-zero stamp does not mean "stale", it means "never built", and current()
    answers a failed rebuild from that state by raising rather than serving what
    it has - which is right when there is genuinely nothing to serve. Zeroing it
    on the way out of a failed fold would borrow that meaning falsely, and a poll
    arriving while the database was locked would get a 500 instead of a client
    list a few seconds out of date.
    """

    def broken(*args, **kwargs):
        raise DatabaseError("database is locked")

    monkeypatch.setattr(index, "_absorb", broken)
    store.add_client("second")
    monkeypatch.setattr(index, "rebuild", broken)

    assert names(listing(api)) == [FIXTURE_CLIENT]


def test_nothing_is_folded_into_an_index_that_was_never_built(server_conf):
    """A row written before the first build would be a server reporting one client.

    The stamp would then say the table is current, and a client list showing one
    of four thousand clients is a worse answer than the slow one.
    """
    assert not ClientIndex.objects.exists()

    store.add_client("second")

    assert not ClientIndex.objects.exists()
    index.current()
    assert set(rows()) == {FIXTURE_CLIENT, "second"}


def test_every_column_of_the_table_is_one_both_writers_fill(settled):
    """A column outside index.FIELDS is a column nothing keeps level with the files.

    FIELDS is what _sync compares to decide a row needs rewriting, what the fast
    path carries forward in _absorb_updated, and what the equivalence test above
    compares the two paths over. A column added to the model and not to FIELDS is
    therefore invisible to all three at once: the rebuild recomputes it and then
    decides the row is unchanged, so it is never written, and no test notices.

    So the model is asked instead of a list kept by hand. Adding a column now
    fails here until it is either indexed or named as one of the two that cannot
    be: the primary key, and the relation to the metadata row.
    """
    columns = {field.name for field in ClientIndex._meta.concrete_fields}

    assert columns - set(index.FIELDS) == {"id", "meta"}


def test_a_change_made_outside_the_panel_is_not_lost_by_the_next_one_inside_it(settled):
    """The failure the whole fast path had to be made safe against.

    An edit over SSH moves the files and the panel is not running to see it. If the next panel mutation folded its own change into the index it
    had, that outside client would be dropped from the copy and the stamp would
    then say the copy is level with the files - so nothing would ever read them
    again to find out, and the client would stay missing from the list for as
    long as the worker lived.
    """
    append_peer(settled, "alice", key_for(77), "10.13.13.77")

    store.add_client("bob")
    index.current()

    assert set(rows()) == {FIXTURE_CLIENT, "alice", "bob"}


def test_an_outside_edit_to_another_client_survives_a_panel_edit(settled):
    """The same failure without a peer being added or removed, which a count would miss.

    A hand edit renames one client and the panel then disables a different one.
    Both are one row, the number of peers never changes, and only comparing what
    the index was built from against what this change started from catches it.
    """
    store.add_client("bob")
    index.current()
    settled.write_text(
        settled.read_text(encoding="utf-8").replace(
            f"# Client = {FIXTURE_CLIENT}", "# Client = renamed"
        ),
        encoding="utf-8",
    )

    store.set_client_enabled("bob", False)
    index.current()

    assert set(rows()) == {"renamed", "bob"}
    assert rows()["bob"].enabled is False


def test_a_config_naming_one_key_twice_is_rebuilt_rather_than_folded(settled):
    """Two peer entries on one public key leave a named peer with no row of its own.

    A removal is addressed by public key, so it would delete the row belonging to
    the twin, and the positions after it move by one more than the shift can
    account for. Only a hand edit or a botched merge writes such a config; the
    rebuild already handles it by keeping the first entry and logging.
    """
    view = store.add_client("second")
    index.current()
    append_peer(settled, "twin", view.public_key, "10.13.13.90")

    store.remove_client(FIXTURE_CLIENT)
    index.current()

    assert set(rows()) == {"second"}


def test_a_mutation_that_never_said_when_it_started_is_rebuilt(settled, monkeypatch):
    """Forgetting to note the start has to cost a read, not accuracy.

    A mutation added later that announces without calling awg.store._begin has
    no way to prove the index was level with the files before it ran, so the
    change is refused and the stamps deal with it - which is what every mutation
    that announces nothing already relies on.
    """
    monkeypatch.setattr(store, "_begin", lambda: None)

    store.add_client("second")

    assert not ClientIndex.objects.filter(name="second").exists()
    index.current()
    assert set(rows()) == {FIXTURE_CLIENT, "second"}


def test_a_mutation_that_writes_nothing_leaves_no_stamp_behind(settled, monkeypatch):
    """What the test above rests on, and what it cannot show on its own.

    A mutation that ends without announcing is ordinary - here it is a client
    asked to be enabled that already was, which the collector does on every pass
    and the panel does on any edit that resends the switch - and it has already
    noted how the files looked before it. Left on the thread, that note is not
    stale in the harmless sense: it was taken before that mutation, so it agrees
    with what the index has stored, and the next change to reach absorb without
    a note of its own would be placed by it and pass the check that exists to
    catch precisely this.

    Which would undo the guarantee the test above asserts: what is refused there
    would be accepted here, against a stamp belonging to somebody else, and the
    peer added over SSH in between would be dropped from the index under a stamp
    saying the index is level with the files.
    """
    store.set_client_enabled(FIXTURE_CLIENT, True)
    append_peer(settled, "alice", key_for(93), "10.13.13.93")

    monkeypatch.setattr(store, "_begin", lambda: None)
    store.add_client("bob")

    index.current()
    assert set(rows()) == {FIXTURE_CLIENT, "alice", "bob"}


def test_removing_a_client_keeps_the_grace_period_its_metadata_had(settled):
    """The fast path prunes on removal, and must prune on the same terms as a rebuild.

    A key rotation writes the new key to the config a moment before the row
    follows it, so a metadata row touched recently is one that may be mid-move.
    Deleting it takes a quota, an expiry date and a note with it, and none of
    that comes back.
    """
    fresh = store.add_client("fresh")
    stale = store.add_client("stale")
    index.current()
    ClientMeta.objects.filter(public_key=stale.public_key).update(
        updated_at=timezone.now() - timedelta(seconds=index.PRUNE_GRACE_SEC + 60)
    )

    store.remove_clients(["fresh", "stale"])

    assert ClientMeta.objects.filter(public_key=fresh.public_key).exists()
    assert not ClientMeta.objects.filter(public_key=stale.public_key).exists()


def test_counters_still_refresh_after_a_change_is_folded_in(settled):
    """The fast path opens no traffic.db, so it must not claim to have read one.

    Stamping it as read would freeze every usage figure on the server until that
    file next moved, which on a panel whose collector had stopped is for good.
    """
    view = store.add_client("second")
    index.current()
    conf_dir = settled.parent
    (conf_dir / "traffic.db").write_text(
        f"{view.public_key} 4096 2048 4096 2048\n", encoding="utf-8"
    )

    index.current()

    row = rows()["second"]
    assert (row.cum_rx, row.cum_tx) == (4096, 2048)


def _flush(conf_dir, key: str, cum_rx: int, cum_tx: int):
    """Write traffic.db the way the collector does, with a stat either side."""
    before = index.Stamp.observe()
    (conf_dir / "traffic.db").write_text(
        f"{key} {cum_rx} {cum_tx} {cum_rx} {cum_tx}\n", encoding="utf-8"
    )
    return before, index.Stamp.observe()


def test_counters_handed_over_are_written_without_reading_the_file(settled, monkeypatch):
    """What the collector already knows, it should not have to be asked for again.

    The old path read the whole of traffic.db and compared every index row
    against it, every ten seconds, for the life of the panel - to write the
    handful of rows a busy ten seconds changes. Told which peers moved, it writes
    those and opens nothing.
    """
    conf_dir = settled.parent
    before, after = _flush(conf_dir, FIXTURE_KEY, 4096, 2048)

    def fail(*args, **kwargs):
        raise AssertionError("traffic.db was read after the collector had said what changed")

    monkeypatch.setattr(index, "_counters", fail)
    assert index.absorb_counters({FIXTURE_KEY: Counters(4096, 2048, 4096, 2048)}, before, after)

    row = rows()[FIXTURE_CLIENT]
    assert (row.cum_rx, row.cum_tx) == (4096, 2048)
    # And stamped, so the next read returns without so much as a refresh.
    assert index.Stamp.stored(index._state()).counters == index.Stamp.observe().counters


def test_counters_are_refused_when_the_index_was_not_level_with_the_file(settled):
    """The same rule absorb() applies to the config, on the other file.

    A PreDown hook between two flushes means this delta does not
    lead from what is stored to what is on disk. Writing it anyway would leave
    rows that are wrong under a stamp saying they are current, which is the one
    failure nothing here would ever correct - so it is refused, and the read that
    follows rebuilds exactly as it always did.
    """
    conf_dir = settled.parent
    _, stale = _flush(conf_dir, FIXTURE_KEY, 4096, 2048)
    before, after = _flush(conf_dir, FIXTURE_KEY, 8192, 4096)

    # `stale` describes a version of the file the index never absorbed.
    assert not index.absorb_counters({FIXTURE_KEY: Counters(8192, 4096, 8192, 4096)}, stale, after)

    assert (rows()[FIXTURE_CLIENT].cum_rx, rows()[FIXTURE_CLIENT].cum_tx) == (0, 0)
    index.current()
    assert (rows()[FIXTURE_CLIENT].cum_rx, rows()[FIXTURE_CLIENT].cum_tx) == (8192, 4096)


def test_a_key_with_no_row_is_passed_over_rather_than_refused(settled):
    """traffic.db outlives the peers in it - removing a client drops its row
    explicitly, but a hand-edited config does not - and one key nothing indexes must not stop
    the rest of the batch being written."""
    conf_dir = settled.parent
    before, after = _flush(conf_dir, FIXTURE_KEY, 512, 256)
    ghost = base64.b64encode(b"g" * 32).decode()

    assert index.absorb_counters(
        {FIXTURE_KEY: Counters(512, 256, 512, 256), ghost: Counters(9, 9, 9, 9)}, before, after
    )

    assert (rows()[FIXTURE_CLIENT].cum_rx, rows()[FIXTURE_CLIENT].cum_tx) == (512, 256)


def test_the_first_read_in_a_process_rebuilds_whatever_the_stamp_says(settled, monkeypatch):
    """What bounds how long a bug in the fast path can survive.

    A wrong row written by absorb is a row the stamp protects: it says the index
    is current, so nothing re-reads the file that would correct it. One
    unconditional rebuild per worker means such a row cannot outlive a restart.
    """
    ClientIndex.objects.filter(name=FIXTURE_CLIENT).update(name="wrong", name_key="wrong")
    assert index.Stamp.stored(index._state()) == index.Stamp.observe()

    # A second read on the same stamp leaves it alone, which is the saving.
    index.current()
    assert set(rows()) == {"wrong"}

    monkeypatch.setattr(index, "_verified", False)
    index.current()

    assert set(rows()) == {FIXTURE_CLIENT}


def test_a_first_read_that_could_not_rebuild_has_not_verified_anything(settled, monkeypatch):
    """A pass that gave up has checked nothing, and must not be counted.

    The worker gets one unconditional rebuild and it is the only thing standing
    between a row absorb wrote wrongly and the end of the process - the stamp
    says the index is current, so nothing else will ever look. Spending it on an
    attempt that hit a locked database would leave the guarantee claimed and not
    delivered, which is worse than not having it: what falls back here is a
    client list a few seconds old, and what is lost is the correction.
    """
    ClientIndex.objects.filter(name=FIXTURE_CLIENT).update(name="wrong", name_key="wrong")
    monkeypatch.setattr(index, "_verified", False)

    real = index.rebuild

    def broken(*args, **kwargs):
        raise DatabaseError("database is locked")

    monkeypatch.setattr(index, "rebuild", broken)
    index.current()
    assert set(rows()) == {"wrong"}

    monkeypatch.setattr(index, "rebuild", real)
    index.current()

    assert set(rows()) == {FIXTURE_CLIENT}
