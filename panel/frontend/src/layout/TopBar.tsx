import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CaretLeft,
  CaretRight,
  Check,
  Desktop,
  GearSix as SettingsIcon,
  List,
  Moon,
  SignOut,
  Sun,
  Translate,
  UserCircle,
  type Icon,
} from "@/lib/icons";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import { NO_PEERS, useLiveStats, useLogout, useSession } from "@/api/hooks";
import { SIDEBAR_RAIL_ID } from "@/layout/Sidebar";
import { LANGUAGES, currentLanguage, setLanguage } from "@/i18n";
import { useTheme } from "@/theme/ThemeProvider";
import { cn, formatDuration } from "@/lib/utils";
import type { ThemePreference } from "@/api/types";

/*
 * The collector rewrites live.json every couple of seconds. A blob older than
 * this means the collector is not running, which on screen looks exactly like a
 * quiet server unless the bar says otherwise.
 */
const STALE_AFTER_SEC = 30;

const THEME_OPTIONS: readonly { value: ThemePreference; labelKey: string; icon: Icon }[] = [
  { value: "light", labelKey: "theme.light", icon: Sun },
  { value: "dark", labelKey: "theme.dark", icon: Moon },
  { value: "system", labelKey: "theme.system", icon: Desktop },
];

export interface TopBarProps {
  /** Already translated: the shell owns the route-to-title mapping. */
  title: string;
  onOpenMenu: () => void;
  /** The rail is off screen on md and up, so this bar carries the way back. */
  sidebarHidden: boolean;
  onToggleSidebar: () => void;
}

export function TopBar({
  title,
  onOpenMenu,
  sidebarHidden,
  onToggleSidebar,
}: TopBarProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <header
      className={cn(
        "sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b border-border",
        "bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:px-6 lg:px-8",
      )}
    >
      <Button
        variant="ghost"
        size="icon"
        className="md:hidden"
        onClick={onOpenMenu}
        aria-label={String(t("nav.openMenu"))}
      >
        <List aria-hidden="true" />
      </Button>

      {/* The same slot on md and up, where there is a rail to put away rather
          than a drawer to open. It is drawn in both states rather than only
          when the rail is gone: a control that appears where nothing was is a
          control nobody knew they had, and the first thing somebody who has
          just dragged the rail off screen looks for is where it went. The caret
          points the way it would move. */}
      <Button
        variant="ghost"
        size="icon"
        className="hidden md:inline-flex"
        onClick={onToggleSidebar}
        aria-label={String(t(sidebarHidden ? "nav.expand" : "nav.collapse"))}
        aria-expanded={!sidebarHidden}
        aria-controls={SIDEBAR_RAIL_ID}
      >
        {sidebarHidden ? (
          <CaretRight aria-hidden="true" className="rtl:rotate-180" />
        ) : (
          <CaretLeft aria-hidden="true" className="rtl:rotate-180" />
        )}
      </Button>

      {/* Not a heading: the page below owns the only <h1>. This repeats the
          route name so a scrolled page still says where it is. */}
      <span className="min-w-0 truncate text-sm font-semibold tracking-tight">{title}</span>

      <div className="ms-auto flex items-center gap-1 sm:gap-2">
        <InterfaceIndicator />
        <ThemeMenu />
        <LanguageMenu />
        <AccountMenu />
      </div>
    </header>
  );
}

