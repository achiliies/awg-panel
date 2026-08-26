"""Parameter rules and the plain-language help the Server Config page renders.

Two things have to stay in step: what the panel accepts, and what it tells the
admin a setting is for. Keeping both in one table means a rule can never be
tightened, or a range corrected, without the sentence that explains it moving
too. The UI reads `param_list()` over the API and renders `label`, `help_short`
under the field and `help_long` in the info popover, so every word here is
user-visible copy, not a code comment.

The target is AmneziaWG 3.1, the line install.sh pins. That differs from older
write-ups in two ways that matter:

* H1-H4 take a range `lo-hi` as well as a single number. Given a range the
  sender picks a fresh value per packet and the receiver accepts anything in
  it, so no constant is left to fingerprint. Ranges must not overlap.
* I1-I5 are imitation packets - whole decoy packets shaped like DNS, NTP, STUN
  or QUIC - not the "signature packets" of the 1.5 documentation.

`must_match_client` means the value has to be byte-identical on both ends, so
changing it invalidates every config already issued. It is deliberately False
for things a client merely receives a copy of (DNS, AllowedIPs): those still
want a re-issue, but that is the store's call, not a property of the protocol.

Most of what is here ends up on both ends. `SERVER_ONLY_PARAMS` is the
exception and DisableCookies is its first member: an [Interface] setting the
server keeps to itself, which is why it is not in `AWG_PARAMS` - that tuple is
what gets copied into every client config.

`importer_safe` is the hard-won part, and it is False for exactly the settings
AmneziaWG 3.0 added: HeaderProtectionKey, ContentPaddingAddition and the timer
overrides - and for RandomTrailers, which 3.1 added on the same terms.
Two things have to be true before one of them is worth setting, and neither is
visible from the server. The peer has to speak the release that added it - 3.0,
or 3.1 for RandomTrailers - and most clients speak neither yet, so these are a
beta feature from the operator's side whatever the server supports - and it has
to have been given the value, which rules out the Amnezia app: it parses .conf
files and discards those lines silently. What that costs
depends on which one: a client without the header protection key cannot read a
protected header at all, so the server refuses it and no error appears at either
end, while the timers and the content padding are each end's own business and a
client that never got them simply keeps the protocol's defaults - the tunnel
works, and only one end is hiding anything.
Imitation packets have the same trap one level down: the kernel
accepts <t>, <c>, <rc> and <rd> tags, but the app rejects the whole file on
import with error 1000.
"""

import base64
import ipaddress
import random
import re
from dataclasses import asdict, dataclass

from . import subnet, subnet6

# Header values are drawn from 5 upwards, because 1-4 are WireGuard's own type
# constants and a range that includes them is a range that can collide with
# plain WireGuard. The ceiling stays inside a signed 32-bit integer: the wire
# format is an unsigned u32 and the top half would be legal, but a parser
# somewhere in the chain reading it as signed would break every handshake with
# nothing in any log, and the extra bit buys almost nothing.
H_FLOOR = 5
H_MIN = 1
H_MAX = 2_147_483_647

# Junk and padding sizes are capped below the smallest sane tunnel MTU: a junk
# packet that needs fragmenting is a signature of its own.
JUNK_MAX = 1280

# The floor under S1-S4, and the reason it exists. Header protection encrypts
# the AmneziaWG header under a shared key, and the nonce it does that with is
# not carried in a field of its own: the receiver reads the first 12 bytes of
# the S-prefix on the packet in front of it and uses those. A prefix shorter
# than that has nowhere to put the nonce, so the kernel refuses the key rather
# than the padding - `awg setconf` returns EINVAL, `awg-quick up` stops on it,
# and the interface the panel just took down to apply the change does not come
# back. All four are checked, not only the one the packet type in hand needs,
# so the floor applies to every one of them the moment a key is set.
HEADER_NONCE = 12

# Domains the imitation DNS packets ask about. Same list as lib/obfs.sh, so a
# profile generated from the panel is indistinguishable from one the installer
# or the TUI wrote.
DOMAINS = [
    "apple.com",
    "www.google.com",
    "cloudflare.com",
    "microsoft.com",
    "www.icloud.com",
    "outlook.com",
    "fastly.net",
    "akamai.net",
]

# The set, and the order, mirrored into every client config. The order is not
# cosmetic: it is what the installer wrote and what every config in the field
# already has, and a client config is compared against its server's by eye more
# often than by tool. A value that is empty or exactly "0" is omitted there,
# which is how "off" is expressed.
AWG_PARAMS: tuple[str, ...] = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
    "RandomTrailers",
)

# Settings that live in the server's [Interface] and stop there.
#
# AWG_PARAMS above is not "the obfuscation settings", it is the set mirrored
# into every client config, and the two stopped being the same thing here. A
# switch the server keeps to itself has no line to write on a client and no
# value for one to agree on, so putting it in that tuple would copy it into
# every config the panel issues and tell the whole fleet to re-import for a
# change no peer can even read. It still belongs to the [Interface] section,
# which is why it is a tuple beside that one rather than a group of its own in
# clients.env: everything that reads or writes awg0.conf wants both.
SERVER_ONLY_PARAMS: tuple[str, ...] = ("DisableCookies",)

NETWORK_PARAMS: tuple[str, ...] = (
    "ListenPort",
    "Address",
    "MTU",
    "DNS",
    "EndpointHost",
    "EndpointPort",
    "AllowedIPs",
    "PersistentKeepalive",
)

HOOK_PARAMS: tuple[str, ...] = ("PostUp", "PostDown", "PreDown")

# The hooks the tunnel carries so that the kernel's own behaviour does not undo
# the panel's. Each of them exists because `awg-quick` builds the interface from
# the config file and the config file cannot express what the panel knows:
#
# * PreDown reads the per-peer counters before an orderly `awg-quick down`
#   discards them, which is all that carries a client's usage across a restart.
# * PostUp re-revokes the disabled peers, which a bring-up would otherwise
#   re-admit because their entries are still in the file - that is what reserves
#   their addresses.
# * PostUp re-applies every bandwidth ceiling, because a tc structure belongs to
#   the network device and goes when the device does. The database still knows
#   what each client is entitled to; nothing in the kernel does.
# * PostDown takes that structure off the *WAN*. The tunnel's own qdiscs left
#   with the tunnel and need no help, but shaping upload puts an HTB root on the
#   interface this machine sends everything through, and leaving it up while the
#   tunnel is down is a queue on the path of the panel and the operator's SSH
#   session, metering clients that are no longer connected to anything.
#
# They live here rather than beside the code that writes them because both halves
# of the panel need to recognise a line as one of these: awg.store re-asserts
# them on every write, and the checks below have to know that a hook is not a
# firewall rule. They were a writer's private constants once, and the first thing
# that cost was a server told on every settings page that its firewall rules did
# not balance - counted against a PostUp line the panel had put there itself.
#
# All of them end in "|| true". awg-quick runs under `set -e`, so a hook that
# exits non-zero takes the whole operation with it: a panel mid-upgrade, or a
# lock held a moment too long, must not be a tunnel that will not come down.
PREDOWN_HOOK = "/usr/local/bin/awg-panel manage trafficsync || true"
PREDOWN_MARKER = "awg-panel manage trafficsync"
POSTUP_HOOK = "/usr/local/bin/awg-panel manage enforce || true"
POSTUP_MARKER = "awg-panel manage enforce"
SHAPE_HOOK = "/usr/local/bin/awg-panel manage shape || true"
SHAPE_MARKER = "awg-panel manage shape"
UNSHAPE_HOOK = "/usr/local/bin/awg-panel manage shape --detach || true"
# Deliberately the whole invocation. "manage shape" alone is a prefix of this
# one, so a marker that stopped at the subcommand would match the PostDown line
# too - and a config that had lost its PostUp hook would look as though it still
# had one, on the strength of the line that removes the shaping rather than the
# one that applies it.
UNSHAPE_MARKER = "awg-panel manage shape --detach"
MANAGE_MARKERS = (PREDOWN_MARKER, POSTUP_MARKER, SHAPE_MARKER, UNSHAPE_MARKER)

# Bounds the generator draws from. Every one of them is a trade between the
# cover a value buys and what it costs, and the reasoning is on the ParamSpec
# that carries the same numbers into the UI. They are deliberately wide: the
# point of generating a set per server is that two servers should not share
# one, and a narrow band would put them back together again.
JC_RANGE = (3, 8)
JMIN_RANGE = (24, 80)
JSPAN_RANGE = (40, 240)
S_RANGE = (24, 320)
# S4 rides on every data packet rather than on handshakes alone, so it is
# capped twice: in itself, and against the MTU. An ordinary 1500-byte path
# leaves 1440 bytes for the tunnel, and S4 comes out of that.
S4_RANGE = (HEADER_NONCE, 40)
MTU_BUDGET = 1440
DEFAULT_MTU = 1400

# What the kernel pads a data packet to when nothing else is configured: the
# plaintext is rounded up to a multiple of 16 before encryption, so an observer
# learns the length of the packet inside only to within 16 bytes. Content
# padding replaces that rounding rather than adding to it, which is why a value
# below this makes the length leak worse rather than better - see the
# ContentPaddingAddition help below, and the range randomize_advanced draws.
PADDING_MULTIPLE = 16

# Groups where a value of "0" means "off" rather than the number zero, matching
# the omission rule these parameters are written with.
_OFF_MEANS_UNSET = frozenset({"junk", "sizes", "headers", "imitation", "advanced"})

# Tags the kernel understands but the Amnezia app's importer refuses.
_APP_HOSTILE_TAGS = frozenset({"t", "c", "rc", "rd"})


@dataclass(frozen=True)
class Profile:
    """One tuning of the obfuscation generator: the bands, not the values.

    Every profile draws every value at random - that is not what separates them.
    What separates them is the band each value comes from, and every band is the
    same trade written down twice: how much of the protocol's shape a setting
    hides, against what sending it costs.
    """

    key: str
    jc: tuple[int, int]
    jmin: tuple[int, int]
    jspan: tuple[int, int]
    s: tuple[int, int]
    s4: tuple[int, int]
    decoys: tuple[int, int]


def _span(*bands: tuple[int, int]) -> tuple[int, int]:
    """The band that contains all of these, floor to ceiling."""
    return (min(band[0] for band in bands), max(band[1] for band in bands))


# The junk in front of a handshake and the padding on the handshake packets are
# nearly free: a peer handshakes about once every two minutes, so even the
# widest band below costs well under a hundred bytes a second. What is not free
# is S4, which rides on every data packet and comes out of the usable MTU, and
# the decoy packets, which are one extra send per handshake each - so those two
# are what the profiles really differ on, and both stay bounded in every one.
#
# Standard is the band the constants above describe and the one every ParamSpec
# quotes in its help text. Fast trims the per-handshake cost for a metered or
# lossy link, at the price of a thinner disguise. DPI-resistant spends more of
# everything, because on a network that is actively classifying traffic the
# bandwidth is the cheap part.
#
# Random exists because the other three are published. Three known bands are
# three things to test for, and a server whose Jc is 2 has said which profile
# built it; Random spans all three so that the choice itself stops being a
# fingerprint. The cost is that it is the one profile that will not tell you in
# advance what the draw is going to cost.
#
# What no profile is free to trade away is HEADER_NONCE: every `s` and `s4` band
# starts at or above it, including Fast's, because a profile that draws below it
# is one that cannot be combined with a header protection key. Twelve bytes on a
# data packet is the whole of what that costs, against a budget that is never
# smaller than twenty, and the alternative is a Fast obfuscation set and a
# generated 3.0 group that silently refuse to be used together.
_FAST = Profile(
    key="fast",
    jc=(1, 3),
    jmin=(16, 40),
    jspan=(24, 80),
    s=(HEADER_NONCE, 96),
    s4=(HEADER_NONCE, 20),
    decoys=(1, 2),
)
_STANDARD = Profile(
    key="standard",
    jc=JC_RANGE,
    jmin=JMIN_RANGE,
    jspan=JSPAN_RANGE,
    s=S_RANGE,
    s4=S4_RANGE,
    decoys=(3, 5),
)
# Jc stops at 12 rather than going higher: past that warnings_for() starts
# telling the admin the handshake is needlessly slow on a mobile link, and a
# generator that hands back a set the save bar then complains about is a
# generator nobody trusts.
_DPI = Profile(
    key="dpi",
    jc=(8, 12),
    jmin=(64, 160),
    jspan=(200, 640),
    s=(160, 900),
    s4=(24, 64),
    decoys=(4, 5),
)
_RANDOM = Profile(
    key="random",
    jc=_span(_FAST.jc, _STANDARD.jc, _DPI.jc),
    jmin=_span(_FAST.jmin, _STANDARD.jmin, _DPI.jmin),
    jspan=_span(_FAST.jspan, _STANDARD.jspan, _DPI.jspan),
    s=_span(_FAST.s, _STANDARD.s, _DPI.s),
    s4=_span(_FAST.s4, _STANDARD.s4, _DPI.s4),
    decoys=_span(_FAST.decoys, _STANDARD.decoys, _DPI.decoys),
)

