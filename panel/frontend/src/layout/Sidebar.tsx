/* eslint-disable react-refresh/only-export-components -- NAV_ITEMS is the one
   list of routes the shell has; the top bar derives the page title from it. */
import * as React from "react";
import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Code,
  Faders,
  GearSix as SettingsIcon,
  Heart,
  Info,
  ListDots,
  MaskHappy,
  SquaresFour,
  Users,
  X,
  type Icon,
} from "@/lib/icons";

import { Badge } from "@/components/ui/badge";
import { ColorBrandMark } from "@/components/BrandMark";
import { Button } from "@/components/ui/button";
import { PANEL_NAME } from "@/lib/brand";
import { cn } from "@/lib/utils";
import {
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  SIDEBAR_WIDTH_STEP,
  widthForPointer,
  type SidebarLayout,
} from "@/lib/sidebarWidth";

export interface NavItem {
  /** Router path, relative to the panel's base path. */
  to: string;
  /** i18n key for the label and for the page title in the top bar. */
  labelKey: string;
  icon: Icon;
  /** Only "/" matches exactly; the rest also own their sub-paths. */
  end?: boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", labelKey: "nav.dashboard", icon: SquaresFour, end: true },
  { to: "/clients", labelKey: "nav.clients", icon: Users },
  { to: "/server", labelKey: "nav.server", icon: Faders },
  { to: "/obfuscation", labelKey: "nav.obfuscation", icon: MaskHappy },
  { to: "/statistics", labelKey: "nav.statistics", icon: ListDots },
  { to: "/settings", labelKey: "nav.settings", icon: SettingsIcon },
  // Below Settings because that is where the tokens it needs are issued, and
  // above About because it is a thing to do rather than a thing to read.
  { to: "/api-docs", labelKey: "nav.api", icon: Code },
  { to: "/about", labelKey: "nav.about", icon: Info },
];

/**
 * The Support page, which is a nav item everywhere except in the rail's list.
 *
 * It lives at the foot of the sidebar instead, where the tagline used to be -
 * a line of text that said "AmneziaWG server management" to somebody already
 * looking at an AmneziaWG server management panel, and was therefore the one
 * piece of furniture here that could be spent on something. Kept out of the
 * list above on purpose: the eight entries there are places to do the job, and
 * an ask for money sitting among them would be read as one of them.
 *
 * Exported because AppShell derives the top bar's title from a nav item, and a
 * route that no item claims comes out titled "Dashboard".
 */
export const SUPPORT_ITEM: NavItem = {
  to: "/support",
  labelKey: "nav.support",
  icon: Heart,
};

/** What the resize handle and the top bar's toggle both point `aria-controls` at. */
export const SIDEBAR_RAIL_ID = "sidebar-rail";

export interface SidebarProps {
  items: readonly NavItem[];
  version: string;
  /** The panel is running against MockController, so nothing here is a real peer. */
  mock?: boolean;
  /** Drawer state below md. Ignored by the fixed rail on md and up. */
  open: boolean;
  onClose: () => void;
  /** Width and visibility of the rail on md and up. The drawer has neither. */
  layout: SidebarLayout;
}

/**
 * A fixed rail from md up, a slide-over drawer below it. Both render the same
 * body, so a nav item can never exist on one and not the other.
 */
