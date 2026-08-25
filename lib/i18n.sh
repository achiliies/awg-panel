# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/i18n.sh - the installer in a second language.
#
# Sourced from lib/common.sh, so everything that reads the shared library has
# it. lib/obfs.sh sources it again on its own account, being the one module
# that is used without common.sh beside it, and panel/install-panel.sh carries
# a copy of the two functions because it also runs standalone out of a release
# bundle that has no lib/ at all.
#
# One helper does the work, and it wraps the string rather than the code:
#
#     step "$(t "Checking the system" "Проверка системы")"
#
# The alternative is a branch at every message - if is_ru; then ... else ...
# fi - and it was tried first. It costs more than it looks. The code around a
# message here is rarely a bare call: it is `(( DISK > 1500 )) || die "..."`,
# and a second branch turns that one line into six, whose two halves then have
# to be kept in step by hand forever with nothing checking that they are. It
# also moves the English out of the line it belongs to, which matters in a
# file where the message and the comment above it are how the thing explains
# itself. Wrapping the string leaves every statement, and every comment
# attached to it, exactly where it was.
#
# A catalogue keyed by name - panel/frontend/src/i18n/{en,ru}.json, the way
# the panel's own UI is translated - goes further in the same direction and is
# right there and wrong here. A React component was never meant to be read
# top to bottom; this file is, and `step "$(msg preflight.checking)"` tells a
# reader nothing about what the install is about to print.

[[ -n "${_AWG_I18N:-}" ]] && return 0
_AWG_I18N=1

