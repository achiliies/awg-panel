#!/usr/bin/env bash
#
# tests/i18n.sh - prove the second language is a translation and not a fork.
#
# The risk this file exists for is not that a message comes out in the wrong
# language. It is that the two languages stop being the same install: a
# message that exists in one and not the other, a column that lines up in
# English and not in Russian, an answer that a Russian prompt offers and then
# will not take. Each of those has a check below.
#
# Nothing here needs root, a tunnel, or a network. It sources the libraries
# and calls into them.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
TESTS=0
FAILS=0

ok()  { TESTS=$((TESTS + 1)); printf '  ok    %s\n' "$1"; }
bad() {
    TESTS=$((TESTS + 1)); FAILS=$((FAILS + 1))
    printf '  FAIL  %s\n' "$1" >&2
    [[ -z "${2:-}" ]] || printf '        %s\n' "$2" >&2
}
is()  { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "got '$2', wanted '$3'"; }

# Pointed into the temporary directory before the library is sourced: it reads
# the stored language at source time, and a test run must not be told what to
# think by the machine it happens to be running on - nor write to it.
export AWG_LANG_FILE="$TMPD/language"

# shellcheck source=lib/i18n.sh
. "$REPO/lib/i18n.sh"

printf '== t, the whole of the mechanism ==\n'

LANG_CHOICE=en; is "English is what t returns by default" "$(t "one" "два")" "one"
LANG_CHOICE=ru; is "Russian is what it returns when asked" "$(t "one" "два")" "два"
LANG_CHOICE=en; is "is_ru is false in English" "$(is_ru && echo y || echo n)" "n"
LANG_CHOICE=ru; is "is_ru is true in Russian"  "$(is_ru && echo y || echo n)" "y"
# An unset or nonsense LANG_CHOICE has to read as English rather than as an
# error, because t() is called from inside messages and a failure there would
# replace the message with nothing.
LANG_CHOICE=""; is "an empty choice falls back to English" "$(t "one" "два")" "one"
LANG_CHOICE="de"; is "so does an unknown one" "$(t "one" "два")" "one"
LANG_CHOICE=en

printf '\n== columns line up in both languages ==\n'