export function Sidebar({
  items,
  version,
  mock = false,
  open,
  onClose,
  layout,
}: SidebarProps): JSX.Element {
  const { t } = useTranslation();
  const closeRef = React.useRef<HTMLButtonElement>(null);

  React.useEffect(() => {
    if (!open) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  React.useEffect(() => {
    if (!open) {
      return;
    }
    // The drawer covers the page; letting the page behind it scroll under the
    // finger is the classic mobile overlay bug.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  return (
    <>
      <aside
        id={SIDEBAR_RAIL_ID}
        className={cn(
          "hidden md:fixed md:inset-y-0 md:start-0 md:z-30 md:flex md:flex-col",
          "md:w-[var(--sidebar-width)] md:border-e md:border-sidebar-border md:bg-sidebar",
          // Clipped rather than reflowed. The body inside holds its minimum
          // width whatever the rail is doing, so closing the rail is a wipe
          // across a still picture rather than seven links folding into each
          // other on the way out.
          "md:overflow-hidden",
          // Visibility rides along with the width. Left to itself it is a
          // discrete property and flips at the start of the close, popping the
          // rail out of existence while the page beside it is still sliding
          // over to fill the space; in a transition it holds until the width
          // has finished and only then goes.
          "md:transition-[width,visibility] md:duration-sidebar",
          // Which is also why this is visibility rather than `display: none` -
          // and why it is not left at width zero either. A rail nobody can see
          // but Tab still walks into is what makes "hidden" a lie for anyone
          // not using a pointer.
          layout.hidden && "md:invisible",
        )}
      >
        <div className="flex min-h-0 flex-1 flex-col" style={{ minWidth: MIN_SIDEBAR_WIDTH }}>
          <SidebarBody items={items} version={version} mock={mock} onNavigate={undefined} />
        </div>
      </aside>

      <SidebarResizer layout={layout} />

      {open ? (
        <div className="md:hidden">
          <div
            aria-hidden="true"
            onClick={onClose}
            // Same scrim as the dialog primitive, so a drawer and a modal read
            // as one system.
            className="fixed inset-0 z-40 bg-overlay/60 backdrop-blur-md animate-in fade-in-0 ease-out"
          />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label={String(t("nav.menu"))}
            className={cn(
              "fixed inset-y-0 start-0 z-50 flex w-72 max-w-[85vw] flex-col",
              "border-e border-sidebar-border bg-sidebar shadow-xl",
              // The drawer slides in from the side the language reads from.
              "animate-in duration-300 ease-out slide-in-from-left-full rtl:slide-in-from-right-full",
            )}
          >
            <Button
              ref={closeRef}
              variant="ghost"
              size="icon"
              onClick={onClose}
              aria-label={String(t("nav.closeMenu"))}
              className="absolute end-2 top-2 text-sidebar-foreground"
            >
              <X aria-hidden="true" />
            </Button>
            <SidebarBody items={items} version={version} mock={mock} onNavigate={onClose} />
          </aside>
        </div>
      ) : null}
    </>
  );
}

/** Is the panel reading right to left, which reverses what "wider" means. */
function isRtl(): boolean {
  return getComputedStyle(document.documentElement).direction === "rtl";
}

/** How far a press has to travel before it counts as a drag rather than a click. */
const DRAG_SLOP = 3;

/**
 * The rail's trailing edge, as something to take hold of.
 *
 * Fixed at the rail's edge rather than inside it, because the rail clips its
 * own overflow and a handle that only reaches inwards is a handle you have to
 * aim at. This straddles the border - four pixels either side - which is what
 * makes it findable without being something the pointer trips over.
 *
 * It carries `role="separator"` with a value and bounds, which is the window
 * splitter pattern: focusable, and driven with the arrow keys by anyone not
 * holding a pointer. What it deliberately does not do is hide the rail on a key
 * press, though a drag past HIDE_SIDEBAR_BELOW does. Hiding the rail takes this
 * handle off screen with it, and with it the focus that pressed the key -
 * whereas the top bar's toggle stays exactly where it is, one Tab away, and
 * does the same job without stranding anybody.
 */
function SidebarResizer({ layout }: { layout: SidebarLayout }): JSX.Element {
  const { t } = useTranslation();
  const { width, hidden, setWidth, setHidden, preview } = layout;

  // A ref rather than state: pointermove has to know whether a drag is running
  // without the answer waiting on a render, and the drag itself must not cause
  // one - see the note in lib/sidebarWidth.ts about what re-renders here cost.
  const drag = React.useRef<{ from: number; moved: boolean } | null>(null);
  // This one is only the handle's own appearance, so it is state, and it
  // re-renders nothing but the handle.
  const [active, setActive] = React.useState(false);

  /** How wide the rail would be if it reached the pointer. */
  const reach = (clientX: number): number => (isRtl() ? window.innerWidth - clientX : clientX);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) {
      return;
    }
    // Capture, so the drag survives the pointer outrunning an 8px strip - which
    // it does immediately, that being the point of dragging it.
    event.currentTarget.setPointerCapture(event.pointerId);
    // Otherwise the gesture starts by selecting the nav labels behind it.
    event.preventDefault();
    drag.current = { from: event.clientX, moved: false };
    setActive(true);
    // Nothing has moved yet; this is only here to take the easing off before
    // the first pointermove lands, so that one does not animate either.
    preview(width);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const state = drag.current;
    if (!state) {
      return;
    }
    // A press that has not travelled is a click, and a click must not move the
    // edge: the handle sits within four pixels of it, so without this every
    // stray press would nudge the rail by however far the aim was off - and the
    // double click that puts the width back would arrive as two of those.
    if (!state.moved && Math.abs(event.clientX - state.from) < DRAG_SLOP) {
      return;
    }
    state.moved = true;
    // Zero rather than the minimum: what the pointer is being shown is where
    // letting go would leave it, so the rail closes under the cursor and opens
    // again if the drag turns back.
    preview(widthForPointer(reach(event.clientX)) ?? 0);
  };

  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>): void => {
    const state = drag.current;
    if (!state) {
      return;
    }
    drag.current = null;
    setActive(false);
    // The preview ends first and the commit follows in the same handler, so
    // both DOM writes land before the browser paints. What it settles on is
    // what the drag last drew, and the re-render is invisible.
    preview(null);
    if (!state.moved) {
      return;
    }
    const next = widthForPointer(reach(event.clientX));
    if (next === null) {
      setHidden(true);
    } else {
      setWidth(next);
    }
  };

  const cancel = React.useCallback((): void => {
    if (!drag.current) {
      return;
    }
    drag.current = null;
    setActive(false);
    preview(null);
  }, [preview]);

  React.useEffect(() => {
    if (!active) {
      return;
    }
    // The cursor belongs to the gesture, not to the 8px it started on: without
    // this it reverts to whatever the pointer is over the moment it leaves the
    // handle, which is every pixel of the drag after the first.
    const body = document.body;
    const cursor = body.style.cursor;
    const select = body.style.userSelect;
    body.style.cursor = "col-resize";
    body.style.userSelect = "none";
    // Escape gets out of a drag the way it gets out of everything else here.
    // The pointer keeps its capture until it is let go; the moves it sends in
    // the meantime are ignored, which is what being cancelled means.
    const onEscape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        cancel();
      }
    };
    window.addEventListener("keydown", onEscape);
    return () => {
      body.style.cursor = cursor;
      body.style.userSelect = select;
      window.removeEventListener("keydown", onEscape);
    };
  }, [active, cancel]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const rtl = isRtl();
    let next: number;
    switch (event.key) {
      case "ArrowLeft":
        next = rtl ? width + SIDEBAR_WIDTH_STEP : width - SIDEBAR_WIDTH_STEP;
        break;
      case "ArrowRight":
        next = rtl ? width - SIDEBAR_WIDTH_STEP : width + SIDEBAR_WIDTH_STEP;
        break;
      case "Home":
        next = MIN_SIDEBAR_WIDTH;
        break;
      case "End":
        next = MAX_SIDEBAR_WIDTH;
        break;
      default:
        return;
    }
    // Arrow keys scroll the page, and Home and End jump it to an end.
    event.preventDefault();
    setWidth(next);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={String(t("nav.resize"))}
      aria-controls={SIDEBAR_RAIL_ID}
      aria-valuenow={width}
      aria-valuemin={MIN_SIDEBAR_WIDTH}
      aria-valuemax={MAX_SIDEBAR_WIDTH}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={cancel}
      onLostPointerCapture={cancel}
      onKeyDown={onKeyDown}
      // Back to the width it shipped at, which is the way out of a rail
      // dragged somewhere unhelpful that does not involve aiming at all.
      onDoubleClick={() => setWidth(DEFAULT_SIDEBAR_WIDTH)}
      className={cn(
        "group fixed inset-y-0 z-40 hidden w-2 touch-none md:block",
        // Negative margin, not a transform: it is the inline start that moves,
        // so the strip straddles the border in either direction the panel reads.
        "-ms-1 start-[var(--sidebar-width)] cursor-col-resize focus-visible:outline-none",
        "transition-[inset-inline-start,visibility] duration-sidebar",
        hidden && "md:invisible",
      )}
    >
      {/* Nothing until it is wanted. A permanent line here would read as a
          second border beside the one the rail already draws, and the rail's
          edge is not information - it is only a thing that can be moved.

          It is also the focus ring: a 2px ring drawn around an 8px strip the
          height of the window is two lines down the page and no indication of
          anything. So the bar itself thickens and comes up to full strength,
          in the same place the pointer would have found it. */}
      <span
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute inset-y-0 end-[3px] bg-sidebar-ring",
          "w-0.5 transition-[width,opacity] group-focus-visible:w-1",
          active
            ? "opacity-100"
            : "opacity-0 group-hover:opacity-50 group-focus-visible:opacity-100",
        )}
      />
    </div>
  );
}

