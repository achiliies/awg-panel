import * as React from "react";
import { useTranslation } from "react-i18next";
import { CaretLeft, CaretRight } from "@/lib/icons";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PAGE_SIZE_CHOICES, type PageSizeChoice } from "@/lib/pageSize";
import { cn } from "@/lib/utils";

/*
 * Walking a list that is too long to send at once.
 *
 * This was a step either way and nothing else, on the reasoning that an
 * operator searches a client list rather than navigating it by page number. The
 * reasoning held while a page was fifty rows and most servers had one; it stops
 * holding the moment the page size is the operator's to choose. Ten rows of
 * four hundred is forty pages, and "next, next, next" is not navigation - it is
 * forty requests to reach a client whose name you did not know well enough to
 * search for, and no way at all to get back to where you were.
 *
 * So there are numbers now, and the whole of the design is the budget: seven
 * slots, never more, at every page count. Up to seven pages that is every page.
 * Past it the two ends are pinned and the middle slides, because the page an
 * operator wants after "next" is the one beside this one and the page they want
 * after that is usually the last - a list sorted newest-first has its interesting
 * end there. What the ellipsis hides is reachable by the jump box, which is why
 * that appears at exactly the page count where the numbers stop being complete.
 *
 * Rows-per-page sits at the other end of the same bar and is drawn even when
 * there is one page, deliberately. Everything else here is about walking a list
 * that did not fit; that control is how an operator who is looking at ten of
 * their twelve clients asks to see twelve, and hiding it whenever it had
 * nothing to page through would hide it precisely then.
 */

/**
 * The most page slots drawn at once, ellipses and both ends included.
 *
 * Seven is what fits a phone once the two arrows and the jump box have taken
 * their share, and it is an odd number, which is what lets the current page sit
 * in the middle of the run with the same count either side of it.
 */
const WINDOW = 7;

/** A page number to draw, or a run of pages the budget could not fit. */
type Slot = number | "gap";

function range(from: number, to: number): number[] {
  return Array.from({ length: to - from + 1 }, (_, index) => from + index);
}

/**
 * The seven slots for this page of this many.
 *
 * The two ends are always drawn, so page one and the last page are one click
 * away from anywhere. What moves is the run in between: it hugs whichever end
 * the current page is near - a run of five, so the first four pages and the last
 * four are reached without an ellipsis ever standing between them - and once the
 * current page is clear of both ends it centres on it with a gap either side.
 */
function pageSlots(page: number, pageCount: number): Slot[] {
  if (pageCount <= WINDOW) {
    return range(1, pageCount);
  }
  // One end pinned, one ellipsis, and five consecutive pages against the near
  // end: 5 + 1 + 1 is the whole budget.
  const run = WINDOW - 2;
  if (page <= run - 1) {
    return [...range(1, run), "gap", pageCount];
  }
  if (page >= pageCount - run + 2) {
    return [1, "gap", ...range(pageCount - run + 1, pageCount)];
  }
  return [1, "gap", page - 1, page, page + 1, "gap", pageCount];
}

export interface PaginationProps {
  /** 1-based, and the server's answer rather than what was asked for. */
  page: number;
  pageCount: number;
  /** The rows-per-page choice, as stored: one of PAGE_SIZE_CHOICES. */
  pageSize: PageSizeChoice;
  onChange: (page: number) => void;
  onPageSizeChange: (choice: PageSizeChoice) => void;
  className?: string;
}

