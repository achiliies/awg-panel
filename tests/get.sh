#!/bin/bash
#
# tests/get.sh - what the one-line installer refuses to run.
#
# get.sh exists to be piped into a root shell off the internet, which makes it
# the least defensible thing in this repository and the one worth checking
# hardest. Nothing here tests that an install works - that needs a VPS, a
# kernel and four minutes. What is checked is every reason it should stop
# before bash is handed a file:
#
#   * a bundle whose sha256 does not match the published SHA256SUMS
#   * a SHA256SUMS that does not mention the bundle at all
#   * a file that is not a shell installer
#   * a bundle that will not unpack
#   * a download that never arrived
#   * a repository or version name that is not one
#
# And the two properties that are the point of the script rather than
# incidental to it: a truncated copy of it does nothing at all, because a
# connection cut mid-pipe is the normal failure of `curl | bash`; and a cached
# bundle is re-verified rather than trusted, because it lives in /var/tmp where
# it can be rewritten between one run and the next.
#
# Driven with no network. AWG_PANEL_RELEASE_URL points at a directory of
# file:// assets - the same seam a server behind a strict egress policy uses to
# install from a mirror, so what is exercised is a supported path rather than a
# hole cut for the tests. The fake bundle answers --help, records the arguments
# it was given and exits; the real one would rebuild the machine running this.
#
# Usage: tests/get.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TOOL="$REPO/get.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"
        [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

# The tool refuses to run as anything but root, and this suite must not be
# root: it drives a script whose last act is to run a downloaded file. The same
# seam bin/awg-update gives tests/update.sh, and it only skips the check - root
# is still what a real install needs.
export AWG_GET_ROOTLESS=1
# Not the machine's /var/tmp. The cache is a directory the tool creates and
# prunes, and pruning the real one would delete a bundle somebody's interrupted
# install is about to resume from.
export AWG_PANEL_CACHE="$ROOT/cache"
mkdir -p "$AWG_PANEL_CACHE"

# ------------------------------------------------------------ the fake release
#
# A mirror laid out the way GitHub serves one, so the same URL shapes the tool
# builds for github.com resolve here: /releases/latest/download/<asset> and
# /releases/download/<tag>/<asset>. file:// cannot redirect, so the version
# never resolves and the tool takes the /latest/download path - which is the
# fallback worth exercising anyway, since it is what an old wget lands on.
RELEASES="$ROOT/releases"
LATEST="$RELEASES/latest/download"
mkdir -p "$LATEST"
export AWG_PANEL_RELEASE_URL="file://$RELEASES"

RAN="$ROOT/ran"

# What a bundle is, as far as this script can tell from outside: a file
# beginning with a bash shebang that answers --help. Everything past that is
# install.sh's business.
write_bundle() {  # [--broken-help]   (LATEST=<dir> to write elsewhere)
    cat > "$LATEST/awg-panel.sh" <<BUNDLE
#!/bin/bash
if [[ " \$* " == *" --help "* ]]; then
    $( [[ "${1:-}" == --broken-help ]] && printf 'exit 1' || printf 'echo usage; exit 0' )
fi
printf '%s\n' "\$*" > "$RAN"
BUNDLE
}

# The checksums published beside it, which is the only statement anybody makes
# about what the bundle should contain.
write_sums() {
    ( cd "$LATEST" && sha256sum awg-panel.sh > SHA256SUMS )
}

# One run of the tool against that release. Never inherits a terminal: the tool
# hands the installer /dev/tty when there is one, and a suite that let it do
# that would be a suite whose fake bundle could read the tester's keyboard.
run() {
    rm -f "$RAN"
    "$TOOL" "$@" </dev/null >"$ROOT/out" 2>&1
}

ran_with() { [[ -f "$RAN" ]] && cat "$RAN"; }
said() { grep -qi -- "$1" "$ROOT/out"; }

echo "==> a release that is what it claims to be"

write_bundle
write_sums

if run --no-ask --lang en; then
    ok "a matching bundle is downloaded and run"
else
    bad "a good release did not install" "$(tail -3 "$ROOT/out")"
fi

# Verbatim, and nothing added: a real bundle prepends --panel to its own
# arguments, and that is the bundle's decision to make rather than this
# script's. Everything after `bash -s --` has to arrive as it was typed.
if [[ "$(ran_with)" == "--no-ask --lang en" ]]; then
    ok "the arguments reach the installer untouched"
else
    bad "the installer was run with '$(ran_with)'"
fi

if said "sha256" && said "matches"; then
    ok "it says which checksum it checked"
else
    bad "the run never mentioned the checksum it verified"
fi

echo "==> a release that is not"

# The bundle is replaced without republishing SHA256SUMS: a mirror serving a
# stale file, a truncated transfer, or somebody who would like this machine to
# run their script as root. The three are indistinguishable from here, and all
# three end the same way.
write_bundle
write_sums
printf '#!/bin/bash\necho pwned\n' > "$LATEST/awg-panel.sh"

if run; then
    bad "a bundle that does not match its checksum was run anyway"
else
    if [[ -z "$(ran_with)" ]] && said "does not match"; then
        ok "a bundle that does not match SHA256SUMS is discarded"
    else
        bad "the mismatch was refused for the wrong reason" "$(tail -3 "$ROOT/out")"
    fi
fi

if [[ ! -f "$AWG_PANEL_CACHE/awg-panel.sh" ]]; then
    ok "the rejected file is not left behind to be found by the next run"
else
    bad "a rejected bundle stayed in the cache"
fi

write_bundle
printf 'd41d8cd98f00b204e9800998ecf8427e  some-other-file\n' > "$LATEST/SHA256SUMS"
if run; then
    bad "a SHA256SUMS that never mentions the bundle was accepted"
else
    if [[ -z "$(ran_with)" ]] && said "checksum"; then
        ok "a SHA256SUMS that does not name the bundle is refused"
    else
        bad "the wrong refusal for an unmentioned bundle" "$(tail -3 "$ROOT/out")"
    fi
fi

# A checksum that matches a file that is not an installer. Both halves of a
# release can be honest about each other and still be the wrong thing - an
# error page saved by a proxy, an HTML login wall from a captive network.
printf '<html>not a shell script</html>\n' > "$LATEST/awg-panel.sh"
write_sums
if run; then
    bad "a file that is not a shell installer was handed to bash"
else
    if [[ -z "$(ran_with)" ]] && said "not a shell installer"; then
        ok "a file that is not an installer is refused before bash sees it"
    else
        bad "the wrong refusal for a non-installer" "$(tail -3 "$ROOT/out")"
    fi
fi

# The shebang is right and the checksum is right, and it still cannot unpack
# the payload it is supposed to carry - the shape a bundle takes when the
# release was cut wrong.
write_bundle --broken-help
write_sums
if run; then
    bad "a bundle that will not unpack was run"
else
    if [[ -z "$(ran_with)" ]] && said "damaged"; then
        ok "a bundle that will not unpack is refused"
    else
        bad "the wrong refusal for an unusable bundle" "$(tail -3 "$ROOT/out")"
    fi
fi

echo "==> a release that is not there"

write_bundle
write_sums
rm -f "$LATEST/SHA256SUMS"
if run; then
    bad "a missing SHA256SUMS did not stop the install"
else
    said "could not download" && ok "a SHA256SUMS that cannot be fetched stops it" \
        || bad "the wrong error for an unreachable SHA256SUMS" "$(tail -3 "$ROOT/out")"
fi

write_bundle
write_sums
rm -f "$LATEST/awg-panel.sh"
if run; then
    bad "a missing bundle did not stop the install"
else
    [[ -z "$(ran_with)" ]] && said "could not download" \
        && ok "a bundle that cannot be fetched stops it" \
        || bad "the wrong error for an unreachable bundle" "$(tail -3 "$ROOT/out")"
fi

echo "==> names that arrived from the environment"

write_bundle
write_sums

if AWG_PANEL_UPDATE_REPO="not a repo" run; then
    bad "a nonsense repository name was accepted"
else
    said "owner/repo" && ok "a repository that is not owner/repo is refused" \
        || bad "the wrong error for a bad repository name"
fi

if AWG_PANEL_VERSION='../../etc' run; then
    bad "a version that is a path was accepted"
else
    said "release tag" && ok "a version that is not a tag is refused" \
        || bad "the wrong error for a bad version"
fi

echo "==> a pinned version, and the cache that goes with it"

# The other URL shape the tool builds - /releases/download/<tag>/<asset> - and
# the only one that caches, because "latest" names a different release from one
# week to the next and a directory named after it would be a lie.
TAGGED="$RELEASES/download/v9.9.9"
mkdir -p "$TAGGED"
LATEST=$TAGGED write_bundle
LATEST=$TAGGED write_sums

pinned() { AWG_PANEL_VERSION=v9.9.9 run; }

if pinned; then
    ok "a pinned version installs from that tag's own URLs"
else
    bad "a pinned version did not install" "$(tail -3 "$ROOT/out")"
fi

CACHED="$AWG_PANEL_CACHE/awg-panel-v9.9.9/awg-panel.sh"
if [[ -f "$CACHED" ]]; then
    ok "the download is kept where a second attempt can find it"
else
    bad "nothing was cached under $AWG_PANEL_CACHE"
fi

# The whole risk of caching in a world-readable directory, and the reason the
# checksum is re-read on every run rather than remembered: this is what someone
# who can write to that directory between two runs would leave in it.
printf '#!/bin/bash\nprintf pwned > %s\n' "$RAN" > "$CACHED"
if pinned; then
    [[ "$(ran_with)" != "pwned" ]] \
        && ok "a cached bundle that no longer matches is downloaded again" \
        || bad "the tampered cache was run" "a cached file is trusted without checking"
else
    bad "a tampered cache was not recovered from" "$(tail -3 "$ROOT/out")"
fi

# And the cache is worth having: with the bundle no longer served, the verified
# copy already on disk still installs. That is the interrupted build being
# retried, which is the case it exists for.
pinned >/dev/null 2>&1
rm -f "$TAGGED/awg-panel.sh"
if pinned && said "Already downloaded"; then
    ok "a verified copy is reused instead of downloaded twice"
else
    bad "the cached bundle was not reused" "$(tail -3 "$ROOT/out")"
fi

# One cache directory, not one per release this machine has ever seen.
mkdir -p "$AWG_PANEL_CACHE/awg-panel-v1.0.0"
LATEST=$TAGGED write_bundle
LATEST=$TAGGED write_sums
pinned >/dev/null 2>&1
if [[ ! -d "$AWG_PANEL_CACHE/awg-panel-v1.0.0" ]]; then
    ok "the previous release's download is pruned"
else
    bad "an old cache directory was left behind"
fi

# The cache directory itself, which is the other half of caching in /var/tmp.
# Its name is the release tag and /var/tmp is 1777, so it is a directory any
# local user can create before root gets there - and `install -d -m 700`, which
# used to make it, accepted one: install sets the mode on a directory that
# already exists and leaves the ownership alone. What that bought whoever made
# it was a symlink at either filename for root to write through, and a bundle
# they could swap in the moment between sha256sum reading it and bash running
# it.
#
# Ownership cannot be faked here without a second uid, so what is checked is the
# mode that goes with it: a directory this script could not have made is one it
# has to remake.
rm -rf "$AWG_PANEL_CACHE/awg-panel-v9.9.9"
mkdir -m 777 "$AWG_PANEL_CACHE/awg-panel-v9.9.9"
# Something only the planter could have put there, which is what the check
# below asks after. Not the directory's inode: `rm -rf` frees it and the
# `mkdir` a moment later is handed the same number back most of the time - on
# an idle filesystem every time - so a test that compared inodes was reading
# the allocator's mood rather than the tool's behaviour. It failed about one
# run in four, and it failed open too: a regression to `install -d -m 700`
# would still have shown a changed inode whenever the number happened to move.
touch "$AWG_PANEL_CACHE/awg-panel-v9.9.9/.planted"
VICTIM="$ROOT/victim"
echo untouched > "$VICTIM"
ln -s "$VICTIM" "$AWG_PANEL_CACHE/awg-panel-v9.9.9/SHA256SUMS"
if pinned; then
    if [[ "$(cat "$VICTIM")" == untouched ]]; then
        ok "a symlink left in the cache directory is not written through"
    else
        bad "the download followed a planted symlink" "$(cat "$VICTIM")"
    fi
    # What was planted, not the mode. `install -d -m 700` on a directory
    # somebody else made comes back with the mode asked for and the ownership
    # untouched, so a test that read the mode would have passed against the code
    # this is here to catch. Contents are the whole claim: the directory root
    # wrote into cannot be the one that was waiting for it if what was left in
    # it is gone.
    if [[ ! -e "$AWG_PANEL_CACHE/awg-panel-v9.9.9/.planted" ]]; then
        ok "a cache directory somebody else made is remade, not reused"
    else
        bad "the pre-made cache directory was written into as it stood"
    fi
else
    bad "a pre-made cache directory stopped the install" "$(tail -3 "$ROOT/out")"
fi

# And the same name as a link to a directory, which is the shape that gets past
# a test for "is it a directory": every check follows it, and root then installs
# somewhere nobody chose.
rm -rf "$AWG_PANEL_CACHE/awg-panel-v9.9.9" "$ROOT/elsewhere"
mkdir -m 700 "$ROOT/elsewhere"
ln -s "$ROOT/elsewhere" "$AWG_PANEL_CACHE/awg-panel-v9.9.9"
if pinned; then
    if [[ ! -L "$AWG_PANEL_CACHE/awg-panel-v9.9.9" && -z "$(ls -A "$ROOT/elsewhere")" ]]; then
        ok "a cache directory that is a symlink is replaced rather than followed"
    else
        bad "the download went through a symlinked cache directory"
    fi
else
    bad "a symlinked cache directory stopped the install" "$(tail -3 "$ROOT/out")"
fi

echo "==> half a script"

# What `curl | bash` does when the connection drops: bash executes the prefix
# it received. Every prefix of this file has to be inert, which is what the
# main() wrapper buys and the only thing that keeps a cut download from doing
# half an install as root.
LINES=$(wc -l < "$TOOL")
TRUNCATED_OK=1
for n in 10 40 80 120 160 200 $(( LINES - 1 )); do
    (( n > 0 && n < LINES )) || continue
    rm -f "$RAN"
    if head -n "$n" "$TOOL" | bash >"$ROOT/trunc" 2>&1; then
        if [[ -s "$ROOT/trunc" || -f "$RAN" ]]; then
            TRUNCATED_OK=0
            bad "the first $n lines of get.sh did something" "$(head -2 "$ROOT/trunc")"
        fi
    else
        # A prefix ending mid-construct is a parse error, which is bash
        # refusing to run it - the outcome this is asking for.
        if [[ -f "$RAN" ]]; then
            TRUNCATED_OK=0
            bad "the first $n lines of get.sh ran the installer"
        fi
    fi
done
(( TRUNCATED_OK )) && ok "every prefix of get.sh does nothing"

# ------------------------------------------------------------------- the tally
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