interface SidebarBodyProps {
  items: readonly NavItem[];
  version: string;
  mock: boolean;
  /** Set in the drawer: following a link has to close it. */
  onNavigate: (() => void) | undefined;
}

function SidebarBody({ items, version, mock, onNavigate }: SidebarBodyProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 px-4 py-4 pe-12 md:pe-4">
        {/* No tile behind it: the mark is already a shield, so a filled square
            was a second container saying nothing, and it left the drawing 20px
            to fit lettering into. On its own the mark gets the whole 36px, and
            draws in its own colours rather than the sidebar's accent. */}
        <ColorBrandMark />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-sidebar-foreground">{PANEL_NAME}</p>
          <p className="truncate text-xs text-muted-foreground">{t("nav.version", { version })}</p>
        </div>
      </div>

      <nav aria-label={String(t("nav.menu"))} className="min-h-0 flex-1 overflow-y-auto px-3">
        <ul className="space-y-1 pb-4">
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
                      "focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar",
                      isActive
                        ? "bg-sidebar-primary text-sidebar-primary-foreground shadow-sm"
                        : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                    )
                  }
                >
                  {/* The current route is the one thing this rail has to say
                      at a glance. Its glyph fills to duotone while the rest
                      stay hairline, so the answer survives even where the
                      active pill's own contrast is low. */}
                  {({ isActive }) => (
                    <>
                      <Icon
                        weight={isActive ? "duotone" : "regular"}
                        className="h-4 w-4 shrink-0"
                        aria-hidden="true"
                      />
                      <span className="truncate">{t(item.labelKey)}</span>
                    </>
                  )}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="space-y-2 border-t border-sidebar-border p-3">
        {mock ? (
          <div className="space-y-1.5 px-1 pt-1">
            <Badge variant="warning">{t("nav.demoMode")}</Badge>
            <p className="text-xs leading-snug text-muted-foreground">{t("about.mockModeHint")}</p>
          </div>
        ) : null}

        {/* Built like the rows above it so that following it feels like every
            other move around the panel, and not like leaving for a payment
            page - which is the other thing a link with a heart on it usually
            is. It is smaller than they are, and it is under the rule rather
            than in the list, which is the whole of the difference.

            The heart is the page's ink, filled: black on the light theme,
            white on the dark one, from the one token. Any of the panel's four
            signal colours would have made it an alert, and there is no version
            of this that is urgent. */}
        <NavLink
          to={SUPPORT_ITEM.to}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-xs font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
              "focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar",
              isActive
                ? "bg-sidebar-primary text-sidebar-primary-foreground shadow-sm"
                : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
            )
          }
        >
          {({ isActive }) => (
            <>
              <Heart
                weight="fill"
                className={cn(
                  "h-4 w-4 shrink-0",
                  isActive ? "text-sidebar-primary-foreground" : "text-foreground",
                )}
                aria-hidden="true"
              />
              <span className="truncate">{t(SUPPORT_ITEM.labelKey)}</span>
            </>
          )}
        </NavLink>
      </div>
    </div>
  );
}
