#!/bin/bash
#
# tests/update.sh - what bin/awg-update refuses, and why it refuses it.
#
# This is the one path in the project that reaches out to the internet and then
# runs what comes back as root, on a machine nobody is watching, at the request
# of a button in a browser. So what is checked here is not that an update works
# - that needs a VPS, a kernel and four minutes - but that every reason not to
# start one is honoured before a single byte is executed:
#
#   * a release tagged as a pre-release is never offered
#   * a version that is not newer is never installed without --force
#   * a bundle whose sha256 does not match the published SHA256SUMS is discarded
#   * a SHA256SUMS that does not mention the bundle at all is refused
#   * a file that is not a bundle is refused before bash is handed it
#   * a backup that cannot be taken stops the update
#
# Everything after the checksum passes is left alone: the next thing that
# happens on a real server is `bash <the bundle>`, and a test that let it get
# that far would be a test that reinstalls the machine running it. The fake
# bundle here answers --help and does nothing else, and the run stops at the
# backup, which is the last gate before the installer.
#
# Driven with no network at all. AWG_UPDATE_FEED points the release lookup at a
# JSON file on disk and the asset URLs in it are file:// ones - the same seam a
# server with no route to github.com would use to mirror a release, so what is
# exercised is a supported path rather than a hole cut for the tests.
#
# Usage: tests/update.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TOOL="$REPO/bin/awg-update"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"
        [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

# The tool re-execs itself under sudo when it is not root, and this suite must
# not be root: every path it drives is pointed at $ROOT through the environment,
# and a run with real privileges would be one typo away from the machine's own
# /var/lib and /opt. AWG_UPDATE_ROOTLESS turns that re-exec off. Nothing here
# needs privileges - the fake bundle is a two-line shell script - and the tool
# is honest about what the variable is: root is still what a real update needs,
# and skipping the re-exec only means the failure comes from the kernel instead
# of from a helpful message.
export AWG_UPDATE_ROOTLESS=1

# ------------------------------------------------------------ the fake server
#
# A panel prefix with the two files awg-update reads off one: the version this
# installation is at, and the release helper it runs. The helper is the real
# one, copied rather than stubbed - the rules about what counts as newer are
# the thing under test.
PREFIX="$ROOT/opt/awg-panel"
mkdir -p "$PREFIX/awg" "$PREFIX/.venv/bin"
cp "$REPO/panel/awg/release.py" "$PREFIX/awg/release.py"
printf '1.0.0\n' > "$PREFIX/VERSION"

# What the updater calls to take the pre-update backup. Replaced per test: the
# default writes a plausible archive, and one case below makes it fail.
backup_ok() {
    cat > "$PREFIX/.venv/bin/python" <<'STUB'
#!/bin/sh
# manage.py backup --output <path>
for arg in "$@"; do
    case "$prev" in --output) printf 'fake archive\n' > "$arg"; echo "$arg"; exit 0 ;; esac
    prev="$arg"
done
exit 0
STUB
    chmod 755 "$PREFIX/.venv/bin/python"
}
backup_fails() {
    printf '#!/bin/sh\necho "the database is locked" >&2\nexit 1\n' > "$PREFIX/.venv/bin/python"
    chmod 755 "$PREFIX/.venv/bin/python"
}
backup_ok

DATA="$ROOT/var/lib/awg-panel"
mkdir -p "$DATA"

ASSETS="$ROOT/assets"
mkdir -p "$ASSETS"

# A bundle, as far as anything before `bash <it>` can tell: a bash script whose
# --help succeeds. The real one unpacks a tar.gz first; what awg-update checks
# before running it is exactly these two properties.
printf '#!/bin/bash\ncase "$1" in --help) echo usage; exit 0 ;; esac\nexit 0\n' \
    > "$ASSETS/awg-panel.sh"

sums() { (cd "$ASSETS" && sha256sum awg-panel.sh > SHA256SUMS); }
sums

# One release description in GitHub's own shape, so the helper parses what it
# would parse in production. Written per test, because the tag is what most of
# these turn on.
feed() {  # tag [extra-json]
    cat > "$ROOT/feed.json" <<EOF
{
  "tag_name": "$1",
  "html_url": "https://example.invalid/releases/tag/$1",
  "body": "notes for $1",
  "published_at": "2026-08-19T00:00:00Z",
  "draft": false,
  "prerelease": ${2:-false},
  "assets": [
    {"name": "awg-panel.sh",
     "browser_download_url": "file://$ASSETS/awg-panel.sh"},
    {"name": "SHA256SUMS",
     "browser_download_url": "file://$ASSETS/SHA256SUMS"}
  ]
}
EOF
}

