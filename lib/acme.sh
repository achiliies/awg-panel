# shellcheck shell=bash
# shellcheck disable=SC2034,SC2154
#
# lib/acme.sh - the panel's TLS certificate.
#
# Everything about it: reading the one that is configured now, getting one from
# Let's Encrypt, pointing the panel at one that already exists on the box,
# renewing, deleting, and going back to plain HTTP.
#
# Requires lib/common.sh (colours and the reporting helpers), lib/firewall.sh
# (fw_allow) and lib/panel.sh (panel_env_get, panel_env_set).
#
# The panel has always installed speaking plain HTTP, and everything that
# matters crosses that connection: the admin password on the way in, the
# session cookie on every request after it, every client's private key and
# preshared key on the way out, and the obfuscation parameters that are the
# whole of this server's resistance to a DPI box. None of it is recovered by
# turning HTTPS on afterwards - a key read off the wire on Tuesday is still
# that client's key on Friday - so the certificate belongs in the install,
# next to the password being printed, rather than in a list of things to get
# round to.
#
# The issuing paths are best-effort by construction. They run at the end of an
# install that has already succeeded, and there is no failure in them worth
# throwing that away for: a certificate that could not be issued leaves a panel
# on HTTP, which is exactly where the panel would have been if this file did
# not exist. Callers invoke acme_offer in a conditional so that bash suspends
# errexit for the whole of it, and every path back out of here leaves the panel
# running.

# The lineage certbot writes under. Fixed, rather than derived from the name
# or address asked for: it makes /etc/letsencrypt/live/awg-panel/ the answer
# whatever was issued, which the deploy hook, the env file and a second run
# that swaps an IP for a domain all depend on. Certbot treats a repeated
# --cert-name as "replace what is in this lineage", which is what a second run
# means.
ACME_LINEAGE=awg-panel
ACME_LE=/etc/letsencrypt
ACME_LIVE="${ACME_LE}/live/${ACME_LINEAGE}"
ACME_HOOK_DIR="${ACME_LE}/renewal-hooks/deploy"
ACME_HOOK="${ACME_HOOK_DIR}/10-awg-panel"

# Where a certificate the service cannot reach gets installed to. The pair
# inside is always panel.pem and panel.key - fixed names, so a second import
# replaces the first rather than stacking a second pair beside it, the same
# reason ACME_LINEAGE is fixed. Overridable from the environment so a test
# harness can repoint it, as IFACE and CONF_DIR are in lib/common.sh.
ACME_TLS_DIR="${AWG_ACME_TLS_DIR:-/etc/ssl/awg-panel}"

# What each kind of certificate needs of certbot. --ip-address arrived in
# certbot 5.3, and it depends on --preferred-profile from 4.0, so the newer
# number covers both; a name has wanted nothing newer than 1.0 for years, which
# every distribution is well past. Checked before certbot is asked, because
# asking an older one for an address fails at the ACME server rather than at
# the argument parser, and that reads as "Let's Encrypt is broken" to whoever
# is watching it happen.
ACME_MIN_IP=5.3
ACME_MIN_DOMAIN=1.0

# The certbot being driven, and where this file puts one when it has to install
# it. Every call goes through the variable rather than through PATH: the
# virtualenv can be built in the middle of this run, and a shell that has
# already looked "certbot" up goes on handing back the path it found the first
# time.
ACME_CERTBOT=certbot
ACME_VENV=/opt/certbot
ACME_UNIT_DIR=/etc/systemd/system
ACME_TIMER=awg-certbot-renew

# Set by the issuing flow for the installer's summary to read: "", "domain",
# "ip" or "existing".
ACME_RESULT=""
ACME_CERT_NAME=""

# Why acme_issue_flow stopped, in one line, for a caller that has to put it in
# a dialog rather than leave it on the terminal the flow was talking to.
#
# It goes with the flow's exit status, and the two are only useful together: 1
# means something failed and this says what, 2 means the admin backed out and
# this is empty, because "cancelled" is not an error anybody needs told back to
# them.
ACME_FLOW_ERR=""

# Where the last certbot run's output was kept, so a caller can show it after
# a failure. Set it before calling to choose the file; otherwise a temp file
# is made on first use.
ACME_LOG="${ACME_LOG:-}"

# Filled in by acme_state, read by every screen that reports on TLS.
ACME_ST_ON=0
ACME_ST_CERT=""
ACME_ST_KEY=""
ACME_ST_NAMES=""
ACME_ST_ISSUER=""
ACME_ST_DAYS=""
ACME_ST_LINEAGE=""
ACME_ST_ERR=""

# Why the last acme_check_pair said no, and which of the paths it was given the
# panel's service cannot see.
ACME_PAIR_ERR=""
ACME_PAIR_MASKED=""

# What the last acme_adopt did, and what it settled on. Declared here with the
# rest so that a caller reading them after a call that never ran gets an empty
# answer rather than an unbound variable.
ACME_ADOPT_HOW=""
ACME_ADOPT_ERR=""
ACME_ADOPT_CERT=""
ACME_ADOPT_KEY=""

# ---------------------------------------------------------------- certbot

# Which certbot to drive. The virtualenv one wins when it is here: this file
# built it because the packaged certbot could not do what was asked, and that
# is still true on the next run.
acme_pick_certbot() {
    if [[ -x "${ACME_VENV}/bin/certbot" ]]; then
        ACME_CERTBOT="${ACME_VENV}/bin/certbot"
    else
        ACME_CERTBOT=certbot
    fi
}

# "5.4.0" out of certbot, or nothing at all when it is not installed. Its
# --version goes to stdout on modern releases and to stderr on old ones, so
# both are read; anything unparsable answers empty and is treated as too old,
# which is the safe direction.
acme_certbot_version() {
    command -v "$ACME_CERTBOT" >/dev/null 2>&1 || return 0
    "$ACME_CERTBOT" --version 2>&1 | sed -n 's/^certbot \([0-9][0-9.]*\).*/\1/p' | head -1
}

# Is version $1 at least $2? Dotted numeric compare, shortest field wins, so
# "5.10" is correctly newer than "5.9" - which sort -V gets right and a string
# compare does not.
acme_version_ge() {
    [[ -n "$1" ]] || return 1
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]
}

# Run certbot, showing its output and keeping a copy for the failure message
# and for whoever wants to page through it afterwards.
acme_run_certbot() {
    # No log at all rather than one at a name anybody could have guessed. This
    # used to fall back to a fixed /tmp/awg-certbot.log and then tee to it as
    # root, so on a shared box whoever created that path first - as a symlink to
    # any file root may write - had certbot's output delivered there. mktemp
    # fails when /tmp is unwritable, full, or not there at all, which is exactly
    # the moment not to be inventing a filename in it.
    #
    # Losing the log costs the one sentence acme_why_failed would have quoted;
    # it already answers "certbot produced no output" when there is nothing to
    # read, and certbot's own output still goes to the terminal either way.
    [[ -n "$ACME_LOG" ]] || ACME_LOG=$(mktemp 2>/dev/null) || ACME_LOG=""
    if [[ -n "$ACME_LOG" ]]; then
        "$ACME_CERTBOT" "$@" 2>&1 | tee -- "$ACME_LOG"
    else
        "$ACME_CERTBOT" "$@" 2>&1
    fi
    return "${PIPESTATUS[0]}"
}

# One line saying why certbot said no, read out of the run ACME_LOG holds.
#
# Certbot's failures are several screens each and the sentence that matters is
# never the last one, so an admin reading the tail sees a stack of ACME URLs
# and no cause. These are the causes that actually happen on this box, in the
# order that distinguishes them: a name that does not resolve looks nothing
# like a name that resolves to a firewall, and the fix is different.
acme_why_failed() {
    local log="$ACME_LOG" line
    # Through %s rather than as the format string itself, which is what these
    # were before there was a second language: a translated message is no
    # longer a literal the author can see, and a stray % in one would be read
    # as a conversion against arguments that are not there.
    [[ -s "$log" ]] && [[ -r "$log" ]] || {
        printf '%s\n' "$(t "certbot produced no output." "certbot ничего не вывел.")"; return 0; }
    if   grep -qi 'NXDOMAIN\|DNS problem\|no valid A records' "$log"; then
        printf '%s\n' "$(t "That name does not resolve to this server yet - it is DNS." \
                           "Это имя пока не разрешается в этот сервер — дело в DNS.")"
    elif grep -qi 'Problem binding to port 80\|Could not bind to IPv4\|Address already in use' "$log"; then
        printf '%s\n' "$(t "Port 80 is already in use here, so nothing could be validated on it." \
                           "Порт 80 здесь уже занят, так что проверить на нём ничего не удалось.")"
    elif grep -qi 'too many certificates\|rateLimited\|rate limit' "$log"; then
        printf '%s\n' "$(t "Let's Encrypt rate limit reached for that name. Wait, or use another." \
                           "Достигнут лимит Let's Encrypt для этого имени. Подождите или возьмите другое.")"
    elif grep -qi 'caa record\|acme:error:caa\|prevents issuance' "$log"; then
        printf '%s\n' "$(t "A CAA record on that domain forbids Let's Encrypt from issuing." \
                           "Запись CAA на этом домене запрещает выпуск через Let's Encrypt.")"
    elif grep -qi 'Timeout during connect\|Connection refused\|connection reset' "$log"; then
        printf '%s\n' "$(t "Port 80 is not reachable from the internet - firewall or security group." \
                           "Порт 80 недоступен из интернета — фаервол или security group.")"
    elif grep -qi 'unauthorized\|Invalid response from http' "$log"; then
        printf '%s\n' "$(t "The validation request never reached this server." \
                           "Проверочный запрос до этого сервера не дошёл.")"
    elif grep -qi 'unrecognized arguments\|no such option\|unrecognized option' "$log"; then
        printf '%s\n' "$(t "This certbot is too old to understand the request." \
                           "Этот certbot слишком стар, чтобы понять запрос.")"
    elif grep -qi 'Missing command line flag\|--agree-tos' "$log"; then
        printf '%s\n' "$(t "Let's Encrypt wants an account registration that could not be made." \
                           "Let's Encrypt требует регистрации учётной записи, а её не удалось сделать.")"
    elif grep -qi 'ip address\|ipAddress' "$log" && grep -qi 'not supported\|profile' "$log"; then
        printf '%s\n' "$(t "Let's Encrypt refused the address - it must be public and routable." \
                           "Let's Encrypt отказал адресу — он должен быть публичным и маршрутизируемым.")"
    else
        line=$(grep -iE '^(error|.*Detail:|.*Error:|.*failed\.)' "$log" | tail -1)
        [[ -n "$line" ]] || line=$(grep -v '^[[:space:]]*$' "$log" | tail -1)
        printf '%s\n' "${line:-$(t "certbot failed without saying why." \
                                   "certbot завершился с ошибкой, не сказав почему.")}"
    fi
}

# ------------------------------------------------------------- reading one

