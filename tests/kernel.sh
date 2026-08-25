#!/bin/bash
#
# tests/kernel.sh - the module has to exist for the kernel that boots next.
#
# The bug this exists for cost somebody a working server. install.sh builds
# amneziawg for `uname -r` and registers it with DKMS, which is where the story
# used to end, and on a box whose newer kernel image was already unpacked -
# any box unattended-upgrades has touched since its last reboot - that is a
# module built for a kernel nobody will be running by morning. DKMS does not
# fill the gap: it rebuilds from a kernel package's postinst hook, so it covers
# kernels installed after amneziawg was registered and not the one that was
# already sitting there. The install finishes, the tunnel works, the admin
# reboots as everybody does, and comes back to `modinfo amneziawg` saying
# nothing and awg-quick saying "Cannot find device awg0".
#
# So the two answers install.sh now works out for itself are checked here:
# which kernel the machine boots next, and which headers package keeps headers
# arriving for the kernels after that. Both functions are lifted out of
# install.sh so what is tested is what ships - the script itself cannot be run
# without root, a compiler and a kernel to build against.
#
# The wiring around them is checked by reading install.sh, which is the only
# way to check it short of a reboot: that the second build is aimed at the
# other kernel with -k, that the tunnel is ordered after DKMS's own boot-time
# rebuild, and that the uninstaller takes that ordering away again.
#
# Usage: tests/kernel.sh         (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"
        [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
is()  { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "got '$2', wanted '$3'"; }

# The real functions, lifted from the shipping installer. A definition that
# opens with `name() {` and closes with a `}` in the first column is the whole
# of the convention this relies on.
lift() { awk "/^$1\\(\\) \\{/,/^\\}/" "$REPO/install.sh"; }
{   lift headers_flavour
    lift next_kernel
} > "$WORK/kernel.sh"
for fn in headers_flavour next_kernel; do
    grep -q "^${fn}() {" "$WORK/kernel.sh" || {
        printf '  FAIL  could not lift %s out of install.sh\n' "$fn"; exit 1; }
done
# shellcheck source=/dev/null
. "$WORK/kernel.sh"

# A dpkg that answers the one question headers_flavour asks it. The real one
# is on every Debian and Ubuntu box and on few others, and this suite runs
# wherever CI runs.
mkdir -p "$WORK/bin"
printf '#!/bin/sh\necho amd64\n' > "$WORK/bin/dpkg"
chmod 755 "$WORK/bin/dpkg"
PATH="$WORK/bin:$PATH"

printf '== the headers package that tracks future kernels ==\n'

# ID is /etc/os-release's, which install.sh sources before it calls any of
# this, and which the two distributions' fallbacks differ on. Exported rather
# than merely set, so that it is a variable the lifted function reads from its
# environment exactly as it does in the script it came out of.
export ID=ubuntu
is "Ubuntu's stock kernel is the generic flavour" \
   "$(headers_flavour 6.8.0-137-generic)" "generic"
# The reason this is not simply "generic": a box booting the AWS kernel takes
# its images from linux-image-aws, and linux-headers-generic on it installs
# headers for a kernel it will never run - which is the same silence as no
# headers at all, one kernel upgrade later.
is "a cloud kernel keeps its own flavour" \
   "$(headers_flavour 6.8.0-1021-aws)" "aws"
is "an ABI bump does not change the flavour" \
   "$(headers_flavour 6.8.0-9-generic)" "generic"
is "a flavour of several words survives whole" \
   "$(headers_flavour 6.8.0-31-generic-64k)" "generic-64k"

export ID=debian
is "Debian's cloud kernel names its own headers" \
   "$(headers_flavour 6.1.0-18-cloud-amd64)" "cloud-amd64"
is "Debian's stock kernel is the architecture" \
   "$(headers_flavour 6.1.0-18-amd64)" "amd64"
# Anything that is not <version>-<abi>-<flavour> is not a distribution kernel,
# and guessing a package name from it invents one that does not exist. The
# distribution's own default is the honest answer.
is "a hand-built kernel falls back to the distribution's default" \
   "$(headers_flavour 6.14.0-rc1)" "amd64"
export ID=ubuntu
is "so does one with no flavour at all" \
   "$(headers_flavour 6.14.0)" "generic"

printf '== the kernel the next reboot will use ==\n'

BOOT="$WORK/boot"
mkdir -p "$BOOT"
export AWG_BOOT_DIR="$BOOT"

is "nothing bootable is an empty answer, not an error" "$(next_kernel)" ""

: > "$BOOT/initrd.img-6.8.0-137-generic"
: > "$BOOT/config-6.8.0-137-generic"
is "an initrd and a config are not kernels" "$(next_kernel)" ""

: > "$BOOT/vmlinuz-6.8.0-9-generic"
: > "$BOOT/vmlinuz-6.8.0-137-generic"
: > "$BOOT/vmlinuz-6.8.0-71-generic"
# Sorted as versions and not as strings. Lexically 9 beats 137, which is the
# whole failure: picking the wrong one here builds a second module for a
# kernel already covered and leaves the one that boots without any.
is "the newest image wins, by version and not by spelling" \
   "$(next_kernel)" "6.8.0-137-generic"

# A purged kernel takes its image and leaves everything else, so /lib/modules
# is not the list of kernels this machine can boot - which is why the images
# are what gets read.
rm -f "$BOOT/vmlinuz-6.8.0-137-generic"
is "an image that has been purged stops counting" \
   "$(next_kernel)" "6.8.0-71-generic"
unset AWG_BOOT_DIR

printf '== what install.sh does with the answers ==\n'

INST="$REPO/install.sh"

grep -q 'dkms build   -m amneziawg -v "$DKMSVER" -k "$NEXT_KVER"' "$INST" \
    && grep -q 'dkms install -m amneziawg -v "$DKMSVER" -k "$NEXT_KVER"' "$INST" \
    && ok "the second build is aimed at the other kernel with -k" \
    || bad "install.sh does not build for NEXT_KVER with -k"

# Without the headers for it there is nothing to build against, and the
# install is on the only machine that can fetch them.
grep -q 'linux-headers-${NEXT_KVER}' "$INST" \
    && ok "headers for that kernel are fetched before building" \
    || bad "install.sh does not install linux-headers for NEXT_KVER"

# The meta package is what the *following* upgrade needs, so it cannot be
# conditional on the exact package having been missing.
grep -q '^\$APT install -y -qq "\$HDR_META"' "$INST" \
    && ok "the headers meta package goes on every install" \
    || bad "install.sh only installs the headers meta package as a fallback"

# One boot in the life of a machine has the module compiled while it comes up,
# and dkms.service spends minutes on it. Ordered against nothing, awg-quick@
# starts inside that window, fails on a device that cannot exist yet, and
# stays failed until somebody notices.
grep -q 'After=dkms.service' "$INST" \
    && ok "the tunnel is ordered after DKMS's boot-time rebuild" \
    || bad "install.sh does not order awg-quick@ after dkms.service"

# Matched on the path rather than on the whole line: the removal runs once per
# interface now, over the list awg_ifaces builds, so the variable in it is the
# loop's and not IFACE. What has to be true is that the drop-in install.sh wrote
# is removed by name, which is what this asks.
grep -q 'rm -rf "/etc/systemd/system/awg-quick@' "$REPO/bin/awg-uninstall" \
    && ok "the uninstaller takes that ordering away again" \
    || bad "awg-uninstall leaves the awg-quick@ drop-in behind"

# And that it is asked of every tunnel found, not only the one the env file
# names - the drop-in for a second interface outlives the unit template exactly
# as the first one does.
awk '/^for _if in "\$\{IFACES\[@\]\}"; do$/,/^done$/' "$REPO/bin/awg-uninstall" \
    | grep -q 'awg-quick@\${_if}.service.d' \
    && ok "and takes it away for every tunnel it found" \
    || bad "awg-uninstall only removes the drop-in for one interface"

# The summary is the part anyone reads, and a kernel with no module at all is
# worse than a module one version behind - so it has to be able to say so.
grep -q 'KMOD_NEXT' "$INST" \
    && ok "a kernel left without a module reaches the closing summary" \
    || bad "install.sh does not report a kernel left without a module"

printf '== a kernel that cannot be built for ==\n'

# The block itself, run for real. Everything above proves install.sh works out
# the right kernel; this proves what it does when it cannot do anything about
# it - headers that apt has never heard of, on a machine whose next kernel is
# not this one.
#
# What matters here is that the install does not die. It runs under
# `set -euo pipefail` with the tunnel already up, and every command in the
# block is one that fails on a box like this: an apt-get that exits 1, a
# missing build tree, a dkms that never runs. A single unguarded one of those
# aborts an install that had already finished the part anybody needed, and
# leaves a machine mid-teardown for the sake of a kernel it has not booted yet.
BOOT2="$WORK/boot2"
mkdir -p "$BOOT2"
: > "$BOOT2/vmlinuz-9.9.9-2-generic"

printf '#!/bin/sh\necho "$@" >> "$CALLS"\nexit 1\n' > "$WORK/bin/apt-get"
printf '#!/bin/sh\necho "$@" >> "$CALLS"\nexit 1\n' > "$WORK/bin/dkms"
printf '#!/bin/sh\nexit 0\n' > "$WORK/bin/depmod"
chmod 755 "$WORK/bin/apt-get" "$WORK/bin/dkms" "$WORK/bin/depmod"

{   printf 'set -euo pipefail\n'
    # The real reporting helpers and the real t(), so the message this prints
    # is the message a server prints.
    printf '. "%s/lib/common.sh"\n' "$REPO"
    printf 'APT=apt-get KVER=0.0.0-1-generic DKMSVER=1.0.0\n'
    lift next_kernel
    awk '/^KMOD_NEXT=""/,/^fi$/' "$REPO/install.sh"
    printf 'printf "KMOD_NEXT=%%s\\n" "$KMOD_NEXT"\n'
} > "$WORK/next.sh"
grep -q '^NEXT_KVER=' "$WORK/next.sh" \
    || { printf '  FAIL  could not lift the next-kernel block out of install.sh\n'; exit 1; }

CALLS="$WORK/calls"
: > "$CALLS"
out=$(CALLS="$CALLS" PATH="$WORK/bin:$PATH" AWG_BOOT_DIR="$BOOT2" \
      AWG_LANG_FILE="$WORK/language" bash "$WORK/next.sh" 2>&1)
rc=$?

(( rc == 0 )) && ok "an install survives a kernel it cannot build for" \
              || bad "the block exited $rc; an install would have died here" "$out"

grep -q 'install -y -qq linux-headers-9.9.9-2-generic' "$CALLS" \
    && ok "it asks apt for that kernel's headers first" \
    || bad "no attempt to install headers for the next kernel" "$(cat "$CALLS")"

# Named, because "the module could not be built" sends an admin to look at a
# compiler, and the actual answer is one reboot into a kernel this machine has
# already installed.
[[ "$out" == *"9.9.9-2-generic"* ]] \
    && ok "the warning names the kernel and what to do about it" \
    || bad "the warning does not name the kernel" "$out"

is "the summary is told, so the closing report can say it too" \
   "$(printf '%s\n' "$out" | sed -n 's/^KMOD_NEXT=//p')" "9.9.9-2-generic"

printf '\n  %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
