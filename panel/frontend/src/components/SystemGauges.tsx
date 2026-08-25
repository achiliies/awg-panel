import * as React from "react";
import { ChartPieSlice, Clock, Cpu, Database, Gauge, HardDrive, Memory, Swap } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn, formatDuration, formatMemory, formatRate, percent } from "@/lib/utils";
import type { SystemStats } from "@/api/types";

/*
 * Host health, not tunnel health.
 *
 * These numbers come from /proc on the machine, so they stay true and worth
 * showing even when the interface is down: an operator whose tunnel died
 * because the box ran out of memory needs to see that here.
 *
 * Load average gets no bar. A bar needs a maximum, the meaningful maximum is
 * the core count, and the collector does not report one. Drawing a bar against
 * a guessed maximum would be a made-up number, so the figures stand alone.
 *
 * Disk activity gets one, and the same rule is why: the share of time the
 * busiest disk spent working is a percentage of a real thing, and a disk at 90%
 * is the reason a box feels stuck long before its CPU says anything. The two
 * rates underneath are what it was doing, and they get no bar - a disk has no
 * advertised ceiling in bytes a second, and the one it does have is not a
 * number this panel can read anywhere.
 *
 * Disk space is the other card on this subject and the one that matters most
 * rarely and most sharply: a filesystem with nothing left on it stops the
 * collector writing traffic.db and the tunnel writing its config, and every
 * other figure here goes on looking healthy while it happens. It sits above
 * disk activity because it is the slower-moving of the two, and it is drawn as
 * a bar with the three figures underneath because "how much is left" is the
 * question people bring to it, and a percentage on its own does not answer that
 * on a machine whose disk they cannot remember the size of.
 *
 * Swap gets a bar for the same reason memory does, and sits beside it because
 * it is the same shortage one step further along: pages the machine could not
 * keep in RAM. A server with no swap says so rather than showing an empty bar,
 * which would read as "plenty spare" when the truth is there is none to spare.
 *
 * The memory split answers the question people actually ask on a small VPS -
 * which of these two things is eating my RAM - and the answer is almost always
 * the panel, by two orders of magnitude. Both figures are lower bounds of the
 * same kind (see awg/sysinfo.py), so they are comparable with each other; they
 * do not add up to the memory bar above them, and are not drawn as if they do.
 */

/** Where a meter stops being informational and starts being a warning. */
const WARN_PERCENT = 75;
const DANGER_PERCENT = 90;

interface MeterTone {
  indicator: string;
  track: string;
  text: string;
}

/*
 * The unfilled track is a light step of the fill's own colour rather than a
 * neutral grey, so the whole bar carries the state at a glance instead of only
 * the filled part.
 */
const TONES: Record<"normal" | "warn" | "danger", MeterTone> = {
  normal: { indicator: "bg-chart-1", track: "bg-chart-1/15", text: "text-foreground" },
  warn: { indicator: "bg-warning", track: "bg-warning/15", text: "text-warning" },
  danger: { indicator: "bg-destructive", track: "bg-destructive/15", text: "text-destructive" },
};

function toneFor(value: number): MeterTone {
  if (value >= DANGER_PERCENT) {
    return TONES.danger;
  }
  if (value >= WARN_PERCENT) {
    return TONES.warn;
  }
  return TONES.normal;
}

interface SectionLabelProps {
  icon: typeof Cpu;
  label: string;
  /** Explanation shown on hover and on keyboard focus. */
  hint: string;
}

/**
 * The name of one reading, with the sentence that says what it counts.
 *
 * Every section in this card carries one, and they all have to behave the same
 * way: focusable, so the explanation is reachable without a pointer, and
 * narrow, so a long hint wraps into a tooltip rather than across the viewport.
 */
function SectionLabel({ icon: Icon, label, hint }: SectionLabelProps): JSX.Element {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="inline-flex items-center gap-2 rounded-sm text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <Icon weight="duotone" className="h-4 w-4 shrink-0" aria-hidden="true" />
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{hint}</TooltipContent>
    </Tooltip>
  );
}

