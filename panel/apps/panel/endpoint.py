"""Which address a client config carries when the panel hands it over.

A server is set up at an IP address, so that is what was written into
every client's ``Endpoint`` line and what sits in every file under
/etc/amnezia/amneziawg/clients. Later a certificate arrives, the panel is put
behind a name, and from then on the name is the address people are given - but
the configs still name the IP, and re-issuing all of them would give every
client new keys for a change that is only about how they spell the same host.

So the swap happens on the way out. The files on disk are never touched: the
config download, the QR image and the export archive are rendered from the file
and the host is replaced in that copy. Anyone reading the file over SSH still
sees the IP, which is the truth about what the server was configured with, and a
panel with this switched off behaves exactly as it did before.

Only the host changes. The port comes from the server's own config, the keys and
everything else are copied verbatim, and a line this module cannot make sense of
is left exactly as it was found - a config that reaches a phone unchanged is a
config that still connects.

Nothing here reads the settings itself: `defaults` validates against it and
`settings_store` is what holds the values, so taking them as an argument is what
keeps the three from importing each other in a circle.
"""

import re
from collections.abc import Mapping

from . import certs

# "the address the server config already uses", which is nearly always the IP
# the installer detected, and "the name on the panel's certificate".
MODE_IP = "ip"
MODE_DOMAIN = "domain"
MODES = (MODE_IP, MODE_DOMAIN)

# `Endpoint = host:port`, with whatever spacing the writer used preserved. The
# value is taken as one token: a comment or a second word after it means this is
# not a line this module wrote, and it is left alone.
_ENDPOINT = re.compile(r"^([ \t]*Endpoint[ \t]*=[ \t]*)(\S+)([ \t]*)$", re.MULTILINE)


def certificate_domain(values: Mapping[str, str]) -> str:
    """The name on the panel's TLS certificate to hand out, or "" if there is none.

    Wildcards are skipped: `*.example.com` is a name a browser matches against,
    not one a client can dial. Addresses are skipped too, and for a reason worth
    spelling out, because Let's Encrypt issues for addresses and a panel is very
    often put behind exactly that certificate: the address on it is the address
    the client configs already carry, so handing it out swaps a host for itself.
    There is nothing to ask about there and nothing to tell the operator, and
    calling that address a domain - offering it under a box labelled "Domain",
    or refusing a save until one is typed - invents a question out of a setting
    that has no work to do. So an address certificate answers the same as no
    certificate: nothing to substitute, configs go out as they are.

    An unreadable or unparsable certificate answers "" rather than raising - it
    is the same answer as having no certificate, and the caller's job is to hand
    out a config either way.
    """
    path = (values.get("tlsCertPath") or "").strip()
    if not path:
        return ""
    for name in certs.certificate_names(path):
        if not name.startswith("*") and not certs.is_address(name):
            return name
    return ""


def wanted_host(values: Mapping[str, str]) -> str:
    """The host client configs should be handed out with, or "" to leave them alone.

    "" is the answer whenever the swap is off, and also whenever it is on but
    there is no name to swap in. The second case is worth allowing: a
    certificate can be replaced with one for a different name, or removed
    entirely, long after this setting was saved, and refusing to render a config
    at that point would take the panel's own downloads out over a preference.
    Handing over the address the file already carries is the honest fallback.
    """
    if (values.get("configEndpointMode") or MODE_IP).strip() != MODE_DOMAIN:
        return ""
    pinned = (values.get("configEndpointHost") or "").strip()
    return pinned or certificate_domain(values)


def rewrite(text: str, host: str) -> str:
    """`text` with the Endpoint host replaced by `host`. Empty `host` changes nothing."""
    if not host:
        return text

    def swap(match: re.Match[str]) -> str:
        prefix, value, trailing = match.group(1), match.group(2), match.group(3)
        # rpartition, not partition: an IPv6 literal is written [2001:db8::1]:51820
        # and every colon but the last one belongs to the address.
        _, separator, port = value.rpartition(":")
        if not separator or not port.isdigit():
            # No port to keep, so there is no way to tell which part of this is
            # the host. Whatever it is, it is not something to guess at.
            return match.group(0)
        return f"{prefix}{host}:{port}{trailing}"

    return _ENDPOINT.sub(swap, text)
