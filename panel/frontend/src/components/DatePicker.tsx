import * as React from "react";
import { useTranslation } from "react-i18next";
import { CalendarBlank, CaretLeft, CaretRight, Clock } from "@/lib/icons";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

/*
 * A date, and a calendar to pick it out of.
 *
 * This replaces `input[type="date"]` and `input[type="datetime-local"]`
 * everywhere in the panel, and the reason is not that the native control works
 * badly - it is that it is the one control here the panel does not draw. Every
 * other field is the panel's own border radius, its own focus ring, its own
 * greys; the browser's date box brings a segmented text field with a system
 * caret, and behind it a calendar in Chrome's colours that has never heard of
 * the theme. On the dark palette that is a white sheet dropped over a black
 * page. It also spends its worst behaviour on its most common use: a segmented
 * box can be left half typed, so "2026-__-14" reports itself as empty, and both
 * places that took a date had to carry code to tell "nothing" apart from "not
 * finished yet".
 *
 * What is written here is a button and a grid. There is no text entry, so a
 * value is a whole date or it is absent, and the half-typed state stops
 * existing rather than being handled.
 *
 * Everything about the calendar is the reader's: the month and weekday names,
 * the day the week starts on, the order of the date in the trigger. Everything
 * about the value is ISO, because that is what both callers store and what the
 * API takes - "2026-08-14", or "2026-08-14T18:30" when a time comes with it.
 *
 * The arithmetic is done on UTC dates and never on local ones, which sounds
 * backwards for a control that shows local days and is the only way to be right
 * about both callers. A calendar grid is pure calendar arithmetic - the day
 * after the 31st, the Monday before the 1st - and doing it on local dates walks
 * into the two hours a year that a local day does not exist or happens twice.
 * The strings here are calendar labels rather than instants: the traffic range
 * means UTC days on the server, the expiry means a wall clock in front of the
 * operator, and neither is converted anywhere in this file.
 */

/** Days in the grid: six weeks, so the popover never changes height. */
const CELLS = 42;

export interface DatePickerProps {
  /** "2026-08-14", or "2026-08-14T18:30" with `time`. Empty means unset. */
  value: string;
  onChange: (value: string) => void;
  /** Carry a time of day as well, and offer a clock under the calendar. */
  time?: boolean;
  /** Inclusive bounds on the day, as "2026-08-14". */
  min?: string;
  max?: string;
  /** What the trigger says when there is no value. */
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  invalid?: boolean;
  /** Only where no visible <label> names the field. */
  ariaLabel?: string;
  describedBy?: string;
  className?: string;
}

/* ------------------------------------------------------------------ dates */

function parts(day: string): [number, number, number] {
  const [year, month, date] = day.split("-").map(Number);
  return [year, month, date];
}

/** A day string as a UTC instant, for arithmetic and for Intl to format. */
function asDate(day: string): Date {
  const [year, month, date] = parts(day);
  return new Date(Date.UTC(year || 1970, (month || 1) - 1, date || 1));
}

function asDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function shiftDays(day: string, days: number): string {
  const date = asDate(day);
  date.setUTCDate(date.getUTCDate() + days);
  return asDay(date);
}

function shiftMonths(day: string, months: number): string {
  const [year, month, date] = parts(day);
  const moved = new Date(Date.UTC(year, month - 1 + months, 1));
  // The 31st of a month before a short one lands on the 1st of the one after
  // unless it is held back, which is the difference between "a month on from
  // the 31st of March" being the 30th of April and being the 1st of May.
  const last = new Date(Date.UTC(moved.getUTCFullYear(), moved.getUTCMonth() + 1, 0)).getUTCDate();
  moved.setUTCDate(Math.min(date, last));
  return asDay(moved);
}

function firstOfMonth(day: string): string {
  const [year, month] = parts(day);
  return asDay(new Date(Date.UTC(year, month - 1, 1)));
}

function lastOfMonth(day: string): string {
  const [year, month] = parts(day);
  // Day zero of the next month, which is the last of this one whatever its
  // length and whether or not February has 29 of them.
  return asDay(new Date(Date.UTC(year, month, 0)));
}

/** Today as the calendar spells it, in the reader's own zone. */
function today(): string {
  const now = new Date();
  return asDay(new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())));
}

