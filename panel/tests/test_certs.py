"""What the panel reads off a certificate file, now that one may be for an address.

Let's Encrypt issues certificates for IP addresses, and a panel is very often
administered at one - so this is not a corner. A certificate issued for
198.51.100.7 carries that as an IP SAN and carries no DNS name whatsoever, and
every caller of `certificate_names` treats an empty list as "nothing can be
said about this file": the settings page reports a certificate covering
nothing, `covers` cannot confirm the address it is being served at, and
`_panel_host` picks a host to hand back without knowing whether it works. All
of that is silent, and all of it is wrong about a certificate that is right.

The expiry half is here for a related reason. Address certificates are issued
for six days at a time, so "the renewal timer stopped" and "the panel stopped
opening" are four days apart rather than two months, and the number has to be
readable while there is still time to do something with it.
"""

import datetime as dt
from io import StringIO
from pathlib import Path

import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from django.core.management import call_command

from apps.panel import certs


@pytest.fixture
def make_cert(tmp_path: Path):
    """Write a certificate covering `hosts`; return its path.

    democert is the one thing in the tree that already writes a certificate
    with the SAN types sorted out - an address as an IPAddress entry and a name
    as a DNSName one - which is exactly the distinction under test. It always
    adds the loopback addresses of its own accord, so assertions here ask what
    is present rather than what the whole set is.
    """

    def build(*hosts: str, days: int = 7, name: str = "c") -> Path:
        cert = tmp_path / f"{name}.crt"
        call_command(
            "democert",
            "--cert",
            str(cert),
            "--key",
            str(tmp_path / f"{name}.key"),
            "--days",
            str(days),
            *[arg for host in hosts for arg in ("--host", host)],
            stdout=StringIO(),
        )
        return cert

    return build


class TestCertificateNames:
    def test_reads_an_address_certificate(self, make_cert):
        """The case that returned nothing at all before: no DNS name anywhere."""
        names = certs.certificate_names(str(make_cert("198.51.100.7")))
        assert "198.51.100.7" in names

    def test_reads_names_and_addresses_together(self, make_cert):
        names = certs.certificate_names(str(make_cert("vpn.example.com", "198.51.100.7")))
        assert "vpn.example.com" in names
        assert "198.51.100.7" in names

    def test_addresses_come_back_canonical(self, make_cert):
        """Written one way, read back the way an address is spelled once."""
        names = certs.certificate_names(str(make_cert("2001:db8:0:0:0:0:0:1")))
        assert "2001:db8::1" in names

    def test_unreadable_file_says_nothing(self, tmp_path):
        assert certs.certificate_names(str(tmp_path / "nope.crt")) == []


class TestCovers:
    def test_matches_an_address(self, make_cert):
        names = certs.certificate_names(str(make_cert("198.51.100.7")))
        assert certs.covers(names, "198.51.100.7")
        assert not certs.covers(names, "198.51.100.8")

    def test_matches_an_address_spelled_differently(self):
        """The certificate holds one spelling; a browser may ask with another."""
        assert certs.covers(["2001:db8::1"], "2001:0db8:0000:0000:0000:0000:0000:0001")

    def test_matches_a_bracketed_literal(self):
        """An IPv6 host out of a URL still wears the brackets that fence off the port."""
        assert certs.covers(["2001:db8::1"], "[2001:db8::1]")

    def test_a_wildcard_never_matches_an_address(self):
        """There is no such thing in x509, and pretending otherwise hides a real warning."""
        assert not certs.covers(["*.example.com"], "198.51.100.7")

    def test_names_still_work(self):
        assert certs.covers(["vpn.example.com"], "vpn.example.com")
        assert certs.covers(["*.example.com"], "a.example.com")
        assert not certs.covers(["*.example.com"], "a.b.example.com")
        assert not certs.covers(["vpn.example.com"], "other.example.com")

    def test_an_empty_host_covers_nothing(self):
        assert not certs.covers(["vpn.example.com"], "")


class TestExpiresInDays:
    def test_counts_whole_days_left(self, make_cert):
        """democert dates from now, so six is what seven days minus a moment floors to."""
        assert certs.expires_in_days(str(make_cert(days=7))) == 6

    def test_a_six_day_certificate_reads_as_days_not_nothing(self, make_cert):
        assert certs.expires_in_days(str(make_cert(days=6))) == 5

    def test_goes_negative_once_expired(self, tmp_path, make_cert, monkeypatch):
        """Read a week after issue, a seven-day certificate is past rather than absent."""
        cert = make_cert(days=7)
        real_now = dt.datetime.now(dt.UTC)

        class Ahead(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_now + dt.timedelta(days=10)

        monkeypatch.setattr(certs.dt, "datetime", Ahead)
        assert certs.expires_in_days(str(cert)) < 0

    def test_unreadable_file_says_nothing(self, tmp_path):
        assert certs.expires_in_days(str(tmp_path / "nope.crt")) is None


class TestKeyMatchesCertificate:
    """The contract the module docstring states: nothing in here raises.

    cryptography 47 moved the unsupported-algorithm and unsupported-curve
    cases off ValueError and onto UnsupportedAlgorithm, which descends
    straight from Exception and so walks through a handler that names only
    ValueError. What is on the other side of that escape is _check_tls_pair
    in defaults.py, on the save that turns HTTPS on: an unusual key file
    would answer with a 500 on the settings form rather than with the "I
    cannot tell, let gunicorn decide" this is required to answer.

    Raised rather than provoked with a real file, because the algorithms that
    do it are exactly the ones a given build of OpenSSL may or may not have
    been compiled with - a test that needed one would pass or fail on the
    wheel underneath it rather than on the handler under test.
    """

    def test_a_real_pair_still_matches(self, tmp_path, make_cert):
        """The positive control: the handlers below must not swallow the answer."""
        cert = make_cert("198.51.100.7")
        assert certs.key_matches_certificate(str(cert), str(tmp_path / "c.key")) is True

    def test_a_key_this_build_cannot_load_says_nothing(self, tmp_path, make_cert, monkeypatch):
        cert = make_cert("198.51.100.7")

        def unsupported(*args, **kwargs):
            raise UnsupportedAlgorithm("this build has no such curve")

        monkeypatch.setattr(certs, "load_pem_private_key", unsupported)
        assert certs.key_matches_certificate(str(cert), str(tmp_path / "c.key")) is None

    def test_a_certificate_that_will_not_yield_a_public_key_says_nothing(
        self, tmp_path, make_cert, monkeypatch
    ):
        """The second handler: the key loaded, and the certificate is the one refusing."""
        cert = make_cert("198.51.100.7")

        class Opaque:
            def public_key(self):
                raise UnsupportedAlgorithm("this build has no such algorithm")

        monkeypatch.setattr(certs, "_load", lambda path: Opaque())
        assert certs.key_matches_certificate(str(cert), str(tmp_path / "c.key")) is None

    def test_an_unloadable_certificate_says_nothing_everywhere(self, tmp_path, monkeypatch):
        """_load is the one door all three readers go through, so it is checked once."""

        def unsupported(*args, **kwargs):
            raise UnsupportedAlgorithm("unknown signature algorithm")

        monkeypatch.setattr(certs.x509, "load_pem_x509_certificate", unsupported)
        cert = tmp_path / "c.crt"
        cert.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
        assert certs.certificate_names(str(cert)) == []
        assert certs.expires_in_days(str(cert)) is None
        assert certs.key_matches_certificate(str(cert), str(tmp_path / "c.key")) is None
