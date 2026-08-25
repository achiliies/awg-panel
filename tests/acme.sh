#!/bin/bash
#
# tests/acme.sh - prove certificate adoption bridges the service sandbox.
#
# The panel's systemd services run with ProtectHome=yes and PrivateTmp=yes.
# An admin who picks a certificate from /root/.acme.sh or a home directory in
# awg-menu sees a readable file on their terminal that gunicorn cannot see at
# all. When the menu applies that path, gunicorn fails to start on it, the
# panel service dies, and the rollback returns the server to plain HTTP.
#
# Adoption bridges that gap: it copies the certificate and key to /etc/ssl/awg-panel/
# (or installs via acme.sh --install-cert with a reloadcmd when acme.sh is present)
# so the service can read them. Unmasked paths remain untouched.
#
# This test verifies path masking detection, certificate pair validation,
# adoption via direct, acme.sh and copy mechanisms, existing cert discovery,
# and proper teardown by acme_remove.
#
# Usage: tests/acme.sh      (exit 0 = all checks passed)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

WORK=$(mktemp -d /tmp/awg-acme-XXXXXX)
# If /dev/shm is available, create an unmasked directory there so unmasked tests
# can run against real, readable certificate and key files.
UNMASKED=""
if [[ -d /dev/shm && -w /dev/shm ]]; then
    UNMASKED=$(mktemp -d -p /dev/shm 2>/dev/null || true)
fi
trap 'rm -rf "$WORK" ${UNMASKED:+"$UNMASKED"}' EXIT

# A stub systemctl that succeeds on every invocation.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'STUB'
#!/bin/sh
exit 0
STUB
chmod 755 "$WORK/bin/systemctl"
PATH="$WORK/bin:$PATH"

# Repoint ACME_TLS_DIR to a temporary directory before and after sourcing.
AWG_ACME_TLS_DIR="$WORK/ssl"
export AWG_ACME_TLS_DIR

# shellcheck source=lib/common.sh
. "$REPO/lib/common.sh"
# shellcheck source=lib/firewall.sh
. "$REPO/lib/firewall.sh"
# shellcheck source=lib/panel.sh
. "$REPO/lib/panel.sh"
# shellcheck source=lib/acme.sh
. "$REPO/lib/acme.sh"

ACME_TLS_DIR="$WORK/ssl"

FAIL=0
# acme_adopt hands its answer back in ACME_ADOPT_HOW, ACME_ADOPT_ERR,
# ACME_ADOPT_CERT and ACME_ADOPT_KEY as well as on stdout, and `$(acme_adopt
# ...)` would run it in a subshell where every one of those is discarded - the
# caller would see an empty ACME_ADOPT_HOW for an adoption that plainly
# happened. So it is run here in this shell with its stdout going to a file,
# and ADOPT_OUT holds what it printed.
ADOPT_OUT=""
adopt() {
    local rc=0
    : > "$WORK/adopt.out"
    acme_adopt "$@" > "$WORK/adopt.out" 2>/dev/null || rc=$?
    ADOPT_OUT=$(cat "$WORK/adopt.out")
    return "$rc"
}
ok()    { printf '  ok    %s\n' "$1"; }
bad()   { printf '  FAIL  %s\n' "$1"; FAIL=1; }
gone()  { [[ -e "$1" ]] && bad "$2 is still there" || ok "$2"; }
there() { [[ -e "$1" ]] && ok "$2" || bad "$2 was removed"; }

# ----------------------------------------------------------- fixture generation
# Generate RSA and EC certificate pairs for testing, as well as a mismatched key.
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/rsa.key" -out "$WORK/rsa.crt" \
    -days 30 -subj "/CN=rsa.example.com" >/dev/null 2>&1

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$WORK/ec.key" -out "$WORK/ec.crt" \
    -days 30 -subj "/CN=ec.example.com" >/dev/null 2>&1

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/other.key" -out "$WORK/other.crt" \
    -days 30 -subj "/CN=other.example.com" >/dev/null 2>&1

if [[ -n "$UNMASKED" ]]; then
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$UNMASKED/unmasked.key" -out "$UNMASKED/unmasked.crt" \
        -days 30 -subj "/CN=unmasked.example.com" >/dev/null 2>&1
fi

HAVE_EXPIRED=0
if openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/exp.key" -out "$WORK/exp.crt" \
    -subj "/CN=expired.example.com" -not_after 20200101000000Z >/dev/null 2>&1; then
    HAVE_EXPIRED=1
fi

echo "acme: path masking and certificate adoption"