# The bug this replaced: printf "%-14s" pads by bytes, and a Cyrillic letter
# is two of them, so a Russian label came out padded to about half the column
# and every row of the summary started somewhere different.
for w in 12 14 18; do
    en=$(pad "username" "$w")
    short=$(pad "сеть" "$w")
    if [[ ${#en} -eq $w && ${#short} -eq $w ]]; then
        ok "pad fills to ${w} characters in either alphabet"
    else
        bad "pad to ${w}: English gave ${#en}, Russian gave ${#short}"
    fi
done
# A label wider than the column is not truncated - it takes the room it needs
# and the row is long, the way printf's own %-Ns behaves.
over=$(pad "имя пользователя" 12)
[[ "$over" == "имя пользователя" ]] \
    && ok "a label wider than the column survives whole" \
    || bad "pad damaged an over-long label" "$over"

# The whole interview summary, rendered, with every row required to break in
# the same column. This is the check that would have caught the original.
# pad deliberately emits no newline - it is used inside a line - so the rows
# are terminated here rather than by it.
render_table() {
    local col label; col=$(t 14 18)
    for label in "$(t "VPN port"   "порт VPN")"         \
                 "$(t "network"    "сеть")"             \
                 "$(t "panel port" "порт панели")"      \
                 "$(t "panel path" "путь панели")"      \
                 "$(t "username"   "имя пользователя")" \
                 "$(t "password"   "пароль")"; do
        printf '%s\n' "$(pad "$label" "$col")"
    done
}
for lang in en ru; do
    LANG_CHOICE=$lang
    widths=$(render_table | while IFS= read -r row; do printf '%s\n' "${#row}"; done | sort -u | wc -l)
    is "the ${lang} settings table breaks in one column" "$widths" "1"
done
LANG_CHOICE=en

printf '\n== the language question ==\n'

# On a pty, because the prompt reads /dev/tty on fd 3 and the point of it is
# that it works for `curl | sudo bash`, where stdin is not a terminal.
ask() {
    python3 - "$1" "$2" <<'PY'
import os, pty, sys, time
typed, snippet = sys.argv[1], sys.argv[2]
master, slave = pty.openpty()
pid = os.fork()
if pid == 0:
    os.close(master)
    os.dup2(slave, 3)
    os.close(slave)
    os.execlp('bash', 'bash', '-c', snippet)
os.close(slave)
time.sleep(0.05)
os.write(master, (typed + '\n').encode())
time.sleep(0.05)
os.close(master)
sys.exit(os.WEXITSTATUS(os.waitpid(pid, 0)[1]))
PY
}
asked() {
    ask "$1" "
        . '$REPO/lib/i18n.sh'
        B='' N='' ASK_TTY=1 LANG_GIVEN=0 LANG_CHOICE=''
        prompt_installer_language
        [[ \"\$LANG_CHOICE\" == '$2' ]]" >/dev/null 2>&1
}

for typed in ru RU Ru rus russian ру рус русский; do
    asked "$typed" ru && ok "'${typed}' chooses Russian" || bad "'${typed}' did not choose Russian"
done
# Enter is the one that has to be right: it is what a hurried operator does,
# and it must not be able to land on a language they cannot read.
for typed in "" en english de "  " "?"; do
    asked "$typed" en && ok "'${typed}' leaves it in English" || bad "'${typed}' did not leave English"
done

# Enter is also what a re-install is answered with, and on a server that was
# set up in Russian it has to stay in Russian. The default follows the stored
# language, which is the one thing that makes the question safe to ask again.
for typed in "" "de" "?"; do
    ask "$typed" "
        AWG_LANG_FILE='$TMPD/prompt-lang'; printf 'ru\n' > \"\$AWG_LANG_FILE\"
        . '$REPO/lib/i18n.sh'
        B='' N='' ASK_TTY=1 LANG_GIVEN=0
        prompt_installer_language
        [[ \"\$LANG_CHOICE\" == ru ]]" >/dev/null 2>&1 \
        && ok "'${typed}' keeps a Russian server in Russian" \
        || bad "'${typed}' dropped a Russian server back to English"
done
# And "en" at that same prompt still gets English, or the choice is not a
# choice any more.
ask "en" "
    AWG_LANG_FILE='$TMPD/prompt-lang'; printf 'ru\n' > \"\$AWG_LANG_FILE\"
    . '$REPO/lib/i18n.sh'
    B='' N='' ASK_TTY=1 LANG_GIVEN=0
    prompt_installer_language
    [[ \"\$LANG_CHOICE\" == en ]]" >/dev/null 2>&1 \
    && ok "'en' turns a Russian server back to English" \
    || bad "'en' did not turn a Russian server back to English"

# Asked once. --lang on the command line sets LANG_GIVEN, and a second
# question would let Enter quietly overrule the flag.
ask "ru" "
    . '$REPO/lib/i18n.sh'
    B='' N='' ASK_TTY=1 LANG_GIVEN=1 LANG_CHOICE=en
    prompt_installer_language
    [[ \"\$LANG_CHOICE\" == en ]]" >/dev/null 2>&1 \
    && ok "a language already chosen is not asked about again" \
    || bad "the prompt overrode a language given on the command line"

# No terminal, no question, and no hanging waiting for one.
ASK_TTY=0 LANG_GIVEN=0 LANG_CHOICE=en prompt_installer_language </dev/null \
    && ok "--no-ask and a run with no terminal skip it" \
    || bad "prompt_installer_language failed with no terminal"

printf '\n== the flags ==\n'

# Accepted, and not merely survived. An install.sh that refused the flag would
# print something too, so the check is that what came out is the usage.
for lang in ru en; do
    if out=$(bash "$REPO/install.sh" --lang "$lang" --help 2>&1) \
       && [[ "$out" == "install.sh - build and configure"* ]]; then
        ok "install.sh accepts '--lang ${lang}'"
    else
        bad "install.sh rejected '--lang ${lang}'" "${out%%$'\n'*}"
    fi
done
out=$(bash "$REPO/install.sh" --lang de 2>&1 || true)
if [[ "$out" == *"'en'"* && "$out" == *"'ru'"* ]]; then
    ok "an unsupported language is refused, naming the two that work"
else
    bad "install.sh did not refuse --lang de" "$out"
fi
out=$(bash "$REPO/panel/install-panel.sh" --lang de 2>&1 || true)
[[ "$out" == *"expected 'en' or 'ru'"* ]] \
    && ok "install-panel.sh refuses one too" \
    || bad "install-panel.sh did not refuse --lang de" "$out"

for f in install.sh panel/install-panel.sh; do
    grep -q -- '--lang' <(bash "$REPO/$f" --help) \
        && ok "${f} --help documents --lang" \
        || bad "${f} --help does not mention --lang"
done

# install.sh has to hand its choice on: the panel installer is a second
# process, and the two halves of one install printing in two languages is
# what forgetting this looks like.
grep -q 'PANEL_ARGS=(.*--lang "\$LANG_CHOICE"' "$REPO/install.sh" \
    && ok "install.sh passes its language to install-panel.sh" \
    || bad "install.sh does not pass --lang to install-panel.sh"

printf '\n== the messages themselves ==\n'

# Every t() call has to have both halves. A one-argument call returns the
# empty string in Russian, which is a message that silently disappears.
#
# Anchored on "$(t " so that this matches a call and not the tail of some
# other word ending in t - "--port "$PANEL_PORT")" reads as one otherwise.
short=$(grep -rnoE '\$\(t "[^"]*"[[:space:]]*\)' "$REPO/install.sh" "$REPO/lib" \
        "$REPO/bin/awg-menu" "$REPO/panel/install-panel.sh" 2>/dev/null || true)
[[ -z "$short" ]] && ok "no t() call is missing its second language" \
                  || bad "t() called with one argument" "$short"

# awg-menu is translated now, so this rule is the old one turned around. A
# message that points into the menu has to name the entry in its own language:
# a Russian sentence ending in "-> Diagnostics" sends the reader looking for a
# line that is not in the menu they were told to open, which is exactly what
# the English names used to prevent and now cause.
#
# Read as: a line with Cyrillic in it - so, the Russian half of a t() call -
# that still arrows at a capitalised Latin word.
menu=$(grep -rn 'awg-menu.*->' "$REPO/install.sh" "$REPO/lib/acme.sh" \
       | grep '[А-Яа-яЁё]' | grep -P '\->\s*[A-Z]' || true)
[[ -z "$menu" ]] && ok "a Russian message names awg-menu's entries in Russian" \
                 || bad "a Russian message names an awg-menu entry in English" "$menu"

# Both languages of awg-menu itself. Every dialog it opens goes through one of
# a handful of helpers, so an untranslated screen is a call to one of them with
# a bare English string where a t() should be.
raw=$(grep -nE '^[^#]*\b(msg|yes_no|yes_no_danger|page|page_str|busy) "[A-Z]' "$REPO/bin/awg-menu" \
      | grep -v '\$(t ' | grep -v 'Language / Язык' || true)
[[ -z "$raw" ]] && ok "every dialog awg-menu opens carries both languages" \
                || bad "an awg-menu dialog is English only" "$raw"

# And its menus: a tag is a key and stays English, but the description beside
# it is what the operator reads.
raw=$(grep -nE '^\s+(status|url|passwd|twofa|port|listen|cert|path|restart|start|stop|boot|logs|panel|diag|update|lang|about|uninstall|view|issue|import|found|renew|remove|off|renewal|selftest|peers|net|module|config|install|notes|rebuild)\s+"[A-Z]' \
      "$REPO/bin/awg-menu" | grep -v '\$(t ' || true)
[[ -z "$raw" ]] && ok "every awg-menu entry is named in both languages" \
                || bad "an awg-menu entry is English only" "$raw"

# A translated description is only half of it. whiptail prints the tag in a
# column of its own to the left of the description, so a menu keyed on
# panel/lang/uninstall came up with those words down the left edge of a
# Russian screen - untranslatable, because they are the keys the case
# statement dispatches on. --notags hides that column, and every menu whose
# tags are identifiers has to carry it.
#
# --default-item is the marker for those menus: it is there precisely because
# the tag is a remembered key rather than a label, so a menu that uses one and
# has no --notags is the shape of the bug. The numbered certificate lists have
# neither and are left alone; their tags are 1, 2, 3, which read the same in
# both languages.
raw=$(awk '/whiptail /{ blk=""; ln=NR }
           { blk = blk $0 "\n" }
           !/\\$/{ if (blk ~ /--menu / && blk ~ /--default-item/ && blk !~ /--notags/)
                        printf "%d: whiptail --menu with --default-item and no --notags\n", ln
                    blk = "" }' "$REPO/bin/awg-menu" || true)
[[ -z "$raw" ]] && ok "every keyed awg-menu menu hides its tag column" \
                || bad "an awg-menu menu would show English tags" "$raw"

# lib/obfs.sh is used by tests/obfs.sh with nothing else sourced, so it has to
# carry its own i18n or it breaks that harness rather than this one.
out=$(bash -c ". '$REPO/lib/obfs.sh'; LANG_CHOICE=ru; gen_obfuscation 1420; printf '%s' \"\$GEN_DESC\"" 2>&1)
[[ "$out" == *[А-Яа-яЁё]* ]] \
    && ok "lib/obfs.sh describes a profile in Russian on its own" \
    || bad "lib/obfs.sh gave no Russian description" "$out"
out=$(bash -c ". '$REPO/lib/obfs.sh'; LANG_CHOICE=en; gen_obfuscation 1420; printf '%s' \"\$GEN_DESC\"" 2>&1)
[[ "$out" != *[А-Яа-яЁё]* && -n "$out" ]] \
    && ok "and in English when that is what was asked for" \
    || bad "lib/obfs.sh leaked Russian into an English run" "$out"

printf '\n== the language, written down ==\n'

# The installer asks the question once and everything after it - awg-menu, the
# next installer run - has to find the answer. The file is the whole of that
# mechanism, so it is exercised rather than assumed.
LANG_FILE="$TMPD/store"
is "nothing stored reads as nothing" "$(lang_stored)" ""
lang_save ru && is "a saved language comes back" "$(lang_stored)" "ru" \
             || bad "lang_save ru failed"
is "and this process is switched to it" "$LANG_CHOICE" "ru"
lang_save en && is "and it can be changed back" "$(lang_stored)" "en" \
             || bad "lang_save en failed"
lang_save de 2>/dev/null && bad "lang_save accepted a language that does not exist" \
                         || ok "lang_save refuses a language that does not exist"
is "a refused save leaves the stored one alone" "$(lang_stored)" "en"
# Anything else in the file reads as nothing rather than as itself. The readers
# put it straight into LANG_CHOICE, and a value neither half of t() knows would
# silently mean English while looking like a setting that had been honoured.
printf 'francais\n' > "$LANG_FILE"
is "a file with something else in it reads as nothing" "$(lang_stored)" ""
printf '  ru  \n' > "$LANG_FILE"
is "and whitespace around the word does not hide it" "$(lang_stored)" "ru"
LANG_FILE="$TMPD/no-such-directory/language"
lang_save ru 2>/dev/null && bad "lang_save claimed to write where it could not" \
                        || ok "lang_save reports a place it cannot write"
LANG_FILE="$TMPD/language"
LANG_CHOICE=en

# The point of writing it down: a tool started afterwards comes up in it
# without being told. Run as a separate process, because that is the only way
# the question is really being asked.
lang_of() {
    env -u LANG_CHOICE AWG_LANG_FILE="$1" bash -c \
        ". '$REPO/lib/i18n.sh'; printf '%s' \"\$LANG_CHOICE\""
}
printf 'ru\n' > "$TMPD/stored-ru"
is "a tool started later comes up in the stored language" "$(lang_of "$TMPD/stored-ru")" "ru"
: > "$TMPD/stored-empty"
is "and in English when nothing was stored" "$(lang_of "$TMPD/stored-empty")" "en"
# An explicit choice still wins: install.sh hands install-panel.sh a --lang,
# and a stored language must not be able to contradict a flag that was typed.
out=$(AWG_LANG_FILE="$TMPD/stored-ru" LANG_CHOICE=en bash -c \
      ". '$REPO/lib/i18n.sh'; printf '%s' \"\$LANG_CHOICE\"")
is "an explicit choice beats the stored one" "$out" "en"

# install.sh has to actually write it. Without this the menu opens in English
# on a server whose whole install was in Russian, which is the bug this file
# was extended for.
grep -q 'lang_save "\$LANG_CHOICE"' "$REPO/install.sh" \
    && ok "install.sh records the language it installed in" \
    || bad "install.sh never calls lang_save"

# And awg-menu has to offer the other one. A menu that can only be changed by
# re-running the installer is not a language setting.
grep -q '^lang_menu() {' "$REPO/bin/awg-menu" \
    && ok "awg-menu has a language screen" \
    || bad "awg-menu has no lang_menu"
grep -qE '^\s+lang\)\s+lang_menu' "$REPO/bin/awg-menu" \
    && ok "and the main menu opens it" \
    || bad "awg-menu's main menu does not reach lang_menu"

printf '\n== what a failing install says ==\n'

# The half that goes missing first, because it is the half nobody rehearses:
# an install that works is read once and an install that dies is read closely,
# and it dies in whichever language the die was written in. The first pass at
# this branch translated every step and left the failures in English, which
# put the one message an operator has to act on in the language they said they
# could not read.
#
# Two messages are exempt and stay English. One about a flag - "--mtu '9001'
# is not between 1280 and 9000" - is a command line and the value typed into
# it, neither of which is in Russian on anybody's terminal. An "internal:" is
# a bug report and its reader is here, not on the server.
untranslated() {
    grep -nE '^[^#]*\b(die|warn)[[:space:]]+"' "$1" \
        | grep -v '\$(t ' \
        | grep -vE '\b(die|warn)[[:space:]]+"(--|internal:)' || true
}
left=$(untranslated "$REPO/install.sh")
[[ -z "$left" ]] && ok "install.sh says why it failed in both languages" \
                 || bad "an install.sh failure is English only" "$left"

# The uninstall half of install-panel.sh is driven by awg-uninstall, which is
# not translated, so it is cut out before the same rule is put to the rest.
sed '/^if (( UNINSTALL )); then$/,/^fi$/d' "$REPO/panel/install-panel.sh" > "$TMPD/panel.sh"
left=$(untranslated "$TMPD/panel.sh")
[[ -z "$left" ]] && ok "install-panel.sh says why it failed in both languages" \
                 || bad "an install-panel.sh failure is English only" "$left"

# lib/acme.sh is not swept the same way. What awg-menu shows out of it is
# translated now, but awg-uninstall calls into it too and is not, so an English
# line in there is still right about as often as it is wrong and a blanket rule
# would only be noise. The one rule that holds everywhere in it is this:
# ACME_FLOW_ERR is the sentence that outlives the terminal and gets printed by
# whatever reports the failure afterwards, so a copy of it in one language is a
# failure report that changes language halfway through.
left=$(grep -n 'ACME_FLOW_ERR="[^"]' "$REPO/lib/acme.sh" | grep -v '\$(t ' || true)
[[ -z "$left" ]] && ok "every ACME_FLOW_ERR carries both languages" \
                 || bad "an ACME_FLOW_ERR is English only" "$left"

printf '\n== a yes/no answer in either language ==\n'

# Under LC_ALL=C on purpose. A server with no locale generated is where this
# broke: bash cannot lowercase Д without a UTF-8 locale, so "ДА" fell through
# to "please answer y or n" and kept doing it. lib/i18n.sh sets the locale for
# exactly this, and the check is worth nothing if it runs where it was already
# right.
yn() {
    LC_ALL=C ask "$1" "
        LC_ALL=C
        . '$REPO/lib/common.sh'
        . '$REPO/lib/acme.sh'
        LANG_CHOICE='$2'
        ACME_FD=3
        acme_tty_drain() { :; }
        acme_ask_yn 'x?'" >/dev/null 2>&1
}
for yes in y yes Y YES д да Д ДА; do
    yn "$yes" ru && ok "Russian takes '${yes}' for yes" || bad "Russian rejected '${yes}'"
done
for no in n no N NO н нет Н НЕТ; do
    yn "$no" ru && bad "Russian read '${no}' as yes" || ok "Russian takes '${no}' for no"
done
# English takes the English pair and nothing else. A Russian answer accepted
# at an English prompt would hide that the language was never set.
for yes in y yes Y YES; do
    yn "$yes" en && ok "English takes '${yes}' for yes" || bad "English rejected '${yes}'"
done

printf '\n%d passed, %d failed\n' "$((TESTS - FAILS))" "$FAILS"
[[ "$FAILS" -eq 0 ]] || exit 1
