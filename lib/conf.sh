# shellcheck shell=bash
# shellcheck disable=SC2034,SC2154
#
# lib/conf.sh - read the AmneziaWG server config.
#
# Requires lib/common.sh first (SERVER_CONF). The parsing here has to agree
# with awg/conf.py in the panel, which writes the file this reads: a status
# screen that counts peers differently from the tool that created them is a
# screen nobody can act on.
#
# Both of them have to agree with `awg` and `awg-quick`, which are what the
# file is actually for, and this is where the four rules below come from:
#
#   A comment runs to the end of the line, wherever it starts. awg-quick cuts
#   every line at the first "#" before it looks at anything, so
#   "Address = 10.9.0.1/24 # main" is an address with nothing after it. This
#   read the comment as part of the value and handed back a CIDR that no
#   parse_cidr would take.
#
#   A section header may be indented and may carry a comment. awg-quick trims
#   the line before comparing it, and conf.py has matched "[Peer] # phone" and
#   "  [Peer]" since it was written. Anchored at column 0, an indented [Peer]
#   was an ordinary line here, and the peer it opened was folded into the one
#   above it - so the name printed on the status screen belonged to a different
#   client than the key beside it.
#
#   Keys are matched without regard to case, because the parser in `awg`
#   compares them that way and conf.py folds them. "publickey =" is a peer that
#   works, that the panel lists, and that this dropped from the count entirely.
#
#   AllowedIPs accumulates. The kernel takes the union of every AllowedIPs line
#   in a peer; keeping only the last one understated what a client may reach,
#   which for the panel is an address it would go on to hand to somebody else.
#
# None of it matters for a file the panel wrote, which is every file on a
# machine nobody has edited by hand. The point is the machines where somebody
# has: install.sh says the same thing where it edits the [Interface] address,
# and refuses to assume the file it is about to rewrite is one of its own.

# Read a key from the [Interface] section of the server config.
iface_get() {
    awk -v key="$1" '
        # What awg-quick reads, rather than what is written: the comment is
        # cut off first, and the header comparison happens on what is left.
        { line = $0; sub(/#.*/, "", line)
          head = tolower(line); sub(/^[[:space:]]+/, "", head) }
        head ~ /^\[interface\][[:space:]]*$/ { i=1; next }
        head ~ /^\[/                         { i=0 }
        i && tolower(line) ~ "^[[:space:]]*" tolower(key) "[[:space:]]*=" {
            sub(/^[^=]*=[[:space:]]*/, "", line); sub(/[[:space:]]+$/, "", line)
            print line; exit
        }
    ' "$SERVER_CONF"
}

# There is no iface_set here, and that absence is the point. Editing the server
# config from bash is what the panel replaced: it is the only writer of these
# files now, so a second implementation of "change a key in [Interface]" would
# be a way for the two to disagree and nothing else. What is left in this file
# reads.
#
# Emit "name<TAB>pubkey<TAB>allowedips" per peer.
peer_list() {
    awk '
        function flush() { if (inp && pub != "") print name "\t" pub "\t" ips }
        function value(s) {
            sub(/^[^=]*=[[:space:]]*/, "", s); sub(/[[:space:]]+$/, "", s); return s
        }
        { line = $0; sub(/#.*/, "", line)
          head = tolower(line); sub(/^[[:space:]]+/, "", head) }
        head ~ /^\[peer\][[:space:]]*$/      { flush(); inp=1; name=""; pub=""; ips=""; next }
        head ~ /^\[interface\][[:space:]]*$/ { flush(); inp=0; next }
        # Any other bracket closes the peer above it, the way conf.py does -
        # "[Bogus]" and a mangled "[Peer #2]" included. Reading one as an
        # ordinary line instead merges the block that follows into the peer
        # before it, which is the same mistake as the indented header.
        head ~ /^\[/                         { flush(); inp=0; next }
        inp {
            # The metadata markers are read off the line as written, not off
            # the comment-stripped copy, because they are comments themselves.
            # Case-sensitive, as conf.py says: the two carry these in the same
            # spelling and a differently-cased one is not one of ours.
            if ($0 ~ /^[[:space:]]*#[[:space:]]*Client[[:space:]]*=/) { name = value($0); next }
            key = tolower(line); sub(/^[[:space:]]+/, "", key)
            if (key ~ /^publickey[[:space:]]*=/)      pub = value(line)
            else if (key ~ /^allowedips[[:space:]]*=/) {
                v = value(line)
                if (v != "") ips = (ips == "" ? v : ips ", " v)
            }
        }
        END { flush() }
    ' "$SERVER_CONF"
}

# Split one peer_list line into PL_NAME / PL_PUB / PL_IPS.
#
# `IFS=$'\t' read -r n p i` cannot be used: a tab counts as IFS whitespace, so
# an unnamed peer's empty first field is swallowed and every value shifts left
# - the public key lands in the name and the address in the key. Explicit
# expansion keeps empty fields where they belong.
split_peer() {
    local rest
    PL_NAME=${1%%$'\t'*}; rest=${1#*$'\t'}
    PL_PUB=${rest%%$'\t'*}; PL_IPS=${rest#*$'\t'}
}

# No lookup-by-name helpers here either. name_exists, pub_of and ip_of each
# answered a question only a tool that creates or edits a client ever asks, and
# the last caller of all three went with bin/awg-client.