PROFILES: dict[str, Profile] = {p.key: p for p in (_STANDARD, _DPI, _FAST, _RANDOM)}
DEFAULT_PROFILE = "standard"


@dataclass(frozen=True)
class AdvancedProfile:
    """The same idea for the advanced group, which trades different things.

    Nothing here is about bandwidth. The timers decide how often a handshake
    happens at all, which is the one event on the wire that obfuscation cannot
    make cheap, so stretching them is what "harder to classify" means on this
    side of the page; shortening them is what "reconnects quickly" means.
    """

    key: str
    #: Where the *top* of the content padding range comes from, not the padding
    #: itself: the kernel picks a fresh value inside the range for every packet,
    #: and a range is the only shape of this setting that is worth writing.
    padding: tuple[int, int]
    rekey_after: tuple[int, int]
    rekey_timeout: tuple[int, int]
    keepalive: tuple[int, int]
    attempts: tuple[int, int]
    #: How far above the minimum the protocol allows RejectAfterTime is placed.
    margin: tuple[int, int]


_ADV_FAST = AdvancedProfile(
    key="fast",
    padding=(0, 0),
    rekey_after=(110, 130),
    rekey_timeout=(2, 4),
    keepalive=(5, 10),
    attempts=(8, 12),
    margin=(60, 120),
)
_ADV_STANDARD = AdvancedProfile(
    key="standard",
    padding=(32, 96),
    rekey_after=(120, 180),
    rekey_timeout=(4, 7),
    keepalive=(8, 15),
    attempts=(16, 24),
    margin=(60, 180),
)
_ADV_DPI = AdvancedProfile(
    key="dpi",
    padding=(128, 384),
    rekey_after=(300, 600),
    rekey_timeout=(8, 20),
    keepalive=(30, 60),
    attempts=(40, 80),
    margin=(120, 360),
)
_ADV_RANDOM = AdvancedProfile(
    key="random",
    padding=_span(_ADV_FAST.padding, _ADV_STANDARD.padding, _ADV_DPI.padding),
    rekey_after=_span(_ADV_FAST.rekey_after, _ADV_STANDARD.rekey_after, _ADV_DPI.rekey_after),
    rekey_timeout=_span(
        _ADV_FAST.rekey_timeout, _ADV_STANDARD.rekey_timeout, _ADV_DPI.rekey_timeout
    ),
    keepalive=_span(_ADV_FAST.keepalive, _ADV_STANDARD.keepalive, _ADV_DPI.keepalive),
    attempts=_span(_ADV_FAST.attempts, _ADV_STANDARD.attempts, _ADV_DPI.attempts),
    margin=_span(_ADV_FAST.margin, _ADV_STANDARD.margin, _ADV_DPI.margin),
)

ADVANCED_PROFILES: dict[str, AdvancedProfile] = {
    p.key: p for p in (_ADV_STANDARD, _ADV_DPI, _ADV_FAST, _ADV_RANDOM)
}


@dataclass(frozen=True)
class ParamSpec:
    """One parameter: how it is checked, and how it is explained to the admin."""

    key: str
    group: str
    label: str
    kind: str
    help_short: str
    help_long: str
    must_match_client: bool
    importer_safe: bool = True
    min: int | None = None
    max: int | None = None
    recommended: str | None = None
    default: str | None = None
    optional: bool = True


