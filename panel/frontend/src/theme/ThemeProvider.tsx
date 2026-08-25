/* eslint-disable react-refresh/only-export-components -- useTheme reads the
   context this file creates, so splitting them apart would only move the
   provider one import away from the hook that cannot work without it. */

/**
 * Theme preference: "light", "dark" or "system".
 *
 * The choice lives in localStorage because the panel has no per-user profile on
 * the server worth a round trip for this, and because index.html has to be able
 * to read it synchronously before the first paint. That inline script owns the
 * initial class; this provider owns every change after mount.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { ThemePreference } from "@/api/types";

/** Same union the API uses for the stored `theme` setting. */
export type Theme = ThemePreference;

/** What is actually on screen once "system" has been resolved. */
export type ResolvedTheme = "light" | "dark";

/** Canonical storage key. */
export const THEME_STORAGE_KEY = "awg-panel-theme";

/**
 * The pre-paint script in index.html reads this shorter key. Writing both keeps
 * a reload of a dark panel from flashing white, which is the whole point of
 * deciding the theme before React mounts.
 */
const BOOTSTRAP_STORAGE_KEY = "awg-theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

/**
 * Class that lets every surface cross-fade instead of snapping to the new
 * palette. It is on the root only while a change is in flight, so the rest of
 * the time no element carries a blanket colour transition.
 */
const FADE_CLASS = "theme-transition";

/** Length of that fade in index.css, plus a frame of slack. */
const FADE_MS = 460;

/** Class that scopes the sweep's view-transition rules in index.css. */
const SWEEP_CLASS = "theme-sweep";

/**
 * Length of the sweep, and the curve the circle grows on.
 *
 * A circle's area goes up with the square of its radius, so a radius moving at a
 * constant speed uncovers the window faster and faster: the reveal is already
 * past halfway before it looks like it has started, and what the eye catches is
 * the end of it rather than where it came from. An ease-out curve, which is the
 * usual choice for something entering, makes that worse by putting the speed at
 * the front as well.
 *
 * So this is eased at both ends. The first frames barely move, which is what
 * makes the circle read as opening from the point the operator touched rather
 * than as a wash arriving from somewhere off to one side, and the last frames
 * settle rather than stop. The two effects cancel: the rate the page actually
 * changes over stays roughly even from one edge of the window to the other.
 *
 * The duration is also how long the page is unresponsive, because a view
 * transition holds the real document behind the transition while it runs. 700ms
 * is the top of what that can be spent on: enough for the spread to be a
 * movement someone can follow, not so much that a second click goes missing.
 */
const SWEEP_MS = 700;
const SWEEP_EASING = "cubic-bezier(0.4, 0, 0.2, 1)";

/**
 * How recently the operator has to have done something for a theme change to
 * count as theirs.
 *
 * A change arrives as a state update and carries no coordinates: the same
 * setTheme runs for a dropdown item, a settings row, another tab and the
 * operating system. So the origin of the sweep is taken from the last thing the
 * operator touched, and only if it was recent enough to plausibly be the cause.
 * Nothing recent means nobody clicked, which is exactly when a circle opening
 * out of the corner of the window would be a mystery instead of an answer.
 */
const INTERACTION_MAX_AGE_MS = 1200;

/** Where the sweep opens from, in client coordinates. */
interface Origin {
  x: number;
  y: number;
  /** performance.now() timeline, which is what event.timeStamp is measured on. */
  at: number;
}

export interface ThemeContextValue {
  /** The preference, including "system". This is what a settings control binds to. */
  theme: Theme;
  /** The theme in effect right now. Charts and canvases need this, not the preference. */
  resolvedTheme: ResolvedTheme;
  /** What the operating system currently asks for, whatever the preference is. */
  systemTheme: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

function readStoredTheme(): Theme | null {
  try {
    const raw =
      window.localStorage.getItem(THEME_STORAGE_KEY) ??
      window.localStorage.getItem(BOOTSTRAP_STORAGE_KEY);
    return isTheme(raw) ? raw : null;
  } catch {
    // Storage blocked (private mode, hardened browser). Not worth surfacing.
    return null;
  }
}

function writeStoredTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    window.localStorage.setItem(BOOTSTRAP_STORAGE_KEY, theme);
  } catch {
    // The choice still applies to this tab; it just will not survive a reload.
  }
}

function readSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

function applyTheme(resolved: ResolvedTheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  // Scrollbars, form widgets and the browser's own UI follow color-scheme, not
  // the class, so a dark panel with light scrollbars is what happens without it.
  root.style.colorScheme = resolved;
}

