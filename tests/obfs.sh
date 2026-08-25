#!/bin/bash
#
# tests/obfs.sh - prove the shell generator and the panel agree.
#
# lib/obfs.sh and panel/awg/validate.py both draw a server's obfuscation
# profile, because install.sh has to work on a box with no panel on it. Two
# implementations of one thing drift, and the way this one would drift is the
# worst kind: silently, into a config the panel then refuses to save, or worse
# into one the kernel accepts and no client can talk to.
#
# They cannot be compared output for output - both draw at random - so this
# checks the thing that actually matters instead. Every profile the shell
# generates is fed to the panel's validator, which owns the rules, and to a
# parser for each protocol the decoys claim to be. A profile that fails either
# is a profile that would have shipped.
#
# Usage: tests/obfs.sh [rounds]      (exit 0 = all checks passed)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ROUNDS=${1:-200}

PY="$REPO/panel/.venv/bin/python"
[[ -x "$PY" ]] || PY=$(command -v python3)
if ! "$PY" -c 'import sys; sys.path.insert(0, "'"$REPO"'/panel"); import awg.validate' 2>/dev/null; then
    echo "skip: panel/awg/validate.py is not importable (make -C panel venv)" >&2
    exit 0
fi

# shellcheck source=lib/obfs.sh
. "$REPO/lib/obfs.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "obfuscation generator: ${ROUNDS} profiles"

# One profile per line as key=value pairs, which is all the checker needs.
for (( round = 0; round < ROUNDS; round++ )); do
    # A different MTU each time, because S4 is drawn against the room left by
    # it. 1420 is the largest the panel accepts and so the tightest case: it
    # leaves 20 bytes, half the width S4 would otherwise be drawn from.
    case $(( round % 4 )) in
        0) mtu=1400 ;;
        1) mtu=1280 ;;
        2) mtu=1420 ;;
        *) mtu=1360 ;;
    esac
    gen_obfuscation "$mtu"
    {
        printf 'MTU=%s\tJc=%s\tJmin=%s\tJmax=%s\t' "$mtu" "$JC" "$JMIN" "$JMAX"
        printf 'S1=%s\tS2=%s\tS3=%s\tS4=%s\t' "$S1" "$S2" "$S3" "$S4"
        printf 'H1=%s\tH2=%s\tH3=%s\tH4=%s\t' "$H1" "$H2" "$H3" "$H4"
        printf 'I1=%s\tI2=%s\tI3=%s\tI4=%s\tI5=%s\n' "$I1" "$I2" "$I3" "$I4" "$I5"
    } >> "$WORK/profiles.tsv"
done

PYTHONPATH="$REPO/panel" "$PY" "$REPO/tests/obfs_check.py" "$WORK/profiles.tsv"
