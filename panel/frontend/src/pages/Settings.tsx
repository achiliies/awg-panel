import * as React from "react";
import { Link } from "react-router-dom";
import { useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import {
  Archive,
  ArrowClockwise,
  ArrowSquareOut,
  CheckCircle,
  DownloadSimple,
  FloppyDisk,
  FolderOpen,
  GearSix,
  Globe,
  Info,
  Key,
  Lock,
  Power,
  ShieldCheck,
  TerminalWindow,
  UploadSimple,
  Warning,
  X,
  type Icon,
} from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { CopyButton } from "@/components/CopyButton";
import { ErrorState } from "@/components/ErrorState";
import { PageHeader } from "@/components/PageHeader";
import { StickyActionBar } from "@/components/StickyActionBar";
import { ApiTokensCard } from "@/components/settings/ApiTokensCard";
import { SessionsCard } from "@/components/settings/SessionsCard";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";
import { bootstrap } from "@/api/client";
import {
  queryKeys,
  useBackup,
  useCertificate,
  useChangeCredentials,
  useDisable2fa,
  useEnable2fa,
  useRestore,
  useSaveSettings,
  useServer,
  useSession,
  useSettings,
  useUpdateApply,
  useUpdateCheck,
  useUpdateStatus,
} from "@/api/hooks";
import { FALLBACK_LANGUAGE, LANGUAGES, isSupportedLanguage, setLanguage } from "@/i18n";
import { SETTING_LABELS } from "@/lib/settingLabels";
import { cn, formatBytes } from "@/lib/utils";
import { usePendingIndicator, useSettled } from "@/lib/pending";
import { useReveal } from "@/lib/reveal";
import { useModalPresence } from "@/lib/toast-space";
import {
  SETTINGS_DEFAULTS,
  type ConfigEndpointMode,
  type Settings as PanelSettings,
  type SettingsInput,
  type TlsCertificate,
  type TwoFactorEnableResult,
  type UpdateCheck,
  type UpdateStatus,
} from "@/api/types";
import type { ApiError } from "@/api/client";

/*
 * Panel settings.
 *
 * Everything on this page is about the panel itself, never about the tunnel:
 * nothing here can disconnect a client. The one exception is Restore, which
 * replaces the server config wholesale, and it is behind a typed confirmation
 * for exactly that reason.
 *
 * Four of these settings (listen address, port, secret path, TLS paths) are
 * written to /etc/awg-panel.env and the web service is restarted a couple of
 * seconds later, so the response to the save is the last one the current URL
 * will deliver. That is why saving them is confirmed first and followed by the
 * reconnect overlay rather than a toast: the page has to say where it went.
 */

/** Settings that land in /etc/awg-panel.env, so saving them restarts awg-panel-web. */
const RESTART_KEYS: ReadonlySet<keyof PanelSettings> = new Set<keyof PanelSettings>([
  "webListen",
  "webPort",
  "webBasePath",
  "tlsCertPath",
  "tlsKeyPath",
]);

interface Choice {
  value: string;
  labelKey: string;
  fallback: string;
  /** Already-translated text, used for a stored value the list does not cover. */
  label?: string;
}

const SESSION_LENGTHS: readonly Choice[] = [
  { value: "3600", labelKey: "settings.session1h", fallback: "1 hour" },
  { value: "28800", labelKey: "settings.session8h", fallback: "8 hours" },
  { value: "86400", labelKey: "settings.session1d", fallback: "1 day" },
  { value: "604800", labelKey: "settings.session7d", fallback: "7 days" },
  { value: "2592000", labelKey: "settings.session30d", fallback: "30 days" },
];

const POLL_INTERVALS: readonly Choice[] = [
  { value: "1", labelKey: "settings.poll1s", fallback: "Every second" },
  { value: "2", labelKey: "settings.poll2s", fallback: "Every 2 seconds" },
  { value: "5", labelKey: "settings.poll5s", fallback: "Every 5 seconds" },
  { value: "10", labelKey: "settings.poll10s", fallback: "Every 10 seconds" },
];

/** The word that has to be typed before a restore runs. Translated on purpose. */
const RESTORE_WORD_KEY = "settings.restoreWord";
const RESTORE_WORD_FALLBACK = "RESTORE";

/**
 * Starting points offered when HTTPS is switched on and there is nothing to put
 * back, so the operator edits a path instead of inventing one. Both are still
 * validated by the service on restart.
 */
const TLS_CERT_DEFAULT = "/etc/ssl/certs/awg-panel.crt";
const TLS_KEY_DEFAULT = "/etc/ssl/private/awg-panel.key";

/** How long the overlay waits for the panel to answer again before giving up. */
const RECONNECT_DEADLINE_MS = 45_000;
const RECONNECT_POLL_MS = 1500;
/** The web unit is restarted a couple of seconds after the response is sent. */
const RECONNECT_FIRST_DELAY_MS = 2500;
/** Seconds before a move to another origin hands the browser the new address. */
const HANDOFF_SECONDS = 8;

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

/** "/ab12cd34/" from anything the user typed, including "" and "ab12cd34". */
function normalizeBasePath(raw: string): string {
  const value = raw.trim();
  if (value === "" || value === "/") {
    return "/";
  }
  const withLead = value.startsWith("/") ? value : `/${value}`;
  return withLead.endsWith("/") ? withLead : `${withLead}/`;
}

const PATH_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";
const PATH_LENGTH = 20;
/** 252 = 7 x 36: the largest multiple of the alphabet a byte can hold. */
const PATH_BYTE_LIMIT = PATH_ALPHABET.length * Math.floor(256 / PATH_ALPHABET.length);

/**
 * Random bytes, from the CSPRNG where there is one.
 * crypto.getRandomValues is available wherever the panel can run at all; the
 * Math.random fallback exists so a hardened browser cannot leave the button
 * doing nothing.
 */
function randomBytes(count: number): Uint8Array {
  const bytes = new Uint8Array(count);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < count; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return bytes;
}

/**
 * A fresh secret path in the shape install-panel.sh writes: a fixed /awg/ so
 * the URL reads as this panel's, and behind it one segment of 20 characters
 * from [a-z0-9] carrying all of the entropy - the prefix is announced to
 * anyone scanning for it, so none of the guessing work may rest on it.
 *
 * Bytes from 252 up are drawn again rather than folded in, because 256 is not
 * a multiple of 36 and taking the remainder alone would make a, b, c and d
 * turn up more often than the other 32 characters.
 */
function randomBasePath(): string {
  let path = "";
  while (path.length < PATH_LENGTH) {
    for (const byte of randomBytes(PATH_LENGTH)) {
      if (byte < PATH_BYTE_LIMIT && path.length < PATH_LENGTH) {
        path += PATH_ALPHABET[byte % PATH_ALPHABET.length];
      }
    }
  }
  return `/awg/${path}/`;
}

/** Is the panel itself terminating TLS, rather than a proxy in front of it? */
function tlsOn(values: PanelSettings): boolean {
  return values.tlsCertPath.trim() !== "" && values.tlsKeyPath.trim() !== "";
}

/** An IP literal rather than a name. Only names are pinned by HSTS or a certificate. */
function isAddress(host: string): boolean {
  const bare = host.replace(/^\[|\]$/g, "");
  return bare.includes(":") || /^\d{1,3}(\.\d{1,3}){3}$/.test(bare);
}

/** An IPv6 literal needs brackets in a URL, and window.location.hostname has them already. */
function bracket(host: string): string {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}

/** Would a browser asking for `host` accept a certificate carrying `names`? */
function covers(names: readonly string[], host: string): boolean {
  // Brackets off first. An IPv6 host read out of a URL keeps the brackets that
  // fence it off from the port, and the certificate stores the address without
  // them - so a panel served over a genuine address certificate compared its
  // own hostname against the one name that covers it and decided it did not.
  const asked = host
    .trim()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "")
    .toLowerCase();
  return (
    asked !== "" &&
    names.some((raw) => {
      const name = raw.trim().replace(/\.$/, "").toLowerCase();
      if (name === asked) {
        return true;
      }
      // One label, and only the leftmost: *.example.com matches a.example.com
      // and neither example.com nor a.b.example.com, which is what browsers do.
      const labels = (value: string): number => value.split(".").length;
      return (
        name.startsWith("*.") && labels(name) === labels(asked) && asked.endsWith(name.slice(1))
      );
    })
  );
}

/** What the page knows about the panel beyond its own settings, for predictUrl. */
interface UrlFacts {
  /** Names on the certificate the form points at now, or [] when that cannot be known. */
  certificateNames: readonly string[];
  /** The server's own address, as client configs already dial it. */
  serverAddress: string;
  /** Was the panel terminating TLS itself before this edit? */
  tlsWasOn: boolean;
}

/**
 * Where the panel will answer after a restart, used when the API did not say.
 *
 * A listen address of 0.0.0.0 or :: means "every interface", so the host the
 * browser already used keeps working; any other address is the only one that
 * will answer, so that is the one to point at.
 *
 * Behind a reverse proxy the public URL has nothing to do with these fields,
 * which is precisely why the API's own `url` is preferred over this and why the
 * overlay always shows the address it is sending you to.
 */
function predictUrl(values: PanelSettings, facts: UrlFacts): string {
  const protocol = tlsOn(values) ? "https:" : "http:";
  const port = values.webPort.trim() || SETTINGS_DEFAULTS.webPort;
  const host = bracket(predictHost(values, facts));
  return `${protocol}//${host}:${port}${normalizeBasePath(values.webBasePath)}`;
}

/**
 * The host half of that, which is where turning TLS on or off changes the answer.
 *
 * Turning it on moves the browser to the name on the certificate, because the
 * address the panel is administered at is usually an IP the certificate cannot
 * match. Turning it off moves it away from that name again: every HTTPS
 * response carried an HSTS header, so this browser has been told to use HTTPS
 * for that name for a year and will quietly upgrade http://<name>/ back to a
 * port that no longer speaks it. The pin is on the name, never on an address.
 *
 * These are the rules the API applies, mirrored so the confirmation says the
 * same address the handover then uses. Where the page cannot know - a
 * certificate path typed but not yet saved, an endpoint left to auto-detect -
 * it keeps the host the browser is already on rather than inventing one.
 */
function predictHost(values: PanelSettings, facts: UrlFacts): string {
  const listen = values.webListen.trim();
  const wildcard = listen === "" || listen === "0.0.0.0" || listen === "::" || listen === "*";
  if (!wildcard) {
    return listen;
  }
  const host = window.location.hostname;
  if (tlsOn(values)) {
    if (facts.certificateNames.length === 0 || covers(facts.certificateNames, host)) {
      return host;
    }
    return facts.certificateNames.find((name) => !name.startsWith("*")) ?? host;
  }
  if (facts.tlsWasOn && !isAddress(host) && isAddress(facts.serverAddress)) {
    return facts.serverAddress;
  }
  return host;
}

function isPositiveInt(value: string, min: number, max: number): boolean {
  if (!/^\d+$/.test(value.trim())) {
    return false;
  }
  const parsed = Number.parseInt(value.trim(), 10);
  return parsed >= min && parsed <= max;
}

type FieldErrorMap = Partial<Record<keyof PanelSettings, string>>;

