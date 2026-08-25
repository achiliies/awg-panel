"""Host vitals for the dashboard, read straight from /proc and /sys.

psutil would put a compiled dependency on a box whose only job is a VPN, for
numbers that are four files away. Everything here is best effort: a container
without /proc/loadavg, a kernel without a steal-time column, an interface that
was renamed - all of them degrade to zeros rather than break the collector's
loop.

The memory split - what the tunnel costs against what the panel costs - is the
one number here that is not simply read off a file, so what it counts is worth
stating:

* The tunnel is a kernel module, so what can be measured is the module's own
  code and data (/sys/module/<mod>/coresize), plus the resident size of a
  userspace tunnel daemon on an installation that runs one instead. What the
  module allocates while carrying traffic cannot be measured, and the reason is
  worth writing down because it looks like an oversight: packet queues, the
  allowedips node per route per peer and the per-peer structs all come out of
  slab caches, and SLUB merges caches by object size and flags. A merged cache
  is billed to whichever cache of that size was created first, and only that
  root cache is named in /proc/slabinfo - the module's own names survive as
  symlinks under /sys/kernel/slab and nowhere else. So there is nothing there to
  attribute, and inventing a per-peer figure instead would be worse than leaving
  it out.
* The panel is ordinary userspace: its gunicorn workers and its collector, added
  up by PSS so the pages a forked worker shares with its parent are counted once
  rather than once per worker.

Both are honest lower bounds, which is what makes them comparable - and the
comparison is the point, because on a box with a gigabyte of RAM it is never the
VPN that is using it. The tunnel's side barely moves, and that is the finding
rather than a fault in it: a kernel module's footprint is its compiled text and
data, and carrying more clients does not shift it anywhere the kernel will
admit to.

Disk is two separate questions and this module answers both, because the answers
come from different places and one is no use for the other. How full the disk is
comes from statvfs on a mount point, and it is the one figure here that predicts
an outage: a full filesystem stops the collector writing traffic.db and the
tunnel writing its config long before anything else notices. How hard the disk
is working comes from /proc/diskstats, and says nothing about how much room is
left on it.

The filesystem measured is the root one, and only that one. A box that separates
/var or /home has a second answer this does not give, but the panel's own state
lives under /var/lib and /run and the tunnel's under /etc, so on the installs
this project makes - and on nearly every VPS - the root filesystem is where all
of it lands. Listing every mount instead would mean deciding which of tmpfs,
overlay, bind mounts and a docker layer is worth a line on a dashboard, and
getting that wrong is worse than answering the one question that is always the
right one to ask.

Used and free do not add up to total, and that is not an error to be corrected:
ext4 keeps a percentage back for root, so a filesystem full to an ordinary
process still has blocks free. Both are reported as the kernel gives them, and
the percentage is used over used-plus-available - what `df` prints, so an
operator comparing the two sees the same number rather than one a few points
kinder.

Swap is read straight out of /proc/meminfo, and used is total minus free the way
`free` computes it. SwapCached is deliberately not subtracted: those pages are
in RAM and still on the disk, and the question this answers is how much of the
swap device is spoken for, not how much of it would have to be read back.

The disk rate figures are rates rather than totals, and which devices they add up
is the one decision behind them. /proc/diskstats lists partitions beside the disk
they are cut from, and stacked devices - dm over LUKS, md over two members, a
loop over a file - beside the hardware their writes land on, so summing the file
counts a single write two or three times. Only whole devices with something
physical behind them are counted: /sys/block lists whole devices and nothing
else, and the `device` link inside each is what separates vda from dm-0, md0 and
loop0. Busy is then the busiest device's share of wall time rather than the sum
over devices, because two disks each half loaded are not one saturated disk.

PROC, SYS_BLOCK, SYS_CLASS_NET and SYS_MODULE are module attributes rather than
literals so a test can point them at a fixture tree.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROC = Path("/proc")
SYS_BLOCK = Path("/sys/block")
SYS_CLASS_NET = Path("/sys/class/net")
SYS_MODULE = Path("/sys/module")

# The filesystem whose free space is worth a line on the dashboard: see this
# module's docstring for why it is this one and not a list of every mount.
FILESYSTEM = Path("/")

_MIB = 1024 * 1024

# /proc/diskstats counts in 512-byte sectors whatever the device's own sector
# size is: the kernel converts, so a disk with 4K hardware sectors is still
# reported here in units of 512 bytes.
_SECTOR = 512

# The kernel module install.sh builds. Spelled the same way in awg.controller,
# which is not imported here: this module deliberately depends on nothing but
# the filesystem, so the collector can read vitals with the tools missing.
MODULE = "amneziawg"

# Command lines that mean "this process is part of the panel". The unit files
# run gunicorn on awgui.wsgi and `manage.py collector`; the last two entries
# catch a development server and the CLI, so the figure means the same thing on
# a laptop as it does on a server.
PANEL_PROCESSES: tuple[str, ...] = (
    "awgui.wsgi",
    "awgui.asgi",
    "manage.py collector",
    "manage.py runserver",
    "awg-panel",
)

# A userspace tunnel, for an installation that runs one instead of the kernel
# module. Normally nothing matches and the module is the whole story.
CORE_PROCESSES: tuple[str, ...] = ("amneziawg-go", "awg-go", "wireguard-go")

# How long a set of matching PIDs is trusted before /proc is walked again. The
# collector asks every couple of seconds and the answer almost never changes;
# a process that exits invalidates the entry immediately, but one that starts
# is picked up on the next sweep rather than instantly.
SCAN_TTL = 15.0

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


@dataclass(frozen=True)
class Memory:
    """Physical memory in bytes, plus the MiB figures the UI shows."""

    total: int
    available: int
    used: int
    percent: float

    @property
    def total_mb(self) -> int:
        return self.total // _MIB

    @property
    def available_mb(self) -> int:
        return self.available // _MIB

    @property
    def used_mb(self) -> int:
        return self.used // _MIB


@dataclass(frozen=True)
class Swap:
    """The swap device, in bytes. All zeros on a machine that has none.

    `used` is total minus free, which is what `free` reports and what the
    dashboard draws; a box with no swap configured has a total of 0 and is shown
    as having none rather than as being empty.
    """

    total: int
    free: int
    used: int
    percent: float


@dataclass(frozen=True)
class Storage:
    """How full a filesystem is, in bytes.

    `free` is what an ordinary process may still write, not what the filesystem
    has unallocated: ext4 holds a percentage back for root, so `used + free` is
    normally a little under `total`. `percent` is used over used-plus-free for
    the same reason - the disk is full when a process can no longer write to it,
    which is the point `df` calls 100%.
    """

    total: int
    used: int
    free: int
    percent: float


_NO_SWAP = Swap(total=0, free=0, used=0, percent=0.0)
_NO_STORAGE = Storage(total=0, used=0, free=0, percent=0.0)


@dataclass(frozen=True)
class Disk:
    """What the machine's block devices did between two readings.

    Rates in bytes per second, and the share of wall time the busiest device
    had at least one request in flight. All three are zero when there is nothing
    to compare against yet, which is the first reading and any reading taken
    after the counters went backwards.
    """

    read: int
    write: int
    busy: float


_IDLE_DISK = Disk(read=0, write=0, busy=0.0)


class CpuSampler:
    """Turns the monotonic counters in /proc/stat into a percentage.

    One reading says nothing: percentage is work done between two readings, so
    the first call has no answer and returns 0.0. The collector keeps one
    sampler for the whole process; the lock is there because gunicorn serves
    /api/v1/stats/live from threads and two interleaved samples would each
    report a fraction of the real load.
    """

    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def sample(self) -> float:
        """Busy CPU percentage since the previous call. 0.0 on the first call."""
        reading = _cpu_times()
        if reading is None:
            return 0.0
        with self._lock:
            prev, self._prev = self._prev, reading
        if prev is None:
            return 0.0
        busy_delta = reading[0] - prev[0]
        total_delta = reading[1] - prev[1]
        # A negative delta means the counters were reset under us (a container
        # migrated, or PROC was repointed); report nothing rather than a spike.
        if total_delta <= 0 or busy_delta < 0:
            return 0.0
        return round(min(100.0, 100.0 * busy_delta / total_delta), 1)

    def reset(self) -> None:
        """Forget the previous reading, so the next sample starts a new pair."""
        with self._lock:
            self._prev = None


class DiskSampler:
    """Turns the counters in /proc/diskstats into rates.

    The same shape as CpuSampler, for the same reason: everything in that file
    is a counter that only ever climbs, so one reading says nothing and the
    first call has no answer. Unlike the CPU, though, there are no idle ticks to
    divide by - a disk publishes what it did, not what it could have done - so
    this one has to hold a clock as well, and the rate is per second of wall
    time between the two readings.

    The clock is a constructor argument so a test can hand it a series of
    moments rather than sleeping through them. The lock is there for the same
    reason CpuSampler has one: two interleaved samples would each report a
    fraction of the traffic and neither would be right.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._prev: tuple[float, dict[str, tuple[int, int, int]]] | None = None
        self._lock = threading.Lock()

    def sample(self) -> Disk:
        """Read and write rates since the previous call, and how busy that kept
        the busiest device. Zeros on the first call."""
        now = self._clock()
        counters = _disk_counters()
        with self._lock:
            prev, self._prev = self._prev, (now, counters)
        if prev is None:
            return _IDLE_DISK
        elapsed = now - prev[0]
        if elapsed <= 0:
            return _IDLE_DISK

        read = 0
        write = 0
        busy = 0.0
        for name, (sectors_read, sectors_written, ticks) in counters.items():
            before = prev[1].get(name)
            # A device that was not there last time has no delta to take. Its
            # counters start wherever the kernel had them, and treating those as
            # a delta would report a boot's worth of I/O as one poll's.
            if before is None:
                continue
            delta_read = sectors_read - before[0]
            delta_write = sectors_written - before[1]
            delta_ticks = ticks - before[2]
            # Counters going backwards means this is not the device it was: one
            # unplugged and another given its name, or PROC repointed under a
            # test. Report nothing for it rather than a spike.
            if delta_read < 0 or delta_write < 0 or delta_ticks < 0:
                continue
            read += delta_read * _SECTOR
            write += delta_write * _SECTOR
            busy = max(busy, delta_ticks / (elapsed * 1000.0))

        return Disk(
            read=int(read / elapsed),
            write=int(write / elapsed),
            # Over 100 is possible on a device that reports its busy time
            # generously, or across a poll the clock jumped through; the figure
            # means "saturated" either way.
            busy=round(min(100.0, 100.0 * busy), 1),
        )

    def reset(self) -> None:
        """Forget the previous reading, so the next sample starts a new pair."""
        with self._lock:
            self._prev = None


