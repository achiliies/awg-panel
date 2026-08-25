"""Host vitals, and the memory split the dashboard shows.

Everything here runs against a fake /proc and /sys built in tmp_path, which is
the reason those roots are module attributes rather than literals. The point is
not that Linux publishes these files - it does - but that the parsing survives
what real machines actually have: a kernel with no smaps_rollup, a module that
is not loaded at all, a userspace daemon where another box has a module. Every
one of those has to come back as a number rather than an exception, because this
code runs inside the collector's loop and the loop must not stop.

The two figures the UI puts side by side are a lower bound of the same kind on
both sides - kernel memory the module owns, userspace memory the panel's
processes own - which is what makes comparing them fair. A test that let one
side start counting something the other does not would break that quietly, so
the shapes are pinned here.
"""

import os
from pathlib import Path

import pytest

from awg import sysinfo

PAGE = os.sysconf("SC_PAGE_SIZE")

# A slabinfo with the tunnel's caches spelled out under their own names, which
# is the shape only a kernel booted with slab_nomerge ever has. It is here to be
# ignored: see the test below.
SLABINFO = """\
slabinfo - version: 2.1
# name            <active_objs> <num_objs> <objsize> <objperslab> <pagesperslab> : tunables
allowedips_node          64     72     64     64    1 : tunables    0    0    0
wg_peer                  12     16   1568      2    1 : tunables    0    0    0
kmalloc-512            1024   1088    512      8    1 : tunables    0    0    0
dentry                40960  41000    192     21    1 : tunables    0    0    0
"""


@pytest.fixture(autouse=True)
def fake_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a tree this test owns, and empty the PID cache.

    The cache is keyed by marker tuple and lives for the process, so without the
    reset the second test in a run would answer from the first one's fake /proc.
    """
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(sysinfo, "PROC", proc)
    monkeypatch.setattr(sysinfo, "SYS_MODULE", tmp_path / "sys" / "module")
    monkeypatch.setattr(sysinfo, "SYS_CLASS_NET", tmp_path / "sys" / "class" / "net")
    monkeypatch.setattr(sysinfo, "SYS_BLOCK", tmp_path / "sys" / "block")
    sysinfo.reset_process_cache()
    # The shared disk sampler holds one reading for the life of the process, and
    # a reading taken against the previous test's fake /proc would come out of
    # this one as a rate nobody wrote.
    sysinfo._disk_sampler.reset()
    yield proc
    sysinfo.reset_process_cache()


def write_process(
    proc: Path, pid: int, cmdline: str, *, pss_kb: int = 0, rss_pages: int = 0
) -> None:
    """A believable /proc/<pid>: NUL-separated cmdline, and one memory source."""
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_text(cmdline.replace(" ", "\0") + "\0", encoding="utf-8")
    if pss_kb:
        (entry / "smaps_rollup").write_text(
            f"55a0-7ffd ---p 00000000 00:00 0 [rollup]\nRss:  {pss_kb * 2} kB\nPss:  {pss_kb} kB\n",
            encoding="utf-8",
        )
    if rss_pages:
        (entry / "statm").write_text(
            f"{rss_pages * 3} {rss_pages} 200 1 0 100 0\n", encoding="utf-8"
        )


def diskstat(name: str, *, sectors_read: int = 0, sectors_written: int = 0, ticks: int = 0) -> str:
    """One /proc/diskstats line, with the three columns this module reads set.

    Twenty fields, as a kernel from 5.5 onward writes them. The ones this module
    does not read carry plausible constants rather than zeros, so a test that
    starts reading a new column cannot pass by accident.
    """
    # major minor name, then four columns of reads (completed, merged, sectors,
    # ms), the same four for writes, requests in flight, ms with anything in
    # flight, weighted ms, and finally the discard and flush columns.
    return (
        f"253 0 {name} 1000 12 {sectors_read} 340 "
        f"2000 34 {sectors_written} 560 "
        f"0 {ticks} 900 0 0 0 0 0 0"
    )


def write_disk(tmp_path: Path, name: str, *, hardware: bool = True) -> None:
    """A device in the fake /sys/block, with or without something behind it.

    `hardware=False` is dm-0, md0 or loop0: a device in its own right whose
    writes also land on a disk that is listed separately.
    """
    device = tmp_path / "sys" / "block" / name
    device.mkdir(parents=True)
    if hardware:
        (device / "device").mkdir()


def write_meminfo(proc: Path, **fields: int) -> None:
    """A /proc/meminfo with the named keys, in the kB the kernel publishes.

    MemTotal and MemFree are always there because a real one always has them,
    and a test that leaves them out is asking about swap rather than saying the
    machine has no memory.
    """
    lines = {"MemTotal": 1_000_000, "MemFree": 400_000, **fields}
    proc.joinpath("meminfo").write_text(
        "".join(f"{key}:{value:>16} kB\n" for key, value in lines.items()), encoding="utf-8"
    )


def statvfs(
    *, blocks: int, bfree: int, bavail: int, frsize: int = 4096, bsize: int = 4096
) -> os.statvfs_result:
    """What os.statvfs answers with, built from the four fields this module reads.

    bsize defaults to frsize because on an ordinary filesystem they are equal;
    the test that cares passes two different numbers on purpose.
    """
    return os.statvfs_result((bsize, frsize, blocks, bfree, bavail, 0, 0, 0, 0, 255))


class Clock:
    """A monotonic clock the test moves by hand, so a rate needs no sleeping."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------- the core


