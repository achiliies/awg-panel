"""In-memory stand-in for the AmneziaWG tools.

AWG_MOCK=1 runs the whole panel - web, collector, frontend - on a machine with
no kernel module, no `awg` binary and no root: a laptop, a CI runner, a demo.

The peer list is not invented. It is read back from the same server config the
store writes, on every call, so the mock cannot drift from what the UI shows and
so a client added in the browser appears in the live view a second later.
Disabled peers are left out exactly as `strip_conf` leaves them out, because
that is what the kernel would know about. Only the numbers are synthetic:
handshakes, endpoints and transfer counters.

Each peer's personality (online or not, how fast, from where) is derived from
its public key, so it survives a panel restart and looks like the same client
every time. The counters seeded on first sight give the dashboard some history
to draw instead of a flat zero line.
"""

import random
import threading
import time

from . import conf as conf_mod
from . import keys, paths
from .controller import Dump, PeerDump
from .errors import ToolError

MOCK_TOOLS_VERSION = "amneziawg-tools v3.1.20260812 - https://amnezia.org (mock)"
MOCK_MODULE_VERSION = "3.0.0 (mock)"

# Roughly this share of peers is connected at any time.
ONLINE_SHARE = 0.6

# A live tunnel re-handshakes about every two minutes, which keeps a peer under
# the default 180s online threshold between polls.
HANDSHAKE_EVERY = 120

# Rates in bytes/sec. From the server's side rx is the client's upload and tx
# its download, so tx is the big one.
RATE_RX = (1.5 * 1024, 8.0 * 1024)
RATE_TX = (18.0 * 1024, 120.0 * 1024)
JITTER = (0.55, 1.45)

# Seed for the counters a peer is first seen with: minutes to hours of use.
HISTORY_SECONDS = (600, 36000)

# Longest gap we will bill for in one step. Without it a process that was
# suspended for an hour, or a test that fakes the clock, adds a gigabyte in a
# single tick and the dashboard shows a rate nobody has.
MAX_STEP = 10.0

# Shortest gap that moves a counter at all. The collector polls seconds apart,
# so this only affects back-to-back calls, where a real kernel would report the
# same numbers twice.
MIN_STEP = 0.25


class _PeerState:
    """Synthetic live state for one public key."""

    def __init__(self, public_key: str) -> None:
        # Seeded from the key: the same client keeps the same character across
        # restarts, and two panels looking at one config agree.
        rng = random.Random(public_key)
        self.online = rng.random() < ONLINE_SHARE
        self.rate_rx = rng.uniform(*RATE_RX)
        self.rate_tx = rng.uniform(*RATE_TX)
        self.endpoint = (
            f"{rng.randint(11, 203)}.{rng.randint(0, 255)}."
            f"{rng.randint(0, 255)}.{rng.randint(1, 254)}:{rng.randint(1024, 65000)}"
        )
        now = time.time()
        history = rng.uniform(*HISTORY_SECONDS)
        self.rx = self.rate_rx * history
        self.tx = self.rate_tx * history
        if self.online:
            self.handshake = int(now - rng.randint(0, HANDSHAKE_EVERY - 1))
        elif rng.random() < 0.3:
            self.handshake = 0  # never connected
        else:
            self.handshake = int(now - rng.randint(600, 172800))

    def advance(self, elapsed: float, now: float) -> None:
        if not self.online:
            return
        jitter = random.uniform(*JITTER)
        self.rx += self.rate_rx * elapsed * jitter
        self.tx += self.rate_tx * elapsed * jitter
        if now - self.handshake >= HANDSHAKE_EVERY:
            self.handshake = int(now)

    def reset_counters(self) -> None:
        self.rx = 0.0
        self.tx = 0.0
        self.handshake = 0


