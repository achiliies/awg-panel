import * as React from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowsClockwise,
  Check,
  Desktop,
  Eye,
  EyeSlash,
  Moon,
  ShieldCheck,
  SignIn,
  Sun,
  Translate,
  Warning,
  WifiSlash,
  type Icon,
} from "@/lib/icons";

import { Badge } from "@/components/ui/badge";
import { BrandMark } from "@/components/BrandMark";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { bootstrap } from "@/api/client";
import { useLogin, useSession } from "@/api/hooks";
import { LANGUAGES, currentLanguage, setLanguage } from "@/i18n";
import { useTheme } from "@/theme/ThemeProvider";
import { MARK_TILE_MASK, MARK_TILE_SIZE, PANEL_NAME } from "@/lib/brand";
import { cn } from "@/lib/utils";
import type { ApiError, ThemePreference } from "@/api/types";

/*
 * The only page outside the app shell, and the only one an unauthenticated
 * visitor can reach.
 *
 * Three things here are not ordinary form handling:
 *
 * - The code field does not exist until the panel says it does. Asking every
 *   operator for a six-digit code they may not have is worse than one extra
 *   round trip, so the first attempt sends username and password, and a 401
 *   whose detail is the marker "totp_required" is what grows the field.
 * - A lockout is not a wrong password. django-axes blocks the source address
 *   after a handful of failures and answers 403 with the cool-off in
 *   errors.lockout, so the form counts that down instead of inviting an attempt
 *   that cannot succeed and would extend the block.
 * - The card shakes once per rejection. It reads as "no" before any sentence
 *   has been parsed, and prefers-reduced-motion in index.css takes it away for
 *   anyone who asked for that.
 */

/** Where the last successful username is kept, alongside awg-panel-theme and -lang. */
const USERNAME_STORAGE_KEY = "awg-panel-username";

/** Nothing above this is worth a running countdown; below it, mm:ss reads as one. */
const HOUR_SECONDS = 3600;

type FieldName = "username" | "password" | "totp";

/**
 * A rejection kept as a descriptor rather than a finished sentence, so the text
 * is chosen at render time and follows a language change made on this page.
 */
type Failure =
  | { kind: "credentials" }
  /** `detail` is the panel's own wording, which distinguishes a wrong code from a throttled device. */
  | { kind: "totp"; detail: string }
  | { kind: "offline" }
  | { kind: "stale" }
  | { kind: "other"; detail: string };

interface Lockout {
  /** Unix ms when the block lifts, or null when it has no automatic expiry. */
  until: number | null;
  /** The panel's sentence; for a block with no expiry it names the command that clears it. */
  detail: string;
}

const THEME_OPTIONS: readonly { value: ThemePreference; labelKey: string; icon: Icon }[] = [
  { value: "light", labelKey: "theme.light", icon: Sun },
  { value: "dark", labelKey: "theme.dark", icon: Moon },
  { value: "system", labelKey: "theme.system", icon: Desktop },
];

function readRememberedUsername(): string {
  try {
    return window.localStorage.getItem(USERNAME_STORAGE_KEY) ?? "";
  } catch {
    // Storage blocked (private window, hardened browser): the field starts empty.
    return "";
  }
}

function rememberUsername(name: string): void {
  try {
    window.localStorage.setItem(USERNAME_STORAGE_KEY, name);
  } catch {
    // The sign-in worked; failing to remember the name is not worth a message.
  }
}

/**
 * Where to go once signed in. RequireAuth puts the interrupted location in
 * history state, but history state can hold anything a link author put there,
 * so only an in-app absolute path is accepted and "//host" is rejected as the
 * off-site redirect it would be.
 *
 * A backslash is rejected with it. "/\evil.example" passes both of the other
 * two tests - it starts with one slash and not with two - and browsers treat a
 * backslash in a path as a slash, so react-router 6 resolves it as the
 * protocol-relative URL "//evil.example" and leaves the site. That is
 * GHSA-wrjc-x8rr-h8h6, whose first fixed release is a major version away, and
 * it is reachable from here: an admin who is signed out, follows a link to
 * `https://panel.example/\evil.example`, and signs in would land on the
 * attacker's page wearing the panel's own referrer.
 *
 * Rejecting it here rather than waiting for the upgrade, because this function
 * is the only thing on that path that belongs to us, and a router that stops
 * resolving backslashes will not miss the check.
 */
