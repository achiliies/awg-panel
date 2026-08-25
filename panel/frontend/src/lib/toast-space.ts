import * as React from "react";

/*
 * Where the toast stack is not allowed to land.
 *
 * Notifications sit on the bottom edge of the window - full width on a phone,
 * bottom-end on anything wider - and that is also where the panel puts the
 * controls that matter. The save bar on Server Config and on Settings is stuck
 * to the bottom edge with Discard and Save at its end, and a dialog on a short
 * window runs the full height of it with its buttons at the bottom. A toast
 * that lands on top of Save is worse than no toast at all: it reports on an
 * action while covering the button that performs it. Reconfigure is the clearest
 * case - it draws a new profile, says "press Save to apply it", and then covers
 * Save.
 *
 * So anything that owns the bottom edge publishes its height, and anything that
 * takes the whole screen says so. The stack reads both: it lifts itself over
 * the bar, and it moves to the top edge for as long as a dialog is up.
 *
 * The height travels as a CSS variable rather than through React state on
 * purpose. It changes on every window resize, and a resize should not re-render
 * every page under the provider to move one box by a few pixels.
 */

/** Height of the tallest thing currently pinned to the bottom edge. */
const BOTTOM_INSET = "--toast-inset-bottom";

const reserved = new Map<symbol, number>();

function publishBottomInset(): void {
  let tallest = 0;
  for (const height of reserved.values()) {
    tallest = Math.max(tallest, height);
  }
  const root = document.documentElement;
  if (tallest > 0) {
    root.style.setProperty(BOTTOM_INSET, `${Math.round(tallest)}px`);
  } else {
    // Removed rather than set to 0px, so the fallback in the class that reads
    // it is what applies when nothing is reserving anything.
    root.style.removeProperty(BOTTOM_INSET);
  }
}

/**
 * Keep notifications clear of `ref` while it is mounted.
 *
 * For an element pinned to the bottom edge of the window. The height is
 * re-measured whenever the element changes size, which on these bars happens
 * every time the list of changed fields wraps onto another line.
 */
export function useBottomInset(ref: React.RefObject<HTMLElement | null>): void {
  React.useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      return;
    }

    const key = Symbol("toast-inset");
    const measure = (): void => {
      reserved.set(key, element.getBoundingClientRect().height);
      publishBottomInset();
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);

    return () => {
      observer.disconnect();
      reserved.delete(key);
      publishBottomInset();
    };
  }, [ref]);
}

const openModals = new Set<symbol>();
const listeners = new Set<() => void>();

function announce(): void {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Declare that this element is holding the whole screen, and get back the ref
 * to hang on it. Pass the forwarded ref through, or null if there is none.
 *
 * Called by the dialog primitives, so every dialog in the panel is covered by
 * the one call rather than by remembering it at each of the call sites.
 *
 * Presence follows the element's data-state rather than its mount, because
 * Radix keeps a closing dialog mounted for the length of its exit animation.
 * These toasts arrive from mutations that land inside that window - confirm
 * Reconfigure, and the answer comes back while the confirmation is still fading
 * - so a mount-scoped version would put the toast at the top edge and then drop
 * it to the bottom a frame later. Anything without a data-state at all, like a
 * hand-built overlay, counts as open for as long as it is mounted.
 */
export function useModalPresence<T extends HTMLElement>(
  forwardedRef: React.ForwardedRef<T>,
): React.RefCallback<T> {
  // State, not a ref: the effect below has to re-run when the node attaches.
  const [element, setElement] = React.useState<T | null>(null);

  // The forwarded ref goes through a box so that `attach` can be built once and
  // never change. A callback ref that changes identity is detached and
  // reattached on every render, and this one sets state when it runs - a caller
  // passing an inline ref would have spun.
  const forwarded = React.useRef(forwardedRef);
  forwarded.current = forwardedRef;

  const attach = React.useCallback((node: T | null) => {
    setElement(node);
    const target = forwarded.current;
    if (typeof target === "function") {
      target(node);
    } else if (target) {
      target.current = node;
    }
  }, []);

  React.useEffect(() => {
    if (!element) {
      return;
    }

    const key = Symbol("modal");
    const sync = (): void => {
      if (element.dataset.state === "closed") {
        openModals.delete(key);
      } else {
        openModals.add(key);
      }
      announce();
    };

    sync();
    const observer = new MutationObserver(sync);
    observer.observe(element, { attributes: true, attributeFilter: ["data-state"] });

    return () => {
      observer.disconnect();
      openModals.delete(key);
      announce();
    };
  }, [element]);

  return attach;
}

/** True while at least one modal is up. */
export function useModalOpen(): boolean {
  return React.useSyncExternalStore(
    subscribe,
    () => openModals.size > 0,
    () => false,
  );
}
