#!/bin/bash
#
# tests/prompt.sh - prove a keystroke typed before a question is not its answer.
#
# The installer promises that the rest of the run is unattended and then spends
# several minutes building the panel, so a tap on Enter at what looks like a
# stalled build is an ordinary thing for somebody to do. That newline sits in
# the terminal's input queue until the next question reads it, and the two
# questions that come after the build - "set up HTTPS?" from lib/acme.sh, and
# every one of install.sh's own - read it as an answer they were never given.
#
# Both halves of that are tested here. The visible half is the HTTPS question
# appearing twice, once refused for not being y or n. The invisible half is a
# stale "y", which is a perfectly good answer: it was taken as consent to issue
# a certificate, and the operator's real answer went to the shell afterwards.
# For install.sh the stale line reads as an accepted default, or as a password.
#
# A pty is the whole point - the queue being drained only exists on a terminal,
# and bash reads a terminal differently from a pipe - so python drives one and
# this checks the transcript that comes back.
#
# Usage: tests/prompt.sh      (exit 0 = all checks passed)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

PY=$(command -v python3)
[[ -x "$PY" ]] || { echo "skip: no python3" >&2; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAIL=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; printf '        %s\n' "$2"; FAIL=1; }

# The victim scripts. Each waits before asking, which is the build the operator
# gets bored of, and prints what it decided in a form the assertions can grep.

cat >"$WORK/acme-ask" <<EOF
. "$REPO/lib/common.sh"
. "$REPO/lib/acme.sh"
acme_tty_open || exit 9
sleep 0.6
if acme_ask_yn "Set up HTTPS for the panel now?"; then echo "VERDICT=yes"; else echo "VERDICT=no"; fi
EOF

# ask_line and the drain in front of it, lifted out of install.sh rather than
# copied, so this tests what ships. Everything the pair touches that the rest of
# the installer would have set is set here instead.
cat >"$WORK/install-ask" <<EOF
B="" N=""
ASK_FD=1 ASK_TTY=1
exec 3<>/dev/tty || exit 9
eval "\$(awk '/^ask_drain\(\) \{/,/^\}/' "$REPO/install.sh")"
eval "\$(awk '/^ask_line\(\) \{/,/^\}/' "$REPO/install.sh")"
[[ \$(type -t ask_drain) == function && \$(type -t ask_line) == function ]] || exit 8
sleep 0.6
ask_line "panel port [Enter = 8080]:"
echo "VERDICT=[\$REPLY_VAL]"
EOF

# Drive one victim on a pty: stale bytes go in while it is still sleeping, the
# real answer once it has had time to ask. The transcript is everything the
# terminal saw, echo included.
run() {
    "$PY" - "$WORK/$1" "$2" "$3" <<'PY'
import os, pty, select, signal, sys, time

script, stale, answer = sys.argv[1], sys.argv[2], sys.argv[3]
pid, fd = pty.fork()
if pid == 0:
    os.execvp("bash", ["bash", script])

out = b""
def pump(seconds):
    global out
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        out += data
    return True

pump(0.2)
if stale:
    os.write(fd, stale.encode())          # typed at the stalled-looking build
pump(1.0)
os.write(fd, answer.encode())             # typed at the question itself
pump(1.5)
try:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
except OSError:
    pass
sys.stdout.write(out.decode(errors="replace"))
PY
}

# How many times the question was put. Trailing \r comes with the pty.
asked() { grep -c "Set up HTTPS for the panel now" <<<"$1"; }

# ---------------------------------------------------------------- lib/acme.sh

T=$(run acme-ask $'\n' $'n\n')
case "$(asked "$T")" in
    1) ok "a stale newline does not make the HTTPS question ask itself twice" ;;
    *) bad "a stale newline does not make the HTTPS question ask itself twice" \
           "asked $(asked "$T") times" ;;
esac
grep -q 'VERDICT=no' <<<"$T" \
    && ok "and the answer that counts is the one typed at the question" \
    || bad "and the answer that counts is the one typed at the question" \
           "$(grep -a VERDICT <<<"$T" || echo 'no verdict at all')"

T=$(run acme-ask $'y\n' $'n\n')
grep -q 'VERDICT=no' <<<"$T" \
    && ok "a stale y is not consent to issue a certificate" \
    || bad "a stale y is not consent to issue a certificate" \
           "the installer would have gone off to issue one"

# Two taps, which is what somebody does when the first one changes nothing.
# read stops at a newline, so the first drained chunk comes back empty - and a
# drain that stopped there took one keystroke and left the rest of the queue
# sitting in front of the question.
T=$(run acme-ask $'\n\n' $'n\n')
case "$(asked "$T")" in
    1) ok "a second stale newline does not reach the HTTPS question either" ;;
    *) bad "a second stale newline does not reach the HTTPS question either" \
           "asked $(asked "$T") times" ;;
esac

# The same queue with something consequential behind the newline. This is the
# shape that mattered: the drain stopped on the empty chunk, the "y" behind it
# was read as agreement, and the installer went off to issue a certificate.
T=$(run acme-ask $'\ny\n' $'n\n')
grep -q 'VERDICT=no' <<<"$T" \
    && ok "a stale y behind a stale newline is not consent either" \
    || bad "a stale y behind a stale newline is not consent either" \
           "the installer would have gone off to issue one"

T=$(run acme-ask '' $'y\n')
grep -q 'VERDICT=yes' <<<"$T" \
    && ok "an answer typed at the question is still read" \
    || bad "an answer typed at the question is still read" \
           "draining ate the real answer"

# ------------------------------------------------------------------ install.sh

T=$(run install-ask $'\n' $'8443\n')
grep -q 'VERDICT=\[8443\]' <<<"$T" \
    && ok "a stale newline is not install.sh accepting a default" \
    || bad "a stale newline is not install.sh accepting a default" \
           "$(grep -a VERDICT <<<"$T" || echo 'no verdict at all')"

T=$(run install-ask $'\n\n' $'8443\n')
grep -q 'VERDICT=\[8443\]' <<<"$T" \
    && ok "nor is a second one, which is where the first drain stopped" \
    || bad "nor is a second one, which is where the first drain stopped" \
           "$(grep -a VERDICT <<<"$T" || echo 'no verdict at all')"

T=$(run install-ask '' $'8443\n')
grep -q 'VERDICT=\[8443\]' <<<"$T" \
    && ok "install.sh still reads what was typed at the question" \
    || bad "install.sh still reads what was typed at the question" \
           "draining ate the real answer, or the pair above is no longer in install.sh"

exit "$FAIL"