/** Is the tunnel carrying traffic right now, and can the panel still tell. */
function InterfaceIndicator(): JSX.Element {
  const { t } = useTranslation();
  // No peers: this reads the interface flag and the timestamp, and it is on
  // every page in the panel. Asking for the peer table as well would put a
  // copy of the whole server on the wire every two seconds behind every screen.
  const live = useLiveStats({ peers: NO_PEERS });

  if (live.isPending) {
    return (
      <span className="flex items-center gap-2 px-1.5 text-xs text-muted-foreground">
        <Spinner size="sm" decorative className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">{t("common.loading")}</span>
      </span>
    );
  }

  const stale = live.isError || !live.data || Date.now() / 1000 - live.data.ts > STALE_AFTER_SEC;
  const up = !stale && (live.data?.ifaceUp ?? false);

  const label = String(t(stale ? "status.unknown" : up ? "server.statusUp" : "server.statusDown"));
  const help = String(
    t(stale ? "dashboard.collectorOfflineHint" : up ? "nav.tunnelUpHelp" : "nav.tunnelDownHelp"),
  );

  /*
   * How long it has been up, for the hover.
   *
   * The dot is the one piece of tunnel state on every page, and "up" is the
   * least of what somebody looking at it wants to know: the question behind the
   * glance is almost always whether it has been up the whole time, or came back
   * a minute ago without anybody noticing it had gone. The dashboard card
   * answers that, and this is the same answer where the eye already is.
   *
   * Same arithmetic as the card, deliberately: both ends of the subtraction come
   * from the blob, so a browser clock an hour out does not read an hour of extra
   * uptime, and the figure advances on the poll rather than on a timer - it
   * stops when the collector does, which by then the dot is already saying.
   *
   * A zero `ifaceSince` is the collector not having observed the interface come
   * up rather than a tunnel with no age, so there is no duration to quote and
   * the tooltip says only what it said before.
   */
  const since = live.data?.ifaceSince ?? 0;
  const upFor =
    up && since > 0
      ? String(
          t("nav.tunnelUpFor", {
            duration: formatDuration(Math.max(0, (live.data?.ts ?? 0) - since)),
          }),
        )
      : "";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          // The tooltip is what a pointer gets; this is the same sentence for
          // the reader who arrives on the element by keyboard.
          aria-label={`${String(t("server.status"))}: ${label}${upFor ? `. ${upFor}` : ""}`}
          className={cn(
            "flex items-center gap-2 rounded-md px-1.5 py-1 text-xs font-medium",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            stale ? "text-muted-foreground" : up ? "text-success" : "text-destructive",
          )}
        >
          <span
            aria-hidden="true"
            className={cn(
              "inline-block h-2.5 w-2.5 shrink-0 rounded-full",
              stale
                ? "border-2 border-muted-foreground/70"
                : up
                  ? "bg-success ring-4 ring-success/20"
                  : "bg-destructive",
            )}
          />
          <span className="hidden sm:inline">{label}</span>
        </span>
      </TooltipTrigger>
      {/* The duration leads and the sentence explaining the state follows it,
          muted: on a tunnel that has been up for a week the first line is the
          only one being read, and on one that has just come back it is the line
          that says so. */}
      <TooltipContent>
        {upFor ? <p className="font-medium">{upFor}</p> : null}
        <p className={cn(upFor && "mt-0.5 text-muted-foreground")}>{help}</p>
      </TooltipContent>
    </Tooltip>
  );
}

function ThemeMenu(): JSX.Element {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();
  const current = THEME_OPTIONS.find((option) => option.value === theme) ?? THEME_OPTIONS[2];
  const CurrentIcon = current.icon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={String(t("theme.toggle"))}>
          <CurrentIcon aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t("theme.label")}</DropdownMenuLabel>
        {THEME_OPTIONS.map((option) => {
          const Icon = option.icon;
          return (
            <DropdownMenuItem key={option.value} onSelect={() => setTheme(option.value)}>
              <Icon aria-hidden="true" />
              <span className="flex-1">{t(option.labelKey)}</span>
              {option.value === theme ? <Check aria-hidden="true" /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function LanguageMenu(): JSX.Element {
  const { t } = useTranslation();
  // useTranslation re-renders on languageChanged, and currentLanguage collapses
  // "ru-RU" to "ru", so the tick cannot land on the wrong row.
  const active = currentLanguage();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={String(t("language.change"))}>
          <Translate aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t("language.label")}</DropdownMenuLabel>
        {LANGUAGES.map((language) => (
          <DropdownMenuItem
            key={language.code}
            // Names stay in their own script: someone who cannot read the
            // current language still has to find their own.
            lang={language.code}
            onSelect={() => setLanguage(language.code)}
          >
            <span className="flex-1">{language.nativeName}</span>
            {language.code === active ? <Check aria-hidden="true" /> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AccountMenu(): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { toast } = useToast();
  const session = useSession();
  const logout = useLogout();

  const username = session.data?.username ?? "";

  const signOut = React.useCallback(() => {
    logout.mutate(undefined, {
      onSuccess: () => {
        navigate("/login", { replace: true });
        toast({ title: String(t("nav.loggedOut")), variant: "success" });
      },
      onError: (error) => {
        toast({
          title: String(t("nav.signOutFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  }, [logout, navigate, t, toast]);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="gap-2 px-2"
          aria-label={String(t("nav.account"))}
        >
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-secondary text-secondary-foreground">
            <UserCircle weight="bold" className="h-3.5 w-3.5" aria-hidden="true" />
          </span>
          <span className="hidden max-w-[10rem] truncate sm:inline">{username}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[12rem]">
        <DropdownMenuLabel className="normal-case">
          {t("nav.signedInAs", { username })}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/settings">
            <SettingsIcon aria-hidden="true" />
            <span>{t("nav.settings")}</span>
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem destructive disabled={logout.isPending} onSelect={signOut}>
          {logout.isPending ? <Spinner size="sm" decorative /> : <SignOut aria-hidden="true" />}
          <span>{t(logout.isPending ? "nav.loggingOut" : "nav.logout")}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