_SPECS: list[ParamSpec] = [
    ParamSpec(
        key="Jc",
        group="junk",
        label="Junk packets per handshake",
        kind="int",
        help_short="How many packets of pure random data go out ahead of every handshake.",
        help_long=(
            "Before each handshake the peer sends Jc packets of random bytes. A filter that "
            "recognises WireGuard by the shape of its opening exchange sees a burst of "
            "unrelated traffic instead of one clean 148-byte initiation. The far end cannot "
            "parse them and drops them, so the only cost is a little bandwidth and a few "
            "milliseconds of connect time. Too few leaves the pattern visible; too many makes "
            "reconnecting on a lossy mobile link slow, which is why Reconfigure draws from 3 "
            "to 8 rather than going as high as the protocol allows. Jc does not have to match "
            "on the client - junk is junk to whoever receives it."
        ),
        must_match_client=False,
        min=1,
        max=128,
        recommended="3-8, drawn per server",
    ),
    ParamSpec(
        key="Jmin",
        group="junk",
        label="Smallest junk packet (bytes)",
        kind="int",
        help_short="Lower bound on the random size of each junk packet.",
        help_long=(
            "Every junk packet gets a random length between Jmin and Jmax. Fixing the size "
            "would just trade one signature for another, so leave enough distance between the "
            "two for a real spread. Jmin must be smaller than Jmax. Under about 20 bytes a "
            "packet resembles nothing else on the wire and stands out by being tiny, so the "
            "generator never goes below 24. The bounds themselves are drawn per server as "
            "well: the sizes inside them are random already, but the floor and the ceiling "
            "are visible to anyone who watches enough handshakes."
        ),
        must_match_client=False,
        min=0,
        max=JUNK_MAX,
        recommended="24-80, drawn per server",
    ),
    ParamSpec(
        key="Jmax",
        group="junk",
        label="Largest junk packet (bytes)",
        kind="int",
        help_short="Upper bound on the random size of each junk packet; must exceed Jmin.",
        help_long=(
            "Sets the top of the junk size range. Keep it below the tunnel MTU or the junk "
            "itself gets fragmented, which is a signature. A handshake costs up to Jc x Jmax "
            "bytes of junk, so on a metered or high-latency link keep that product modest. "
            "Reconfigure puts it 40 to 240 bytes above Jmin, which is a wide enough spread "
            "that no single size stands out and still costs under 3 kB on the worst handshake."
        ),
        must_match_client=False,
        min=1,
        max=JUNK_MAX,
        recommended="Jmin plus 40-240, drawn per server",
    ),
    ParamSpec(
        key="S1",
        group="sizes",
        label="Junk in front of the handshake initiation (bytes)",
        kind="int",
        help_short="Random bytes prepended to the initiation packet to move it off 148 bytes.",
        help_long=(
            "A WireGuard handshake initiation is always exactly 148 bytes, and that number "
            "alone identifies the protocol. S1 random bytes are prepended so the packet lands "
            "on an uninteresting size instead. Both ends must use the same value: the receiver "
            "skips exactly S1 bytes to find the real packet, and a mismatch makes the "
            "handshake fail with nothing logged. S1 + 56 must also differ from S2, or the "
            "initiation and the response come out the same size and become a recognisable "
            "pair. This is the single most valuable value to keep to yourself: a fixed S1 "
            "means every server running the same script puts its initiation on the same "
            "number of bytes, and one filter rule then matches all of them. Reconfigure draws "
            "it fresh, so there is no such rule to write."
        ),
        must_match_client=True,
        min=0,
        max=JUNK_MAX,
        recommended="24-320, drawn per server",
    ),
    ParamSpec(
        key="S2",
        group="sizes",
        label="Junk in front of the handshake response (bytes)",
        kind="int",
        help_short="Random bytes prepended to the response packet, which is otherwise 92 bytes.",
        help_long=(
            "The same trick as S1, applied to the 92-byte handshake response the server sends "
            "back. Both ends must agree on the number. Choose it so that S1 + 56 does not "
            "equal S2, otherwise the two handshake packets end up identical in size, which is "
            "a pattern in itself - Reconfigure draws S1 and S2 independently and steps S2 by "
            "one byte on the rare draw that collides."
        ),
        must_match_client=True,
        min=0,
        max=JUNK_MAX,
        recommended="24-320, drawn per server",
    ),
    ParamSpec(
        key="S3",
        group="sizes",
        label="Junk in front of the cookie reply (bytes)",
        kind="int",
        help_short="Random bytes prepended to cookie-reply packets.",
        help_long=(
            "Cookie replies are what the server sends when it is under load and wants the "
            "client to prove it can receive; they have their own fixed size, so they get their "
            "own padding. Both ends must agree. The packet type is rare in normal operation, "
            "so the exact value matters less than S1 and S2, but leaving it at 0 leaves one "
            "recognisable shape behind for anyone who watches long enough. It costs nothing "
            "to pad, so Reconfigure draws it from the same band as S1 and S2."
        ),
        must_match_client=True,
        min=0,
        max=JUNK_MAX,
        recommended="24-320, drawn per server",
    ),
    ParamSpec(
        key="S4",
        group="sizes",
        label="Junk in front of every data packet (bytes)",
        kind="int",
        help_short="Random bytes added to EVERY data packet. Each byte costs usable MTU.",
        help_long=(
            "S4 pads transport packets, the ones carrying your actual traffic. Unlike S1-S3 it "
            "applies constantly, so every byte here is a byte of usable MTU gone: on an "
            "ordinary 1500-byte path the tunnel MTU has to be at most 1440 - S4 or full-size "
            "packets are silently dropped and pages hang half-loaded. Both ends must agree. "
            "It still earns its keep, because without it a tunnel packet is always the packet "
            "inside it plus a constant, and that constant is worth hiding. Reconfigure draws "
            "12 to 40 bytes and never more than the MTU leaves free, so the tunnel keeps "
            "working; raise it by hand and you must lower the MTU by the same amount. The "
            "floor of 12 is not a matter of taste: a header protection key is carried by a "
            "nonce read off the front of this prefix, so with a key set anything below 12 "
            "here - or in S1, S2 or S3 - makes the kernel refuse the whole configuration and "
            "the interface will not come up."
        ),
        must_match_client=True,
        min=0,
        max=JUNK_MAX,
        recommended="12-40, drawn per server within the MTU headroom",
    ),
    ParamSpec(
        key="H1",
        group="headers",
        label="Header value for handshake initiations",
        kind="range",
        help_short="Replaces WireGuard's fixed type 1. A single number, or a lo-hi range.",
        help_long=(
            "WireGuard labels its four packet types with the constants 1, 2, 3 and 4 in the "
            "first byte, which is the easiest possible way to spot it. H1 replaces the label "
            "on handshake initiations. Give it a range and a fresh random value is chosen for "
            "every packet while the peer accepts anything within it, so there is no constant "
            "left to fingerprint. All four H values must be distinct and their ranges must not "
            "overlap, or the receiver cannot tell one packet type from another and the tunnel "
            "never comes up. Both ends must use exactly the same values. Leaving H1-H4 at "
            "1, 2, 3, 4 means header obfuscation is off. Where the ranges sit matters as much "
            "as having them: if every server split the space the same way, the part of it a "
            "value landed in would give the packet type away again, so Reconfigure places all "
            "four at random and then shuffles which type gets which."
        ),
        must_match_client=True,
        min=H_MIN,
        max=H_MAX,
        recommended="a random range in one quarter of 5-2147483647, drawn per server",
    ),
    ParamSpec(
        key="H2",
        group="headers",
        label="Header value for handshake responses",
        kind="range",
        help_short="Replaces WireGuard's fixed type 2. A single number, or a lo-hi range.",
        help_long=(
            "The type label the server puts on its handshake response. Same rules as H1: a "
            "range gives a different value on every packet, the whole set has to be distinct "
            "and non-overlapping, and both ends must carry identical values or nothing "
            "connects. Generate all four together rather than editing one by hand - "
            "Reconfigure lays out four ranges that cannot collide, which is easy to get wrong "
            "one field at a time."
        ),
        must_match_client=True,
        min=H_MIN,
        max=H_MAX,
        recommended="a random range in one quarter of 5-2147483647, drawn per server",
    ),
    ParamSpec(
        key="H3",
        group="headers",
        label="Header value for cookie replies",
        kind="range",
        help_short="Replaces WireGuard's fixed type 3. A single number, or a lo-hi range.",
        help_long=(
            "The type label on cookie replies, the packets sent under load to make a client "
            "prove it can receive. They are rare, which is exactly why leaving this one at 3 "
            "is worth avoiding: a single unobfuscated packet type is enough to classify the "
            "whole flow. Must be distinct from and non-overlapping with H1, H2 and H4, and "
            "identical on both ends."
        ),
        must_match_client=True,
        min=H_MIN,
        max=H_MAX,
        recommended="a random range in one quarter of 5-2147483647, drawn per server",
    ),
    ParamSpec(
        key="H4",
        group="headers",
        label="Header value for data packets",
        kind="range",
        help_short="Replaces WireGuard's fixed type 4. Applies to every packet the tunnel carries.",
        help_long=(
            "The type label on transport packets, which is nearly all of the traffic, so this "
            "is the value an observer sees most often. With a range, every data packet carries "
            "a different label and the flow has no repeating byte at that offset at all. Must "
            "be distinct from and non-overlapping with H1-H3, and identical on both ends. "
            "Changing it takes effect the moment the config is applied, so every client has to "
            "re-import before it can talk again."
        ),
        must_match_client=True,
        min=H_MIN,
        max=H_MAX,
        recommended="a random range in one quarter of 5-2147483647, drawn per server",
    ),
    ParamSpec(
        key="I1",
        group="imitation",
        label="Imitation packet 1",
        kind="imitation",
        help_short="A decoy packet sent before the handshake. Built from <r N> and <b 0xHEX> tags.",
        help_long=(
            "Imitation packets are complete, well-formed decoys sent ahead of the handshake so "
            "that a session opens looking like ordinary internet traffic rather than like a "
            "VPN starting up. Reconfigure fills these slots with one protocol rather than a "
            "mixture - a video call, a QUIC connection or a run of name lookups - because a "
            "decoy only works on a filter that parses it, and no real host speaks four "
            "different protocols down one UDP socket pair. Build them only from <r N> (N "
            "random bytes) and <b 0xHEX> (literal bytes): the kernel also accepts <t>, <c>, "
            "<rc> and <rd>, but the Amnezia app refuses to import a config containing any of "
            "them with error 1000. Both ends must carry an identical set."
        ),
        must_match_client=True,
        recommended="the first packet of a generated decoy session",
    ),
    ParamSpec(
        key="I2",
        group="imitation",
        label="Imitation packet 2",
        kind="imitation",
        help_short="The second decoy in the set. Same tag rules as the rest.",
        help_long=(
            "Second packet of the generated session, and it follows from the first: a STUN "
            "request is answered by a STUN response, a query by its answer, a QUIC initial by "
            "the 1-RTT packets that would come after it. An exchange that makes sense is the "
            "part a filter checks; five unrelated packets are easier to spot than none. Same "
            "tag rules and the same matching requirement as every other slot."
        ),
        must_match_client=True,
        recommended="the second packet of a generated decoy session",
    ),
    ParamSpec(
        key="I3",
        group="imitation",
        label="Imitation packet 3",
        kind="imitation",
        help_short="The third decoy in the set. Same tag rules as the rest.",
        help_long=(
            "Third packet of the generated session. Randomising this set matters as much as "
            "having it: a set everyone copies from the same guide becomes its own fingerprint, "
            "so Reconfigure picks a fresh protocol, fresh sizes and fresh contents rather than "
            "shipping one everybody shares. Must match on both ends."
        ),
        must_match_client=True,
        recommended="the third packet of a generated decoy session",
    ),
    ParamSpec(
        key="I4",
        group="imitation",
        label="Imitation packet 4",
        kind="imitation",
        help_short="The fourth decoy. May be empty: not every generated session needs five.",
        help_long=(
            "Fourth packet of the generated session, and the first that is sometimes left "
            "empty - the generated session is three to five packets long, because a fixed "
            "count is one more thing to match on. An empty slot is removed from the config "
            "rather than written as a blank. Must match on both ends when it is set."
        ),
        must_match_client=True,
        recommended="the fourth packet of a generated decoy session, or empty",
    ),
    ParamSpec(
        key="I5",
        group="imitation",
        label="Imitation packet 5",
        kind="imitation",
        help_short="The fifth decoy. May be empty: not every generated session needs five.",
        help_long=(
            "Last of the five slots, and often unused. Each imitation packet costs one extra "
            "packet per handshake, so on a very flaky link fewer is genuinely better - the "
            "decoys are dropped by whoever receives them, but they still have to be sent. "
            "Must match on both ends when it is set."
        ),
        must_match_client=True,
        recommended="the fifth packet of a generated decoy session, or empty",
    ),
    ParamSpec(
        key="HeaderProtectionKey",
        group="advanced",
        label="Header protection key",
        kind="key",
        help_short="Encrypts the packet header itself. Needs AmneziaWG 3.0 on every client.",
        help_long=(
            "With a header protection key the whole AmneziaWG header is encrypted under a "
            "shared secret, so even the randomised H1-H4 values never appear on the wire. It "
            "arrived with AmneziaWG 3.0, it is the strongest option this server offers, and "
            "between two peers that both speak 3.0 it works perfectly. It is also the biggest "
            "trap in this configuration, because most clients are not there yet: one still on "
            "2.x has no idea the setting exists, and the Amnezia apps read .conf files and "
            "throw the line away without a word. Either way the client connects without header "
            "protection, the server rejects everything it sends, and nothing reports why - "
            "just a tunnel that never comes up. Set it only once every peer is known to run "
            "3.0 and is configured by hand, or through Amnezia's own JSON / vpn:// format. "
            "Leave it empty otherwise. Accepts a 44-character base64 key or 64 hex characters. "
            "It also puts a floor under the padding sizes: the nonce this key is used with is "
            "read from the first 12 bytes of the S1-S4 prefix on each packet, so all four have "
            "to be at least 12 or the kernel refuses the configuration outright and the "
            "interface stops coming up."
        ),
        must_match_client=True,
        importer_safe=False,
    ),
    ParamSpec(
        key="ContentPaddingAddition",
        group="advanced",
        label="Content padding (bytes)",
        kind="range",
        help_short="Pads packet contents to hide their real length. Needs AmneziaWG 3.0 on every client.",
        help_long=(
            "Adds padding inside each encrypted packet so that observed packet sizes stop "
            "tracking the sizes of the traffic inside the tunnel - the leak that lets a "
            "passive observer guess which page or file was fetched. Give it a range such as "
            "16-96 and a fresh amount is chosen for every packet; a single number is almost "
            "always the wrong answer, because this setting replaces the padding WireGuard "
            "already does rather than adding to it. Left alone, every packet is rounded up to "
            "a multiple of 16 bytes, which hides the last four bits of its length for free. "
            "Set a constant and that rounding is gone: the packet on the wire becomes the "
            "packet inside it plus a fixed number, which tracks the original byte for byte. "
            "Anything below 16 is therefore worse than leaving this empty, and Reconfigure "
            "will not draw one. It is an AmneziaWG 3.0 addition: a peer still on 2.x, or one "
            "that imported a .conf through the Amnezia app, simply never pads - the tunnel "
            "still works, but only the end that has this set is hiding anything."
        ),
        must_match_client=True,
        importer_safe=False,
        min=0,
        max=JUNK_MAX,
        recommended="a range such as 32-96, drawn per server",
    ),
    ParamSpec(
        key="RekeyAfterTime",
        group="advanced",
        label="Rekey after (seconds)",
        kind="int",
        help_short="How long a session key is used before a new handshake starts. Needs 3.0 on both ends.",
        help_long=(
            "WireGuard renegotiates session keys every 120 seconds; this overrides that. "
            "Shortening it means more handshakes, which is more of the very pattern you are "
            "trying to hide. Lengthening it keeps a key alive longer than the protocol was "
            "designed around. It has to stay well below RejectAfterTime or a key expires "
            "before its replacement is agreed and the tunnel stalls for a few seconds on every "
            "cycle. The timer overrides are AmneziaWG 3.0 additions, and the Amnezia app's "
            "importer drops them besides, so a client keeps the protocol's own defaults while "
            "the server does not - leave these empty unless every peer runs 3.0 and is "
            "configured by hand."
        ),
        must_match_client=True,
        importer_safe=False,
        min=5,
        max=3600,
        recommended="120",
    ),
    ParamSpec(
        key="RekeyTimeout",
        group="advanced",
        label="Handshake retry interval (seconds)",
        kind="int",
        help_short="How long to wait for a handshake reply before retrying. Needs 3.0 on both ends.",
        help_long=(
            "After sending a handshake initiation a peer waits this long for the reply before "
            "trying again; the default is 5 seconds. Raising it makes the retry pattern less "
            "regular and less chatty when a filter is dropping the first attempts; lowering it "
            "makes reconnects quicker at the cost of a burst that is easy to spot. Multiplied "
            "by the handshake attempt limit it decides how long a peer keeps trying before it "
            "gives up and waits for new traffic. An AmneziaWG 3.0 setting, and one the app's "
            ".conf importer drops besides, so a client below 3.0 keeps the default of 5."
        ),
        must_match_client=True,
        importer_safe=False,
        min=1,
        max=60,
        recommended="5",
    ),
    ParamSpec(
        key="RejectAfterTime",
        group="advanced",
        label="Reject session after (seconds)",
        kind="int",
        help_short="Hard expiry of a session key; nothing older is accepted. Needs 3.0 on both ends.",
        help_long=(
            "The absolute lifetime of a session key - after this many seconds it is refused "
            "even if a replacement handshake has not completed. The default is 180. It must "
            "stay larger than the rekey time plus a full retry cycle; the protocol requires it "
            "to exceed the keepalive timeout plus the handshake retry interval, and a value "
            "that breaks that leaves the tunnel dropping traffic while it renegotiates. Both "
            "ends should carry the same number, but only an AmneziaWG 3.0 client reads it at "
            "all, and the app importer drops the line besides, so most clients will use 180 "
            "whatever is set here."
        ),
        must_match_client=True,
        importer_safe=False,
        min=10,
        max=7200,
        recommended="180",
    ),
    ParamSpec(
        key="KeepaliveTimeout",
        group="advanced",
        label="Keepalive timeout (seconds)",
        kind="int",
        help_short="Idle time before a keepalive is overdue - not PersistentKeepalive. Needs 3.0 on both ends.",
        help_long=(
            "How long the protocol waits on an idle session before it expects to hear a "
            "keepalive; the default is 10 seconds. This is not the same setting as "
            "PersistentKeepalive, which is what a client sends to hold its NAT mapping open. "
            "Raising it cuts background chatter on a quiet link; lowering it notices a dead "
            "peer sooner at the cost of more traffic. An AmneziaWG 3.0 setting that the app's "
            ".conf importer drops besides, so leave it empty unless every peer runs 3.0."
        ),
        must_match_client=True,
        importer_safe=False,
        min=1,
        max=3600,
        recommended="10",
    ),
    ParamSpec(
        key="MaxHandshakeAttempts",
        group="advanced",
        label="Handshake attempts",
        kind="int",
        help_short="How many times a peer retries a handshake before giving up. Needs 3.0 on both ends.",
        help_long=(
            "A peer sends a handshake initiation, waits the retry interval, and tries again up "
            "to this many times before declaring the link dead and going quiet until there is "
            "new traffic to send; the default works out at 18 attempts. A higher number helps "
            "where the first attempts are being dropped deliberately, but a very high one "
            "means a client that never stops probing a server that is gone. This is local "
            "retry behaviour, so the two ends do not have to agree - but only an AmneziaWG 3.0 "
            "client reads it, and the app importer drops it along with the rest of the timers."
        ),
        must_match_client=False,
        importer_safe=False,
        min=1,
        max=1000,
        recommended="18",
    ),
    ParamSpec(
        key="RandomTrailers",
        group="advanced",
        label="Random packet trailers",
        kind="bool",
        help_short="Pads every packet to a random length. Needs AmneziaWG 3.1 on every client.",
        help_long=(
            "The length of an AmneziaWG packet follows from what is inside it, and a handshake "
            "is the same length every time - which is a pattern to match on even when every "
            "byte in it is random. With this on, the module appends a trailer of random length "
            "to each packet it sends, handshakes and data alike, drawn against what the path "
            "has already carried so the padding never pushes a packet over the MTU. It is the "
            "one setting on this card with nothing to copy: there is no value to agree on, "
            "only on or off. "
            "It arrived in AmneziaWG 3.1 and both ends still have to have it. A peer without "
            "it measures an arriving handshake, finds it longer than the one it expects and "
            "drops it, with no error at either end; the Amnezia app's .conf importer discards "
            "the line, so a client imported that way is a client without it. "
            "It also does nothing to data packets while ContentPaddingAddition is set - that "
            "one already decides their padding, and the two do not stack."
        ),
        must_match_client=True,
        importer_safe=False,
        recommended="on, once every peer is known to run AmneziaWG 3.1",
    ),
    ParamSpec(
        key="DisableCookies",
        group="protection",
        label="Disable cookie replies",
        kind="bool",
        help_short="Stops the server answering a handshake flood with a cookie challenge.",
        help_long=(
            "Verifying a handshake costs real work, so forged ones sent from addresses that do "
            "not exist are a cheap way to load a server. The cookie is WireGuard's answer to "
            "that: while the server is under load it stops doing the work and replies with a "
            "challenge instead, and only a peer really at the address it claims ever receives "
            "the reply and can send it back. A genuine client passes that and connects; a flood "
            "from spoofed addresses never sees the challenge and gets no further. "
            "Switching this on takes the answer away. Under load the handshakes are dropped "
            "where the challenge would have gone out, so a real client gets silence with no "
            "error and no way back in until the flood stops - the cookie was its ticket. "
            "It is the server's own business and no client reads it: nothing is written into "
            "any client config, no peer needs it and nothing has to be re-imported. It also "
            "does not hide anything, and is not a way to stop the server being recognised - a "
            "cookie only ever goes to someone who already has the server's public key and is "
            "flooding it, and H3 and S3 are what shape the reply on the wire. Leave it off "
            "unless something in front of this server already absorbs floods and the replies "
            "are the thing you want gone."
        ),
        must_match_client=False,
        recommended="off - the cookie is worth more than it costs",
    ),
    ParamSpec(
        key="ListenPort",
        group="network",
        label="UDP listen port",
        kind="port",
        help_short="The UDP port the server listens on. Clients dial it directly.",
        help_long=(
            "The port every client's Endpoint points at. 51820 is WireGuard's default and is a "
            "fingerprint on its own, so the installer picks a random high port instead. Any "
            "port from 1 to 65535 is allowed, and a low one is often the whole point: on a "
            "network that only lets 443 or 53 out, that is where the tunnel has to sit, and "
            "awg-quick runs as root so binding a privileged port costs nothing. Saving one "
            "something else already holds is refused and the holding process is named - 443/udp is QUIC "
            "and 53/udp is a local resolver on many hosts - because two services on one port "
            "means the interface fails to come up, and nothing else would say why. Saving a new "
            "port restarts the tunnel and moves the rule in an active ufw or firewalld; a cloud "
            "security group still has to be edited by hand, and every client needs a re-issued "
            "config, because the old endpoint no longer answers."
        ),
        must_match_client=True,
        min=1,
        max=65535,
        recommended="a random port between 20000 and 59999",
        optional=False,
    ),
    ParamSpec(
        key="Address",
        group="network",
        label="Server address inside the tunnel",
        kind="cidr",
        help_short="The server's own tunnel address, and the network clients are allocated from.",
        help_long=(
            "The prefix decides how many clients fit: a /24 holds 253, a /20 holds 4093, and a /16 holds "
            "65533, which is as wide as the panel accepts - a tunnel larger than that is beyond what "
            "the per-client traffic shaping is built to handle, and /30 is the narrowest that still has "
            "room for a client. The server takes the first host and each client gets the "
            "lowest free address from the second upward, with gaps reused. It only has to be a "
            "private range that does not collide with a network your clients are already on - "
            "192.168.1.0/24 is a poor choice for exactly that reason, since it is what half the "
            "world's home routers hand out. Widening the prefix later keeps every existing "
            "client where it is, as long as the network address does not move; changing the "
            "network itself strands every client on an address the server no longer routes, so "
            "each one has to be removed and re-added. Either way it needs a full tunnel restart "
            "rather than a live sync, and the MASQUERADE rule in PostUp/PostDown moves with it."
        ),
        must_match_client=True,
        recommended="10.13.0.1/20",
        default="10.13.0.1/20",
        optional=False,
    ),
    ParamSpec(
        key="MTU",
        group="network",
        label="Tunnel MTU",
        kind="int",
        help_short="Largest packet the tunnel carries. Too high and big packets vanish.",
        help_long=(
            "The tunnel wraps every packet in headers of its own, so the MTU inside has to be "
            "smaller than the path outside. 1400 leaves room for the usual 60 bytes of "
            "overhead on a 1500-byte path plus the S4 junk prefix. Set it too high and you get "
            "the classic half-broken tunnel: small requests work, large downloads and some "
            "websites hang forever, because only full-size packets are being dropped. Below "
            "1280 breaks IPv6. Raising S4 means lowering this by the same amount. Clients "
            "should use the same number, and the panel writes it into every config it "
            "generates."
        ),
        must_match_client=True,
        min=1280,
        max=1420,
        recommended="1400",
        default="1400",
    ),
    ParamSpec(
        key="DNS",
        group="network",
        label="DNS servers for clients",
        kind="iplist",
        help_short="Resolvers written into each client config, comma separated.",
        help_long=(
            "The DNS servers a client uses while the tunnel is up. This matters more than it "
            "looks: if a client keeps using the resolver handed out by the cafe wifi, every "
            "name it looks up is still visible there, which undoes a good part of the point. "
            "Use resolvers reachable from the server side - a public one such as 1.1.1.1, or "
            "the server's own tunnel address if you run a resolver on it. Changing this does "
            "not affect the server at all, but clients keep the old value until their config "
            "is re-issued and re-imported."
        ),
        must_match_client=False,
        # What install.sh writes, which is what every server here actually has.
        # This said 1.1.1.1 while the installer wrote Google's pair, so a fresh
        # install read as sitting on a value the panel did not recommend.
        recommended="8.8.8.8, 8.8.4.4",
        default="8.8.8.8, 8.8.4.4",
    ),
    ParamSpec(
        key="EndpointHost",
        group="network",
        label="Public endpoint address",
        kind="text",
        help_short="The host or IP clients connect to. Blank means auto-detect on the server.",
        help_long=(
            "The address written into every client's Endpoint line. Left blank, the tools ask "
            "the cloud metadata service for the instance's public IPv4 - right on an ordinary "
            "VPS, wrong behind NAT or on a machine with several addresses. Set it explicitly "
            "to a fixed IP, or to a hostname if the address moves and you keep a DNS record "
            "pointed at it. It has to be reachable from the outside world; an internal address "
            "here produces configs that can never connect, and the failure looks exactly like "
            "a firewall problem."
        ),
        must_match_client=True,
        default="",
    ),
    ParamSpec(
        key="EndpointPort",
        group="network",
        label="Public endpoint port",
        kind="port",
        help_short="Port clients connect to. Blank means the same as the listen port.",
        help_long=(
            "Only needed when the port clients dial is not the port the server binds. That "
            "happens behind a NAT or a load balancer that forwards, say, public 443/udp to the "
            "server's real listen port - useful where only a few ports are allowed out. Leave "
            "it blank in the normal case and it follows the listen port automatically, so the "
            "two can never drift apart."
        ),
        must_match_client=True,
        min=1,
        max=65535,
        default="",
    ),
    ParamSpec(
        key="AllowedIPs",
        group="network",
        label="Default routes for new clients",
        kind="iplist",
        help_short="Which traffic a client sends through the tunnel. 0.0.0.0/0 is everything.",
        help_long=(
            "This becomes the AllowedIPs line in each client config, and on the client it is a "
            "routing decision: 0.0.0.0/0 sends all traffic through the VPN, while 10.13.0.0/20 "
            "gives a split tunnel where only traffic to other peers goes through and everything "
            "else uses the local connection. Full tunnel is the right default when the point is "
            "privacy; split tunnel is for using the VPN purely as a private network between "
            "machines. It is a per-client setting, so changing the default here only affects "
            "clients created afterwards."
        ),
        must_match_client=False,
        recommended="0.0.0.0/0",
        default="0.0.0.0/0",
    ),
    ParamSpec(
        key="PersistentKeepalive",
        group="network",
        label="Keepalive interval (seconds)",
        kind="int",
        help_short="How often a client pings the server to hold its NAT mapping open.",
        help_long=(
            "Nearly every client sits behind NAT, and a NAT entry that sees no traffic is "
            "discarded after roughly 30 seconds - after which the server can no longer reach "
            "the client at all and the tunnel looks dead until the client happens to send "
            "something. A 25-second keepalive stays comfortably inside that window. Set it to "
            "0 to switch keepalives off, which only makes sense for a peer with a public "
            "address. Lower values just spend phone battery for no gain."
        ),
        must_match_client=False,
        min=0,
        max=65535,
        recommended="25",
        default="25",
    ),
    ParamSpec(
        key="PostUp",
        group="hooks",
        label="Commands after the tunnel comes up",
        kind="text",
        help_short="Shell commands run once the interface exists. %i is the interface name.",
        help_long=(
            "This is where the NAT and forwarding rules live. The installer writes three "
            "lines: a MASQUERADE rule so client traffic leaves through the server's public "
            "interface, and two FORWARD accepts so the kernel is willing to route it at all. "
            "Delete them and clients still connect but reach nothing, which is the single most "
            "common way to end up with a tunnel that hands out addresses and no internet. "
            "Above them sits 'awg-panel manage enforce', which is the panel's own and is put "
            "back if you remove it: a bring-up loads every peer in the config, disabled ones "
            "included, so without it a restart re-admits everyone a quota, an expiry or an "
            "admin had switched off. Each line runs as root through a shell when the tunnel "
            "starts, in order, and a line that fails aborts the bring-up. Every rule PostUp "
            "adds needs a matching PostDown that undoes it."
        ),
        must_match_client=False,
    ),
    ParamSpec(
        key="PostDown",
        group="hooks",
        label="Commands after the tunnel goes down",
        kind="text",
        help_short="Shell commands run after the interface is torn down. Must undo PostUp.",
        help_long=(
            "The mirror image of PostUp, running when the tunnel stops. The installer's lines "
            "delete exactly the firewall rules PostUp added - note the -D where PostUp used "
            "-A. If the pairs do not match, every restart leaves a duplicate rule behind, and "
            "after enough restarts the firewall is full of near-identical entries and the NAT "
            "behaviour stops making sense to anyone reading it. Keep them symmetrical."
        ),
        must_match_client=False,
    ),
    ParamSpec(
        key="PreDown",
        group="hooks",
        label="Commands before the tunnel goes down",
        kind="text",
        help_short="Runs while the interface still exists. Used to snapshot traffic counters.",
        help_long=(
            "Runs before the interface is destroyed, so anything that needs the tunnel to "
            "still be there belongs here. This installation uses it for exactly one thing: "
            "'awg-panel manage trafficsync', which reads the kernel's byte counters and folds "
            "them into traffic.db. Those counters reset to zero on every restart, so without "
            "the hook an orderly restart quietly loses whatever traffic had not been recorded "
            "yet. It is the panel's own line and is put back if you remove it; doing so does "
            "not break the tunnel, only the accuracy of the all-time usage figures."
        ),
        must_match_client=False,
    ),
]

