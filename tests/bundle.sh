#!/bin/bash
#
# tests/bundle.sh - the single-file installer must not need the memory of
# what it carries.
#
# The bundle build.sh writes is a shell script with a tar.gz appended to it.
# The obvious way to write one instead - base64 the archive into a shell
# variable - works everywhere it is tested and fails only where it is used:
# bash holds the assignment in its parser, keeps the value, and copies it for
# every expansion, so the release bundle cost around ten times its own size in
# resident memory before install.sh had printed a line. On the 1 GB VPS this
# software runs on, with a panel already running and being upgraded, that is
# the machine. And running out is silent - the OOM killer or systemd-oomd
# takes the login session, so the terminal the install was started in vanishes
# with no error anywhere for the operator to read.
#
# So the property worth defending is not "the bundle unpacks", which any
# implementation manages. It is "the bundle unpacks in memory that has nothing
# to do with how big it is", and the way to check it is to give the extractor
# far less memory than the payload and watch it succeed. A 40 MB payload under
# a 300 MB address-space limit passes streaming and cannot pass a variable.
#
# Usage: tests/bundle.sh      (exit 0 = the bundle streams its payload)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAIL=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=1; }

# A stand-in repository, so this can put a payload of a known size in vendor/
# without touching the checkout it runs from. build.sh resolves everything
# relative to itself, so a directory with these five entries is all it needs.
SRC="$WORK/src"
mkdir -p "$SRC"
cp -a "$REPO/build.sh" "$REPO/install.sh" "$REPO/lib" "$REPO/bin" "$SRC/"

# Incompressible, because what has to be large is the payload rather than the
# directory: 40 MB of zeroes would leave a bundle of a few kilobytes and prove
# nothing. This stands in for the wheels and the vendored sources a release
# bundle carries, which come to about the same.
mkdir -p "$SRC/vendor"
head -c 40000000 /dev/urandom > "$SRC/vendor/blob.bin"

BUNDLE="$WORK/install.sh"
if ! "$SRC/build.sh" -o "$BUNDLE" >/dev/null 2>&1; then
    bad "build.sh could not write a bundle"
    exit 1
fi
SIZE=$(( $(stat -c %s "$BUNDLE") / 1048576 ))
(( SIZE >= 35 )) || bad "the test payload came out at ${SIZE} MB; it is meant to be ~40"

# The part of the file that is shell has to parse as shell. `bash -n` on the
# whole file cannot say that any more - it would try to parse the archive too -
# so the check stops where the archive starts, which is also the line the
# bundle itself finds at run time.
MARKER=$(sed -n 's/^MARKER=//p' "$BUNDLE" | head -1)
if [[ -z "$MARKER" ]]; then
    bad "the bundle declares no payload marker"
else
    if awk -v m="$MARKER" '$0 == m { exit } { print }' "$BUNDLE" | bash -n; then
        ok "the bundle's shell prologue parses"
    else
        bad "the bundle's shell prologue does not parse"
    fi
fi

# The point of the whole exercise. 300 MB of address space is ample for tail,
# tar and gzip, and nowhere near enough to hold a 40 MB payload the way a
# shell variable holds one - that measured 459 MB resident for 46 MB.
OUT="$WORK/out.txt"
if ( ulimit -v 307200; bash "$BUNDLE" --help ) >"$OUT" 2>&1; then
    ok "unpacks and runs under a 300 MB address-space limit"
else
    bad "cannot unpack under a 300 MB address-space limit (payload held in memory?)"
    sed -n '1,5p' "$OUT" | sed 's/^/        /'
fi
grep -q 'install.sh - build and configure' "$OUT" \
    && ok "the unpacked installer is the one that ran" \
    || bad "the unpacked installer did not run"

# Run from something that is not a file, the bundle cannot find its own
# payload. It has to say so rather than unpack an empty directory and start
# installing from it.
if bash < "$BUNDLE" >"$OUT" 2>&1; then
    bad "piped into bash, the bundle claimed to succeed"
else
    grep -q 'has to be' "$OUT" \
        && ok "piped into bash, it says to run the file instead" \
        || bad "piped into bash, it failed without saying why"
    # And names the script that does the piping properly, because "download it
    # first" is not what anyone who just pasted a one-line install wants to
    # hear on its own.
    grep -q 'get.sh' "$OUT" \
        && ok "piped into bash, it points at get.sh" \
        || bad "piped into bash, it never mentions get.sh"
fi

# Truncation is the other way a downloaded bundle arrives wrong, and half an
# archive is a case tar reports in its own words. The bundle has to catch it
# before it hands a half-unpacked directory to install.sh.
head -c $(( $(stat -c %s "$BUNDLE") / 2 )) "$BUNDLE" > "$WORK/half.sh"
if bash "$WORK/half.sh" --help >"$OUT" 2>&1; then
    bad "a truncated bundle installed anyway"
else
    ok "a truncated bundle stops with an error"
fi

(( FAIL == 0 )) && echo "bundle: ok"
exit "$FAIL"