# Does this path lie under a directory masked from the panel service?
#
# The panel's systemd units run with ProtectHome=yes and PrivateTmp=yes, so
# the service is handed an empty /root, /home, /run/user and a private /tmp and
# /var/tmp. awg-menu runs outside that namespace and can read files there, but
# passing those paths to gunicorn makes the service fail to start. Returns 0
# when the path is under one of these directories, 1 otherwise.
acme_path_masked() {
    local p="${1:-}"
    [[ -n "$p" ]] || return 1
    case "$p" in
        /root|/root/*|/home|/home/*|/run/user|/run/user/*|/tmp|/tmp/*|/var/tmp|/var/tmp/*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

acme_cert_readable() { [[ -s "$1" ]] && openssl x509 -in "$1" -noout >/dev/null 2>&1; }

# What a certificate is issued for, as a single line. Both kinds of name: an
# address is an IP SAN and not a DNS one, so a reader that only knows about
# DNS: entries says "covers nothing" about precisely the certificate this file
# exists to obtain.
acme_cert_names() {
    openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null \
        | tr ',' '\n' \
        | sed -n -e 's/.*DNS:\([^ ,]*\).*/\1/p' -e 's/.*IP Address:\([^ ,]*\).*/\1/p' \
        | paste -sd' ' -
}

# Who signed it. The organisation rather than the CN, because Let's Encrypt's
# intermediates are called things like "R11" and that answers nothing.
# openssl prints the name either comma-separated or slash-separated depending
# on its age, so both are split.
acme_cert_issuer() {
    local iss org cn
    iss=$(openssl x509 -in "$1" -noout -issuer 2>/dev/null) || return 0
    iss="${iss#issuer=}"
    org=$(printf '%s\n' "$iss" | tr ',/' '\n\n' \
          | sed -n 's/^[[:space:]]*O[[:space:]]*=[[:space:]]*//p' | head -1)
    cn=$(printf '%s\n' "$iss" | tr ',/' '\n\n' \
         | sed -n 's/^[[:space:]]*CN[[:space:]]*=[[:space:]]*//p' | head -1)
    printf '%s\n' "${org:-${cn:-unknown}}"
}

# Signed by itself, which every browser refuses with a full-page warning. Worth
# saying out loud: a self-signed certificate is the one case where turning TLS
# on makes the panel look broken rather than safe.
acme_cert_selfsigned() {
    local s i
    s=$(openssl x509 -in "$1" -noout -subject 2>/dev/null | sed 's/^subject=//')
    i=$(openssl x509 -in "$1" -noout -issuer  2>/dev/null | sed 's/^issuer=//')
    [[ -n "$s" && "$s" == "$i" ]]
}

acme_cert_until() {
    openssl x509 -in "$1" -noout -enddate 2>/dev/null | sed 's/^notAfter=//'
}

# Already expired. openssl answers this itself, so it is right on a busybox
# date that cannot parse the notAfter line below.
acme_cert_expired() { ! openssl x509 -in "$1" -noout -checkend 0 >/dev/null 2>&1; }

# Whole days left, or nothing when the date cannot be parsed - busybox date
# does not take openssl's format, and Alpine is a supported host. Callers fall
# back to acme_cert_until, which needs no arithmetic.
acme_cert_days() {
    local end now
    end=$(acme_cert_until "$1")
    [[ -n "$end" ]] || return 0
    end=$(date -d "$end" +%s 2>/dev/null) || return 0
    [[ -n "$end" ]] || return 0
    now=$(date +%s)
    printf '%s\n' "$(( (end - now) / 86400 ))"
}

# "34 days" / "expired 2 days ago" / "until Aug 18 ...", for one column of a
# table.
acme_cert_left() {
    local d
    acme_cert_readable "$1" || { printf '%s\n' "$(t "unreadable" "не читается")"; return 0; }
    d=$(acme_cert_days "$1")
    if [[ -z "$d" ]]; then
        acme_cert_expired "$1" && { printf '%s\n' "$(t "expired" "истёк")"; return 0; }
        printf '%s\n' "$(t "until $(acme_cert_until "$1")" "до $(acme_cert_until "$1")")"
        return 0
    fi
    (( d < 0 )) && { printf '%s\n' "$(t "expired $(( -d )) day(s) ago" \
                                         "истёк $(( -d )) дн. назад")"; return 0; }
    printf '%s\n' "$(t "${d} day(s)" "${d} дн.")"
}

