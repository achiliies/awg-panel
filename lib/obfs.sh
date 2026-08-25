# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/obfs.sh - generate a server's DPI-evasion profile.
#
# Self-contained, and the shell half of a pair: panel/awg/validate.py does the
# same job for the panel. The two do not have to agree byte for byte - both
# draw at random, so they could not - but they must draw from the same bounds
# and emit the same packet shapes, or a server reconfigured from the panel
# stops resembling one set up by the installer. tests/obfs.sh checks the output
# of this file against the panel's validator for exactly that reason.
#
# Nothing here is a constant that ends up on the wire. That is the whole point:
# a value every install shares is not obfuscation, it is a signature with an
# extra step, and one filter rule then matches every server running this
# script. Draw them per server and there is no such rule to write.
#
# Sets globals rather than printing, because one generation feeds several
# config keys and the callers decide which of them to write where.
#
# The one description it produces is translated, so it needs i18n.sh - sourced
# here rather than relied on from common.sh, because "self-contained" above is
# load-bearing: tests/obfs.sh sources this file and nothing else, and a t()
# that arrived only via common.sh would make that harness a liar.
#
# Only <r N> and <b 0xHEX> are used in imitation packets: the Amnezia client's
# .conf importer rejects the kernel's other tags (<t> <c> <rc> <rd>) with
# error code 1000. HeaderProtectionKey and ContentPaddingAddition stay unset
# for the same reason - the importer silently drops them, and a server that
# expects them then fails every handshake with no error. RandomTrailers, which
# AmneziaWG 3.1 added, is left unset here on the same grounds: the installer
# has no way to know what every client runs, and the panel is where it can be
# switched on once they are known. Nothing below draws it.

# shellcheck source=lib/i18n.sh
. "$(dirname "${BASH_SOURCE[0]}")/i18n.sh"

# Header values start at 5, because 1-4 are WireGuard's own type constants.
# The ceiling stays inside a signed 32-bit integer: the wire format is an
# unsigned u32 and the top half would be legal, but a parser anywhere in the
# chain reading it as signed would break every handshake with nothing in any
# log, and the extra bit buys almost nothing.
OBFS_H_FLOOR=5
OBFS_H_MAX=2147483647

# An ordinary 1500-byte path leaves 1440 bytes for the tunnel, and S4 comes
# out of that because it rides on every data packet rather than on handshakes.
OBFS_MTU_BUDGET=1440

# The floor under S1-S4. Nothing the installer writes needs it: header
# protection is left unset here, and without a key the padding sizes are junk
# that nobody reads. It is the panel turning one on later that needs it, and by
# then these numbers are already in awg0.conf - the key's nonce is read from the
# first 12 bytes of the prefix on each packet, so a server installed below this
# is one where the header protection button produces a config the kernel refuses
# and an interface that stops coming up. Twelve bytes a packet is a cheap way
# not to have that conversation.
OBFS_HEADER_NONCE=12

# Domains the imitation DNS packets ask about. Same list as the panel's.
OBFS_DOMAINS=(apple.com www.google.com cloudflare.com microsoft.com
              www.icloud.com outlook.com fastly.net akamai.net)

# ---------------------------------------------------------------- randomness
#
# $RANDOM is seeded from the pid and the clock, which is fine for choosing a
# menu colour and not fine for a value whose whole job is to be unguessable.
# Everything below draws from /dev/urandom instead.

rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

# A uniform integer in [lo, hi]. Rejection sampling on a 64-bit draw, so no
# modulo bias: a band of 297 values taken modulo 2^64 would otherwise favour
# its first few by a hair, and doing it properly costs one loop that almost
# never runs twice.
rand_int() {
    # Split across statements on purpose: bash expands every word of a `local`
    # before it creates any of them, so a span computed in the same statement
    # would read lo and hi while they are still unset, which under `set -u` is
    # an error rather than a zero.
    local lo=$1 hi=$2
    local span=$(( hi - lo + 1 ))
    local draw limit
    (( span > 0 )) || { printf '%s' "$lo"; return; }
    limit=$(( (((1 << 62) / span) * span) ))
    while :; do
        draw=$(( 0x$(rand_hex 8) & 0x3fffffffffffffff ))
        (( draw < limit )) && break
    done
    printf '%s' $(( lo + draw % span ))
}

