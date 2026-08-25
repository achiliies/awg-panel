import * as React from "react";
import { useTranslation } from "react-i18next";

import { ErrorState } from "@/components/ErrorState";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import { useLoginSessions, useRevokeOtherSessions, useRevokeSession } from "@/api/hooks";
import { DeviceMobile, Monitor, SignOut, type Icon } from "@/lib/icons";
import { cn, formatDateTime, relativeParts } from "@/lib/utils";
import type { LoginSession } from "@/api/types";

/*
 * Every browser that can currently reach this panel, and the button that ends
 * one of them.
 *
 * The list is the security screen an admin comes to when something feels wrong:
 * a device they do not recognise, an address they were never at, a laptop that
 * is no longer theirs. So each row says the four things that make that judgement
 * possible - what the browser claims to be, where it was signed in from, when it
 * started and when it was last used - and nothing it cannot actually know. The
 * device name is read out of a header the browser writes itself, so it is
 * offered as a guess, with the raw string a hover away.
 *
 * The session doing the reading is first and is marked. It has no Revoke button
 * on purpose: ending it from here would leave this browser holding a cookie for
 * a session that no longer exists, and every page would start failing with
 * nothing to explain it. Sign out does that properly, and the row says so.
 */

interface SessionsCardProps {
  /** t() with an English original, so a key the catalog lacks never shows raw. */
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

/** Platforms that are held in a hand. Everything else is drawn as a screen. */
const HANDHELD = ["Android", "iPhone", "iPad"];

function deviceIcon(session: LoginSession): Icon {
  return HANDHELD.includes(session.platform) ? DeviceMobile : Monitor;
}

export function SessionsCard({ text }: SessionsCardProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const sessions = useLoginSessions();
  const revoke = useRevokeSession();
  const revokeOthers = useRevokeOtherSessions();

  /** The row whose confirmation is open, or "others" for the bulk one. */
  const [confirming, setConfirming] = React.useState<LoginSession | "others" | null>(null);

  const rows = sessions.data ?? [];
  const others = rows.filter((row) => !row.current);
  const busy = revoke.isPending || revokeOthers.isPending;

  const endOne = (session: LoginSession): void => {
    revoke.mutate(session.id, {
      onSuccess: () => {
        setConfirming(null);
        toast({
          title: text("settings.sessionEnded", "Device signed out"),
          description: text(
            "settings.sessionEndedBody",
            "It is back at the login page. Signing in again needs the password.",
          ),
          variant: "success",
        });
      },
      onError: (error) => {
        setConfirming(null);
        toast({
          title: text("settings.sessionEndFailed", "Could not sign that device out"),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  const endOthers = (): void => {
    revokeOthers.mutate(undefined, {
      onSuccess: (ended) => {
        setConfirming(null);
        toast({
          title: text("settings.sessionsEnded", "Signed out everywhere else"),
          description: text("settings.sessionsEndedBody", "{{count}} other sessions were ended.", {
            count: ended,
          }),
          variant: "success",
        });
      },
      onError: (error) => {
        setConfirming(null);
        toast({
          title: text("settings.sessionsEndFailed", "Could not sign out everywhere else"),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{text("settings.sessions", "Signed-in devices")}</CardTitle>
            <CardDescription className="mt-1.5">
              {text(
                "settings.sessionsHint",
                "Every browser holding a valid session for this account. Signing one out ends it immediately, wherever it is.",
              )}
            </CardDescription>
          </div>
          {others.length > 0 ? (
            <Button
              variant="outline"
              size="sm"
              loading={revokeOthers.isPending}
              disabled={busy}
              onClick={() => setConfirming("others")}
            >
              {revokeOthers.isPending ? (
                <Spinner aria-hidden="true" />
              ) : (
                <SignOut aria-hidden="true" />
              )}
              {text("settings.sessionsEndOthers", "Sign out everywhere else")}
            </Button>
          ) : null}
        </div>
      </CardHeader>

      <CardContent>
        {sessions.isPending ? (
          <LoadingRows />
        ) : sessions.isError ? (
          <ErrorState
            variant="inline"
            error={sessions.error}
            title={text("errors.loadFailed", "Could not load {{what}}", {
              what: text("settings.sessions", "Signed-in devices").toLowerCase(),
            })}
            onRetry={() => void sessions.refetch()}
          />
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {rows.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                busy={busy}
                text={text}
                onRevoke={() => setConfirming(session)}
              />
            ))}
          </ul>
        )}
      </CardContent>

      <AlertDialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open && !busy) {
            setConfirming(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirming === "others"
                ? text("settings.sessionsEndOthersConfirm", "Sign out every other device?")
                : text("settings.sessionEndConfirm", "Sign out {{device}}?", {
                    device: confirming ? deviceName(confirming, text) : "",
                  })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirming === "others"
                ? text(
                    "settings.sessionsEndOthersConfirmBody",
                    "Every browser except this one is signed out at once. Nothing on the server changes and no client is disconnected; anyone who needs to come back signs in again with the password.",
                  )
                : text(
                    "settings.sessionEndConfirmBody",
                    "That browser is signed out immediately and lands on the login page. Nothing on the server changes and no VPN client is disconnected. Whoever holds the password can sign in again, so change it as well if the device is out of your hands.",
                  )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className={cn(
                buttonVariants({ variant: "destructive" }),
                // Stays disabled, but at full strength: this button holds the
                // page open while the request runs, and dimming it hides the
                // spinner that is the only sign anything is happening.
                busy && "cursor-progress disabled:opacity-100",
              )}
              disabled={busy}
              aria-busy={busy || undefined}
              onClick={(event) => {
                // The dialog closes itself on click; the toast has to be able to
                // report a failure, so it is closed when the request answers.
                event.preventDefault();
                if (confirming === "others") {
                  endOthers();
                } else if (confirming) {
                  endOne(confirming);
                }
              }}
            >
              {busy ? <Spinner aria-hidden="true" /> : null}
              {text("settings.sessionEnd", "Sign out")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

/** "Chrome on Windows", or the half of it the browser was willing to say. */
function deviceName(session: LoginSession, text: SessionsCardProps["text"]): string {
  if (session.browser && session.platform) {
    return text("settings.sessionDeviceOn", "{{browser}} on {{platform}}", {
      browser: session.browser,
      platform: session.platform,
    });
  }
  return (
    session.browser ||
    session.platform ||
    text("settings.sessionDeviceUnknown", "Unrecognised browser")
  );
}

interface SessionRowProps {
  session: LoginSession;
  busy: boolean;
  text: SessionsCardProps["text"];
  onRevoke: () => void;
}

function SessionRow({ session, busy, text, onRevoke }: SessionRowProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const Glyph = deviceIcon(session);

  return (
    <li className={cn("p-4", session.current && "bg-muted/40")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          <span
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
              session.current ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
            )}
          >
            <Glyph weight="duotone" className="h-5 w-5" aria-hidden="true" />
          </span>

          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              {/* The user agent is what the two words above are read from, so it
                  is kept within reach rather than dropped. */}
              <p className="text-sm font-medium leading-tight" title={session.userAgent}>
                {deviceName(session, text)}
              </p>
              {session.current ? (
                <Badge variant="success" size="sm">
                  {text("settings.sessionThisDevice", "This device")}
                </Badge>
              ) : null}
            </div>

            <dl className="grid gap-x-6 gap-y-1 text-xs text-muted-foreground sm:grid-cols-[auto_1fr]">
              <Detail label={text("settings.sessionAddress", "Address")}>
                <span className="font-mono">
                  {session.ip || text("settings.sessionAddressUnknown", "not recorded")}
                </span>
              </Detail>
              <Detail label={text("settings.sessionLastActive", "Last active")}>
                <Ago iso={session.lastSeenAt} />
              </Detail>
              <Detail label={text("settings.sessionStarted", "Signed in")}>
                {formatDateTime(session.createdAt, i18n.language) || String(t("common.notSet"))}
              </Detail>
              <Detail label={text("settings.sessionExpires", "Expires")}>
                <Until iso={session.expiresAt} />
              </Detail>
            </dl>

            {session.current ? (
              <p className="text-xs leading-relaxed text-muted-foreground">
                {text(
                  "settings.sessionThisDeviceHint",
                  "This is the session you are reading this with. Use {{signOut}} in the account menu to end it.",
                  { signOut: String(t("nav.logout")) },
                )}
              </p>
            ) : null}
          </div>
        </div>

        {session.current ? null : (
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={onRevoke}
          >
            <SignOut aria-hidden="true" />
            {text("settings.sessionEnd", "Sign out")}
          </Button>
        )}
      </div>
    </li>
  );
}

interface DetailProps {
  label: string;
  children: React.ReactNode;
}

function Detail({ label, children }: DetailProps): JSX.Element {
  return (
    <>
      <dt className="sm:text-end">{label}</dt>
      <dd className="text-foreground/80 tabular-nums">{children}</dd>
    </>
  );
}

/** "2m ago", with the exact moment on hover. */
function Ago({ iso }: { iso: string }): JSX.Element {
  const { t, i18n } = useTranslation();
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return <span>{t("common.notAvailable")}</span>;
  }

  const { unit, value } = relativeParts(Math.floor(at / 1000));
  const label =
    unit === "now" || unit === "never"
      ? String(t("time.justNow"))
      : unit === "second"
        ? String(t("time.secondsAgo", { count: value }))
        : unit === "minute"
          ? String(t("time.minutesAgo", { count: value }))
          : unit === "hour"
            ? String(t("time.hoursAgo", { count: value }))
            : String(t("time.daysAgo", { count: value }));

  return <span title={formatDateTime(iso, i18n.language)}>{label}</span>;
}

/**
 * "in 23h". The expiry is a rolling window - using the panel pushes it out
 * again - so this is how long the session would last if it were left alone.
 */
function Until({ iso }: { iso: string }): JSX.Element {
  const { t, i18n } = useTranslation();
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return <span>{t("common.notAvailable")}</span>;
  }

  const seconds = Math.max(0, Math.floor((at - Date.now()) / 1000));
  const label =
    seconds < 60
      ? String(t("time.inSeconds", { count: seconds }))
      : seconds < 3600
        ? String(t("time.inMinutes", { count: Math.floor(seconds / 60) }))
        : seconds < 86400
          ? String(t("time.inHours", { count: Math.floor(seconds / 3600) }))
          : String(t("time.inDays", { count: Math.floor(seconds / 86400) }));

  return <span title={formatDateTime(iso, i18n.language)}>{label}</span>;
}

/** The card loads as a list, so the page does not jump when the rows arrive. */
function LoadingRows(): JSX.Element {
  return (
    <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
      {[0, 1].map((row) => (
        <li key={row} className="flex gap-3 p-4">
          <Skeleton className="h-9 w-9 shrink-0 rounded-md" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-full max-w-sm" />
            <Skeleton className="h-3 w-32" />
          </div>
        </li>
      ))}
    </ul>
  );
}