# The names clipped to fit a fixed-width column or a one-line menu header. A
# certificate can carry a dozen of them, which overflows a table column and
# wraps a dialog header onto a third line; the full list is on the report.
acme_cert_names_brief() {
    local nm max="${2:-34}"
    nm=$(acme_cert_names "$1")
    [[ -n "$nm" ]] || nm=$(t "unnamed" "без имени")
    (( ${#nm} > max )) && nm="${nm:0:$(( max - 3 ))}..."
    printf '%s\n' "$nm"
}

acme_cert_keytype() {
    openssl x509 -in "$1" -noout -text 2>/dev/null \
        | sed -n -e 's/^ *Public Key Algorithm: *//p' -e 's/^ *Public-Key: *//p' \
        | paste -sd' ' -
}

# How many certificates are in the file. One means the leaf alone, and a leaf
# with no intermediate beside it is accepted by a browser that has cached the
# chain from somewhere else and refused by everything that has not - which is
# the hardest TLS failure to diagnose from the server side, because it works
# for whoever set it up.
acme_cert_count() {
    local n
    n=$(grep -c '^-----BEGIN CERTIFICATE-----' "$1" 2>/dev/null)
    printf '%s\n' "${n:-0}"
}

# Do this key and this certificate belong together? Compared as public keys,
# which works for RSA and EC alike, where a modulus compare only covers RSA.
acme_pair_ok() {
    local a b
    a=$(openssl x509 -in "$1" -noout -pubkey 2>/dev/null) || return 1
    b=$(openssl pkey -in "$2" -pubout 2>/dev/null) || return 1
    [[ -n "$a" && "$a" == "$b" ]]
}

# Everything that decides whether gunicorn will start with this pair, checked
# before the switch is thrown rather than after. 0 usable, 2 usable but
# expired, 1 not usable - with ACME_PAIR_ERR saying which of them it is in the
# words the admin needs, since "the panel did not come up" is not a diagnosis.
acme_check_pair() {
    local cert="$1" key="$2"
    local -a masked=()
    ACME_PAIR_ERR=""
    ACME_PAIR_MASKED=""
    [[ -n "$cert" && -n "$key" ]] || { ACME_PAIR_ERR=$(t "Both a certificate and a key are needed." \
                                                         "Нужны и сертификат, и ключ."); return 1; }
    [[ "$cert" == /* && "$key" == /* ]] || { ACME_PAIR_ERR=$(t "Use absolute paths." \
                                                              "Укажите абсолютные пути."); return 1; }
    [[ -e "$cert" ]] || { ACME_PAIR_ERR=$(t "Nothing at ${cert}" "По пути ${cert} ничего нет"); return 1; }
    [[ -e "$key"  ]] || { ACME_PAIR_ERR=$(t "Nothing at ${key}"  "По пути ${key} ничего нет");  return 1; }
    [[ -f "$cert" ]] || { ACME_PAIR_ERR=$(t "${cert} is not a file." "${cert} — не файл."); return 1; }
    [[ -f "$key"  ]] || { ACME_PAIR_ERR=$(t "${key} is not a file."  "${key} — не файл.");  return 1; }
    [[ -s "$cert" ]] || { ACME_PAIR_ERR=$(t "${cert} is empty." "Файл ${cert} пуст."); return 1; }
    [[ -s "$key"  ]] || { ACME_PAIR_ERR=$(t "${key} is empty."  "Файл ${key} пуст.");  return 1; }
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 \
        || { ACME_PAIR_ERR=$(t "${cert} is not a PEM certificate." \
                               "${cert} — не сертификат в формате PEM."); return 1; }
    openssl pkey -in "$key" -noout >/dev/null 2>&1 \
        || { ACME_PAIR_ERR=$(t "${key} is not a usable PEM private key. An encrypted key cannot be used: gunicorn has nowhere to ask for the passphrase." \
                               "${key} — не пригодный закрытый ключ PEM. Зашифрованный ключ не подойдёт: gunicorn негде спросить пароль к нему."); return 1; }
    acme_pair_ok "$cert" "$key" \
        || { ACME_PAIR_ERR=$(t "That key does not belong to that certificate." \
                               "Этот ключ не от этого сертификата."); return 1; }
    if acme_path_masked "$cert"; then
        masked+=("$cert")
    fi
    if acme_path_masked "$key"; then
        masked+=("$key")
    fi
    ACME_PAIR_MASKED="${masked[*]:-}"
    if acme_cert_expired "$cert"; then
        ACME_PAIR_ERR=$(t "That certificate expired on $(acme_cert_until "$cert")." \
                          "Этот сертификат истёк $(acme_cert_until "$cert").")
        return 2
    fi
    return 0
}

# The full account of one certificate, for a screen somebody opened on purpose.
#
# The labels are laid out with pad() rather than printf's own "%-14s", which
# counts bytes: a Cyrillic label padded that way fills about half the column it
# was given, and every value on the screen then starts somewhere different.
acme_cert_report() {
    local cert="$1" key="${2:-}" n w=14
    printf '  %s%s\n' "$(pad "$(t "certificate"  "сертификат")"    "$w")" "$cert"
    [[ -n "$key" ]] && printf '  %s%s\n' "$(pad "$(t "private key" "закрытый ключ")" "$w")" "$key"
    if ! acme_cert_readable "$cert"; then
        printf '\n  %s\n' "$(t "${cert} cannot be read as a certificate." \
                                 "${cert} не читается как сертификат.")"
        return 0
    fi
    printf '  %s%s\n' "$(pad "$(t "covers"      "покрывает")"    "$w")" "$(acme_cert_names "$cert")"
    printf '  %s%s\n' "$(pad "$(t "issuer"      "издатель")"     "$w")" "$(acme_cert_issuer "$cert")"
    printf '  %s%s\n' "$(pad "$(t "valid until" "действует до")" "$w")" "$(acme_cert_until "$cert")"
    printf '  %s%s\n' "$(pad "$(t "time left"   "осталось")"     "$w")" "$(acme_cert_left "$cert")"
    printf '  %s%s\n' "$(pad "$(t "key type"    "тип ключа")"    "$w")" "$(acme_cert_keytype "$cert")"
    n=$(acme_cert_count "$cert")
    printf '  %s%s\n' "$(pad "$(t "chain" "цепочка")" "$w")" \
        "$(t "${n} certificate(s) in the file" "сертификатов в файле: ${n}")"
    [[ -n "$(acme_lineage_of "$cert")" ]] \
        && printf '  %s%s\n' "$(pad "certbot" "$w")" \
           "$(t "lineage $(acme_lineage_of "$cert")" "линия $(acme_lineage_of "$cert")")"

    printf '\n'
    if acme_cert_selfsigned "$cert"; then
        printf '  !! %s\n' "$(t "self-signed - every browser shows a full-page warning" \
                                 "самоподписанный — любой браузер покажет предупреждение во весь экран")"
    elif (( n < 2 )); then
        printf '  !! %s\n' "$(t "no intermediate in this file - clients that have not cached" \
                                 "в файле нет промежуточного сертификата: клиенты, у которых")"
        printf '     %s\n' "$(t "the chain elsewhere will reject it. Use fullchain.pem." \
                                 "цепочка не сохранена, его отвергнут. Возьмите fullchain.pem.")"
    fi
    if [[ -n "$key" ]] && ! acme_pair_ok "$cert" "$key"; then
        printf '  !! %s\n' "$(t "the key does not match this certificate" \
                                 "ключ не подходит к этому сертификату")"
    fi
    acme_cert_expired "$cert" && printf '  !! %s\n' "$(t "expired" "истёк")"
    return 0
}

# --------------------------------------------------------- what is live now

# The certbot lineage a path belongs to, or nothing.
acme_lineage_of() {
    local d
    [[ -n "${1:-}" ]] || return 0
    d=$(dirname -- "$1")
    [[ "$d" == "${ACME_LE}/live/"* ]] || return 0
    printf '%s\n' "${d##*/}"
}

# Every certbot lineage on this machine, one name per line.
acme_lineages() {
    local d
    for d in "${ACME_LE}"/live/*/; do
        [[ -f "${d}fullchain.pem" ]] || continue
        d="${d%/}"
        printf '%s\n' "${d##*/}"
    done
}

# Certificates already on this machine that are still valid, one path per line.
# A server that has run another panel - x-ui, Marzban, a plain nginx site -
# very often already has one of these, and pointing at it issues nothing, opens
# no port and publishes nothing new to a transparency log. It is the best
# outcome available here and so it is looked for first.
#
# Everything on this list is applied by a single confirmation, so everything on
# it has to be applicable. The list used to be "every certificate that parses
# and has not expired", which put entries on the screen that answered "not
# usable" the moment they were picked - a menu offering something and then
# explaining why it could not have been offered. A certificate whose key sits
# in a layout acme_key_beside does not know is not lost by this: the import
# entry still takes two typed paths and says what is wrong with them.
#
# Masked paths are deliberately not filtered out. A certificate under
# /root/.acme.sh is one the panel's service cannot open, but acme_adopt makes
# it one it can, and dropping it here would delete the only reason those globs
# exist.
acme_existing_certs() {
    local cert key seen=""
    for cert in "${ACME_LE}"/live/*/fullchain.pem \
                /root/.acme.sh/*/fullchain.cer \
                /root/.acme.sh/*_ecc/fullchain.cer; do
        [[ -f "$cert" ]] || continue
        # One line per certificate. The middle glob already matches the "_ecc"
        # directories the last one is for, so every acme.sh certificate issued
        # against an EC key came out of here twice and was numbered twice on
        # the menu built from it.
        [[ "$seen" == *"|${cert}|"* ]] && continue
        seen="${seen}|${cert}|"
        # Still valid, and for long enough to be worth offering. -checkend
        # takes seconds; a day of margin keeps this from proposing one that
        # expires during the conversation about it.
        openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null 2>&1 || continue
        # And ready to be served: readable, with a key beside it that can be
        # found, that gunicorn could open, and that belongs to this
        # certificate. These are the four things acme_check_pair would have
        # refused it for afterwards.
        acme_cert_readable "$cert" || continue
        key=$(acme_key_beside "$cert")
        [[ -n "$key" && -s "$key" ]] || continue
        openssl pkey -in "$key" -noout >/dev/null 2>&1 || continue
        acme_pair_ok "$cert" "$key" || continue
        printf '%s\n' "$cert"
    done
}

# The private key beside a certificate, or nothing when the pair is not one of
# the two layouts this knows. Guessing wider would hand gunicorn a path that
# does not open, and gunicorn's answer to that is a service that does not come
# back.
acme_key_beside() {
    local cert="$1" dir
    [[ -n "$cert" ]] || return 0
    dir=$(dirname -- "$cert")
    if [[ -f "$dir/privkey.pem" ]]; then printf '%s\n' "$dir/privkey.pem"; return; fi   # certbot
    if [[ -f "$dir/${ACME_LINEAGE}.key" ]]; then printf '%s\n' "$dir/${ACME_LINEAGE}.key"; return; fi
    # acme.sh names the key after the domain the directory is named for.
    local base; base=$(basename -- "$dir"); base="${base%_ecc}"
    [[ -f "$dir/${base}.key" ]] && printf '%s\n' "$dir/${base}.key"
    return 0
}

# What the panel is actually serving, into the ACME_ST_* variables.
#
# This is the question every screen here used to answer from nothing. The
# certificate offer greeted an admin who already had HTTPS with "the panel is
# answering over plain HTTP", and the on/off switch beside it could not say
# which certificate it was switching, because neither of them ever looked. One
# reader, called on every redraw, is what makes the screens describe the box
# rather than a guess about it.
#
# ACME_ST_ERR is the difference between "HTTPS is on" and "HTTPS is on and
# working": TLS can be enabled in the env file against a certificate that has
# been deleted, expired, or had its key replaced, and every one of those is a
# panel that is refusing connections right now.
acme_state() {
    ACME_ST_ON=0; ACME_ST_CERT=""; ACME_ST_KEY=""; ACME_ST_NAMES=""
    ACME_ST_ISSUER=""; ACME_ST_DAYS=""; ACME_ST_LINEAGE=""; ACME_ST_ERR=""

    [[ "$(panel_env_get AWG_PANEL_TLS)" == "1" ]] && ACME_ST_ON=1
    ACME_ST_CERT=$(panel_env_get AWG_PANEL_TLS_CERT)
    ACME_ST_KEY=$(panel_env_get AWG_PANEL_TLS_KEY)
    ACME_ST_LINEAGE=$(acme_lineage_of "$ACME_ST_CERT")

    (( ACME_ST_ON )) || return 0
    [[ -n "$ACME_ST_CERT" ]] || { ACME_ST_ERR=$(t "no certificate path is set" \
                                                  "путь к сертификату не задан"); return 0; }
    [[ -f "$ACME_ST_CERT" ]] || { ACME_ST_ERR=$(t "the certificate file is gone" \
                                                  "файл сертификата исчез"); return 0; }
    [[ -n "$ACME_ST_KEY"  ]] || { ACME_ST_ERR=$(t "no private key path is set" \
                                                  "путь к закрытому ключу не задан"); return 0; }
    [[ -f "$ACME_ST_KEY"  ]] || { ACME_ST_ERR=$(t "the private key file is gone" \
                                                  "файл закрытого ключа исчез"); return 0; }
    acme_cert_readable "$ACME_ST_CERT" || { ACME_ST_ERR=$(t "the certificate cannot be read" \
                                                            "сертификат не читается"); return 0; }

    ACME_ST_NAMES=$(acme_cert_names "$ACME_ST_CERT")
    ACME_ST_ISSUER=$(acme_cert_issuer "$ACME_ST_CERT")
    ACME_ST_DAYS=$(acme_cert_days "$ACME_ST_CERT")

    acme_cert_expired "$ACME_ST_CERT" && { ACME_ST_ERR=$(t "it expired on $(acme_cert_until "$ACME_ST_CERT")" \
                                                           "истёк $(acme_cert_until "$ACME_ST_CERT")"); return 0; }
    acme_pair_ok "$ACME_ST_CERT" "$ACME_ST_KEY" || { ACME_ST_ERR=$(t "the key does not match the certificate" \
                                                                     "ключ не подходит к сертификату"); return 0; }
    return 0
}

# One line short enough for a menu header. Calls acme_state itself, so a caller
# that only wants the line does not have to know about the variables.
acme_state_line() {
    acme_state
    if (( ! ACME_ST_ON )); then
        printf '%s' "$(t "plain HTTP - no certificate" "обычный HTTP — сертификата нет")"
        return 0
    fi
    [[ -n "$ACME_ST_ERR" ]] && { printf '%s' "$(t "HTTPS BROKEN - ${ACME_ST_ERR}" \
                                                  "HTTPS СЛОМАН — ${ACME_ST_ERR}")"; return 0; }
    printf '%s' "$(t "HTTPS  $(acme_cert_names_brief "$ACME_ST_CERT" 44)  ($(acme_cert_left "$ACME_ST_CERT") left)" \
                     "HTTPS  $(acme_cert_names_brief "$ACME_ST_CERT" 44)  (осталось $(acme_cert_left "$ACME_ST_CERT"))")"
}

# ------------------------------------------------------------- installing

# The distribution's certbot: signed, cached, and enough for a name. Only ever
# reached as a fallback now, when PyPI could not be got at and a domain
# certificate is what was asked for.
acme_install_certbot() {
    echo "  installing the packaged certbot"
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y certbot >/dev/null 2>&1
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y certbot >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
        yum install -y certbot >/dev/null 2>&1
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --noconfirm certbot >/dev/null 2>&1
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache certbot >/dev/null 2>&1
    else
        return 1
    fi
    acme_pick_certbot
    command -v "$ACME_CERTBOT" >/dev/null 2>&1
}

# certbot from PyPI, in a virtualenv of its own.
#
# This is the certbot this file installs, whichever kind of certificate is
# being asked for. It is current, so it covers a name and an address alike, and
# it is certbot's own answer to the same problem - their instructions build
# exactly this virtualenv. The packaged certbot is left alone, nothing under
# /usr/bin is written, and an admin who wants rid of this deletes
# /usr/local/bin/certbot and /opt/certbot.
#
# certbot 5 wants Python 3.10, which is the panel's floor too, so a machine
# that got this far has one. On anything older pip quietly resolves to some
# ancient certbot rather than failing, which is why the caller re-reads the
# version afterwards instead of trusting a zero exit here.
acme_install_certbot_venv() {
    local -a apt=(apt-get -o DPkg::Lock::Timeout=300)
    echo "  installing certbot from PyPI into ${ACME_VENV} (a minute or so)"
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        if command -v apt-get >/dev/null 2>&1; then
            # Once against the index that is there, then again after refreshing
            # it: a box that has not run apt update since its image was built
            # has package lists too old to find anything, and that is the
            # common state of a VPS an hour into its life.
            DEBIAN_FRONTEND=noninteractive "${apt[@]}" install -y -qq python3-venv >/dev/null 2>&1 \
                || { "${apt[@]}" update -qq >/dev/null 2>&1
                     DEBIAN_FRONTEND=noninteractive "${apt[@]}" install -y -qq python3-venv >/dev/null 2>&1; }
        fi
        python3 -c 'import venv, ensurepip' 2>/dev/null || {
            warn "$(t "python3 here cannot build a virtualenv (python3-venv is missing)" \
                       "python3 здесь не умеет собрать virtualenv (нет пакета python3-venv)")"
            return 1
        }
    fi
    [[ -x "${ACME_VENV}/bin/python" ]] || python3 -m venv "$ACME_VENV" >/dev/null 2>&1 || {
        warn "$(t "could not create the virtualenv at ${ACME_VENV}" \
                  "не удалось создать virtualenv в ${ACME_VENV}")"
        return 1
    }
    "${ACME_VENV}/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "${ACME_VENV}/bin/pip" install --quiet --upgrade certbot >/dev/null 2>&1 || {
        warn "$(t "pip could not install certbot - no route to PyPI, most likely" \
                  "pip не смог установить certbot — скорее всего, нет доступа к PyPI")"
        return 1
    }
    [[ -x "${ACME_VENV}/bin/certbot" ]] || {
        warn "$(t "pip installed no certbot" "pip не установил certbot")"; return 1; }
    ACME_CERTBOT="${ACME_VENV}/bin/certbot"
    # A name on PATH as well, so `certbot` at a prompt is the certbot that
    # holds the panel's lineage. Only where the spot is free or already a
    # symlink: a real file there belongs to whoever put it there, and the
    # absolute path above is what this file uses either way.
    if [[ ! -e /usr/local/bin/certbot || -L /usr/local/bin/certbot ]]; then
        ln -sfn "$ACME_CERTBOT" /usr/local/bin/certbot 2>/dev/null || true
        hash -r 2>/dev/null || true
    fi
    echo "  certbot $(acme_certbot_version) installed"
    return 0
}

# The certbot this needs, fetched if what is here cannot do it. $1 is the
# version the chosen path takes.
#
# Two rules, and between them they answer the question of how many certbots end
# up on the box. What is already installed is used untouched when it is new
# enough - a Debian box holding certbot 2.x gets no virtualenv for a domain
# certificate, and nothing is downloaded. When something does have to be
# installed, it is the current one from PyPI, which covers a name and an
# address both.
#
# That second rule is the correction. The arrangement before it apt-installed
# an old certbot for a name, and then, when the same admin came back for an
# address certificate, found that certbot too old and installed a second one
# beside it: two certbots on one machine, with the question of which holds the
# panel's lineage settled by whichever ran last, and the renewal timer for the
# lineage pointing at whichever the installer happened to be holding. Installing
# the new one the first time removes the second install and the ambiguity with
# it.
acme_ensure_certbot() {
    local need="$1" ver
    acme_pick_certbot
    ver=$(acme_certbot_version)
    acme_version_ge "$ver" "$need" && return 0

    acme_say ""
    if [[ -z "$ver" ]]; then
        acme_say "$(t "certbot is not installed here; it is what issues the certificate." \
                      "certbot здесь не установлен, а сертификат выпускает именно он.")"
    else
        acme_say "$(t "${Y}certbot ${ver} is too old for this${N} - it takes ${B}${need}${N} or newer." \
                      "${Y}certbot ${ver} для этого слишком стар${N} — нужен ${B}${need}${N} или новее.")"
    fi
    acme_say "$(t "It goes into a virtualenv at ${B}${ACME_VENV}${N}, the way certbot's own" \
                  "Он ставится в virtualenv в ${B}${ACME_VENV}${N}, как советует собственная")"
    acme_say "$(t "instructions install it, with a renewal timer beside it." \
                  "инструкция certbot, и рядом с ним таймер продления.")"

    acme_ask_yn "$(t "Install certbot now?" "Установить certbot сейчас?")" || {
        acme_say ""
        acme_say "$(t "Nothing installed. ${B}awg-menu -> Web panel -> Certificates${N} asks again." \
                      "Ничего не установлено. ${B}awg-menu -> Веб-панель -> Сертификаты${N} спросит снова.")"
        ACME_FLOW_ERR=""
        return 2
    }

    if acme_install_certbot_venv; then
        ver=$(acme_certbot_version)
        acme_version_ge "$ver" "$need" && return 0
    fi

    # PyPI out of reach. A name can still be issued by whatever the
    # distribution packages; an address cannot, because no distribution
    # packages one new enough.
    if acme_version_ge "$ACME_MIN_DOMAIN" "$need" && acme_install_certbot; then
        ver=$(acme_certbot_version)
        if acme_version_ge "$ver" "$need"; then
            acme_say "$(t "PyPI was out of reach; the packaged certbot ${ver} will do for a name." \
                          "PyPI недоступен; системного пакета certbot ${ver} достаточно для работы с доменным именем.")"
            return 0
        fi
    fi

    warn "$(t "the newest certbot available here is ${ver:-none}, short of ${need}" \
              "наиболее свежая доступная версия certbot: ${ver:-нет}, требуется ${need}")"
    acme_say "$(t "A domain certificate needs only ${B}${ACME_MIN_DOMAIN}${N}, so that route may still" \
                  "Для сертификата на домен достаточно ${B}${ACME_MIN_DOMAIN}${N}, этот вариант может сработать.")"
    acme_say "$(t "work. ${B}awg-menu -> Web panel -> Certificates${N} re-runs this." \
                  "${B}awg-menu -> Веб-панель -> Сертификаты${N} проходит это заново.")"
    if [[ "$need" == "$ACME_MIN_DOMAIN" ]]; then
        ACME_FLOW_ERR="$(t "The newest certbot available here is ${ver:-none}, and nothing older than ${need} can issue a certificate at all." \
                           "Самый новый доступный здесь certbot — ${ver:-нет}, а ничто старше ${need} сертификат выпустить вообще не может.")"
    else
        ACME_FLOW_ERR="$(t "The newest certbot available here is ${ver:-none}, short of the ${need} an address certificate needs. A domain certificate needs only ${ACME_MIN_DOMAIN}, so that route may still work." \
                           "Самый новый доступный здесь certbot — ${ver:-нет}, а сертификату на адрес нужен ${need}. Сертификату на домен достаточно ${ACME_MIN_DOMAIN}, так что этот путь ещё может сработать.")"
    fi
    return 1
}

# Is anything already scheduled to renew certbot certificates? Debian's package
# ships a timer and a cron entry, snap ships a timer of its own, and Alpine and
# Arch ship neither - so "certbot is installed" says nothing about whether
# renewals actually happen.
acme_renewal_scheduled() {
    systemctl list-timers --all --no-legend 2>/dev/null | grep -q certbot && return 0
    [[ -f /etc/cron.d/certbot || -f /etc/cron.daily/certbot ]] && return 0
    return 1
}

# The timer that runs the renewals.
#
# A virtualenv from PyPI brings no schedule with it - upstream leaves the cron
# entry to the reader - and a certificate nothing renews is precisely the
# failure this file exists to prevent. On the address certificate that is a
# panel serving an expired certificate inside a week; the deploy hook below
# would never even be reached.
#
# Written when this file installed the certbot in use, because the packaged
# timer runs the packaged certbot, and that is the one that cannot renew an
# address lineage: it reads the SANs off the certificate, keeps the names,
# drops the addresses, and renews for nothing at all. Written too when nothing
# on the machine is scheduled to renew anything, which is how certbot arrives
# on Alpine and Arch.
#
# Four checks a day, jittered. certbot renews when the ACME server's renewal
# information says it is time - every couple of days on a 160-hour certificate
# - and a check with nothing to do costs a second.
acme_write_timer() {
    local bin
    command -v systemctl >/dev/null 2>&1 || return 0
    bin=$(command -v "$ACME_CERTBOT" 2>/dev/null) || return 0
    [[ -n "$bin" ]] || return 0
    if [[ "$ACME_CERTBOT" != "${ACME_VENV}/bin/certbot" ]] && acme_renewal_scheduled; then
        return 0
    fi
    mkdir -p "$ACME_UNIT_DIR" || return 1
    cat > "${ACME_UNIT_DIR}/${ACME_TIMER}.service" <<UNIT || return 1
[Unit]
Description=Renew the certificate AWG Panel serves
Documentation=https://certbot.eff.org/

[Service]
Type=oneshot
ExecStart=${bin} -q renew
UNIT
    cat > "${ACME_UNIT_DIR}/${ACME_TIMER}.timer" <<UNIT || return 1
[Unit]
Description=Check four times a day whether the panel's certificate can be renewed

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now "${ACME_TIMER}.timer" >/dev/null 2>&1 || return 1
    echo "  ${ACME_TIMER}.timer renews it, four checks a day"
    return 0
}

# The hook that makes a renewal reach the running panel.
#
# gunicorn reads the certificate once, when it starts. Certbot replacing the
# file underneath it changes nothing until the service restarts, so without
# this the panel serves the certificate it started with until something else
# happens to restart it - which, on a six-day certificate renewed every couple
# of days, means serving an expired one within the week.
#
# It lives in the directory certbot runs for every lineage, so it has to check
# which lineage renewed: an unrelated certificate on the same box would
# otherwise bounce the panel every time it came up for renewal. The comparison
# is against the panel's own configured path rather than a name written in
# here, so an admin who repoints the panel at a different certificate gets a
# hook that follows them without being rewritten.
acme_write_hook() {
    mkdir -p "$ACME_HOOK_DIR" || return 1
    cat > "$ACME_HOOK" <<'HOOK'
#!/bin/bash
# Restart AWG Panel when the certificate it is actually using has
# been renewed. Installed by lib/acme.sh; safe to delete if the panel is gone.
#
# RENEWED_LINEAGE is set by certbot to the live/ directory it just rewrote.
# Every deploy hook runs for every lineage, so a panel that did not renew must
# not be restarted - a restart drops every open session on the dashboard.
set -u
env_file=/etc/awg-panel.env
[[ -f "$env_file" && -n "${RENEWED_LINEAGE:-}" ]] || exit 0
cert=$(sed -n 's/^AWG_PANEL_TLS_CERT=//p' "$env_file" | tr -d '"' | tail -1)
[[ -n "$cert" ]] || exit 0
# dirname never leaves a trailing slash, so neither may this. Certbot does not
# add one today, and a version that did would turn this hook off without ever
# saying anything - the panel would simply go on serving the old certificate.
[[ "$(dirname "$cert")" == "${RENEWED_LINEAGE%/}" ]] || exit 0
[[ -x /usr/local/bin/awg-panel ]] || exit 0
/usr/local/bin/awg-panel restart-deferred >/dev/null 2>&1 || true
HOOK
    chmod 755 "$ACME_HOOK"
}

# Does anything here renew by binding port 80 itself? Certbot stores the
# authenticator per lineage, so a certificate imported from a webroot or DNS
# setup renews without touching the port and has nothing to answer for here.
acme_standalone_renewal() {
    grep -qs '^authenticator = standalone' "${ACME_LE}"/renewal/*.conf
}

# What renews what, for the screen that asks. Three separate things have to be
# true for a renewal to reach the panel - a certbot that can reissue the
# lineage, a schedule that runs it, and the deploy hook that restarts gunicorn
# afterwards - and each of them fails silently on its own.
acme_renewal_report() {
    local bin ver line l p p80 any=0 w
    # One column per language: the Russian labels here are the longer ones, and
    # a column wide enough for them leaves the English table looking sparse.
    w=$(t 14 20)
    acme_pick_certbot
    bin=$(command -v "$ACME_CERTBOT" 2>/dev/null)
    ver=$(acme_certbot_version)
    printf '  %s%s\n' "$(pad "certbot" "$w")" "${ver:-$(t "not installed" "не установлен")}"
    printf '  %s%s\n' "$(pad "$(t "binary" "программа")" "$w")" "${bin:-$(t "none" "нет")}"
    if [[ -x "$ACME_HOOK" ]]; then
        printf '  %s%s\n' "$(pad "$(t "deploy hook" "хук развёртывания")" "$w")" "$ACME_HOOK"
    else
        printf '  %s%s\n' "$(pad "$(t "deploy hook" "хук развёртывания")" "$w")" \
            "$(t "MISSING - a renewal would not reach the panel" \
                 "ОТСУТСТВУЕТ — продление не дойдёт до панели")"
    fi
    # The fourth thing that has to be true, and the one nothing else reports:
    # a standalone renewal binds port 80 at three in the morning, and a
    # renewal that could not bind it says so in a log nobody reads until the
    # certificate has already expired.
    if acme_standalone_renewal; then
        if port_busy tcp 80; then
            p80=$(port_holder tcp 80)
            printf '  %s%s\n' "$(pad "$(t "port 80" "порт 80")" "$w")" \
                "$(t "HELD by ${p80:-something} - renewals will fail" \
                     "ЗАНЯТ (${p80:-что-то}) — продление будет падать")"
        else
            printf '  %s%s\n' "$(pad "$(t "port 80" "порт 80")" "$w")" \
                "$(t "free - renewals can bind it" "свободен — продление сможет его занять")"
        fi
    fi

    printf '\n  %s\n' "$(t "schedules" "расписания")"
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        any=1
        printf '    %s\n' "$line"
    done < <(systemctl list-timers --all --no-legend 2>/dev/null | grep -i certbot)
    [[ -f /etc/cron.d/certbot ]]    && { any=1; printf '    /etc/cron.d/certbot\n'; }
    [[ -f /etc/cron.daily/certbot ]] && { any=1; printf '    /etc/cron.daily/certbot\n'; }
    (( any )) || printf '    %s\n' "$(t "none - nothing on this machine renews anything" \
                                         "нет — на этой машине ничто ничего не продлевает")"

    printf '\n  %s\n' "$(t "certificates certbot holds" "сертификаты, которые держит certbot")"
    any=0
    while IFS= read -r l; do
        [[ -n "$l" ]] || continue
        any=1
        p="${ACME_LE}/live/${l}/fullchain.pem"
        printf '    %-18s %-34s %s\n' "$l" "$(acme_cert_names_brief "$p")" "$(acme_cert_left "$p")"
    done < <(acme_lineages)
    (( any )) || printf '    %s\n' "$(t "none" "нет")"
    return 0
}

# --------------------------------------------------------------- applying

# Adopt a certificate and private key so they are accessible to the panel
# service inside its systemd sandbox.
#
# The panel's systemd units run with ProtectHome=yes and PrivateTmp=yes, so
# the service is handed an empty /root, /home, /run/user and a private /tmp.
# awg-menu runs outside that namespace and can read files there, but passing
# those paths directly to gunicorn makes the service fail to start. This
# function turns a pair the service cannot reach into one it can, without
# widening the sandbox.
#
# On success it leaves the pair to use in ACME_ADOPT_CERT and ACME_ADOPT_KEY,
# prints the same two paths one per line, and returns 0. On failure it prints
# nothing, says why in ACME_ADOPT_ERR, and returns 1. ACME_ADOPT_HOW records
# which of the three routes below it took, because only one of them leaves
# renewals reaching the panel and the caller has to be able to say so.
acme_adopt() {
    local cert="${1:-}" key="${2:-}"
    local dst_cert dst_key cert_base cert_dir home domain
    local -a ecc_flag=()

    ACME_ADOPT_ERR=""
    ACME_ADOPT_HOW=""
    # The pair is handed back in variables as well as on stdout, and the
    # variables are the ones a caller should read. A caller that wants both the
    # paths and ACME_ADOPT_HOW cannot have them from `$(acme_adopt ...)`: the
    # command substitution is a subshell, so every variable set in here is
    # discarded when it closes, and the caller silently gets an empty
    # ACME_ADOPT_HOW and an empty ACME_ADOPT_ERR for a failure it can see
    # happened. Reading these instead keeps the whole answer in one shell.
    ACME_ADOPT_CERT=""
    ACME_ADOPT_KEY=""

    # An unmasked pair is already reachable by the service, so nothing needs
    # to be copied. A certbot lineage under /etc/letsencrypt/live/ stays live
    # where it is, which the existing deploy hook depends on.
    if ! acme_path_masked "$cert" && ! acme_path_masked "$key"; then
        ACME_ADOPT_HOW=direct
        ACME_ADOPT_CERT="$cert"
        ACME_ADOPT_KEY="$key"
        printf '%s\n%s\n' "$cert" "$key"
        return 0
    fi

    dst_cert="${ACME_TLS_DIR}/panel.pem"
    dst_key="${ACME_TLS_DIR}/panel.key"

    mkdir -p "$ACME_TLS_DIR" 2>/dev/null || {
        ACME_ADOPT_ERR=$(t "could not create ${ACME_TLS_DIR}" \
                           "не удалось создать ${ACME_TLS_DIR}")
        return 1
    }
    chmod 0755 "$ACME_TLS_DIR" 2>/dev/null || true

    # Try the acme.sh route first. A simple file copy that renewal never
    # refreshes leaves the panel silently serving an expired certificate in
    # sixty days. acme.sh's --install-cert with a reloadcmd is its own answer
    # to that problem: it installs the files into ACME_TLS_DIR, rewrites the
    # copy on every renewal, and restarts the panel - the exact counterpart of
    # the certbot deploy hook acme_write_hook installs.
    cert_base=$(basename -- "$cert")
    cert_dir=$(dirname -- "$cert")
    home=$(dirname -- "$cert_dir")
    if [[ "$cert_base" == "fullchain.cer" && -x "${home}/acme.sh" ]]; then
        domain=$(basename -- "$cert_dir")
        if [[ "$domain" == *_ecc ]]; then
            domain="${domain%_ecc}"
            ecc_flag=(--ecc)
        fi
        if "${home}/acme.sh" --install-cert -d "$domain" "${ecc_flag[@]}" \
                --home "$home" \
                --fullchain-file "$dst_cert" \
                --key-file "$dst_key" \
                --reloadcmd "systemctl restart awg-panel-web" >/dev/null 2>&1 \
            && [[ -s "$dst_cert" && -s "$dst_key" ]]; then
            ACME_ADOPT_HOW=acme.sh
        fi
    fi

    # The copy route. When acme.sh is not present or failed to install the
    # certificate (for instance, due to a stale account or untracked domain),
    # fall back to making a plain copy so the admin is not left with nothing.
    if [[ "$ACME_ADOPT_HOW" != "acme.sh" ]]; then
        ACME_ADOPT_HOW=copy
        install -m 0644 -- "$cert" "$dst_cert" >/dev/null 2>&1 || {
            ACME_ADOPT_ERR=$(t "could not copy ${cert} to ${dst_cert}" \
                               "не удалось скопировать ${cert} в ${dst_cert}")
            return 1
        }
        install -m 0600 -- "$key" "$dst_key" >/dev/null 2>&1 || {
            ACME_ADOPT_ERR=$(t "could not copy ${key} to ${dst_key}" \
                               "не удалось скопировать ${key} в ${dst_key}")
            return 1
        }
    fi

    # The mode is set here rather than only on the copy route, because the
    # acme.sh route did not write these files and cannot be relied on to have
    # written them tightly. A private key left world-readable under /etc/ssl
    # is the failure this whole function would otherwise have introduced: the
    # original under /root was unreachable, but it was also unreadable by
    # anything but root, and the copy has to stay that way.
    chmod 0644 "$dst_cert" 2>/dev/null || true
    chmod 0600 "$dst_key"  2>/dev/null || true

    # Verify destination files before printing paths. Both destination files
    # must be non-empty, the certificate must parse as PEM, and the key must
    # match the certificate so the caller can trust what it hands to
    # acme_apply.
    if [[ ! -s "$dst_cert" || ! -s "$dst_key" ]]; then
        ACME_ADOPT_ERR=$(t "adopted certificate or key is missing or empty" \
                           "принятый сертификат или ключ отсутствует или пуст")
        return 1
    fi
    if ! openssl x509 -in "$dst_cert" -noout >/dev/null 2>&1; then
        ACME_ADOPT_ERR=$(t "${dst_cert} is not a PEM certificate." \
                           "${dst_cert} — не сертификат в формате PEM.")
        return 1
    fi
    if ! acme_pair_ok "$dst_cert" "$dst_key"; then
        ACME_ADOPT_ERR=$(t "That key does not belong to that certificate." \
                           "Этот ключ не от этого сертификата.")
        return 1
    fi

    ACME_ADOPT_CERT="$dst_cert"
    ACME_ADOPT_KEY="$dst_key"
    printf '%s\n%s\n' "$dst_cert" "$dst_key"
    return 0
}

# What the panel said for itself, since a moment the caller noted beforehand.
#
# The failures here used to end by naming `journalctl -u awg-panel-web -n 30`
# and leaving the admin to run it. By the time they did, the rollback had
# already restarted the service, and that restart's own migrate step and
# gunicorn boot - on top of the Restart=on-failure retries that happen while
# the failure is still being watched for - had pushed the real error well past
# the last thirty lines. The command answered with a perfectly healthy start,
# which is how a certificate gets refused and the journal appears to say
# nothing about it.
#
# So the lines are read here instead, while they are still the last thing in
# the journal, and printed with the failure that produced them. Never returns
# non-zero: every caller runs under errexit, and a machine with no journalctl
# has no answer to give rather than a reason to stop.
acme_journal_since() {
    local stamp="${1:-}"
    [[ -n "$stamp" ]] || return 0
    command -v journalctl >/dev/null 2>&1 || return 0
    journalctl -u awg-panel-web --since "$stamp" --no-pager -o cat 2>/dev/null \
        | tail -40
    return 0
}

# And the block that reports one. No colour anywhere in it: this is read
# through whiptail's textbox as often as from a terminal, and whiptail draws an
# escape sequence rather than obeying it.
acme_say_journal() {
    local log="${1:-}"
    printf '\n'
    if [[ -n "$log" ]]; then
        printf '%s\n' "$(t "--- what the panel said ---" "--- что сказала панель ---")"
        printf '%s\n' "$log"
    else
        printf '  %s\n' "$(t "The journal had nothing to say. journalctl -u awg-panel-web -n 50 is the place to look." \
                             "В журнале ничего нет. Смотреть здесь: journalctl -u awg-panel-web -n 50.")"
    fi
    return 0
}

# Point the panel at a certificate and key, restart it, and put it back the way
# it was if it does not come up.
#
# The rollback is the whole reason this is a function. gunicorn refuses to
# start when AWG_PANEL_TLS is 1 and either path is unusable, which is the right
# refusal - serving cleartext to somebody who asked for TLS would be worse -
# but it turns a bad certificate into a panel that is simply gone, with the
# admin's only route back in being the SSH session they may not have open. So
# the switch is thrown, the service is watched, and a service that is not
# running a few seconds later gets its old settings back.
acme_apply() {
    local cert="$1" key="$2" prev_tls prev_cert prev_key env_unwritable i stamp log
    local -a masked=()
    # A path the service cannot open is a bad argument in exactly the way an
    # empty file is, and it fails the same way: gunicorn exits, the rollback
    # below puts the panel back, and the admin reads a journal dump instead of
    # a sentence saying which path was wrong. Refused here rather than in each
    # caller, so no future one can reintroduce it - acme_adopt is what turns
    # such a pair into one this will accept.
    #
    # Ahead of the existence check below, because this function runs as root
    # outside the service's namespace: /root/.acme.sh/…/fullchain.cer is a file
    # it can stat perfectly well, and "missing" is the one thing that path is
    # not. Whether the file is there is a different question from whether the
    # service could ever read it, and answering the second one first is what
    # makes the message match the fault.
    if acme_path_masked "$cert"; then
        masked+=("$cert")
    fi
    if acme_path_masked "$key"; then
        masked+=("$key")
    fi
    if (( ${#masked[@]} )); then
        warn "$(t "the panel's service cannot open ${masked[*]} - it runs with ProtectHome=yes and is handed an empty /root and /home" \
                  "служба панели не может открыть ${masked[*]}: она работает с ProtectHome=yes и получает пустые /root и /home")"
        return 1
    fi

    [[ -s "$cert" && -s "$key" ]] || {
        warn "$(t "certificate or key missing at ${cert}" \
                  "нет сертификата или ключа по пути ${cert}")"; return 1; }

    prev_tls=$(panel_env_get AWG_PANEL_TLS)
    prev_cert=$(panel_env_get AWG_PANEL_TLS_CERT)
    prev_key=$(panel_env_get AWG_PANEL_TLS_KEY)

    # The same sentence three times, so it is written once and read from a
    # variable: three copies of one message are three chances for two of them
    # to be translated and the third not.
    env_unwritable="$(t "could not write ${PANEL_ENV}" "не удалось записать ${PANEL_ENV}")"
    panel_env_set AWG_PANEL_TLS_CERT "$cert" || { warn "$env_unwritable"; return 1; }
    panel_env_set AWG_PANEL_TLS_KEY  "$key"  || { warn "$env_unwritable"; return 1; }
    panel_env_set AWG_PANEL_TLS      1       || { warn "$env_unwritable"; return 1; }

    # Noted before the restart rather than after the failure, so that what gets
    # read back is this attempt and not the tail of whatever the service was
    # doing beforehand.
    stamp=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart awg-panel-web >/dev/null 2>&1 || true
    # Up to five seconds. systemd reports a unit active the moment it forks, so
    # the loop is looking for the failure that follows - gunicorn reading the
    # certificate, disliking it and exiting - rather than for the start itself.
    for i in 1 2 3 4 5; do
        sleep 1
        systemctl is-active --quiet awg-panel-web || break
    done
    if systemctl is-active --quiet awg-panel-web; then
        # The hook only means anything for a certbot lineage: it fires from
        # certbot's own renewal, and a certificate from anywhere else is
        # renewed by whatever put it there.
        if [[ "$cert" == "${ACME_LE}/"* ]]; then
            acme_write_hook || warn "$(t "the renewal hook did not go in; a renewal will not reach the panel until it restarts" \
                                         "хук продления не встал; продлённый сертификат дойдёт до панели только после её перезапуска")"
        fi
        return 0
    fi

    # Read now, before the rollback below restarts the service: that restart is
    # exactly what buries the reason under a healthy start.
    log=$(acme_journal_since "$stamp")

    warn "$(t "the panel did not come up with that certificate - putting it back" \
              "панель с этим сертификатом не поднялась — возвращаем как было")"
    panel_env_set AWG_PANEL_TLS      "${prev_tls:-0}"
    panel_env_set AWG_PANEL_TLS_CERT "$prev_cert"
    panel_env_set AWG_PANEL_TLS_KEY  "$prev_key"
    systemctl restart awg-panel-web >/dev/null 2>&1 || true
    acme_say_journal "$log"
    return 1
}

# Back to plain HTTP.
#
# The two paths stay in the env file. Turning TLS off is not the same as
# forgetting which certificate was in use - an admin putting the panel behind a
# reverse proxy for an afternoon should not have to retype them - and the panel
# ignores both while AWG_PANEL_TLS is 0.
acme_disable() {
    local stamp
    panel_env_set AWG_PANEL_TLS 0 || {
        warn "$(t "could not write ${PANEL_ENV}" "не удалось записать ${PANEL_ENV}")"; return 1; }
    stamp=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart awg-panel-web >/dev/null 2>&1 || true
    sleep 1
    systemctl is-active --quiet awg-panel-web || {
        warn "$(t "the panel did not come back up on plain HTTP" \
                  "панель не поднялась обратно на обычном HTTP")"
        # Nothing restarts the service again on this path, so the lines are
        # still the last thing in the journal when they are read.
        acme_say_journal "$(acme_journal_since "$stamp")"
        return 1
    }
    return 0
}

# ----------------------------------------------------------------- issuing

# Port 80 has to be reachable from the internet for http-01, at issuance and
# again at every renewal, so the rule is left in place rather than opened and
# closed around each run. What that exposes is small: standalone binds the port
# only for the seconds a renewal takes, so between renewals a scan of 80 finds
# nothing listening - the same answer it would get from a port that was never
# opened. A renewal that has to ask the admin to open a firewall first is a
# renewal that does not happen.
acme_open_port_80() {
    fw_allow 80 tcp
    [[ -n "$FW_HANDLED" ]] && echo "  ${FW_HANDLED}: allowed 80/tcp (needed for renewals too)"
    return 0
}

# Whether a port is held, and by what, is port_busy/port_holder in
# lib/common.sh - the installer asks the same of the panel and VPN ports.

# Is port 80 free for standalone to bind, and if not, why that matters here.
#
# Checked before the conversation starts and again just before certbot runs,
# because minutes pass in between - a certbot install, a domain typed, an
# email - and the port is only actually needed at the end of them.
#
# The panel itself is the case worth naming. An admin who put the panel on 80
# reads "stop whatever is holding it" as advice to stop the thing they are
# configuring, and stopping it would only free the port until the next
# restart; the fix is to move the panel, which awg-menu can do.
#
# Leaves the sentence in ACME_FLOW_ERR for the screen that reports this after
# the terminal is gone, and says nothing on its own when the port is free.
acme_port80_clear() {
    local holder
    port_busy tcp 80 || return 0
    if [[ "$(panel_env_get AWG_PANEL_PORT 2>/dev/null)" == 80 ]]; then
        ACME_FLOW_ERR="$(t "The panel is on port 80, which is the port certbot has to bind. Move the panel to another port, or import a certificate you already have." \
                           "Панель занимает порт 80, а именно его должен слушать certbot. Перенесите панель на другой порт или импортируйте сертификат, который у вас уже есть.")"
        acme_say ""
        acme_say "$(t "${Y}The panel is on port 80${N}, which is the port certbot has to bind." \
                      "${Y}Панель занимает порт 80${N}, а именно его должен слушать certbot.")"
        acme_say "$(t "Move the panel to another port, or import a certificate you already have." \
                      "Перенесите панель на другой порт или импортируйте свой сертификат.")"
    else
        holder=$(port_holder tcp 80)
        ACME_FLOW_ERR="$(t "Port 80 is held by ${holder:-something else}, and certbot has to bind it. Stop it and come back, or import a certificate you already have." \
                           "Порт 80 занят: ${holder:-что-то другое}, а certbot должен его слушать. Остановите это и вернитесь, или импортируйте сертификат, который у вас уже есть.")"
        acme_say ""
        acme_say "$(t "${Y}Port 80 is held by ${holder:-something else}${N}, and certbot has to bind it." \
                      "${Y}Порт 80 занят: ${holder:-что-то другое}${N}, а certbot должен его слушать.")"
        acme_say "$(t "Stop it and come back, or import a certificate you already have." \
                      "Остановите это и вернитесь, или импортируйте свой сертификат.")"
    fi
    return 1
}

# Is this an address Let's Encrypt could be asked about at all?
#
# Not a full parser. Certbot and the ACME server both validate properly, and
# duplicating their rules here would only add a second opinion to disagree with
# them. What this catches is the typo worth catching before a firewall rule is
# added and a network round trip is spent: four octets that are octets, or
# something with the shape of an IPv6 literal.
acme_valid_ip() {
    local ip="$1" octet
    local -a parts=()
    if [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
        IFS=. read -ra parts <<<"$ip"
        for octet in "${parts[@]}"; do
            (( 10#$octet <= 255 )) || return 1
        done
        return 0
    fi
    # Hex groups and colons, with at least one colon and no more than one "::".
    [[ "$ip" == *:* && "$ip" =~ ^[0-9A-Fa-f:]+$ && "$ip" != *:::* ]]
}

acme_valid_domain() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]]
}

# An address that Let's Encrypt will not issue for whatever the network says.
# Caught here because the failure otherwise arrives as an ACME error several
# screens long, and the cause - the endpoint detected for this box is the one
# behind the NAT, not the one in front of it - is not in that error anywhere.
acme_private_ip() {
    local ip="$1"
    [[ "$ip" =~ ^10\. ]] && return 0
    [[ "$ip" =~ ^127\. ]] && return 0
    [[ "$ip" =~ ^192\.168\. ]] && return 0
    [[ "$ip" =~ ^169\.254\. ]] && return 0
    [[ "$ip" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] && return 0
    [[ "${ip,,}" =~ ^(fc|fd|fe80) ]] && return 0
    [[ "$ip" == "::1" ]] && return 0
    return 1
}

# certbot certonly, with the arguments both paths share. --cert-name pins the
# lineage; -n is non-interactive, which matters because this runs inside an
# installer that has its own idea of what the terminal is doing.
#
# --keep-until-expiring is what makes a second run safe. Asked again for the
# name it already holds, certbot wants to know whether to renew early, and
# under -n a question it cannot ask is an error rather than a default - so
# without this, re-running from awg-menu to check the setup would fail on a
# certificate that is perfectly good. It does not stand in the way of a real
# change: a different name is a different set from the one on file, which
# certbot reissues for rather than treating as a renewal it may skip.
acme_certbot() {
    local email="$1"; shift
    local -a args=(certonly -n --agree-tos --cert-name "$ACME_LINEAGE" --keep-until-expiring)
    if [[ -n "$email" ]]; then args+=(--email "$email")
    else                       args+=(--register-unsafely-without-email); fi
    acme_run_certbot "${args[@]}" "$@"
}

acme_issue_domain() {
    local domain="$1" email="$2"
    echo "  asking Let's Encrypt for ${domain}"
    acme_certbot "$email" --standalone -d "$domain"
}

# An address certificate is a different request in three ways, all of them
# required by Let's Encrypt rather than chosen here: the shortlived profile,
# which is the only one addresses are issued under; --ip-address rather than
# -d; and http-01, because dns-01 has nothing to answer for an address.
acme_issue_ip() {
    local ip="$1"
    echo "  asking Let's Encrypt for ${ip}"
    acme_certbot "" --standalone --preferred-profile shortlived --ip-address "$ip"
}

# Reissue one lineage now, rather than when certbot thinks it is due.
#
# Forced, because this is only ever reached by an admin who went looking for
# it: an unforced `certbot renew` on a certificate with weeks left prints "not
# yet due" and exits zero, which reads as a renewal that happened.
# awg-menu is the only caller of this and of acme_delete_lineage below it, and
# what they print lands on one of its screens - so both carry the second
# language like the rest of the menu does. They were English-only for as long
# as the menu was.
acme_renew_now() {
    local name="${1:-$ACME_LINEAGE}" holder
    acme_pick_certbot
    command -v "$ACME_CERTBOT" >/dev/null 2>&1 \
        || { warn "$(t "certbot is not installed here" "certbot здесь не установлен")"; return 1; }
    [[ -d "${ACME_LE}/live/${name}" ]] \
        || { warn "$(t "certbot holds no certificate called ${name}" \
                       "у certbot нет сертификата с именем ${name}")"; return 1; }
    if port_busy tcp 80; then
        holder=$(port_holder tcp 80)
        warn "$(t "port 80 is held by ${holder:-something} - a standalone renewal will fail" \
                  "порт 80 занят (${holder:-что-то}) — автономное продление не пройдёт")"
    fi
    acme_run_certbot renew -n --cert-name "$name" --force-renewal
}

# Delete a lineage: the live symlinks, the archived material and the renewal
# config, so nothing goes on trying to renew it.
#
# certbot delete is asked first because it is the only thing that knows what
# else the lineage owns. The direct removal underneath is for a machine whose
# certbot has been uninstalled out from under its own /etc/letsencrypt, which
# is common enough - a snap removed, a virtualenv deleted - and leaves files
# that nothing else will ever clean up.
acme_delete_lineage() {
    local name="${1:-}"
    [[ -n "$name" && "$name" != */* && "$name" != .* && "$name" != *..* ]] \
        || { warn "$(t "not a certificate name: ${name}" \
                       "это не имя сертификата: ${name}")"; return 1; }
    [[ -d "${ACME_LE}/live/${name}" ]] \
        || { warn "$(t "no certificate called ${name}" "сертификата с именем ${name} нет")"; return 1; }

    acme_pick_certbot
    if command -v "$ACME_CERTBOT" >/dev/null 2>&1; then
        if "$ACME_CERTBOT" delete -n --cert-name "$name" 2>&1; then
            return 0
        fi
        warn "$(t "certbot could not delete ${name}; removing its files directly" \
                  "certbot не смог удалить ${name}; убираем его файлы напрямую")"
    fi
    rm -rf -- "${ACME_LE}/live/${name}" "${ACME_LE}/archive/${name}"
    rm -f  -- "${ACME_LE}/renewal/${name}.conf"
    [[ -d "${ACME_LE}/live/${name}" ]] \
        && { warn "$(t "could not remove ${ACME_LE}/live/${name}" \
                       "не удалось удалить ${ACME_LE}/live/${name}")"; return 1; }
    return 0
}

# Take everything this file installed back off the machine: the renewal timer,
# the deploy hook, the certificate it issued, and the virtualenv it built to
# issue that certificate with. For awg-uninstall, which without it leaves a box
# with no panel on it still checking four times a day whether the panel's
# certificate can be renewed, and a hook waiting to restart a service that is
# gone.
#
# The lineage goes only when the caller says so. It is the one part of this
# that cannot simply be made again: reissuing needs the address or the name to
# still point here and the rate limit to still allow it, so an uninstall that
# keeps the configuration for a later reinstall keeps the certificate with it.
#
# The virtualenv is the careful one. /opt/certbot is not this project's path -
# it is the one certbot's own pip instructions give everybody - so it is
# removed only when no lineage is left anywhere for it to renew. A box with
# another certificate on it keeps its certbot, and what is left behind is then
# a virtualenv something else here is still using.
#
# Best-effort throughout, like the rest of this file. Nothing in a teardown is
# worth failing an uninstall over, and the panel these belonged to has already
# been stopped by the time this runs.
acme_remove() {
    local keep_cert=0 removed=0
    [[ "${1:-}" == "--keep-cert" ]] && keep_cert=1

    if [[ -f "${ACME_UNIT_DIR}/${ACME_TIMER}.timer" ]]; then
        systemctl disable --now "${ACME_TIMER}.timer" >/dev/null 2>&1
        rm -f "${ACME_UNIT_DIR}/${ACME_TIMER}.timer" \
              "${ACME_UNIT_DIR}/${ACME_TIMER}.service"
        systemctl daemon-reload >/dev/null 2>&1
        echo "  ${ACME_TIMER}.timer and its service removed"
        removed=1
    fi
    if [[ -e "$ACME_HOOK" ]]; then
        rm -f "$ACME_HOOK"
        # Only if this project put the directory there and nothing else uses
        # it; rmdir on a directory with another hook in it fails and says so,
        # which is why the error goes nowhere.
        rmdir "$ACME_HOOK_DIR" 2>/dev/null
        echo "  renewal deploy hook removed"
        removed=1
    fi
    if (( ! keep_cert )) && [[ -d "${ACME_LE}/live/${ACME_LINEAGE}" ]]; then
        acme_delete_lineage "$ACME_LINEAGE" >/dev/null 2>&1
        echo "  certificate ${ACME_LINEAGE} deleted"
        removed=1
    fi
    # The copy acme_adopt made for a certificate the service could not reach.
    # Only the pair this project writes, and only ever the copy: the original
    # it was taken from belongs to whoever put it there, and deleting an
    # admin's own certificate because the panel once borrowed it would be the
    # one uninstall step that costs something outside this project.
    if [[ -e "${ACME_TLS_DIR}/panel.pem" || -e "${ACME_TLS_DIR}/panel.key" ]]; then
        rm -f "${ACME_TLS_DIR}/panel.pem" "${ACME_TLS_DIR}/panel.key"
        # Only if nothing else was put in beside them, which is why the error
        # goes nowhere - the same reasoning as the hook directory above.
        rmdir "$ACME_TLS_DIR" 2>/dev/null
        echo "  the panel's copy of the certificate removed"
        removed=1
    fi
    if [[ -d "$ACME_VENV" && -z "$(acme_lineages)" ]]; then
        rm -rf "$ACME_VENV"
        echo "  ${ACME_VENV} removed - no certificate left for it to renew"
        removed=1
    fi
    (( removed )) || echo "  nothing installed"
    return 0
}

# ------------------------------------------------------------------- flow

# A descriptor on the controlling terminal, for the questions below.
#
# install.sh closed the one its earlier questions used several steps ago, and
# stdin is a pipe on the documented `curl | bash` install, where a prompt read
# from it would swallow the rest of the script.
#
# The group is what carries the redirection: bash reports a failed exec
# redirection on the stderr it had before the exec ran, so `2>/dev/null` on the
# exec itself still lets "/dev/tty: No such device or address" through - into
# the middle of an installer's output, on every run with no terminal.
ACME_FD=0
acme_tty_open() {
    ACME_FD=0
    [[ -e /dev/tty ]] || return 1
    { exec {ACME_FD}<>/dev/tty; } 2>/dev/null || return 1
    return 0
}

acme_tty_close() {
    [[ "${ACME_FD:-0}" -gt 2 ]] || { ACME_FD=0; return 0; }
    exec {ACME_FD}>&-
    ACME_FD=0
    return 0
}

# Throw away anything already waiting on that terminal.
#
# A keystroke typed before a question was printed is not an answer to it. The
# installer says as much - it promises the rest of the run is unattended, and
# then spends minutes building the panel - so an operator who taps Enter at what
# looks like a stalled build leaves a newline in the terminal's input queue, and
# the next read here takes it. What that looked like was the HTTPS question
# asking itself twice: once answered by the stale newline, which is neither y nor
# n, and once for real. The half that did not show is the worse one. A stale "y"
# is a valid answer, so it was taken as consent - the installer went off to issue
# a certificate nobody had agreed to, and the operator's real answer landed in
# the shell after the install had finished.
#
# -n is what makes this work on a terminal: bash reads with ICANON off, so a
# line typed with no Enter behind it drains as well, rather than sitting there
# to be prefixed onto the answer. The timeout is the whole cost when there is
# nothing waiting, which is the usual case.
acme_tty_drain() {
    # "_" because nothing reads it: read has to put the chunk somewhere, and
    # the whole point is that it goes nowhere the next question can reach.
    local _
    [[ "${ACME_FD:-0}" -gt 2 ]] || return 0
    # No test on the chunk either. See ask_drain in install.sh: an empty one is
    # a line that was only a newline, and stopping on it drained a single Enter
    # and left every other keystroke in the queue to answer the question about
    # to be printed.
    while read -r -t 0.05 -n 4096 -u "$ACME_FD" _; do
        continue
    done
    return 0
}

# Read one line from that terminal, into ACME_REPLY.
#
# Drained before every prompt rather than only the first, because a re-asked
# question is a newly printed one too: anybody answering it typed after reading
# the complaint about the last answer, which is far longer ago than 50ms.
acme_ask() {
    ACME_REPLY=""
    acme_tty_drain
    printf '\n%s%s%s ' "$B" "$1" "$N" >&"$ACME_FD"
    read -r -u "$ACME_FD" ACME_REPLY || return 1
    return 0
}

# y or n, and nothing else counts. No default on purpose: Enter is what
# somebody presses to make a prompt go away, and both answers here are
# consequential enough that the installer should not be able to guess one of
# them from silence.
acme_ask_yn() {
    while :; do
        acme_ask "$1 $(t "(y/n)" "(д/н)")" || return 1
        # The English pair is taken in either language and the Russian one only
        # in Russian. Somebody working in Russian on an English-keyboard server
        # types "y" as often as "д", and there is no reading of "y" at a yes/no
        # question that means anything else; "д" at an English prompt has no
        # such excuse, and taking it would only hide that the language never
        # got set.
        case "${ACME_REPLY,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
        esac
        if is_ru; then
            case "${ACME_REPLY,,}" in
                д|да)  return 0 ;;
                н|нет) return 1 ;;
            esac
        fi
        acme_bad "$(t "please answer y or n" "ответьте д или н")"
    done
}