# One of the arguments, chosen uniformly. Takes the elements rather than the
# array's name: a nameref would read better here, but shellcheck cannot follow
# one and reports every array assignment through it as a mistake.
rand_pick() {
    local -a options=("$@")
    printf '%s' "${options[$(rand_int 0 $(( $# - 1 )) )]}"
}

dns_name_hex() {
    local out="" lbl parts; local IFS='.'; read -ra parts <<< "$1"
    for lbl in "${parts[@]}"; do
        out+=$(printf '%02x' "${#lbl}")
        out+=$(printf '%s' "$lbl" | od -An -tx1 | tr -d ' \n')
    done
    printf '%s00' "$out"
}

# ------------------------------------------------------------ junk and sizes

# How many junk packets go ahead of a handshake and how big they may be. The
# sizes are already random per packet - the kernel picks one in the range for
# each - but the range itself is visible to anyone who watches enough
# handshakes, so it is drawn too. Sets JC, JMIN, JMAX.
gen_junk() {
    JC=$(rand_int 3 8)
    JMIN=$(rand_int 24 80)
    JMAX=$(( JMIN + $(rand_int 40 240) ))
}

# The four padding sizes. S1 and S2 are the ones that matter: they set the
# on-wire length of the handshake initiation and response, which are otherwise
# the fixed 148 and 92 bytes that identify WireGuard outright. S4 is the one to
# keep honest, since it is added to every data packet and comes out of the
# usable MTU. Takes the tunnel MTU; sets S1-S4.
gen_sizes() {
    local mtu=${1:-1400} room
    S1=$(rand_int 24 320)
    S2=$(rand_int 24 320)
    # S1 + 56 == S2 puts both handshake packets on the same on-wire length,
    # which is a pattern of its own. One byte either way separates them.
    if (( S2 == S1 + 56 )); then
        S2=$(( S2 == 320 ? 24 : S2 + 1 ))
    fi
    S3=$(rand_int 24 320)
    room=$(( OBFS_MTU_BUDGET - mtu ))
    # No room left means no padding, rather than a value that would quietly
    # black-hole every full-size packet. The floor is OBFS_HEADER_NONCE rather
    # than something smaller so that turning header protection on afterwards
    # never has to redraw these.
    if (( room >= OBFS_HEADER_NONCE )); then
        S4=$(rand_int "$OBFS_HEADER_NONCE" $(( room < 40 ? room : 40 )))
    else
        S4=0
    fi
}

# Four non-overlapping header ranges, placed at random and handed out at
# random. Splitting the space into quarters makes overlap impossible without
# having to check for it, but a fixed split would leave the quarter a value
# lands in equal to the packet type - which is the whole thing H1-H4 exist to
# hide. So each range is a random sub-interval of its quarter, and the four are
# shuffled before being assigned. Sets H1-H4.
gen_header_ranges() {
    local slot base width start i out=() tmp j
    slot=$(( (OBFS_H_MAX - OBFS_H_FLOOR + 1) / 4 ))
    for (( i = 0; i < 4; i++ )); do
        base=$(( OBFS_H_FLOOR + i * slot ))
        width=$(rand_int $(( slot / 2 )) "$slot")
        start=$(( base + $(rand_int 0 $(( slot - width )) ) ))
        out+=( "${start}-$(( start + width - 1 ))" )
    done
    # Fisher-Yates, so which packet type gets which quarter is unguessable too.
    for (( i = 3; i > 0; i-- )); do
        j=$(rand_int 0 "$i")
        tmp=${out[i]}; out[i]=${out[j]}; out[j]=$tmp
    done
    H1=${out[0]}; H2=${out[1]}; H3=${out[2]}; H4=${out[3]}
}

