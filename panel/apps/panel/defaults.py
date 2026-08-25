"""Every panel setting: its default, what a valid value looks like, and where it lands.

One table, because three things have to agree and drifting apart is silent: the
defaults the API serves, the rules a save is checked against, and the five
settings that are also written into /etc/awg-panel.env for systemd to read.
The frontend's SETTINGS_DEFAULTS mirrors this list; changing a key here means
changing it there.

Values are stored and served as text. Coercion happens on read
(``settings_store.get_int`` / ``get_bool``) rather than in the table, so a
setting a future version tightens cannot make an old row unreadable.

Validation is strict about the five env-file settings for two reasons. A bad
port or an unreadable certificate stops the service from coming back up after
the restart that applies it, which locks the admin out of the only UI they have.
And the file is sourced by bash (`set -a; . /etc/awg-panel.env`), so a value
carrying shell metacharacters would be executed as root on the next start.
"""

import ipaddress
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from awg.errors import ValidationError
from awgui.settings import normalise_base_path

from . import certs, endpoint

# Anything else is either impossible to quote safely for a file bash sources, or
# is a value nobody legitimately needs (a certificate path with a space in it
# can be moved).
_ENV_SAFE = re.compile(r"^[A-Za-z0-9._:/@,+=-]*$")
_ENV_SAFE_HELP = "Use letters, digits and . _ - / : @ , + = with no spaces"

_BASE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# One label of a domain name: letters, digits and inner hyphens, 63 at most.
_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

THEMES = ("system", "light", "dark")


@dataclass(frozen=True)
class SettingSpec:
    """One setting. `minimum`/`maximum` bound the number, or the length of the name."""

    key: str
    default: str
    # One of the keys in _CLEANERS: "int", "port", "ip", "basepath", "file",
    # "hostname", "choice" or "language".
    kind: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    env: str = ""  # variable name in /etc/awg-panel.env; "" means panel-only
    # Whether a save may write this setting's *value* into the activity log,
    # which the log needs to say "speed limits were turned off" rather than
    # "something about speed limits changed".
    #
    # Off by default, so a setting added here is recorded by name until somebody
    # decides its value is fit to keep. What makes a value unfit is not that it
    # is secret in the cryptographic sense - none of these are - but that it
    # says how to reach this panel: the secret URL path most of all, and the
    # listen address, the port, the certificate paths and the domain handed to
    # clients after it. The log is read on screen, carried in every backup and
    # handed over in support threads, and none of those need to say where the
    # front door is. What is left is how the panel behaves, which is the half
    # somebody reads this log to reconstruct.
    logged: bool = False


SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("webListen", "0.0.0.0", "ip", env="AWG_PANEL_LISTEN"),
    SettingSpec("webPort", "2097", "port", env="AWG_PANEL_PORT"),
    SettingSpec("webBasePath", "/", "basepath", env="AWG_PANEL_BASE_PATH"),
    SettingSpec("tlsCertPath", "", "file", env="AWG_PANEL_TLS_CERT"),
    SettingSpec("tlsKeyPath", "", "file", env="AWG_PANEL_TLS_KEY"),
    # How a client config spells this server on its way out of the panel. Both
    # are read by apps.panel.endpoint when a config is downloaded, shown as a QR
    # code or exported; neither touches a file, so neither restarts anything.
    SettingSpec(
        "configEndpointMode", endpoint.MODE_IP, "choice", choices=endpoint.MODES, logged=True
    ),
    # Blank means "whichever name the certificate carries", so a renewal for a
    # different name is followed without anyone editing this.
    SettingSpec("configEndpointHost", "", "hostname", maximum=253),
    # Ten minutes to thirty days. Shorter is a support ticket every morning;
    # longer is a stolen laptop that stays signed in for a season.
    SettingSpec("sessionMaxAge", "86400", "int", minimum=600, maximum=2592000, logged=True),
    SettingSpec("loginRateLimit", "5", "int", minimum=1, maximum=100, logged=True),
    SettingSpec("theme", "system", "choice", choices=THEMES, logged=True),
    SettingSpec("language", "en", "language", logged=True),
    # The dashboard polls at this rate and the collector samples at it; below a
    # second it is all overhead, above a minute the live view stops being live.
    SettingSpec("trafficPollSec", "2", "int", minimum=1, maximum=60, logged=True),
    SettingSpec("onlineThresholdSec", "180", "int", minimum=30, maximum=3600, logged=True),
    # How often the collector re-reads the config and every client's limits. Not
    # how quickly a limit is applied: a client spending its way to one is caught
    # on the poll rate above, and a limit an admin changes is applied by the
    # request that changed it. This is the interval on which a change made
    # outside the panel - a hand edit over SSH, a restore, a bring-up that
    # re-admitted everyone - is noticed. Widening it costs freshness against
    # those, and one thing more: a limit lowered to somewhere the client has not
    # reached yet still trips on this clock rather than on the poll, because the
    # figure the poll spends is only rebuilt here.
    SettingSpec("enforceIntervalSec", "60", "int", minimum=10, maximum=3600, logged=True),
    # Whether bandwidth limits work at all on this server. Off means no client is
    # shaped and nothing is attached to any interface, whatever a client's own
    # numbers say - and the numbers are kept, so switching it back on puts every
    # one of them back.
    #
    # This used to be the server's link capacity in megabits, with 0 for off, and
    # the capacity turned out to be worth nothing: HTB holds a class to its own
    # ceiling regardless of what is above it, so the figure only ever decided an
    # error message. It could not be discovered either - a virtio NIC reports
    # 10 Gbit while sitting behind a 200 Mbit allowance - so it was an operator's
    # guess at their own uplink, and a guess on the low side capped the whole
    # tunnel. awg.shaper hangs everything under a fixed root instead.
    SettingSpec("shaperOn", "0", "bool", logged=True),
    # What a client created from now on is given, in megabits per second, when
    # the request that creates it names no limit of its own. 0 is "no limit",
    # which is what every server starts out doing.
    #
    # Only new clients. Changing it is not a decision about the ones already
    # here, and a setting that silently re-rated four thousand people because a
    # default moved would be the worst kind of surprise. The settings page has a
    # button that does exactly that, once, when it is asked to.
    SettingSpec("shaperDefaultDownMbps", "0", "int", minimum=0, maximum=1_000_000, logged=True),
    SettingSpec("shaperDefaultUpMbps", "0", "int", minimum=0, maximum=1_000_000, logged=True),
    # Whether to shape upload as well as download. Off by default and
    # deliberately a decision rather than a consequence: a client's upload has
    # been through MASQUERADE by the time it can be shaped, so the only place to
    # do it is the WAN's own egress - and an HTB root there is on the path of
    # every packet this machine sends, the panel's replies and the operator's SSH
    # session included. Download needs none of that.
    SettingSpec("shaperUpload", "0", "bool", logged=True),
    # Which interface that is. Blank means the one holding the default route,
    # which is the one install.sh masquerades out of and is right on every server
    # with a single uplink.
    SettingSpec("shaperWanIface", "", "iface", logged=True),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}

DEFAULTS: dict[str, str] = {spec.key: spec.default for spec in SPECS}

# Setting key -> the variable install-panel.sh writes into /etc/awg-panel.env.
ENV_KEYS: dict[str, str] = {spec.key: spec.env for spec in SPECS if spec.env}

# Changing one of these cannot take effect in the running process: the listen
# socket, the URL prefix and the TLS context are all decided at startup.
RESTART_KEYS: frozenset[str] = frozenset(ENV_KEYS)

# Settings whose value the activity log may quote; see SettingSpec.logged.
LOGGED_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.logged)


def logged_changes(changed: Mapping[str, str]) -> list[str]:
    """``["shaperOn=0", "theme=dark"]`` for the settings in `changed` that may be quoted.

    One flat list of strings rather than a mapping, because that is the whole of
    what an event detail may hold - see apps.events.recorder, which drops
    anything richer on the floor rather than risk an object reaching that column.
    Splitting on the first "=" gives the pair back, and a value containing one
    keeps it.

    A setting that is not quotable is simply absent, and stays named by the
    `keys` beside this: the log says the secret path was changed and never what
    it was changed to. In the order the settings page shows them, so a save of
    six reads down the page rather than alphabetically.
    """
    return [
        f"{spec.key}={changed[spec.key]}" for spec in SPECS if spec.logged and spec.key in changed
    ]