function redirectTarget(state: unknown): string {
  const home = "/";
  if (typeof state !== "object" || state === null) {
    return home;
  }
  const record = state as Record<string, unknown>;
  const from =
    typeof record.from === "object" && record.from !== null
      ? (record.from as Record<string, unknown>)
      : null;
  const next =
    typeof record.next === "string"
      ? record.next
      : from && typeof from.pathname === "string"
        ? `${from.pathname}${typeof from.search === "string" ? from.search : ""}`
        : "";

  if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return home;
  }
  // Bouncing back to the login page would sign the user in and show them this
  // form again.
  if (next === "/login" || next.startsWith("/login?")) {
    return home;
  }
  return next;
}

/** The 403 that means "blocked", or null for the 403 that means something else. */
function lockoutFrom(error: ApiError): Lockout | null {
  const raw = error.fieldError("lockout");
  if (raw === undefined) {
    // No machine-readable cool-off: a reverse proxy or an older panel answered,
    // and the wording is all there is to go on.
    return error.lockedOut ? { until: null, detail: error.detail } : null;
  }
  const seconds = Number.parseInt(raw, 10);
  return {
    // "0" is axes with no cool-off configured: the block holds until it is
    // cleared on the server, so there is nothing to count down.
    until: Number.isFinite(seconds) && seconds > 0 ? Date.now() + seconds * 1000 : null,
    detail: error.detail,
  };
}