_sampler = CpuSampler()
_disk_sampler = DiskSampler()


def cpu_percent() -> float:
    """Busy CPU percentage since this function last ran in this process.

    Returns 0.0 the first time, because there is nothing to compare against.
    Callers that need their own timeline (a one-shot CLI, a test) should hold
    their own CpuSampler instead of sharing this one.
    """
    return _sampler.sample()


def disk() -> Disk:
    """Disk read and write rates since this function last ran in this process.

    Zeros the first time, and zeros forever on a machine whose block devices
    this process cannot see - a container with no /sys/block, most often. Same
    caveat as cpu_percent: a caller wanting its own timeline holds its own
    DiskSampler rather than sharing this one.
    """
    return _disk_sampler.sample()


def mem() -> Memory:
    """Memory usage as `free` reports it: used is what is not available."""
    fields = _meminfo()
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable")
    if available is None:
        # Kernels older than 3.14 have no MemAvailable, and neither do some
        # container runtimes; free+buffers+cache is what `free` used before it.
        available = fields.get("MemFree", 0) + fields.get("Buffers", 0) + fields.get("Cached", 0)
    available = max(0, min(available, total))
    used = total - available
    percent = round(100.0 * used / total, 1) if total else 0.0
    return Memory(total=total, available=available, used=used, percent=percent)