/**
 * The checks worth doing before a round trip: a port that cannot be bound and a
 * base path that would lock the operator out of their own panel. Everything
 * else is the API's call.
 */
function validate(
  values: PanelSettings,
  changed: readonly (keyof PanelSettings)[],
  text: (key: string, fallback: string) => string,
): FieldErrorMap {
  const errors: FieldErrorMap = {};
  const check = (key: keyof PanelSettings, ok: boolean, message: string): void => {
    if (changed.includes(key) && !ok) {
      errors[key] = message;
    }
  };

  check(
    "webPort",
    isPositiveInt(values.webPort, 1, 65535),
    text("validation.port", "Enter a port between 1 and 65535."),
  );
  check(
    "webListen",
    /^[A-Za-z0-9.:*_-]+$/.test(values.webListen.trim()),
    text("validation.invalid", "That value is not valid."),
  );
  check(
    "webBasePath",
    /^\/([A-Za-z0-9._~-]+\/)*$/.test(normalizeBasePath(values.webBasePath)),
    text(
      "settings.basePathInvalid",
      "Use letters, digits, dots, dashes and slashes only, for example /awg/ab12cd34/",
    ),
  );
  check(
    "sessionMaxAge",
    isPositiveInt(values.sessionMaxAge, 60, 31_536_000),
    text("validation.number", "Enter a whole number."),
  );
  check(
    "loginRateLimit",
    isPositiveInt(values.loginRateLimit, 1, 100),
    text("validation.number", "Enter a whole number."),
  );
  check(
    "trafficPollSec",
    isPositiveInt(values.trafficPollSec, 1, 3600),
    text("validation.number", "Enter a whole number."),
  );
  check(
    "onlineThresholdSec",
    isPositiveInt(values.onlineThresholdSec, 10, 86_400),
    text("validation.number", "Enter a whole number."),
  );
  check(
    "enforceIntervalSec",
    isPositiveInt(values.enforceIntervalSec, 5, 3600),
    text("validation.number", "Enter a whole number."),
  );
  // Half a TLS pair starts the service, fails to bind and looks exactly like
  // the panel having disappeared, so it is caught here rather than at boot.
  const cert = values.tlsCertPath.trim();
  const key = values.tlsKeyPath.trim();
  const tlsTouched = changed.includes("tlsCertPath") || changed.includes("tlsKeyPath");
  if (tlsTouched && cert !== "" && key === "") {
    errors.tlsKeyPath = text(
      "settings.tlsMissingKey",
      "HTTPS needs both files. Give the private key as well, or clear the certificate.",
    );
  }
  if (tlsTouched && key !== "" && cert === "") {
    errors.tlsCertPath = text(
      "settings.tlsMissingCert",
      "HTTPS needs both files. Give the certificate as well, or clear the private key.",
    );
  }

  return errors;
}

/* -------------------------------------------------------------------------- */
/* Small building blocks                                                       */
/* -------------------------------------------------------------------------- */

interface NoticeProps {
  icon: Icon;
  title: string;
  tone?: "warning" | "info" | "danger";
  action?: React.ReactNode;
  children?: React.ReactNode;
}

/** A calm block for something that needs knowing, not an alarm. */
function Notice({
  icon: Icon,
  title,
  tone = "warning",
  action,
  children,
}: NoticeProps): JSX.Element {
  const tones = {
    warning: { box: "border-warning/40 bg-warning/5", badge: "bg-warning/15 text-warning" },
    info: { box: "border-info/40 bg-info/5", badge: "bg-info/15 text-info" },
    danger: {
      box: "border-destructive/40 bg-destructive/5",
      badge: "bg-destructive/15 text-destructive",
    },
  } as const;

  return (
    <div role="status" className={cn("rounded-lg border p-4", tones[tone].box)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <span
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
              tones[tone].badge,
            )}
          >
            <Icon weight="duotone" className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0 space-y-1.5">
            <p className="text-sm font-medium leading-tight">{title}</p>
            {children ? (
              <div className="space-y-1.5 text-sm leading-relaxed text-muted-foreground">
                {children}
              </div>
            ) : null}
          </div>
        </div>
        {action ? <div className="flex shrink-0 flex-wrap gap-2">{action}</div> : null}
      </div>
    </div>
  );
}

interface InlineNoteProps {
  icon: Icon;
  tone?: "warning" | "success" | "danger";
  children: React.ReactNode;
}

/**
 * One sentence of feedback, on the line of the button that produced it.
 *
 * A Notice is for an answer with a body - release notes, a command to run, a
 * list of what a save left behind. An answer that is one sentence long does
 * not need a border and an icon tile: boxed, "You are on the latest version"
 * carries the weight of a warning, and every check shoves the rest of the card
 * down the page to announce a non-event. Here the icon carries the tone and
 * the sentence stays quiet, so it reads as the button's reply rather than as
 * something else that has gone wrong.
 */
