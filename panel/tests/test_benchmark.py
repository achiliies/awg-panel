"""What the panel costs on a server far larger than anyone will run.

Off by default and run with AWG_BENCH=1, because building the fixture writes
four thousand config files and the whole thing takes the better part of a
minute - which is the wrong trade for a suite that runs on every commit, and
the right one for a number somebody is going to quote.

    AWG_BENCH=1 pytest tests/test_benchmark.py -s
    AWG_BENCH=1 AWG_BENCH_CLIENTS=10000 pytest tests/test_benchmark.py -s

It asserts nothing. Timings on a shared machine are not a pass/fail signal, and
a threshold here would either be so loose it never fires or so tight it fails on
a busy laptop; what catches a performance regression in CI is
test_client_index.py's query counts, which are exact and machine-independent.
This is for the question that asks "and how long does that actually take".

Everything is measured through the HTTP API, on the real view and serialiser
stack, because that is what an operator's browser waits for. Numbers taken any
closer to the database describe a layer nobody uses on its own.

Two things it does not reproduce, and both flatter the result slightly. The
kernel call is the mock controller rather than `awg syncconf`, so an add is
missing one subprocess. And the fixture's client configs are minimal, so parsing
one is a little cheaper than parsing a real one. Neither touches the shape of
what is being compared: both are constant per client and neither grows with the
size of the server, which is the only thing these figures are about.
"""

import base64
import os
import time

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.clients import index
from awg import store

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        not os.environ.get("AWG_BENCH"),
        reason="set AWG_BENCH=1 to run the benchmark; it builds thousands of config files",
    ),
]

# Bounded below by one because every read below needs something to read, and
# above by what the fixture's /16 can address without handing two peers the same
# tunnel address - which no real server would do, and which would quietly make
# the thing being measured unrepresentative rather than fail.
CLIENTS = min(max(int(os.environ.get("AWG_BENCH_CLIENTS", "4000")), 1), 65_000)
REPS = int(os.environ.get("AWG_BENCH_REPS", "12"))

# The client the single-client read asks for. Derived rather than named, because
# a fixed one is a benchmark that only runs at the size it was written at: the
# whole point of AWG_BENCH_CLIENTS is comparing sizes, and half of them are
# smaller. The middle of the config also keeps that read honest - the first and
# last peer are the two a linear scan would find soonest.
PROBE = f"c{CLIENTS // 2}"


def _key(seed: int) -> str:
    """A distinct, well-formed public key. Never parsed, only matched."""
    return base64.b64encode(seed.to_bytes(4, "big") + bytes(28)).decode()


def _populate(server_conf, conf_dir, count):
    """Write a server holding `count` clients, the way the bash tools would have.

    Peers are appended to the server config and each gets a file in clients/,
    which is the state the panel would be in after a peer had been appended
    that many times. Built directly rather than through the panel because
    four thousand real adds is minutes of work to set up a measurement.
    """
    server_conf.write_text(
        server_conf.read_text(encoding="utf-8").replace("10.13.13.1/24", "10.13.0.1/16"),
        encoding="utf-8",
    )
    env_path = conf_dir / "clients.env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace("10.13.13", "10.13.0"), encoding="utf-8"
    )
    with open(server_conf, "a", encoding="utf-8") as handle:
        handle.write(
            "".join(
                f"\n[Peer]\n# Client = c{i}\n# Created = 2026-09-01T00:00:00Z\n"
                f"PublicKey = {_key(i + 1000)}\nAllowedIPs = 10.13.{(i // 254) % 256}."
                f"{i % 254 + 1}/32\n"
                for i in range(count)
            )
        )
    clients = conf_dir / "clients"
    clients.mkdir(exist_ok=True)
    body = (
        f"[Interface]\nPrivateKey = {_key(9)}\nAddress = 10.13.0.2/32\nDNS = 1.1.1.1\n\n"
        "[Peer]\nAllowedIPs = 0.0.0.0/0\n"
    )
    for i in range(count):
        (clients / f"c{i}.conf").write_text(body, encoding="utf-8")


