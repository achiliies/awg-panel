#!/bin/bash
#
# get.sh - the one command that installs AWG Panel.
#
#   curl -fsSLO https://github.com/achiliies/awg-panel/releases/latest/download/get.sh
#   sudo bash get.sh
#
# Piping this into `sudo bash` installs just as well, and is the right form for
# an unattended run, but it cannot carry an answer back on Ubuntu 25.10 and
# later: sudo there is sudo-rs, which runs the command under a pty of its own
# and feeds that pty from its own stdin - which in a pipeline is curl. The
# installer says so and takes the default for every question. See the trap
# beside install.sh's /dev/tty for the rest of what that change cost.
#
# What this is for is not the typing it saves. The release bundle carries its
# payload in its own tail and finds it through ${BASH_SOURCE[0]}, so it cannot
# be piped into bash at all - it has to exist as a file first. That left the
# documented install as two commands, a curl and a sudo, and the curl in it
# checked nothing: the person pasting it downloaded an installer, ran it as
# root, and never once compared it against the checksum published beside it.
#
# So this script is the missing half. It does what bin/awg-update already does
# before every unattended upgrade - resolve the release, download the bundle
# and the SHA256SUMS beside it, refuse anything that does not match, refuse
# anything that is not a bundle - and only then hands the file to bash. A first
# install and an update now arrive by the same road and are trusted for the
# same reasons, instead of the first one, the one run as root on a machine
# nobody has looked at yet, being the less examined of the two.
#
# Deliberately thin, and deliberately not a second implementation of anything.
# It never asks whether one version is newer than another - that rule lives in
# panel/awg/release.py and stays there, and a fresh server has no installed
# version to compare against anyway. It takes whatever /releases/latest names,
# which is the same release release.py would name, reached through the web
# redirect rather than the API so that a bare server needs neither python3 nor
# a rate-limited API call to install. Everything after the checksum is
# install.sh's business, including the language, the questions, and the
# entirety of what gets built.
#
# Environment:
#   AWG_PANEL_UPDATE_REPO   owner/repo to install from; the same variable
#                           release.py reads, so a fork that sets it in the
#                           install command keeps updating from itself
#   AWG_PANEL_RELEASE_URL   base URL of the releases area, for a mirror on a
#                           network that cannot reach github.com; https:// or
#                           file:// only, since plain http would carry the
#                           checksums as readably as the thing they vouch for
#   AWG_PANEL_VERSION       a tag to install instead of the latest release
#   AWG_PANEL_CACHE         where the download is kept; /var/tmp by default
#
# Every argument is passed through to the installer:
#
#   curl -fsSL <url> | sudo bash -s -- --no-ask --lang en
#
set -euo pipefail