/** mm:ss while that is meaningful, whole hours beyond it. */
function formatWait(seconds: number, hoursLabel: string): string {
  if (seconds >= HOUR_SECONDS) {
    return `${Math.ceil(seconds / HOUR_SECONDS)} ${hoursLabel}`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export default function Login(): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const session = useSession();
  const login = useLogin();

  const [username, setUsername] = React.useState(readRememberedUsername);
  const [password, setPassword] = React.useState("");
  const [totp, setTotp] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [needsTotp, setNeedsTotp] = React.useState(false);
  const [missing, setMissing] = React.useState<FieldName | null>(null);
  const [failure, setFailure] = React.useState<Failure | null>(null);
  const [lockout, setLockout] = React.useState<Lockout | null>(null);
  const [remaining, setRemaining] = React.useState(0);
  const [rejections, setRejections] = React.useState(0);

  const cardRef = React.useRef<HTMLDivElement>(null);
  const usernameRef = React.useRef<HTMLInputElement>(null);
  const passwordRef = React.useRef<HTMLInputElement>(null);
  const totpRef = React.useRef<HTMLInputElement>(null);
  const rememberedRef = React.useRef(username !== "");

  const target = redirectTarget(location.state);
  const version = session.data?.version ?? bootstrap.version;
  const until = lockout?.until ?? null;

  // A remembered name means the password is the only thing left to type.
  React.useEffect(() => {
    const node = rememberedRef.current ? passwordRef.current : usernameRef.current;
    node?.focus();
  }, []);

  React.useEffect(() => {
    if (needsTotp) {
      totpRef.current?.focus();
    }
  }, [needsTotp]);

  React.useEffect(() => {
    if (until === null) {
      setRemaining(0);
      return;
    }
    const tick = (): void => {
      const left = Math.max(0, Math.round((until - Date.now()) / 1000));
      setRemaining(left);
      if (left <= 0) {
        // The block has lifted: clearing it re-enables the form rather than
        // leaving a stale "try again in 0:00" on screen.
        setLockout(null);
      }
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [until]);

  React.useEffect(() => {
    const node = cardRef.current;
    if (rejections === 0 || node === null) {
      return;
    }
    // Toggling the class through state would not restart the animation: without
    // a layout flush between the removal and the addition the browser never
    // sees a change. Reading a geometry property forces that flush.
    node.classList.remove("animate-shake");
    node.getBoundingClientRect();
    node.classList.add("animate-shake");
  }, [rejections]);

  const reject = React.useCallback((next: Failure | null): void => {
    setFailure(next);
    setRejections((count) => count + 1);
  }, []);

  const handleError = React.useCallback(
    (error: ApiError): void => {
      const blocked = lockoutFrom(error);
      if (blocked !== null) {
        setLockout(blocked);
        reject(null);
        return;
      }

      if (error.totpRequired) {
        // Progress, not a failure: the account has a second factor and the form
        // has to grow a field before it can be answered.
        setNeedsTotp(true);
        setFailure(null);
        return;
      }

      if (error.offline) {
        reject({ kind: "offline" });
        return;
      }

      if (error.status === 401) {
        const totpDetail = error.fieldError("totp");
        // The panel names the code field only when the password was accepted
        // and the code was not, so this is the one reliable way to tell the two
        // rejections apart.
        if (totpDetail !== undefined) {
          setTotp("");
          reject({ kind: "totp", detail: totpDetail });
          totpRef.current?.focus();
        } else {
          reject({ kind: "credentials" });
          passwordRef.current?.select();
        }
        return;
      }

      if (error.status === 403) {
        reject({ kind: "stale" });
        return;
      }

      reject({ kind: "other", detail: error.detail });
    },
    [reject],
  );

  const busy = login.isPending;
  // The session request is also what plants the CSRF cookie this form has to
  // send back, so submitting before it lands would fail for a reason the user
  // cannot act on.
  const canSubmit = !busy && !session.isPending && lockout === null;

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }

    const name = username.trim();
    const code = totp.trim();

    if (name === "") {
      setMissing("username");
      usernameRef.current?.focus();
      return;
    }
    if (password === "") {
      setMissing("password");
      passwordRef.current?.focus();
      return;
    }
    if (needsTotp && code === "") {
      setMissing("totp");
      totpRef.current?.focus();
      return;
    }

    setMissing(null);
    setFailure(null);
    login.mutate(
      { username: name, password, ...(code === "" ? {} : { totp: code }) },
      {
        onSuccess: () => {
          rememberUsername(name);
          navigate(target, { replace: true });
        },
        onError: handleError,
      },
    );
  };

  // Reached by opening /login with a live session, and again for a moment after
  // a successful sign-in, before the navigate() above runs.
  if (session.data?.authenticated === true) {
    return <Navigate to={target} replace />;
  }

  const failureText = ((): string => {
    switch (failure?.kind) {
      case "credentials":
        return String(t("login.failed"));
      case "totp":
        return String(t("login.totpFailed"));
      case "offline":
        return String(t("login.offline"));
      case "stale":
        // defaultValue only for the two keys this page introduced: an untranslated
        // catalog must not put a raw key in front of someone who cannot sign in.
        return String(
          t("login.staleForm", {
            defaultValue:
              "This page has been open too long and its security token expired. Reload the page and sign in again.",
          }),
        );
      case "other":
        return failure.detail || String(t("errors.generic"));
      default:
        return "";
    }
  })();

  const sessionOffline = session.isError && session.error.offline;

  return (
    <div className="relative flex min-h-dvh flex-col items-center justify-center overflow-hidden bg-background px-4 py-10 sm:px-6">
      <Backdrop />

      <main className="relative flex w-full max-w-sm flex-col items-center">
        <Card
          ref={cardRef}
          className="w-full shadow-lg"
          // Child animations (the code field sliding in) bubble here too, and
          // clearing the class on one of those would cut the shake short.
          onAnimationEnd={(event) => {
            if (event.target === event.currentTarget) {
              event.currentTarget.classList.remove("animate-shake");
            }
          }}
        >
          <CardHeader className="items-center gap-2 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary ring-1 ring-primary/20">
              <BrandMark className="h-7 w-7" />
            </span>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <p className="text-sm font-medium text-foreground">{PANEL_NAME}</p>
              {session.data?.mock === true ? (
                <Badge variant="warning" title={String(t("about.mockModeHint"))}>
                  {t("about.mockMode")}
                </Badge>
              ) : null}
            </div>
            <h1 className="text-xl font-semibold tracking-tight">{t("login.title")}</h1>
            <CardDescription>{t("login.subtitle")}</CardDescription>
          </CardHeader>

          <CardContent>
            <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
              {lockout !== null ? (
                <Notice tone="destructive" icon={Warning} title={String(t("login.lockedOut"))}>
                  <p className="text-sm text-foreground">
                    {until !== null
                      ? t("login.lockedOutRetry", {
                          duration: formatWait(remaining, String(t("units.hours"))),
                          defaultValue: "You can try again in {{duration}}.",
                        })
                      : lockout.detail || t("login.lockedOutHint")}
                  </p>
                  {until !== null ? (
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      {t("login.lockedOutHint")}
                    </p>
                  ) : null}
                </Notice>
              ) : failure !== null ? (
                <Notice
                  tone="destructive"
                  icon={failure.kind === "offline" ? WifiSlash : Warning}
                  title={failureText}
                >
                  {failure.kind === "totp" && failure.detail !== "" ? (
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      {failure.detail}
                    </p>
                  ) : null}
                </Notice>
              ) : sessionOffline ? (
                <Notice tone="destructive" icon={WifiSlash} title={String(t("login.offline"))}>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void session.refetch()}
                    disabled={session.isFetching}
                  >
                    <ArrowsClockwise aria-hidden="true" />
                    {t("common.retry")}
                  </Button>
                </Notice>
              ) : needsTotp ? (
                <Notice tone="info" icon={ShieldCheck} title={String(t("login.totpRequired"))} />
              ) : null}

              <div className="grid gap-1.5">
                <Label htmlFor="awg-login-username">{t("login.username")}</Label>
                <Input
                  id="awg-login-username"
                  ref={usernameRef}
                  name="username"
                  value={username}
                  onChange={(event) => {
                    setUsername(event.target.value);
                    setMissing(null);
                  }}
                  placeholder={String(t("login.usernamePlaceholder"))}
                  autoComplete="username"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  disabled={busy}
                  aria-invalid={missing === "username" || undefined}
                  aria-describedby={missing === "username" ? "awg-login-username-error" : undefined}
                />
                {missing === "username" ? (
                  <p id="awg-login-username-error" className="text-xs text-destructive">
                    {t("validation.required")}
                  </p>
                ) : null}
              </div>

              <div className="grid gap-1.5">
                <Label htmlFor="awg-login-password">{t("login.password")}</Label>
                <div className="relative">
                  <Input
                    id="awg-login-password"
                    ref={passwordRef}
                    name="password"
                    type={showPassword ? "text" : "password"}
                    className="pe-10"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value);
                      setMissing(null);
                    }}
                    placeholder={String(t("login.passwordPlaceholder"))}
                    autoComplete="current-password"
                    spellCheck={false}
                    disabled={busy}
                    aria-invalid={
                      missing === "password" || failure?.kind === "credentials" || undefined
                    }
                    aria-describedby={
                      missing === "password" ? "awg-login-password-error" : undefined
                    }
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="absolute end-0 top-0 text-muted-foreground hover:bg-transparent hover:text-foreground"
                    onClick={() => setShowPassword((shown) => !shown)}
                    aria-pressed={showPassword}
                    aria-label={String(
                      t(showPassword ? "login.hidePassword" : "login.showPassword"),
                    )}
                    disabled={busy}
                  >
                    {showPassword ? <EyeSlash aria-hidden="true" /> : <Eye aria-hidden="true" />}
                  </Button>
                </div>
                {missing === "password" ? (
                  <p id="awg-login-password-error" className="text-xs text-destructive">
                    {t("validation.required")}
                  </p>
                ) : null}
              </div>

              {needsTotp ? (
                <div className="grid gap-1.5 duration-200 animate-in fade-in slide-in-from-top-2">
                  <Label htmlFor="awg-login-totp">{t("login.totp")}</Label>
                  <Input
                    id="awg-login-totp"
                    ref={totpRef}
                    name="totp"
                    value={totp}
                    // Six digits normally, eight on a device configured that way.
                    // Dropping everything else lets a code pasted with a space
                    // through instead of failing on the server.
                    onChange={(event) => {
                      setTotp(event.target.value.replace(/\D/g, "").slice(0, 8));
                      setMissing(null);
                    }}
                    placeholder={String(t("login.totpPlaceholder"))}
                    className="text-center font-mono text-base tracking-[0.35em]"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    spellCheck={false}
                    disabled={busy}
                    aria-invalid={missing === "totp" || failure?.kind === "totp" || undefined}
                    aria-describedby="awg-login-totp-hint"
                  />
                  <p
                    id="awg-login-totp-hint"
                    className="text-xs leading-relaxed text-muted-foreground"
                  >
                    {missing === "totp" ? (
                      <span className="text-destructive">{t("validation.required")}</span>
                    ) : (
                      t("login.totpHint")
                    )}
                  </p>
                </div>
              ) : null}

              <Button
                type="submit"
                size="lg"
                className="w-full"
                loading={busy}
                disabled={!canSubmit}
              >
                {busy ? (
                  <>
                    <Spinner size="sm" decorative />
                    {t("login.submitting")}
                  </>
                ) : (
                  <>
                    <SignIn aria-hidden="true" />
                    {t("login.submit")}
                  </>
                )}
              </Button>

              {session.isPending ? (
                <p className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
                  <Spinner size="sm" decorative className="h-3.5 w-3.5" />
                  {t("auth.checking")}
                </p>
              ) : null}
            </form>
          </CardContent>
        </Card>

        <div className="mt-4 flex items-center gap-1">
          <ThemeMenu />
          <LanguageMenu />
        </div>

        <p className="mt-3 text-xs text-muted-foreground">
          {t("about.version")} <span className="font-mono">{version}</span>
        </p>
      </main>
    </div>
  );
}

