"""Shared fixtures: every test runs against a throwaway copy of a real server.

The panel writes to /etc/amnezia/amneziawg and /var/lib/awg-panel, which on a
developer's machine is either absent or a live VPN. So the environment is
redirected into tmp_path for every test, autouse, with no way to opt out: a test
that forgot to ask for a fixture must still not be able to touch a running
server's config.

Nothing here imports Django. These are core tests over plain files and they have
to keep running under bare `pytest` on a box where the panel was never
installed.
"""

import datetime
import ipaddress
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

FIXTURES = Path(__file__).parent / "fixtures"

# clients.env exactly as install.sh writes it for SUBNET=10.13.13 and MTU=1400,
# the run that also produced fixtures/server.conf. The two belong together: a
# client config cannot be rendered without an endpoint host, and the subnet base
# here is what the address allocator reads.
#
# The header is the pre-CLI-removal one on purpose, matching the field: every
# server installed before that upgrade still has this line, and nothing reads it.
CLIENTS_ENV = """\
# awg-client settings. Sourced by bash - every value must be QUOTED.
ENDPOINT_HOST="203.0.113.10"    # blank = auto-detect
ENDPOINT_PORT=""                # blank = ListenPort from the server config
CLIENT_DNS="8.8.8.8, 8.8.4.4"
CLIENT_MTU="1400"
CLIENT_ALLOWED_IPS="0.0.0.0/0"  # "10.13.13.0/24" for split tunnel
SUBNET_BASE="10.13.13"
KEEPALIVE="25"
"""


@dataclass(frozen=True)
class GoldenClient:
    """The inputs behind fixtures/expected_client.conf.

    add_client() generates its own keys and picks its own address, so a golden
    comparison has to pin all three: monkeypatch keys.genkey/genpsk to return
    these, and the peer lands on the next free address in the fixture, .4.
    """

    name: str
    ip: str
    private_key: str
    preshared_key: str
    text: str


@pytest.fixture(autouse=True)
def awg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path in awg.paths at tmp_path and turn the mock controller on.

    AWG_IFACE is pinned too: an exported value in the developer's shell would
    otherwise change which file name the tests write.
    """
    conf = tmp_path / "amneziawg"
    data = tmp_path / "awg-panel"
    run = tmp_path / "run"
    (conf / "clients").mkdir(parents=True, mode=0o700)
    data.mkdir(mode=0o700)
    # Created up front because systemd's RuntimeDirectory= creates the real one
    # before either unit starts, so no code under test has to.
    run.mkdir(mode=0o700)

    monkeypatch.setenv("AWG_IFACE", "awg0")
    monkeypatch.setenv("AWG_CONF_DIR", str(conf))
    monkeypatch.setenv("AWG_PANEL_DATA", str(data))
    monkeypatch.setenv("AWG_PANEL_RUN", str(run))
    # Without this a settings save would rewrite the real /etc/awg-panel.env.
    monkeypatch.setenv("AWG_PANEL_ENV", str(tmp_path / "awg-panel.env"))
    monkeypatch.setenv("AWG_MOCK", "1")
    return conf


@pytest.fixture(autouse=True)
def no_systemctl(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what a restore would do to the collector instead of doing it.

    Every path in this suite is redirected into tmp_path, but a systemd unit is
    not a path: apps.panel.backup names awg-panel-collector.service outright, so
    a restore test run on the server this panel is installed on stops and starts
    the operator's live collector - and the suite is most likely to be run
    exactly there. Nothing else about the test even looks different, which is
    what makes it worth closing rather than remembering.

    Imported inside the fixture: this module is loaded for the core tests too,
    which have to keep working without Django set up.
    """
    calls: list[str] = []

    def record(action: str) -> bool:
        calls.append(action)
        return False  # "no collector here", which is the truth on a dev box

    try:
        from apps.panel import backup
    except Exception:  # pragma: no cover - no Django, so no restore tests either
        return calls
    monkeypatch.setattr(backup, "_collector", record)
    return calls