# ------------------------------------------------------------- acme_path_masked
# Paths in directories that the systemd unit sandbox empties out must be detected
# as masked, while public and standard system locations must be considered unmasked.
echo
echo "acme_path_masked: identifying paths the service cannot reach"

for p in "/root/.acme.sh/x/fullchain.cer" \
         "/home/someone/cert.pem" \
         "/tmp/c.pem" \
         "/var/tmp/c.pem" \
         "/run/user/0/c.pem"; do
    if acme_path_masked "$p"; then
        ok "masked: $p"
    else
        bad "expected $p to be masked"
    fi
done

for p in "/etc/ssl/panel.pem" \
         "/etc/letsencrypt/live/awg-panel/fullchain.pem" \
         "/rootcert/x.pem" \
         ""; do
    if acme_path_masked "$p"; then
        bad "expected '${p:-<empty>}' NOT to be masked"
    else
        ok "unmasked: ${p:-<empty>}"
    fi
done

# -------------------------------------------------------------- acme_check_pair
# acme_check_pair must return 0 for good pairs, return 1 for mismatched pairs,
# and populate ACME_PAIR_MASKED with any masked paths without failing on them.
echo
echo "acme_check_pair: validating certificate and key pairs"

ACME_PAIR_MASKED=""
ACME_PAIR_ERR=""
if acme_check_pair "$WORK/rsa.crt" "$WORK/rsa.key"; then
    ok "valid RSA pair returns 0"
else
    bad "valid RSA pair returned non-zero ($ACME_PAIR_ERR)"
fi

if acme_check_pair "$WORK/ec.crt" "$WORK/ec.key"; then
    ok "valid EC pair returns 0"
else
    bad "valid EC pair returned non-zero ($ACME_PAIR_ERR)"
fi

# Files inside $WORK (under /tmp) are masked; acme_check_pair must report them
# in ACME_PAIR_MASKED. Re-run against the RSA pair rather than reading what the
# EC call above left behind - checking one call's variable after a later call
# has overwritten it is how a check like this passes without meaning anything.
acme_check_pair "$WORK/rsa.crt" "$WORK/rsa.key"
if [[ -n "$ACME_PAIR_MASKED" && "$ACME_PAIR_MASKED" == *"$WORK/rsa.crt"* && "$ACME_PAIR_MASKED" == *"$WORK/rsa.key"* ]]; then
    ok "masked paths recorded in ACME_PAIR_MASKED without refusing"
else
    bad "ACME_PAIR_MASKED did not record masked paths (got: '$ACME_PAIR_MASKED')"
fi

# A key from another certificate must be rejected with exit code 1 and an error description.
ACME_PAIR_ERR=""
res=0
acme_check_pair "$WORK/rsa.crt" "$WORK/other.key" || res=$?
if [[ "$res" -eq 1 && -n "$ACME_PAIR_ERR" ]]; then
    ok "mismatched key returns 1 with ACME_PAIR_ERR set ($ACME_PAIR_ERR)"
else
    bad "mismatched key did not return 1 with error set (ret=$res, err='$ACME_PAIR_ERR')"
fi

# An unmasked pair should leave ACME_PAIR_MASKED empty.
if [[ -n "$UNMASKED" && -f "$UNMASKED/unmasked.crt" ]]; then
    ACME_PAIR_MASKED="sentinel"
    if acme_check_pair "$UNMASKED/unmasked.crt" "$UNMASKED/unmasked.key"; then
        if [[ -z "$ACME_PAIR_MASKED" ]]; then
            ok "unmasked pair leaves ACME_PAIR_MASKED empty"
        else
            bad "unmasked pair set ACME_PAIR_MASKED: '$ACME_PAIR_MASKED'"
        fi
    else
        bad "acme_check_pair failed on unmasked pair"
    fi
fi

if (( HAVE_EXPIRED )); then
    res=0
    acme_check_pair "$WORK/exp.crt" "$WORK/exp.key" || res=$?
    if [[ "$res" -eq 2 ]]; then
        ok "expired certificate returns 2"
    else
        bad "expired certificate did not return 2 (got $res)"
    fi
fi

# ----------------------------------------------------- acme_adopt: direct route
# An unmasked pair is already reachable by the service, so acme_adopt leaves
# the files in place and returns them directly without copying anything.
echo
echo "acme_adopt: unmasked pair uses direct route"

