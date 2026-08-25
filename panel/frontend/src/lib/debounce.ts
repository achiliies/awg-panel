import * as React from "react";

/**
 * A value that only settles once it has stopped changing for `delayMs`.
 *
 * For the search box on the client list, which is a server round trip now
 * rather than a filter over a list already in the browser. Typing "phone" would
 * otherwise be five requests, four of them for a prefix nobody wanted an answer
 * to - and on a server where the search is the slow part, the answers arrive
 * out of order and the last one to land is not the last one asked for.
 *
 * The first value is returned as-is rather than after a delay: a list opened
 * from a link that already carries `?q=` should render the results, not the
 * unsearched page followed by a flicker into the searched one.
 */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = React.useState(value);

  React.useEffect(() => {
    if (settled === value) {
      return;
    }
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs, settled]);

  return settled;
}