def test_the_module_size_is_read_from_sys_module(tmp_path: Path):
    module = tmp_path / "sys" / "module" / "amneziawg"
    module.mkdir(parents=True)
    (module / "coresize").write_text("233472\n", encoding="utf-8")
    # initsize is the __init section, which the kernel frees the moment the
    # module has loaded. Counting it would report memory nobody holds.
    (module / "initsize").write_text("16384\n", encoding="utf-8")

    assert sysinfo.module_memory() == 233472


def test_a_module_that_is_not_loaded_is_zero_rather_than_an_error():
    assert sysinfo.module_memory() == 0
    assert sysinfo.core_memory() == 0


@pytest.mark.parametrize("name", ["../../etc/passwd", "awg/0", ".hidden", ""])
def test_a_module_name_is_never_used_as_a_path(name: str):
    """The interface name reaches this from clients.env, so it is not ours."""
    assert sysinfo.module_memory(name) == 0


def test_the_core_ignores_slabinfo_even_when_it_names_the_tunnels_caches(
    fake_proc: Path, tmp_path: Path
):
    """The slab half was removed on purpose, and this is what stops it coming back.

    It used to add up every cache whose name began wg_ or allowedips_node, which
    reported a number nobody could get: SLUB merges caches by object size, so on
    an ordinary kernel those caches are billed to a root cache named for its
    size (":0000072") and the tunnel's names survive only as symlinks under
    /sys/kernel/slab. The scan matched nothing, the tunnel read as a flat
    coresize forever, and the docstring promised a figure that grew with
    traffic. Counting the fixture below would not fix that - it would only make
    a slab_nomerge box disagree with every other box about the same tunnel.
    """
    module = tmp_path / "sys" / "module" / "amneziawg"
    module.mkdir(parents=True)
    (module / "coresize").write_text("135168\n", encoding="utf-8")
    (fake_proc / "slabinfo").write_text(SLABINFO, encoding="utf-8")

    assert sysinfo.core_memory() == 135168


def test_a_userspace_tunnel_daemon_counts_towards_the_core(fake_proc: Path, tmp_path: Path):
    """An installation running amneziawg-go has no module to measure."""
    module = tmp_path / "sys" / "module" / "amneziawg"
    module.mkdir(parents=True)
    (module / "coresize").write_text("100000\n", encoding="utf-8")
    write_process(fake_proc, 7, "/usr/bin/amneziawg-go awg0", pss_kb=2048)

    assert sysinfo.core_memory() == 100000 + 2048 * 1024


# ------------------------------------------------------------------ the panel


def test_panel_memory_adds_up_the_web_workers_and_the_collector(fake_proc: Path):
    write_process(
        fake_proc, 10, "/opt/awg-panel/.venv/bin/python manage.py collector", pss_kb=40_000
    )
    write_process(
        fake_proc,
        11,
        "/opt/awg-panel/.venv/bin/gunicorn -c deploy/gunicorn.conf.py awgui.wsgi:application",
        pss_kb=30_000,
    )
    write_process(
        fake_proc,
        12,
        "/opt/awg-panel/.venv/bin/gunicorn -c deploy/gunicorn.conf.py awgui.wsgi:application",
        pss_kb=25_000,
    )
    # Somebody else's Python. The markers are specific for exactly this reason.
    write_process(fake_proc, 13, "/usr/bin/python3 /usr/bin/unattended-upgrade", pss_kb=90_000)

    assert sysinfo.panel_memory() == (40_000 + 30_000 + 25_000) * 1024


