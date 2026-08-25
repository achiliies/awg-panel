"""The demo's certificate is a real one, and covers the addresses it is shown at.

`make demo` is the one target that puts the panel on every interface, and the
certificate this command writes is the only thing between the password that
target prints and everything on the network in between. Nothing else in the
suite looks at it: the target is run by hand, and a certificate that is subtly
wrong does not fail - it produces a browser warning somebody clicks through,
which is the same warning a certificate that is right produces.

Two of these are about the failures that warning hides. A key that does not
belong to its certificate is a panel that never comes up, which at least
announces itself; a certificate carrying "10.0.0.2" as a DNS name is worse,
because it covers everything except the addresses the demo is actually reached
at, and reads as one more thing to dismiss.

test_gunicorn_config.py already proves gunicorn serves TLS from a certificate
and a key. What is proved here is that these two files are ones it can serve.
"""

import ipaddress
import ssl
from io import StringIO
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.panel import certs
from apps.panel.management.commands.democert import LOOPBACK


def write(directory: Path, *hosts: str, days: int = 7) -> tuple[Path, Path, str]:
    """Run the command into `directory`; return the two paths and what it printed."""
    cert = directory / "demo.crt"
    key = directory / "demo.key"
    out = StringIO()
    call_command(
        "democert",
        "--cert",
        str(cert),
        "--key",
        str(key),
        "--days",
        str(days),
        *[arg for host in hosts for arg in ("--host", host)],
        stdout=out,
    )
    return cert, key, out.getvalue()


def names(cert: Path) -> x509.SubjectAlternativeName:
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    return parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value


def covered(cert: Path) -> set[str]:
    """Every SAN entry as the string it was asked for, addresses and names alike."""
    san = names(cert)
    return {str(value) for value in san.get_values_for_type(x509.IPAddress)} | set(
        san.get_values_for_type(x509.DNSName)
    )


def test_the_key_belongs_to_the_certificate(tmp_path):
    cert, key, _ = write(tmp_path)
    assert certs.key_matches_certificate(str(cert), str(key)) is True


def test_an_address_is_covered_as_an_address(tmp_path):
    """A browser asked for https://10.0.0.2/ matches IP entries and nothing else.

    So an address that went in as a DNS name would produce a certificate
    covering none of the URLs the target prints - the one mistake here that
    still looks entirely fine to every check that only parses the file.
    """
    cert, _, _ = write(tmp_path, "10.0.0.2")
    san = names(cert)
    assert ipaddress.ip_address("10.0.0.2") in san.get_values_for_type(x509.IPAddress)
    assert "10.0.0.2" not in san.get_values_for_type(x509.DNSName)


def test_a_name_is_covered_as_a_name(tmp_path):
    cert, _, _ = write(tmp_path, "demo.example.internal")
    assert "demo.example.internal" in names(cert).get_values_for_type(x509.DNSName)


def test_loopback_is_covered_without_being_asked(tmp_path):
    """`make demo DEMO_HOST=127.0.0.1` behind an ssh tunnel passes no --host at all."""
    cert, _, _ = write(tmp_path)
    assert set(LOOPBACK) <= covered(cert)


def test_an_address_named_twice_is_listed_once(tmp_path):
    """`hostname -I` on a box that reports loopback, or a --host somebody repeated."""
    cert, _, _ = write(tmp_path, "127.0.0.1", "10.0.0.2", "10.0.0.2")
    addresses = names(cert).get_values_for_type(x509.IPAddress)
    assert addresses.count(ipaddress.ip_address("10.0.0.2")) == 1
    assert addresses.count(ipaddress.ip_address("127.0.0.1")) == 1


def test_the_pair_can_actually_serve_tls(tmp_path):
    """The question gunicorn asks: does an SSL context accept these two files?

    It is asked here by the same code that would ask it inside the demo, where
    the answer arrives as a panel that never came up.
    """
    cert, key, _ = write(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))


def test_a_client_checking_the_address_completes_the_handshake(tmp_path):
    """End to end, with the address verified the way a browser verifies it.

    Held in memory rather than over a socket: no port, no thread, and nothing
    that can hang the suite. `server_hostname="127.0.0.1"` is what turns the
    IP-versus-DNS entry above into a connection that either happens or does not.
    """
    cert, key, _ = write(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    client_ctx = ssl.create_default_context(cafile=str(cert))

    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    server = server_ctx.wrap_bio(to_server, to_client, server_side=True)
    client = client_ctx.wrap_bio(to_client, to_server, server_hostname="127.0.0.1")

    pending = {"client": client, "server": server}
    for _ in range(20):
        for side in list(pending):
            try:
                pending[side].do_handshake()
            except ssl.SSLWantReadError:
                continue
            del pending[side]
        if not pending:
            return
    pytest.fail("the handshake never completed")


def test_the_key_is_readable_only_by_its_owner(tmp_path):
    cert, key, _ = write(tmp_path)
    assert key.stat().st_mode & 0o777 == 0o600
    assert cert.stat().st_mode & 0o777 == 0o644


def test_a_key_left_behind_by_a_killed_run_is_not_left_wide_open(tmp_path):
    """O_CREAT's mode is ignored for a file that already exists.

    Which is the file a `kill -9` leaves in the sandbox: the next run opens it,
    writes a fresh key into it, and would inherit the dead run's permissions if
    the mode were not applied again afterwards.
    """
    stale = tmp_path / "demo.key"
    stale.write_bytes(b"not a key\n")
    stale.chmod(0o644)
    _, key, _ = write(tmp_path)
    assert key.stat().st_mode & 0o777 == 0o600


def test_every_run_gets_its_own_key(tmp_path):
    """The demo's whole claim is that nothing it made outlives it."""
    first, first_key, _ = write(tmp_path / "one")
    second, second_key, _ = write(tmp_path / "two")
    assert first_key.read_bytes() != second_key.read_bytes()
    assert first.read_bytes() != second.read_bytes()


def test_the_printed_fingerprint_is_the_certificate(tmp_path):
    """It is printed to be held up against the browser's, so it has to match."""
    cert, _, output = write(tmp_path)
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    printed = "".join(word for word in output.split() if ":" in word).replace(":", "")
    assert printed == parsed.fingerprint(hashes.SHA256()).hex().upper()


def test_it_is_valid_now_and_for_the_days_asked_for(tmp_path):
    cert, _, _ = write(tmp_path, days=3)
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    hours = (parsed.not_valid_after_utc - parsed.not_valid_before_utc).total_seconds() / 3600
    # The days asked for, plus the few minutes of backdating that cover the
    # showing machine and the watching machine disagreeing about the time.
    assert 3 * 24 <= hours <= 3 * 24 + 1


def test_a_certificate_valid_for_no_time_at_all_is_refused(tmp_path):
    with pytest.raises(CommandError):
        write(tmp_path, days=0)