/** The clock now, to the minute, for a date chosen without one. */
function nowTime(): string {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

/**
 * Which weekday a week starts on here, as 0 for Sunday.
 *
 * `getWeekInfo` is the only honest answer and not every browser has it, so the
 * fallback is Monday: it is what the ISO week is, what both of the panel's
 * languages use, and a calendar that starts on the wrong day is off by one
 * column rather than wrong about a date.
 */
function weekStart(locale: string): number {
  try {
    const info = (
      new Intl.Locale(locale) as Intl.Locale & { getWeekInfo?: () => { firstDay: number } }
    ).getWeekInfo?.();
    return info ? info.firstDay % 7 : 1;
  } catch {
    return 1;
  }
}

/* ---------------------------------------------------------------- the grid */

interface CalendarProps {
  /** The day the grid is built around; the month it falls in is the one shown. */
  cursor: string;
  onCursor: (day: string) => void;
  selected: string;
  onSelect: (day: string) => void;
  min?: string;
  max?: string;
}

function Calendar({ cursor, onCursor, selected, onSelect, min, max }: CalendarProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const grid = React.useRef<HTMLDivElement>(null);
  // Focus follows the cursor, but only once the grid already had it: moving the
  // month with the buttons must not steal focus from them, and opening the
  // popover must not scroll the page to a day nobody has asked for yet.
  const [roving, setRoving] = React.useState(false);

  const month = firstOfMonth(cursor);
  const now = today();

  const format = React.useMemo(
    () => ({
      month: new Intl.DateTimeFormat(i18n.language, {
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      }),
      weekday: new Intl.DateTimeFormat(i18n.language, { weekday: "short", timeZone: "UTC" }),
      weekdayLong: new Intl.DateTimeFormat(i18n.language, { weekday: "long", timeZone: "UTC" }),
      long: new Intl.DateTimeFormat(i18n.language, { dateStyle: "full", timeZone: "UTC" }),
    }),
    [i18n.language],
  );

  const days = React.useMemo(() => {
    const start = asDate(month);
    // Back up to the first day of the week the 1st falls in.
    const lead = (start.getUTCDay() - weekStart(i18n.language) + 7) % 7;
    const first = shiftDays(month, -lead);
    return Array.from({ length: CELLS }, (_, index) => shiftDays(first, index));
  }, [month, i18n.language]);

  const outOfRange = (day: string): boolean =>
    (min !== undefined && day < min) || (max !== undefined && day > max);

  // Held inside the bounds rather than stopped at them: a page back from the
  // 15th when the 1st is the floor should land on the 1st, not refuse to move
  // and leave the reader pressing a key that does nothing.
  const move = (day: string): void => {
    setRoving(true);
    onCursor(min !== undefined && day < min ? min : max !== undefined && day > max ? max : day);
  };

  const onKeyDown = (event: React.KeyboardEvent): void => {
    const by: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (event.key in by) {
      event.preventDefault();
      move(shiftDays(cursor, by[event.key]));
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const offset = (asDate(cursor).getUTCDay() - weekStart(i18n.language) + 7) % 7;
      move(shiftDays(cursor, event.key === "Home" ? -offset : 6 - offset));
      return;
    }
    if (event.key === "PageUp" || event.key === "PageDown") {
      event.preventDefault();
      move(shiftMonths(cursor, event.key === "PageUp" ? -1 : 1));
    }
  };

  // The focused cell is the only one in the tab order, which is what makes the
  // whole grid one stop and the arrows the way through it.
  React.useEffect(() => {
    if (roving) {
      grid.current?.querySelector<HTMLElement>('[tabindex="0"]')?.focus();
    }
  }, [cursor, roving]);

  // Opening puts the caret on the day the field already holds, so the arrows
  // work on the first press rather than after tabbing past two buttons. This
  // mounts with the popover, so it runs once per opening.
  React.useEffect(() => setRoving(true), []);

  const step = (months: number): void => {
    setRoving(false);
    onCursor(shiftMonths(cursor, months));
  };

  // Stopped at the edge of what can be picked, rather than walking on into
  // months where every day is greyed out. A control that goes on responding
  // while nothing it shows can be chosen is a control that has stopped saying
  // anything.
  const canGoBack = min === undefined || lastOfMonth(shiftMonths(month, -1)) >= min;
  const canGoOn = max === undefined || firstOfMonth(shiftMonths(month, 1)) <= max;

  return (
    // Fixed rather than fitted to the numbers in it: seven columns of 32px is a
    // square cell under a 32px row, and a grid that sized itself would be a
    // different width in February than in March.
    <div className="w-56">
      <div className="flex items-center justify-between gap-2">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          aria-label={String(t("calendar.previousMonth"))}
          disabled={!canGoBack}
          onClick={() => step(-1)}
        >
          <CaretLeft weight="bold" className="h-4 w-4 rtl:rotate-180" aria-hidden="true" />
        </Button>
        <p aria-live="polite" className="text-sm font-medium capitalize">
          {format.month.format(asDate(month))}
        </p>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          aria-label={String(t("calendar.nextMonth"))}
          disabled={!canGoOn}
          onClick={() => step(1)}
        >
          <CaretRight weight="bold" className="h-4 w-4 rtl:rotate-180" aria-hidden="true" />
        </Button>
      </div>

      <div
        ref={grid}
        role="grid"
        aria-label={String(t("calendar.label"))}
        onKeyDown={onKeyDown}
        className="mt-3"
      >
        <div role="row" className="grid grid-cols-7">
          {days.slice(0, 7).map((day) => (
            <abbr
              key={day}
              role="columnheader"
              title={format.weekdayLong.format(asDate(day))}
              className="py-1 text-center text-[11px] font-medium capitalize text-muted-foreground no-underline"
            >
              {format.weekday.format(asDate(day))}
            </abbr>
          ))}
        </div>

        {Array.from({ length: CELLS / 7 }, (_, week) => (
          <div role="row" key={week} className="grid grid-cols-7">
            {days.slice(week * 7, week * 7 + 7).map((day) => {
              const inMonth = day.slice(0, 7) === month.slice(0, 7);
              const disabled = outOfRange(day);
              const isSelected = day === selected;
              return (
                <div role="gridcell" key={day} className="p-0.5">
                  <button
                    type="button"
                    tabIndex={day === cursor ? 0 : -1}
                    disabled={disabled}
                    aria-selected={isSelected}
                    aria-current={day === now ? "date" : undefined}
                    aria-label={format.long.format(asDate(day))}
                    onClick={() => {
                      setRoving(true);
                      onSelect(day);
                    }}
                    className={cn(
                      "flex h-8 w-full items-center justify-center rounded-md text-sm tabular-nums",
                      "transition-colors focus-visible:outline-none focus-visible:ring-2",
                      "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-popover",
                      "disabled:pointer-events-none disabled:opacity-30",
                      inMonth ? "text-foreground" : "text-muted-foreground/50",
                      isSelected
                        ? "bg-primary font-semibold text-primary-foreground hover:bg-primary"
                        : "hover:bg-accent",
                      // Today is marked even when it is not the selection, and
                      // by an underline rather than a fill: a filled today next
                      // to a filled selection is two answers to "which day is
                      // this".
                      !isSelected && day === now && "font-semibold underline underline-offset-4",
                    )}
                  >
                    {asDate(day).getUTCDate()}
                  </button>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- the control */

export function DatePicker({
  value,
  onChange,
  time = false,
  min,
  max,
  placeholder,
  id,
  disabled = false,
  invalid = false,
  ariaLabel,
  describedBy,
  className,
}: DatePickerProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = React.useState(false);

  const [day, clock] = value.split("T");
  const selected = day ?? "";
  // The month on show. It follows the value while the popover is shut, so
  // opening it always lands on the month the field is talking about, and is the
  // reader's to move once it is open.
  const [cursor, setCursor] = React.useState(selected || today());
  React.useEffect(() => {
    if (!open) {
      setCursor(selected || today());
    }
  }, [open, selected]);

  const label = React.useMemo(() => {
    if (!selected) {
      return "";
    }
    const date = new Intl.DateTimeFormat(i18n.language, {
      dateStyle: "medium",
      timeZone: "UTC",
    }).format(asDate(selected));
    return time && clock ? t("calendar.at", { date, time: clock }) : date;
  }, [selected, clock, time, i18n.language, t]);

  const emit = (nextDay: string, nextTime: string | undefined): void => {
    onChange(time ? `${nextDay}T${nextTime || nowTime()}` : nextDay);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          id={id}
          disabled={disabled}
          aria-label={ariaLabel}
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          className={cn(
            // The same box as Input, because it stands in a form beside them and
            // a control that opens a panel is still a field until it does.
            "flex h-9 w-full items-center justify-between gap-2 rounded-md border border-input",
            "bg-background px-3 py-1 text-start text-sm shadow-sm transition-colors",
            "hover:bg-accent/40 data-[state=open]:bg-accent/40",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "focus-visible:ring-offset-1 focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
            "aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive",
            !selected && "text-muted-foreground",
            className,
          )}
        >
          <span className="truncate tabular-nums">{label || placeholder || ""}</span>
          <CalendarBlank className="h-4 w-4 shrink-0 opacity-60" aria-hidden="true" />
        </button>
      </PopoverTrigger>

      <PopoverContent className="w-auto p-3" align="start">
        <Calendar
          cursor={cursor}
          onCursor={setCursor}
          selected={selected}
          onSelect={(picked) => {
            setCursor(picked);
            emit(picked, clock);
            // A date is one decision and the popover has said all it has to
            // say - except when a time comes with it, where closing would put
            // the clock behind a second click.
            if (!time) {
              setOpen(false);
            }
          }}
          min={min}
          max={max}
        />

        {time ? (
          <label className="mt-3 flex items-center gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
            <Clock className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="shrink-0">{t("calendar.time")}</span>
            <input
              type="time"
              value={clock ?? ""}
              onChange={(event) => {
                // An empty box is the clock being retyped rather than the date
                // being withdrawn, so the day is kept and the value waits for a
                // whole time to come back.
                if (event.target.value) {
                  emit(selected || today(), event.target.value);
                }
              }}
              className={cn(
                "h-8 w-full rounded-md border border-input bg-background px-2 text-sm tabular-nums",
                "text-foreground shadow-sm transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                "focus-visible:ring-offset-1 focus-visible:ring-offset-background",
              )}
            />
          </label>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