class MockController:
    """BaseController with no kernel behind it."""

    def __init__(self, iface: str | None = None) -> None:
        self._iface = iface
        self._peers: dict[str, _PeerState] = {}
        self._removed: set[str] = set()
        self._allowed: dict[str, str] = {}
        self._up = True
        self._tick = time.time()
        self._private_key: str | None = None
        # gunicorn serves requests on threads; the collector polls on its own.
        self._lock = threading.Lock()

    @property
    def iface(self) -> str:
        return self._iface or paths.iface()

    # ------------------------------------------------------------- discovery

    def available(self) -> bool:
        return True

    def tools_version(self) -> str | None:
        return MOCK_TOOLS_VERSION

    def module_version(self) -> str | None:
        return MOCK_MODULE_VERSION

    def module_version_on_disk(self) -> str | None:
        # The mock is never mid-upgrade, so the loaded module and the file it
        # came from are the same one.
        return MOCK_MODULE_VERSION

    def module_loaded(self) -> bool:
        return True

    def iface_up(self) -> bool:
        return self._up

    # ------------------------------------------------------------------ read

    def show_dump(self) -> Dump | None:
        if not self._up:
            return None
        conf = self._read_conf()
        with self._lock:
            self._advance()
            dump = Dump(
                private_key=self._interface_key(conf),
                public_key=keys.pubkey(self._interface_key(conf)),
                listen_port=_int(conf.interface.get("ListenPort")),
                fwmark="",
                peers=[],
            )
            for peer in self._live_peers(conf):
                state = self._peers.get(peer.public_key)
                if state is None:
                    state = _PeerState(peer.public_key)
                    self._peers[peer.public_key] = state
                dump.peers.append(
                    PeerDump(
                        public_key=peer.public_key,
                        preshared_key=peer.preshared_key or "",
                        endpoint=state.endpoint if state.handshake else "",
                        allowed_ips=self._allowed.get(peer.public_key, peer.allowed_ips),
                        latest_handshake=state.handshake,
                        rx=int(state.rx),
                        tx=int(state.tx),
                        keepalive=(
                            str(peer.persistent_keepalive) if peer.persistent_keepalive else "off"
                        ),
                    )
                )
            return dump

    # --------------------------------------------------------------- mutate

    def syncconf(self, stripped_text: str) -> None:
        """Adopt a stripped config as the interface's peer set.

        The real tool fails on a downed interface, so this one does too: a store
        that forgets to check finds out in mock, not in production.
        """
        if not self._up:
            raise ToolError(
                ["awg", "syncconf", self.iface],
                message=f"{self.iface} is down; bring the tunnel up before applying changes",
            )
        synced = {
            peer.public_key for peer in conf_mod.parse_conf(stripped_text).peers if peer.public_key
        }
        on_disk = self._conf_keys()
        with self._lock:
            self._removed -= synced
            # Anything the config still lists but the sync left out (a disabled
            # peer, an explicit exclusion) is not on the interface any more.
            for public_key in on_disk - synced:
                self._removed.add(public_key)
                self._peers.pop(public_key, None)

    def set_peer(self, pub: str, *, allowed_ips: str | None = None, psk: str | None = None) -> None:
        with self._lock:
            self._removed.discard(pub)
            if allowed_ips is not None:
                self._allowed[pub] = allowed_ips

    def remove_peer(self, pub: str) -> None:
        with self._lock:
            self._removed.add(pub)
            self._peers.pop(pub, None)
            self._allowed.pop(pub, None)

    def up(self) -> None:
        with self._lock:
            self._up = True
            self._tick = time.time()

    def down(self) -> None:
        with self._lock:
            self._up = False

    def restart(self) -> None:
        """Down then up, counters back to zero.

        The kernel's transfer counters really do restart here, so the mock does
        the same: it is the only way to exercise the traffic.db epoch handling
        without a kernel module.
        """
        self.down()
        with self._lock:
            for state in self._peers.values():
                state.reset_counters()
        self.up()

    # The real controller drives systemd for these two and awg-quick for up and
    # down, because systemd's idea of the unit has to keep up with the tunnel.
    # Nothing here has an idea of anything, so they are the same act.
    def service_start(self) -> None:
        self.up()

    def service_stop(self) -> None:
        self.down()

    # ------------------------------------------------------------- features

    def features(self) -> dict:
        return {
            "tools_version": MOCK_TOOLS_VERSION,
            "module_version": MOCK_MODULE_VERSION,
            "module_version_on_disk": MOCK_MODULE_VERSION,
            "module_loaded": True,
            "header_ranges": True,
            "imitation_packets": True,
            "header_protection_key": True,
            "content_padding": True,
            "timers": True,
            "random_trailers": True,
            "unknown": [],
        }

    def service_state(self) -> dict:
        return {
            "unit": f"awg-quick@{self.iface}",
            "active": "active" if self._up else "inactive",
            "enabled": "enabled",
        }

    # ------------------------------------------------------------- internals

    def _read_conf(self) -> conf_mod.ServerConf:
        """The config as it is on disk right now. Missing is empty, never fatal."""
        try:
            text = paths.server_conf().read_text(encoding="utf-8")
        except OSError:
            return conf_mod.ServerConf()
        return conf_mod.parse_conf(text)

    def _live_peers(self, conf: conf_mod.ServerConf) -> list[conf_mod.Peer]:
        """Peers the kernel would hold: named or not, but neither disabled nor removed."""
        return [
            peer
            for peer in conf.peers
            if peer.public_key and peer.disabled_at is None and peer.public_key not in self._removed
        ]

    def _conf_keys(self) -> set[str]:
        return {peer.public_key for peer in self._read_conf().peers if peer.public_key}

    def _interface_key(self, conf: conf_mod.ServerConf) -> str:
        """The server private key, or a stable made-up one when there is no config."""
        found = conf.interface.get("PrivateKey")
        if found and keys.is_key(found):
            return found
        if self._private_key is None:
            self._private_key = keys.genkey()
        return self._private_key

    def _advance(self) -> None:
        now = time.time()
        elapsed = max(now - self._tick, 0.0)
        if elapsed < MIN_STEP:
            # A real interface counts whole packets, so two dumps taken in the
            # same instant report the same numbers. Modelling traffic as a
            # continuous rate would invent a few bytes between them - enough to
            # make the counters non-zero immediately after a restart, which is
            # exactly the case traffic.db's epoch handling has to see as zero.
            # The tick is left alone, so the skipped time is billed next pass.
            return
        self._tick = now
        for state in self._peers.values():
            state.advance(min(elapsed, MAX_STEP), now)


def _int(value: str | None) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return 0