def test_pss_is_preferred_to_rss_so_forked_workers_are_not_counted_twice(fake_proc: Path):
    """A gunicorn worker shares nearly all of its pages with its parent."""
    write_process(fake_proc, 20, "gunicorn awgui.wsgi:application", pss_kb=30_000, rss_pages=25_000)

    assert sysinfo.panel_memory() == 30_000 * 1024


def test_a_kernel_without_smaps_rollup_falls_back_to_resident_size(fake_proc: Path):
    write_process(fake_proc, 21, "gunicorn awgui.wsgi:application", rss_pages=5_000)

    assert sysinfo.panel_memory() == 5_000 * PAGE


def test_a_process_with_no_memory_files_at_all_is_skipped(fake_proc: Path):
    write_process(fake_proc, 22, "gunicorn awgui.wsgi:application")

    assert sysinfo.panel_memory() == 0


# ------------------------------------------------------------------- the scan


def test_a_process_that_exits_stops_being_counted_immediately(fake_proc: Path):
    """gunicorn recycles workers; a cached PID must not keep its memory alive."""
    write_process(fake_proc, 30, "gunicorn awgui.wsgi:application", pss_kb=10_000)
    write_process(fake_proc, 31, "gunicorn awgui.wsgi:application", pss_kb=10_000)
    assert sysinfo.panel_memory() == 20_000 * 1024

    for name in ("cmdline", "smaps_rollup"):
        (fake_proc / "31" / name).unlink()
    (fake_proc / "31").rmdir()

    assert sysinfo.panel_memory() == 10_000 * 1024


def test_the_scan_is_cached_so_a_two_second_poll_does_not_walk_proc_every_time(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
):
    write_process(fake_proc, 40, "gunicorn awgui.wsgi:application", pss_kb=10_000)
    sysinfo.panel_memory()

    calls: list[tuple] = []
    monkeypatch.setattr(sysinfo, "_scan_pids", lambda markers: calls.append(markers) or [])

    assert sysinfo.panel_memory() == 10_000 * 1024
    assert calls == []


def test_an_unreadable_proc_is_an_empty_answer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sysinfo, "PROC", Path("/nonexistent-proc"))
    sysinfo.reset_process_cache()

    assert sysinfo.panel_memory() == 0


# ------------------------------------------------------------- the interface


def test_the_interface_index_is_read_from_sys_class_net(tmp_path: Path):
    """What the collector fingerprints a tunnel with: it changes on every
    down/up, so a remembered start time can be checked against it."""
    iface = tmp_path / "sys" / "class" / "net" / "awg0"
    iface.mkdir(parents=True)
    (iface / "ifindex").write_text("12\n", encoding="utf-8")

    assert sysinfo.iface_index("awg0") == 12


def test_an_interface_that_does_not_exist_has_no_index():
    """A server between install.sh and the first bring-up, not an error."""
    assert sysinfo.iface_index("awg0") == 0


@pytest.mark.parametrize("name", ["", "../../etc", ".hidden", "a/b"])
def test_an_interface_name_is_never_used_as_a_path(name: str):
    """The name arrives from clients.env, so it is checked before it becomes one."""
    assert sysinfo.iface_index(name) == 0


# --------------------------------------------------------------------- the disk


def test_a_rate_needs_two_readings_so_the_first_one_is_zero(fake_proc: Path, tmp_path: Path):
    """Every column in diskstats is a counter since boot, so one reading is a
    total rather than a rate, and reporting it as one would put a whole uptime
    of I/O on the dashboard as this second's."""
    write_disk(tmp_path, "vda")
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_read=999_999, sectors_written=999_999, ticks=500_000) + "\n",
        encoding="utf-8",
    )

    assert sysinfo.DiskSampler(Clock()).sample() == sysinfo.Disk(read=0, write=0, busy=0.0)


def test_the_rates_are_bytes_a_second_between_the_two_readings(fake_proc: Path, tmp_path: Path):
    write_disk(tmp_path, "vda")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_read=1_000, sectors_written=2_000) + "\n", encoding="utf-8"
    )
    sampler.sample()

    clock.now += 2.0
    (fake_proc / "diskstats").write_text(
        # 4096 sectors read and 2048 written in those two seconds, and half a
        # second of the two with something in flight.
        diskstat("vda", sectors_read=5_096, sectors_written=4_048, ticks=500) + "\n",
        encoding="utf-8",
    )

    reading = sampler.sample()
    assert reading.read == 4_096 * 512 // 2
    assert reading.write == 2_048 * 512 // 2
    assert reading.busy == 25.0


