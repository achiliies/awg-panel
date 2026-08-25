# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/common.sh - shared ground for every AmneziaWG management script.
#
# Sourced, never executed. The paths, the colour palette and the reporting
# helpers: the things install.sh, awg-menu and awg-uninstall would otherwise
# each carry a private copy of. The variables are the module's API, hence the
# SC2034 waiver.
#
# Everything here must behave identically under `set -e` and without it.
#
# The paths honour AWG_IFACE and AWG_CONF_DIR, so the test harnesses (and a
# second tunnel on the same box) can repoint a tool without editing it.

#
# Only the three these tools still open. clients/, traffic.db and .lock were
# named here too, back when bash wrote all three; the panel owns them now, and
# a path constant nothing opens is just a second place for one to be wrong.
IFACE="${AWG_IFACE:-awg0}"
CONF_DIR="${AWG_CONF_DIR:-/etc/amnezia/amneziawg}"
SERVER_CONF="$CONF_DIR/${IFACE}.conf"
ENV_FILE="$CONF_DIR/clients.env"

# The second language. Unconditional, and not behind a test that the file is
# there: lib/ is installed and copied as one directory, so an i18n.sh that is
# missing is the same broken checkout as a common.sh that is missing, and a
# quiet fallback to English would hide it rather than report it.
# shellcheck source=lib/i18n.sh
. "$(dirname "${BASH_SOURCE[0]}")/i18n.sh"

# Colours only on a terminal, so piped and logged output stays clean.
if [[ -t 1 ]]; then
    B=$'\e[1m'; DIM=$'\e[2m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
else
    B='' DIM='' R='' G='' Y='' N=''
fi

die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
info() { printf '%s==>%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!! %s%s\n' "$Y" "$*" "$N"; }
step() { printf '\n%s==> %s%s\n' "$G" "$*" "$N"; }

# Which amneziawg module is which. These are two different questions and the
# answers come apart exactly when it matters: modinfo describes the .ko file on
# disk, sysfs describes what the kernel actually loaded from it. An upgrade that
# could not unload the module in use installs the new one and says so, leaving
# the file new and the kernel old until a reboot - and every behaviour anyone is
# looking at is the old one's.
kmod_running()   { cat /sys/module/amneziawg/version 2>/dev/null; }
kmod_installed() { modinfo amneziawg 2>/dev/null | awk '/^version/{print $2}'; }

# "3.0.20260805", or "3.0.20260731-04 (3.0.20260805 installed, needs reboot)"
# when the two disagree. Empty when neither can be read.
kmod_version_line() {
    local running installed
    running=$(kmod_running); installed=$(kmod_installed)
    if [[ -n "$running" && -n "$installed" && "$running" != "$installed" ]]; then
        printf '%s (%s %s)\n' "$running" "$installed" \
            "$(t "installed, needs reboot" "установлена, нужна перезагрузка")"
    else
        printf '%s\n' "${running:-$installed}"
    fi
}

# Is anything on this machine already bound to a port? "$1" is tcp or udp.
#
# Both of the ports this project asks an admin to choose are ports something
# else may already hold, and the failure when one does is silent in both
# directions: gunicorn exits with "address already in use" into a journal, and
# awg-quick brings an interface up that never answers a packet. Asking ss
# first costs nothing and turns either of those into a sentence at the moment
# the number is typed.
#
# What this can and cannot see is worth being plain about, because the answer
# is used to warn rather than to refuse. It sees sockets bound on this host,
# now. It does not see a service that is installed but stopped and will want
# the port back on its next start, and it cannot see the far side of a NAT.
# UDP has no listening state at all - -l on a UDP socket means unconnected,
# which is what a resolver and a WireGuard interface both look like - so a
# busy answer for udp is reliable and a free one is merely likely.
port_busy() {
    local flags=-Hltn
    [[ "$1" == udp ]] && flags=-Hlun
    [[ -n "$(ss "$flags" "sport = :${2}" 2>/dev/null)" ]]
}

# What is holding it, for the message that says so. Empty when ss will not say,
# which is what a non-root caller gets: the port is still busy, the warning
# still stands, and only the name of the culprit is missing.
#
# Never fails, and that is not tidiness. Every caller writes `holder=$(...)`
# under `set -e`, so a non-zero return here would end the run - and the way to
# get one is a host with no iproute2, where the answer is "cannot tell" and the
# install has no business stopping.
port_holder() {
    local flags=-Hltnp out
    [[ "$1" == udp ]] && flags=-Hlunp
    out=$(ss "$flags" "sport = :${2}" 2>/dev/null) || return 0
    sed -n 's/.*users:(("\([^"]*\)".*/\1/p' <<<"$out" | head -1
}

# The address this machine would send from to reach the public internet, which
# is the closest thing to "my IP" a box can answer without asking anybody.
# Empty when there is no route out, or no iproute2 to ask.
#
# The field is found by name rather than by counting, and that is the whole
# point of the function. `ip route get` prints a `via <gw>` pair only when the
# next hop is a gateway; on a point-to-point or directly-attached route there
# is no gateway to name, every later field shifts two to the left, and the
# seventh word - which is the source address on a normal server - becomes the
# number after `uid`. Both callers used to read $7, so on a PPPoE or /32-routed
# VPS the panel offered "1000" as the address to paste into a browser and as
# the default to put on a certificate. Searching for `src` cannot shift.
#
# Never fails, for the reason port_holder does not: the callers all have a
# further fallback to try and none of them wants `set -e` to end the run
# because this box has no default route yet.
route_src_addr() {
    ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }'
    return 0
}

# No lock helper any more. It existed so that awg-client and awg-menu could not
# interleave writes to the server config with each other or with the panel, and
# nothing in bash writes that config now - the panel is the only writer, and it
# takes the same flock on the same file for its own concurrency.
#
# It is worth saying what went with it. The budget was `flock -w 10`, and that
# number was the panel's constraint too: any panel operation holding the lock
# for longer than ten seconds turned into a CLI command that failed. Re-issuing
# every client config on a large server takes longer than that, so the ceiling
# was real. It is gone with the second writer that set it.