# ------------------------------------------------------------------- decoys

# Imitation packets for the slots I1-I5, all of one protocol.
#
# A decoy only works on a filter that parses it, and that rules out mixing
# families: no host on earth speaks DNS, NTP, STUN and QUIC down one UDP socket
# pair, so a set that does is more distinctive than sending nothing at all. One
# family is drawn per server and the whole set built from it, so the flow opens
# looking like one plausible thing.
#
# Unused slots come back empty, which is how the callers spell "remove this
# line". Sets I1-I5 and GEN_DESC.
gen_imitation() {
    OBFS_PACKETS=()
    case $(rand_int 0 2) in
        0) _family_media ;;
        1) _family_quic ;;
        *) _family_dns ;;
    esac
    I1=${OBFS_PACKETS[0]:-}; I2=${OBFS_PACKETS[1]:-}; I3=${OBFS_PACKETS[2]:-}
    I4=${OBFS_PACKETS[3]:-}; I5=${OBFS_PACKETS[4]:-}
}

# A video call starting: ICE connectivity checks, then media. The best fit for
# what this actually is - the tunnel listens on a random high port and clients
# dial it from another one, and the everyday thing that looks like that is a
# WebRTC stream, which also explains why the packets after it are small,
# constant-rate and opaque.
_family_media() {
    local ssrc pt media n
    ssrc=$(rand_hex 4)
    pt=$(rand_int 96 127)
    media=$(rand_int 1 3)
    OBFS_PACKETS=( "$(_stun_request)" "$(_stun_response)" )
    for (( n = media; n > 0; n-- )); do
        OBFS_PACKETS+=( "$(_rtp "$ssrc" "$pt")" )
    done
    GEN_DESC="$(t "a WebRTC call - two ICE probes, then ${media} packet(s) of media" \
                  "звонок WebRTC — две проверки ICE, затем ${media} медиапакет(а/ов)")"
}

# A QUIC connection opening: a padded Initial, then 1-RTT packets.
_family_quic() {
    local n
    OBFS_PACKETS=( "$(_quic_initial)" )
    for (( n = $(rand_int 2 4); n > 0; n-- )); do
        OBFS_PACKETS+=( "$(_quic_short)" )
    done
    GEN_DESC="$(t "a QUIC connection (${#OBFS_PACKETS[@]} packets)" \
                  "соединение QUIC (пакетов: ${#OBFS_PACKETS[@]})")"
}

# Name lookups, each question paired with its answer, then sometimes one more
# question. Loose responses are the one thing a resolver never sends.
_family_dns() {
    local domain n
    OBFS_PACKETS=()
    for (( n = 0; n < 2; n++ )); do
        domain=$(rand_pick "${OBFS_DOMAINS[@]}")
        OBFS_PACKETS+=( "$(_dns_query "$domain")" )
        OBFS_PACKETS+=( "$(_dns_answer "$domain")" )
    done
    if (( $(rand_int 0 1) )); then
        OBFS_PACKETS+=( "$(_dns_query "$(rand_pick "${OBFS_DOMAINS[@]}")")" )
    fi
    GEN_DESC="$(t "name lookups - ${#OBFS_PACKETS[@]} packets of DNS" \
                  "запросы имён — пакетов DNS: ${#OBFS_PACKETS[@]}")"
}

# A recursive query carrying an EDNS0 OPT record, the way resolvers ask now.
#   <r 2>  transaction id, fresh on every send
#   0100   standard query, recursion desired
#   0001 0000 0000 0001   one question, one additional (the OPT record)
#   then the question, then OPT: root name, type 41, UDP size, no flags
_dns_query() {
    local sizes=(04d0 1000 0500)
    printf '<r 2><b 0x01000001000000000001%s00010001000029%s000000000000>' \
        "$(dns_name_hex "$1")" "$(rand_pick "${sizes[@]}")"
}

# The matching answer: header, the same question, then one A record.
_dns_answer() {
    local flags_opts=(8180 8580)
    printf '<r 2><b 0x%s0001000100000000%s00010001c00c00010001%08x0004%s>' \
        "$(rand_pick "${flags_opts[@]}")" "$(dns_name_hex "$1")" \
        "$(rand_int 120 32887)" "$(_routable_ipv4_hex)"
}

# Four bytes that read as a public address rather than an obvious bogon: only
# the first octet is constrained, and only enough to keep the answer off the
# ranges no public name resolves to.
_routable_ipv4_hex() {
    local first
    while :; do
        first=$(rand_int 1 223)
        case $first in 10|100|127|169|172|192|198|203) continue ;; esac
        break
    done
    printf '%02x%s' "$first" "$(rand_hex 3)"
}