def test_a_partition_is_not_counted_beside_the_disk_it_is_cut_from(fake_proc: Path, tmp_path: Path):
    """diskstats lists both, and every write to vda1 is also a write to vda.

    /sys/block is what tells them apart: it holds whole devices, and a partition
    lives inside its own disk rather than beside it.
    """
    write_disk(tmp_path, "vda")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_written=1_000) + "\n" + diskstat("vda1", sectors_written=1_000),
        encoding="utf-8",
    )
    sampler.sample()

    clock.now += 1.0
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_written=3_000) + "\n" + diskstat("vda1", sectors_written=3_000),
        encoding="utf-8",
    )

    assert sampler.sample().write == 2_000 * 512


def test_a_stacked_device_is_not_counted_beside_the_hardware_underneath_it(
    fake_proc: Path, tmp_path: Path
):
    """dm-0 over LUKS over vda publishes the same writes twice, once each.

    Both are whole devices in /sys/block, so the partition test above does not
    catch this one; what separates them is that only vda has hardware behind it.
    A loop device is the same story with a file underneath instead of a disk.
    """
    write_disk(tmp_path, "vda")
    write_disk(tmp_path, "dm-0", hardware=False)
    write_disk(tmp_path, "loop0", hardware=False)
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    lines = [
        diskstat("vda", sectors_read=1_000),
        diskstat("dm-0", sectors_read=1_000),
        diskstat("loop0", sectors_read=1_000),
    ]
    (fake_proc / "diskstats").write_text("\n".join(lines), encoding="utf-8")
    sampler.sample()

    clock.now += 1.0
    lines = [
        diskstat("vda", sectors_read=1_512),
        diskstat("dm-0", sectors_read=1_512),
        diskstat("loop0", sectors_read=1_512),
    ]
    (fake_proc / "diskstats").write_text("\n".join(lines), encoding="utf-8")

    assert sampler.sample().read == 512 * 512


def test_busy_is_the_busiest_disk_rather_than_the_sum_of_them(fake_proc: Path, tmp_path: Path):
    """Two disks each half loaded are not one saturated disk, and a sum would
    say they were - or, on four of them, report 200%."""
    write_disk(tmp_path, "vda")
    write_disk(tmp_path, "sdb")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(diskstat("vda") + "\n" + diskstat("sdb"), encoding="utf-8")
    sampler.sample()

    clock.now += 1.0
    (fake_proc / "diskstats").write_text(
        diskstat("vda", ticks=600) + "\n" + diskstat("sdb", ticks=400), encoding="utf-8"
    )

    assert sampler.sample().busy == 60.0


def test_busy_never_reads_past_a_hundred(fake_proc: Path, tmp_path: Path):
    """A device can report more busy milliseconds than the wall clock had, and
    a dashboard meter has nowhere to draw 140%."""
    write_disk(tmp_path, "vda")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(diskstat("vda"), encoding="utf-8")
    sampler.sample()

    clock.now += 1.0
    (fake_proc / "diskstats").write_text(diskstat("vda", ticks=1_400), encoding="utf-8")

    assert sampler.sample().busy == 100.0


def test_a_counter_that_went_backwards_reports_nothing_rather_than_a_spike(
    fake_proc: Path, tmp_path: Path
):
    """A disk swapped out under its own name starts its counters again, and the
    subtraction that follows is not a reading of anything."""
    write_disk(tmp_path, "vda")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_read=100_000, ticks=90_000), encoding="utf-8"
    )
    sampler.sample()

    clock.now += 1.0
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_read=40, ticks=10), encoding="utf-8"
    )

    assert sampler.sample() == sysinfo.Disk(read=0, write=0, busy=0.0)


def test_a_disk_that_appeared_since_the_last_reading_waits_for_a_pair(
    fake_proc: Path, tmp_path: Path
):
    """Its counters are since boot, not since it was plugged in, so the first
    reading of it is a total. vda beside it still reports its own delta."""
    write_disk(tmp_path, "vda")
    write_disk(tmp_path, "sdb")
    clock = Clock()
    sampler = sysinfo.DiskSampler(clock)
    (fake_proc / "diskstats").write_text(diskstat("vda", sectors_read=1_000), encoding="utf-8")
    sampler.sample()

    clock.now += 1.0
    (fake_proc / "diskstats").write_text(
        diskstat("vda", sectors_read=1_100) + "\n" + diskstat("sdb", sectors_read=800_000),
        encoding="utf-8",
    )

    assert sampler.sample().read == 100 * 512