@pytest.fixture(autouse=True)
def fresh_settings():
    """Start and end every test with an empty settings cache.

    settings_store holds its rows in the process for a few seconds so that a
    dashboard poll does not go to the database for every request. That is right
    on a server and wrong in a suite: each test gets its own database, but they
    all share this process, so a test that sets a value leaves it readable by the
    next test that never set one - which passes, for the wrong reason, until the
    day somebody runs it on its own and it does not.

    Imported inside the fixture like `no_systemctl` above: this file is loaded
    for the core tests too, which run without Django configured.
    """
    try:
        from apps.panel import settings_store
    except Exception:  # pragma: no cover - no Django, so no settings either
        yield
        return
    settings_store.invalidate()
    yield
    settings_store.invalidate()


@pytest.fixture
def conf_dir(awg_env: Path) -> Path:
    """The tmp stand-in for /etc/amnezia/amneziawg."""
    return awg_env


@pytest.fixture
def data_dir(awg_env: Path) -> Path:
    """The tmp stand-in for /var/lib/awg-panel."""
    return awg_env.parent / "awg-panel"


@pytest.fixture
def clients_env(conf_dir: Path) -> Path:
    """Install clients.env and hand back its path."""
    target = conf_dir / "clients.env"
    target.write_text(CLIENTS_ENV, encoding="utf-8")
    target.chmod(0o600)
    return target


@pytest.fixture
def server_conf(conf_dir: Path, clients_env: Path) -> Path:
    """Install fixtures/server.conf as awg0.conf and hand back its path.

    clients.env comes with it because install.sh writes both in the same step and
    nothing downstream of the server config works without the endpoint and the
    subnet base it carries.
    """
    target = conf_dir / "awg0.conf"
    shutil.copyfile(FIXTURES / "server.conf", target)
    target.chmod(0o600)
    return target


@pytest.fixture
def server_conf_text() -> str:
    """The fixture config as bytes-on-disk, for round-trip comparisons."""
    return (FIXTURES / "server.conf").read_text(encoding="utf-8")


@pytest.fixture
def make_certificate():
    """Build self-signed certificates on demand: (cert path, key path, key material).

    Real ones, not stubs. Both places that read a certificate parse it with
    cryptography and answer "nothing" for a file they cannot make sense of, so a
    plausible-looking text file would pass the assertions for the wrong reason.

    `key_of` reuses another pair's private key, which is how a mismatched pair is
    built: two files that are each valid and do not belong together.

    A name that parses as an address is written as an IP SAN, because that is
    what a real certificate for an address carries and it is the whole of what
    tells one apart from a certificate for a name. Writing 198.51.100.7 as a
    DNS entry would build a file no certificate authority issues, and the code
    under test would read a name off it and be right to.
    """

    def build(directory: Path, *names: str, key_of=None):
        key = key_of or ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, names[0])])
        # Fixed dates: nothing in the panel checks validity, and a certificate
        # generated from the clock is a test that expires.
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))
            .not_valid_after(datetime.datetime(2034, 1, 1, tzinfo=datetime.UTC))
            .add_extension(
                x509.SubjectAlternativeName([_san(name) for name in names]), critical=False
            )
            .sign(key, hashes.SHA256())
        )

        directory.mkdir(parents=True, exist_ok=True)
        stem = names[0].replace("*", "wildcard")
        cert_path = directory / f"{stem}.crt"
        key_path = directory / f"{stem}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return cert_path, key_path, key

    return build


def _san(name: str) -> x509.GeneralName:
    """An address as an IPAddress entry, anything else as a DNS one."""
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


@pytest.fixture
def golden_client() -> GoldenClient:
    """Key material and expected output for the add_client golden test."""
    return GoldenClient(
        name="phone",
        ip="10.13.13.4",
        private_key="qCOkQo0ROacEtytL5yOJl5jX88NlRppfkcn1fo2wJ3g=",
        preshared_key="elstKOnuCKHgq/aWZnUvVHhujFKUOwhOrxEMJSHVwMw=",
        text=(FIXTURES / "expected_client.conf").read_text(encoding="utf-8"),
    )