interface MeterProps extends SectionLabelProps {
  /** 0..100. */
  value: number;
  /** The reading in words, e.g. "42%" or "812 MiB of 3.8 GiB". */
  readout: string;
}

function Meter({ icon, label, hint, value, readout }: MeterProps): JSX.Element {
  const tone = toneFor(value);

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <SectionLabel icon={icon} label={label} hint={hint} />
        <span className={cn("shrink-0 text-sm font-medium tabular-nums", tone.text)}>
          {readout}
        </span>
      </div>
      <Progress
        value={value}
        aria-label={label}
        className={cn("mt-2 h-1.5", tone.track)}
        indicatorClassName={tone.indicator}
      />
    </div>
  );
}

interface Figure {
  key: string;
  label: React.ReactNode;
  value: React.ReactNode;
}

/** Tailwind needs the class whole, so the count picks one rather than building it. */
const FIGURE_COLUMNS: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-2",
  3: "grid-cols-3",
};

/**
 * A row of small labelled figures - the shape every reading on this card that
 * is not a bar is drawn in.
 *
 * One component rather than a copy per section, because the sections are read
 * as a column: the load averages, the two disk rates, the memory split and the
 * disk's three figures all have to sit on the same baseline and wear the same
 * tile, or the card looks like four cards that happened to be stacked.
 */
