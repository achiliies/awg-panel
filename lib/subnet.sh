# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/subnet.sh - the tunnel network, in one place.
#
# This used to exist three times: as parse_cidr in awg-client, parse_subnet
# in install.sh and an awk re-implementation in awg-menu, each one duty-bound
# to match the others and awg/subnet.py in the panel. Now the bash side has
# exactly one parser, with the same rules as the Python one: first IPv4 entry
# of an Address list wins, /16../30, no leading zeros in octets, a bare
# three-octet base still means a /24.
#
# parse_cidr sets SUBNET_NET (network address as an integer), SUBNET_PREFIX
# (its length) and SUBNET_SIZE (how many addresses it holds); everything else
# derives from those.

SUBNET_NET=0 SUBNET_PREFIX=0 SUBNET_SIZE=0

int2ip() {
    printf '%d.%d.%d.%d' \
        $(( ($1 >> 24) & 255 )) $(( ($1 >> 16) & 255 )) \
        $(( ($1 >> 8) & 255 ))  $(( $1 & 255 ))
}

# The other direction: one IPv4 address as an integer, or nothing. Same octet
# rule as parse_cidr_one, so an address this accepts is one that could have
# come out of the pool. Used to turn a client's address back into its offset
# within the network, which is the number its IPv6 address is derived from.
ip2int() {
    local oct='(0|[1-9][0-9]{0,2})' a b c d
    [[ "${1:-}" =~ ^$oct\.$oct\.$oct\.$oct$ ]] || return 1
    a=${BASH_REMATCH[1]}; b=${BASH_REMATCH[2]}
    c=${BASH_REMATCH[3]}; d=${BASH_REMATCH[4]}
    (( a < 256 && b < 256 && c < 256 && d < 256 )) || return 1
    printf '%d' $(( (a << 24) | (b << 16) | (c << 8) | d ))
}

# Read one network into the SUBNET_* globals. Accepts an Address with host
# bits set (10.13.13.1/24), a plain network, an Address list of which the
# first IPv4 entry wins - "Address = fd00::1/64, 10.13.13.1/24" is a legal
# line - and the legacy three-octet base.
parse_cidr() {
    local part parts IFS=,
    read -ra parts <<<"${1:-}"
    for part in "${parts[@]:-}"; do
        parse_cidr_one "$part" && return 0
    done
    return 1
}

parse_cidr_one() {
    # An octet is 0, or a digit string with no leading zero: python's ipaddress
    # rejects "010.1.1.1", so accepting it here would let the CLI and the panel
    # allocate out of two different networks from the same config.
    local oct='(0|[1-9][0-9]{0,2})'
    local raw="$1" ip prefix a b c d mask
    raw="${raw//[[:space:]]/}"
    [[ -n "$raw" ]] || return 1
    [[ "$raw" =~ ^$oct\.$oct\.$oct$ ]] && raw="${raw}.0/24"
    if [[ "$raw" == */* ]]; then ip="${raw%%/*}"; prefix="${raw#*/}"
    else                         ip="$raw";       prefix=24
    fi
    [[ "$prefix" =~ ^[0-9]{1,2}$ ]] || return 1
    # Same bounds as awg/subnet.py: a /30 is the smallest network with room for
    # the server and one client, and a /16 is already 65533 of them - wider
    # than that is not a tunnel any more, and it is more clients than the
    # per-client shaping is dimensioned for.
    # 10# forces base ten - bash reads a bare 08 as a bad octal number.
    (( 10#$prefix >= 16 && 10#$prefix <= 30 )) || return 1
    [[ "$ip" =~ ^$oct\.$oct\.$oct\.$oct$ ]] || return 1
    a=${BASH_REMATCH[1]}; b=${BASH_REMATCH[2]}
    c=${BASH_REMATCH[3]}; d=${BASH_REMATCH[4]}
    (( a < 256 && b < 256 && c < 256 && d < 256 )) || return 1
    prefix=$((10#$prefix))
    mask=$(( (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF ))
    SUBNET_NET=$(( ((a << 24) | (b << 16) | (c << 8) | d) & mask ))
    SUBNET_PREFIX=$prefix
    SUBNET_SIZE=$(( 1 << (32 - prefix) ))
    return 0
}

# The network in CIDR form. With an argument it parses that first and fails
# loudly on junk - empty output, never the raw text, because awg-menu's
# self-test feeds this straight to `ip route add`.
subnet_cidr() {
    if [[ $# -gt 0 ]]; then parse_cidr "$1" || return 1; fi
    printf '%s/%s' "$(int2ip "$SUBNET_NET")" "$SUBNET_PREFIX"
}

# How many clients fit: every address minus network, broadcast and the
# server's own. With an argument, "?" for junk - this one only ever lands in
# status text.
subnet_hosts() {
    if [[ $# -gt 0 ]]; then
        parse_cidr "$1" || { printf '?'; return 0; }
    fi
    printf '%s' $(( SUBNET_SIZE - 3 ))
}

# The server's own address (the network's first host) with its prefix.
subnet_server_addr() {
    printf '%s/%s' "$(int2ip $(( SUBNET_NET + 1 )))" "$SUBNET_PREFIX"
}
