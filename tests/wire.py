"""What a profile actually puts on the wire, in bytes.

The generators draw a tunnel MTU and a set of padding sizes; what decides
whether a client works is neither of those on its own but the size of the UDP
datagram they add up to. That number is not written down anywhere in this
repository - `MTU_BUDGET` is a conclusion drawn from it - so it is written down
here, once, and both halves of the test suite measure against it: tests/obfs.sh
for the shell generator, panel/tests/test_wire_sizes.py for the panel's.

Every constant below is read off the module install.sh builds, and the source
line is named beside it. That is the point of the file: an arithmetic error in
a budget constant is invisible in review, because the constant is a plausible
number either way, and the only thing that makes it checkable is having the
packet layout somewhere a test can walk.

The failure this exists to catch has no symptom at the moment it is made. The
interface comes up, the handshake completes, small requests work. What is
broken is only the full-size packet, and because AmneziaWG clears DF on the
outer datagram (socket.c:85 and socket.c:151, `skb->ignore_df = 1` with a `df`
argument of 0), it is not even dropped - it is fragmented, somewhere out on the
path, and arrives if the fragments do. What an operator sees is throughput that
fluctuates and stalls on a tunnel whose ping is perfect.

Nothing here imports from the panel or from lib/obfs.sh on purpose: a model
that reuses the code under test agrees with it by construction.
"""

from __future__ import annotations

from typing import NamedTuple

# --------------------------------------------------------------- the layout

# vendor/amneziawg-linux-kernel-module/src/messages.h: struct message_data is
# type(4) + key_idx(4) + counter(8), and noise_encrypted_len adds the 16-byte
# Poly1305 tag. MESSAGE_MINIMUM_LENGTH is message_data_len(0), the two together.
MESSAGE_DATA_HEADER = 16
AUTHTAG = 16
MESSAGE_MINIMUM_LENGTH = MESSAGE_DATA_HEADER + AUTHTAG

# The outer headers the datagram is wrapped in once it leaves the interface.
UDP_HEADER = 8
IPV4_HEADER = 20
IPV6_HEADER = 40

# The three handshake messages, from the structs in messages.h. Sizes rather
# than a parser, because they are protocol constants that have not moved since
# WireGuard shipped and a test that recomputed them would only be testing its
# own arithmetic.
HANDSHAKE_INITIATION = 148
HANDSHAKE_RESPONSE = 92
HANDSHAKE_COOKIE = 64

# peer.c:38 and socket.c:335 - what udp_window is before a peer has sent or
# received anything, and what it is reset to when the endpoint moves.
DEFAULT_UDP_WINDOW = 500


class Family(NamedTuple):
    """One address family the endpoint might be reached over."""

    name: str
    header: int


IPV4 = Family("IPv4", IPV4_HEADER)
IPV6 = Family("IPv6", IPV6_HEADER)
FAMILIES = (IPV4, IPV6)


class Link(NamedTuple):
    """One kind of path a client's datagram has to cross intact."""

    name: str
    mtu: int
    why: str


# The first is the one every config must fit and the one the assertions use: a
# plain 1500-byte link is the best case that exists, so a profile that does not
# fit it does not fit anything. The second is reported rather than enforced,
# because how much margin to leave under 1500 is a product decision about who
# this is for and not an arithmetic error - see the module docstring in
# panel/tests/test_wire_sizes.py.
ETHERNET = Link("Ethernet", 1500, "an untunnelled link, the best case there is")
PPPOE = Link("PPPoE", 1492, "DSL and VDSL, where most home clients are")
LINKS = (ETHERNET, PPPOE)


# ------------------------------------------------------------ data packets


def data_packet(mtu: int, s4: int, family: Family = IPV4) -> int:
    """The largest datagram a full-size data packet becomes, outer headers in.

    send.c:288-321 in order: S4 random bytes pushed onto the front of an
    already finished packet, the 16-byte message_data header, the plaintext,
    and the authentication tag. The plaintext is the packet the tunnel was
    handed plus its padding, and both padding paths clamp that sum to the MTU -
    calculate_skb_padding takes min(mtu, ALIGN(len, 16)) and
    randomize_skb_padding takes min(addition, mtu - len) - so a full-size
    packet is exactly MTU bytes of plaintext and gets no padding at all.

    That clamp is why ContentPaddingAddition is absent from this sum. It rides
    on every data packet as S4 does, and there the resemblance stops: it goes
    inside the encrypted payload where the MTU bounds it, S4 goes in front
    where nothing does.
    """
    return s4 + MESSAGE_DATA_HEADER + mtu + AUTHTAG + UDP_HEADER + family.header


