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
# error code 1000.
#
# The AmneziaWG 3.0 group - the header protection key, the content padding and
# the timers - is drawn here too, and did not used to be. It was held back
# because the app's .conf importer drops those lines silently, so a server that
# expected them refused every client that had been set up that way and said
# nothing about why. What has changed is the fleet: 3.0 is what a current
# AmneziaWG speaks, the panel draws the group as part of one profile rather
# than as a beta somebody opts into, and a server whose strongest settings
# depend on an operator finding a second button is a server that mostly does
# not have them. So the installer draws what the panel draws, from the same
# bands, and the two stay the pair this file has always been half of.
#
# RandomTrailers is drawn with them, and used to be the exception. It arrived in
# 3.1 rather than 3.0 - one release newer - and a peer without it measures an
# arriving handshake, finds it longer than the one it expects and drops it with
# no error at either end, so it was held back as a switch to turn on once the
# fleet was known to be there. The fleet has moved: the module and tools this
# installer builds are 3.1, and so are the official AmneziaWG clients the README
# links to. Be plain about who is newly shut out, because somebody is.
#
# Where a header protection key was drawn, that is a peer on exactly 3.0:
# anything older already fails on the key beside it, and fails the same silent
# way. Where one was not - an --mtu of 1409 or more leaves S4 no room for the
# nonce, and no key is written at all - there is nothing else in the config that
# fails outright, because a peer too old for the padding or the timers ignores
# those lines and stays up. On that server this switch is the only hard stop,
# and it turns away every client below 3.1 rather than one release of them.
# That is the cost, and --mtu is where it is paid.
#
# What was left either way was the strongest setting on the page waiting for an
# operator to go and find a button, which is the argument that emptied the 3.0
# group and is answered the same way.
#
# It is a switch rather than a value, so the two ends have nothing to agree on
# beyond both having it, and it does nothing to data packets while
# ContentPaddingAddition is set - that one already decides their padding. What
# it covers is the handshake, whose length is otherwise the same every time.
#
# DisableCookies, 3.1's other addition, is left unset for a different reason:
# not a client that cannot read it, but a default worth keeping. It stops the
# server counting itself as under load, so it never asks for the cookie that
# answers a handshake flood and verifies every forged one in full instead - and
# off is what a server wants unless something in front of it is already
# absorbing that work. It is not obfuscation and nothing below draws it either.

# shellcheck source=lib/i18n.sh
. "$(dirname "${BASH_SOURCE[0]}")/i18n.sh"

# Header values start at 5, because 1-4 are WireGuard's own type constants.
# The ceiling stays inside a signed 32-bit integer: the wire format is an
# unsigned u32 and the top half would be legal, but a parser anywhere in the
# chain reading it as signed would break every handshake with nothing in any
# log, and the extra bit buys almost nothing.
OBFS_H_FLOOR=5
OBFS_H_MAX=2147483647

# How wide each of the four ranges is drawn, and why it is narrow.
#
# RandomTrailers makes a packet's length unbounded, so receive.c stops testing
# an arriving handshake for an exact length and tests it for a minimum instead:
# `skb->len >= expected_len` where it used to be `==`. That length test was
# carrying most of the work of telling a handshake from a data packet. What is
# left is u32_range_contains against H1, H2 and H3, read at the S1, S2 and S3
# offsets of a packet whose bytes there are junk, ciphertext or a protected
# header - uniform over the whole u32 space. So a range covering a fraction of
# that space is that fraction of every data packet misfiled as a handshake,
# queued to the handshake worker, failed on its MAC and dropped.
#
# These were drawn at half a quarter to a whole quarter of the space each, which
# is 6 to 12 percent apiece: a quarter of all data packets above roughly 470
# bytes lost per direction, in each direction, from the moment the switch went
# on. Small packets stay under the length floors and survive, which is what made
# it read as a throughput problem rather than a broken tunnel. At this width the
# same figure is under three in a million.
#
# What the narrow window costs is per-packet spread: a header value now falls in
# one of a few thousand rather than a few hundred million, so values repeat.
# Where the range sits is still drawn across its whole quarter - around 29 bits,
# and that is the part that hides which range belongs to which packet type - and
# on any server that drew a header protection key the type field is chacha20
# encrypted on the wire anyway, so the width is not observable there at all.
#
# The width is drawn rather than fixed so that it is not itself the constant,
# and the ceiling is what keeps the switch above safe to leave on. Raising it
# and leaving RandomTrailers set is the whole bug again.
OBFS_H_WIDTH_LO=1024
OBFS_H_WIDTH_HI=4096