def test_benchmark(server_conf, conf_dir, capsys):
    _populate(server_conf, conf_dir, CLIENTS)
    api = APIClient()
    api.force_login(get_user_model().objects.create_user("admin"))

    def get(path, **params):
        response = api.get(f"{settings.BASE_PATH}api/v1/{path}", params)
        assert response.status_code == 200, (path, response.status_code)
        return response

    def timed(label, work):
        work()  # once to build the index and warm the caches, never counted
        start = time.perf_counter()
        for _ in range(REPS):
            work()
        print(f"  {label:<38} {(time.perf_counter() - start) * 1000 / REPS:8.1f} ms")

    with capsys.disabled():
        print(f"\n{CLIENTS} clients, {REPS} reps, through the API\n")

        print("reads, nothing having changed")
        timed("a page of fifty", lambda: get("clients", limit=50))
        timed("the dashboard summary", lambda: get("stats/summary"))
        timed("one client", lambda: get(f"clients/{PROBE}"))
        # Searched for the probe rather than a fixed string, so the query has
        # something to find at any size. What is being timed is the scan, which
        # the number of rows that survive it barely moves.
        timed("a page, searched", lambda: get("clients", limit=50, q=PROBE))
        timed("a page, ordered by usage", lambda: get("clients", limit=50, sort="usage"))
        timed("the freshness check alone", index.current)

        print("\nwriting, and the read that follows it")
        names = iter(f"bench{i}" for i in range(1_000_000))

        def add_and_read():
            store.add_client(next(names))
            get("clients", limit=50)

        timed("add a client, then read the list", add_and_read)
        timed("a full rebuild of the index", lambda: index.rebuild(force=True))

        print("\nthe writes enforcement makes, which nobody waits on")

        def flipper(subset):
            """Switch the subset on and off, so every call really does the write.

            Asked for the state it is already in, the store does nothing and
            returns nothing - which is the common case on a real server and is
            measured separately below, but would make this row a measurement of
            a parse rather than of a mutation.
            """
            store.set_clients_enabled(subset, True)
            state = {"on": True}

            def flip() -> None:
                state["on"] = not state["on"]
                moved = store.set_clients_enabled(subset, state["on"])
                assert len(moved) == len(subset), (len(moved), len(subset))

            return flip

        timed("disable one client", flipper([f"c{CLIENTS // 2}"]))
        # The burst: an admin lowering a default quota puts everybody over at
        # once. What this row is really asking is whether that costs more than
        # one client does, and the answer should be almost nothing.
        #
        # Bounded by the size of the server, like everything else here: a
        # thousand is what the documented figures were taken at, and asking for
        # a thousand names on a server of forty is a row that fails rather than
        # one that answers a smaller version of the same question.
        burst = min(1000, CLIENTS)
        timed(f"disable {burst}, in one call", flipper([f"c{i}" for i in range(burst)]))
        # What a pass usually asks for: it re-asserts a decision the previous one
        # already applied. Named for the call rather than for the pass, because
        # that is what it times - the reconcile around it also re-reads every
        # client's limits and every client's config file, and none of that is
        # here.
        timed(
            "asking for a change that is already applied",
            lambda: store.set_clients_enabled([f"c{CLIENTS // 2}"], False),
        )

        print("\nafter a change made outside the panel")
        outside = iter(range(500_000, 600_000))

        def outside_then_read():
            # Appended in place, which is what an edit over SSH does
            # with nothing here running. The panel cannot fold in what it did not
            # do, so this is the case that still costs a full rebuild.
            seed = next(outside)
            with open(server_conf, "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[Peer]\n# Client = {next(names)}\n"
                    f"PublicKey = {_key(seed)}\n"
                    f"AllowedIPs = 10.13.200.{seed % 254 + 1}/32\n"
                )
            get("clients", limit=50)

        timed("something appends a peer, then read", outside_then_read)
