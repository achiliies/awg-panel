"""A certificate that exists for the length of one demo and is then deleted.

`make demo DEMO_TLS=self` runs this, and nothing else does. The target serves a
panel on every interface so it can be looked at from another machine, and by
default it serves it in the clear: the admin password the target generates and
prints crosses whatever network is between the two, along with every private
key on the clients page and the session cookie for the rest of the run. "It is
only a demo" is not an argument about the wire - the wire does not know that -
and on the LAN a demo is actually shown over, the whole exchange is readable by
anything on it.

This is one of the two answers to that. The other is `DEMO_TLS=<cert>` with a
certificate somebody already holds, which is the better one wherever it is
available: a demo shown to other people over a certificate a browser already
trusts teaches them nothing about clicking past warnings. This command is for
where that is not available - a box with no name, reached by IP, that will not
exist in an hour and should not be asking a certificate authority for anything
on its behalf. What it buys is the encryption, not the identity, and encryption
is the half that was missing.

The identity half is handed to the person instead. The fingerprint printed at
the end is the one the browser shows behind "this connection is not private",
so somebody who cares can compare the two and know they are talking to the
panel that printed it rather than to whatever else answered on that address.
Nobody is made to: a browser warning that cannot be checked is a warning that
gets clicked through, and one that can be is the difference between a demo that
is merely encrypted and one that is also authenticated.

Everything here is generated per run and lives in the demo sandbox, which the
target deletes at both ends. A key kept between runs is a key that outlives the
reason it existed, sitting in a checkout, belonging to nobody - the same
argument the sandbox itself is deleted for. Regenerating costs milliseconds.

The addresses come from the caller, because `hostname -I` is what the Makefile
already builds the URLs and the CSRF origins from, and a certificate that
covers a different set than the URLs point at is a certificate that produces a
second, scarier warning about the name not matching. Loopback is always
included so a tunnelled demo works without arguments.

Not a production tool. The panel takes a certificate and a key from
/etc/awg-panel.env, and on a server those come from a certificate authority -
Let's Encrypt or an internal one - not from a command that signs its own.
"""

import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.core.management.base import BaseCommand, CommandError, CommandParser

# Always covered, whatever the caller passes. `make demo DEMO_TLS=self
# DEMO_HOST=127.0.0.1` plus an ssh tunnel is the documented way to run this
# without exposing it, and it must not need a --host of its own to work.
#
# The addresses and not the name "localhost", which would seem the obvious thing
# to add beside them. A certificate that carries any DNS name at all changes how
# the panel's own settings page behaves: saving anything sends the admin to the
# name on the certificate whenever the address they arrived at is not on it, on
# the reasoning that a certificate names where the panel really lives. For a
# demo the reasoning is backwards - it lives wherever it was reached from - and
# the result would be every remote viewer bounced to localhost, meaning their
# own machine. With addresses alone the panel finds no name to prefer and keeps
# the one they came in on, which is right for every way this target is reached.
LOOPBACK = ("127.0.0.1", "::1")

# Long enough that a demo left up over a weekend still opens, short enough that
# a copy somebody rescued from a sandbox stops working soon after. Neither
# number is load-bearing: the files are deleted when the run ends, and this is
# only about the copy that escaped that.
DEFAULT_DAYS = 7

# The clock on the machine showing the demo and the clock on the machine looking
# at it are not the same clock, and a certificate that is not valid yet is
# refused exactly like an expired one.
BACKDATE = dt.timedelta(minutes=5)


class Command(BaseCommand):
    help = "Write a self-signed certificate and key for one demo run"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--cert", required=True, help="where to write the certificate (PEM)")
        parser.add_argument("--key", required=True, help="where to write the private key (PEM)")
        parser.add_argument(
            "--host",
            action="append",
            default=[],
            metavar="ADDR",
            help="an address or name the certificate must cover; repeatable",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_DAYS,
            help=f"how long the certificate is valid (default {DEFAULT_DAYS})",
        )

    def handle(self, *args: object, **options: object) -> None:
        days = int(options["days"])
        if days < 1:
            raise CommandError("--days must be at least 1")

        names = subject_alt_names([*LOOPBACK, *options["host"]])
        key = ec.generate_private_key(ec.SECP256R1())
        cert = _certificate(key, names, days)

        # The key first, and with its mode set by open() rather than after the
        # write: a key that is world-readable for the microsecond between the
        # two is one an unlucky demo shares with everybody on the box.
        _write(Path(options["key"]), _key_pem(key), 0o600)
        _write(Path(options["cert"]), cert.public_bytes(serialization.Encoding.PEM), 0o644)

        self._report(cert, days)

    def _report(self, cert: x509.Certificate, days: int) -> None:
        """The fingerprint, laid out the way a browser lays it out.

        Two lines of sixteen bytes, uppercase, colon-separated. Chrome and
        Firefox both show it that way, and the point of printing it at all is
        that somebody can hold the two up against each other without having to
        transcribe anything.
        """
        pairs = [f"{byte:02X}" for byte in cert.fingerprint(hashes.SHA256())]
        self.stdout.write(f"  cert         self-signed, this run only, {days} days")
        self.stdout.write(f"               sha-256  {':'.join(pairs[:16])}")
        self.stdout.write(f"                        {':'.join(pairs[16:])}")


def subject_alt_names(hosts: list[str]) -> list[x509.GeneralName]:
    """The SAN entries for `hosts`, in order, without repeats.

    An address has to go in as an IPAddress rather than a DNSName: a browser
    asked for https://10.0.0.2/ matches the address entries and ignores the
    names entirely, so a certificate carrying "10.0.0.2" as a DNS name covers
    nothing at all - which is precisely the certificate a demo reached by IP
    would otherwise get.
    """
    seen: set[str] = set()
    names: list[x509.GeneralName] = []
    for raw in hosts:
        host = raw.strip().rstrip(".")
        if not host or host in seen:
            continue
        seen.add(host)
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def _certificate(
    key: ec.EllipticCurvePrivateKey, names: list[x509.GeneralName], days: int
) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    # No organisation, no country, no email. The subject of a self-signed
    # certificate is a claim nobody checks, and inventing one only makes the
    # browser's warning look more like something it is not.
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AWG Panel demo")])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        # Not a certificate authority. It signs itself and nothing else, and
        # saying so is what stops a browser that was talked into trusting this
        # file from accepting anything else it ever signs.
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # digital_signature alone: an EC key in TLS signs the handshake, it
        # never has a session key encrypted to it, so key_encipherment would be
        # a use this key cannot be put to.
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _write(path: Path, data: bytes, mode: int) -> None:
    """Write `data` to `path`, never letting it exist with wider permissions.

    O_CREAT's mode is ignored for a file that already exists - the previous
    run's, if one was killed hard enough to leave the sandbox behind - so the
    mode is applied again afterwards rather than trusted to the open.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)