PARAMS: dict[str, ParamSpec] = {spec.key: spec for spec in _SPECS}

# The advanced group, read off the catalog rather than listed again here:
# it is what the generator fills and what "clear the advanced settings" empties,
# and a second copy of the list is a second thing to forget to update.
ADVANCED_PARAMS: tuple[str, ...] = tuple(
    key for key, spec in PARAMS.items() if spec.group == "advanced"
)

# Which controller feature flag has to be true before a parameter can be used.
# H1-H4 are absent on purpose: a single value works on every module version and
# only a lo-hi range needs the newer kernel, so that one is checked inline.
FEATURE_OF: dict[str, str] = {
    "I1": "imitation_packets",
    "I2": "imitation_packets",
    "I3": "imitation_packets",
    "I4": "imitation_packets",
    "I5": "imitation_packets",
    "HeaderProtectionKey": "header_protection_key",
    "ContentPaddingAddition": "content_padding",
    "RekeyAfterTime": "timers",
    "RekeyTimeout": "timers",
    "RejectAfterTime": "timers",
    "KeepaliveTimeout": "timers",
    "MaxHandshakeAttempts": "timers",
    "RandomTrailers": "random_trailers",
    "DisableCookies": "disable_cookies",
}

_FEATURE_LABEL: dict[str, str] = {
    "imitation_packets": "imitation packets",
    "header_protection_key": "header protection keys",
    "content_padding": "content padding",
    "timers": "timer overrides",
    "header_ranges": "header ranges",
    "random_trailers": "random packet trailers",
    "disable_cookies": "the cookie switch",
}