if [[ -n "$UNMASKED" && -f "$UNMASKED/unmasked.crt" ]]; then
    ACME_ADOPT_HOW=""
    ACME_ADOPT_ERR=""
    rm -rf "$ACME_TLS_DIR"
    out=""
    adopt "$UNMASKED/unmasked.crt" "$UNMASKED/unmasked.key" || bad "acme_adopt on unmasked pair returned non-zero"
    out="$ADOPT_OUT"
    lines=()
    while IFS= read -r l; do [[ -n "$l" ]] && lines+=("$l"); done <<<"$out"

    if [[ "${#lines[@]}" -eq 2 && "${lines[0]}" == "$UNMASKED/unmasked.crt" && "${lines[1]}" == "$UNMASKED/unmasked.key" ]]; then
        ok "direct adoption prints original paths back unchanged"
    else
        bad "direct adoption printed unexpected output: $out"
    fi

    if [[ "$ACME_ADOPT_HOW" == "direct" ]]; then
        ok "ACME_ADOPT_HOW is direct"
    else
        bad "ACME_ADOPT_HOW expected direct, got '$ACME_ADOPT_HOW'"
    fi

    if [[ ! -e "$ACME_TLS_DIR/panel.pem" ]]; then
        ok "direct adoption copies nothing into ACME_TLS_DIR"
    else
        bad "direct adoption copied files into $ACME_TLS_DIR"
    fi
fi

# ------------------------------------------------------- acme_adopt: copy route
# When a certificate is in a masked directory and no acme.sh is present, acme_adopt
# copies the pair into $ACME_TLS_DIR with mode 0600 on the private key.
echo
echo "acme_adopt: masked pair with no acme.sh uses copy route"

rm -rf "$ACME_TLS_DIR"
ACME_ADOPT_HOW=""
ACME_ADOPT_ERR=""

out=""
adopt "$WORK/rsa.crt" "$WORK/rsa.key" || bad "acme_adopt copy route returned non-zero"
out="$ADOPT_OUT"
lines=()
while IFS= read -r l; do [[ -n "$l" ]] && lines+=("$l"); done <<<"$out"

if [[ "${#lines[@]}" -eq 2 && "${lines[0]}" == "$ACME_TLS_DIR/panel.pem" && "${lines[1]}" == "$ACME_TLS_DIR/panel.key" ]]; then
    ok "copy adoption prints panel.pem and panel.key paths"
else
    bad "copy adoption printed unexpected paths: $out"
fi

if [[ "$ACME_ADOPT_HOW" == "copy" ]]; then
    ok "ACME_ADOPT_HOW is copy"
else
    bad "ACME_ADOPT_HOW expected copy, got '$ACME_ADOPT_HOW'"
fi

if [[ -f "$ACME_TLS_DIR/panel.pem" && -f "$ACME_TLS_DIR/panel.key" ]]; then
    ok "both panel.pem and panel.key exist in ACME_TLS_DIR"
else
    bad "panel.pem or panel.key missing in $ACME_TLS_DIR"
fi

key_mode=$(stat -c %a "$ACME_TLS_DIR/panel.key" 2>/dev/null || stat -f %Lp "$ACME_TLS_DIR/panel.key" 2>/dev/null || echo "")
if [[ "$key_mode" == "600" ]]; then
    ok "panel.key has mode 0600"
else
    bad "panel.key mode is '$key_mode' (wanted 600)"
fi

if acme_pair_ok "$ACME_TLS_DIR/panel.pem" "$ACME_TLS_DIR/panel.key"; then
    ok "acme_pair_ok holds for the copied pair"
else
    bad "copied pair failed acme_pair_ok"
fi

# ---------------------------------------------------- acme_adopt: acme.sh route
# When the certificate was issued by acme.sh, adoption runs acme.sh --install-cert
# with a --reloadcmd so renewals will automatically refresh the copy.
echo
echo "acme_adopt: acme.sh route with install-cert and reloadcmd"

mkdir -p "$WORK/acme_home/adopted.example.com_ecc"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$WORK/acme_home/adopted.example.com_ecc/adopted.example.com.key" \
    -out "$WORK/acme_home/adopted.example.com_ecc/fullchain.cer" \
    -days 30 -subj "/CN=adopted.example.com" >/dev/null 2>&1

cat > "$WORK/acme_home/acme.sh" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$(dirname "$0")/acme.sh.args"
fullchain=""
keyfile=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fullchain-file) fullchain="$2"; shift 2 ;;
        --key-file)       keyfile="$2"; shift 2 ;;
        *) shift ;;
    esac
done
dir="$(dirname "$0")"
if [[ -n "$fullchain" ]]; then
    mkdir -p "$(dirname "$fullchain")"
    cp "$dir/adopted.example.com_ecc/fullchain.cer" "$fullchain"
fi
if [[ -n "$keyfile" ]]; then
    mkdir -p "$(dirname "$keyfile")"
    cp "$dir/adopted.example.com_ecc/adopted.example.com.key" "$keyfile"
    chmod 600 "$keyfile"