def swap() -> Swap:
    """Swap usage as `free` reports it: used is total minus free.

    Zeros on a machine with no swap, and zeros again on one whose /proc/meminfo
    cannot be read - the two are the same answer to the UI, which draws neither.
    """
    fields = _meminfo()
    total = fields.get("SwapTotal", 0)
    if total <= 0:
        return _NO_SWAP
    free = max(0, min(fields.get("SwapFree", 0), total))
    used = total - free
    return Swap(total=total, free=free, used=used, percent=round(100.0 * used / total, 1))


def storage(path: Path | None = None) -> Storage:
    """How full the filesystem holding `path` is. Zeros when it cannot be read.

    Defaults to the root filesystem, which on the installs this project makes is
    the one everything lands on: the panel's database under /var/lib, its live
    state under /run, the tunnel's configuration under /etc.

    Unreadable means a path that is gone or a kernel without statvfs, both of
    which are answered with zeros rather than an exception: this runs inside the
    collector's loop, and the loop must not stop over a dashboard figure.
    """
    try:
        stat = os.statvfs(path or FILESYSTEM)
    except (OSError, ValueError):
        return _NO_STORAGE
    # f_frsize is the unit the block counts are in. f_bsize is the preferred I/O
    # size and is not always the same number, so using it would quietly misreport
    # every figure here on a filesystem where they differ.
    unit = stat.f_frsize or stat.f_bsize
    total = stat.f_blocks * unit
    if total <= 0:
        return _NO_STORAGE
    # f_bfree counts blocks nobody has written; f_bavail leaves out the ones
    # reserved for root. Used comes from the first - the reserve is not in use -
    # and free from the second, which is what a process may still write.
    used = max(0, (stat.f_blocks - stat.f_bfree) * unit)
    free = max(0, stat.f_bavail * unit)
    writable = used + free
    return Storage(
        total=total,
        used=used,
        free=free,
        percent=round(100.0 * used / writable, 1) if writable else 100.0,
    )