def udp_window(mtu: int, s4: int) -> int:
    """The trailer window a peer opens once full-size traffic has flowed.

    send.c:266 on send and receive.c:571 on receive, both `padding +
    MESSAGE_MINIMUM_LENGTH + skb->len`, kept as a high-water mark. Note what it
    is measured from: the packet this peer put on the wire, not one the far end
    acknowledged. A path that is silently fragmenting every full-size packet
    raises this just as readily as one carrying them, so it cannot be read as
    evidence that anything arrived.
    """
    return s4 + MESSAGE_MINIMUM_LENGTH + mtu


# ------------------------------------------------------- handshake packets


def handshake_packet(
    body: int,
    padding: int,
    window: int,
    trailers: bool,
    family: Family = IPV4,
) -> int:
    """The largest datagram one handshake-time packet becomes.

    Everything sent outside the data path goes through
    wg_socket_send_buffer_to_peer (socket.c:190): the initiation (send.c:87),
    the response (send.c:158), each of the Jc junk packets (send.c:75) and each
    of the I1-I5 imitation packets (send.c:56). All four get the same treatment
    - `padding` random bytes in front, then a trailer of
    get_random_u32_below(udp_window - size) bytes appended when RandomTrailers
    is set (peer.h:98-107).

    So with the switch on, every one of those packets is drawn uniformly across
    the whole window: a 20-byte STUN decoy and a 148-byte initiation both
    arrive at any length up to one byte short of the largest data packet the
    peer has sent. This is the part that is easy to get wrong twice - once by
    forgetting the trailer applies to the decoys at all, and once by reading
    the window as a property of the path rather than of the sender.
    """
    size = padding + body
    if trailers and window > size:
        size = window - 1
    return size + UDP_HEADER + family.header


def handshake_burst(
    profile: dict[str, int],
    imitation: tuple[int, ...] = (),
    family: Family = IPV4,
) -> int:
    """The largest datagram anything in a handshake burst becomes.

    `profile` wants MTU, S1, S2, S3, S4, Jmax and RandomTrailers; `imitation`
    is the rendered byte length of each I-packet. The burst is up to Jc junk
    packets, up to five decoys and the initiation, sent back to back, so this
    is the size of its worst member rather than its total.
    """
    trailers = bool(profile.get("RandomTrailers"))
    window = udp_window(profile["MTU"], profile["S4"])
    sizes = [
        handshake_packet(HANDSHAKE_INITIATION, profile["S1"], window, trailers, family),
        handshake_packet(HANDSHAKE_RESPONSE, profile["S2"], window, trailers, family),
        handshake_packet(HANDSHAKE_COOKIE, profile["S3"], window, trailers, family),
        handshake_packet(profile["Jmax"], 0, window, trailers, family),
    ]
    sizes += [handshake_packet(length, 0, window, trailers, family) for length in imitation]
    return max(sizes)


# --------------------------------------------------------------- the verdict


def largest_on_wire(
    profile: dict[str, int],
    imitation: tuple[int, ...] = (),
    family: Family = IPV4,
) -> int:
    """The largest datagram this profile can produce, whatever the packet."""
    return max(
        data_packet(profile["MTU"], profile["S4"], family),
        handshake_burst(profile, imitation, family),
    )


def headroom(profile: dict[str, int], link: Link, imitation: tuple[int, ...] = ()) -> int:
    """Bytes to spare on `link`, taken across both address families.

    Both, because the family is not the operator's to choose: install.sh
    accepts a hostname for --endpoint (valid_endpoint, install.sh:275) and a
    client resolving it to an AAAA pays the extra twenty bytes with nothing
    written down anywhere saying so. Negative means the profile fragments.
    """
    return link.mtu - max(largest_on_wire(profile, imitation, family) for family in FAMILIES)


def explain(profile: dict[str, int], link: Link, imitation: tuple[int, ...] = ()) -> str:
    """One line naming the overrun and where the bytes went."""
    worst = max(FAMILIES, key=lambda family: largest_on_wire(profile, imitation, family))
    total = largest_on_wire(profile, imitation, worst)
    return (
        f"MTU {profile['MTU']} + S4 {profile['S4']} + {MESSAGE_MINIMUM_LENGTH} transport"
        f" + {UDP_HEADER} UDP + {worst.header} {worst.name} = {total} bytes,"
        f" {total - link.mtu} over {link.name}'s {link.mtu}"
    )
