import * as React from "react";

/*
 * How wide the navigation rail is, and whether it is on screen at all.
 *
 * Both are choices rather than constants because the screens this panel is read
 * on do not agree about what the rail is worth. On a laptop beside a terminal
 * it is 16rem of a 1280px window spent on seven links that never change; on a
 * wide monitor there is room to spare and a Russian label that truncated at the
 * old fixed width no longer has to. So the edge is draggable, and dragging it
 * far enough past its minimum puts the rail away entirely - which is the same
 * gesture, carried on rather than stopped at a wall, and is what somebody
 * pulling it leftwards is asking for by then.
 *
 * The choice lives in localStorage, like the theme and the rows-per-page
 * choice, and for the same reason: it describes the machine the panel is being
 * read on rather than the server being administered, and there is no per-user
 * profile on the server worth a round trip for it.
 *
 * The width reaches the layout as a custom property on <html> rather than as a
 * style prop, and that is the one decision here worth explaining. A drag has to
 * repaint at the rate the pointer moves, and the rail's width is also the
 * routed page's start padding: a state update per pointermove would re-render
 * the whole page under the cursor - charts, tables and all - sixty times a
 * second, to move one edge. So a drag writes the property directly and commits
 * to React once, when the pointer is let go. The layout effect below then
 * re-applies exactly what the drag last painted, so the commit that finally
 * does re-render the page changes nothing on screen.
 */

/** localStorage key holding the rail's width, in CSS pixels. */
export const SIDEBAR_WIDTH_STORAGE_KEY = "awg-panel-sidebar-width";

/** localStorage key holding whether the rail has been put away. */
export const SIDEBAR_HIDDEN_STORAGE_KEY = "awg-panel-sidebar-hidden";

/*
 * The two custom properties the layout is built out of. index.css declares
 * defaults for both so a stylesheet that arrives before React has something to
 * resolve; from mount on, this module owns them.
 */
const WIDTH_VAR = "--sidebar-width";
const MOTION_VAR = "--sidebar-motion";

/**
 * How long the rail takes to open or close.
 *
 * Long enough to read as the panel moving rather than the page re-laying out,
 * short enough that nobody waits for it. It is spent on `width` and on the
 * page's start padding, which are both layout, so this is also the one moment
 * the shell reflows on a timer - hence the small number.
 */
const MOTION = "200ms";

/**
 * The narrowest the rail may be dragged.
 *
 * Set by the longest label rather than by taste: at 14rem "Statistics and logs"
 * and its Russian equivalent still fit beside their glyph without the ellipsis
 * that would make the rail unreadable at exactly the width somebody chose in
 * order to keep reading it.
 */
export const MIN_SIDEBAR_WIDTH = 224;

/**
 * The widest.
 *
 * The rail holds seven links and a version string; past this it is not showing
 * anything more, it is only taking the page's width away. 400px still leaves
 * 368px of content at the 768px where the rail first appears at all.
 */
export const MAX_SIDEBAR_WIDTH = 400;

/** What a browser that has never dragged it gets: the width it was fixed at. */
export const DEFAULT_SIDEBAR_WIDTH = 256;

/**
 * Let go left of this and the rail goes away instead of snapping back.
 *
 * The gap below MIN_SIDEBAR_WIDTH is deliberate and is what stops the two
 * outcomes from being one twitchy boundary: the rail stops moving at its
 * minimum and stays there for another 74px of travel, so hiding it is a pull
 * somebody has to mean, and the resistance on the way is the warning.
 */
export const HIDE_SIDEBAR_BELOW = 150;

/** How far one arrow key moves the edge. */
export const SIDEBAR_WIDTH_STEP = 16;

function clampWidth(px: number): number {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, Math.round(px)));
}

/**
 * What a pointer that far from the window's leading edge means: a width, or
 * null for "let go here and the rail goes away".
 */