def core_memory(module: str = MODULE) -> int:
    """Bytes of RAM the tunnel itself occupies, in the kernel and in userspace.

    The module's code and data, plus the resident size of a userspace tunnel
    daemon if this installation runs one instead of the module. Zero is a real
    answer: it means neither of them is there.

    Expect this to sit still. On a kernel-module install it is the module's
    compiled size, which moves when the module is rebuilt or reloaded and at no
    other time - not with the client count, and not with traffic. That is not a
    stale reading; see this module's docstring for why the part that does grow
    is not attributable to the module at all.
    """
    return module_memory(module) + process_memory(CORE_PROCESSES)


def panel_memory() -> int:
    """Bytes of RAM the panel's own processes hold: web workers and collector.

    PSS, not RSS. gunicorn's workers are forks that share most of their pages
    with the parent, and adding up RSS would count the interpreter, Django and
    every loaded module once per worker - a number two to three times the truth.
    """
    return process_memory(PANEL_PROCESSES)


def module_memory(module: str = MODULE) -> int:
    """Size of a loaded kernel module's code and data, from /sys/module.

    coresize only. initsize covers the __init section, which the kernel frees
    once the module has loaded, so counting it would report memory nobody holds.
    """
    if not module or "/" in module or module.startswith("."):
        return 0
    return _read_int(SYS_MODULE / module / "coresize")


def process_memory(markers: tuple[str, ...]) -> int:
    """Bytes held by every running process whose command line names one of `markers`.

    PSS where the kernel publishes it, resident size where it does not, so the
    answer degrades on an old kernel rather than disappearing.
    """
    return sum(_pid_memory(pid) for pid in _matching_pids(tuple(markers)))


def reset_process_cache() -> None:
    """Forget which PIDs matched. For tests, and for a process that just forked."""
    with _scan_lock:
        _scan_cache.clear()


def uptime() -> float:
    """Seconds since boot. 0.0 when /proc/uptime cannot be read."""
    text = _read_text(PROC / "uptime")
    if not text:
        return 0.0
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return 0.0


def load() -> list[float]:
    """The 1, 5 and 15 minute load averages. Zeros when unavailable."""
    text = _read_text(PROC / "loadavg")
    if not text:
        return [0.0, 0.0, 0.0]
    try:
        return [float(value) for value in text.split()[:3]]
    except ValueError:
        return [0.0, 0.0, 0.0]


def net_bytes(iface: str) -> tuple[int, int]:
    """Total (rx, tx) bytes on an interface since boot. (0, 0) if it is gone.

    The name arrives from the default route or from clients.env, so it is
    checked before it becomes a path: /sys/class/net/../../ resolves to real
    files.
    """
    if not iface or "/" in iface or iface.startswith("."):
        return (0, 0)
    stats = SYS_CLASS_NET / iface / "statistics"
    return (_read_int(stats / "rx_bytes"), _read_int(stats / "tx_bytes"))


