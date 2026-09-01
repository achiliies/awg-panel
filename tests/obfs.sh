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

FOOTGUN_RC=0

echo "obfuscation generator: ${ROUNDS} profiles"

# One profile per line as key=value pairs, which is all the checker needs.
for (( round = 0; round < ROUNDS; round++ )); do
    # A different MTU each time, because S4 is drawn against the room left by
    # it. The ceiling is the tightest case and is computed rather than typed:
    # the panel's own MTU bound is OBFS_MTU_BUDGET less the header protection
    # nonce, so a literal here would keep sweeping a value the panel had stopped
    # accepting - and would stop covering the one that replaced it.
    case $(( round % 4 )) in
        0) mtu=$OBFS_DEFAULT_MTU ;;
        1) mtu=1280 ;;
        2) mtu=$(( OBFS_MTU_BUDGET - OBFS_HEADER_NONCE )) ;;
        *) mtu=1360 ;;
    esac
    gen_obfuscation "$mtu"
    # The detector the upgrade path reads an existing config with, against the
    # profile this file just drew. It is asserted here rather than beside
    # install.sh because what it measures is this file's own invariant, and a
    # draw that trips it is a draw that has widened past the ceiling.
    if obfs_trailer_footgun "$RANDOM_TRAILERS" "$H1" "$H2" "$H3"; then
        echo "  FAIL  round ${round}: a freshly drawn profile trips the footgun check" \
             "(${OBFS_TRAILER_WIDE}, ${OBFS_TRAILER_LOSS})"
        FOOTGUN_RC=1
    fi
    {
        printf 'MTU=%s\tJc=%s\tJmin=%s\tJmax=%s\t' "$mtu" "$JC" "$JMIN" "$JMAX"
        printf 'S1=%s\tS2=%s\tS3=%s\tS4=%s\t' "$S1" "$S2" "$S3" "$S4"
        printf 'H1=%s\tH2=%s\tH3=%s\tH4=%s\t' "$H1" "$H2" "$H3" "$H4"
        printf 'I1=%s\tI2=%s\tI3=%s\tI4=%s\tI5=%s\t' "$I1" "$I2" "$I3" "$I4" "$I5"
        printf 'HeaderProtectionKey=%s\tContentPaddingAddition=%s\t' "$HPK" "$CPA"
        printf 'RekeyAfterTime=%s\tRekeyTimeout=%s\tRejectAfterTime=%s\t' \
               "$REKEY_AFTER" "$REKEY_TIMEOUT" "$REJECT_AFTER"
        printf 'KeepaliveTimeout=%s\tMaxHandshakeAttempts=%s\t' \
               "$KEEPALIVE_TIMEOUT" "$MAX_ATTEMPTS"
        printf 'RandomTrailers=%s\n' "$RANDOM_TRAILERS"
    } >> "$WORK/profiles.tsv"
done

# The two constants go with it. They are what the shell drew every profile
# above against, and obfs_check.py is the only place that can hold them up to
# both the packet layout and the panel's own copy of the same numbers.
OBFS_MTU_BUDGET="$OBFS_MTU_BUDGET" OBFS_HEADER_NONCE="$OBFS_HEADER_NONCE" \
    PYTHONPATH="$REPO/panel" "$PY" "$REPO/tests/obfs_check.py" "$WORK/profiles.tsv"
RC=$?

# The one case the validator above cannot be asked about, because it is not a
# config the panel would accept. install.sh now refuses an --mtu past
# OBFS_MTU_BUDGET - OBFS_HEADER_NONCE, so this is no longer something it can
# write - but gen_obfuscation is handed the MTU off a live awg0.conf too, and
# a server installed before that bound still carries whatever it was given.
# Past the bound there is no room left for S4 at all, and the header
# protection key's nonce is read from the first OBFS_HEADER_NONCE bytes of
# that prefix - so a key written over a missing one is `awg setconf`
# returning EINVAL and an interface that never comes up - on a box the operator
# is watching install itself. The key has to be the thing that gives way, and
# the timers and the trailer switch have to survive it: neither is carried in
# the padding and neither has anything to do with it. The trailer is sized by
# the kernel against what the path has already carried, so there is no MTU it
# can be squeezed out of.
echo "  jumbo MTU: the key gives way, the timers and the trailers do not"
for mtu in $(( OBFS_MTU_BUDGET - OBFS_HEADER_NONCE + 1 )) 1500 9000; do
    gen_obfuscation "$mtu"
    if [[ -n "$HPK" ]]; then
        echo "  FAIL  MTU ${mtu} left S4=${S4}, and a header protection key was drawn anyway"
        RC=1
    fi
    if [[ -z "$REKEY_AFTER" || -z "$REJECT_AFTER" ]]; then
        echo "  FAIL  MTU ${mtu} dropped the timers, which the padding does not carry"
        RC=1
    fi
    if [[ "$RANDOM_TRAILERS" != "on" ]]; then
        echo "  FAIL  MTU ${mtu} dropped the trailer switch, which the padding does not carry"
        RC=1
    fi