/**
 * A grid of marks faded towards the edges plus two soft washes. Every colour is
 * a theme variable, so the dark theme gets the same shape at its own luminance
 * instead of a second hand-picked palette.
 *
 * The grid used to be drawn with a radial-gradient dot per cell. It is the
 * panel's own silhouette now, at the same spacing and about the same weight on
 * the page: the mark is wider than the dot was, so it is laid down at roughly
 * half the tint to keep the texture as quiet as it was. Two nested elements
 * rather than one because both the grid and the fade are masks, and
 * mask-composite is the kind of property whose keywords differ between engines.
 *
 * Both washes are drawn from the grey ramp rather than from a state colour.
 * They are the largest areas of colour anywhere in the panel, and the login
 * page is the first thing anyone sees, so a tint here would set the tone for
 * everything behind it - which is what the old indigo-and-blue pair did. On the
 * neutral ramp the same two shapes read as light instead: a dark vignette
 * bleeding in from the edges of a pale page, and a soft glow lifting off a
 * black one, from one set of values.
 */
function Backdrop(): JSX.Element {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute inset-0 [mask-image:radial-gradient(ellipse_65%_55%_at_50%_38%,black,transparent_100%)]">
        <div
          className="absolute inset-0 bg-foreground/[0.07]"
          style={{
            maskImage: MARK_TILE_MASK,
            WebkitMaskImage: MARK_TILE_MASK,
            maskSize: `${MARK_TILE_SIZE}px ${MARK_TILE_SIZE}px`,
            WebkitMaskSize: `${MARK_TILE_SIZE}px ${MARK_TILE_SIZE}px`,
          }}
        />
      </div>
      <div className="absolute inset-x-0 -top-40 mx-auto h-80 w-80 rounded-full bg-primary/20 blur-3xl sm:h-[26rem] sm:w-[26rem]" />
      <div className="absolute inset-x-0 -bottom-48 mx-auto h-80 w-80 rounded-full bg-foreground/10 blur-3xl sm:h-[26rem] sm:w-[26rem]" />
    </div>
  );
}

interface NoticeProps {
  tone: "destructive" | "info";
  icon: Icon;
  /** Already translated: the caller picks between a catalog string and the panel's own sentence. */
  title: string;
  children?: React.ReactNode;
}

/** The one message slot above the fields. role=alert so a rejection is announced. */
function Notice({ tone, icon: Icon, title, children }: NoticeProps): JSX.Element {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col gap-1.5 rounded-md border p-3 text-start",
        tone === "destructive"
          ? "border-destructive/40 bg-destructive/10"
          : "border-info/40 bg-info/10",
      )}
    >
      <p
        className={cn(
          "flex items-start gap-2 text-sm font-medium leading-snug",
          tone === "destructive" ? "text-destructive" : "text-info",
        )}
      >
        {/* No tinted chip behind this one, unlike the notices inside the app:
            the glyph itself has to carry the tone, so it is filled. */}
        <Icon weight="fill" className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>{title}</span>
      </p>
      {children}
    </div>
  );
}

/** Same control as the app shell's, because the theme is chosen before signing in. */
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
      <DropdownMenuContent align="center">
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
  const active = currentLanguage();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={String(t("language.change"))}>
          <Translate aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="center">
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
