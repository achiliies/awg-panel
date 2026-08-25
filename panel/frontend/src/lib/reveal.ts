import * as React from "react";

/*
 * Bringing the answer to a save into view.
 *
 * Both forms in the panel report what a save cost in a notice at the top of the
 * page, and both put the button that starts it on the bottom edge of the
 * window. In between are five tabs of cards on Settings and a card per
 * parameter group on Server config, so the notice lands a screen or three above
 * the operator who pressed Save. "The warnings are already on the page" is true
 * and no help at all: what they see is the save bar disappearing and nothing
 * else happening.
 *
 * A toast says that the save happened. This is what puts the part that has to
 * be read where it can be read.
 *
 * `token` is whatever the page uses for "there is something new to say" - the
 * warnings array, the save result - and it is the identity that moves the page,
 * not the contents. Two saves in a row that draw the same advice are two
 * separate answers, and the second one is worth scrolling back to.
 */
export function useReveal<T extends HTMLElement>(token: unknown): React.RefObject<T> {
  const ref = React.useRef<T>(null);

  React.useEffect(() => {
    const element = ref.current;
    if (!token || !element) {
      return;
    }
    // The page is about to move under someone who is looking somewhere else, so
    // it moves visibly - unless they have asked for less of that, in which case
    // the jump is the whole point and the animation is what is dropped.
    const still =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
  }, [token]);

  return ref;
}