_INT_RE = re.compile(r"^-?\d+$")
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_TAG_RE = re.compile(r"<\s*([A-Za-z]+)\s*([^>]*?)\s*>")
_HEX_RE = re.compile(r"^0[xX]([0-9A-Fa-f]+)$")
_B64_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_HEX_KEY_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)


def param_list() -> list[dict]:
    """Return every ParamSpec as a JSON-serialisable dict, in catalog order.

    Served verbatim by GET /api/v1/server/params. Keys are the dataclass field
    names, plus `feature`: the controller feature flag the parameter needs, or
    None when it works on every supported module version.
    """
    out: list[dict] = []
    for spec in PARAMS.values():
        row = asdict(spec)
        row["feature"] = FEATURE_OF.get(spec.key)
        out.append(row)
    return out


def validate_params(values: dict[str, str], *, features: dict) -> dict[str, str]:
    """Check a set of parameters and return {key: human message} for the bad ones.

    An empty dict means everything is acceptable. Keys this module does not know
    about are ignored, so a caller can hand over a whole parsed [Interface]
    section without stripping PrivateKey and friends first.

    Cross-field rules only fire when both sides are present in `values`, so pass
    the complete merged set rather than only what changed. A parameter the
    installed module cannot do - per `features` from the controller - is an
    error rather than a silent drop, because a dropped obfuscation parameter
    fails the handshake with nothing in the log.
    """
    errors: dict[str, str] = {}

    def add(key: str, message: str) -> None:
        errors.setdefault(key, message)

    for key, raw in values.items():
        spec = PARAMS.get(key)
        if spec is None:
            continue
        value = "" if raw is None else str(raw).strip()
        if not _is_set(spec, value):
            if not spec.optional:
                add(key, f"{spec.label} cannot be empty.")
            continue
        message = _check_value(spec, value)
        if message:
            add(key, message)

    _check_junk(values, errors, add)
    _check_sizes(values, errors, add)
    _check_data_padding(values, errors, add)
    _check_header_protection(values, errors, add)
    _check_headers(values, errors, add)
    _check_timers(values, errors, add)
    _check_features(values, errors, add, features or {})
    return errors


def warnings_for(values: dict[str, str]) -> list[str]:
    """Non-fatal advisories about a valid configuration.

    These are the things that will not stop the config being written but will
    stop somebody's phone from connecting, so the UI shows them next to the save
    button rather than hiding them in a log.
    """
    out: list[str] = []

    unsafe = [
        key
        for key, spec in PARAMS.items()
        if not spec.importer_safe and _is_set(spec, _get(values, key))
    ]
    if unsafe:
        out.append(
            f"{', '.join(unsafe)}: these arrived with AmneziaWG 3.0 and 3.1, and each needs the "
            "version that added it at both ends. A peer on an older release ignores them, and so "
            "does one that imported a .conf through the Amnezia app, which discards these lines "
            "without saying so. Either way that client negotiates without them, is rejected by "
            "the server, and shows no error at all. Clear them, or make sure every peer is new "
            "enough and is configured by hand."
        )

    # Not "this will not work" like the rest of this function, but "this works
    # and costs you something you may not have meant to spend". Nothing about a
    # flood is visible from the settings page, so the moment the switch goes on
    # is the only moment anyone is in a position to be told.
    if _is_set(PARAMS["DisableCookies"], _get(values, "DisableCookies")):
        out.append(
            "DisableCookies is on: this server no longer answers a handshake flood with a "
            "cookie challenge, which is what lets a genuine client through one while forged "
            "handshakes from spoofed addresses are turned away. While it is under load those "
            "handshakes are dropped instead, and real clients are dropped with them, with no "
            "error at either end. It hides nothing and no client reads it - leave it off "
            "unless something else in front of this server is absorbing floods."
        )

    hostile: list[str] = []
    for key in ("I1", "I2", "I3", "I4", "I5"):
        value = _get(values, key)
        if not value:
            continue
        tags, _ = _parse_imitation(value)
        names = sorted({name for name, _arg, _size in tags} & _APP_HOSTILE_TAGS)
        if names:
            hostile.append(f"{key} ({', '.join('<' + n + '>' for n in names)})")
    if hostile:
        out.append(
            f"{', '.join(hostile)} uses tags the kernel accepts but the Amnezia app rejects: "
            "importing the config fails with error 1000. Keep imitation packets to <r N> and "
            "<b 0xHEX>."
        )

    imitation_keys = [k for k in ("I1", "I2", "I3", "I4", "I5") if k in values]
    if imitation_keys and not any(_get(values, k) for k in imitation_keys):
        out.append(
            "No imitation packets are configured. Handshakes still carry junk and randomised "
            "headers, but the session no longer opens looking like ordinary traffic."
        )

    # The panel refuses to save either of these, so reaching one means the config
    # was written by something else - a hand-edited awg0.conf, or a build from
    # before the check existed. Saying so on the status page is the only way
    # anyone finds out, because neither symptom points anywhere near the cause:
    # large transfers hanging for the first, and for the second an interface that
    # stopped coming up at some restart nobody connects to the setting.
    overrun = data_padding_overrun(values)
    if overrun is not None:
        s4, free = overrun
        out.append(_padding_overrun_message(s4, free, _set_int(values, "MTU") or 0))

    # Only when the padding is in front of us. validate_params reads an absent
    # S4 as the zero the kernel will read it as, which is the case that bites;
    # an advisory over a set that does not carry the fields it is about is just
    # noise, and the preview of a freshly drawn 3.0 group is exactly that set.
    short = header_protection_short(values) if any(k in values for k in HEADER_PROTECTED) else []
    if short:
        out.append(
            f"{', '.join(short)} is below {HEADER_NONCE} while a header protection key is set. "
            f"The nonce that key is used with is read from the first {HEADER_NONCE} bytes of the "
            "padding on each packet, so the kernel refuses this configuration outright: the "
            "interface will not start until the padding is raised or the key is cleared."
        )

    header_values = [_get(values, k) for k in ("H1", "H2", "H3", "H4")]
    if header_values == ["1", "2", "3", "4"]:
        out.append(
            "H1-H4 are at WireGuard's own values 1 to 4, so packet types are not disguised at "
            "all and the flow can be classified from its first byte. Reconfigure replaces them "
            "with four ranges that cannot collide."
        )

    jc = _set_int(values, "Jc")
    if jc and jc > 12:
        out.append(
            f"Jc = {jc} sends {jc} junk packets before every handshake. On a lossy mobile link "
            "that makes reconnecting noticeably slower for no extra protection; 4 to 8 is "
            "usually enough."
        )

    up = _count_rules(_get(values, "PostUp"))
    down = _count_rules(_get(values, "PostDown"))
    if "PostUp" in values and "PostDown" in values and up != down:
        out.append(
            f"PostUp has {up} rule(s) and PostDown has {down}. Every rule PostUp adds should be "
            "removed by a matching PostDown, or each restart leaves another duplicate firewall "
            "rule behind."
        )

    return out


def randomize(
    rng: random.Random | None = None,
    *,
    mtu: int | None = None,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, str]:
    """Generate one server's obfuscation settings, sharing nothing with any other.

    Every value AmneziaWG lets a server choose is drawn here, because a value
    that is the same everywhere is not obfuscation - it is a signature with an
    extra step. Ship a fixed S1 and every install of this script puts its
    handshake initiation on the same number of bytes, and one rule written once
    matches all of them at once. Draw it per server and there is no such rule to
    write: an adversary who characterises one server has learnt nothing about
    the next, which is the difference between one blocked server and every
    server blocked on the same afternoon.

    The bounds are chosen so that no draw can cost anything measurable. The junk
    and padding go on handshakes, which happen about once every two minutes per
    client; only S4 touches data packets, and it is held under the MTU headroom
    so a full-size packet still fits. Pass `mtu` when the interface is not on the
    default, so S4 is drawn against the room that is actually left.

    `profile` chooses which bands to draw from - see Profile. It changes what a
    draw costs and how much it hides, never whether the values are random: the
    default is the band every ParamSpec's help text quotes, and an unknown name
    falls back to it rather than failing, because a profile is a preference and
    this function's job is to return a working set.

    Only <r N> and <b 0xHEX> tags are emitted, because the Amnezia app refuses
    to import a config containing any of the others. Pass `rng` to make the
    result reproducible in tests; the default is SystemRandom, because these
    values are a fingerprint and a predictable fingerprint is worse than none.
    """
    rng = rng or random.SystemRandom()
    band = PROFILES.get(profile, _STANDARD)
    out: dict[str, str] = {}
    out.update(_junk_bounds(rng, band))
    out.update(_padding_sizes(rng, mtu, band))
    out.update(_header_ranges(rng))
    out.update(_imitation_set(rng, band))
    return out