def iface_index(iface: str) -> int:
    """The kernel's index for an interface, or 0 when there is no such interface.

    Deliberately not a stable identity. The kernel hands out the next free index
    when a device is created, so `awg-quick down && up` gives the tunnel a new
    one - and that is what makes this worth reading. Nothing records when a
    netdev was created, so the collector has to remember the moment itself, and
    the index is the cheapest way to tell "the same tunnel, still up" from "a
    tunnel with the same name that was restarted while nobody was watching".

    Indexes start again from low numbers after a reboot, so a caller comparing
    one against a remembered value has to check that the memory belongs to this
    boot as well; on its own this only ever narrows the question.
    """
    if not iface or "/" in iface or iface.startswith("."):
        return 0
    return _read_int(SYS_CLASS_NET / iface / "ifindex")


def default_iface() -> str | None:
    """Interface holding the default route - the one install.sh masquerades out of.

    Parsed from /proc/net/route instead of `ip route show default` so that a
    two-second poll never forks. Lowest metric wins, which is the order `ip`
    prints its routes in.
    """
    text = _read_text(PROC / "net" / "route")
    if not text:
        return None
    best: tuple[int, str] | None = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        # Destination and genmask both all-zero is the default route.
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            metric = int(fields[6])
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, fields[0])
    return best[1] if best else None


def snapshot(wan: str | None = None) -> dict[str, Any]:
    """The "system" block of live.json, already in the shape the UI expects.

    camelCase here and nowhere else in this package: the collector writes this
    dict into live.json and the API serves that file back unchanged.
    """
    if wan is None:
        wan = default_iface()
    rx, tx = net_bytes(wan) if wan else (0, 0)
    memory = mem()
    swapping = swap()
    space = storage()
    io = disk()
    return {
        "cpu": cpu_percent(),
        "memUsed": memory.used_mb,
        "memTotal": memory.total_mb,
        # Bytes, like the two below them and unlike the two above: swap on a
        # small VPS is often a few hundred megabytes, and the disk figures next
        # to it are tens of gigabytes, so one unit has to cover both ends and
        # bytes is the only one that does without rounding either away.
        "swapUsed": swapping.used,
        "swapTotal": swapping.total,
        # Bytes, unlike the two above: the tunnel's footprint is measured in
        # hundreds of kilobytes and would round to a flat 0 MiB all day.
        "memCore": core_memory(),
        "memPanel": panel_memory(),
        "uptime": int(uptime()),
        "load": load(),
        # Bytes per second since the previous snapshot, and the percentage of
        # that stretch the busiest disk spent working. Rates rather than the
        # counters themselves, because the counters are since boot and a reader
        # polling this file has no second reading to subtract.
        "diskRead": io.read,
        "diskWrite": io.write,
        "diskBusy": io.busy,
        # How full the root filesystem is, which is a different question from
        # the three above and the one that predicts an outage. Used and free are
        # both as the kernel gives them and do not add up to total: see this
        # module's docstring for the reserve that accounts for the difference.
        "diskUsed": space.used,
        "diskFree": space.free,
        "diskTotal": space.total,
        "wanRx": rx,
        "wanTx": tx,
    }