# A string operation in bash is a locale decision, and both of the ones used
# below split on it. ${#s} counts characters in a UTF-8 locale and bytes in C,
# where a Cyrillic letter counts twice, so every column padded to a width
# drifts by the length of the word in front of it. ${s,,} splits the same way:
# it lowercases Д in one and leaves it alone in the other, which is the
# difference between "ДА" being an answer and being refused forever by a
# prompt that will take nothing else.
#
# Neither is hypothetical here. This installs onto minimal server images where
# nobody has run locale-gen and root's shell comes up in C. So the locale is
# set, if it has to be, out of what the machine actually has: C.UTF-8 is
# compiled into glibc rather than generated, so it is normally present even
# when nothing else is. Nothing is exported: on the machines this is written
# for, LC_ALL is not in the environment at all, so the assignment below makes
# a shell variable, and apt, certbot and make go on running in whatever the
# operator set. One case escapes that, and is left escaping it - an operator
# who exported LC_ALL themselves, `LC_ALL=C sudo bash install.sh`, sudo being
# a thing that passes LC_* through. The name already carries the export
# attribute there and an assignment cannot take it away, so those children get
# C.UTF-8 where they had C. `export -n` would not put them back in C either:
# it drops them to whatever LANG says, which is the larger change of the two.
_i18n_charwise() { local probe='да'; (( ${#probe} == 2 )); }
if ! _i18n_charwise; then
    while read -r _i18n_loc; do
        case "${_i18n_loc,,}" in
            c.utf-8|c.utf8|en_us.utf-8|en_us.utf8) LC_ALL="$_i18n_loc"; break ;;
        esac
    done < <(locale -a 2>/dev/null)
    unset _i18n_loc
fi

# Where the answer to the language question is kept between runs.
#
# The installer asks it once, and everything installed afterwards - awg-menu,
# awg-update, the uninstaller, and the installer itself on its next run - has
# to come up speaking the same language, because an operator who said they
# read Russian did not stop reading Russian when the install finished. Nothing
# else on the machine knows what they answered unless it is written down.
#
# One word on one line, not a key=value file. The readers are shell scripts
# sourcing this before they have parsed anything of their own, so a format
# with syntax would need a parser here; and a file with room for a second key
# invites one that only some of its readers understand.
#
# It lives in the config directory rather than beside the binaries because
# that is the directory an uninstall keeps with --keep-config: reinstalling
# onto a server that was set up in Russian should not come back in English.
LANG_FILE="${AWG_LANG_FILE:-${AWG_CONF_DIR:-/etc/amnezia/amneziawg}/language}"

# The stored language, or nothing. Never fails: every caller reads it into a
# default, and a machine that has not been asked yet is the ordinary case
# rather than an error.
lang_stored() {
    local saved=""
    [[ -r "$LANG_FILE" ]] || return 0
    read -r saved < "$LANG_FILE" 2>/dev/null || true
    saved="${saved//[[:space:]]/}"
    [[ "$saved" == en || "$saved" == ru ]] && printf '%s' "$saved"
    return 0
}

# Write it down and switch this process to it, or say it could not. Both
# halves matter to the caller: awg-menu's language screen has to report a file
# it could not write, because the alternative is a menu that changes language
# and is back in the old one after the next reboot with nothing to explain it.
#
# The mode is set rather than left to whoever happened to write it: the
# installer runs under `umask 077` by the time it calls this and awg-menu does
# not, so the same file would come out 0600 from one and 0644 from the other.
# Nothing here is secret - the directory around it is 0700 and root-owned - but
# a file whose permissions depend on which tool last touched it is a thing
# somebody has to work out is meaningless.
lang_save() {
    [[ "$1" == en || "$1" == ru ]] || return 1
    printf '%s\n' "$1" > "$LANG_FILE" 2>/dev/null || return 1
    chmod 600 "$LANG_FILE" 2>/dev/null
    LANG_CHOICE="$1"
    return 0
}

# What this run speaks. A choice already in the environment wins over the
# stored one, which wins over English - and --lang, parsed by the caller after
# this file is sourced, wins over all three.
LANG_CHOICE="${LANG_CHOICE:-$(lang_stored)}"
LANG_CHOICE="${LANG_CHOICE:-en}"
# Whether the language was settled before this run started asking - a --lang
# on the command line, or an environment that already carried a choice. Both
# mean the question below has been answered and must not be put again. A
# stored language is not one of those: it is the default for the question, not
# an answer to it, so an operator re-running the installer is still offered the
# other language - with Enter now landing on the one they chose last time.
LANG_GIVEN="${LANG_GIVEN:-0}"

is_ru() { [[ "${LANG_CHOICE:-en}" == "ru" ]]; }

# English first, Russian second, in that order at every call site so that the
# file still reads as English prose to somebody who does not have the second
# language. Nothing is looked up: what you see beside the call is what the
# install prints.
#
# Both arguments are expanded before either is chosen, which is what an
# argument to any function does and worth saying out loud because these
# arguments are messages: a $( ) inside one of them runs whether or not that
# language wins. Every one of them here is a cheap read - nproc, a grep -c
# over the server config - and none has a side effect, which is the property
# that has to hold rather than a coincidence to be relied on.
t() {
    if [[ "${LANG_CHOICE:-en}" == "ru" ]]; then printf '%s' "$2"
    else                                        printf '%s' "$1"; fi
}

# A label padded out to a column, counted in characters. printf's own "%-14s"
# counts bytes, so against a Russian label it pads by roughly half what the
# terminal will draw and the value beside it lands somewhere different on
# every line of the same table.
pad() {
    local s="$1" w="$2"
    printf '%s%*s' "$s" "$(( w > ${#s} ? w - ${#s} : 0 ))" ''
}

# The one question asked before anything is built, because every line printed
# after it is an answer to it. It is also the only question that has to be
# asked in both languages at once: whoever needs the Russian is precisely the
# person who cannot be relied on to read an English prompt offering it.
#
# ask_drain belongs to install.sh rather than to this file, and is called
# through `type` because install-panel.sh sources neither. Draining matters
# for the same reason it does at every other question: a newline left over on
# the terminal answers this one before it is read, and the answer it gives is
# the default.
#
# The default is whatever the machine was last set to, so a re-install or an
# upgrade of a Russian server does not quietly hand the operator an English
# install because they pressed Enter. On a machine that has never been asked
# there is nothing stored and the default is English, which is the first run
# every server has and the only behaviour anybody has seen so far.
#
# Anything unrecognised keeps the default rather than forcing English. Typing
# "de" is not a request for English; it is a typo, and on a Russian server
# answering it in English is the one outcome the operator cannot read.
prompt_installer_language() {
    (( ASK_TTY )) || return 0
    (( LANG_GIVEN )) && return 0
    local def="${LANG_CHOICE:-en}"
    [[ "$def" == ru ]] || def=en
    type ask_drain >/dev/null 2>&1 && ask_drain
    printf '\n  %sLanguage / Язык%s\n' "$B" "$N" >&3
    if [[ "$def" == ru ]]; then
        printf '  Нажмите Enter, чтобы продолжить на русском языке.\n' >&3
        printf '  Type "en" to continue the installation in English.\n' >&3
    else
        printf '  Press Enter to continue in English.\n' >&3
        printf '  Введите "ru", чтобы продолжить установку на русском языке.\n' >&3
    fi
    local lang_in=""
    printf '\n  %s[Enter = %s]:%s ' "$B" "$def" "$N" >&3
    read -r -u 3 lang_in || { ASK_TTY=0; LANG_CHOICE="$def"; return 0; }
    case "${lang_in,,}" in
        ru|rus|russian|ру|рус|русский)      LANG_CHOICE="ru" ;;
        en|eng|english|ен|англ|английский)  LANG_CHOICE="en" ;;
        *)                                  LANG_CHOICE="$def" ;;
    esac
}