export function widthForPointer(distance: number): number | null {
  return distance < HIDE_SIDEBAR_BELOW ? null : clampWidth(distance);
}

function readStoredWidth(): number | null {
  try {
    const raw = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
    const px = Number(raw);
    // A hand-edited value, or one stored before the bounds moved. Clamping it
    // is kinder than the default: it keeps whichever end of the range the
    // operator was asking for.
    return raw !== null && raw !== "" && Number.isFinite(px) ? clampWidth(px) : null;
  } catch {
    // Storage blocked (private mode, hardened browser). Not worth surfacing.
    return null;
  }
}

function readStoredHidden(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_HIDDEN_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function store(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // The choice holds for this tab; it just will not survive a reload.
  }
}

/**
 * Whether a change to the width is worth watching.
 *
 * Off for the live half of a drag, where an eased width is a rail that lags the
 * cursor by the length of the ease - which reads as the drag being broken
 * rather than as motion.
 */
function paintMotion(animate: boolean): void {
  document.documentElement.style.setProperty(MOTION_VAR, animate ? MOTION : "0ms");
}

function paint(width: number, animate: boolean): void {
  document.documentElement.style.setProperty(WIDTH_VAR, `${width}px`);
  paintMotion(animate);
}

export interface SidebarLayout {
  /** The remembered width, which is what the rail returns to when it is shown again. */
  width: number;
  /** The rail is off screen; the top bar carries the way back. */
  hidden: boolean;
  /** Remember a width and paint it. */
  setWidth: (px: number) => void;
  /** Put the rail away, or bring it back at the width it had. */
  setHidden: (hidden: boolean) => void;
  /**
   * Paint a width without remembering it - the live half of a drag, where 0 is
   * the preview of letting go past HIDE_SIDEBAR_BELOW. Passing null ends the
   * preview and puts the committed width back, which is also how a cancelled
   * drag unwinds.
   */
  preview: (px: number | null) => void;
}

export function useSidebarLayout(): SidebarLayout {
  // Read in the initializer rather than in an effect: the layout effect below
  // runs before the first paint, so the rail is drawn at the stored width
  // once instead of at 16rem and then again a frame later.
  const [width, setWidthState] = React.useState<number>(
    () => readStoredWidth() ?? DEFAULT_SIDEBAR_WIDTH,
  );
  const [hidden, setHiddenState] = React.useState<boolean>(readStoredHidden);

  // Whether anything has been on screen yet, on the same reasoning as
  // ThemeProvider's: the first application is not a change and must not be
  // animated as one. index.css declares the width the rail used to be fixed at,
  // so a panel whose rail was widened or put away would otherwise open with it
  // sliding out from 16rem on every single load.
  const painted = React.useRef(false);

  React.useLayoutEffect(() => {
    if (painted.current) {
      paint(hidden ? 0 : width, true);
      return;
    }
    painted.current = true;
    paint(hidden ? 0 : width, false);
    // A frame later, because turning motion back on in the same one would put
    // the transition back before the browser had drawn the width without it.
    const frame = requestAnimationFrame(() => paintMotion(true));
    return () => cancelAnimationFrame(frame);
  }, [width, hidden]);

  const setWidth = React.useCallback((px: number): void => {
    const next = clampWidth(px);
    setWidthState(next);
    store(SIDEBAR_WIDTH_STORAGE_KEY, String(next));
  }, []);

  const setHidden = React.useCallback((next: boolean): void => {
    setHiddenState(next);
    store(SIDEBAR_HIDDEN_STORAGE_KEY, next ? "1" : "0");
  }, []);

  const preview = React.useCallback(
    (px: number | null): void => {
      paint(px ?? (hidden ? 0 : width), px === null);
    },
    [hidden, width],
  );

  return React.useMemo<SidebarLayout>(
    () => ({ width, hidden, setWidth, setHidden, preview }),
    [width, hidden, setWidth, setHidden, preview],
  );
}