# The ceiling a profile that already exists is measured against, as opposed to
# the band above, which is what a fresh draw is held to. Sixteen times the
# widest this file draws, so nothing it produced can trip it, and the mirror of
# H_WIDTH_WARN in panel/awg/validate.py: the panel reports the same finding on
# the same numbers when the same config is opened there, and a server should
# not hear two different answers depending on which one it asked.
OBFS_H_WIDTH_WARN=65536

# The width of one header range as a config spells it: "start-end", or a bare
# number, which is a range of one. Everything else is 0 - an unwritten H is not
# a wide one, and neither is a reversed pair somebody typed by hand.
obfs_h_width() {
    local v=$1
    if [[ $v =~ ^([0-9]+)-([0-9]+)$ ]] && (( BASH_REMATCH[2] >= BASH_REMATCH[1] )); then
        printf '%s' $(( BASH_REMATCH[2] - BASH_REMATCH[1] + 1 ))
    elif [[ $v =~ ^[0-9]+$ ]]; then
        printf 1
    else
        printf 0
    fi
}

# The share of data packets a total header width costs, phrased the way
# warnings_for phrases the same finding in the panel: a percentage once it is
# one, and a count below that. A percentage of the bottom of this band rounds
# to "0.0%", and a warning about silent packet loss that reports no loss is
# worse than no warning at all.
#
# The whole phrase, "about" included, rather than a bare figure the caller
# wraps. The two halves do not take the same sentence in either language - one
# is a share and the other is an ordinal - and a caller assembling them would
# have to know which it got back.
obfs_loss_phrase() {
    local total=$1 tenths whole frac
    (( total > 0 )) || { printf '%s' "$(t "nothing" "ничего")"; return; }
    tenths=$(( total * 1000 / 4294967296 ))
    if (( tenths >= 10 )); then
        whole=$(( tenths / 10 ))
        frac=$(( tenths % 10 ))
        printf '%s' "$(t "about ${whole}.${frac}% of data packets" \
                         "около ${whole}.${frac}% пакетов данных")"
    else
        printf '%s' "$(t "about 1 in $(( 4294967296 / total )) data packets" \
                         "примерно 1 из $(( 4294967296 / total )) пакетов данных")"
    fi
}

# The pairing that costs packets, read off a profile as written rather than as
# drawn: RandomTrailers set, over header ranges wider than anything this file
# would draw. Takes the four values as they appear in the config, because the
# caller owns reading them and the places they are read from differ.
#
# Returns 0 when the pairing is present, and then OBFS_TRAILER_WIDE names the
# ranges that are too wide and OBFS_TRAILER_LOSS says what they cost. H4 is not
# among them: receive.c tests the transport range last and by minimum length
# either way, so a wide H4 misfiles nothing - it is H1, H2 and H3 that are
# tested first and that a data packet can fall into.
#
# Usage: obfs_trailer_footgun "$RandomTrailers" "$H1" "$H2" "$H3"
obfs_trailer_footgun() {
    local rt=$1 total=0 width n=0
    OBFS_TRAILER_WIDE=""
    OBFS_TRAILER_LOSS=""

    # parse_bool's answer, which is all the tools read here. Anything else on
    # the line, the empty string and a missing line included, is a switch that
    # is off, and an off switch leaves any width safe.
    case ${rt,,} in
        on|true|yes|1) ;;
        *) return 1 ;;
    esac

    shift
    for width in "$@"; do
        n=$(( n + 1 ))
        width=$(obfs_h_width "$width")
        (( total += width ))
        (( width > OBFS_H_WIDTH_WARN )) &&
            OBFS_TRAILER_WIDE+="${OBFS_TRAILER_WIDE:+, }H${n}"
    done

    [[ -n "$OBFS_TRAILER_WIDE" ]] || return 1
    OBFS_TRAILER_LOSS=$(obfs_loss_phrase "$total")
}