# awg-update, pointed entirely at the tree above. AWG_UPDATE_DIR keeps the state
# out of the data directory so each case starts from nothing.
run() {
    rm -rf "$ROOT/state"
    env AWG_PANEL_PREFIX="$PREFIX" \
        AWG_PANEL_ENV="$ROOT/nonexistent.env" \
        AWG_PANEL_DATA="$DATA" \
        AWG_UPDATE_DIR="$ROOT/state" \
        AWG_UPDATE_FEED="$ROOT/feed.json" \
        bash "$TOOL" "$@" 2>&1
}

# `apply` hands the work to systemd and then tails the log, which needs a
# systemd this test cannot assume. `run` is the worker itself, in the
# foreground, which is the half with every check in it.
worker() {
    rm -rf "$ROOT/state"
    env AWG_PANEL_PREFIX="$PREFIX" \
        AWG_PANEL_ENV="$ROOT/nonexistent.env" \
        AWG_PANEL_DATA="$DATA" \
        AWG_UPDATE_DIR="$ROOT/state" \
        AWG_UPDATE_FEED="$ROOT/feed.json" \
        bash "$TOOL" run "$@" >/dev/null 2>&1
    printf '%s\n' "$?"
}

log_text() { cat "$ROOT/state/log" 2>/dev/null; }
state_of() {
    python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    print("")' "$ROOT/state/state.json" "$1"
}

# ---------------------------------------------------------------- the checks
printf '\n== version comparison ==\n'

# The rule the old implementation got wrong, and the reason this file exists at
# all: it pulled every run of digits out of a tag, so 1.0.0-rc1 read as
# (1,0,0,1) and every server on 1.0.0 would have been offered the candidate it
# was cut from as an upgrade.
newer() {  # latest current -> prints 1 or 0
    python3 - "$PREFIX" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from awg.release import is_newer
print("1" if is_newer(sys.argv[2], sys.argv[3]) else "0")
PY
}

while read -r latest current want why; do
    [[ -z "$latest" ]] && continue
    got=$(newer "$latest" "$current")
    if [[ "$got" == "$want" ]]; then ok "$why"
    else bad "$why" "is_newer($latest, $current) = $got, wanted $want"; fi
done <<'CASES'
v1.1.0    1.0.0      1  a newer release is newer
1.0.1     1.0.0      1  a patch release is newer
1.0.0     1.0.0      0  the same version is not newer
1.0.0     1.1.0      0  an older release is not newer
1.0.0-rc1 1.0.0      0  a candidate for the installed release is not newer
1.0.0     1.0.0-rc1  1  the release a candidate led to is newer
1.1.0-rc1 1.0.0      1  a candidate for a later release still orders above it
1.0.0     1.0        0  1.0 and 1.0.0 are the same version
2.0.0     1.99.99    1  the major number is compared as a number
nightly   1.0.0      0  a tag that is not a version is never newer
1.0.1     garbage    0  an unreadable installed version offers nothing
CASES

printf '\n== what is offered ==\n'

feed v1.0.0
out=$(run check)
if grep -q 'up to date' <<<"$out"; then ok "the installed version reports up to date"
else bad "the installed version reports up to date" "$out"; fi

feed v1.1.0
out=$(run check)
if grep -q 'An update is available: 1.0.0 -> v1.1.0' <<<"$out"; then
    ok "a newer release is offered by name"
else bad "a newer release is offered by name" "$out"; fi
if grep -q 'notes for v1.1.0' <<<"$out"; then ok "the release notes are shown with it"
else bad "the release notes are shown with it" "$out"; fi

# The whole of "stable" as the user asked for it. Said twice on purpose: once
# by the feed's own flag, once by the shape of the tag, because a mirrored feed
# is written by hand and a hand-written flag is a flag that can be wrong.
feed v2.0.0 true
out=$(run check)
if grep -q 'pre-release' <<<"$out"; then ok "a release flagged as a pre-release is refused"
else bad "a release flagged as a pre-release is refused" "$out"; fi

feed v2.0.0-rc1
out=$(run check)
if grep -q 'pre-release' <<<"$out"; then ok "a tag ending -rc1 is refused whatever the flag says"
else bad "a tag ending -rc1 is refused whatever the flag says" "$out"; fi

rm -f "$ROOT/feed.json"
out=$(run check)
if grep -q 'could not check' <<<"$out"; then ok "an unreachable feed says so instead of failing"
else bad "an unreachable feed says so instead of failing" "$out"; fi