done
(( RC == 0 )) && echo "  ok    no key drawn above the budget, at 1500 or at 9000;"\
                       "timers and trailers still drawn"

# The other direction, and the one that matters: a check that only ever says no
# is indistinguishable from one that is not wired up. These are the ranges
# v1.1.1 shipped - a whole quarter of the space each, drawn beside a switch that
# went on two commits before they were narrowed - and they are what an upgrade
# has to find on a machine installed from that release. An unattended
# awg-update cannot repair them, because H1-H4 and the switch have to match at
# both ends and redrawing them is a config every client has to import again, so
# finding them and saying so is the whole of what the installer can do.
echo "  the profile v1.1.1 shipped, as an upgrade finds it"
if obfs_trailer_footgun on 812431677-1197514238 1615300653-2108240574 136213748-435787136
then
    if [[ "$OBFS_TRAILER_WIDE" == "H1, H2, H3" && "$OBFS_TRAILER_LOSS" == *"27.4%"* ]]; then
        echo "  ok    named H1, H2 and H3 and put ${OBFS_TRAILER_LOSS} on them"
    else
        echo "  FAIL  fired, but said ${OBFS_TRAILER_WIDE:-nothing} / ${OBFS_TRAILER_LOSS:-nothing}"
        FOOTGUN_RC=1
    fi
else
    echo "  FAIL  quarter-wide ranges under RandomTrailers did not trip the check"
    FOOTGUN_RC=1
fi

# The switch is half of the pairing, so the same ranges with it off are a
# profile nothing should be said about: wide ranges alone cost nothing, and an
# installer that warned about them anyway would be teaching operators to skip
# the sentence that matters.
if obfs_trailer_footgun "" 812431677-1197514238 1615300653-2108240574 136213748-435787136
then
    echo "  FAIL  the same ranges warned with the switch off, where they are safe"
    FOOTGUN_RC=1
else
    echo "  ok    the same ranges say nothing once RandomTrailers is clear"
fi

# One range a hair over the ceiling: the bottom of the band, where a percentage
# would round to nothing and the phrase has to become a count instead.
if obfs_trailer_footgun on "5-$(( OBFS_H_WIDTH_WARN + 5 ))" 5-6 7-8 &&
   [[ "$OBFS_TRAILER_LOSS" == *"1 in "* ]]; then
    echo "  ok    at the ceiling it counts packets rather than reporting 0.0%"
else
    echo "  FAIL  at the ceiling it said: ${OBFS_TRAILER_LOSS:-nothing}"
    FOOTGUN_RC=1
fi

(( FOOTGUN_RC == 0 )) || RC=1

# What the generator returns, which nothing above this can see: this file runs
# without `set -e` so that a failed check can be counted and reported rather
# than ending the run, and install.sh runs with it. A generator whose last
# statement is a test - `(( ceiling )) && var=...`, with a ceiling no draw
# reaches - hands that test's status back as its own, and the installer then
# exits 1 in the middle of the step with nothing printed. So the status is
# asserted here the way install.sh consumes it, in a shell that has -e set.
echo "  exit status under set -e"
if bash -c "set -euo pipefail; . '$REPO/lib/obfs.sh'; gen_obfuscation $OBFS_DEFAULT_MTU" >/dev/null 2>&1; then
    echo "  ok    gen_obfuscation returns 0, so install.sh gets past the step"
else
    echo "  FAIL  gen_obfuscation returned non-zero; install.sh would exit here"
    RC=1
fi

exit "$RC"