def test_a_machine_whose_disks_cannot_be_seen_reads_as_idle(fake_proc: Path):
    """A container with no /proc/diskstats and no /sys/block. Zeros, because
    this runs inside the collector's loop and the loop must not stop."""
    sampler = sysinfo.DiskSampler(Clock())
    sampler.sample()

    assert sampler.sample() == sysinfo.Disk(read=0, write=0, busy=0.0)


def test_two_readings_at_the_same_moment_are_not_divided_by_zero(fake_proc: Path, tmp_path: Path):
    """A clock that did not move between polls, which a coarse one can do."""
    write_disk(tmp_path, "vda")
    sampler = sysinfo.DiskSampler(Clock())
    (fake_proc / "diskstats").write_text(diskstat("vda", sectors_read=1_000), encoding="utf-8")
    sampler.sample()
    (fake_proc / "diskstats").write_text(diskstat("vda", sectors_read=9_000), encoding="utf-8")

    assert sampler.sample() == sysinfo.Disk(read=0, write=0, busy=0.0)


@pytest.mark.parametrize("name", ["../../etc/passwd", ".hidden", ""])
def test_a_device_name_is_never_used_as_a_path(name: str):
    """diskstats is the kernel's, but this one is cheap to hold to the same rule
    as the interface and module names."""
    assert sysinfo._is_whole_disk(name) is False


# --------------------------------------------------------------- the free space


def test_used_and_free_are_both_taken_from_the_kernel_rather_than_from_each_other(
    monkeypatch: pytest.MonkeyPatch,
):
    """A thousand blocks, two hundred unwritten, fifty of those reserved for root.

    The reserve is the whole point of this test. Used counts every block that
    has been written, so it comes from f_bfree; free is what an ordinary process
    may still write, so it comes from f_bavail. Deriving either from the other
    would silently promise fifty blocks that no process here can have.
    """
    monkeypatch.setattr(
        sysinfo.os, "statvfs", lambda _: statvfs(blocks=1_000, bfree=200, bavail=150)
    )

    space = sysinfo.storage()

    assert space.total == 1_000 * 4096
    assert space.used == 800 * 4096
    assert space.free == 150 * 4096
    # 800 of the 950 blocks anything can be written to, which is the figure df
    # prints - and a percentage over the total would read 80.0 and be kinder
    # than the filesystem is.
    assert space.percent == 84.2