function prefersReducedMotion(): boolean {
  if (typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * The circle has to cover the window from wherever it starts, so it grows to the
 * furthest corner: the further axis on each side, then the diagonal between them.
 * Anything shorter leaves a wedge of the old theme behind at the end.
 */
function sweepRadius(origin: Origin): number {
  return Math.hypot(
    Math.max(origin.x, window.innerWidth - origin.x),
    Math.max(origin.y, window.innerHeight - origin.y),
  );
}

export interface ThemeProviderProps {
  children: ReactNode;
  /** Used only when nothing has been stored yet. */
  defaultTheme?: Theme;
}

export function ThemeProvider({
  children,
  defaultTheme = "system",
}: ThemeProviderProps): JSX.Element {
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme() ?? defaultTheme);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(readSystemTheme);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia(DARK_QUERY);
    const onChange = (event: MediaQueryListEvent): void => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    // The OS can flip between render and effect, e.g. at sunset on a schedule.
    setSystemTheme(query.matches ? "dark" : "light");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const onStorage = (event: StorageEvent): void => {
      if (event.key !== THEME_STORAGE_KEY && event.key !== BOOTSTRAP_STORAGE_KEY) {
        return;
      }
      if (isTheme(event.newValue)) {
        setThemeState(event.newValue);
      }
    };
    // Operators often keep the dashboard open in one tab and work in another.
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const resolvedTheme: ResolvedTheme = theme === "system" ? systemTheme : theme;

  // The first pass only agrees with what index.html already painted, so fading
  // it would fade the page in from the wrong palette on every load.
  const applied = useRef(false);

  // The last thing the operator did, whatever it was for. See INTERACTION_MAX_AGE_MS.
  const interaction = useRef<Origin | null>(null);

  // Which change owns the sweep class right now. Toggling twice inside half a
  // second leaves the first transition to finish after the second has started,
  // and the class has to survive that: taking it off mid-sweep hands the second
  // one back to the browser's cross-fade, halfway through.
  const changes = useRef(0);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent): void => {
      interaction.current = { x: event.clientX, y: event.clientY, at: event.timeStamp };
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      // A key has no coordinates, but the control it lands on does - and the
      // theme menu is as reachable by keyboard as by mouse. The body is not a
      // control: it is where focus sits when nothing has it, and its rect is the
      // whole page, so it would put the sweep in the middle of the window on a
      // change the operator had nothing to do with.
      const target = event.target;
      if (!(target instanceof HTMLElement) || target === document.body) {
        return;
      }
      const rect = target.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) {
        return;
      }
      interaction.current = {
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        at: event.timeStamp,
      };
    };
    // Capture, and on the window: a menu that stops propagation on its own
    // events would otherwise hide the very press that is about to change theme.
    const options = { capture: true, passive: true } as const;
    window.addEventListener("pointerdown", onPointerDown, options);
    window.addEventListener("keydown", onKeyDown, options);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, { capture: true });
      window.removeEventListener("keydown", onKeyDown, { capture: true });
    };
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const change = ++changes.current;

    if (!applied.current) {
      applied.current = true;
      applyTheme(resolvedTheme);
      return;
    }

    const recent = interaction.current;
    const origin = recent && performance.now() - recent.at < INTERACTION_MAX_AGE_MS ? recent : null;

    // View transitions are the only way to have both palettes on screen at once;
    // without them, and whenever the change was not asked for, the cross-fade is
    // the whole of the animation.
    if (origin && !prefersReducedMotion() && typeof document.startViewTransition === "function") {
      root.classList.add(SWEEP_CLASS);
      const transition = document.startViewTransition(() => {
        applyTheme(resolvedTheme);
      });

      let live = true;
      void transition.ready.then(
        () => {
          if (!live) {
            return;
          }
          const radius = sweepRadius(origin);
          root.animate(
            {
              clipPath: [
                `circle(0px at ${origin.x}px ${origin.y}px)`,
                `circle(${radius}px at ${origin.x}px ${origin.y}px)`,
              ],
            },
            {
              duration: SWEEP_MS,
              easing: SWEEP_EASING,
              pseudoElement: "::view-transition-new(root)",
            },
          );
        },
        () => {
          // Skipped rather than failed: a background tab, or a second change
          // arriving on this one's heels. The new theme is applied either way,
          // which is the part that matters.
        },
      );

      void transition.finished.then(() => {
        if (changes.current === change) {
          root.classList.remove(SWEEP_CLASS);
        }
      });

      return () => {
        live = false;
      };
    }

    root.classList.add(FADE_CLASS);
    applyTheme(resolvedTheme);
    const timer = window.setTimeout(() => root.classList.remove(FADE_CLASS), FADE_MS);
    return () => {
      window.clearTimeout(timer);
      root.classList.remove(FADE_CLASS);
    };
  }, [resolvedTheme]);

  const setTheme = useCallback((next: Theme): void => {
    writeStoredTheme(next);
    setThemeState(next);
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, resolvedTheme, systemTheme, setTheme }),
    [theme, resolvedTheme, systemTheme, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used inside <ThemeProvider>");
  }
  return context;
}