function Figures({ items }: { items: Figure[] }): JSX.Element {
  return (
    <dl className={cn("mt-2 grid gap-2", FIGURE_COLUMNS[items.length] ?? "grid-cols-3")}>
      {items.map((item) => (
        <div key={item.key} className="rounded-md bg-muted/60 px-2 py-1.5 text-center">
          <dt className="truncate text-xs text-muted-foreground">{item.label}</dt>
          <dd className="mt-0.5 text-sm font-medium tabular-nums">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The three load figures, labelled by the window each one averages over. */
function LoadAverage({ load }: { load: number[] }): JSX.Element {
  const { t } = useTranslation();
  const windows = [1, 5, 15];

  return (
    <div>
      <SectionLabel
        icon={Gauge}
        label={String(t("dashboard.loadAverage"))}
        hint={String(t("dashboard.loadAverageHint"))}
      />
      <Figures
        items={windows.map((minutes, index) => ({
          key: String(minutes),
          label: `${minutes}${t("units.minutesShort")}`,
          value: (load[index] ?? 0).toFixed(2),
        }))}
      />
    </div>
  );
}

/**
 * How much of the disk is used up, and - the part people are actually after -
 * how much of it is left.
 *
 * The bar is used over used-plus-free rather than over the total, which is what
 * `df` reports and therefore what an operator who runs `df -h` beside this page
 * will see. The difference is the reserve a filesystem keeps for root, a few
 * percent of the whole disk on a default ext4, and a bar reading comfortably
 * under while writes are already failing would be the worst kind of wrong here.
 *
 * All three figures are shown because two of them do not imply the third. Free
 * is smaller than total minus used by exactly that reserve, so a reader given
 * any two would work out a third that is off by a couple of gigabytes on a
 * small disk - and the whole reason to show free separately is that it is the
 * one an operator can spend.
 */
function DiskSpace({
  used,
  free,
  total,
}: {
  used: number;
  free: number;
  total: number;
}): JSX.Element {
  const { t } = useTranslation();
  const full = percent(used, used + free);

  return (
    <div>
      {/* A percentage rather than "8 GiB of 40 GiB", which the row of figures
          under it already says twice over. It is also the reading the two bars
          below this one carry, so the three of them can be compared down the
          card without reading each label first. */}
      <Meter
        icon={Database}
        label={String(t("dashboard.diskSpace"))}
        hint={String(t("dashboard.diskSpaceHint"))}
        value={full}
        readout={String(t("units.percent", { value: full.toFixed(0) }))}
      />
      <Figures
        items={[
          { key: "used", label: t("dashboard.diskUsed"), value: formatMemory(used) },
          { key: "free", label: t("dashboard.diskFree"), value: formatMemory(free) },
          { key: "total", label: t("dashboard.diskTotal"), value: formatMemory(total) },
        ]}
      />
    </div>
  );
}

/**
 * How much of the disk the machine is using as memory it did not have.
 *
 * Swapping is not a fault on its own - Linux will page out something it has not
 * touched in a week and be right to - but a swap device filling up on a box
 * with a gigabyte of RAM is the last quiet warning before the OOM killer starts
 * choosing processes, and the tunnel is a kernel module while the panel is not.
 *
 * A machine with no swap says so instead of drawing a bar. An empty bar means
 * "none of this in use", and here that would be a claim about a device that
 * does not exist; the two states look identical in the numbers and read as
 * opposites to the operator.
 */
function SwapUsage({ used, total }: { used: number; total: number }): JSX.Element {
  const { t } = useTranslation();

  if (total <= 0) {
    return (
      <div className="flex items-baseline justify-between gap-3">
        <SectionLabel
          icon={Swap}
          label={String(t("dashboard.swap"))}
          hint={String(t("dashboard.swapHint"))}
        />
        <span className="shrink-0 text-sm font-medium text-muted-foreground">
          {t("dashboard.swapNone")}
        </span>
      </div>
    );
  }

  return (
    <Meter
      icon={Swap}
      label={String(t("dashboard.swap"))}
      hint={String(t("dashboard.swapHint"))}
      value={percent(used, total)}
      readout={String(
        t("dashboard.memoryUsed", {
          used: formatMemory(used),
          total: formatMemory(total),
        }),
      )}
    />
  );
}

/**
 * How hard the disks are working, and what they are moving while they do it.
 *
 * The meter is the answer to "is the disk the thing that is slow": time with a
 * request in flight on the busiest device, which saturates at a hundred whatever
 * the hardware is. The two rates below it are the same reading described in
 * bytes, and they are what tells a busy disk that is earning it from one that is
 * thrashing - a percentage on its own cannot separate those.
 *
 * Both rates are per second over the collector's last poll rather than totals
 * since boot, so an idle server reads 0 B/s rather than the gigabytes it wrote
 * last Tuesday.
 */
function DiskActivity({
  busy,
  read,
  write,
}: {
  busy: number;
  read: number;
  write: number;
}): JSX.Element {
  const { t } = useTranslation();

  return (
    <div>
      <Meter
        icon={HardDrive}
        label={String(t("dashboard.disk"))}
        hint={String(t("dashboard.diskHint"))}
        value={busy}
        readout={String(t("units.percent", { value: busy.toFixed(0) }))}
      />
      <Figures
        items={[
          { key: "read", label: t("dashboard.diskRead"), value: formatRate(read) },
          { key: "write", label: t("dashboard.diskWrite"), value: formatRate(write) },
        ]}
      />
    </div>
  );
}

/**
 * What the tunnel costs against what the panel costs, as two plain figures.
 *
 * No bars: against total system memory both would be slivers, and against each
 * other a bar would suggest they are two halves of one budget when the panel is
 * routinely a thousand times the kernel module. Numbers side by side make the
 * comparison without dressing it up.
 */
function MemorySplit({ core, panel }: { core: number; panel: number }): JSX.Element {
  const { t } = useTranslation();

  const rows = [
    { key: "core", label: t("dashboard.memoryCore"), value: core },
    { key: "panel", label: t("dashboard.memoryPanel"), value: panel },
  ];

  return (
    <div>
      <SectionLabel
        icon={ChartPieSlice}
        label={String(t("dashboard.memoryBreakdown"))}
        hint={String(t("dashboard.memoryBreakdownHint"))}
      />
      <Figures
        items={rows.map((row) => ({
          key: row.key,
          label: row.label,
          value: row.value > 0 ? formatMemory(row.value) : t("common.notAvailable"),
        }))}
      />
    </div>
  );
}

/**
 * How long the machine has been up, which is a different question from how long
 * the tunnel has been up - and the reason both are on this page.
 *
 * It sits here rather than on a card of its own because it is a host figure
 * among host figures: an operator reading "the box rebooted forty minutes ago"
 * beside a CPU number has the whole story. No bar, for the same reason load
 * average has none: there is no maximum for it to be a fraction of.
 */
function HostUptime({ seconds }: { seconds: number }): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="flex items-baseline justify-between gap-3">
      <SectionLabel
        icon={Clock}
        label={String(t("dashboard.hostUptime"))}
        hint={String(t("dashboard.hostUptimeHint"))}
      />
      <span className="shrink-0 text-sm font-medium tabular-nums">
        {seconds > 0 ? formatDuration(seconds) : t("common.notAvailable")}
      </span>
    </div>
  );
}

export interface SystemGaugesProps {
  /** The `system` block of the collector's blob. */
  system: SystemStats | undefined;
  /** First load, before any blob has arrived. */
  loading?: boolean;
  className?: string;
}

export function SystemGauges({
  system,
  loading = false,
  className,
}: SystemGaugesProps): JSX.Element {
  const { t } = useTranslation();

  const cpu = system ? Math.max(0, Math.min(100, system.cpu)) : 0;
  const memPercent = percent(system?.memUsed, system?.memTotal);
  // The collector already clamps this, and clamping it again here is what keeps
  // a meter from drawing past its own track if a future one ever stops.
  const diskBusy = Math.max(0, Math.min(100, system?.diskBusy ?? 0));
  // The collector reports mebibytes; formatMemory wants bytes and then picks the
  // unit itself, so a 512 MiB box and a 64 GiB box both read naturally.
  const memReadout = system
    ? String(
        t("dashboard.memoryUsed", {
          used: formatMemory(system.memUsed * 1024 * 1024),
          total: formatMemory(system.memTotal * 1024 * 1024),
        }),
      )
    : "";

  return (
    <Card className={cn("flex flex-col", className)} aria-busy={loading || undefined}>
      <CardHeader>
        <CardTitle>{t("dashboard.systemLoad")}</CardTitle>
      </CardHeader>
      <CardContent className="flex-1 space-y-5">
        {loading || !system ? (
          <>
            <div className="space-y-2">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-1.5 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-1.5 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-1.5 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-11 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-1.5 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-1.5 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
            <div className="space-y-2">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-12 w-full" />
            </div>
            <Skeleton className="h-4 w-32" />
          </>
        ) : (
          <>
            <Meter
              icon={Cpu}
              label={String(t("dashboard.cpu"))}
              hint={String(t("dashboard.cpuHint"))}
              value={cpu}
              readout={String(t("units.percent", { value: cpu.toFixed(0) }))}
            />
            <Meter
              icon={Memory}
              label={String(t("dashboard.memory"))}
              hint={String(t("dashboard.memoryHint"))}
              value={memPercent}
              readout={memReadout}
            />
            {/* Absent from a blob an older collector wrote. Left out entirely
                rather than shown as a machine with no swap, which is a real
                state this cannot be allowed to invent. */}
            {system.swapTotal !== undefined ? (
              <SwapUsage used={system.swapUsed ?? 0} total={system.swapTotal} />
            ) : null}
            {/* Both are absent from a blob an older collector wrote, in which
                case the section is left out rather than shown as two zeros. */}
            {system.memCore !== undefined || system.memPanel !== undefined ? (
              <MemorySplit core={system.memCore ?? 0} panel={system.memPanel ?? 0} />
            ) : null}
            {/* Same rule again, and a second reason to obey it: a disk drawn as
                empty is the one reading here nobody would think to doubt. */}
            {system.diskTotal ? (
              <DiskSpace
                used={system.diskUsed ?? 0}
                free={system.diskFree ?? 0}
                total={system.diskTotal}
              />
            ) : null}
            {/* Same as the split above: an older collector reports none of the
                three, and a disk drawn as permanently idle would be a claim
                about the machine rather than about the blob. */}
            {system.diskBusy !== undefined ||
            system.diskRead !== undefined ||
            system.diskWrite !== undefined ? (
              <DiskActivity
                busy={diskBusy}
                read={system.diskRead ?? 0}
                write={system.diskWrite ?? 0}
              />
            ) : null}
            {/* An older collector can omit the array entirely; three dashes are
                better than a crashed dashboard. */}
            <LoadAverage load={system.load ?? []} />
            <HostUptime seconds={system.uptime ?? 0} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