# The whole script is one function, called on the last line. That is not a
# style: this file is meant to be read off a socket by a bash that executes
# each command as it arrives, and a connection cut halfway through delivers a
# prefix of it - a prefix that bash would happily run. Wrapped this way there
# is nothing to run until the closing brace has arrived, so a truncated
# download does nothing at all instead of doing half of this as root.
main() {
    local DEFAULT_REPO="achiliies/awg-panel"
    local ASSET_BUNDLE="awg-panel.sh"
    local ASSET_SUMS="SHA256SUMS"

    local B='' R='' G='' Y='' N=''
    if [[ -t 1 ]]; then
        B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
    fi

    say()  { printf '%s\n' "$*"; }
    die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
    step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }

    # Root first, before anything is downloaded, because the alternative is a
    # script that spends a minute fetching fifty megabytes and then discovers
    # it cannot install them. Not re-exec'd under sudo: this script is normally
    # being read from a pipe, so there is no file to hand sudo, and the honest
    # answer is the command that works.
    #
    # AWG_GET_ROOTLESS turns this off, the way AWG_UPDATE_ROOTLESS does for the
    # updater and for the same reason: tests/get.sh drives every refusal below
    # with a fake release, and a suite that had to be root to run at all would
    # be one typo away from the machine running it.
    if [[ ${EUID} -ne 0 && -z "${AWG_GET_ROOTLESS:-}" ]]; then
        printf '%serror:%s installing needs root. Run it as:\n\n' "$R" "$N" >&2
        printf '  curl -fsSL %s | sudo bash\n\n' \
            "https://github.com/${DEFAULT_REPO}/releases/latest/download/get.sh" >&2
        exit 1
    fi

    local repo="${AWG_PANEL_UPDATE_REPO:-}"
    repo="${repo:-$DEFAULT_REPO}"
    [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
        || die "AWG_PANEL_UPDATE_REPO is ${repo}, which is not an owner/repo name."

    local base="${AWG_PANEL_RELEASE_URL:-https://github.com/${repo}/releases}"
    base="${base%/}"

    # The scheme, before either downloader is handed it. curl refuses plain
    # http on its own - --proto '=https' - but wget does not: --https-only
    # governs which redirects it will follow and says nothing about the URL it
    # was given, so a wget-only box fetched the bundle over http quite happily.
    # That is the one configuration where the checksum below buys nothing at
    # all: SHA256SUMS arrives over the same cleartext connection as the thing
    # it vouches for, so anything on the path rewrites both and the comparison
    # still passes.
    #
    # Said here rather than left to the downloaders, so that the two of them
    # cannot disagree about it again, and so the refusal names the reason
    # instead of curl's "Protocol http not supported", which reads like a
    # curl that was built wrong.
    case "$base" in
        https://*|file://*) ;;
        *) die "AWG_PANEL_RELEASE_URL is ${base}, which is not https:// or file://.
       The installer and the checksums that vouch for it would both arrive
       over a connection anything on the path can rewrite, so the check this
       script exists to make would pass on whatever was substituted." ;;
    esac

    # curl or wget, whichever the image shipped with. Debian cloud images
    # commonly carry only one of them, and which one is not predictable enough
    # to depend on.
    local downloader=""
    if command -v curl >/dev/null 2>&1; then
        downloader=curl
    elif command -v wget >/dev/null 2>&1; then
        downloader=wget
    else
        die "neither curl nor wget is installed, and one of them has to be:
       apt-get update && apt-get install -y curl"
    fi
    command -v sha256sum >/dev/null 2>&1 \
        || die "sha256sum is not installed, and the download cannot be verified without it:
       apt-get update && apt-get install -y coreutils"
    command -v tar >/dev/null 2>&1 \
        || die "tar is not installed, and the installer cannot unpack itself without it:
       apt-get update && apt-get install -y tar"

    # A local mirror is allowed to be a directory of files rather than a
    # server, which is what AWG_UPDATE_FEED already offers the updater and what
    # tests/get.sh drives this with. Handled here rather than left to the
    # downloader because wget --https-only refuses file:// and curl's error for
    # it is not one anybody can act on.
    #
    # The third argument asks for a progress bar, which only the bundle wants:
    # it is fifty megabytes over a link that is often slow and sometimes
    # censored, and a minute of silence there is indistinguishable from a hang.
    # The checksums are a few hundred bytes and deserve no meter at all.
    fetch() {  # url dest [progress]
        case "$1" in
            file://*)
                if cp -- "${1#file://}" "$2" 2>/dev/null; then return 0; else return 1; fi ;;
        esac
        local meter=()
        case "$downloader" in
            # No --max-time. It is a wall clock over the whole transfer, not a
            # stall detector: at 900 seconds for a fifty megabyte bundle it
            # refused every link slower than about 57 KB/s, and it refused them
            # while they were still making progress. The people this software
            # is for are disproportionately on exactly those links, and what
            # they got was "could not download", which names neither the
            # timeout nor the speed that tripped it.
            #
            # --speed-limit with --speed-time asks the question actually worth
            # asking - has this transfer stopped moving? - and lets one that is
            # merely slow finish. A download that is dropped rather than
            # stalled is not retried whatever is set here: curl will not retry
            # a transfer it has already written output for, with or without
            # --retry-all-errors. The cache above is what covers that, by
            # making the second attempt cost nothing that already arrived.
            curl) [[ -n "${3:-}" ]] && meter=(--progress-bar) || meter=(-sS)
                  curl -fL "${meter[@]}" --proto '=https' --tlsv1.2 \
                       --retry 3 --retry-delay 2 \
                       --connect-timeout 20 \
                       --speed-limit 1024 --speed-time 60 -o "$2" "$1" ;;
            wget) [[ -n "${3:-}" ]] && meter=(-q --show-progress) || meter=(-q)
                  wget "${meter[@]}" --https-only --tries=3 --timeout=30 -O "$2" "$1" ;;
        esac
    }

    # Which release /releases/latest currently points at, asked once so that
    # both files are then taken from that exact tag. Asking twice for "latest"
    # instead would leave a window - narrow, but real on the day a release is
    # published - where the checksums come from one release and the bundle
    # from the next, and the only thing the operator would see is a checksum
    # mismatch on a download that was never corrupted. It also means the
    # version can be printed before fifty megabytes start moving.
    resolve_tag() {
        local seen=""
        case "$downloader" in
            curl) seen=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
                              --proto '=https' --tlsv1.2 --retry 2 \
                              --connect-timeout 15 --max-time 60 \
                              "${base}/latest" 2>/dev/null) || return 1 ;;
            wget) seen=$(wget -qS --spider --max-redirect=10 "${base}/latest" 2>&1 \
                         | sed -n 's/^[[:space:]]*[Ll]ocation:[[:space:]]*//p' \
                         | tail -1) || return 1 ;;
        esac
        [[ "$seen" == */releases/tag/* ]] || return 1
        seen="${seen##*/tag/}"
        seen="${seen%%[?#]*}"
        # Whatever came back is about to become part of a URL and part of a
        # path under /var/tmp, and it arrived over the network.
        [[ "$seen" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
        printf '%s\n' "$seen"
    }

    local tag="${AWG_PANEL_VERSION:-}"
    if [[ -n "$tag" ]]; then
        [[ "$tag" =~ ^[A-Za-z0-9._-]+$ ]] \
            || die "AWG_PANEL_VERSION is ${tag}, which is not a release tag."
    elif [[ "$base" == https://* ]]; then
        step "Finding the latest release"
        tag=$(resolve_tag) || tag=""
        if [[ -n "$tag" ]]; then
            say "  ${G}${tag}${N}"
        else
            # The redirect could not be read - an old wget, a proxy that
            # answers HEAD differently, a mirror that serves the assets and
            # nothing else. That is a reason to stop naming the version, not a
            # reason to stop: /latest/download is still the right pair of URLs,
            # they are simply resolved one at a time.
            say "  ${Y}could not read the version from ${base}/latest; taking what it serves${N}"
        fi
    fi

    # A mirror that is a directory of files rather than a server has no
    # redirect to follow and is not asked for one, which is why nothing above
    # runs for it. It gets the same URLs, and stays quiet about a version it
    # was never in a position to name.
    local url_bundle url_sums
    if [[ -n "$tag" ]]; then
        url_bundle="${base}/download/${tag}/${ASSET_BUNDLE}"
        url_sums="${base}/download/${tag}/${ASSET_SUMS}"
    else
        url_bundle="${base}/latest/download/${ASSET_BUNDLE}"
        url_sums="${base}/latest/download/${ASSET_SUMS}"
    fi

    # /var/tmp rather than /tmp, and this is the machine it matters on. /tmp is
    # a tmpfs on a good half of the images this gets installed on, so a fifty
    # megabyte download into it is fifty megabytes of RAM on a box that has a
    # gigabyte and is about to compile a kernel module - the exact pressure
    # build.sh streams the payload to avoid. /var/tmp is on disk, and is also
    # the directory that is meant to survive a reboot, which is what makes the
    # cache below worth having.
    local cache="${AWG_PANEL_CACHE:-/var/tmp}"
    # mkdir, not `install -d -m`: the default is /var/tmp, which already exists
    # and is 1777, and install would set the mode on it whether it created it
    # or not. Taking the sticky bit off /var/tmp is not a thing an installer
    # gets to do on the way past.
    mkdir -p "$cache" || die "could not create ${cache}."

    # Is this a directory this script could have made itself - ours, private,
    # and a directory rather than a link to one? stat is not asked to follow
    # symlinks, so a link reports itself as a link and fails here, which is the
    # answer wanted: its target may well be a root-owned 700 directory, and it
    # is still not the one this was going to write into.
    dir_is_ours() {  # path
        local meta
        meta=$(stat -c '%u:%a:%F' -- "$1" 2>/dev/null) || return 1
        [[ "$meta" == "$(id -u):700:directory" ]]
    }

    local work reuse=0
    if [[ -n "$tag" ]]; then
        work="${cache}/awg-panel-${tag}"
        # One cache directory, not one per release ever installed on this
        # machine. The bundle is fifty megabytes and the previous one is of no
        # further use the moment this one is named.
        local old
        for old in "$cache"/awg-panel-*; do
            if [[ -d "$old" && "$old" != "$work" ]]; then
                rm -rf -- "$old"
            fi
        done
        # That directory is kept between runs on purpose - the cache is what
        # makes a second attempt after a failed install cost nothing. But its
        # name is the release tag, which anybody can read off the releases
        # page, and it sits in /var/tmp, which is 1777: it is a directory any
        # local user can create before this script gets there.
        #
        # `install -d -m 700` used to be what created it, and it accepted that
        # one. install sets the mode on a directory that already exists and
        # leaves the ownership alone, so root carried on and downloaded into a
        # directory somebody else still owned - which is a symlink planted at
        # either filename for root to write through, and a bundle that can be
        # swapped in the moment between sha256sum reading it and bash running
        # it. The checksum below is worth nothing against the second of those:
        # it is checked on one file and a different one is executed.
        #
        # So a directory is reused only when it is one this script could have
        # made. Anything else is removed and remade below.
        dir_is_ours "$work" && reuse=1
    else
        work=$(mktemp -d "${cache}/awg-panel.XXXXXX") \
            || die "could not create a directory under ${cache}."
        reuse=1
        # Expanded now rather than at exit, and that is the point of the double
        # quotes: $work is a local of this function, so a trap deferring the
        # expansion would fire after it has gone and take `set -u` with it.
        # shellcheck disable=SC2064
        trap "rm -rf -- '$work'" EXIT
    fi
    if (( ! reuse )); then
        rm -rf -- "$work"
        # mkdir rather than mkdir -p, because the whole job of this line is to
        # be the thing that fails if the name is back by the time it runs. -p
        # takes somebody else's directory as success, which is the state this
        # is here to refuse.
        mkdir -m 700 -- "$work" 2>/dev/null \
            || die "could not create ${work}.
       If something else on this machine created it first, remove it and run
       this again."
    fi
    dir_is_ours "$work" \
        || die "${work} is not a directory this install can safely use.
       Remove it and run this again."

    step "Downloading ${ASSET_SUMS}"
    fetch "$url_sums" "$work/$ASSET_SUMS" \
        || die "could not download ${url_sums}
       Nothing on this server has been changed. If github.com is unreachable
       from here, mirror the release and set AWG_PANEL_RELEASE_URL to it."

    local want
    want=$(awk -v f="$ASSET_BUNDLE" '$2 == f || $2 == "*" f { print $1; exit }' \
               "$work/$ASSET_SUMS")
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] \
        || die "${ASSET_SUMS} does not name a checksum for ${ASSET_BUNDLE}, so the
       installer cannot be verified. Nothing on this server has been changed."

    # A bundle left by an earlier run of this script - an install that was
    # interrupted, or a build that failed and is being tried again. It is only
    # ever trusted after it has been checked against the SHA256SUMS downloaded
    # a moment ago, so a stale one, a truncated one or one somebody swapped is
    # a cache miss rather than a hazard.
    local got=""
    if [[ -f "$work/$ASSET_BUNDLE" ]]; then
        got=$(sha256sum "$work/$ASSET_BUNDLE" | awk '{print $1}')
    fi
    if [[ "$got" == "$want" ]]; then
        step "Already downloaded, and it matches ${ASSET_SUMS}"
    else
        rm -f "$work/$ASSET_BUNDLE"
        step "Downloading ${ASSET_BUNDLE}"
        fetch "$url_bundle" "$work/$ASSET_BUNDLE" progress \
            || die "could not download ${url_bundle}
       Nothing on this server has been changed."
        say "  $(du -h "$work/$ASSET_BUNDLE" | cut -f1) downloaded"

        # Not a formality, and the reason this script exists. What is on disk
        # is about to be run as root, and it came over the public internet from
        # a host neither end of this controls. The checksum published beside it
        # is the only statement anybody made about what the bytes should be.
        step "Checking it against ${ASSET_SUMS}"
        got=$(sha256sum "$work/$ASSET_BUNDLE" | awk '{print $1}')
        [[ "$want" == "$got" ]] || {
            rm -f "$work/$ASSET_BUNDLE"
            die "the downloaded installer does not match the checksum published for it
       (expected ${want},
             got ${got}).
       It was discarded and nothing on this server has been changed."; }
        say "  sha256 ${got} matches"
    fi

    # And that it is the shape of thing it claims to be, before bash is handed
    # it: a bundle is a shell prologue with a tar.gz after a marker line, and
    # --help unpacks it and asks the installer inside for its usage. The same
    # two checks bin/awg-update makes, for the same reason - a file that cannot
    # do this is not one to run as root.
    head -1 "$work/$ASSET_BUNDLE" | grep -q '^#!/bin/bash' \
        || die "what was downloaded is not a shell installer. Nothing has been changed."
    # TMPDIR here and below for the reason the download went to /var/tmp: the
    # bundle unpacks itself into a temporary directory, and on the images where
    # /tmp is a tmpfs that is its whole payload in RAM, twice - once for this
    # check and once for the install. Beside the bundle it is on disk, and the
    # bundle's own trap still removes it.
    TMPDIR="$work" bash "$work/$ASSET_BUNDLE" --help >/dev/null 2>&1 \
        || die "the downloaded installer will not unpack, so it arrived damaged.
       Nothing on this server has been changed."

    step "Running the installer"
    say ""

    # stdin is the pipe this script is being read from, and handing that to the
    # installer would let anything under it - apt, dkms, a prompt - swallow the
    # rest of this file, after which bash parses whatever is left. The
    # installer does not want it either: it opens /dev/tty on its own fd for
    # every question it asks, precisely so that the pipe is free to be a pipe.
    # So it is given the terminal if there is one and nothing if there is not.
    #
    # Run rather than exec'd, so that the temporary directory an unresolved
    # version falls back to is still cleaned up afterwards: exec would replace
    # this shell and take the EXIT trap with it.
    local rc=0
    if [[ -e /dev/tty ]] && (exec 3<>/dev/tty) 2>/dev/null; then
        TMPDIR="$work" bash "$work/$ASSET_BUNDLE" "$@" </dev/tty || rc=$?
    else
        TMPDIR="$work" bash "$work/$ASSET_BUNDLE" "$@" </dev/null || rc=$?
    fi
    return "$rc"
}

main "$@"