printf '\n== what is installed ==\n'

feed v1.0.0
rc=$(worker)
if [[ "$rc" == "0" && "$(state_of status)" == "succeeded" ]]; then
    ok "nothing to do on the newest version, and it is not an error"
else bad "nothing to do on the newest version" "exit $rc, status $(state_of status)"; fi
if [[ -z "$(state_of backup)" ]]; then ok "and nothing was backed up for it"
else bad "and nothing was backed up for it" "$(state_of backup)"; fi

feed v1.1.0
sums
worker >/dev/null
# The fake bundle answers anything with 0, so this run goes all the way through
# to the installer. What is asserted is the order of the gates before it - the
# checksum, then the backup - and then that the update is *not* called a success
# just because the installer exited 0: there is no panel and no tunnel on this
# machine for it to have brought back, and a server in that state after a real
# update is a server whose operator has to be told.
if grep -q 'sha256 .* matches' <<<"$(log_text)"; then ok "the download was checked against SHA256SUMS"
else bad "the download was checked against SHA256SUMS" "$(log_text | tail -5)"; fi
if [[ -s "$(state_of backup)" ]]; then ok "a backup was taken before anything was replaced"
else bad "a backup was taken before anything was replaced" "$(state_of backup)"; fi
if grep -q 'the installer finished' <<<"$(log_text)"; then ok "and then the installer was run"
else bad "and then the installer was run" "$(log_text | tail -5)"; fi
if [[ "$(state_of status)" == "failed" ]] && grep -q 'FAILED to come back up' <<<"$(log_text)"; then
    ok "a panel that does not come back afterwards is reported, not glossed over"
else bad "a panel that does not come back afterwards is reported, not glossed over" \
         "status $(state_of status): $(log_text | tail -3)"; fi

# --force, which is the whole of the repair path: it reinstalls the release
# already on the server, and it is what docs/UPDATES.md sends an operator to
# after an install that stopped half way - the case where VERSION is already
# the new one, so a plain apply says "nothing to do" and there is no other
# route back. It resolved the bundle out of an answer that carried asset URLs
# only when an upgrade was on offer, so it failed every single time, with
# "release X carries no awg-panel.sh" - which blamed the release for it.
feed v1.0.0
worker --force >/dev/null
if grep -q 'reinstalling v1.0.0 over 1.0.0' <<<"$(log_text)"; then
    ok "--force reinstalls the version already installed"
else bad "--force reinstalls the version already installed" "$(log_text | tail -5)"; fi
if grep -q 'sha256 .* matches' <<<"$(log_text)" && grep -q 'the installer finished' <<<"$(log_text)"
then ok "and it reaches the installer, checksum and backup and all"
else bad "and it reaches the installer, checksum and backup and all" "$(log_text | tail -5)"; fi
if grep -q 'carries no awg-panel.sh' <<<"$(log_text)"; then
    bad "and it does not blame the release for carrying no installer" "$(log_text | tail -3)"
else ok "and it does not blame the release for carrying no installer"; fi
# Back to an upgrade being on offer, which is what everything below assumes.
feed v1.1.0
sums

printf '\n== what is refused ==\n'

# A checksum for different bytes: the download is real, the statement about it
# is real, and they disagree. Nothing may be executed.
printf '%s  awg-panel.sh\n' \
    "0000000000000000000000000000000000000000000000000000000000000000" \
    > "$ASSETS/SHA256SUMS"
rc=$(worker)
if [[ "$rc" != "0" && "$(state_of status)" == "failed" ]]; then
    ok "a bundle that does not match its checksum is refused"
else bad "a bundle that does not match its checksum is refused" "exit $rc"; fi
if grep -q 'does not match the checksum' <<<"$(log_text)"; then
    ok "and the log says the checksum is why"
else bad "and the log says the checksum is why" "$(log_text | tail -3)"; fi

# A SHA256SUMS that is about some other file. Not a mismatch - there is nothing
# to compare - and the answer has to be the same refusal rather than a skip.
printf '%s  something-else.tar.gz\n' \
    "0000000000000000000000000000000000000000000000000000000000000000" \
    > "$ASSETS/SHA256SUMS"
rc=$(worker)
if [[ "$rc" != "0" ]] && grep -q 'does not mention' <<<"$(log_text)"; then
    ok "a SHA256SUMS that does not name the bundle is refused"
else bad "a SHA256SUMS that does not name the bundle is refused" "$(log_text | tail -3)"; fi