def validate_settings(
    changes: Mapping[str, object], current: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Clean a partial settings update, or raise ValidationError with per-field messages.

    `current` is the full settings map the change applies to; it is needed for
    the rules that span two fields. Returns only the keys that were supplied,
    normalised to the form that will be stored.
    """
    if not isinstance(changes, Mapping):
        raise ValidationError("Send the settings to change as a JSON object.")

    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}
    for key, raw in changes.items():
        spec = SPEC_BY_KEY.get(str(key))
        if spec is None:
            errors[str(key)] = f"This version of the panel has no setting called '{key}'."
            continue
        if raw is None:
            continue  # "not supplied", the same as leaving the key out
        try:
            cleaned[spec.key] = clean(spec, raw)
        except ValueError as exc:
            errors[spec.key] = str(exc)

    merged = {**DEFAULTS, **dict(current or {}), **cleaned}
    _check_tls_pair(merged, errors)
    _check_config_endpoint(cleaned, merged, errors)

    if errors:
        raise ValidationError(errors)
    return cleaned


def tls_problems(cert: str, key: str) -> dict[str, str]:
    """Why a save of these two paths would be refused, by field. Empty when it would not.

    The checks validate_settings would run on the pair, in the same words, for a
    page that wants to put the answer under the box it is about while it is
    still being typed. Turning HTTPS on offers two paths as a starting point and
    they are usually wrong; without this the first thing that says so is the
    save, and by then the admin has already agreed to restart the service for it.

    Only the two files themselves: whether the service can reach each one, and
    whether they are halves of the same issuance. That one is filled in and the
    other is not is not a fault - it is a form half filled in, and the page says
    so for itself while a box is still empty.
    """
    problems: dict[str, str] = {}
    cleaned: dict[str, str] = {}
    for field, raw in (("tlsCertPath", cert), ("tlsKeyPath", key)):
        try:
            cleaned[field] = clean(SPEC_BY_KEY[field], raw)
        except ValueError as exc:
            problems[field] = str(exc)
    if not problems and all(cleaned.values()):
        _check_tls_pair(cleaned, problems)
    return problems


def warnings_for(changed: Iterable[str], values: Mapping[str, str], url: str) -> list[str]:
    """Non-fatal advisories about a save that has already been accepted.

    `url` is the address the page is about to reconnect at, whole: scheme, host,
    port and the secret base path. Every line that sends the admin somewhere
    quotes it rather than describing it. The panel is normally mounted under a
    random prefix, so "reconnect over https" or "the new address" is an
    instruction that ends at a 404 - and the one thing the admin cannot look up
    at that point is the panel they have just been locked out of.
    """
    changed = frozenset(changed)
    out: list[str] = []
    if "webPort" in changed:
        out.append(
            f"Port {values['webPort']} has to be open in the firewall and in any cloud "
            "security group, or the panel will be unreachable after the restart."
        )
    if "webListen" in changed and values["webListen"] not in {"0.0.0.0", "::"}:
        out.append(
            f"The panel will only answer on {values['webListen']} from now on. Reach it from "
            "another address and it will look like the service is down."
        )
    if "webBasePath" in changed:
        out.append(
            "Session and CSRF cookies are scoped to the base path, so you will be signed out "
            f"and have to sign in again at {url}. Bookmark it before you close this tab."
        )
    if changed.intersection({"tlsCertPath", "tlsKeyPath"}):
        enabled = bool(values["tlsCertPath"] and values["tlsKeyPath"])
        state = "TLS is now on" if enabled else "TLS is now off"
        out.append(f"{state}; this page reconnects at {url}.")
    out.extend(_config_endpoint_warnings(changed, values))
    return out


def _config_endpoint_warnings(changed: frozenset[str], values: Mapping[str, str]) -> list[str]:
    """When the address in client configs changed without being asked to change.

    Setting it deliberately warns about nothing. The page names the address it
    is about to hand out in the line that confirms the save, which is one
    sentence in the place the operator is already looking; repeating it here
    turns a working save into a banner across the top of the page, and a banner
    that appears every single time is one nobody reads when it matters.
    """
    host = endpoint.wanted_host(values)
    domain_mode = values.get("configEndpointMode") == endpoint.MODE_DOMAIN

    # The answer changed underneath the setting: the name came off the
    # certificate and there is nothing left to substitute. Nobody asked for
    # this one, so nothing else is going to mention it.
    if domain_mode and not host and changed.intersection({"tlsCertPath", "tlsKeyPath"}):
        return [
            "Client configs are set to hand out the certificate's domain, and there is no "
            "certificate with a name on it any more, so they will carry the address in the "
            "server config again until one is set."
        ]
    return []


def clean(spec: SettingSpec, raw: object) -> str:
    """Normalise one value for one setting, or raise ValueError with the reason."""
    value = "" if raw is None else str(raw).strip()
    if _CONTROL.search(value):
        raise ValueError("Remove the line breaks and control characters from this value.")
    if spec.env and not _ENV_SAFE.match(value):
        raise ValueError(
            f"{_ENV_SAFE_HELP}: this value is written to /etc/awg-panel.env, which the "
            "service reads as shell."
        )

    handler = _CLEANERS.get(spec.kind)
    if handler is None:  # unreachable unless a spec names a kind nothing implements
        raise ValueError("The panel does not know how to check this value.")
    return handler(spec, value)


# ------------------------------------------------------------------ per kind


def _clean_int(spec: SettingSpec, value: str) -> str:
    try:
        number = int(value)
    except ValueError:
        raise ValueError("Enter a whole number.") from None
    low = 0 if spec.minimum is None else spec.minimum
    high = spec.maximum
    if number < low or (high is not None and number > high):
        bounds = f"{low} and {high}" if high is not None else f"at least {low}"
        raise ValueError(f"Enter a number between {bounds}.")
    return str(number)


def _clean_port(spec: SettingSpec, value: str) -> str:
    try:
        port = int(value)
    except ValueError:
        raise ValueError("Enter a port number between 1 and 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("Enter a port number between 1 and 65535.")
    return str(port)


def _clean_ip(spec: SettingSpec, value: str) -> str:
    if not value:
        raise ValueError("Enter an address to listen on, or 0.0.0.0 for every interface.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(
            f"'{value}' is not an IP address. Use 0.0.0.0 for every interface, 127.0.0.1 for "
            "local access only, or one of this machine's addresses."
        ) from None
    return value


def _clean_basepath(spec: SettingSpec, value: str) -> str:
    normalised = normalise_base_path(value)
    for segment in normalised.strip("/").split("/"):
        if segment and not _BASE_PATH_SEGMENT.match(segment):
            raise ValueError(
                "Use letters, digits, dots, dashes, underscores and slashes only, "
                "for example /awg/ab12cd34/."
            )
    return normalised


def _clean_file(spec: SettingSpec, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Use an absolute path, for example /etc/ssl/private/panel.key.")
    # Checked now rather than at startup: the service restarts a couple of
    # seconds after this save, and a wrong path means it never comes back.
    try:
        found = path.is_file()
    except OSError as exc:
        # is_file() swallows "not there" and nothing else, so a directory on the
        # way that this process may not enter comes back out as an exception. It
        # is still a bad value for this field, not a broken request: uncaught it
        # would be a 500 with no field to point at.
        if _masked_from_the_service(path):
            raise ValueError(_MASKED_HELP.format(path=path)) from None
        raise ValueError(
            f"The panel cannot reach {path}: {exc.strerror or exc}. Every directory above it "
            "has to be enterable by the service, which runs as root."
        ) from None
    if not found:
        if _masked_from_the_service(path):
            raise ValueError(_MASKED_HELP.format(path=path))
        raise ValueError(f"There is no file at {path}.")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{path} exists but cannot be read by the panel service.")
    return str(path)


# What the admin is actually looking at when a certificate they can cat as root
# is reported missing: both units set systemd's ProtectHome=yes, which replaces
# /root, /home and /run/user with an empty directory for the process, and the
# container image does not carry them either. Saying only "no such file" sends
# them off to re-check a path that was right all along.
_MASKED_HELP = (
    "The panel cannot see {path}. Its service runs with systemd's ProtectHome=yes, which "
    "hands it an empty /root and /home, so a file there does not exist as far as it is "
    "concerned - and gunicorn would fail on it the same way once this save restarted the "
    "service. Copy it somewhere like /etc/ssl/ and give that path instead."
)


def _masked_from_the_service(path: Path) -> bool:
    """Is this under one of the directories ProtectHome=yes empties out?"""
    parts = path.parts[1:]  # everything after the leading "/"
    return any(parts[: len(root)] == root for root in (("root",), ("home",), ("run", "user")))


def _clean_hostname(spec: SettingSpec, value: str) -> str:
    # A fully-qualified name is legitimately written with a trailing dot, and no
    # client would dial one: the resolver is fine with it, the Endpoint parser
    # in some apps is not.
    name = value.rstrip(".")
    if not name:
        return ""
    if spec.maximum and len(name) > spec.maximum:
        raise ValueError(f"A domain name is at most {spec.maximum} characters.")
    if not all(_HOSTNAME_LABEL.match(label) for label in name.split(".")):
        raise ValueError(
            "Enter a domain name such as vpn.example.com. Leave it empty to use whichever "
            "name the panel's certificate carries."
        )
    return name


def _clean_choice(spec: SettingSpec, value: str) -> str:
    if value not in spec.choices:
        raise ValueError(f"Choose one of: {', '.join(spec.choices)}.")
    return value


def _clean_bool(spec: SettingSpec, value: str) -> str:
    """A switch, stored as "1" or "0" so settings_store.get_bool reads it back.

    Every spelling a JSON body or a form can arrive with is accepted, because
    the value crosses the wire as text and `true`, `"true"` and `"on"` are the
    same answer said by three different clients.
    """
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return "1"
    if lowered in ("0", "false", "no", "off", ""):
        return "0"
    raise ValueError("Use true or false.")


def _clean_iface(spec: SettingSpec, value: str) -> str:
    """A network interface name, or "" to let the panel work it out.

    Bounded at fifteen characters because that is the kernel's own limit
    (IFNAMSIZ less the terminator), and restricted to the characters a name can
    actually contain - which also keeps a space or a quote out of a value that
    ends up as one word of a `tc` command line.
    """
    if not value:
        return ""
    if len(value) > 15:
        raise ValueError("An interface name is at most 15 characters.")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$", value):
        raise ValueError(
            "Enter an interface name such as eth0 or ens3. Leave it empty to use whichever "
            "interface holds the default route."
        )
    return value


def _clean_language(spec: SettingSpec, value: str) -> str:
    if not _LANGUAGE.match(value):
        raise ValueError("Use a language code such as en or ru.")
    return value


_CLEANERS = {
    "int": _clean_int,
    "port": _clean_port,
    "ip": _clean_ip,
    "basepath": _clean_basepath,
    "file": _clean_file,
    "hostname": _clean_hostname,
    "choice": _clean_choice,
    "language": _clean_language,
    "bool": _clean_bool,
    "iface": _clean_iface,
}


def _check_tls_pair(merged: Mapping[str, str], errors: dict[str, str]) -> None:
    """Half a TLS configuration is worse than none: gunicorn refuses to start on it."""
    cert, key = merged.get("tlsCertPath", ""), merged.get("tlsKeyPath", "")
    if bool(cert) != bool(key):
        # The message goes on the field that is still empty, which is the one the
        # admin has to fill in.
        errors.setdefault(
            "tlsKeyPath" if cert else "tlsCertPath",
            "TLS needs both a certificate and its private key. Set this one too, or clear the "
            "other to serve plain HTTP.",
        )
        return
    if not cert or errors:
        return
    # Two files that both exist can still be two halves of different pairs, and
    # nothing before the SSL context is built notices. That context is built by
    # gunicorn on the restart this save triggers, so the panel would go down
    # rather than come back on HTTPS. certs says None when it cannot tell, and
    # an unreadable or unusual file is gunicorn's call to make, not this one.
    if certs.key_matches_certificate(cert, key) is False:
        errors.setdefault(
            "tlsKeyPath",
            "This private key does not belong to that certificate. gunicorn would refuse to "
            "start on the pair and the panel would not come back, so the save is refused "
            "instead. Check the two paths are from the same issuance.",
        )


def _check_config_endpoint(
    cleaned: Mapping[str, str], merged: Mapping[str, str], errors: dict[str, str]
) -> None:
    """Asking for a domain in client configs when there is no domain to give.

    Checked only when one of the two settings is actually being sent. The
    question can also be answered "no" long afterwards - a certificate is
    replaced with one for another name, or removed on the way to a reverse
    proxy - and refusing *that* save would mean the choice made here could block
    an admin from turning TLS off. The renderer falls back to the address in the
    server config for exactly that case, and warnings_for says so.
    """
    if not cleaned.keys() & {"configEndpointMode", "configEndpointHost"}:
        return
    # A certificate path that failed its own check makes this unanswerable, and
    # the message about the path is the one worth reading first.
    if errors or merged.get("configEndpointMode") != endpoint.MODE_DOMAIN:
        return
    if endpoint.wanted_host(merged):
        return
    errors.setdefault(
        "configEndpointHost",
        "There is no domain to put in a client config: the panel has no TLS certificate, or "
        "the one it has carries no name - a certificate issued for an address names the address "
        "the configs already carry. Set a certificate with a name on it, or type the domain to "
        "hand out here.",
    )
