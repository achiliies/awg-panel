"""What GET api/v1/stats/live sends, and how much of it.

This is the most polled endpoint in the panel - every open tab asks every two
seconds - and it is the one whose response grew with the server. The blob the
collector writes holds every peer the kernel does, because the totals beside
them are sums over all of them; a browser drawing one page of clients needs the
peers on that page. So the endpoint narrows, and these tests are about the seam
between those two facts: the aggregate block is always whole, `peers` is
whatever was asked for, and asking for nothing is not the same as asking for
everything.
"""

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.stats import live
from apps.stats.views import MAX_LIVE_PEERS
from awg import store, traffic

pytestmark = pytest.mark.django_db


def api_url(path: str) -> str:
    return f"/api/v1/{path}"


@pytest.fixture
def api(server_conf) -> APIClient:
    user = get_user_model().objects.create_user("admin")
    client = APIClient()
    client.force_login(user)
    return client


def peer(**fields) -> dict:
    """One entry in the blob's `peers`, in the collector's own spelling."""
    return {
        "rateRx": 0,
        "rateTx": 0,
        "rx": 0,
        "tx": 0,
        "handshake": 0,
        "lastRx": 0,
        "endpoint": "",
        "online": False,
        "status": "offline",
        **fields,
    }


def write_blob(*keys: str) -> None:
    """A blob reporting the named peers, with totals that are not sums of them.

    Deliberately not consistent with the peers: every test here that reads the
    totals is checking that they came through untouched by the narrowing, and a
    figure that happened to equal the sum of the returned peers would pass that
    check without meaning anything.
    """
    live.write_live(
        {
            **live.empty_blob(),
            "ts": 1_700_000_000,
            "ifaceUp": True,
            "online": 7,
            "total": len(keys),
            "totalRateRx": 4096,
            "totalRateTx": 2048,
            "peers": {key: peer(rateRx=1) for key in keys},
        }
    )


def test_without_the_parameter_every_peer_comes_back(api):
    """What the endpoint has always answered, and what an unaware caller still gets."""
    write_blob("aaa", "bbb", "ccc")

    body = api.get(api_url("stats/live")).json()

    assert sorted(body["peers"]) == ["aaa", "bbb", "ccc"]


def test_only_the_peers_asked_for_come_back(api):
    """The whole point: one page of clients costs one page of peers."""
    write_blob("aaa", "bbb", "ccc")

    body = api.get(api_url("stats/live"), {"peers": "aaa,ccc"}).json()

    assert sorted(body["peers"]) == ["aaa", "ccc"]


def test_an_empty_parameter_asks_for_no_peers_at_all(api):
    """The dashboard's case. It reads the totals and never looks at a peer, so
    sending it a few thousand of them is the whole problem in miniature."""
    write_blob("aaa", "bbb")

    body = api.get(api_url("stats/live"), {"peers": ""}).json()

    assert body["peers"] == {}
    assert body["online"] == 7


def test_the_aggregate_block_is_never_narrowed(api):
    """Every field beside `peers` is a fact about the server, not about the page.

    The counts are sums over every peer the kernel holds, so a request for two
    of them must still report how many are online in total - otherwise the
    dashboard's cards would count whatever the client table last scrolled to.
    """
    write_blob("aaa", "bbb", "ccc")

    body = api.get(api_url("stats/live"), {"peers": "aaa"}).json()

    assert body["peers"].keys() == {"aaa"}
    assert body["ts"] == 1_700_000_000
    assert body["ifaceUp"] is True
    assert body["online"] == 7
    assert body["total"] == 3
    assert (body["totalRateRx"], body["totalRateTx"]) == (4096, 2048)
    assert "system" in body


def test_a_peer_the_collector_knows_nothing_about_is_left_out(api):
    """Not answered with a zeroed entry.

    "The collector is reporting nothing for this peer" is a state the UI already
    reads - a disabled client has no key in the kernel and appears in no blob -
    and inventing an empty peer for it would report it as connected but idle.
    """
    write_blob("aaa")

    body = api.get(api_url("stats/live"), {"peers": "aaa,gone"}).json()

    assert sorted(body["peers"]) == ["aaa"]


def test_a_request_naming_more_peers_than_a_page_could_hold_is_capped(api):
    """A hand-made request with ten thousand keys cannot turn a cheap endpoint
    into a long one; the cap is the largest page the client list will serve."""
    keys = [f"peer{index}" for index in range(MAX_LIVE_PEERS + 50)]
    write_blob(*keys)

    body = api.get(api_url("stats/live"), {"peers": ",".join(keys)}).json()

    assert len(body["peers"]) == MAX_LIVE_PEERS


def test_a_collector_that_never_ran_is_not_an_error(api):
    """No file at all, which is a web-only container or a panel installed a
    minute ago. The blob's ts of 0 is what tells the UI to say so."""
    body = api.get(api_url("stats/live"), {"peers": "aaa"}).json()

    assert body["ts"] == 0
    assert body["peers"] == {}


def test_the_config_stamp_moves_when_a_client_is_switched_off(api):
    """What the whole field is for. A client that crosses its data limit loses
    its key within one collector poll, and a caller reading the client list on
    its own slow timer has no other way to hear about it - so it goes on drawing
    the row as enabled and idle underneath a usage bar this same blob has just
    filled to the end."""
    write_blob("aaa")
    before = api.get(api_url("stats/live"), {"peers": ""}).json()["confStamp"]
    assert before

    store.set_clients_enabled(["client1"], False)

    assert api.get(api_url("stats/live"), {"peers": ""}).json()["confStamp"] != before


def test_the_config_stamp_stands_still_while_the_configuration_does(api):
    """The other half of the contract, and the more expensive one to get wrong:
    a stamp that moved on its own would put the client list's refetch on a timer
    of exactly the kind this exists to avoid.

    traffic.db in particular, which the collector rewrites every ten seconds for
    as long as it runs. It is deliberately not one of the files behind this
    stamp - its contents are what the blob is already carrying.
    """
    write_blob("aaa")
    before = api.get(api_url("stats/live"), {"peers": ""}).json()["confStamp"]

    traffic.sync({"aaa": (1_000, 2_000)})
    write_blob("aaa", "bbb")

    assert api.get(api_url("stats/live"), {"peers": ""}).json()["confStamp"] == before


def test_a_server_with_no_configuration_stamps_nothing(api, monkeypatch):
    """Empty is "no opinion", not a stamp of its own. A caller comparing it with
    the last one it saw is told nothing changed, which is true, rather than being
    sent to refetch a list about a server that does not exist."""
    monkeypatch.setattr(live, "server_conf", lambda: Path("/nonexistent/awg0.conf"))

    assert api.get(api_url("stats/live"), {"peers": ""}).json()["confStamp"] == ""