export function Pagination({
  page,
  pageCount,
  pageSize,
  onChange,
  onPageSizeChange,
  className,
}: PaginationProps): JSX.Element {
  const { t } = useTranslation();
  const [jump, setJump] = React.useState("");
  const jumpId = React.useId();

  const first = page <= 1;
  const last = page >= pageCount;
  const slots = pageSlots(page, pageCount);
  // Only once the numbers have stopped being every page. Below that every page
  // is already one click away and a box asking which one to go to is a control
  // that can do nothing the row beside it cannot.
  const canJump = pageCount > WINDOW;

  const goToTyped = (event: React.FormEvent): void => {
    event.preventDefault();
    const wanted = Number.parseInt(jump, 10);
    if (Number.isNaN(wanted)) {
      return;
    }
    // Clamped rather than refused, which is also what the server does with a
    // page past the end: somebody who types 99 into a list of 12 pages means
    // the last one, and an error message here would be pedantry.
    onChange(Math.min(Math.max(wanted, 1), pageCount));
    setJump("");
  };

  return (
    <nav
      aria-label={String(t("pagination.label"))}
      className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-3 pt-1", className)}
    >
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">{t("pagination.rowsPerPage")}</span>
        <Select
          value={pageSize}
          onValueChange={(value) => onPageSizeChange(value as PageSizeChoice)}
        >
          <SelectTrigger
            className="h-8 w-[4.75rem] text-xs"
            aria-label={String(t("pagination.rowsPerPage"))}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PAGE_SIZE_CHOICES.map((choice) => (
              <SelectItem key={choice} value={choice} className="text-xs">
                {choice === "all" ? t("common.all") : choice}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {pageCount > 1 ? (
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          {/* Announced, because stepping a page replaces the table under a
              reader with no other sign that anything moved. Shown as text only
              where the numbers are not: on a phone this is the whole of the
              position, and beside a row of numbered buttons it would be saying
              a second time what the highlighted one already says. */}
          <p className="text-xs text-muted-foreground sm:sr-only" aria-live="polite">
            {t("pagination.position", { page, pageCount })}
          </p>

          <ul className="flex items-center gap-1">
            <li>
              <Button
                variant="outline"
                size="sm"
                className="w-8 px-0"
                disabled={first}
                onClick={() => onChange(page - 1)}
                aria-label={String(t("pagination.previous"))}
              >
                {/* The caret points back, which under RTL is the other way round. */}
                <CaretLeft aria-hidden="true" className="rtl:rotate-180" />
              </Button>
            </li>

            {/* Hidden below sm rather than wrapped onto a second line: seven
                buttons, two arrows and a jump box do not fit a phone at any
                arrangement, and the arrows and the position line above are a
                complete control on their own. */}
            {slots.map((slot, index) =>
              slot === "gap" ? (
                <li
                  key={`gap-${index}`}
                  className="hidden h-8 w-8 select-none items-center justify-center text-muted-foreground sm:flex"
                  aria-hidden="true"
                >
                  …
                </li>
              ) : (
                <li key={slot} className="hidden sm:block">
                  <Button
                    variant={slot === page ? "default" : "ghost"}
                    size="sm"
                    className={cn(
                      "w-8 px-0 tabular-nums",
                      // The button that just became the current page pops in
                      // rather than snapping to its highlighted state, so
                      // paging reads as landing on a number instead of the
                      // list jump-cutting under it. Only an entrance: the
                      // button losing the page stays a plain ghost button
                      // the instant it does, which is the fade-out already
                      // built into transition-colors above.
                      slot === page && "duration-300 animate-in fade-in-0 zoom-in-75",
                    )}
                    // Marked rather than disabled: a disabled button leaves the
                    // tab order, so taking the current page out of it would put
                    // a hole in the middle of a row being walked by keyboard.
                    // Pressing it asks for the page already on screen, which
                    // costs nothing - the query key does not move.
                    aria-current={slot === page ? "page" : undefined}
                    onClick={() => onChange(slot)}
                    aria-label={String(t("pagination.page", { page: slot }))}
                  >
                    {slot}
                  </Button>
                </li>
              ),
            )}

            <li>
              <Button
                variant="outline"
                size="sm"
                className="w-8 px-0"
                disabled={last}
                onClick={() => onChange(page + 1)}
                aria-label={String(t("pagination.next"))}
              >
                <CaretRight aria-hidden="true" className="rtl:rotate-180" />
              </Button>
            </li>
          </ul>

          {canJump ? (
            <form className="flex items-center gap-2" onSubmit={goToTyped}>
              <label className="hidden text-xs text-muted-foreground sm:inline" htmlFor={jumpId}>
                {t("pagination.jump")}
              </label>
              <Input
                id={jumpId}
                // Not type="number": the spinner arrows are a second, tinier
                // pair of step controls next to the two real ones, and a number
                // input silently reports an empty string for "1e5".
                type="text"
                inputMode="numeric"
                autoComplete="off"
                value={jump}
                onChange={(event) => setJump(event.target.value.replace(/[^0-9]/g, ""))}
                placeholder={String(page)}
                aria-label={String(t("pagination.jump"))}
                className="h-8 w-14 text-center text-xs tabular-nums"
              />
              <Button type="submit" variant="outline" size="sm" disabled={jump === ""}>
                {t("pagination.jumpAction")}
              </Button>
            </form>
          ) : null}
        </div>
      ) : null}
    </nav>
  );
}