# Something that arrived intact and is not a bundle. The checksum cannot catch
# this - it is the checksum of exactly these bytes - so the shape is checked
# before bash is handed the file.
printf 'not a script at all\n' > "$ASSETS/awg-panel.sh"
sums
rc=$(worker)
if [[ "$rc" != "0" ]] && grep -q 'not a shell installer' <<<"$(log_text)"; then
    ok "a download that is not a shell installer is refused"
else bad "a download that is not a shell installer is refused" "$(log_text | tail -3)"; fi

printf '#!/bin/bash\nexit 3\n' > "$ASSETS/awg-panel.sh"
sums
rc=$(worker)
if [[ "$rc" != "0" ]] && grep -q 'will not unpack' <<<"$(log_text)"; then
    ok "a bundle whose --help fails is refused as damaged"
else bad "a bundle whose --help fails is refused as damaged" "$(log_text | tail -3)"; fi

# Back to a working bundle for the last two.
printf '#!/bin/bash\ncase "$1" in --help) echo usage; exit 0 ;; esac\nexit 0\n' \
    > "$ASSETS/awg-panel.sh"
sums

# No backup, no update. The archive is the whole of what protects the data
# through a release that turns out to be bad, so an update that could not take
# one is an update that has to stop rather than proceed uninsured.
backup_fails
rc=$(worker)
if [[ "$rc" != "0" ]] && grep -q 'pre-update backup could not be taken' <<<"$(log_text)"; then
    ok "an update stops when the backup fails"
else bad "an update stops when the backup fails" "$(log_text | tail -3)"; fi
worker --skip-backup >/dev/null
if grep -q 'the update goes on' <<<"$(log_text)" && grep -q 'the installer finished' <<<"$(log_text)"
then ok "and --skip-backup is the way past it"
else bad "and --skip-backup is the way past it" "$(log_text | tail -3)"; fi
backup_ok

printf '\n== one at a time ==\n'

# The lock, which is what stops a second click on a slow button reinstalling the
# server twice at once. Held for the whole run, so a second worker must refuse
# rather than wait: the caller is a web request, and a request that blocks until
# a four-minute install finishes is a request that times out.
feed v1.1.0
rm -rf "$ROOT/state"
mkdir -p "$ROOT/state"
( exec 9>"$ROOT/state/lock"; flock -n 9 && sleep 5 ) &
holder=$!
sleep 0.5
out=$(env AWG_PANEL_PREFIX="$PREFIX" AWG_PANEL_ENV="$ROOT/nonexistent.env" \
      AWG_PANEL_DATA="$DATA" AWG_UPDATE_DIR="$ROOT/state" \
      AWG_UPDATE_FEED="$ROOT/feed.json" bash "$TOOL" run 2>&1)
rc=$?
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
if [[ "$rc" != "0" ]] && grep -q 'already running' <<<"$out"; then
    ok "a second update refuses while one holds the lock"
else bad "a second update refuses while one holds the lock" "exit $rc: $out"; fi

# The bytes below are about to be run as root, and the checksum that vouches
# for them arrives over the same connection - so a cleartext one proves
# nothing at all: anything on the path rewrites both and the comparison still
# passes. curl refuses http itself (--proto '=https'); wget does not, because
# --https-only governs which links it follows when recursing and says nothing
# about the URL it was handed. So the scheme is checked before either of them
# sees it, which is what get.sh has always done.
feed v1.1.0
sed -i 's|file://|http://mirror.invalid|g' "$ROOT/feed.json"
rc=$(worker)
if [[ "$rc" != "0" ]] && grep -q 'refusing to download over http' <<<"$(log_text)"; then
    ok "a release served over plain http is refused before it is fetched"
else bad "a release served over plain http is refused before it is fetched" "$(log_text | tail -3)"; fi
feed v1.1.0

printf '\n== a worker that is gone ==\n'