# This conversation prints flush left, the way info(), warn() and step() in
# lib/common.sh do. It used to sit under seven spaces, which pushed the
# questions and the numbered choices into the middle of the terminal and made
# them read as a boxed-off widget rather than as the installer still talking.
# The two spaces on a list item below are the only indent left, and they mean
# what an indent normally means.
acme_say() { printf '%s\n' "$*" >&"$ACME_FD"; }
acme_bad() { printf '%s!! %s%s\n' "$Y" "$*" "$N" >&"$ACME_FD"; }

# Why certbot said no, and where the rest of it is.
acme_issue_failed() {
    ACME_FLOW_ERR=$(acme_why_failed)
    warn "$(t "certbot could not issue a certificate for ${1}" \
              "certbot не смог выпустить сертификат для ${1}")"
    acme_say ""
    acme_say "${Y}${ACME_FLOW_ERR}${N}"
    [[ -f /var/log/letsencrypt/letsencrypt.log ]] \
        && acme_say "${DIM}$(t "full output: /var/log/letsencrypt/letsencrypt.log" \
                               "полный вывод: /var/log/letsencrypt/letsencrypt.log")${N}"
    return 0
}

# Choose a name or an address, get a certbot that can issue for it, issue it,
# and point the panel at the result. ACME_FD must already be open.
#
# Separate from acme_offer because the two callers arrive with different things
# already said. The installer has just explained what plain HTTP costs and
# asked whether to fix it; awg-menu's certificate screen has the current state
# on the line above the menu and nothing left to argue. Sharing that paragraph
# between them is what made the menu greet an admin who already had HTTPS with
# "the panel is answering over plain HTTP".
#
# $1 is the address detected for this server, offered as the default.
#
# 0 when the panel is on the new certificate, 2 when the admin backed out of
# the conversation, and 1 when something failed - with ACME_FLOW_ERR saying
# what, for the screen that has to report it after the terminal is gone.
acme_issue_flow() {
    local endpoint="${1:-}" want email choice

    ACME_FLOW_ERR=""

    acme_port80_clear || return 1

    acme_say ""
    acme_say "$(t "  ${B}1)${N} Domain name pointing to this server ${DIM}(recommended, 90-day auto-renew)${N}" \
                  "  ${B}1)${N} Доменное имя, направленное на этот сервер ${DIM}(рекомендуется, автопродление на 90 дней)${N}")"
    acme_say "$(t "  ${B}2)${N} Server IP address${endpoint:+ (${endpoint})} ${DIM}(auto-renews every 6 days)${N}" \
                  "  ${B}2)${N} IP-адрес сервера${endpoint:+ (${endpoint})} ${DIM}(автопродление каждые 6 дней)${N}")"
    acme_say "$(t "  ${B}3)${N} Cancel" "  ${B}3)${N} Отмена")"
    acme_say ""
    acme_say "$(t "${DIM}Either needs port 80 free here and reachable from the internet, now" \
                  "${DIM}Для любого из вариантов порт 80/tcp на сервере должен быть свободен и доступен")"
    acme_say "$(t "and at every renewal.${N}" \
                  "из интернета прямо сейчас и при каждом последующем продлении.${N}")"

    while :; do
        acme_ask "$(t "1, 2 or 3:" "1, 2 или 3:")" || return 2
        case "$ACME_REPLY" in
            1) choice=domain; break ;;
            2) choice=ip;     break ;;
            3) return 2 ;;
            *) acme_bad "$(t "1, 2 or 3" "1, 2 или 3")" ;;
        esac
    done

    if [[ "$choice" == domain ]]; then
        acme_ensure_certbot "$ACME_MIN_DOMAIN" || return $?
        while :; do
            acme_ask "$(t "domain name:" "доменное имя:")" || return 2
            want="${ACME_REPLY// /}"
            acme_valid_domain "$want" && break
            acme_bad "$(t "that is not a domain name - vpn.example.com, say" \
                          "некорректное доменное имя (например, vpn.example.com)")"
        done
        acme_say ""
        acme_say "${DIM}$(t "An email gets Let's Encrypt's expiry warnings. Enter to skip." \
                            "На email приходят предупреждения Let's Encrypt об истечении. Enter — пропустить.")${N}"
        acme_ask "$(t "email [none]:" "email [нет]:")" || true
        email="${ACME_REPLY// /}"
        acme_port80_clear || return 1
        acme_open_port_80
        if ! acme_issue_domain "$want" "$email"; then
            acme_issue_failed "$want"
            return 1
        fi
        ACME_RESULT=domain; ACME_CERT_NAME="$want"
    else
        acme_ensure_certbot "$ACME_MIN_IP" || return $?
        while :; do
            acme_ask "$(t "IP address${endpoint:+ [${endpoint}]}:" \
                          "IP-адрес${endpoint:+ [${endpoint}]}:")" || return 2
            want="${ACME_REPLY// /}"
            [[ -z "$want" ]] && want="$endpoint"
            if ! acme_valid_ip "$want"; then
                acme_bad "$(t "that is not an IP address" "это не IP-адрес")"
                continue
            fi
            if acme_private_ip "$want"; then
                acme_bad "$(t "${want} is a private address - Let's Encrypt will not issue for it" \
                              "адрес ${want} является локальным (private) — Let's Encrypt не выдает сертификаты для локальных адресов")"
                continue
            fi
            break
        done
        acme_port80_clear || return 1
        acme_open_port_80
        if ! acme_issue_ip "$want"; then
            acme_issue_failed "$want"
            return 1
        fi
        ACME_RESULT=ip; ACME_CERT_NAME="$want"
    fi

    acme_write_timer || warn "$(t "no renewal timer went in; nothing here will renew this certificate" \
                                  "таймер продления не встал; продлевать этот сертификат здесь будет некому")"

    if acme_apply "${ACME_LIVE}/fullchain.pem" "${ACME_LIVE}/privkey.pem"; then
        return 0
    fi
    ACME_RESULT=""
    ACME_FLOW_ERR="$(t "The certificate was issued, but the panel would not start with it, so it has been put back the way it was. journalctl -u awg-panel-web -n 30 says why." \
                       "Сертификат выпущен, но панель с ним не поднялась, так что всё вернули как было. journalctl -u awg-panel-web -n 30 скажет почему.")"
    return 1
}