def test_a_filesystem_with_nothing_left_reads_as_full_rather_than_as_nearly_full(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every block an ordinary process may write is gone, and the reserve it may
    not touch is still there. Writes are failing; the bar has to say so."""
    monkeypatch.setattr(sysinfo.os, "statvfs", lambda _: statvfs(blocks=1_000, bfree=50, bavail=0))

    assert sysinfo.storage().percent == 100.0


def test_the_block_count_is_scaled_by_the_fragment_size_not_the_io_size(
    monkeypatch: pytest.MonkeyPatch,
):
    """f_bsize is what the filesystem would rather be asked for; f_frsize is what
    the block counts are actually in. They differ, and using the wrong one
    misreports every figure here by whatever the ratio happens to be."""
    monkeypatch.setattr(
        sysinfo.os,
        "statvfs",
        lambda _: statvfs(blocks=1_000, bfree=400, bavail=400, frsize=1024, bsize=4096),
    )

    assert sysinfo.storage().total == 1_000 * 1024


def test_a_filesystem_that_cannot_be_measured_is_zeros_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """This runs inside the collector's loop, and the loop must not stop over a
    figure the dashboard would have drawn a bar with."""

    def refuse(_):
        raise OSError("no such file or directory")

    monkeypatch.setattr(sysinfo.os, "statvfs", refuse)

    assert sysinfo.storage() == sysinfo.Storage(total=0, used=0, free=0, percent=0.0)


def test_a_filesystem_with_no_blocks_at_all_is_zeros(monkeypatch: pytest.MonkeyPatch):
    """A pseudo filesystem answers statvfs with zeros; dividing by that would be
    the one way this function could raise."""
    monkeypatch.setattr(sysinfo.os, "statvfs", lambda _: statvfs(blocks=0, bfree=0, bavail=0))

    assert sysinfo.storage() == sysinfo.Storage(total=0, used=0, free=0, percent=0.0)


def test_the_filesystem_measured_is_the_one_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The default is the root filesystem, and a caller may name another."""
    asked: list[object] = []
    monkeypatch.setattr(
        sysinfo.os,
        "statvfs",
        lambda path: asked.append(path) or statvfs(blocks=8, bfree=4, bavail=4),
    )

    sysinfo.storage()
    sysinfo.storage(tmp_path)

    assert asked == [sysinfo.FILESYSTEM, tmp_path]


# --------------------------------------------------------------------- the swap


def test_swap_used_is_the_total_less_what_is_free(fake_proc: Path):
    write_meminfo(fake_proc, SwapTotal=2_097_152, SwapFree=1_572_864)

    swap = sysinfo.swap()

    assert swap.total == 2_097_152 * 1024
    assert swap.used == 524_288 * 1024
    assert swap.free == 1_572_864 * 1024
    assert swap.percent == 25.0


def test_swap_cached_pages_are_still_counted_as_used(fake_proc: Path):
    """SwapCached is pages that are in RAM and on the disk both. `free` does not
    subtract them and neither does this: the question is how much of the swap
    device is spoken for, not how much of it would have to be read back."""
    write_meminfo(fake_proc, SwapTotal=1_000_000, SwapFree=400_000, SwapCached=300_000)

    assert sysinfo.swap().used == 600_000 * 1024


def test_a_machine_with_no_swap_is_zeros_rather_than_an_empty_device(fake_proc: Path):
    """Nothing configured and a device with nothing on it are different claims,
    and a total of zero is how the UI tells them apart."""
    write_meminfo(fake_proc, SwapTotal=0, SwapFree=0)

    assert sysinfo.swap() == sysinfo.Swap(total=0, free=0, used=0, percent=0.0)


def test_a_meminfo_that_cannot_be_read_reports_no_swap(fake_proc: Path):
    """A container without /proc/meminfo. Zeros, not an exception."""
    assert sysinfo.swap() == sysinfo.Swap(total=0, free=0, used=0, percent=0.0)


def test_swap_free_larger_than_the_total_is_clamped(fake_proc: Path):
    """Two lines read a moment apart on a machine whose swap was being resized.
    Clamped rather than answered with a negative amount in use."""
    write_meminfo(fake_proc, SwapTotal=1_000, SwapFree=9_000)

    swap = sysinfo.swap()

    assert swap.used == 0
    assert swap.free == 1_000 * 1024


# ---------------------------------------------------------------- the snapshot


def test_the_snapshot_carries_both_figures_in_bytes(fake_proc: Path, tmp_path: Path):
    """MiB would round the tunnel's few hundred kilobytes away to a flat zero."""
    module = tmp_path / "sys" / "module" / "amneziawg"
    module.mkdir(parents=True)
    (module / "coresize").write_text("233472\n", encoding="utf-8")
    write_process(fake_proc, 50, "gunicorn awgui.wsgi:application", pss_kb=64_000)

    snapshot = sysinfo.snapshot(wan="")

    assert snapshot["memCore"] == 233472
    assert snapshot["memPanel"] == 64_000 * 1024
    # The two host figures next to them stay in mebibytes, as the UI expects.
    assert set(snapshot) == {
        "cpu",
        "memUsed",
        "memTotal",
        "memCore",
        "memPanel",
        "swapUsed",
        "swapTotal",
        "uptime",
        "load",
        "diskRead",
        "diskWrite",
        "diskBusy",
        "diskUsed",
        "diskFree",
        "diskTotal",
        "wanRx",
        "wanTx",
    }


def test_the_snapshot_carries_the_free_space_and_the_swap_in_bytes(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bytes for both, unlike memUsed and memTotal beside them.

    One unit has to cover a few hundred megabytes of swap and a few hundred
    gigabytes of disk, and mebibytes on the small end of that would be a
    conversion the UI only has to undo.
    """
    write_meminfo(fake_proc, SwapTotal=524_288, SwapFree=131_072)
    monkeypatch.setattr(
        sysinfo.os,
        "statvfs",
        lambda _: statvfs(blocks=10_000_000, bfree=4_000_000, bavail=3_500_000),
    )

    snapshot = sysinfo.snapshot(wan="")

    assert snapshot["swapUsed"] == 393_216 * 1024
    assert snapshot["swapTotal"] == 524_288 * 1024
    assert snapshot["diskUsed"] == 6_000_000 * 4096
    assert snapshot["diskFree"] == 3_500_000 * 4096
    assert snapshot["diskTotal"] == 10_000_000 * 4096
