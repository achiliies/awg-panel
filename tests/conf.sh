#!/bin/bash
#
# tests/conf.sh - the bash half of the server-config parity check.
#
# lib/conf.sh says at the top that its parsing has to agree with awg/conf.py,
# "a status screen that counts peers differently from the tool that created
# them is a screen nobody can act on". This is what holds it to that: the
# corpus and the expected answers come from tests/conf_check.py, which asks
# conf.py; everything here does is put the same files through peer_list and
# iface_get and hand the answers back.
#
# Nothing here needs root, a tunnel or a panel. The configs are temporary.
#
# Usage: tests/conf.sh          (exit 0 = the two sides agree)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

PY="$REPO/panel/.venv/bin/python"
[[ -x "$PY" ]] || PY=$(command -v python3)
[[ -x "$PY" ]] || { echo "skip: no python3" >&2; exit 0; }
[[ -f "$REPO/panel/awg/conf.py" ]] || { echo "skip: panel/awg/conf.py is not present" >&2; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# shellcheck source=lib/conf.sh
. "$REPO/lib/conf.sh"

echo "== generating the corpus =="
"$PY" "$REPO/tests/conf_check.py" generate "$WORK" || exit 1

mapfile -t NAMES < "$WORK/names.txt"
mapfile -t KEYS  < "$WORK/keys.txt"

echo "== running it through lib/conf.sh =="
mkdir -p "$WORK/got"
for name in "${NAMES[@]}"; do
    # SERVER_CONF is what lib/common.sh sets on a real machine and what both
    # functions read; here it is one case at a time.
    SERVER_CONF="$WORK/cases/${name}.conf"

    # peer_list's own output, checked through split_peer rather than printed
    # straight: an unnamed peer's empty first field is exactly what split_peer
    # exists to keep in place, and a corpus with one in it should say so.
    : > "$WORK/got/${name}.peers"
    while IFS= read -r line; do
        split_peer "$line"
        printf '%s\t%s\t%s\n' "$PL_NAME" "$PL_PUB" "$PL_IPS" >> "$WORK/got/${name}.peers"
    done < <(peer_list)

    : > "$WORK/got/${name}.iface"
    for key in "${KEYS[@]}"; do
        printf '%s\t%s\n' "$key" "$(iface_get "$key")" >> "$WORK/got/${name}.iface"
    done
done

echo "== comparing =="
"$PY" "$REPO/tests/conf_check.py" compare "$WORK"