def _cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies from the aggregate cpu line, or None if unreadable."""
    text = _read_text(PROC / "stat")
    if not text:
        return None
    line = next((ln for ln in text.splitlines() if ln.startswith("cpu ")), None)
    if line is None:
        return None
    try:
        values = [int(value) for value in line.split()[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    # Columns after the fourth are kernel-version dependent; pad the ones this
    # kernel does not publish with zeros.
    values += [0] * (10 - len(values))
    user, nice, system, idle, iowait = values[0:5]
    irq, softirq, steal, guest, guest_nice = values[5:10]
    # guest is already counted inside user, guest_nice inside nice; leaving them
    # in would inflate the total and understate load on a virtualised host.
    total = (user - guest) + (nice - guest_nice) + system + idle + iowait + irq + softirq + steal
    return total - idle - iowait, total


def _disk_counters() -> dict[str, tuple[int, int, int]]:
    """Per whole device: (sectors read, sectors written, ms with I/O in flight).

    The three columns of /proc/diskstats worth having. Fields are positional and
    have only ever been appended to - discards arrived in 4.18, flushes in 5.5 -
    so a line is taken when it is long enough to hold the thirteenth column and
    skipped when it is not, which is what a 2.6 kernel's shorter partition lines
    are.
    """
    text = _read_text(PROC / "diskstats")
    if not text:
        return {}
    counters: dict[str, tuple[int, int, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        # major, minor, name, then four columns per direction; sectors read is
        # the sixth, sectors written the tenth, and time with anything in flight
        # the thirteenth.
        if len(fields) < 13 or not _is_whole_disk(fields[2]):
            continue
        try:
            counters[fields[2]] = (int(fields[5]), int(fields[9]), int(fields[12]))
        except ValueError:
            continue
    return counters


def _is_whole_disk(name: str) -> bool:
    """Whether `name` is a device whose counters should be added up once.

    /sys/block lists whole devices and puts each partition inside its own disk,
    so a name missing from it is a partition and adding it would count the same
    write twice. The `device` link inside is the second half of the test: dm-0
    over a LUKS volume, md0 over its members and loop0 over a file all appear in
    /sys/block as devices in their own right, and every byte they report also
    lands on the hardware underneath. Only what has hardware behind it has that
    link, which leaves each write counted exactly once.
    """
    if not name or "/" in name or name.startswith("."):
        return False
    return (SYS_BLOCK / name / "device").exists()


# PIDs last seen matching a marker set: {markers: (monotonic time, pids)}.
_scan_cache: dict[tuple[str, ...], tuple[float, list[int]]] = {}
_scan_lock = threading.Lock()


def _matching_pids(markers: tuple[str, ...]) -> list[int]:
    """PIDs whose command line contains one of `markers`, cached for SCAN_TTL.

    Walking /proc means one small read per process on the box, and the collector
    asks every couple of seconds forever. The cache is dropped early when any
    remembered process is gone - a recycled gunicorn worker must not keep being
    counted - so the only thing the TTL delays is noticing a new one.
    """
    now = time.monotonic()
    with _scan_lock:
        hit = _scan_cache.get(markers)
    if hit is not None and now - hit[0] < SCAN_TTL:
        if all((PROC / str(pid)).exists() for pid in hit[1]):
            return hit[1]

    found = _scan_pids(markers)
    with _scan_lock:
        _scan_cache[markers] = (now, found)
    return found


def _scan_pids(markers: tuple[str, ...]) -> list[int]:
    """Every process whose /proc/<pid>/cmdline mentions one of `markers`."""
    found: list[int] = []
    try:
        entries = sorted(entry for entry in os.listdir(PROC) if entry.isdigit())
    except OSError:
        return found
    for entry in entries:
        # NUL separated on disk; joined with spaces so a marker can span two
        # arguments the way "manage.py collector" does.
        raw = _read_text(PROC / entry / "cmdline")
        if not raw:
            continue
        cmdline = raw.replace("\0", " ")
        if any(marker in cmdline for marker in markers):
            found.append(int(entry))
    return found


def _pid_memory(pid: int) -> int:
    """One process's share of physical memory, in bytes.

    PSS from smaps_rollup when the kernel has it: it divides every shared page
    by the number of processes holding it, which is the only way a sum over a
    gunicorn parent and its forks means anything. Resident size otherwise.
    """
    for line in (_read_text(PROC / str(pid) / "smaps_rollup") or "").splitlines():
        if line.startswith("Pss:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                break
    # /proc/<pid>/statm: size, resident, shared, ... in pages.
    fields = (_read_text(PROC / str(pid) / "statm") or "").split()
    try:
        return int(fields[1]) * _PAGE_SIZE
    except (IndexError, ValueError):
        return 0


def _meminfo() -> dict[str, int]:
    """/proc/meminfo as bytes per key. Values are published in kB."""
    text = _read_text(PROC / "meminfo")
    if not text:
        return {}
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        fields[key] = value * 1024 if parts[1:2] == ["kB"] else value
    return fields


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_int(path: Path) -> int:
    text = _read_text(path)
    try:
        return int((text or "").strip())
    except ValueError:
        return 0
