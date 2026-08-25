import * as React from "react";
import { useTranslation } from "react-i18next";

import { DatePicker } from "@/components/DatePicker";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  CUSTOM,
  DEFAULT_SPAN,
  EARLIEST_DAY,
  PRESETS,
  SCALES,
  presetOf,
  presetWindow,
  utcToday,
  windowOf,
  withScale,
  type TrafficRange,
  type TrafficScale,
} from "@/lib/trafficRange";
import { cn } from "@/lib/utils";

/*
 * Which bars, and how many of them.
 *
 * Two controls rather than one list of nine options, because the two questions
 * are independent: a fortnight can be read as fourteen days or as one month,
 * and a year can be read either way too. Folding them together would produce a
 * menu whose entries were mostly nonsense ("36 months, daily") and would still
 * need a date picker bolted to the end of it.
 *
 * The presets exist because almost nobody wants an arbitrary window - they want
 * the last month, or the last year - and typing two dates to get one is a poor
 * trade. The dates exist because "almost nobody" is not nobody, and an operator
 * reconciling a bill for March has one question that no preset answers.
 *
 * Native date inputs, and no calendar written here. The browser's own picker
 * already speaks the reader's language, knows their week-start, works from a
 * keyboard and on a phone opens the platform's spinner - all of which a hand
 * built one would have to earn back, and none of which is what this panel is
 * for. What is written here is the part the browser cannot know: the window
 * must not run past today, and on a client it must not run past what the sweep
 * still holds.
 */

export interface TrafficRangeControlsProps {
  range: TrafficRange;
  onRangeChange: (range: TrafficRange) => void;
  /**
   * The earliest day the picker will offer, as "2026-05-16". Left out, it
   * reaches as far back as the server's own history can, which is what the
   * whole-server chart wants; a client's rows are swept, so its dialog passes
   * the day the sweep stops at rather than offering months that can only come
   * back empty.
   */
  earliest?: string;
  className?: string;
}

export function TrafficRangeControls({
  range,
  onRangeChange,
  earliest = EARLIEST_DAY,
  className,
}: TrafficRangeControlsProps): JSX.Element {
  const { t } = useTranslation();
  // One "today" for the life of a render, so the preset the select shows and
  // the window a click on it produces cannot be cut on two sides of midnight.
  const today = utcToday();
  const shown = windowOf(range, today);
  /*
   * Whether the dates are showing, which is not quite the same question as
   * whether the window is a preset.
   *
   * It cannot be derived from the window alone, and the reason is the first
   * thing anybody does with this control: picking "custom" while looking at the
   * last 90 days hands back exactly the last 90 days, which *is* a preset - so a
   * derived answer would close the dates in the same tick that opened them, and
   * the option would do nothing at all. Asking for custom is therefore
   * remembered, and a window that matches nothing counts as custom whether it
   * was asked for here or arrived with the page.
   */
  const [asked, setAsked] = React.useState(false);
  const matched = presetOf(range, today);
  const custom = asked || matched === CUSTOM;
  const selected = custom ? CUSTOM : matched;

  const setWindow = (from: string, to: string): void => {
    // The pickers below are already bounded by each other and by today, so
    // nothing they hand back can fail this. It stays because the bounds are an
    // argument to a control and this is the one place that knows what they are
    // for: a window that runs backwards is a request the server answers with a
    // 400, and a chart drawn around one is a redraw nobody asked for.
    if (!from || !to || from > to || to > today || from < earliest) {
      return;
    }
    onRangeChange({ ...range, window: { from, to } });
  };

  const choose = (value: string): void => {
    if (value === CUSTOM) {
      setAsked(true);
      // Spelled out from wherever the chart already is, so opening the dates
      // starts from the window on screen rather than from an empty pair.
      onRangeChange({ ...range, window: shown });
      return;
    }
    setAsked(false);
    const span = Number(value);
    onRangeChange({
      ...range,
      // The default span travels as "no window", which is the one request that
      // carries no query string and the one cache entry every page shares.
      window: span === DEFAULT_SPAN[range.scale] ? null : presetWindow(range.scale, span, today),
    });
  };

  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-2", className)}>
      <Tabs
        value={range.scale}
        onValueChange={(value) => onRangeChange(withScale(range, value as TrafficScale))}
      >
        <TabsList>
          {SCALES.map((scale) => (
            <TabsTrigger key={scale} value={scale}>
              {t(`chart.scale.${scale}`)}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <Select value={selected} onValueChange={choose}>
        <SelectTrigger className="h-8 w-auto min-w-[9.5rem] gap-1.5 text-xs">
          <SelectValue aria-label={String(t("chart.range"))} />
        </SelectTrigger>
        <SelectContent>
          {PRESETS[range.scale].map((preset) => (
            <SelectItem key={preset.id} value={String(preset.span)}>
              {t(`chart.preset.${preset.id}`)}
            </SelectItem>
          ))}
          <SelectItem value={CUSTOM}>{t("chart.preset.custom")}</SelectItem>
        </SelectContent>
      </Select>

      {custom ? (
        <div className="flex items-center gap-1.5">
          <DatePicker
            className="h-8 w-auto min-w-[8.5rem] text-xs"
            ariaLabel={String(t("chart.from"))}
            value={shown.from}
            min={earliest}
            max={shown.to}
            onChange={(picked) => setWindow(picked, shown.to)}
          />
          <span aria-hidden="true" className="text-xs text-muted-foreground">
            –
          </span>
          <DatePicker
            className="h-8 w-auto min-w-[8.5rem] text-xs"
            ariaLabel={String(t("chart.to"))}
            value={shown.to}
            min={shown.from}
            max={today}
            onChange={(picked) => setWindow(shown.from, picked)}
          />
        </div>
      ) : null}
    </div>
  );
}
