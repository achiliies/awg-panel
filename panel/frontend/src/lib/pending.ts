import * as React from "react";

/**
 * Hold a pending flag on screen long enough for someone to see it.
 *
 * The panel's API runs on the same machine as the panel, so saving a setting
 * usually answers in less time than it takes to notice anything happened:
 * `isPending` goes true and false again inside a couple of frames, React drops
 * the spinner it just mounted, and the operator clicks Save and sees nothing.
 * A save that silently worked and a save that silently did nothing look
 * identical, which is the worst of the two.
 *
 * So: true the moment the work starts, and true for at least `minMs` after
 * that, however quickly the answer comes back. Requests that outlive the
 * window are untouched - this only ever adds time to waits that were too short
 * to read, never to real ones.
 *
 * The floor is on the indicator, not on the request: callers already have the
 * result by the time this goes false, and nothing here delays it.
 */
export function usePendingIndicator(pending: boolean, minMs = 550): boolean {
  const [held, setHeld] = React.useState(pending);
  const startedAt = React.useRef(0);

  React.useEffect(() => {
    if (pending) {
      // Restarted rather than extended: a second save gets its own full window.
      startedAt.current = Date.now();
      setHeld(true);
      return;
    }

    if (!held) {
      return;
    }

    const remaining = minMs - (Date.now() - startedAt.current);
    if (remaining <= 0) {
      setHeld(false);
      return;
    }

    const timer = window.setTimeout(() => setHeld(false), remaining);
    return () => window.clearTimeout(timer);
  }, [held, minMs, pending]);

  return held;
}

/**
 * A value, but only once it has stopped changing for `ms`.
 *
 * For work that hangs off a text field and is too expensive to do per
 * keystroke - a request, in practice. Typing a path thirty characters long is
 * thirty answers about files that were never meant to exist, twenty-nine of
 * which are thrown away.
 *
 * The trailing edge only: the first keystroke waits like every other one, which
 * is what makes the caller's "is this answer about what is on screen now?"
 * check meaningful. Callers have to reckon with the gap - what they hold during
 * it describes the previous value, not the one on screen.
 */
export function useSettled<T>(value: T, ms = 400): T {
  const [settled, setSettled] = React.useState(value);

  React.useEffect(() => {
    if (Object.is(value, settled)) {
      return;
    }
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [ms, settled, value]);

  return settled;
}