fi
exit 0
STUB
chmod 755 "$WORK/acme_home/acme.sh"
rm -f "$WORK/acme_home/acme.sh.args"
rm -rf "$ACME_TLS_DIR"

OLD_PATH="$PATH"
PATH="$WORK/acme_home:$PATH"

ACME_ADOPT_HOW=""
ACME_ADOPT_ERR=""
out=""
adopt "$WORK/acme_home/adopted.example.com_ecc/fullchain.cer" \
      "$WORK/acme_home/adopted.example.com_ecc/adopted.example.com.key" \
    || bad "acme_adopt on acme.sh pair returned non-zero"
out="$ADOPT_OUT"

if [[ "$ACME_ADOPT_HOW" == "acme.sh" ]]; then
    ok "ACME_ADOPT_HOW is acme.sh"
else
    bad "ACME_ADOPT_HOW expected acme.sh, got '$ACME_ADOPT_HOW'"
fi

args_recorded=""
[[ -f "$WORK/acme_home/acme.sh.args" ]] && args_recorded=$(cat "$WORK/acme_home/acme.sh.args")

if [[ "$args_recorded" == *"--install-cert"* ]]; then
    ok "acme.sh was called with --install-cert"
else
    bad "acme.sh args missing --install-cert (got: '$args_recorded')"
fi

if [[ "$args_recorded" == *"--ecc"* ]]; then
    ok "acme.sh was called with --ecc"
else
    bad "acme.sh args missing --ecc (got: '$args_recorded')"
fi

if [[ "$args_recorded" == *"adopted.example.com"* && "$args_recorded" != *"adopted.example.com_ecc"* ]]; then
    ok "acme.sh was called with domain stripped of _ecc"
else
    bad "acme.sh args did not pass stripped domain (got: '$args_recorded')"
fi

if [[ "$args_recorded" == *"--reloadcmd"*"systemctl restart awg-panel-web"* ]]; then
    ok "acme.sh reloadcmd includes systemctl restart awg-panel-web"
else
    bad "acme.sh args missing reloadcmd for awg-panel-web (got: '$args_recorded')"
fi

# ------------------------------------------------- acme_adopt: acme.sh fallback
# If acme.sh fails during adoption, acme_adopt must fall back to a plain copy
# rather than failing.
echo
echo "acme_adopt: acme.sh failure falls back to copy route"

cat > "$WORK/acme_home/acme.sh" <<'STUB'
#!/bin/bash
exit 1
STUB
chmod 755 "$WORK/acme_home/acme.sh"
rm -rf "$ACME_TLS_DIR"

ACME_ADOPT_HOW=""
ACME_ADOPT_ERR=""
out=""
adopt "$WORK/acme_home/adopted.example.com_ecc/fullchain.cer" \
      "$WORK/acme_home/adopted.example.com_ecc/adopted.example.com.key" \
    || bad "acme_adopt fallback returned non-zero"
out="$ADOPT_OUT"

if [[ "$ACME_ADOPT_HOW" == "copy" ]]; then
    ok "ACME_ADOPT_HOW fell back to copy"
else
    bad "ACME_ADOPT_HOW expected copy on acme.sh failure, got '$ACME_ADOPT_HOW'"
fi

if [[ -f "$ACME_TLS_DIR/panel.pem" && -f "$ACME_TLS_DIR/panel.key" ]]; then
    ok "fallback produced panel.pem and panel.key"
else
    bad "fallback missing panel.pem or panel.key"
fi

if acme_pair_ok "$ACME_TLS_DIR/panel.pem" "$ACME_TLS_DIR/panel.key"; then
    ok "fallback copy is a usable pair"
else
    bad "fallback copy pair failed acme_pair_ok"
fi

PATH="$OLD_PATH"

# ---------------------------------------------- acme_adopt: bad pair rejection
# acme_adopt on a mismatched pair must return 1, set ACME_ADOPT_ERR, and produce
# no stdout output.
echo
echo "acme_adopt: mismatched pair fails cleanly"

ACME_ADOPT_ERR=""
res=0
adopt "$WORK/rsa.crt" "$WORK/other.key" || res=$?
stdout_out="$ADOPT_OUT"

if [[ "$res" -eq 1 ]]; then
    ok "acme_adopt returns 1 for mismatched pair"
else
    bad "acme_adopt on mismatched pair returned $res (expected 1)"
fi

if [[ -n "$ACME_ADOPT_ERR" ]]; then
    ok "ACME_ADOPT_ERR is set for mismatched pair ($ACME_ADOPT_ERR)"