# What an ordinary 1500-byte path leaves for the tunnel once the datagram is
# wrapped: 8 bytes of UDP, 32 of transport header and authentication tag, and
# the outer IP header. S4 comes out of it too, because it rides on every data
# packet rather than on handshakes.
#
# 40 for that IP header and not 20, which is the whole of what this constant
# got wrong until now. The address family is not the operator's to pick: the
# installer takes a hostname for --endpoint, and a client that resolves it to
# an AAAA pays the larger header with nothing in its config saying so. The
# module this installer builds reserves the same way and for the same reason -
# `dev->mtu = ETH_DATA_LEN - overhead` with `overhead` counting
# max(sizeof(struct ipv6hdr), sizeof(struct iphdr)) (device.c) - so a budget of
# 1440 was 20 bytes more generous than the kernel it was budgeting for.
#
# What those 20 bytes bought was a datagram that did not fit the wire. Nothing
# reports that: AmneziaWG clears DF on the outer packet (skb->ignore_df in
# socket.c), so it is not refused with an ICMP anything can act on, it is
# fragmented - and fragments are dropped by the middleboxes and reassembly
# queues out on a real path, not in any lab. What the operator hears is that
# downloads stall.
#
# tests/wire.py holds the packet layout this is derived from, and tests/mtu.sh
# puts a tunnel on a narrow link and counts the fragments, so this number is
# now checked rather than asserted.
OBFS_MTU_BUDGET=1420

# The tunnel MTU everything here assumes when a caller names none. Chosen so
# the budget leaves more room than the S4 band can spend: the ceiling that
# binds is then the band's own 40 rather than whatever is left over, which
# puts the largest data packet at 1492 - the PPPoE link most home clients sit
# behind - instead of at whatever the budget happens to allow. install.sh
# carries the same number as a literal, because that is the one the panel's
# test_clientsenv.py reads back to keep the installer, clients.env and the
# panel's own default from drifting apart.
OBFS_DEFAULT_MTU=1372

# The floor under S1-S4. The key's nonce is read from the first 12 bytes of the
# prefix on each packet, so a padding size below this is one the kernel refuses
# the whole device configuration over: `awg setconf` returns EINVAL and
# `awg-quick up` stops there. It used to be a floor kept for the panel's
# benefit, against the day somebody turned header protection on over numbers
# already written; now the key is drawn in the same run as the padding, and it
# is what makes that pair safe to write in the first place. Twelve bytes a
# packet is a cheap way not to have the conversation either way.
OBFS_HEADER_NONCE=12

# The bands the AmneziaWG 3.0 group is drawn from: the standard profile of
# ADVANCED_PROFILES in panel/awg/validate.py, which is the one this file
# mirrors - install.sh offers no profile picker, and standard is the band every
# parameter's help text quotes.
#
# Nothing here trades bandwidth, which is what makes them a second table rather
# than more rows in the first. The timers decide how often a handshake happens
# at all, which is the one event on the wire that obfuscation cannot make
# cheap; the content padding rides inside the encrypted packet and the sender
# clamps it to what the MTU leaves, so unlike S4 it is charged against nothing.
OBFS_CPA_LO=32
OBFS_CPA_HI=96
OBFS_REKEY_AFTER_LO=120
OBFS_REKEY_AFTER_HI=180
OBFS_REKEY_TIMEOUT_LO=4
OBFS_REKEY_TIMEOUT_HI=7
OBFS_KEEPALIVE_LO=8
OBFS_KEEPALIVE_HI=15
OBFS_ATTEMPTS_LO=16
OBFS_ATTEMPTS_HI=24
# How far above the protocol's own floor RejectAfterTime is placed.
OBFS_MARGIN_LO=60
OBFS_MARGIN_HI=180
# RejectAfterTime's ceiling, from the parameter's own bounds in the panel.
OBFS_REJECT_MAX=7200

# The kernel rounds every packet up to a multiple of this when no content
# padding is set, which hides the last four bits of a length for free. Content
# padding replaces that rounding rather than adding to it, so a range whose top
# is under this buys less than writing nothing does - see the ContentPaddingAddition
# help text in the panel, and the draw in gen_advanced.
OBFS_PADDING_MULTIPLE=16