# The state file a worker leaves when it never reaches its exit trap - the OOM
# killer, or the reset button. It says "running" with a pid in it, and pids are
# handed out from the bottom again after a reboot, so that number belongs to
# something else a few minutes later. Everything downstream then believed an
# update was in progress forever: status said running, start refused, and
# cancel - the way out docs/UPDATES.md names - refused as well, because it was
# asking the same question of the same wrong answer.
proc_start() {  # pid
    local raw; raw=$(< "/proc/$1/stat") || { printf ''; return 0; }
    raw=${raw##*') '}
    awk '{print $20}' <<<"$raw"
}
THIS_BOOT=$(tr -d '[:space:]' < /proc/sys/kernel/random/boot_id 2>/dev/null || printf '')

wedge() {  # extra-json-fields
    rm -rf "$ROOT/state"; mkdir -p "$ROOT/state"
    cat > "$ROOT/state/state.json" <<EOF
{"status": "running", "phase": "install", "detail": "Installing",
 "from_version": "1.0.0", "to_version": "v1.1.0",
 "started": "2020-01-01T00:00:00Z", "finished": "", "backup": "",
 "pid": "$$", "unit": "awg-panel-update"${1:+, $1}}
EOF
}
# run(), but without the state directory being wiped first: what these cases
# are about is the file that is already there.
at() {
    env AWG_PANEL_PREFIX="$PREFIX" \
        AWG_PANEL_ENV="$ROOT/nonexistent.env" \
        AWG_PANEL_DATA="$DATA" \
        AWG_UPDATE_DIR="$ROOT/state" \
        AWG_UPDATE_FEED="$ROOT/feed.json" \
        bash "$TOOL" "$@" 2>&1
}

wedge '"boot": "00000000-0000-0000-0000-000000000000"'
if grep -q 'failed' <<<"$(at status)"; then
    ok "a state file from a previous boot is not a running update"
else bad "a state file from a previous boot is not a running update" "$(at status)"; fi

wedge "\"boot\": \"$THIS_BOOT\", \"pid_start\": \"1\""
if grep -q 'failed' <<<"$(at status)"; then
    ok "and neither is a pid this boot has since handed out again"
else bad "and neither is a pid this boot has since handed out again" "$(at status)"; fi

# The live worker, which none of the above may call dead.
wedge "\"boot\": \"$THIS_BOOT\", \"pid_start\": \"$(proc_start $$)\""
if grep -q 'running' <<<"$(at status)"; then
    ok "and an update that really is running still reads as running"
else bad "and an update that really is running still reads as running" "$(at status)"; fi

# The escape hatch, which used to refuse in exactly the case it exists for.
# Nothing holds the lock here, and a free lock is the one answer that does not
# come from the state file everything else has just disagreed with.
out=$(at cancel)
if grep -q 'cleared\|очищено' <<<"$out"; then
    ok "cancel clears a stale state when no worker holds the lock"
else bad "cancel clears a stale state when no worker holds the lock" "$out"; fi

# And it still refuses while one genuinely does, unless told outright. The
# state file is written first: wedge() remakes the directory, and a holder
# started before it would be holding a lock file that had since been unlinked.
wedge "\"boot\": \"$THIS_BOOT\", \"pid_start\": \"$(proc_start $$)\""
( exec 9>"$ROOT/state/lock"; flock -n 9 && sleep 5 ) &
holder=$!
sleep 0.5
out=$(at cancel); rc=$?
if [[ "$rc" != "0" ]]; then ok "and refuses while a worker really is holding it"
else bad "and refuses while a worker really is holding it" "exit $rc: $out"; fi
out=$(at cancel --force)
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
if grep -q 'cleared\|очищено' <<<"$out"; then ok "and --force clears it anyway"
else bad "and --force clears it anyway" "$out"; fi

printf '\n== one repository, named once ==\n'

# install-panel.sh writes the default into /etc/awg-panel.env by reading it back
# out of release.py, so that the literal exists in one place. If that sed ever
# stops matching it writes an empty value, which still works - the Python falls
# back to its own default - and would go unnoticed until somebody tried to
# change it in the file and found nothing there to change.
from_py=$(python3 -c 'import re,sys
print(re.search(r"^DEFAULT_REPO = \"(.*)\"$", open(sys.argv[1]).read(), re.M).group(1))' \
    "$REPO/panel/awg/release.py")
from_sh=$(sed -n 's/^DEFAULT_REPO = "\(.*\)"$/\1/p' "$REPO/panel/awg/release.py" | head -1)
if [[ -n "$from_py" && "$from_py" == "$from_sh" ]]; then
    ok "install-panel.sh reads the same default repository the panel uses ($from_py)"
else bad "install-panel.sh reads the same default repository the panel uses" \
         "python says '$from_py', the installer's sed says '$from_sh'"; fi

# And the two asset names the release workflow asserts on are the two this asks
# for. The workflow greps them out of this same file; a rename that misses one
# end is a release nothing can install.
for name in awg-panel.sh SHA256SUMS; do
    if grep -q "^ASSET_[A-Z]* = \"${name}\"$" "$REPO/panel/awg/release.py"; then
        ok "the release helper asks for ${name} by name"
    else bad "the release helper asks for ${name} by name"; fi
done

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