# The offer the installer makes at the end of a first install. $1 is the
# address it detected for this server.
#
# Returns 0 when the panel is now on HTTPS and non-zero every other way,
# including when there was nobody to ask - the caller only uses that to decide
# which warning the summary carries.
acme_offer() {
    local endpoint="${1:-}" key existing

    acme_tty_open || return 1

    # Already sorted. The installer does not call this in that case, but
    # anything else that does gets an answer about this machine rather than the
    # speech below.
    acme_state
    if (( ACME_ST_ON )) && [[ -z "$ACME_ST_ERR" ]]; then
        acme_say ""
        acme_say "$(t "${G}The panel already serves HTTPS${N} for ${B}${ACME_ST_NAMES}${N}." \
                      "${G}Веб-панель уже работает по HTTPS${N} для ${B}${ACME_ST_NAMES}${N}.")"
        acme_tty_close
        return 1
    fi

    printf '\n' >&"$ACME_FD"
    acme_say "$(t "The panel is answering over ${Y}plain HTTP${N}. Anything on the network" \
                  "Панель отвечает по ${Y}обычному HTTP${N}. Всё, что стоит на сетевом пути,")"
    acme_say "$(t "path can read your password, your session, and every client config" \
                  "читает ваш пароль, вашу сессию и каждую конфигурацию клиента, которую вы")"
    acme_say "$(t "you download - private keys included. Turning HTTPS on later does" \
                  "скачиваете, приватные ключи включительно. Включённый позже HTTPS ничего")"
    acme_say "$(t "not undo any of that." "из этого не отменит.")"
    acme_say ""
    acme_say "$(t "A certificate is free from Let's Encrypt and renews itself. Getting" \
                  "Сертификат от Let's Encrypt бесплатен и продлевается сам. Получить его —")"
    acme_say "$(t "one agrees to https://letsencrypt.org/repository/" \
                  "значит согласиться с https://letsencrypt.org/repository/")"

    acme_ask_yn "$(t "Set up HTTPS for the panel now?" "Настроить HTTPS для панели сейчас?")" \
        || { acme_tty_close; return 1; }

    # Anything already here first: it issues nothing and tells no log.
    existing=$(acme_existing_certs | head -1)
    if [[ -n "$existing" ]]; then
        key=$(acme_key_beside "$existing")
        if [[ -n "$key" ]] && acme_pair_ok "$existing" "$key"; then
            acme_say ""
            acme_say "$(t "This machine already has one:" "На этой машине уже есть один:")"
            acme_say "  ${B}$(acme_cert_names "$existing")${N}   ${DIM}${existing}${N}"
            if acme_ask_yn "$(t "Use that one?" "Использовать его?")"; then
                # Through acme_adopt rather than straight into acme_apply. The
                # list above deliberately offers certificates under
                # /root/.acme.sh, which the panel's service cannot open, and
                # handing one of those to acme_apply produced a panel that
                # would not start and a rollback - an offer that could never be
                # accepted. Called directly, not in `$(...)`: the subshell a
                # command substitution opens would discard ACME_ADOPT_HOW and
                # ACME_ADOPT_ERR along with it.
                if ! acme_adopt "$existing" "$key" >/dev/null; then
                    acme_say ""
                    acme_say "$(t "That certificate could not be installed where the panel can read it:" \
                                  "Не удалось установить сертификат туда, откуда его прочитает панель:")"
                    acme_say "  ${ACME_ADOPT_ERR}"
                    acme_tty_close
                    return 1
                fi
                if acme_apply "$ACME_ADOPT_CERT" "$ACME_ADOPT_KEY"; then
                    ACME_RESULT=existing
                    ACME_CERT_NAME=$(acme_cert_names "$existing")
                    # Worth a line only for the plain copy. "direct" copied
                    # nothing, and the acme.sh route left acme.sh rewriting
                    # this copy and restarting the panel on every renewal; a
                    # copy nothing refreshes is a panel that serves the old
                    # certificate after the real one has been renewed, and that
                    # is invisible until it expires.
                    if [[ "$ACME_ADOPT_HOW" == "copy" ]]; then
                        acme_say ""
                        acme_say "$(t "Copied to ${ACME_TLS_DIR}. Whatever renews the original will not" \
                                      "Скопировано в ${ACME_TLS_DIR}. То, что продлевает оригинал, до этой")"
                        acme_say "$(t "reach that copy, so the panel serves this one until it is made again." \
                                      "копии не дойдёт: панель отдаёт её, пока копию не сделают заново.")"
                    fi
                    acme_tty_close
                    return 0
                fi
                acme_tty_close
                return 1
            fi
        fi
    fi

    # acme.sh is common on servers that have run another panel. Nothing here
    # drives it - its CLI is different enough that getting it wrong would break
    # a working setup - but saying nothing would look like this file had not
    # noticed, and its certificates are what acme_existing_certs offers above.
    if [[ -d /root/.acme.sh ]]; then
        acme_say ""
        acme_say "$(t "${DIM}(acme.sh is installed here. This drives certbot; to use acme.sh" \
                      "${DIM}(Здесь установлен acme.sh. Установщик использует certbot; чтобы использовать acme.sh,")"
        acme_say "$(t "instead, issue with it and import the files from awg-menu.)${N}" \
                      "выпустите сертификат с его помощью и импортируйте файлы через awg-menu.)${N}")"
    fi

    if acme_issue_flow "$endpoint"; then
        acme_tty_close
        return 0
    fi
    acme_tty_close
    return 1
}
