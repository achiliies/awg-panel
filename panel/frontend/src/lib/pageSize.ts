import * as React from "react";

/*
 * How many clients a page of the table holds, and where that answer is kept.
 *
 * It is a choice rather than a constant because the two operators this list has
 * want opposite things from it. One runs a handful of clients and wants the
 * whole server on one screen; the other runs a few thousand and wants ten rows
 * they can read, because every row costs a live sample every two seconds and a
 * page of five hundred is five hundred of them. Neither number is wrong, and
 * the panel has no business picking for both.
 *
 * The choice lives in localStorage, like the theme and the language, and for the
 * same reason: it describes the machine the panel is being read on rather than
 * the server being administered, and there is no per-user profile on the server
 * worth a round trip for it. It is deliberately not in the URL either - `?q=`
 * is there because a search is worth sharing, and "ten rows" is not something
 * anyone means to send to a colleague.
 */

/** localStorage key holding the rows-per-page choice for the client list. */
export const PAGE_SIZE_STORAGE_KEY = "awg-panel-clients-page-size";

/**
 * The offered sizes. Stored and compared as strings because that is what the
 * select trades in, and because "all" is one of them.
 */
export type PageSizeChoice = "10" | "25" | "50" | "all";

export const PAGE_SIZE_CHOICES: readonly PageSizeChoice[] = ["10", "25", "50", "all"];

/**
 * What a browser that has never chosen gets.
 *
 * Ten, where this used to be a fixed fifty. A first page that is short is a
 * first page that is cheap - fifty rows is fifty live samples on every two
 * second poll - and the operator who wants more now has a control that says so.
 */
export const DEFAULT_PAGE_SIZE_CHOICE: PageSizeChoice = "10";

/**
 * The `pageSize` that asks for every row: apps.clients.merge.UNPAGED.
 *
 * This was five hundred, the largest size the API will accept from a caller who
 * names one, on the reasoning that a page nobody could scroll is not a page. It
 * meant that an operator with 1072 clients picked "All" and got three pages of
 * it, and no part of the screen explained why - the control said All and the
 * pager under it said 1 2 3, and only one of the two could be right.
 *
 * Zero is not a larger number, it is a different request: the panel does not
 * know the client count when it asks, so it cannot name a size that means all
 * of them. What it costs is real and it is why LARGE_PAGE warns before it is
 * drawn.
 */
export const ALL_PAGE_SIZE = 0;

/**
 * The row count past which a single page is worth warning about.
 *
 * Five hundred, which is where the API used to stop on its own, and it is about
 * where a page stops being a table and becomes a scroll: every row is a Radix
 * menu root and a set of live figures re-derived from the collector's blob
 * twice a second, and every one of them is in the document at once.
 *
 * A threshold rather than a refusal, and a warning rather than a smaller page,
 * because "slow" is a property of the machine reading it and not of the number.
 * A workstation draws two thousand rows and shrugs.
 */
export const LARGE_PAGE = 500;

/**
 * Where the warning over a very long page offers to go.
 *
 * The largest size that still pages, so backing out of "all" costs the operator
 * as few rows as it can. It is offered at the top of the page rather than only
 * at the control itself, which by then is at the foot of a table some thousands
 * of rows tall - the way out of a page too long to scroll cannot be at the
 * bottom of it.
 */
export const LARGE_PAGE_FALLBACK: PageSizeChoice = "50";

/** The `pageSize` to send for a choice. */
export function rowsPerPage(choice: PageSizeChoice): number {
  return choice === "all" ? ALL_PAGE_SIZE : Number(choice);
}

function isChoice(value: unknown): value is PageSizeChoice {
  return PAGE_SIZE_CHOICES.includes(value as PageSizeChoice);
}

function readStored(): PageSizeChoice | null {
  try {
    const raw = window.localStorage.getItem(PAGE_SIZE_STORAGE_KEY);
    // Anything else is a key from a version that offered different sizes, or a
    // hand-edited value. The default is a better answer than a broken table.
    return isChoice(raw) ? raw : null;
  } catch {
    // Storage blocked (private mode, hardened browser). Not worth surfacing.
    return null;
  }
}

/**
 * The rows-per-page choice, remembered across reloads.
 *
 * Read once, in the initializer, so the first request the page makes already
 * asks for the right number of rows: reading it in an effect would fetch fifty
 * rows and then immediately fetch ten, and the operator would watch the table
 * change height under them on every visit.
 */
export function usePageSizeChoice(): [PageSizeChoice, (next: PageSizeChoice) => void] {
  const [choice, setChoice] = React.useState<PageSizeChoice>(
    () => readStored() ?? DEFAULT_PAGE_SIZE_CHOICE,
  );

  const choose = React.useCallback((next: PageSizeChoice) => {
    setChoice(next);
    try {
      window.localStorage.setItem(PAGE_SIZE_STORAGE_KEY, next);
    } catch {
      // The choice holds for this tab; it just will not survive a reload.
    }
  }, []);

  return [choice, choose];
}