# Domains the imitation DNS packets ask about. Same list as the panel's.
OBFS_DOMAINS=(apple.com www.google.com cloudflare.com microsoft.com
              www.icloud.com outlook.com fastly.net akamai.net)

# ---------------------------------------------------------------- randomness
#
# $RANDOM is seeded from the pid and the clock, which is fine for choosing a
# menu colour and not fine for a value whose whole job is to be unguessable.
# Everything below draws from /dev/urandom instead.

rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

# A 32-byte key in the one spelling awg's .conf parser takes: 44 characters of
# base64 ending in '='. config.c parses HeaderProtectionKey through parse_key,
# which is key_from_base64 and nothing else - key_from_hex exists in the same
# tools but is only ever used on the UAPI socket, never on a file. A hex key in
# awg0.conf is "Key is not the correct length or format" and then a refusal of
# the whole config, so this is not a spelling preference.
#
# base64(1) is coreutils, the same package as the od(1) rand_hex already needs,
# so it is not a dependency this adds.
rand_key() { head -c 32 /dev/urandom | base64; }

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

# A sub-interval of [lo, hi], printed as "a-b" with b strictly above a.
#
# The mirror of _timer_range in panel/awg/validate.py, and it exists for the
# same reason: `awg setconf` parses the AmneziaWG 3.0 timers with
# u16_range_from_string, and the kernel calls u16_range_pick_one every time it
# arms one - a fresh draw per event. A single number leaves the handshake
# cadence, the one thing on the wire that no padding can disguise, strictly
# periodic. What is drawn here is the range's position as well as its width,
# because a range every install shares is a signature exactly the way a value
# every install shares is one - the argument this whole file is built on.
rand_range() {
    local lo=$1 hi=$2 low
    (( hi > lo )) || { printf '%s' "$lo"; return; }
    low=$(rand_int "$lo" $(( hi - 1 )))
    printf '%s-%s' "$low" "$(rand_int $(( low + 1 )) "$hi")"
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
    local mtu=${1:-$OBFS_DEFAULT_MTU} room
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
    # fragment every full-size packet. The floor is OBFS_HEADER_NONCE rather
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
#
# The sub-interval is narrow, and has to stay narrow for as long as
# gen_advanced draws RandomTrailers: with the switch on, the width of these
# ranges is the rate at which the kernel misfiles data packets as handshakes.
# See OBFS_H_WIDTH_LO above - it is the whole of the reason the two are coupled.
gen_header_ranges() {
    local slot base width start i out=() tmp j
    slot=$(( (OBFS_H_MAX - OBFS_H_FLOOR + 1) / 4 ))
    for (( i = 0; i < 4; i++ )); do
        base=$(( OBFS_H_FLOOR + i * slot ))
        width=$(rand_int "$OBFS_H_WIDTH_LO" "$OBFS_H_WIDTH_HI")
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
# ------------------------------------------------- the AmneziaWG 3.0 group

# What `awg set` prints when it is given no interface, lowercased. Read once
# and kept, because the answer cannot change inside one run and the generator
# asks three times. "-" stands for "there was nothing to ask", which is a
# different answer from an empty usage text.
OBFS_AWG_USAGE=""

# True when the installed tools name every one of these tokens in that usage.
#
# Every token, not any of them, and each call names the exact argument for each
# line it is gating. The alternative spellings this used to hedge with were
# guesses, and two of the four in the timer probe could never have matched
# anything: upstream is inconsistent about the separator - `rekey-after-time`
# and `header-protection-key` with hyphens, `reject_after_time`,
# `keepalive_timeout` and `max_handshake_attempts` with underscores - so a
# guessed spelling is not a safety net, it is a probe that passes on one token
# and then writes five lines. The list is vendor/amneziawg-tools/src/set.c at
# the commit this release pins.
#
# The tool's argument names are the config keys lowercased, so a hit is hard
# evidence. A miss is evidence too, and is treated as one here: a line the tools
# cannot parse is not a setting that quietly does nothing, it is `awg setconf`
# refusing the file and an interface that never comes up at all, on a box the
# operator is watching install itself.
#
# Not being able to ask is the third answer, and it is a yes. That is what the
# panel does with the same probe, for the same reason - a guess that writes the
# setting can be corrected, a guess that silently drops it leaves a server
# quietly weaker than the one the operator thinks they installed. It cannot
# happen inside install.sh, which builds and installs the release it pins well
# before this and runs `awg genkey` in the same breath as writing the config;
# it is what lets tests/obfs.sh source this file on a machine with no AmneziaWG
# on it and still be checking something.
awg_supports() {
    local token
    if [[ -z "$OBFS_AWG_USAGE" ]]; then
        if command -v awg >/dev/null 2>&1; then
            OBFS_AWG_USAGE=$(awg set 2>&1 | tr '[:upper:]' '[:lower:]')
        fi
        [[ -n "$OBFS_AWG_USAGE" ]] || OBFS_AWG_USAGE="-"
    fi
    [[ "$OBFS_AWG_USAGE" == "-" ]] && return 0
    for token in "$@"; do
        [[ "$OBFS_AWG_USAGE" == *"$token"* ]] || return 1
    done
    return 0
}

# The header protection key, the content padding, the trailer switch and the
# timers, drawn as one consistent set. Sets HPK, CPA, RANDOM_TRAILERS,
# REKEY_AFTER, REKEY_TIMEOUT, REJECT_AFTER, KEEPALIVE_TIMEOUT and MAX_ATTEMPTS;
# any of them may come back empty, and an empty one is a line the caller must
# not write rather than a value to write blank.
#
# Call after gen_sizes: the key is only drawn when the padding it would be
# carried in can hold its nonce. Writing one over a shorter prefix is not a
# setting that underperforms, it is an interface that does not come up, and the
# only case that reaches it is an MTU high enough to leave S4 no room - which
# --mtu allows and the panel does not.
gen_advanced() {
    local MARGIN REJECT_LO REJECT_HI
    HPK=""; CPA=""; RANDOM_TRAILERS=""
    REKEY_AFTER=""; REKEY_TIMEOUT=""; REJECT_AFTER=""
    KEEPALIVE_TIMEOUT=""; MAX_ATTEMPTS=""

    if awg_supports header-protection-key; then
        local size floor=1
        for size in "$S1" "$S2" "$S3" "$S4"; do
            (( size >= OBFS_HEADER_NONCE )) || floor=0
        done
        # The same 44-character base64 the panel writes, because it is the only
        # spelling `awg setconf` reads - see rand_key. Not `awg genkey`, whose
        # output is a curve25519 private key with three bits clamped: harmless
        # for a symmetric key, and still three bits nobody has to give away.
        (( floor )) && HPK=$(rand_key)
    fi

    if awg_supports content-padding-addition; then
        # A range, never a number: this replaces the rounding the kernel does
        # anyway, so a constant addition trades a length known to within
        # OBFS_PADDING_MULTIPLE bytes for one that tracks the packet inside it
        # byte for byte. The bottom sits well below the top because the point is
        # a spread of observed sizes, and 90-96 is barely one.
        local high
        high=$(rand_int "$OBFS_CPA_LO" "$OBFS_CPA_HI")
        (( high >= OBFS_PADDING_MULTIPLE )) && CPA="$(rand_int 0 $(( high / 3 )))-${high}"
    fi

    # On, and the only member of the group that is a switch rather than a draw.
    # A handshake is the same length every time it is sent - that is a pattern
    # to match on even when every byte inside it is random, and the one part of
    # the profile that padding drawn per server does not touch, because what is
    # constant is the length itself and not its value. The kernel appends a
    # trailer of random length to each packet it sends, sized against what the
    # path has already carried, so it never pushes one over the MTU and there is
    # no budget to charge it against.
    #
    # It is safe to leave on only because gen_header_ranges draws H1-H4 narrow.
    # With the switch on, receive.c tests an arriving handshake for a minimum
    # length rather than an exact one, and the header ranges are then the only
    # thing separating a handshake from a data packet; a range covering a
    # fraction of the u32 space costs that fraction of every data packet. The
    # two settings cannot be reasoned about apart - see OBFS_H_WIDTH_LO.
    #
    # "on" rather than a drawn value, because parse_bool is all the tools read
    # here. The other state is the empty string and not the word "off": empty is
    # how the caller is told to write no line at all, and a written
    # `RandomTrailers = off` is a line that means nothing on either end. See the
    # note at the top of this file for who this shuts out, and why that set is
    # now a small one.
    if awg_supports random-trailers; then
        RANDOM_TRAILERS="on"
    fi

    # All five, because all five are written below. Asking about two of them
    # and writing five is how a build that has some of the group gets a line it
    # cannot parse, which `awg setconf` answers by refusing the whole file.
    if awg_supports rekey-after-time rekey_timeout reject_after_time \
                    keepalive_timeout max_handshake_attempts; then
        # Ranges rather than numbers - see rand_range. The kernel redraws inside
        # each of these every time it arms the timer, so what a range costs is
        # nothing and what it buys is that the handshake cadence stops being a
        # constant an observer can measure off one flow.
        REKEY_AFTER=$(rand_range "$OBFS_REKEY_AFTER_LO" "$OBFS_REKEY_AFTER_HI")
        REKEY_TIMEOUT=$(rand_range "$OBFS_REKEY_TIMEOUT_LO" "$OBFS_REKEY_TIMEOUT_HI")
        KEEPALIVE_TIMEOUT=$(rand_range "$OBFS_KEEPALIVE_LO" "$OBFS_KEEPALIVE_HI")
        MAX_ATTEMPTS=$(rand_range "$OBFS_ATTEMPTS_LO" "$OBFS_ATTEMPTS_HI")
        # Derived, not drawn. A peer starts a new handshake at RekeyAfterTime and
        # may then wait KeepaliveTimeout + RekeyTimeout before it hears back, so
        # the three add up: a RejectAfterTime above only the largest of them can
        # still expire the key mid-negotiation. It is also the number the far end
        # measures its own key against, and a responder that reaches the
        # threshold first starts handshaking on top of the initiator - which
        # doubles the one event on the wire none of this can disguise.
        #
        # With ranges the sum is taken from the *top* of each of the three and
        # cleared by the *bottom* of the reject range, which is stricter than the
        # kernel is: receive.c subtracts the bottom of keepalive and rekey-timeout
        # rather than the top. Leaning on that would make the unluckiest draw in
        # a few thousand a stalled tunnel on a server nobody is watching, and the
        # room costs nothing.
        #
        # The ceiling is folded into the same expression rather than tested
        # after it. A bare `(( ... )) && var=...` on the last line of a function
        # hands back the status of the test, and this one is false for every
        # draw the bands can produce - so the function returned 1, and the
        # installer runs under `set -e`.
        MARGIN=$(rand_int "$OBFS_MARGIN_LO" "$OBFS_MARGIN_HI")
        REJECT_LO=$(( ${REKEY_AFTER#*-} + ${KEEPALIVE_TIMEOUT#*-} + ${REKEY_TIMEOUT#*-} + MARGIN ))
        REJECT_LO=$(( REJECT_LO > OBFS_REJECT_MAX ? OBFS_REJECT_MAX : REJECT_LO ))
        MARGIN=$(rand_int "$OBFS_MARGIN_LO" "$OBFS_MARGIN_HI")
        REJECT_HI=$(( REJECT_LO + MARGIN ))
        REJECT_HI=$(( REJECT_HI > OBFS_REJECT_MAX ? OBFS_REJECT_MAX : REJECT_HI ))
        # A width of zero is written as the single number it is, which is also
        # what the panel emits and what `awg showconf` prints back.
        #
        # An if/else rather than `(( ... )) && var=...` for the same reason the
        # ceiling above is folded into its own expression: this is the last
        # statement of the function, a bare test-and-assign hands back the status
        # of the test, and the installer runs under `set -e`. The false branch is
        # unreachable with the bands above - OBFS_MARGIN_LO is 60 and REJECT_LO
        # tops out at 382 against a ceiling of 7200 - which is exactly what makes
        # it worth writing so that it stays unreachable if a band moves.
        if (( REJECT_HI > REJECT_LO )); then
            REJECT_AFTER="${REJECT_LO}-${REJECT_HI}"
        else
            REJECT_AFTER="$REJECT_LO"
        fi
    fi
}

gen_obfuscation() {
    gen_junk
    gen_sizes "${1:-$OBFS_DEFAULT_MTU}"
    gen_header_ranges
    gen_imitation
    gen_advanced
}