# A STUN binding request with the ICE attributes a real call carries. Both
# attribute values are random on every send, which is what they are in the real
# thing too: a priority computed per candidate pair, and a tiebreaker drawn
# once per agent.
_stun_request() {
    # The length field counts the attribute bytes only, so it is accumulated
    # alongside them rather than written as a total: a STUN parser checks the
    # two against each other, and a decoy that fails that check is worse than
    # no decoy at all.
    local attrs='<b 0x00240004><r 4>'                  # PRIORITY: 4 + 4 bytes
    local len=8
    if (( $(rand_int 0 1) )); then
        attrs+='<b 0x802a0008><r 8>'                   # ICE-CONTROLLING: 4 + 8
        len=$(( len + 12 ))
    fi
    printf '<b 0x0001%04x2112a442><r 12>%s' "$len" "$attrs"
}

# The success response, carrying the XOR-MAPPED-ADDRESS it was asked for.
# Nothing here is drawn per server because nothing in a binding response varies
# per host: the shape is fixed, and the parts that differ are already fresh on
# every send.
_stun_response() {
    printf '<b 0x0101000c2112a442><r 12><b 0x002000080001><r 6>'
}

# One media packet: the 12-byte RTP header, then payload that looks encrypted.
# The SSRC is fixed for the set, because it is what marks these as one stream
# rather than several unrelated packets; sequence number and timestamp are
# random per send, so no two handshakes replay the same bytes.
_rtp() {
    local ssrc=$1 pt=$2 marker=0
    if (( $(rand_int 0 4) == 0 )); then
        marker=128
    fi
    printf '<b 0x80%02x><r 6><b 0x%s><r %s>' \
        $(( pt | marker )) "$ssrc" "$(rand_int 120 260)"
}

# A client Initial, padded past 1200 bytes the way RFC 9000 requires. The
# padding is the point rather than an accident of the size: an Initial shorter
# than that is one no client would ever send, so anything that actually parses
# QUIC would flag it, and a 1200-byte first datagram is among the most common
# shapes on the internet.
#
# 6 bytes of long header, the connection ID, then 4 bytes of empty source ID,
# empty token and the length varint. What is left is packet number and payload,
# which is also what the length field counts.
_quic_initial() {
    local dcids=(4 8) dcid total body
    dcid=$(rand_pick "${dcids[@]}")
    total=$(rand_int 1200 1232)
    body=$(( total - 10 - dcid ))
    printf '<b 0xc300000001%02x><r %s><b 0x0000%04x><r %s>' \
        "$dcid" "$dcid" $(( 0x4000 | body )) "$body"
}

# A 1-RTT packet from the same connection. The first byte varies because in a
# real one it is header-protected, so the spin bit, key phase and packet number
# length all come out looking random.
_quic_short() {
    local firsts=(43 53 63 73)
    printf '<b 0x%s><r 8><r %s>' "$(rand_pick "${firsts[@]}")" "$(rand_int 80 400)"
}

# --------------------------------------------------------------- everything

# One complete profile. Takes the tunnel MTU, so S4 is drawn against the room
# that is actually left. Sets JC, JMIN, JMAX, S1-S4, H1-H4, I1-I5, GEN_DESC.
gen_obfuscation() {
    gen_junk
    gen_sizes "${1:-1400}"
    gen_header_ranges
    gen_imitation
}