function InlineNote({ icon: Icon, tone = "warning", children }: InlineNoteProps): JSX.Element {
  const tints = {
    warning: "text-warning",
    success: "text-success",
    danger: "text-destructive",
  } as const;

  return (
    <p
      role="status"
      className={cn(
        "flex min-w-0 items-center gap-1.5 text-sm leading-snug",
        tone === "danger" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      <Icon weight="duotone" className={cn("h-4 w-4 shrink-0", tints[tone])} aria-hidden="true" />
      {children}
    </p>
  );
}

interface CommandProps {
  command: string;
  /** What running it does. Never leave a command unexplained. */
  description?: string;
}

/** A shell command with a copy button, because retyping these is where typos live. */
function Command({ command, description }: CommandProps): JSX.Element {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
        <TerminalWindow className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-xs leading-relaxed">
          {command}
        </code>
        <CopyButton value={command} />
      </div>
      {description ? (
        <p className="text-xs leading-relaxed text-muted-foreground">{description}</p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function SettingsPage(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();

  /** t() with an English original, so a key the catalog lacks never shows raw. */
  const text = React.useCallback(
    (key: string, fallback: string, vars?: Record<string, string | number>): string =>
      String(t(key, { defaultValue: fallback, ...vars })),
    [t],
  );

  const settings = useSettings();
  const session = useSession();
  const save = useSaveSettings();
  const server = useServer();
  /*
   * Not save.isPending directly. The API is on this machine, so a settings
   * save often answers in a frame or two - and onSuccess clears the draft,
   * which unmounts the whole bar the spinner lives in. The save worked, but
   * pressing the button looked like it did nothing at all.
   */
  const saving = usePendingIndicator(save.isPending);

  const [draft, setDraft] = React.useState<Partial<PanelSettings>>({});
  const [errors, setErrors] = React.useState<FieldErrorMap>({});
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [warnings, setWarnings] = React.useState<string[]>([]);
  /** Non-null once a save has restarted the web service: the overlay owns the page. */
  const [reconnectUrl, setReconnectUrl] = React.useState<string | null>(null);
  /*
   * The notice sits above the tabs and the save bar is on the bottom edge, so
   * on the TLS or Advanced cards it arrives well off the top of the screen.
   * Every save replaces the array, so this scrolls for each one rather than
   * only for the first.
   */
  const noticeRef = useReveal<HTMLDivElement>(warnings.length > 0 ? warnings : null);

  const stored = settings.data ?? SETTINGS_DEFAULTS;
  const values = React.useMemo<PanelSettings>(() => ({ ...stored, ...draft }), [stored, draft]);

  const changed = React.useMemo(
    () =>
      (Object.keys(SETTINGS_DEFAULTS) as (keyof PanelSettings)[]).filter(
        (key) => draft[key] !== undefined && draft[key] !== stored[key],
      ),
    [draft, stored],
  );
  const dirty = changed.length > 0;
  const restarts = changed.some((key) => RESTART_KEYS.has(key));

  /*
   * The TLS pair the form points at, not the one that is stored.
   *
   * Turning HTTPS on moves the browser to the name inside the certificate, so
   * the confirmation cannot say where the page will reconnect without reading
   * the file the operator has just named - and it is by definition not saved
   * yet. It is also the only way to find out whether the two files are there
   * before the save, which matters more: the switch fills the boxes in with a
   * pair of paths that are only a suggestion, and a save of a path that is not
   * there is refused. Settled first, because this hangs off two text fields and
   * a path is thirty keystrokes; one query at page level rather than one per
   * card, so the address card below and the dialog cannot disagree about what
   * is on disk.
   */
  const certPath = useSettled(values.tlsCertPath.trim());
  const keyPath = useSettled(values.tlsKeyPath.trim());
  const certificate = useCertificate(certPath, keyPath);

  /*
   * Whether the answer being held is about the two paths on screen. In the gap
   * between a keystroke and the reply it describes the previous ones, and a
   * verdict on the certificate being replaced would be worse than no verdict:
   * it names the wrong file, or clears an error that is still true.
   */
  const describesForm =
    certificate.data?.path === values.tlsCertPath.trim() &&
    certificate.data?.keyPath === values.tlsKeyPath.trim();

  const facts = React.useMemo<UrlFacts>(
    () => ({
      certificateNames: describesForm ? (certificate.data?.names ?? []) : [],
      serverAddress: server.data?.endpointHost.trim() ?? "",
      tlsWasOn: tlsOn(stored),
    }),
    [certificate.data, describesForm, server.data, stored],
  );

  /*
   * The address a config downloaded right now would carry - the typed domain,
   * or the certificate's when the box is left empty, and otherwise whatever the
   * server config holds. It is the whole of what changing this setting does, so
   * it is what the save says, in place of a banner explaining the feature.
   */
  const clientAddress =
    (values.configEndpointMode === "domain"
      ? values.configEndpointHost.trim() || (describesForm ? certificate.data?.domain.trim() : "")
      : "") ||
    server.data?.endpointHost.trim() ||
    "";

  /*
   * Only while one of the two paths is being changed. A certificate that went
   * missing under a running panel is not this form's business - gunicorn has
   * the file open and is serving with it - and an error on a field nobody
   * touched would block every unrelated save on the page.
   */
  const tlsTouched = changed.includes("tlsCertPath") || changed.includes("tlsKeyPath");

  /*
   * A file is named and the disk has not been asked about it yet: the paths are
   * still settling, or the answer is in flight. Saving through this window is
   * how the confirmation dialog gets in front of a save that is refused a second
   * later, which is the whole thing this is here to prevent - so the button
   * waits, and the bar says what for. Turning HTTPS off names no file and waits
   * for nothing.
   *
   * Neither condition can stick. A query that fails stops fetching, nothing is
   * left settling, and the save goes through to the API, which is the authority
   * on the two files anyway.
   */
  const checkingTls =
    tlsTouched &&
    (values.tlsCertPath.trim() !== "" || values.tlsKeyPath.trim() !== "") &&
    (certPath !== values.tlsCertPath.trim() ||
      keyPath !== values.tlsKeyPath.trim() ||
      certificate.isFetching);

  /** What the panel makes of those two files, on the fields they belong to. */
  const tlsProblems = React.useMemo<FieldErrorMap>(() => {
    if (!tlsTouched || !describesForm || !certificate.data) {
      return {};
    }
    const found: FieldErrorMap = {};
    if (certificate.data.problem) {
      found.tlsCertPath = certificate.data.problem;
    }
    if (certificate.data.keyProblem) {
      found.tlsKeyPath = certificate.data.keyProblem;
    }
    return found;
  }, [certificate.data, describesForm, tlsTouched]);

  const handleChange = React.useCallback(
    <K extends keyof PanelSettings>(key: K, value: PanelSettings[K]): void => {
      setDraft((current) => ({ ...current, [key]: value }));
      setErrors((current) => {
        if (!(key in current)) {
          return current;
        }
        const next = { ...current };
        delete next[key];
        return next;
      });
    },
    [],
  );

  const handleDiscard = React.useCallback(() => {
    setDraft({});
    setErrors({});
  }, []);

  const runSave = React.useCallback(() => {
    // Only what changed goes on the wire; every Settings value is a string, so
    // the shape matches SettingsInput by construction.
    const payload = Object.fromEntries(changed.map((key) => [key, values[key]])) as SettingsInput;
    const touchedEndpoint = changed.some(
      (key) => key === "configEndpointMode" || key === "configEndpointHost",
    );

    save.mutate(payload, {
      onSuccess: (result) => {
        setDraft({});
        setErrors({});
        setWarnings(result.warnings);
        /*
         * The API's flag, and not this page's guess at it. They agree whenever
         * the restart is really happening; where they part company is a save
         * that was meant to restart the service and could not - the env file
         * would not be written, or awg-panel is not installed to do it - and
         * there the overlay is a promise of a reconnection that nobody
         * scheduled. Worse, it keeps the promise: the address it polls is the
         * one this page is already on, so it answers at once and reloads,
         * taking the notes that explained the failure with it.
         *
         * They also part company on a save that normalised to nothing - a base
         * path retyped without its slashes, a port with a space in front of it
         * - where this page counts a change the API does not. That one used to
         * restart the panel to apply nothing at all.
         */
        if (result.needsRestart) {
          setReconnectUrl(result.url || predictUrl(values, facts));
          return;
        }
        /*
         * Every warning that reaches this line is a save that did not take.
         * Everything the API has advice about restarts the web service, so on a
         * save that worked the overlay above has already returned; getting here
         * with something to say means the restart was attempted and refused -
         * the environment file could not be written, or awg-panel is not
         * installed to do the restarting. The last line is the one that says so,
         * because that is the one the API appends after the advice.
         *
         * It stays up until it is dismissed. This is the case where the operator
         * has to go and do something on the server, and a sentence that fades
         * after five seconds is how they end up believing the panel moved.
         */
        if (result.warnings.length > 0) {
          toast({
            title: text("settings.savedNotLive", "Saved, but not in effect"),
            description: result.warnings[result.warnings.length - 1],
            duration: 0,
          });
          return;
        }
        toast({
          title: text("settings.saved", "Settings saved"),
          // Changing which address clients are handed is invisible everywhere
          // else on the page - nothing on the server is rewritten and no config
          // on a device moves - so the confirmation is where it gets named.
          description:
            touchedEndpoint && clientAddress
              ? text(
                  "settings.savedEndpoint",
                  "New configs hand out {{host}}. Devices already set up keep the address they have.",
                  { host: clientAddress },
                )
              : text("settings.savedBody", "The change is in effect."),
          variant: "success",
        });
      },
      onError: (error) => {
        const mapped: FieldErrorMap = {};
        for (const [field, message] of Object.entries(error.errors)) {
          if (field in SETTINGS_DEFAULTS) {
            mapped[field as keyof PanelSettings] = message;
          }
        }
        setErrors(mapped);
        toast({
          title: text("errors.saveFailed", "Could not save the change"),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  }, [changed, clientAddress, facts, save, text, toast, values]);

  const handleSave = React.useCallback(() => {
    /*
     * The files come first, and they stop the save here rather than at the API.
     * Saving a TLS path restarts the web service, so a confirmation stands in
     * front of this one; a path that is not there is refused after it, which
     * asks the operator to agree to a restart in order to be told the form was
     * never valid. The dialog is a promise about where the page reconnects, and
     * it should only be made about a save that is going to happen.
     */
    const problems = { ...tlsProblems, ...validate(values, changed, text) };
    setErrors(problems);
    if (Object.keys(problems).length > 0) {
      toast({
        title: text("errors.validation", "Some fields need fixing"),
        description: text(
          "errors.validationBody",
          "Nothing was saved. The fields with a message below them are the ones to look at.",
        ),
        variant: "destructive",
      });
      return;
    }
    if (restarts) {
      setConfirmOpen(true);
      return;
    }
    runSave();
  }, [changed, restarts, runSave, text, tlsProblems, toast, values]);

  const header = (
    <PageHeader
      title={String(t("settings.title"))}
      description={String(t("settings.subtitle"))}
      icon={GearSix}
      badge={
        session.data?.mock ? <Badge variant="warning">{t("about.mockMode")}</Badge> : undefined
      }
    />
  );

  if (settings.isError) {
    return (
      <>
        {header}
        <ErrorState
          error={settings.error}
          title={text("errors.loadFailed", "Could not load {{what}}", {
            what: String(t("settings.title")).toLowerCase(),
          })}
          onRetry={() => void settings.refetch()}
        />
      </>
    );
  }

  // One line under the bar's heading, and it stays one line: the bar must not
  // resize under the pointer at the moment the button is pressed.
  const barNote = checkingTls
    ? text("settings.tlsChecking", "Checking that the panel can read the certificate and key.")
    : restarts
      ? text(
          "settings.restartWarning",
          "Saving restarts the panel web service. The tunnel is not touched, so clients stay connected.",
        )
      : text("settings.applyNow", "Applies as soon as you save.");

  const formProps: SettingsFormProps = {
    values,
    saved: stored,
    // Under the box as it is typed into, not once the save has come back. What
    // a save said wins while it stands, because it is the newer answer; editing
    // the field drops it, and what is on disk shows through again.
    errors: { ...tlsProblems, ...errors },
    disabled: settings.isPending || saving,
    onChange: handleChange,
    text,
  };

  return (
    <>
      {header}

      {warnings.length > 0 ? (
        // scroll-mt clears the sticky top bar, which "start" would otherwise
        // park the notice underneath.
        <div ref={noticeRef} className="mb-4 scroll-mt-20">
          <Notice
            icon={Warning}
            tone="info"
            title={text("settings.savedWithWarnings", "Saved, with something worth reading")}
            action={
              <Button size="sm" variant="ghost" onClick={() => setWarnings([])}>
                {t("common.dismiss")}
              </Button>
            }
          >
            <ul className="space-y-1.5">
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </Notice>
        </div>
      ) : null}

      {/* Grows into whatever the window has left over, so the save bar below is
          pushed to the bottom of it rather than floating up against the end of
          a short tab. On a tab taller than the window there is nothing to grow
          into and it changes nothing. */}
      <Tabs defaultValue="general" className="flex-1">
        <TabsList>
          <TabsTrigger value="general">
            <Globe aria-hidden="true" />
            {t("settings.general")}
          </TabsTrigger>
          <TabsTrigger value="authentication">
            <ShieldCheck aria-hidden="true" />
            {text("settings.authentication", "Authentication")}
          </TabsTrigger>
          <TabsTrigger value="tls">
            <Lock aria-hidden="true" />
            {t("settings.tls")}
          </TabsTrigger>
          <TabsTrigger value="backup">
            <Archive aria-hidden="true" />
            {t("settings.backup")}
          </TabsTrigger>
          <TabsTrigger value="update">
            <DownloadSimple aria-hidden="true" />
            {t("settings.update")}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          {settings.isPending ? <LoadingForm /> : <GeneralTab {...formProps} />}
        </TabsContent>

        <TabsContent value="authentication">
          <AuthenticationTab
            {...formProps}
            username={session.data?.username ?? ""}
            twoFactorOn={session.data?.otpRequired ?? false}
            sessionPending={session.isPending}
          />
        </TabsContent>

        <TabsContent value="tls">
          {settings.isPending ? (
            <LoadingForm />
          ) : (
            <TlsTab {...formProps} certificate={certificate} />
          )}
        </TabsContent>

        <TabsContent value="backup">
          <BackupTab text={text} />
        </TabsContent>

        <TabsContent value="update">
          <UpdateTab text={text} version={session.data?.version ?? bootstrap.version} />
        </TabsContent>
      </Tabs>

      {/* Stays up while the save is in flight even though the draft - and so
          `dirty` - is already cleared, otherwise the bar and its spinner
          vanish together the instant the request lands. */}
      {dirty || saving ? (
        <StickyActionBar
          className="mt-6"
          actions={
            <>
              <Button variant="ghost" onClick={handleDiscard} disabled={saving}>
                {t("common.discard")}
              </Button>
              <Button onClick={handleSave} loading={saving} disabled={saving || checkingTls}>
                {saving ? <Spinner aria-hidden="true" /> : <FloppyDisk aria-hidden="true" />}
                {t(saving ? "common.saving" : "common.save")}
              </Button>
            </>
          }
        >
          {/* Both states are two lines, so the bar does not resize under the
              pointer at the moment the button is pressed. */}
          <p className="text-sm font-medium">
            {saving ? (
              t("common.saving")
            ) : (
              <>
                {text("settings.unsaved", "Unsaved changes")}
                <span className="ms-2 font-normal text-muted-foreground">
                  {changed.map((key) => String(t(SETTING_LABELS[key]))).join(", ")}
                </span>
              </>
            )}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">{barNote}</p>
        </StickyActionBar>
      ) : null}

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {text("settings.restartConfirm", "Save and restart the panel?")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {text(
                "settings.restartConfirmBody",
                "Changing the port, the listen address, the secret path or the certificate restarts the web service. This page will reconnect at {{url}}. The tunnel keeps running and no client is disconnected.",
                { url: predictUrl(values, facts) },
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={runSave}>
              {text("settings.saveAndRestart", "Save and restart")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {reconnectUrl ? (
        <ReconnectOverlay url={reconnectUrl} text={text} onDismiss={() => setReconnectUrl(null)} />
      ) : null}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Tabs                                                                        */
/* -------------------------------------------------------------------------- */

interface SettingsFormProps {
  values: PanelSettings;
  /** The same settings as they are stored, so a control can put one back. */
  saved: PanelSettings;
  errors: FieldErrorMap;
  disabled: boolean;
  onChange: <K extends keyof PanelSettings>(key: K, value: PanelSettings[K]) => void;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

function GeneralTab({ values, errors, disabled, onChange, text }: SettingsFormProps): JSX.Element {
  const { t } = useTranslation();

  // A value set from the command line or an older release still has to be
  // selectable, otherwise opening the menu silently changes the setting. Its
  // label is built from the seconds and the translated unit, so it needs no
  // catalog entry of its own.
  const custom = (value: string): Choice => ({
    value,
    labelKey: "",
    fallback: "",
    label: `${value} ${String(t("units.seconds"))}`,
  });
  const sessionChoices = SESSION_LENGTHS.some((choice) => choice.value === values.sessionMaxAge)
    ? SESSION_LENGTHS
    : [custom(values.sessionMaxAge), ...SESSION_LENGTHS];
  const pollChoices = POLL_INTERVALS.some((choice) => choice.value === values.trafficPollSec)
    ? POLL_INTERVALS
    : [custom(values.trafficPollSec), ...POLL_INTERVALS];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("settings.general")}</CardTitle>
          <CardDescription>
            {text(
              "settings.generalHint",
              "Where the panel answers and what it looks like. Nothing here touches the tunnel.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 sm:grid-cols-2">
          <Field
            id="webPort"
            label={String(t("settings.port"))}
            hint={String(t("settings.portHint"))}
            error={errors.webPort}
          >
            <Input
              id="webPort"
              inputMode="numeric"
              value={values.webPort}
              disabled={disabled}
              aria-invalid={Boolean(errors.webPort)}
              onChange={(event) => onChange("webPort", event.target.value)}
            />
          </Field>

          <Field
            id="webListen"
            label={String(t("settings.listen"))}
            hint={String(t("settings.listenHint"))}
            error={errors.webListen}
          >
            <Input
              id="webListen"
              value={values.webListen}
              disabled={disabled}
              aria-invalid={Boolean(errors.webListen)}
              onChange={(event) => onChange("webListen", event.target.value)}
            />
          </Field>

          <Field
            id="webBasePath"
            label={String(t("settings.basePath"))}
            hint={String(t("settings.basePathHint"))}
            error={errors.webBasePath}
          >
            {/*
              The button sits on the end of the field rather than up beside
              the label: it writes into the input next to it, and the two read
              as one control when they touch. It keeps its own width and the
              input takes the rest, so a long path scrolls inside the input
              instead of pushing the button off the row.
            */}
            <div className="flex items-center gap-2">
              <Input
                id="webBasePath"
                value={values.webBasePath}
                disabled={disabled}
                spellCheck={false}
                autoComplete="off"
                className="min-w-0 flex-1 font-mono"
                aria-invalid={Boolean(errors.webBasePath)}
                onChange={(event) => onChange("webBasePath", event.target.value)}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="shrink-0"
                disabled={disabled}
                onClick={() => onChange("webBasePath", randomBasePath())}
              >
                <ArrowClockwise aria-hidden="true" />
                {text("settings.basePathRegenerate", "Generate a new one")}
              </Button>
            </div>
          </Field>

          <Field
            id="sessionMaxAge"
            label={String(t("settings.sessionLength"))}
            hint={String(t("settings.sessionLengthHint"))}
            error={errors.sessionMaxAge}
          >
            <Select
              value={values.sessionMaxAge}
              disabled={disabled}
              onValueChange={(value) => onChange("sessionMaxAge", value)}
            >
              <SelectTrigger id="sessionMaxAge">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {sessionChoices.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    {choice.label ?? text(choice.labelKey, choice.fallback)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field
            id="trafficPollSec"
            label={String(t("settings.pollInterval"))}
            hint={String(t("settings.pollIntervalHint"))}
            error={errors.trafficPollSec}
          >
            <Select
              value={values.trafficPollSec}
              disabled={disabled}
              onValueChange={(value) => onChange("trafficPollSec", value)}
            >
              <SelectTrigger id="trafficPollSec">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {pollChoices.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    {choice.label ?? text(choice.labelKey, choice.fallback)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field id="language" label={String(t("settings.language"))} error={errors.language}>
            {/*
              A stored code the panel no longer carries a catalog for - a
              language dropped since this was last saved - matches no row, and
              the box is drawn empty rather than wrong. Fall back to the
              default, so the field always names a language the operator can
              read and pick over.
            */}
            <Select
              value={isSupportedLanguage(values.language) ? values.language : FALLBACK_LANGUAGE}
              disabled={disabled}
              onValueChange={(value) => {
                onChange("language", value);
                setLanguage(value);
              }}
            >
              <SelectTrigger id="language">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {LANGUAGES.map((language) => (
                  <SelectItem key={language.code} value={language.code}>
                    {language.nativeName}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("settings.advanced")}</CardTitle>
          <CardDescription>
            {text(
              "settings.collectorHint",
              "How long traffic history is kept, and how often data limits and expiry dates are enforced. The defaults suit a small server.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 sm:grid-cols-2">
          <NumberField
            id="onlineThresholdSec"
            settingKey="onlineThresholdSec"
            label={String(t("settings.onlineThreshold"))}
            hint={String(t("settings.onlineThresholdHint"))}
            unit={String(t("units.seconds"))}
            values={values}
            errors={errors}
            disabled={disabled}
            onChange={onChange}
          />
          <NumberField
            id="enforceIntervalSec"
            settingKey="enforceIntervalSec"
            label={String(t("settings.enforceInterval"))}
            hint={String(t("settings.enforceIntervalHint"))}
            unit={String(t("units.seconds"))}
            values={values}
            errors={errors}
            disabled={disabled}
            onChange={onChange}
          />
        </CardContent>
      </Card>
    </div>
  );
}

interface NumberFieldProps {
  id: string;
  settingKey: keyof PanelSettings;
  label: string;
  hint: string;
  unit: string;
  values: PanelSettings;
  errors: FieldErrorMap;
  disabled: boolean;
  onChange: <K extends keyof PanelSettings>(key: K, value: PanelSettings[K]) => void;
}

/** A whole number with its unit spelled out beside it, never just a bare box. */
function NumberField({
  id,
  settingKey,
  label,
  hint,
  unit,
  values,
  errors,
  disabled,
  onChange,
}: NumberFieldProps): JSX.Element {
  return (
    <Field id={id} label={label} hint={hint} error={errors[settingKey]}>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          inputMode="numeric"
          className="w-32"
          value={values[settingKey]}
          disabled={disabled}
          aria-invalid={Boolean(errors[settingKey])}
          onChange={(event) => onChange(settingKey, event.target.value)}
        />
        <span className="text-sm text-muted-foreground">{unit}</span>
      </div>
    </Field>
  );
}

interface AuthenticationTabProps extends SettingsFormProps {
  /** The account's name as the panel currently knows it, "" until the session lands. */
  username: string;
  twoFactorOn: boolean;
  sessionPending: boolean;
}

function AuthenticationTab({
  values,
  errors,
  disabled,
  onChange,
  text,
  username,
  twoFactorOn,
  sessionPending,
}: AuthenticationTabProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="space-y-4">
      <CredentialsCard text={text} username={username} pending={sessionPending} />
      <TwoFactorCard text={text} enabled={twoFactorOn} pending={sessionPending} />
      {/* Below the two credentials cards on purpose: this is where an admin
          checks that a password change or a new second factor actually left
          nobody else signed in. */}
      <SessionsCard text={text} />
      {/* And below that, because it is the same question asked of everything
          that is not a browser: what else can reach this panel, and can I stop
          it from here.

          The same card is on the API page, where a token is issued in the
          middle of writing a script rather than in the middle of administering
          a panel. One component, so the two cannot drift; the line under it is
          for the reader who arrived at the tokens first and has not found the
          page that says what to do with one. */}
      <ApiTokensCard text={text} />
      <p className="text-sm leading-relaxed text-muted-foreground">
        {text("settings.tokensSeeApi", "What a token can reach, and how to send it, is on the")}{" "}
        <Link
          to="/api-docs"
          className="font-medium text-primary underline-offset-4 hover:underline"
        >
          {text("settings.tokensApiPage", "API page")}
        </Link>
        .
      </p>

      <Card>
        <CardHeader>
          <CardTitle>{text("settings.signInLimits", "Sign-in attempts")}</CardTitle>
          <CardDescription>{t("settings.loginRateLimitHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          <NumberField
            id="loginRateLimit"
            settingKey="loginRateLimit"
            label={String(t("settings.loginRateLimit"))}
            hint={text(
              "settings.loginRateLimitNote",
              "Locking yourself out is recoverable: the block clears after 15 minutes, and a shell on the server always works.",
            )}
            unit={text("settings.attempts", "attempts")}
            values={values}
            errors={errors}
            disabled={disabled}
            onChange={onChange}
          />
        </CardContent>
      </Card>
    </div>
  );
}

interface TextProp {
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

// The fields this form draws an error under. Anything else the API names has
// nowhere to be shown but the banner at the foot of the form.
const INLINE_FIELDS = ["current", "username"];

interface CredentialsCardProps extends TextProp {
  /** The name the account signs in with now; "" while the session is still loading. */
  username: string;
  pending: boolean;
}

/**
 * The account's own credentials: the name it signs in with and the password
 * behind it, changed together or one at a time.
 *
 * One form and one round trip for both, because they are one credential and
 * because the current password is the proof for either change. Whichever box is
 * left as it was is simply not sent, so renaming the account does not oblige
 * anybody to invent a new password at the same time.
 */
function CredentialsCard({ text, username, pending }: CredentialsCardProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const change = useChangeCredentials();

  const [current, setCurrent] = React.useState("");
  /*
   * null means "not touched", so the box follows the account's real name until
   * somebody types in it - which matters twice: the session arrives after the
   * first render, and a save that renames the account has to leave the field
   * showing the new name rather than a stale draft of it.
   */
  const [draftName, setDraftName] = React.useState<string | null>(null);
  const [next, setNext] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [problem, setProblem] = React.useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = React.useState<Record<string, string>>({});

  const name = draftName ?? username;
  const mismatch = confirm.length > 0 && next !== confirm;

  const renaming = name.trim() !== "" && name.trim() !== username;
  // Either box counts: somebody who filled in only the repeat box has started
  // changing the password and deserves the mismatch message, not a form that
  // quietly submits a rename instead.
  const repassword = next !== "" || confirm !== "";

  const submit = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setFieldErrors({});
    if (!renaming && !repassword) {
      setProblem(
        text("settings.credentialsNothing", "Change the username, the password, or both."),
      );
      return;
    }
    // Nothing checks the password itself. Whatever the owner typed is what they
    // meant, and the server does not second-guess it either. The repeat box is
    // still checked, because that guards against a typo rather than a choice.
    if (repassword && next !== confirm) {
      setProblem(String(t("settings.passwordMismatch")));
      return;
    }
    setProblem(null);

    change.mutate(
      {
        current,
        ...(renaming ? { username: name.trim() } : {}),
        ...(repassword ? { new: next } : {}),
      },
      {
        onSuccess: (session) => {
          setCurrent("");
          setDraftName(null);
          setNext("");
          setConfirm("");
          toast({
            title: renaming
              ? repassword
                ? text("settings.credentialsChanged", "Username and password changed")
                : text("settings.usernameChanged", "Username changed")
              : String(t("settings.passwordChanged")),
            description: renaming
              ? text(
                  "settings.usernameChangedBody",
                  "You sign in as {{username}} from now on. Other sessions were signed out; this one stays open.",
                  { username: session.username },
                )
              : String(t("settings.passwordChangedBody")),
            variant: "success",
          });
        },
        onError: (error) => {
          /*
           * A message belongs in one place. An error named after a box on this
           * form goes under that box, and the banner is left for what has no
           * box to go under: a failure with no field attached at all, or one
           * named after a field this form does not show. Showing `detail` as
           * well would print the same sentence twice, because the API builds it
           * out of the very field errors that are about to be attached - and
           * builds it as "username: ...", which is a field name the person
           * typing has never seen.
           */
          const attached: Record<string, string> = {};
          const loose: string[] = [];
          for (const [field, message] of Object.entries(error.errors)) {
            if (INLINE_FIELDS.includes(field)) {
              attached[field] = message;
            } else {
              loose.push(message);
            }
          }
          setFieldErrors(attached);
          if (loose.length > 0) {
            setProblem(loose.join(" "));
          } else {
            setProblem(Object.keys(attached).length > 0 ? null : error.detail);
          }
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{text("settings.credentials", "Username and password")}</CardTitle>
        <CardDescription>
          {text(
            "settings.credentialsHint",
            "What signs you in to this panel. Fill in your current password, then change the username, the password, or both. Whatever you leave alone stays as it is.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-6 sm:grid-cols-2" onSubmit={submit}>
          <Field
            id="currentPassword"
            label={String(t("settings.currentPassword"))}
            hint={text(
              "settings.currentPasswordHint",
              "Proof that it is you, whichever of the two you are changing.",
            )}
            error={fieldErrors.current}
            className="sm:col-span-2 sm:max-w-md"
          >
            <Input
              id="currentPassword"
              type="password"
              autoComplete="current-password"
              value={current}
              disabled={change.isPending}
              aria-invalid={Boolean(fieldErrors.current)}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </Field>

          <Field
            id="username"
            label={text("settings.username", "Username")}
            hint={text(
              "settings.usernameHint",
              "The name you type at the login page. Leave it as it is to keep it.",
            )}
            error={fieldErrors.username}
            className="sm:col-span-2 sm:max-w-md"
          >
            <Input
              id="username"
              autoComplete="username"
              spellCheck={false}
              value={name}
              disabled={change.isPending || (pending && username === "")}
              aria-invalid={Boolean(fieldErrors.username)}
              onChange={(event) => setDraftName(event.target.value)}
            />
          </Field>

          <Field id="newPassword" label={String(t("settings.newPassword"))}>
            <Input
              id="newPassword"
              type="password"
              autoComplete="new-password"
              value={next}
              disabled={change.isPending}
              onChange={(event) => setNext(event.target.value)}
            />
          </Field>

          <Field
            id="confirmPassword"
            label={String(t("settings.confirmPassword"))}
            error={mismatch ? String(t("settings.passwordMismatch")) : undefined}
          >
            <Input
              id="confirmPassword"
              type="password"
              autoComplete="new-password"
              value={confirm}
              disabled={change.isPending}
              aria-invalid={mismatch}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </Field>

          {problem ? (
            <p role="alert" className="text-sm font-medium text-destructive sm:col-span-2">
              {problem}
            </p>
          ) : null}

          <div className="sm:col-span-2">
            <Button
              type="submit"
              loading={change.isPending}
              disabled={current === "" || (!renaming && !repassword)}
            >
              {change.isPending ? <Spinner aria-hidden="true" /> : <Key aria-hidden="true" />}
              {text("settings.saveCredentials", "Save changes")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

interface TwoFactorCardProps extends TextProp {
  enabled: boolean;
  pending: boolean;
}

function TwoFactorCard({ text, enabled, pending }: TwoFactorCardProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const enable = useEnable2fa();
  const disable = useDisable2fa();

  const [enrolOpen, setEnrolOpen] = React.useState(false);
  const [disableOpen, setDisableOpen] = React.useState(false);
  /** Kept locally because the confirming call returns a different shape. */
  const [enrolment, setEnrolment] = React.useState<TwoFactorEnableResult | null>(null);
  const [code, setCode] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [problem, setProblem] = React.useState<string | null>(null);

  const startEnrolment = (): void => {
    setEnrolment(null);
    setCode("");
    setProblem(null);
    setEnrolOpen(true);
    enable.mutate(
      {},
      {
        onSuccess: (result) => setEnrolment(result),
        onError: (error) => setProblem(error.detail),
      },
    );
  };

  const confirmEnrolment = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setProblem(null);
    enable.mutate(
      { code: code.trim() },
      {
        onSuccess: (result) => {
          if (!result.confirmed && !result.enabled) {
            setProblem(String(t("login.totpFailed")));
            return;
          }
          setEnrolOpen(false);
          setEnrolment(null);
          setCode("");
          toast({
            title: String(t("settings.twoFactorEnabled")),
            description: String(t("settings.twoFactorEnabledBody")),
            variant: "success",
          });
        },
        onError: (error) => setProblem(error.detail),
      },
    );
  };

  const confirmDisable = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setProblem(null);
    disable.mutate(
      { password },
      {
        onSuccess: () => {
          setDisableOpen(false);
          setPassword("");
          toast({
            title: String(t("settings.twoFactorDisabled")),
            description: String(t("settings.twoFactorDisabledBody")),
            variant: "success",
          });
        },
        onError: (error) => setProblem(error.detail),
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-3">
          <CardTitle>{t("settings.twoFactor")}</CardTitle>
          {pending ? (
            <Skeleton className="h-5 w-20" />
          ) : (
            <Badge variant={enabled ? "success" : "secondary"}>
              {t(enabled ? "settings.twoFactorOn" : "settings.twoFactorOff")}
            </Badge>
          )}
        </div>
        <CardDescription>{t("settings.twoFactorHint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <Notice icon={Warning} title={text("settings.twoFactorLost", "Keep a way back in")}>
          <p>
            {text(
              "settings.twoFactorLostBody",
              "If you lose the phone with the authenticator app, the password alone will not get you in. The only way back is a shell on the server:",
            )}
          </p>
          <Command command="sudo awg-panel reset-2fa" />
        </Notice>

        {enabled ? (
          <Button variant="outline" onClick={() => setDisableOpen(true)}>
            <ShieldCheck aria-hidden="true" />
            {t("settings.disable2fa")}
          </Button>
        ) : (
          <Button onClick={startEnrolment}>
            <ShieldCheck aria-hidden="true" />
            {t("settings.enable2fa")}
          </Button>
        )}
      </CardContent>

      <Dialog open={enrolOpen} onOpenChange={setEnrolOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("settings.enable2fa")}</DialogTitle>
            <DialogDescription>{t("settings.scanTotp")}</DialogDescription>
          </DialogHeader>

          {enable.isPending && !enrolment ? (
            <div className="flex flex-col items-center gap-3 py-8">
              <Spinner className="h-6 w-6" aria-hidden="true" />
              <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
            </div>
          ) : null}

          {enrolment ? (
            <form className="space-y-4" onSubmit={confirmEnrolment}>
              {enrolment.qr ? (
                <div className="flex justify-center">
                  {/* The QR is a data: URI from the panel; the CSP allows those
                      and nothing else, so no image ever leaves the server. */}
                  <img
                    src={enrolment.qr}
                    alt={text("settings.totpQrAlt", "QR code for the authenticator app")}
                    className="h-48 w-48 rounded-md border border-border bg-white p-2"
                  />
                </div>
              ) : null}

              {enrolment.secret ? (
                <div className="space-y-1.5">
                  <p className="text-sm text-muted-foreground">{t("settings.scanTotpManual")}</p>
                  <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
                    <code className="min-w-0 flex-1 break-all font-mono text-xs">
                      {enrolment.secret}
                    </code>
                    <CopyButton value={enrolment.secret} />
                  </div>
                </div>
              ) : null}

              <Field id="totpCode" label={String(t("settings.totpCode"))}>
                <Input
                  id="totpCode"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={8}
                  className="w-40 font-mono tracking-widest"
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                />
              </Field>

              {problem ? (
                <p role="alert" className="text-sm font-medium text-destructive">
                  {problem}
                </p>
              ) : null}

              <DialogFooter>
                <DialogClose asChild>
                  <Button type="button" variant="ghost">
                    {t("common.cancel")}
                  </Button>
                </DialogClose>
                <Button type="submit" loading={enable.isPending} disabled={code.trim().length < 6}>
                  {enable.isPending ? <Spinner aria-hidden="true" /> : null}
                  {t("common.confirm")}
                </Button>
              </DialogFooter>
            </form>
          ) : null}

          {!enrolment && !enable.isPending && problem ? (
            <ErrorState variant="inline" description={problem} />
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={disableOpen} onOpenChange={setDisableOpen}>
        <DialogContent>
          <form onSubmit={confirmDisable}>
            <DialogHeader>
              <DialogTitle>{t("settings.disable2faConfirm")}</DialogTitle>
              <DialogDescription>{t("settings.disable2faConfirmBody")}</DialogDescription>
            </DialogHeader>

            <div className="py-4">
              <Field
                id="disable2faPassword"
                label={String(t("settings.currentPassword"))}
                hint={text(
                  "settings.disable2faPasswordHint",
                  "Turning two-factor off is a password-protected change, exactly like turning it on.",
                )}
              >
                <Input
                  id="disable2faPassword"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </Field>
              {problem ? (
                <p role="alert" className="mt-3 text-sm font-medium text-destructive">
                  {problem}
                </p>
              ) : null}
            </div>

            <DialogFooter>
              <DialogClose asChild>
                <Button type="button" variant="ghost">
                  {t("common.cancel")}
                </Button>
              </DialogClose>
              <Button
                type="submit"
                variant="destructive"
                loading={disable.isPending}
                disabled={!password}
              >
                {disable.isPending ? <Spinner aria-hidden="true" /> : null}
                {t("settings.disable2fa")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/**
 * The certificate the form's path points at, read by the page rather than here.
 *
 * Two things on this tab describe the same file - which name a client config
 * gets, and which address the page reconnects at - and the second belongs to
 * the page, which owns the confirmation and the handover. One query, passed
 * down, is what keeps them from answering differently.
 */
interface TlsFormProps extends SettingsFormProps {
  certificate: UseQueryResult<TlsCertificate, ApiError>;
}

function TlsTab({
  values,
  saved,
  errors,
  disabled,
  onChange,
  text,
  certificate,
}: TlsFormProps): JSX.Element {
  const { t } = useTranslation();

  // There is no separate on/off setting: the service serves HTTPS when it has
  // both a certificate and a key, so the switch is those two fields.
  const on = tlsOn(values);
  const partially = !on && (values.tlsCertPath.trim() !== "" || values.tlsKeyPath.trim() !== "");
  const servedOverHttp = typeof window !== "undefined" && window.location.protocol !== "https:";

  /*
   * Two days, and the number is about trouble rather than about lifetime.
   *
   * A certificate issued for an address lives six days and is renewed with
   * roughly two to spare, so anything that warned on "less than a fortnight
   * left" would be lit permanently on a panel where nothing is wrong - and a
   * warning that is always on is one nobody reads on the day it starts meaning
   * something. Below two days, both kinds are late: a ninety-day certificate
   * should have renewed sixty days ago and a six-day one should have renewed
   * yesterday, so either way a renewal has already failed at least once and
   * there is still a day or so to notice.
   */
  const days = certificate.data?.expiresInDays ?? null;
  const expiryWarning = on && days !== null && days <= 2 ? { expired: days < 0 } : null;

  /*
   * What the two boxes held when the switch was last turned off.
   *
   * Turning it off empties them, so turning it straight back on has nothing to
   * read: it used to write the default paths, which left a panel whose
   * certificate lives anywhere else offering to save a change the operator did
   * not make and could not see - the fields it names are hidden behind the
   * switch that is back where it started.
   *
   * The stored paths are the fallback, because Radix drops this tab's contents
   * on the way to another one and takes the ref with them. They are the same
   * two values in every case but one: a path typed, not saved, and the switch
   * flicked while another tab was in front.
   */
  const cleared = React.useRef<Pick<PanelSettings, "tlsCertPath" | "tlsKeyPath"> | null>(null);

  const toggle = (next: boolean): void => {
    if (!next) {
      cleared.current = { tlsCertPath: values.tlsCertPath, tlsKeyPath: values.tlsKeyPath };
      onChange("tlsCertPath", "");
      onChange("tlsKeyPath", "");
      return;
    }
    const back = cleared.current ?? saved;
    if (values.tlsCertPath.trim() === "") {
      onChange("tlsCertPath", back.tlsCertPath || TLS_CERT_DEFAULT);
    }
    if (values.tlsKeyPath.trim() === "") {
      onChange("tlsKeyPath", back.tlsKeyPath || TLS_KEY_DEFAULT);
    }
  };

  return (
    <div className="space-y-4">
      {servedOverHttp ? (
        <Notice icon={Warning} title={String(t("settings.tlsOff"))}>
          <p>{t("settings.tlsOffHint")}</p>
        </Notice>
      ) : null}

      <Card>
        <CardHeader>
          {/*
           * The switch sits against the word it governs rather than across the
           * card from it, so the state and the thing in that state are read in
           * one glance. Both are coloured, and the colour only repeats what the
           * switch already says with its position: nothing here is told by
           * colour alone.
           */}
          <div className="flex flex-wrap items-center gap-3">
            <CardTitle className={on ? "text-success" : "text-destructive"}>
              {t("settings.tls")}
            </CardTitle>
            <Switch
              id="tlsEnabled"
              checked={on}
              disabled={disabled}
              onCheckedChange={toggle}
              aria-label={String(t("settings.tls"))}
              className="data-[state=checked]:bg-success data-[state=unchecked]:bg-destructive"
            />
          </div>
          <CardDescription>{t("settings.tlsHint")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {on || partially ? (
            <div className="grid gap-6 sm:grid-cols-2">
              <Field
                id="tlsCertPath"
                label={String(t("settings.tlsCert"))}
                hint={text(
                  "settings.tlsCertHint",
                  "Full chain in PEM form, readable by root and outside /root and /home, which the service cannot see. A self-signed certificate works, and browsers will warn about it.",
                )}
                error={errors.tlsCertPath}
              >
                <Input
                  id="tlsCertPath"
                  value={values.tlsCertPath}
                  disabled={disabled}
                  spellCheck={false}
                  autoComplete="off"
                  className="font-mono"
                  aria-invalid={Boolean(errors.tlsCertPath)}
                  onChange={(event) => onChange("tlsCertPath", event.target.value)}
                />
              </Field>

              <Field
                id="tlsKeyPath"
                label={String(t("settings.tlsKey"))}
                hint={text(
                  "settings.tlsKeyHint",
                  "The matching private key in PEM form. Keep it 0600 and owned by root, and beside the certificate rather than under /root or /home.",
                )}
                error={errors.tlsKeyPath}
              >
                <Input
                  id="tlsKeyPath"
                  value={values.tlsKeyPath}
                  disabled={disabled}
                  spellCheck={false}
                  autoComplete="off"
                  className="font-mono"
                  aria-invalid={Boolean(errors.tlsKeyPath)}
                  onChange={(event) => onChange("tlsKeyPath", event.target.value)}
                />
              </Field>
            </div>
          ) : null}

          {expiryWarning ? (
            <Notice
              icon={Warning}
              tone={expiryWarning.expired ? "danger" : "warning"}
              title={
                expiryWarning.expired
                  ? text("settings.tlsExpiredTitle", "The certificate has expired")
                  : text("settings.tlsExpiringTitle", "The certificate expires within days")
              }
            >
              <p>
                {text(
                  "settings.tlsExpiringBody",
                  "Renewals normally replace it well before this point, so seeing this means the renewal has already missed at least one attempt. Check the timer that owns it - for certbot, systemctl list-timers '*certbot*' and certbot renew --dry-run - and remember that an http-01 renewal needs inbound port 80 to reach this server.",
                )}
              </p>
            </Notice>
          ) : null}
        </CardContent>
      </Card>

      <ClientAddressCard
        values={values}
        saved={saved}
        errors={errors}
        disabled={disabled}
        onChange={onChange}
        text={text}
        certificate={certificate}
      />
    </div>
  );
}

function isEndpointMode(value: string): value is ConfigEndpointMode {
  return value === "ip" || value === "domain";
}

/**
 * Which address a client config is handed out with.
 *
 * A server is set up at an IP, so that is what every config on disk says. Once
 * a certificate arrives the panel has a name, and the name is what people are
 * given from then on - but re-issuing every client to change how one line spells
 * the same host would hand every device new keys. So the swap happens as the
 * config leaves: download, QR code and export archive all go through it, the
 * files on disk are untouched, and the tunnel keeps running what it was given.
 *
 * Nothing here restarts anything, which is why it is a plain save rather than
 * part of the confirmed, reconnecting one the fields above it need.
 *
 * The names come from the path in the form above, not from the stored one, so
 * a certificate about to be saved offers its names before the save rather than
 * after it - and this card and the confirmation over it are reading one answer.
 */
function ClientAddressCard({
  values,
  errors,
  disabled,
  onChange,
  text,
  certificate,
}: TlsFormProps): JSX.Element {
  /*
   * Wildcards are dropped: *.example.com is a name a browser matches against,
   * not one a client can dial. Addresses go with them for a different reason.
   * A certificate issued for 198.51.100.7 - which is what Let's Encrypt hands
   * back for a panel administered at an address - names the address the client
   * configs already carry, so there is no name on it to offer and no swap it
   * could make. Leaving it in would put an address under a box labelled
   * "Domain" and ask which one to hand out, when both answers are the same
   * string.
   */
  const dialable = (certificate.data?.names ?? []).filter((name) => !name.startsWith("*"));
  const names = dialable.filter((name) => !isAddress(name));
  const fromCertificate = certificate.data?.domain ?? "";
  /** The address such a certificate covers, when an address is all it covers. */
  const address = names.length === 0 ? (dialable[0] ?? "") : "";
  const usingDomain = values.configEndpointMode === "domain";
  const pinned = values.configEndpointHost.trim();
  /** What a config downloaded right now would carry, or "" for "whatever it says". */
  const handedOut = usingDomain ? pinned || fromCertificate : "";

  let hint = text(
    "settings.configEndpointHostHintNoCert",
    "The panel has no certificate to take a name from, so type the domain clients should connect to.",
  );
  if (fromCertificate) {
    hint = text(
      "settings.configEndpointHostHint",
      "Leave this empty to follow the certificate ({{domain}}), so a renewal for another name is picked up on its own.",
      { domain: fromCertificate },
    );
  } else if (address) {
    hint = text(
      "settings.configEndpointHostHintAddress",
      "The certificate is for the server's own address ({{address}}) rather than a name, and that address is what client configs already carry. Fill this in only if clients should be handed a domain instead.",
      { address },
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{text("settings.configEndpoint", "Address in client configs")}</CardTitle>
        <CardDescription>
          {text(
            "settings.configEndpointHint",
            "Client configs are written with the address the server was set up on, which is normally its IP. The panel can hand them over with the certificate's domain instead. Nothing on the server is rewritten: the swap happens on the download, the QR code and the export.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-6 sm:grid-cols-2">
        <Field
          id="configEndpointMode"
          label={text("settings.configEndpointMode", "Clients connect to")}
          error={errors.configEndpointMode}
        >
          <Select
            value={values.configEndpointMode}
            disabled={disabled}
            onValueChange={(value) => {
              if (isEndpointMode(value)) {
                onChange("configEndpointMode", value);
              }
            }}
          >
            <SelectTrigger id="configEndpointMode">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ip">
                {text("settings.configEndpointIp", "The server's own address (IP)")}
              </SelectItem>
              <SelectItem value="domain">
                {text("settings.configEndpointDomain", "A domain name")}
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>

        {usingDomain ? (
          <Field
            id="configEndpointHost"
            label={text("settings.configEndpointHost", "Domain")}
            hint={hint}
            error={errors.configEndpointHost}
          >
            <Input
              id="configEndpointHost"
              value={values.configEndpointHost}
              disabled={disabled}
              spellCheck={false}
              autoComplete="off"
              className="font-mono"
              placeholder={fromCertificate}
              list={names.length > 0 ? "awg-certificate-names" : undefined}
              aria-invalid={Boolean(errors.configEndpointHost)}
              onChange={(event) => onChange("configEndpointHost", event.target.value)}
            />
            {names.length > 0 ? (
              <datalist id="awg-certificate-names">
                {names.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            ) : null}
          </Field>
        ) : null}

        {usingDomain && !handedOut ? (
          <div className="sm:col-span-2">
            <Notice
              icon={Warning}
              title={text("settings.configEndpointNothingToUse", "There is no domain to hand out")}
            >
              <p>
                {address
                  ? text(
                      "settings.configEndpointNothingToUseAddressBody",
                      "The certificate covers the server's own address ({{address}}), which is the address client configs already carry, so there is nothing for it to swap in. Type the domain clients should connect to, or set this back to the server's own address.",
                      { address },
                    )
                  : text(
                      "settings.configEndpointNothingToUseBody",
                      "Set a certificate above, or type the domain here. Until then configs go out with the address the server config carries.",
                    )}
              </p>
            </Notice>
          </div>
        ) : null}

        {certificate.isError ? (
          <p className="text-xs leading-relaxed text-muted-foreground sm:col-span-2">
            {text(
              "settings.configEndpointCertUnread",
              "The certificate could not be read just now, so there is no list of names to choose from. Typing one still works.",
            )}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function BackupTab({ text }: TextProp): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const backup = useBackup();
  const restore = useRestore();

  const [file, setFile] = React.useState<File | null>(null);
  const [typed, setTyped] = React.useState("");
  const [problem, setProblem] = React.useState<string | null>(null);
  /*
   * What the server said the restore did, kept on the card rather than shown in
   * a toast. It is not a confirmation: it is where a settings change that was
   * declined, a client config that was removed, a tunnel that did not come back
   * up and a password that has just reverted to the backup's are reported, and
   * every one of those is something the admin has to act on after reading it
   * twice. A toast that has already faded is the wrong place for all of it.
   */
  const [outcome, setOutcome] = React.useState<string | null>(null);
  const [reconnectUrl, setReconnectUrl] = React.useState<string | null>(null);
  const word = text(RESTORE_WORD_KEY, RESTORE_WORD_FALLBACK);
  const armed = file !== null && typed.trim().toUpperCase() === word.toUpperCase();

  /*
   * The <input type="file"> is kept off screen and driven by the button beside
   * it. Its own control is drawn by the browser rather than by this panel: it
   * ignores the theme, and the words on it - "Choose File", "No file chosen" -
   * come from the browser's language, so a panel running in Russian showed one
   * English control in the middle of a translated form.
   *
   * What the operator sees is therefore this component's own button and the
   * name of whatever they picked, and `file` below is the only record of that
   * choice. The element is emptied whenever the choice is dropped, because a
   * file input that still holds a name fires no change event when the same file
   * is picked again - and picking the same archive after a failed restore is
   * exactly what someone would do.
   */
  const picker = React.useRef<HTMLInputElement>(null);

  const forget = (): void => {
    setFile(null);
    setProblem(null);
    if (picker.current) {
      picker.current.value = "";
    }
  };

  const download = (): void => {
    backup.mutate(undefined, {
      onSuccess: (result) => {
        toast({
          title: String(t("settings.backupCreated")),
          description: String(t("settings.backupCreatedBody", { name: result.filename })),
          variant: "success",
        });
      },
      onError: (error) => {
        toast({
          title: String(t("settings.backupFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  const runRestore = (): void => {
    if (!file) {
      return;
    }
    setProblem(null);
    setOutcome(null);
    restore.mutate(file, {
      onSuccess: (result) => {
        forget();
        setTyped("");
        setOutcome(result.detail ?? null);
        toast({
          title: String(t("settings.restored")),
          description: String(t("settings.restoredBody", { count: result.clients ?? 0 })),
          variant: "success",
        });
        /*
         * The archive carried a database, so the service is going down and
         * coming back a couple of seconds from now. The same handover the
         * settings page uses, and to the same address: a restore deliberately
         * does not move the panel, so this is the origin the page is already
         * on and the overlay can poll the health endpoint until it answers.
         */
        if (result.restarting) {
          setReconnectUrl(`${window.location.origin}${bootstrap.basePath}`);
        }
      },
      onError: (error) => {
        setProblem(error.detail);
        toast({
          title: String(t("settings.restoreFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("settings.backupDownload")}</CardTitle>
          <CardDescription>{t("settings.backupHint")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Notice icon={Key} title={text("settings.backupSecretTitle", "This file is a secret")}>
            <p>
              {text(
                "settings.backupSecretBody",
                "The archive contains the server private key and the private key of every client. Anyone who gets hold of it can impersonate your server or connect as any of your devices. Store it the way you would store a password, and do not mail it to yourself.",
              )}
            </p>
          </Notice>

          <ul className="space-y-1.5 text-sm text-muted-foreground">
            <li>
              {text("settings.backupContents1", "The server config and its obfuscation settings")}
            </li>
            <li>{text("settings.backupContents2", "Every client config, keys included")}</li>
            <li>
              {text(
                "settings.backupContents3",
                "The panel database: users, settings and traffic history",
              )}
            </li>
          </ul>

          <Button onClick={download} loading={backup.isPending}>
            {backup.isPending ? (
              <Spinner aria-hidden="true" />
            ) : (
              <DownloadSimple aria-hidden="true" />
            )}
            {t("settings.backupDownload")}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("settings.backupRestore")}</CardTitle>
          <CardDescription>{t("settings.restoreWarning")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Field
            id="restoreFile"
            label={String(t("settings.restoreSelect"))}
            hint={text(
              "settings.restoreSelectHint",
              "A .tar.gz written by this panel, or an older one from when awg-menu wrote them.",
            )}
          >
            <input
              ref={picker}
              type="file"
              className="hidden"
              accept=".tar.gz,.tgz,application/gzip,application/x-gzip"
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setProblem(null);
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button
                id="restoreFile"
                type="button"
                variant="outline"
                disabled={restore.isPending}
                onClick={() => picker.current?.click()}
              >
                <FolderOpen aria-hidden="true" />
                {text("settings.restoreChoose", "Choose file")}
              </Button>

              {file ? (
                <span className="flex min-w-0 items-center gap-2 rounded-md border bg-muted/40 py-1 pe-1 ps-2.5 text-sm">
                  <Archive aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate" title={file.name}>
                    {file.name}
                  </span>
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                    {formatBytes(file.size)}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 shrink-0"
                    disabled={restore.isPending}
                    aria-label={text("settings.restoreClearFile", "Forget this file")}
                    onClick={forget}
                  >
                    <X aria-hidden="true" />
                  </Button>
                </span>
              ) : (
                <span className="text-sm text-muted-foreground">
                  {text("settings.restoreNoFile", "No file chosen")}
                </span>
              )}
            </div>
          </Field>

          <Field
            id="restoreConfirmWord"
            label={text("settings.restoreTypeToConfirm", "Type {{word}} to confirm", { word })}
            hint={text(
              "settings.restoreTypeHint",
              "Restoring replaces the server config and every client. Devices whose keys are not in this archive stop working immediately.",
            )}
          >
            <Input
              id="restoreConfirmWord"
              value={typed}
              disabled={restore.isPending || file === null}
              autoComplete="off"
              spellCheck={false}
              className="w-48 font-mono tracking-wide"
              onChange={(event) => setTyped(event.target.value)}
            />
          </Field>

          {problem ? (
            <p role="alert" className="text-sm font-medium text-destructive">
              {problem}
            </p>
          ) : null}

          {outcome ? (
            <Notice
              icon={Info}
              tone="info"
              title={text("settings.restoreOutcomeTitle", "What the restore did")}
            >
              <p>{outcome}</p>
            </Notice>
          ) : null}

          <Button
            variant="destructive"
            loading={restore.isPending}
            disabled={!armed}
            onClick={runRestore}
          >
            {restore.isPending ? (
              <Spinner aria-hidden="true" />
            ) : (
              <UploadSimple aria-hidden="true" />
            )}
            {t(restore.isPending ? "settings.restoring" : "settings.backupRestore")}
          </Button>
        </CardContent>
      </Card>

      {reconnectUrl ? (
        <ReconnectOverlay url={reconnectUrl} text={text} onDismiss={() => setReconnectUrl(null)} />
      ) : null}
    </div>
  );
}

interface UpdateTabProps extends TextProp {
  version: string;
}

/**
 * The steps bin/awg-update reports, in the order it reports them.
 *
 * Used only to draw "step 3 of 7" and fill a bar. A phase this does not know
 * about is not an error and must not blank the progress out: the updater is a
 * shell script on the server and can be a version ahead of this page, which is
 * precisely what happens on the run that installs the new one.
 */
const UPDATE_PHASES = [
  "resolve",
  "preflight",
  "download",
  "verify",
  "backup",
  "install",
  "confirm",
] as const;

function phaseIndex(phase: string | undefined): number {
  const at = UPDATE_PHASES.indexOf((phase ?? "") as (typeof UPDATE_PHASES)[number]);
  return at < 0 ? 0 : at;
}

function UpdateTab({ text, version }: UpdateTabProps): JSX.Element {
  const { t } = useTranslation();
  const client = useQueryClient();
  const check = useUpdateCheck();
  const apply = useUpdateApply();
  const state = useUpdateStatus();

  const [confirmOpen, setConfirmOpen] = React.useState(false);

  // The hook caches the answer, so switching tabs and coming back does not
  // look like the check never happened.
  const result: UpdateCheck | undefined =
    check.data ?? client.getQueryData<UpdateCheck>(queryKeys.updateCheck());

  const newer = result?.updateAvailable === true;
  const status = state.data;
  const running = status?.status === "running";
  const unavailable = status?.unavailable ?? "";

  // Whether this update finished while this page was open, as opposed to one
  // that ran days ago and is only still in the state file. The difference is
  // the whole reason the reload notice is worth showing at all: a page that has
  // been open across a successful update is a page running the previous
  // release's JavaScript against the new release's API.
  const wasRunning = React.useRef(false);
  const [justFinished, setJustFinished] = React.useState(false);
  React.useEffect(() => {
    if (running) {
      wasRunning.current = true;
      return;
    }
    if (wasRunning.current && (status?.status === "succeeded" || status?.status === "failed")) {
      wasRunning.current = false;
      setJustFinished(true);
    }
  }, [running, status?.status]);

  const startUpdate = (): void => {
    setConfirmOpen(false);
    apply.mutate();
  };

  const target = status?.toVersion || result?.latest || "";
  const step = phaseIndex(status?.phase) + 1;

  /*
   * The single sentence the buttons above have to answer with, and never more
   * than one of them: a failed apply is the answer to the button just pressed,
   * and a check that could not run has more to say than a stale "up to date"
   * left over from the last one.
   *
   * Every one of these is a finished sentence from the server - the release
   * feed's own explanation, or the updater's - so it is shown as it stands,
   * with no title above it repeating that something went wrong.
   */
  const note: { icon: Icon; tone: "warning" | "success" | "danger"; message: string } | null =
    apply.isError
      ? { icon: Warning, tone: "danger", message: apply.error.detail }
      : check.isError
        ? { icon: Warning, tone: "danger", message: check.error.detail }
        : result?.checked === false
          ? {
              icon: Warning,
              tone: "warning",
              message:
                result.reason ||
                text(
                  "settings.updateCheckFailedHint",
                  "The server could not reach the release feed. This does not affect anything running.",
                ),
            }
          : result?.checked && !newer && !running
            ? {
                icon: ShieldCheck,
                tone: "success",
                message: text("settings.upToDate", "You are on the latest version"),
              }
            : null;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("settings.update")}</CardTitle>
          <CardDescription>{t("settings.updateHint")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <dt className="text-sm text-muted-foreground">{t("settings.currentVersion")}</dt>
              <dd className="font-mono text-sm font-medium">{version}</dd>
            </div>
            {result?.latest ? (
              <div className="space-y-1">
                <dt className="text-sm text-muted-foreground">{t("settings.latestVersion")}</dt>
                <dd className="flex flex-wrap items-center gap-2 font-mono text-sm font-medium">
                  {result.latest}
                  <Badge variant={newer ? "warning" : "success"}>
                    {newer
                      ? text("settings.newRelease", "Newer")
                      : text("settings.sameRelease", "Installed")}
                  </Badge>
                </dd>
              </div>
            ) : null}
          </dl>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button
              variant="outline"
              onClick={() => check.mutate()}
              loading={check.isPending}
              disabled={running}
            >
              {check.isPending ? (
                <Spinner aria-hidden="true" />
              ) : (
                <ArrowClockwise aria-hidden="true" />
              )}
              {t(check.isPending ? "settings.checking" : "settings.checkUpdate")}
            </Button>
            {newer && !running && !unavailable ? (
              <Button onClick={() => setConfirmOpen(true)} loading={apply.isPending}>
                {apply.isPending ? (
                  <Spinner aria-hidden="true" />
                ) : (
                  <DownloadSimple aria-hidden="true" />
                )}
                {text("settings.updateNow", "Update to {{version}}", {
                  version: result?.latest ?? "",
                })}
              </Button>
            ) : null}
            {result?.url ? (
              <Button asChild variant="ghost">
                <a href={result.url} target="_blank" rel="noreferrer noopener">
                  <ArrowSquareOut aria-hidden="true" />
                  {t("settings.viewRelease")}
                </a>
              </Button>
            ) : null}
            {note ? (
              <InlineNote icon={note.icon} tone={note.tone}>
                {note.message}
              </InlineNote>
            ) : null}
          </div>

          {/*
           * The release notes, which are the one answer with a body to it and
           * so the one that still gets a block. Without them the news that a
           * version is available is already on this card twice over - the badge
           * beside the version, the button offering to install it - and a third
           * copy in a border would say nothing new.
           */}
          {newer && result?.latest && result.notes ? (
            <Notice
              icon={DownloadSimple}
              title={String(t("settings.updateAvailable", { version: result.latest }))}
            >
              <div className="space-y-1.5">
                <p className="font-medium text-foreground">{t("settings.releaseNotes")}</p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted/60 p-3 font-mono text-xs leading-relaxed">
                  {result.notes}
                </pre>
              </div>
            </Notice>
          ) : null}

          {/*
           * Why the button is not on this page, said where the button would
           * have been. A panel installed some other way, or one whose updater
           * has been removed, can still be updated - just not from here - so
           * this says so rather than leaving an operator clicking nothing.
           */}
          {unavailable ? (
            <Notice icon={Warning} tone="info" title={String(t("settings.updateNotSelfUpdatable"))}>
              <p>{unavailable}</p>
            </Notice>
          ) : null}
        </CardContent>
      </Card>

      {status && status.status !== "idle" ? (
        <UpdateProgressCard
          status={status}
          step={step}
          steps={UPDATE_PHASES.length}
          justFinished={justFinished}
          text={text}
        />
      ) : null}

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {text("settings.updateConfirm", "Update this server to {{version}}?", {
                version: target,
              })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {text(
                "settings.updateConfirmBody",
                "A full backup is taken first, then the release is downloaded, checked against its published checksum and installed. Your settings, clients, keys, accounts and traffic history are all kept. It takes a few minutes: the panel restarts near the end and the tunnel drops for a few seconds, so connected clients reconnect.",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={startUpdate}>
              {text("settings.updateStart", "Update now")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

interface UpdateProgressCardProps extends TextProp {
  status: UpdateStatus;
  step: number;
  steps: number;
  justFinished: boolean;
}

/**
 * What the update is doing, while it does it.
 *
 * The log is shown rather than summarised because there is nothing else to look
 * at for the four minutes a kernel module takes to build, and because the one
 * question an operator has during an update - is it stuck? - is answered by
 * lines still arriving and by nothing else.
 */
function UpdateProgressCard({
  status,
  step,
  steps,
  justFinished,
  text,
}: UpdateProgressCardProps): JSX.Element {
  const { t } = useTranslation();
  const running = status.status === "running";
  const failed = status.status === "failed";

  // Pinned to the bottom as lines arrive, unless the operator has scrolled up
  // to read something - at which point they are reading, and yanking the view
  // back down is the panel arguing with them.
  const logRef = React.useRef<HTMLPreElement>(null);
  const stick = React.useRef(true);
  React.useEffect(() => {
    const box = logRef.current;
    if (!box || !stick.current) {
      return;
    }
    box.scrollTop = box.scrollHeight;
  }, [status.log]);

  const onScroll = (): void => {
    const box = logRef.current;
    if (!box) {
      return;
    }
    stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
  };

  const title = running
    ? text("settings.updateRunning", "Updating to {{version}}", {
        version: status.toVersion || "",
      })
    : failed
      ? String(t("settings.updateFailed"))
      : String(t("settings.updateDone"));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {running ? <Spinner className="h-4 w-4" aria-hidden="true" /> : null}
          {title}
        </CardTitle>
        <CardDescription>
          {status.fromVersion && status.toVersion
            ? `${status.fromVersion} → ${status.toVersion}`
            : null}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {running ? (
          <div className="space-y-1.5">
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={steps}
              aria-valuenow={step}
              aria-label={String(t("settings.updateProgress"))}
              className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
            >
              <div
                className="h-full rounded-full bg-primary transition-[width] duration-500"
                style={{ width: `${Math.round((step / steps) * 100)}%` }}
              />
            </div>
            <p aria-live="polite" className="text-sm text-muted-foreground">
              {text("settings.updateStepOf", "Step {{step}} of {{steps}}", { step, steps })}
              {status.detail ? ` — ${status.detail}` : null}
            </p>
          </div>
        ) : (
          <Notice
            icon={failed ? Warning : CheckCircle}
            tone={failed ? "danger" : "info"}
            title={title}
          >
            {status.detail ? <p>{status.detail}</p> : null}
          </Notice>
        )}

        {/*
         * Only after an update this page watched finish. The panel it is
         * talking to is a different release from the one that served this
         * JavaScript, and while that mostly works it is not something to leave
         * somebody in without telling them.
         */}
        {justFinished && !failed ? (
          <Notice
            icon={ArrowClockwise}
            tone="info"
            title={String(t("settings.updateReload"))}
            action={
              <Button size="sm" onClick={() => window.location.reload()}>
                <ArrowClockwise aria-hidden="true" />
                {text("settings.updateReloadAction", "Reload")}
              </Button>
            }
          >
            <p>
              {text(
                "settings.updateReloadBody",
                "This page is still the previous version's. Reload it to pick up the new one.",
              )}
            </p>
          </Notice>
        ) : null}

        {status.backup ? (
          <div className="space-y-1">
            <p className="text-sm font-medium">{text("settings.updateBackup", "Backup taken")}</p>
            <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
              <Archive className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-xs">
                {status.backup}
              </code>
              <CopyButton value={status.backup} />
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {text(
                "settings.updateBackupHint",
                "Taken on this server before anything was replaced. Restore it from the Backup tab if you ever need to.",
              )}
            </p>
          </div>
        ) : null}

        {status.log ? (
          <div className="space-y-1.5">
            <p className="text-sm font-medium">{text("settings.updateLog", "Update log")}</p>
            <pre
              ref={logRef}
              onScroll={onScroll}
              aria-live="off"
              className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-muted/60 p-3 font-mono text-xs leading-relaxed"
            >
              {status.log}
            </pre>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Reconnect overlay                                                           */
/* -------------------------------------------------------------------------- */

interface ReconnectOverlayProps extends TextProp {
  url: string;
  onDismiss: () => void;
}

/** The unauthenticated liveness endpoint, which is what tells us it is back. */
function healthUrl(panelUrl: string): string {
  const base = panelUrl.endsWith("/") ? panelUrl : `${panelUrl}/`;
  return `${base}api/v1/health`;
}

/**
 * Full-screen takeover after a save that restarts the web service.
 *
 * When only the secret path changed, the new URL is the same origin, so the
 * health endpoint can be polled and the page can move itself the moment it
 * answers. When the port, the listen address or TLS changed, the new URL is a
 * different origin and the panel's own CSP (default-src 'self') stops this page
 * from probing it at all, so pretending to poll would just be a spinner that
 * never ends. There we count down and hand the browser the address, which is
 * the only thing that can actually reach it.
 */
function ReconnectOverlay({ url, text, onDismiss }: ReconnectOverlayProps): JSX.Element {
  const { t } = useTranslation();
  // Hand-built rather than a Dialog, so it has to declare itself: the toast
  // from the save that opened it is still on screen, over these two buttons.
  const overlayRef = useModalPresence<HTMLDivElement>(null);

  const sameOrigin = React.useMemo(() => {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch {
      return false;
    }
  }, [url]);

  const [phase, setPhase] = React.useState<"waiting" | "failed">("waiting");
  const [seconds, setSeconds] = React.useState(HANDOFF_SECONDS);

  // Cross-origin: nothing to poll, so run the clock and then navigate. The
  // navigation happens in the effect rather than inside the state updater,
  // which React is free to run more than once.
  React.useEffect(() => {
    if (sameOrigin) {
      return;
    }
    if (seconds <= 0) {
      window.location.assign(url);
      return;
    }
    const timer = window.setTimeout(() => setSeconds((left) => left - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [sameOrigin, seconds, url]);

  // Same origin: ask the health endpoint until it answers.
  React.useEffect(() => {
    if (!sameOrigin) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const startedAt = Date.now();
    const target = healthUrl(url);

    const attempt = async (): Promise<void> => {
      if (cancelled) {
        return;
      }
      try {
        const response = await fetch(target, { cache: "no-store", credentials: "same-origin" });
        if (!cancelled && response.ok) {
          window.location.replace(url);
          return;
        }
      } catch {
        // Still down. That is the expected answer for the first few seconds.
      }
      if (cancelled) {
        return;
      }
      if (Date.now() - startedAt > RECONNECT_DEADLINE_MS) {
        setPhase("failed");
        return;
      }
      timer = window.setTimeout(() => void attempt(), RECONNECT_POLL_MS);
    };

    timer = window.setTimeout(() => void attempt(), RECONNECT_FIRST_DELAY_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [sameOrigin, url]);

  const failed = phase === "failed";

  return (
    <div
      ref={overlayRef}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="reconnect-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 p-4 backdrop-blur"
    >
      <div className="w-full max-w-lg rounded-lg border border-border bg-card p-6 text-card-foreground shadow-lg">
        <div className="flex items-start gap-3">
          <span
            className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-full",
              failed ? "bg-warning/15 text-warning" : "bg-primary/10 text-primary",
            )}
          >
            {failed ? (
              <Warning weight="duotone" className="h-5 w-5" aria-hidden="true" />
            ) : (
              <Power weight="duotone" className="h-5 w-5" aria-hidden="true" />
            )}
          </span>
          <div className="min-w-0">
            <h2 id="reconnect-title" className="text-lg font-semibold tracking-tight">
              {t(failed ? "settings.reconnectFailed" : "settings.restartNotice")}
            </h2>
            <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
              {failed
                ? String(t("settings.reconnectFailedHint", { url }))
                : String(t("settings.restartNoticeBody", { url }))}
            </p>
          </div>
        </div>

        {!failed ? (
          <div className="mt-5 flex items-center gap-3 rounded-md border border-border bg-muted/50 px-3 py-2.5">
            <Spinner className="h-4 w-4" aria-hidden="true" />
            <p aria-live="polite" className="text-sm text-muted-foreground">
              {sameOrigin
                ? String(t("settings.reconnecting"))
                : text(
                    "settings.reconnectHandoff",
                    "This page cannot check another address for you, so you will be taken there in {{count}}s.",
                    { count: seconds },
                  )}
            </p>
          </div>
        ) : null}

        <div className="mt-5 space-y-2">
          <p className="text-xs font-medium text-muted-foreground">
            {text("settings.reconnectAddress", "The new address")}
          </p>
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
            <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs">
              {url}
            </code>
            <CopyButton value={url} />
          </div>
        </div>

        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={onDismiss}>
            {text("settings.reconnectStay", "Stay on this page")}
          </Button>
          <Button asChild>
            <a href={url}>
              <ArrowSquareOut aria-hidden="true" />
              {text("settings.reconnectOpen", "Open the new address")}
            </a>
          </Button>
        </div>

        {failed ? (
          <div className="mt-5 space-y-2">
            <Separator />
            <p className="pt-3 text-xs text-muted-foreground">
              {text("settings.reconnectCheck", "Still nothing? Check the service on the server:")}
            </p>
            <Command command="sudo awg-panel status" />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** The tab is a form, so it loads as a form rather than as a spinner. */
function LoadingForm(): JSX.Element {
  return (
    <Card>
      <CardHeader>
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-4 w-full max-w-md" />
      </CardHeader>
      <CardContent className="grid gap-6 sm:grid-cols-2">
        {[0, 1, 2, 3, 4, 5].map((field) => (
          <div key={field} className="space-y-2">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-3 w-48" />
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
