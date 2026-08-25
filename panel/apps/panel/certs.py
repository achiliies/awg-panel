"""What a TLS certificate on disk says about itself.

Two questions are worth asking before a save turns HTTPS on, because both are
answered by gunicorn a couple of seconds later and its answer is a service that
does not come back:

Does the key belong to the certificate? A mismatched pair is accepted by every
check that only stats the two files, and refused by the SSL context that is
built from them.

Which names does the certificate cover? The panel is usually administered by IP
address, while a certificate is issued for a domain. Sending the browser to
https://<ip>/ after the restart hands it a certificate that cannot match the
address it asked for, which reads exactly like a panel that died - and the admin
who was just told "this page will reconnect" has no reason to think the address
is the problem.

And how long it has left, which stopped being a background detail the day
Let's Encrypt began issuing for six days at a time. A ninety-day certificate
that quietly stops renewing is noticed by the warning mail; a six-day one is
noticed by the panel being unreachable on Thursday, so the number is read off
the file and shown while there is still time to act on it.

Nothing here raises: a certificate this module cannot parse is one the panel
knows nothing extra about, and it must not be the reason a save is refused.
gunicorn is the authority on whether a file works, and it is about to say so.
"""

import datetime as dt
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

log = logging.getLogger(__name__)


def certificate_names(path: str) -> list[str]:
    """Names the certificate at `path` covers, most specific first. [] if unreadable.

    Both kinds of name, because a panel is very often administered at an
    address and Let's Encrypt now issues for addresses. An address is carried
    as an IP SAN and not as a DNS one, so reading only DNS entries answers
    "covers nothing" about exactly the certificate somebody obtained for the
    address they are looking at - and every caller here treats "no names" as
    "nothing can be said", which turns a correct certificate into a silent one.

    Addresses come back in their canonical form rather than as written, so a
    certificate for 2001:db8::1 is comparable with a browser asking for
    2001:0db8:0000::1. Wildcards are kept exactly as they are written
    (``*.example.com``); `covers` knows how to match them and a caller looking
    for something to show a human can skip them.
    """
    cert = _load(path)
    if cert is None:
        return []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = list(san.value.get_values_for_type(x509.DNSName))
        names += [str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        # Pre-2017 certificates put the name in the subject only. Browsers stopped
        # honouring that, but reading it costs nothing and it is still what a
        # hand-rolled self-signed certificate is most likely to carry.
        names = [
            attr.value
            for attr in cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
            if isinstance(attr.value, str)
        ]
    # A wildcard is never the address to send someone to, so it sorts last.
    return sorted(names, key=lambda name: (name.startswith("*"), name))


def is_address(value: str) -> bool:
    """Is this an IP literal rather than a name?

    Asked by anything that has to tell "the certificate covers a name" from
    "the certificate covers the address the panel is already reached at", which
    are different answers for a caller looking for something to hand out.
    Brackets are tolerated because an IPv6 host read out of a URL wears them.
    """
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return _as_ip(value) is not None


def covers(names: list[str], host: str) -> bool:
    """Would a browser asking for `host` accept a certificate carrying `names`?"""
    host = host.strip().rstrip(".").lower()
    if not host:
        return False
    # An IPv6 literal arrives from a URL wearing the brackets that keep it apart
    # from the port. The certificate carries the address without them.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    asked_ip = _as_ip(host)
    for name in names:
        name = name.strip().rstrip(".").lower()
        if name == host:
            return True
        # Addresses compare as addresses: the same one has several spellings and
        # only one of them is what got written into the certificate. A wildcard
        # never matches an address, and this arm is where that is decided -
        # x509 has no such thing, and the string arm below would not match one
        # anyway.
        if asked_ip is not None:
            if _as_ip(name) == asked_ip:
                return True
            continue
        # One label, and only the leftmost one: *.example.com matches a.example.com
        # and neither example.com nor a.b.example.com. That is the rule browsers
        # apply, and guessing wider here would suppress a warning that is right.
        if name.startswith("*.") and host.count(".") == name.count(".") and host.endswith(name[1:]):
            return True
    return False


def key_matches_certificate(cert_path: str, key_path: str) -> bool | None:
    """Do these two files belong together? None when the question cannot be answered."""
    cert = _load(cert_path)
    if cert is None:
        return None
    try:
        key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        # TypeError is what an encrypted key raises, and gunicorn has nowhere to
        # type a passphrase either - but that is its refusal to make, not a
        # reason for this to claim the pair is wrong.
        log.debug("cannot read private key %s: %s", key_path, exc)
        return None
    try:
        return key.public_key().public_numbers() == cert.public_key().public_numbers()
    except (AttributeError, ValueError, TypeError) as exc:  # an algorithm without numbers
        log.debug("cannot compare %s with %s: %s", cert_path, key_path, exc)
        return None


def expires_in_days(path: str) -> int | None:
    """Whole days until the certificate at `path` expires. None if it cannot be read.

    Rounded down, and negative once it has expired, so a caller can say "two
    days left" and "expired three days ago" from the one number. Whole days
    because that is the resolution the answer is acted on at, and because a
    six-day certificate renewed on schedule sits between four and six for its
    whole life - an hours figure there would move on every page load without
    ever meaning anything different.
    """
    cert = _load(path)
    if cert is None:
        return None
    try:
        expiry = cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        expiry = cert.not_valid_after.replace(tzinfo=dt.UTC)
    return (expiry - dt.datetime.now(dt.UTC)).days


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`value` as an address, or None when it is a name rather than one."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _load(path: str) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        log.debug("cannot read certificate %s: %s", path, exc)
        return None
