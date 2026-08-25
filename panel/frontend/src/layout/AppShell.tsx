import * as React from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { Sidebar, NAV_ITEMS, SUPPORT_ITEM, type NavItem } from "@/layout/Sidebar";
import { TopBar } from "@/layout/TopBar";
import { PANEL_NAME } from "@/lib/brand";
import { bootstrap } from "@/api/client";
import { useSession } from "@/api/hooks";
import { useSidebarLayout } from "@/lib/sidebarWidth";
import { cn } from "@/lib/utils";

/**
 * Which nav entry owns a path. Only the dashboard matches exactly; every other
 * section keeps its highlight on its sub-paths (/clients/phone).
 *
 * Support is in here although it is not in the rail's list: it is still a
 * route with a name, and leaving it out would title its page "Dashboard" in
 * the top bar and in the browser tab.
 */
const TITLED_ITEMS: readonly NavItem[] = [...NAV_ITEMS, SUPPORT_ITEM];

function activeItem(pathname: string): NavItem | undefined {
  return TITLED_ITEMS.find((item) =>
    item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(`${item.to}/`),
  );
}

/**
 * Sidebar plus top bar plus the routed page. The sidebar is a fixed rail from
 * md up and a drawer below it, so this component owns the one piece of state
 * both halves need.
 */
export function AppShell(): JSX.Element {
  const { t } = useTranslation();
  const location = useLocation();
  const session = useSession();
  const [menuOpen, setMenuOpen] = React.useState(false);
  // The rail's width and whether it is on screen at all. Both halves need it:
  // the rail is what it sizes, and the page beside it is what that width is
  // taken from.
  const sidebar = useSidebarLayout();

  const closeMenu = React.useCallback(() => setMenuOpen(false), []);

  // A drawer left open across a navigation would cover the page it just opened.
  React.useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  // The session is the live copy; the injected bootstrap covers the moment
  // before it resolves and the case where the panel was upgraded since.
  const version = session.data?.version.trim() || bootstrap.version;
  const mock = session.data?.mock ?? false;

  const current = activeItem(location.pathname);
  const title = String(t(current?.labelKey ?? "nav.dashboard"));

  React.useEffect(() => {
    document.title = `${title} - ${PANEL_NAME}`;
  }, [title]);

  return (
    <div className="min-h-dvh bg-background">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:start-4 focus:top-4 focus:z-[60] focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground"
      >
        {t("app.skipToContent")}
      </a>

      <Sidebar
        items={NAV_ITEMS}
        version={version}
        mock={mock}
        open={menuOpen}
        onClose={closeMenu}
        layout={sidebar}
      />

      {/* The page is inset by whatever the rail currently takes, and moves with
          it: the rail is fixed, so this padding is the only thing keeping the
          two from overlapping, and a width that changed without it would slide
          the rail over the page rather than beside it. */}
      <div
        className={cn(
          "flex min-h-dvh flex-col md:ps-[var(--sidebar-width)]",
          "md:transition-[padding-inline-start] md:duration-sidebar",
        )}
      >
        <TopBar
          title={title}
          onOpenMenu={() => setMenuOpen(true)}
          sidebarHidden={sidebar.hidden}
          onToggleSidebar={() => sidebar.setHidden(!sidebar.hidden)}
        />
        {/* A column, so a page with a save bar at the end of it can push that
            bar onto the bottom edge of the window. Left as a block, the bar
            stops wherever the content happens to stop - which on a short tab is
            halfway up the screen, with the page still visible below it. */}
        <main
          id="main-content"
          tabIndex={-1}
          className="mx-auto flex w-full max-w-[100rem] flex-1 flex-col px-4 py-6 focus:outline-none sm:px-6 lg:px-8"
        >
          <Outlet />
        </main>
      </div>
    </div>
  );
}