def randomize_advanced(
    rng: random.Random | None = None,
    *,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, str]:
    """Generate the advanced group: a header key, padding, the timers and the switch.

    The group has no generator of its own until now, which left the strongest
    settings the server offers as seven empty boxes an admin was expected to
    fill in from the protocol specification. They are filled here for the same
    reason the obfuscation above is: a value everyone copies out of the same
    document is not a setting, it is a constant, and the timers in particular
    say exactly how often this server handshakes.

    Everything comes back as one consistent set, because these interlock.
    RejectAfterTime is derived rather than drawn: it has to outlast a whole
    rekey cycle, which is RekeyAfterTime plus the time the initiator may spend
    waiting for an answer, and a draw that misses that leaves the tunnel
    stalling for a few seconds on every rekey.

    Content padding comes back as a range rather than a number, which is the
    only shape of it worth writing - see ContentPaddingAddition's help text. It
    is not charged against the MTU: the kernel clamps each packet's padding to
    what its own MTU leaves, so unlike S4 this one can never push a full-size
    packet over the path.

    RandomTrailers comes back on. It is the one member of the group with
    nothing to draw - there is no value, only a switch - and leaving it off
    would make "fill in the advanced settings" quietly produce a group with a
    hole in it. Every other setting here already costs a peer that is too old
    its connection, which is what the confirmation in front of this asks about.

    What this does not do is decide whether the group should be set at all.
    Every one of these needs the AmneziaWG release that added it on the far
    end - 3.0, or 3.1 for RandomTrailers - and the caller is what knows whether
    the installed module even supports them.
    """
    rng = rng or random.SystemRandom()
    band = ADVANCED_PROFILES.get(profile, _ADV_STANDARD)

    rekey_after = rng.randint(*band.rekey_after)
    rekey_timeout = rng.randint(*band.rekey_timeout)
    keepalive = rng.randint(*band.keepalive)
    # The three add up rather than competing. A peer starts a new handshake at
    # RekeyAfterTime and may then wait KeepaliveTimeout + RekeyTimeout before it
    # hears back, so a RejectAfterTime above only the largest of them can still
    # expire the key mid-negotiation. It is also the number the far end measures
    # its own key against - RejectAfterTime minus those two is when a responder
    # gives up waiting and initiates itself - so a floor that leaves them out
    # puts the responder ahead of the initiator and doubles the handshakes,
    # which is the one event on the wire none of this can disguise.
    floor = rekey_after + keepalive + rekey_timeout
    reject_after = min(floor + rng.randint(*band.margin), PARAMS["RejectAfterTime"].max or 7200)

    return {
        "HeaderProtectionKey": _header_protection_key(rng),
        "ContentPaddingAddition": _content_padding(rng, band),
        "RekeyAfterTime": str(rekey_after),
        "RekeyTimeout": str(rekey_timeout),
        "RejectAfterTime": str(reject_after),
        "KeepaliveTimeout": str(keepalive),
        "MaxHandshakeAttempts": str(rng.randint(*band.attempts)),
        "RandomTrailers": "on",
    }


def dns_name_hex(name: str) -> str:
    """Encode a domain as DNS wire-format labels: length byte, bytes, ... , 00.

    Lowercase hex, no separators, matching the dns_name_hex shell function in
    lib/obfs.sh byte for byte.
    """
    out: list[str] = []
    for label in name.split("."):
        raw = label.encode("ascii")
        out.append(f"{len(raw):02x}")
        out.append(raw.hex())
    return "".join(out) + "00"


# --------------------------------------------------------------- generation


def _header_protection_key(rng: random.Random) -> str:
    """A fresh 32-byte key in the 44-character base64 spelling the parser wants.

    Drawn through `rng` rather than from os.urandom so that a seeded generator
    still reproduces a whole set in tests. In production `rng` is SystemRandom,
    which is the same CSPRNG os.urandom reads from.
    """
    return base64.b64encode(bytes(rng.randrange(256) for _ in range(32))).decode("ascii")


def _content_padding(rng: random.Random, band: AdvancedProfile) -> str:
    """A padding range for data packets, or nothing when a range would not help.

    Always a range, never a number. The kernel treats this setting as a
    replacement for the padding it does anyway - without it every packet is
    rounded up to a multiple of PADDING_MULTIPLE, with it the rounding is gone
    and this is used instead - so a constant addition trades a length known to
    within 16 bytes for one known exactly. Only a range buys that back, and only
    a range whose top reaches past the rounding it displaced buys back all of
    it, which is why a draw that lands under PADDING_MULTIPLE comes home empty
    rather than as a value that quietly makes the leak finer.

    The bottom of the range sits well below the top rather than beside it: the
    point is a spread of observed sizes, and 90-96 is barely one.
    """
    high = rng.randint(*band.padding)
    if high < PADDING_MULTIPLE:
        return ""
    return f"{rng.randint(0, high // 3)}-{high}"


def _junk_bounds(rng: random.Random, band: Profile) -> dict[str, str]:
    """How many junk packets precede a handshake, and how big they may be.

    The sizes are already random per packet - the kernel picks one in the range
    for each - but the range itself is observable to anyone who watches enough
    handshakes, so it is drawn too. The count matters for the same reason: a
    burst of exactly four is as good a marker as a burst of exactly one.
    """
    low = rng.randint(*band.jmin)
    return {
        "Jc": str(rng.randint(*band.jc)),
        "Jmin": str(low),
        "Jmax": str(low + rng.randint(*band.jspan)),
    }


def _padding_sizes(
    rng: random.Random,
    mtu: int | str | None = None,
    band: Profile = _STANDARD,
) -> dict[str, str]:
    """The four padding sizes, drawn so no two servers pad a handshake alike.

    S1 and S2 are the ones that matter: they decide the on-wire length of the
    handshake initiation and response, which are otherwise the fixed 148 and 92
    bytes that identify WireGuard outright. S4 is the one that has to be kept
    honest, because it is added to every data packet and comes straight out of
    the usable MTU.
    """
    low, high = band.s
    s1 = rng.randint(low, high)
    s2 = rng.randint(low, high)
    if s2 == s1 + 56:
        # _check_sizes rejects exactly this: it lands the initiation and the
        # response on the same on-wire length, which is a pattern of its own.
        # One step in either direction is all it takes to separate them.
        s2 = low if s2 == high else s2 + 1

    room = MTU_BUDGET - _mtu_or_default(mtu)
    s4_low, s4_high = band.s4
    # The MTU overrules the profile at both ends of the band, not only the top:
    # DPI-resistant asks for 24 upwards and a 1420-byte tunnel leaves 20, and
    # answering that with the profile's floor would overrun while answering it
    # with nothing at all would put S4 below HEADER_NONCE - a set that cannot be
    # combined with a header protection key. So it draws what fits.
    #
    # Under HEADER_NONCE there is no honest answer left, and it is no padding
    # rather than a value that would quietly black-hole every full-size packet.
    # It takes an MTU the panel will not save to get there - the largest it
    # accepts is 1420, which leaves 20 - so a set drawn against a config in that
    # state is one whose MTU is already an error, and that is what is shown.
    s4 = rng.randint(min(s4_low, room), min(s4_high, room)) if room >= HEADER_NONCE else 0

    return {"S1": str(s1), "S2": str(s2), "S3": str(rng.randint(low, high)), "S4": str(s4)}


def _mtu_or_default(mtu: int | str | None) -> int:
    """The MTU to draw S4 against, however the caller happened to have it.

    Config values reach this module as text as often as they do as integers, and
    a caller that passes the string would otherwise get a TypeError somewhere
    inside the arithmetic rather than an answer. Anything unusable falls back to
    the default, which is the conservative direction: it leaves the most room.
    """
    try:
        value = int(str(mtu).strip())
    except (TypeError, ValueError):
        return DEFAULT_MTU
    return value if value > 0 else DEFAULT_MTU