else
    bad "ACME_ADOPT_ERR was not set for mismatched pair"
fi

if [[ -z "$stdout_out" ]]; then
    ok "acme_adopt prints nothing on stdout on failure"
else
    bad "acme_adopt printed to stdout on failure: '$stdout_out'"
fi

# ----------------------------------------------------------- acme_existing_certs
# acme_existing_certs must drop certificates whose key beside it does not match,
# and keep ones whose key does.
echo
echo "acme_existing_certs: filtering out invalid or mismatched certificates"

ACME_LE="$WORK/fake_le/etc/letsencrypt"
mkdir -p "$ACME_LE/live/good.example.com" "$ACME_LE/live/badkey.example.com"

cp "$WORK/rsa.crt" "$ACME_LE/live/good.example.com/fullchain.pem"
cp "$WORK/rsa.key" "$ACME_LE/live/good.example.com/privkey.pem"

cp "$WORK/rsa.crt"   "$ACME_LE/live/badkey.example.com/fullchain.pem"
cp "$WORK/other.key" "$ACME_LE/live/badkey.example.com/privkey.pem"

found_certs=$(acme_existing_certs)

if grep -q "$ACME_LE/live/good.example.com/fullchain.pem" <<<"$found_certs"; then
    ok "acme_existing_certs includes matching certificate"
else
    bad "acme_existing_certs missed matching certificate"
fi

if grep -q "$ACME_LE/live/badkey.example.com/fullchain.pem" <<<"$found_certs"; then
    bad "acme_existing_certs included certificate with mismatched key"
else
    ok "acme_existing_certs dropped certificate with mismatched key"
fi

# -------------------------------------------------------------------- acme_apply
# acme_apply must refuse a pair the service cannot open, before it writes
# anything. gunicorn is what would otherwise discover the problem - by exiting -
# and the admin would get a journal dump where a sentence naming the path
# belongs. The env file is checked afterwards because a refusal that has already
# rewritten AWG_PANEL_TLS is not a refusal.
echo
echo "acme_apply: refusing a certificate the service cannot open"

PANEL_ENV="$WORK/panel.env"
printf 'AWG_PANEL_TLS=0\n' > "$PANEL_ENV"

if acme_apply "/root/.acme.sh/x.example.com/fullchain.cer" "$WORK/rsa.key" >/dev/null 2>&1; then
    bad "acme_apply accepted a masked certificate path"
else
    ok "acme_apply refused a masked certificate path"
fi

if acme_apply "$WORK/rsa.crt" "/root/.acme.sh/x.example.com/x.key" >/dev/null 2>&1; then
    bad "acme_apply accepted a masked key path"
else
    ok "acme_apply refused a masked key path"
fi

if [[ "$(cat "$PANEL_ENV")" == "AWG_PANEL_TLS=0" ]]; then
    ok "acme_apply left the env file alone when it refused"
else
    bad "acme_apply wrote to the env file for a pair it refused"
fi

# ------------------------------------------------------------------ acme_remove
# acme_remove must delete the adopted panel.pem and panel.key without touching
# the original certificate that was copied.
echo
echo "acme_remove: cleaning up adopted certificate without touching originals"

mkdir -p "$ACME_TLS_DIR"
cp "$WORK/rsa.crt" "$ACME_TLS_DIR/panel.pem"
cp "$WORK/rsa.key" "$ACME_TLS_DIR/panel.key"

ORIG_DIR="$WORK/original_cert"
mkdir -p "$ORIG_DIR"
cp "$WORK/rsa.crt" "$ORIG_DIR/fullchain.pem"
cp "$WORK/rsa.key" "$ORIG_DIR/privkey.pem"

ACME_UNIT_DIR="$WORK/tree/etc/systemd/system"
ACME_HOOK_DIR="$WORK/tree/etc/letsencrypt/renewal-hooks/deploy"
ACME_HOOK="${ACME_HOOK_DIR}/10-awg-panel"
ACME_VENV="$WORK/tree/opt/certbot"
mkdir -p "$ACME_UNIT_DIR" "$ACME_HOOK_DIR"

acme_remove >/dev/null 2>&1 || bad "acme_remove returned non-zero"

gone  "$ACME_TLS_DIR/panel.pem" "adopted panel.pem"
gone  "$ACME_TLS_DIR/panel.key" "adopted panel.key"
there "$ORIG_DIR/fullchain.pem" "original fullchain.pem"
there "$ORIG_DIR/privkey.pem"   "original privkey.pem"

echo
if (( FAIL )); then printf 'RESULT: FAIL\n'; else printf 'RESULT: PASS\n'; fi
exit $FAIL