def _header_ranges(rng: random.Random) -> dict[str, str]:
    """Four non-overlapping ranges, placed at random and handed out at random.

    Splitting the space into quarters guarantees the ranges cannot overlap
    without having to check, but a fixed split would leave the quarter a value
    falls in equal to the packet type - which is the whole thing H1-H4 exist to
    hide. So each range is a random sub-interval of its quarter, and the four
    are then shuffled before being assigned, so knowing how they are generated
    still does not say which quarter carries handshakes and which carries data.

    Each range stays at least half a quarter wide, around 268 million values, so
    a repeated header value remains something that does not happen.
    """
    slot = (H_MAX - H_FLOOR + 1) // 4
    bounds: list[tuple[int, int]] = []
    for index in range(4):
        base = H_FLOOR + index * slot
        width = rng.randint(slot // 2, slot)
        start = base + rng.randint(0, slot - width)
        bounds.append((start, start + width - 1))
    rng.shuffle(bounds)
    return {f"H{n}": f"{low}-{high}" for n, (low, high) in enumerate(bounds, start=1)}


def _imitation_set(rng: random.Random, band: Profile = _STANDARD) -> dict[str, str]:
    """Decoy packets for the slots I1-I5, all of one protocol.

    A decoy only works if a filter that parses it believes it, and that rules
    out mixing families: no host on earth speaks DNS, NTP, STUN and QUIC down a
    single UDP socket pair, so a set that does is more distinctive than sending
    nothing at all. One family is picked per server and the whole set is built
    from it, so the flow opens looking like one plausible thing.

    How many packets is drawn too, and it is the one part of the generator that
    an admin pays for on every handshake: each decoy is another send before the
    real exchange starts. The profile decides the band, but the count is still
    random inside it, because a session that is always exactly five packets long
    is its own marker.

    Unused slots come back empty, which is how the callers spell "remove this
    line" rather than leaving a stale packet behind.
    """
    count = min(rng.randint(*band.decoys), 5)
    packets = rng.choice(_FAMILIES)(rng, count)
    out = {f"I{n}": "" for n in range(1, 6)}
    for slot, packet in enumerate(packets[:count], start=1):
        out[f"I{slot}"] = packet
    return out


def _family_media(rng: random.Random, count: int) -> list[str]:
    """A video call starting: ICE connectivity checks, then media.

    The best fit for what this actually is. The tunnel listens on a random high
    port and clients dial it from another one, and the one everyday thing that
    looks like that is a WebRTC media stream - which also explains why the
    packets that follow are small, constant-rate and opaque.

    Truncated from the front, so a one-packet set is the binding request rather
    than a response to a question nobody asked.
    """
    ssrc = _rand_hex(rng, 4)
    payload_type = rng.randrange(96, 128)
    packets = [_stun_request(rng), _stun_response()][:count]
    packets.extend(_rtp(rng, ssrc, payload_type) for _ in range(count - len(packets)))
    return packets


def _family_quic(rng: random.Random, count: int) -> list[str]:
    """A QUIC connection opening: a padded Initial, then 1-RTT packets."""
    packets = [_quic_initial(rng)]
    packets.extend(_quic_short(rng) for _ in range(count - 1))
    return packets


def _family_dns(rng: random.Random, count: int) -> list[str]:
    """Name lookups: each question paired with its answer, and so on.

    Kept as query/response pairs rather than loose responses, because a response
    nobody asked for is the one thing a resolver never sends. An odd count ends
    on a question, which is what an unanswered lookup looks like.
    """
    packets: list[str] = []
    while len(packets) < count:
        domain = rng.choice(DOMAINS)
        packets.append(_dns_query(domain, rng))
        packets.append(_dns_answer(domain, rng.choice(("8180", "8580")), rng))
    return packets[:count]


_FAMILIES = (_family_media, _family_quic, _family_dns)


def _dns_query(domain: str, rng: random.Random) -> str:
    """A recursive query carrying an EDNS0 OPT record, the way resolvers ask now."""
    udp_size = rng.choice(("04d0", "1000", "0500"))
    #    <r 2>  transaction id, fresh on every send
    #    0100   standard query, recursion desired
    #    0001 0000 0000 0001   one question, one additional (the OPT record)
    #    ...    the question, then OPT: root name, type 41, UDP size, no flags
    return (
        f"<r 2><b 0x01000001000000000001{dns_name_hex(domain)}00010001000029{udp_size}000000000000>"
    )


def _dns_answer(domain: str, flags: str, rng: random.Random) -> str:
    """One DNS response: <r 2> transaction ID, then header, question and A record."""
    ttl = f"{rng.randrange(32768) + 120:08x}"
    return (
        f"<r 2><b 0x{flags}0001000100000000{dns_name_hex(domain)}"
        f"00010001c00c00010001{ttl}0004{_routable_ipv4_hex(rng)}>"
    )


def _stun_request(rng: random.Random) -> str:
    """A binding request with the ICE attributes a real call carries.

    Both attribute values are random on every send, which is what they are in
    the real thing too: a priority computed per candidate pair and a tiebreaker
    drawn once per agent.
    """
    attributes = ["<b 0x00240004><r 4>"]  # PRIORITY
    length = 8
    if rng.random() < 0.5:
        attributes.append("<b 0x802a0008><r 8>")  # ICE-CONTROLLING
        length += 12
    return f"<b 0x0001{length:04x}2112a442><r 12>" + "".join(attributes)


def _stun_response() -> str:
    """The success response, carrying the XOR-MAPPED-ADDRESS that was asked for.

    Nothing here is drawn per server because nothing in a binding response
    varies per host: the shape is fixed and the parts that differ - transaction
    ID, the address being reported - are already fresh on every send.
    """
    return "<b 0x0101000c2112a442><r 12><b 0x002000080001><r 6>"


def _rtp(rng: random.Random, ssrc: str, payload_type: int) -> str:
    """One media packet: the 12-byte RTP header, then payload that looks encrypted.

    The SSRC is fixed for the set, because it is what marks these as one stream
    rather than several unrelated packets; the sequence number and timestamp are
    random per send, so no two handshakes replay the same bytes.
    """
    marker = 0x80 if rng.random() < 0.2 else 0x00
    return f"<b 0x80{payload_type | marker:02x}><r 6><b 0x{ssrc}><r {rng.randrange(120, 261)}>"


def _quic_initial(rng: random.Random) -> str:
    """A client Initial, padded past 1200 bytes the way RFC 9000 requires.

    The padding is the point, not an accident of the size. An Initial shorter
    than 1200 bytes is one no client would ever send, so anything that actually
    parses QUIC would flag it - and a 1200-byte first datagram is among the most
    common shapes on the internet, which is exactly what makes it worth copying.
    """
    dcid = rng.choice((4, 8))
    total = rng.randrange(1200, 1233)
    # 6 bytes of long header, the connection ID, then 4 bytes of empty source
    # ID, empty token and the length varint. What is left is packet number and
    # payload, which is also what the length field counts.
    body = total - 10 - dcid
    return f"<b 0xc300000001{dcid:02x}><r {dcid}><b 0x0000{0x4000 | body:04x}><r {body}>"


def _quic_short(rng: random.Random) -> str:
    """A 1-RTT packet from the same connection: short header, then ciphertext.

    The first byte varies because in a real one it is header-protected, so the
    spin bit, key phase and packet number length all come out looking random.
    """
    first = rng.choice((0x43, 0x53, 0x63, 0x73))
    return f"<b 0x{first:02x}><r 8><r {rng.randrange(80, 401)}>"


# Second octets and below are drawn freely; only the first is constrained, and
# only enough to keep the answer off the ranges no public name resolves to.
_BOGON_FIRST = frozenset({10, 100, 127, 169, 172, 192, 198, 203})


def _routable_ipv4_hex(rng: random.Random) -> str:
    """Four bytes that read as a public address rather than an obvious bogon."""
    first = rng.choice([octet for octet in range(1, 224) if octet not in _BOGON_FIRST])
    return f"{first:02x}{_rand_hex(rng, 3)}"


def _rand_hex(rng: random.Random, nbytes: int) -> str:
    return "".join(f"{rng.randrange(256):02x}" for _ in range(nbytes))


# --------------------------------------------------------------- validation


def _is_set(spec: ParamSpec, value: str) -> bool:
    """True when the value would actually be written out.

    A parameter whose value is empty or exactly "0" is not written out at all,
    so for the obfuscation groups those both mean "off" and skip range checks.
    A switch says it in words rather than in a number, and "off" is the same
    answer: nothing to write, nothing to check, nothing to warn about.

    That second reading is on the kind rather than on the group, because the
    groups hold numbers as well. Read "off" as unset for all of them and a
    hand-written `Jc = off` stops being the error it is - the tools parse Jc
    with parse_uint16 and refuse that config - and starts being silently
    skipped, which leaves the settings page with nothing to say about a line
    that will bring the interface down.
    """
    text = value.strip()
    if not text:
        return False
    if spec.kind == "bool":
        return text.lower() != "off"
    return not (spec.group in _OFF_MEANS_UNSET and text == "0")


def is_set(key: str, value: str) -> bool:
    """_is_set by parameter name, for the config writers in store.py.

    Whether a value gets a line in a config is this module's answer to give,
    and the writers used to carry their own copy of it - `value != "0"` - which
    knew about numbers and not about switches. A key this module does not know
    is written as it stands: the caller had a reason to carry it.
    """
    spec = PARAMS.get(key)
    return bool(value.strip()) if spec is None else _is_set(spec, value)


def _get(values: dict[str, str], key: str) -> str:
    raw = values.get(key)
    return "" if raw is None else str(raw).strip()


def _set_int(values: dict[str, str], key: str) -> int | None:
    """Integer value of a parameter that is present and switched on, else None."""
    spec = PARAMS.get(key)
    value = _get(values, key)
    if spec is None or not _is_set(spec, value) or not _INT_RE.match(value):
        return None
    return int(value)


def _count_lines(value: str) -> int:
    return len([line for line in value.splitlines() if line.strip()])


def _count_rules(value: str) -> int:
    """Hook lines that are somebody's firewall rule, so PostUp and PostDown compare.

    The panel writes one PostUp of its own - `awg-panel manage enforce` - and it
    has no PostDown, because it adds nothing to undo. Counting it made every
    server report four PostUp against three PostDown and told the admin their
    rules were unbalanced, on a config the panel had written itself.
    """
    return len(
        [
            line
            for line in value.splitlines()
            if line.strip() and not any(marker in line for marker in MANAGE_MARKERS)
        ]
    )


def _check_value(spec: ParamSpec, value: str) -> str | None:
    if spec.kind in ("int", "port"):
        return _check_int(spec, value)
    if spec.kind == "range":
        return _check_range(spec, value)
    if spec.kind == "imitation":
        return _parse_imitation(value)[1]
    if spec.kind == "key":
        return _check_key(value)
    if spec.kind == "cidr":
        return _check_cidr(spec, value)
    if spec.kind == "iplist":
        return _check_iplist(spec, value)
    if spec.kind == "text":
        return _check_text(spec, value)
    if spec.kind == "bool":
        return _check_bool(value)
    return None


def _check_bool(value: str) -> str | None:
    """The two words `awg setconf` parses for a switch.

    Both of them, though only one arrives: _check_value is reached only for a
    value _is_set called set, and that reads "off" as unset. So a config that
    says "off" in words - one an admin wrote by hand - is skipped rather than
    checked, and saves for that reason rather than this one. "off" is named
    here anyway so the answer does not depend on which caller asks: this
    function is about what the tool parses, not about what the panel writes.
    """
    if value.strip().lower() in ("on", "off"):
        return None
    return "Use on or off, or leave it empty to leave the line out of the config."


def _check_int(spec: ParamSpec, value: str) -> str | None:
    if not _INT_RE.match(value):
        return f"'{value}' is not a whole number."
    number = int(value)
    if spec.min is not None and number < spec.min:
        return f"Must be at least {spec.min}."
    if spec.max is not None and number > spec.max:
        return f"Must be at most {spec.max}."
    return None


def _check_range(spec: ParamSpec, value: str) -> str | None:
    parsed = _parse_range(value)
    if parsed is None:
        return (
            f"'{value}' is neither a number nor a range. Use a single value such as 12345, or "
            "a range such as 5-500000000."
        )
    low, high = parsed
    if low > high:
        return f"The range runs backwards: {low} is larger than {high}."
    lowest = spec.min if spec.min is not None else H_MIN
    highest = spec.max if spec.max is not None else H_MAX
    if low < lowest or high > highest:
        return f"Values must be between {lowest} and {highest}."
    return None


def _parse_range(value: str) -> tuple[int, int] | None:
    match = _RANGE_RE.match(value)
    if match:
        return int(match.group(1)), int(match.group(2))
    if _INT_RE.match(value):
        number = int(value)
        return number, number
    return None


def _check_key(value: str) -> str | None:
    if _B64_KEY_RE.match(value) or _HEX_KEY_RE.match(value):
        return None
    return (
        "Expected a 32-byte key: 44 characters of base64 ending in '=', or 64 hexadecimal "
        "characters."
    )


def _check_cidr(spec: ParamSpec, value: str) -> str | None:
    """The server's own address line: one IPv4 network, and beside it an optional v6 one.

    `Address = 10.13.0.1/20, fd00::1/64` is one line with two families on it,
    which is what a dual-stack install writes and what the page shows back for
    editing. Reading only the first spelling - a single IPv4 address, and
    nothing else - meant that on such a server every save failed on the address
    field, including the ones that never touched it, because a save is checked
    against the whole merged configuration rather than only what changed.

    The allocator is IPv4 and the v6 half is derived from it, so exactly one
    address of each family is what the tunnel can be numbered from. A second of
    either is rejected rather than ignored: awg/subnet.py takes the first entry
    of its family, and a silently unused address is one the admin believes is in
    use.
    """
    entries = [part.strip() for part in value.split(",")]
    if any(not entry for entry in entries):
        return "Empty entry in the list; separate the addresses with a single comma."

    seen: dict[int, str] = {}
    for entry in entries:
        try:
            iface = ipaddress.ip_interface(entry)
        except ValueError:
            return f"'{entry}' is not an address with a prefix, e.g. 10.13.13.1/24."
        if iface.version in seen:
            return (
                f"Two IPv{iface.version} addresses here, '{seen[iface.version]}' and '{entry}'. "
                "The tunnel is one network per family; only the first would ever be used."
            )
        seen[iface.version] = entry
        if spec.key != "Address":
            continue
        message = _check_address4(iface) if iface.version == 4 else _check_address6(iface)
        if message:
            return message

    if 4 not in seen:
        return "Only IPv6 here; the client allocator works from a single IPv4 network."
    return None


def _check_address4(iface: ipaddress.IPv4Interface) -> str | None:
    prefix = iface.network.prefixlen
    if prefix < subnet.MIN_PREFIX or prefix > subnet.MAX_PREFIX:
        return (
            f"Use a prefix between /{subnet.MIN_PREFIX} and /{subnet.MAX_PREFIX}. A "
            f"/{subnet.MAX_PREFIX} is the smallest network with room for the server and one "
            f"client; a /{subnet.MIN_PREFIX} already holds "
            f"{subnet.capacity(ipaddress.ip_network(f'10.0.0.0/{subnet.MIN_PREFIX}')):,} of "
            "them."
        )
    if int(iface.ip) - int(iface.network.network_address) != 1:
        return (
            "The server has to be the first host of its subnet; clients start at the second. "
            f"Try {iface.network.network_address + 1}/{prefix}."
        )
    return None


def _check_address6(iface: ipaddress.IPv6Interface) -> str | None:
    """What awg/subnet6.py will accept, asked before the file is written rather than after.

    The first-address rule is the v4 one for the same reason: a client's v6
    address is its offset in the v4 pool written into the prefix, so a server
    sitting at ::5 is a server that hands its own address to the fifth client.
    """
    try:
        subnet6.parse(str(iface))
    except ValueError as exc:
        return str(exc)
    if int(iface.ip) - int(iface.network.network_address) != 1:
        return (
            "The server has to be the first address of its IPv6 prefix, as it is of its IPv4 "
            f"subnet; clients are numbered upward from there. Try "
            f"{subnet6.server_addr(iface.network)}."
        )
    return None


def _check_iplist(spec: ParamSpec, value: str) -> str | None:
    entries = [part.strip() for part in value.split(",")]
    if any(not entry for entry in entries):
        return "Empty entry in the list; separate the values with a single comma."
    for entry in entries:
        if spec.key == "DNS":
            try:
                ipaddress.ip_address(entry)
            except ValueError:
                return f"'{entry}' is not an IP address. Clients need addresses here, not names."
        else:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                return f"'{entry}' is not a network. Use CIDR notation such as 0.0.0.0/0."
    return None


def _check_text(spec: ParamSpec, value: str) -> str | None:
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        return "Contains a control character; only printable text is allowed here."
    if spec.key == "EndpointHost":
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not _HOSTNAME_RE.match(value):
                return f"'{value}' is neither an IP address nor a host name."
    return None


def _parse_imitation(value: str) -> tuple[list[tuple[str, str, int]], str | None]:
    """Parse an imitation packet template.

    Returns (tags, error). Each tag is (name, argument, bytes it contributes).
    Tags the Amnezia app rejects are parsed successfully - they are legal for
    the kernel - and reported by warnings_for instead.
    """
    tags: list[tuple[str, str, int]] = []
    position = 0
    length = len(value)
    while position < length:
        if value[position].isspace():
            position += 1
            continue
        match = _TAG_RE.match(value, position)
        if not match:
            stray = value[position : position + 12]
            return tags, (
                f"'{stray}' is outside a tag. The whole value is a sequence of tags such as "
                "<r 2><b 0x8580...>."
            )
        name = match.group(1).lower()
        arg = match.group(2).strip()
        if name == "r":
            if not arg.isdigit() or not 1 <= int(arg) <= 1500:
                return tags, f"<r {arg}> needs a byte count between 1 and 1500."
            tags.append((name, arg, int(arg)))
        elif name == "b":
            hex_match = _HEX_RE.match(arg)
            if not hex_match:
                return tags, f"<b {arg}> needs literal bytes written as 0x followed by hex."
            digits = hex_match.group(1)
            if len(digits) % 2:
                return tags, (
                    f"<b {arg}> has an odd number of hex digits; every byte needs two of them."
                )
            tags.append((name, arg, len(digits) // 2))
        elif name in ("t", "c"):
            if arg:
                return tags, f"<{name}> takes no argument."
            tags.append((name, "", 4))
        elif name in ("rc", "rd"):
            if arg and not arg.isdigit():
                return tags, f"<{name} {arg}> needs a count or no argument at all."
            tags.append((name, arg, int(arg) if arg else 0))
        else:
            return tags, (
                f"<{name}> is not a tag AmneziaWG understands. Use <r N> for N random bytes "
                "and <b 0xHEX> for literal bytes."
            )
        position = match.end()

    if not tags:
        return tags, "Empty. Give a sequence of tags, or clear the field to disable this packet."
    total = sum(size for _name, _arg, size in tags)
    if total > JUNK_MAX:
        return tags, (
            f"The packet works out at {total} bytes, over the {JUNK_MAX}-byte limit; it would "
            "be fragmented, which is a signature of its own."
        )
    return tags, None


def _check_junk(values: dict[str, str], errors: dict[str, str], add) -> None:
    low = _set_int(values, "Jmin")
    high = _set_int(values, "Jmax")
    if low is None or high is None or "Jmin" in errors or "Jmax" in errors:
        return
    if low >= high:
        add(
            "Jmax",
            f"Jmax must be larger than Jmin (Jmin is {low}, Jmax is {high}); junk packets are "
            "sized at random between the two.",
        )


def _check_sizes(values: dict[str, str], errors: dict[str, str], add) -> None:
    s1 = _set_int(values, "S1")
    s2 = _set_int(values, "S2")
    if s1 is None or s2 is None or "S1" in errors or "S2" in errors:
        return
    # A WireGuard initiation is 148 bytes and a response 92, so S1 + 56 == S2
    # makes the two padded packets exactly the same size.
    if s1 + 56 == s2:
        add(
            "S2",
            f"S1 + 56 must not equal S2. With S1 = {s1} both handshake packets would come out "
            f"at {148 + s1} bytes, which is a pattern in itself. Move S2 by a few bytes.",
        )


def data_padding_overrun(values: dict[str, str]) -> tuple[int, int] | None:
    """(s4, free) when S4 does not fit inside what the MTU leaves, else None.

    An ordinary 1500-byte path leaves MTU_BUDGET for the tunnel once the outer
    headers and the authentication tag are accounted for, and S4 comes out of
    what is left after the MTU because the kernel pushes it onto the front of an
    already finished packet. Exceed it and nothing reports an error: small
    requests keep working, full-size packets are silently dropped, and large
    transfers hang half-finished.

    S4 is alone in this, which is worth stating because the arithmetic invites
    the opposite conclusion. ContentPaddingAddition also rides on every data
    packet, but it goes inside the encrypted payload and the sender clamps each
    packet's share of it to what that packet leaves below the MTU - so the
    plaintext is never larger than the MTU however wide the range is, and adding
    it to this sum only refuses configurations that work.

    None when there is no MTU to measure against - the same rule every other
    cross-field check follows, since a caller may hand over a partial set.
    """
    mtu = _set_int(values, "MTU")
    if mtu is None:
        return None
    s4 = _set_int(values, "S4") or 0
    free = MTU_BUDGET - mtu
    return (s4, free) if s4 > free else None


def _padding_overrun_message(s4: int, free: int, mtu: int) -> str:
    """One sentence for the overrun, naming the arithmetic and both ways out."""
    return (
        f"S4 = {s4} is added to every data packet, but an MTU of {mtu} leaves only {free} byte(s) "
        f"for it on an ordinary 1500-byte path. Full-size packets would be dropped without an "
        f"error and large transfers would hang. Lower S4 to {free}, or lower the MTU to "
        f"{MTU_BUDGET - s4}."
    )


def _check_data_padding(values: dict[str, str], errors: dict[str, str], add) -> None:
    """Refuse a combination that would black-hole full-size packets.

    Reported against the MTU as well as S4, because the two are edited on
    different pages: an error left only on the field the operator cannot see is
    an error they cannot act on. Either one can be moved to resolve it.
    """
    if "MTU" in errors or "S4" in errors:
        return
    overrun = data_padding_overrun(values)
    if overrun is None:
        return
    s4, free = overrun
    message = _padding_overrun_message(s4, free, _set_int(values, "MTU") or 0)
    for key in ("MTU", "S4"):
        if key in values:
            add(key, message)


#: The four prefixes a header protection key hides behind. The nonce it is used
#: with is read off the front of whichever one the packet in hand carries, so
#: all four have to be long enough to hold one - not only the one being thought
#: about at the time.
HEADER_PROTECTED: tuple[str, ...] = ("S1", "S2", "S3", "S4")


def header_protection_short(values: dict[str, str]) -> list[str]:
    """Which of S1-S4 are too short to carry the header protection nonce.

    Empty when no key is set, because the padding sizes are free to be anything
    then: the prefix is junk that nobody reads. A key stops it being junk. The
    receiver takes the first HEADER_NONCE bytes of it as the nonce it decrypts
    the header with, so a shorter prefix has nowhere to carry one, and the
    kernel refuses the entire device configuration rather than the one
    parameter - `awg setconf` returns EINVAL and `awg-quick up` stops there.

    That is what makes it an error and not an advisory. A saved obfuscation
    change restarts the interface, so the tunnel goes down to apply a
    combination that will not come back up, and the only sign of why is a
    net_dbg line the admin has no reason to go looking for.

    Absent counts as short. A parameter the config omits is one the kernel sees
    as zero, which is the case this catches most often: an install whose S4 was
    drawn before this floor existed, meeting a freshly generated key.
    """
    if not _get(values, "HeaderProtectionKey"):
        return []
    return [key for key in HEADER_PROTECTED if (_set_int(values, key) or 0) < HEADER_NONCE]


def _check_header_protection(values: dict[str, str], errors: dict[str, str], add) -> None:
    """Refuse a header protection key the padding is too short to carry."""
    if "HeaderProtectionKey" in errors:
        return
    short = header_protection_short(values)
    if not short:
        return
    message = (
        f"{', '.join(short)} must be at least {HEADER_NONCE} to use a header protection key: the "
        f"nonce it is applied with is read from the first {HEADER_NONCE} bytes of the padding on "
        "each packet. The kernel refuses the whole configuration otherwise, so the interface "
        "would not come back up. Raise them, or clear the key."
    )
    add("HeaderProtectionKey", message)
    for key in short:
        if key in values:
            add(key, message)


def _check_headers(values: dict[str, str], errors: dict[str, str], add) -> None:
    keys = [key for key in ("H1", "H2", "H3", "H4") if key in values]
    ranges: dict[str, tuple[int, int]] = {}
    for key in keys:
        if key in errors:
            continue
        value = _get(values, key)
        if not _is_set(PARAMS[key], value):
            continue
        parsed = _parse_range(value)
        if parsed is not None:
            ranges[key] = parsed

    if ranges:
        for key in keys:
            if key not in ranges and key not in errors:
                add(
                    key,
                    "H1-H4 have to be set together. Leaving this one empty means its packet "
                    "type keeps WireGuard's own constant while the others are disguised, which "
                    "gives the whole flow away.",
                )

    ordered = [key for key in ("H1", "H2", "H3", "H4") if key in ranges]
    for index, key in enumerate(ordered):
        low, high = ranges[key]
        for earlier in ordered[:index]:
            other_low, other_high = ranges[earlier]
            if low <= other_high and other_low <= high:
                add(
                    key,
                    f"This range overlaps {earlier} ({_fmt_range(ranges[earlier])}). Each packet "
                    "type needs a range of its own, or the receiver cannot tell them apart.",
                )
                break


def _check_timers(values: dict[str, str], errors: dict[str, str], add) -> None:
    rekey_after = _set_int(values, "RekeyAfterTime")
    reject_after = _set_int(values, "RejectAfterTime")
    rekey_timeout = _set_int(values, "RekeyTimeout")
    keepalive = _set_int(values, "KeepaliveTimeout")

    fixable = reject_after is not None and "RejectAfterTime" not in errors
    if fixable and rekey_after and rekey_after >= reject_after:
        add(
            "RejectAfterTime",
            f"RejectAfterTime ({reject_after}s) must be longer than RekeyAfterTime "
            f"({rekey_after}s), or a key expires before its replacement has been agreed and "
            "the tunnel stalls on every cycle.",
        )
        return

    if fixable and rekey_timeout and keepalive and reject_after <= keepalive + rekey_timeout:
        add(
            "RejectAfterTime",
            f"RejectAfterTime must be greater than KeepaliveTimeout + RekeyTimeout "
            f"({keepalive} + {rekey_timeout} = {keepalive + rekey_timeout}s); the protocol "
            "relies on that gap to renegotiate without dropping traffic.",
        )


def _check_features(values: dict[str, str], errors: dict[str, str], add, features: dict) -> None:
    for key, feature in FEATURE_OF.items():
        if key in errors or not _is_set(PARAMS[key], _get(values, key)):
            continue
        if features.get(feature) is False:
            add(key, _unsupported(feature, key))

    for key in ("H1", "H2", "H3", "H4"):
        if key in errors or key not in values:
            continue
        value = _get(values, key)
        if not _is_set(PARAMS[key], value) or "-" not in value:
            continue
        if features.get("header_ranges") is False:
            add(
                key,
                "The installed AmneziaWG does not support header ranges. Give a single number "
                "instead of a lo-hi range, or upgrade the kernel module and tools.",
            )


def _unsupported(feature: str, key: str) -> str:
    label = _FEATURE_LABEL.get(feature, feature)
    # What an unsupported setting costs depends on who else was going to read
    # it. For everything mirrored into a client config the answer is the clients
    # - they use the value, the server ignores it and the handshake fails. A
    # server-only setting has no such other end: it is simply not done, and
    # saying otherwise would send an admin looking for a client to blame.
    consequence = (
        "so it would do nothing at all"
        if key in SERVER_ONLY_PARAMS
        else "so this value would be ignored and the clients that do use it would fail to connect"
    )
    return (
        f"The installed AmneziaWG does not support {label}, {consequence}. Clear it, or "
        "upgrade the kernel module and tools."
    )


def _fmt_range(bounds: tuple[int, int]) -> str:
    low, high = bounds
    return str(low) if low == high else f"{low}-{high}"
