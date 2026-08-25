import * as React from "react";
import { useTranslation } from "react-i18next";
import { Clock, Shuffle, Warning } from "@/lib/icons";

import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ErrorState";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DatePicker } from "@/components/DatePicker";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { useClient, useCreateClient, useSettings, useUpdateClient } from "@/api/hooks";
import { randomName, replaceSuggestion, typedCharacter } from "@/lib/names";
import { MBIT, cn, expiryParts, formatBytes } from "@/lib/utils";
import type { ApiError } from "@/api/client";
import type { Client, CreateClientInput, UpdateClientInput } from "@/api/types";

/*
 * Create and edit in one form, because the two differ in three details and not
 * in shape: creating also allocates an address and a keypair, editing shows the
 * address it already has, and renaming keeps both.
 *
 * The name is the fourth difference, and the only one that changes what the
 * form asks for. A client's name is its identifier - the file its key is
 * written into, the word every per-client route is addressed by - so one has to
 * be chosen before anything can be created, and on the fortieth client of an
 * afternoon nobody has a fortieth word. So the box opens with nine random
 * characters already in it, drawn the same way the server draws them: save it
 * as it stands, type over it, or clear it and let the server name the client.
 * Editing shows the name the client has and offers none of that.
 *
 * Nothing typed here is secret. The keys are generated on the server and never
 * cross this form, which is why the dialog can be reopened and resubmitted
 * without any risk of handing out a stale private key.
 */

/*
 * Decimal, like the plans a data limit is sold in: 10 GB is ten thousand million
 * bytes, which is the number an operator quoting ten gigabytes means. Every
 * traffic figure in the panel is counted the same way (see formatBytes), so the
 * usage under this field is measured in the unit the limit above it was set in.
 */
const GB = 1e9;
const TB = 1e12;
/** One byte, as a fraction of a gigabyte: the finest a limit is ever worth saying. */
const BYTE_DECIMALS = 9;

/** Route everything through the tunnel. The value the config file carries. */
const FULL_TUNNEL = "0.0.0.0/0";

/** Same rule as store.validate_name, so the panel rejects what the CLI would. */
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;

/** ClientMeta.email is a CharField(190); anything longer is refused by the API. */
const EMAIL_MAX = 190;

/**
 * What the quick buttons add, in days and in gigabytes.
 *
 * Offsets rather than settings, so four buttons cover any period and any size:
 * a click adds to whatever the field already holds, and three clicks on +30d
 * are a quarter. The alternative - each button naming an absolute value - needs
 * one button per period an operator might sell, and still cannot express the
 * one that is missing.
 */
const EXPIRY_PRESET_DAYS = [1, 7, 30, 90] as const;
const QUOTA_PRESET_GB = [1, 2, 5, 10] as const;

/**
 * The speeds worth one click. Absolute rather than cumulative, unlike the quota
 * and expiry buttons beside them: a data allowance is a thing you add more of,
 * and a speed is a tier you pick. Clicking 50 twice should not mean 100.
 */
const SPEED_PRESET_MBPS = [10, 25, 50, 100] as const;

const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;

type AllowedIpsMode = "full" | "split" | "custom";

type QuotaUnit = "GB" | "TB";

interface FormState {
  name: string;
  email: string;
  note: string;
  allowedIpsMode: AllowedIpsMode;
  /** Only used when the mode is "custom". */
  allowedIpsCustom: string;
  dns: string;
  /** Empty or "0" means no limit. */
  quotaValue: string;
  quotaUnit: QuotaUnit;
  /** Megabits per second, as typed. Empty or "0" means no speed limit. */
  downMbps: string;
  upMbps: string;
  /**
   * yyyy-mm-ddThh:mm from the datetime-local input, read in the operator's own
   * time zone. Empty means it never expires.
   */
  expiresAt: string;
}

/** Which message slot a form field writes to. Server field names map onto these too. */
type ErrorField =
  "name" | "email" | "note" | "allowedIps" | "dns" | "quota" | "expiresAt" | "speed";

type FieldErrorMap = Partial<Record<ErrorField, string>>;

const SERVER_FIELDS: Readonly<Record<string, ErrorField>> = {
  name: "name",
  email: "email",
  note: "note",
  allowedIps: "allowedIps",
  allowed_ips: "allowedIps",
  dns: "dns",
  quota: "quota",
  quotaBytes: "quota",
  quota_bytes: "quota",
  expiresAt: "expiresAt",
  expires_at: "expiresAt",
  // Both directions share one message slot, because they share one field: the
  // two boxes sit side by side under a single label, and a message about either
  // of them belongs under both.
  downBps: "speed",
  down_bps: "speed",
  upBps: "speed",
  up_bps: "speed",
};

/**
 * Bits per second as the megabits the box shows, to as many decimals as it takes
 * to name the same number and no more.
 *
 * The same care the quota field takes, for the same reason: the value is turned
 * back into bits on save and compared with what the client already had, so a
 * rounded readout would make editing somebody's note quietly move their speed
 * limit. A limit set through the API in bits can be any number at all.
 */
function speedField(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) {
    return "";
  }
  return exactly(bps / MBIT, bps, MBIT);
}

/** What the box says, back in bits per second. 0 for empty, blank or nonsense. */
function speedBits(value: string): number {
  const typed = Number(value.trim());
  if (!value.trim() || !Number.isFinite(typed) || typed <= 0) {
    return 0;
  }
  return Math.round(typed * MBIT);
}

/** Split tunnel means "route the tunnel network and nothing else", which is the
 * network itself - the prefix is already part of it, whatever width it is. */
function splitTunnelValue(subnetCidr: string): string {
  return subnetCidr;
}

function normalize(value: string): string {
  return value.replace(/\s+/g, "");
}

function modeFor(allowedIps: string, subnetCidr: string): AllowedIpsMode {
  const value = normalize(allowedIps);
  if (value === FULL_TUNNEL || value === "0.0.0.0/0,::/0") {
    return "full";
  }
  if (subnetCidr && value === normalize(splitTunnelValue(subnetCidr))) {
    return "split";
  }
  return "custom";
}

/**
 * Bytes back into the largest unit that divides evenly, so 10 GB reads as 10 GB.
 *
 * Shown to as many decimals as it takes to name the same byte count and no
 * more - which is none of them for a limit this form set, and seven for one set
 * in binary before this form counted in decimal, or set through the API in
 * bytes. That is what keeps it lossless: the value is turned back into bytes on
 * save and compared with what the client already had, so a rounded readout of an
 * awkward number would make editing a client's name quietly move its data limit
 * by a few megabytes.
 */
function quotaFields(bytes: number): { quotaValue: string; quotaUnit: QuotaUnit } {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return { quotaValue: "", quotaUnit: "GB" };
  }
  if (bytes % TB === 0) {
    return { quotaValue: String(bytes / TB), quotaUnit: "TB" };
  }
  return { quotaValue: exactly(bytes / GB, bytes, GB), quotaUnit: "GB" };
}

/** The shortest way to write `value` that still means exactly `bytes`. */
function exactly(value: number, bytes: number, unit: number): string {
  for (let places = 0; places < BYTE_DECIMALS; places += 1) {
    const text = plain(value.toFixed(places));
    if (Math.round(Number(text) * unit) === bytes) {
      return text;
    }
  }
  return plain(value.toFixed(BYTE_DECIMALS));
}

/**
 * A fixed-point number with its trailing zeroes off: "10.00" is "10".
 *
 * Trimmed as text rather than by passing it through `Number`, which writes
 * anything under a millionth in exponential notation - so a limit of a few
 * hundred bytes, which only an API caller can set, would come back into the box
 * as "1e-9" rather than as a number.
 */
function plain(text: string): string {
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

/**
 * The limit with that many gigabytes added to it, always said in gigabytes.
 *
 * A limit already set in terabytes comes back in gigabytes rather than as a
 * fraction of one: none of the offsets divides a terabyte evenly, so 2 TB plus
 * 10 GB is either "2010 GB" or "2.01 TB", and the first is the one that can be
 * checked against the button that was pressed. The bytes are the same either way.
 *
 * Rounded to the byte because the sum is done in binary floating point, where
 * 1.1 + 1 can land a few digits past where either number ended. Nothing below a
 * byte is a limit, so nothing below a byte reaches the box.
 */
function addGigabytes(value: string, unit: QuotaUnit, gb: number): string {
  const typed = Number(value.trim());
  const base = Number.isFinite(typed) && typed > 0 ? typed : 0;
  const total = (unit === "TB" ? base * 1000 : base) + gb;
  return plain(total.toFixed(BYTE_DECIMALS));
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * What a `type="datetime-local"` input wants: the local calendar date and the
 * local clock to the minute, never a UTC one.
 */
function dateTimeValue(date: Date): string {
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${day}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseIso(iso: string | null): Date | null {
  if (!iso) {
    return null;
  }
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toDateTimeInput(iso: string | null): string {
  const date = parseIso(iso);
  return date ? dateTimeValue(date) : "";
}

/**
 * The box as the instant it describes, read in the operator's own time zone - so
 * an expiry set for six in the evening is six in the evening where it was typed,
 * whatever the server has its clock set to. It reaches the server as UTC through
 * `fromDateTimeInput` below, and is what the quick buttons extend from.
 *
 * Parsed field by field rather than handed to `new Date(value)`: a bare
 * "2027-01-01T18:30" counts as local time only because the specification was
 * changed to say so, and an expiry is not worth resting on which reading a
 * browser settled on. The parts say it unambiguously.
 *
 * Seconds are dropped rather than carried: the form shows minutes, so a stored
 * value with anything below that would be a promise the operator never made and
 * cannot see. The collector checks its deadlines on an interval anyway, so a
 * client goes off within a minute of its time either way.
 */
function parseDateTimeInput(value: string): Date | null {
  const [day, time] = value.split("T");
  if (!day || !time) {
    return null;
  }
  const [year, month, date] = day.split("-").map(Number);
  const [hours, minutes] = time.split(":").map(Number);
  if (
    !year ||
    !month ||
    !date ||
    !Number.isInteger(hours) ||
    !Number.isInteger(minutes) ||
    hours < 0 ||
    hours > 23 ||
    minutes < 0 ||
    minutes > 59
  ) {
    return null;
  }
  const moment = new Date(year, month - 1, date, hours, minutes, 0, 0);
  return Number.isNaN(moment.getTime()) ? null : moment;
}

function fromDateTimeInput(value: string): string | null {
  const moment = parseDateTimeInput(value);
  return moment === null ? null : moment.toISOString();
}

/**
 * The same clock time, that many calendar days later - not that many 24-hour
 * blocks. A week added across a daylight-saving change is still the same hour of
 * the day, which is what "+7d" says and what an operator selling a week means.
 * Month ends and leap days are the Date constructor's own arithmetic.
 */
function shiftDays(date: Date, days: number): Date {
  return new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate() + days,
    date.getHours(),
    date.getMinutes(),
    0,
    0,
  );
}

/**
 * Whether two timestamps are the same moment, rather than the same string. The
 * server spells an expiry "2027-01-01T00:00:00Z" and this form builds
 * "2027-01-01T00:00:00.000Z" for it; comparing the text would send an edit for
 * a field nobody touched.
 */
function sameInstant(left: string | null, right: string | null): boolean {
  const a = parseIso(left);
  const b = parseIso(right);
  if (a === null || b === null) {
    return a === b;
  }
  return a.getTime() === b.getTime();
}

function isAddress(value: string): boolean {
  if (value.includes(":")) {
    return /^[0-9a-fA-F:]{2,}$/.test(value);
  }
  const parts = value.split(".");
  return (
    parts.length === 4 &&
    parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) >= 0 && Number(part) <= 255)
  );
}

function isCidr(value: string): boolean {
  const [address, prefix, ...rest] = value.split("/");
  if (rest.length > 0) {
    return false;
  }
  if (prefix !== undefined) {
    const bits = Number(prefix);
    const max = address.includes(":") ? 128 : 32;
    if (!Number.isInteger(bits) || bits < 0 || bits > max) {
      return false;
    }
  }
  return isAddress(address);
}

function listParts(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

/* -------------------------------------------------------------------------- */
/* Field wrapper                                                               */
/* -------------------------------------------------------------------------- */

interface FieldProps {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
  /**
   * A fact about what is already there, said straight after the field's name:
   * what this client has used, against a limit about to be set for it. Quiet by
   * construction - it is context for the answer, not part of it.
   */
  aside?: string;
  className?: string;
  children: React.ReactNode;
}

function Field({
  id,
  label,
  hint,
  error,
  required,
  aside,
  className,
  children,
}: FieldProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className={cn("space-y-1.5", className)}>
      <Label htmlFor={id}>
        {label}
        {required ? (
          <>
            <span aria-hidden="true" className="text-destructive">
              {" *"}
            </span>
            <span className="sr-only">{` (${String(t("common.required"))})`}</span>
          </>
        ) : null}
        {aside === undefined ? null : (
          // Inside the label rather than in a row of its own beside it: a flex
          // row is a line box of its own height, and this field sat in a grid
          // next to one without an aside, so the two columns' inputs no longer
          // started on the same line. An inline span leaves the label the
          // height it has everywhere else.
          <span className="ms-2 text-xs font-normal leading-none text-muted-foreground tabular-nums">
            {aside}
          </span>
        )}
      </Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs leading-relaxed text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-xs font-medium text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function describedBy(id: string, hint: boolean, error: boolean): string | undefined {
  const parts = [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : undefined;
}

/* -------------------------------------------------------------------------- */
/* Form                                                                        */
/* -------------------------------------------------------------------------- */

interface ClientFormProps {
  /** null creates a new client; anything else edits that one. */
  client: Client | null;
  subnetCidr: string;
  /** Server-wide DNS, shown as the placeholder for the per-client override. */
  serverDns: string;
  /** What a new client routes through the tunnel unless the operator says otherwise. */
  defaultAllowedIps: string;
  onCreated: (client: Client) => void;
  onSaved: (client: Client) => void;
  onCancel: () => void;
}

function initialState(
  client: Client | null,
  subnetCidr: string,
  defaultAllowedIps: string,
): FormState {
  const allowedIps = client ? client.allowedIps : defaultAllowedIps || FULL_TUNNEL;
  const mode = modeFor(allowedIps, subnetCidr);
  return {
    // A new client opens with a name already drawn, rather than with an empty
    // box: the name is the client's identifier, somebody has to choose one, and
    // for most of the clients a panel ever adds nobody cares which. Typing over
    // it is the same one action as accepting it. Cleared, the server draws its
    // own - see `payload`.
    name: client?.name ?? randomName(),
    email: client?.email ?? "",
    note: client?.note ?? "",
    allowedIpsMode: mode,
    allowedIpsCustom: mode === "custom" ? allowedIps : "",
    dns: client?.dns ?? "",
    ...quotaFields(client?.quotaBytes ?? 0),
    downMbps: speedField(client?.downBps ?? 0),
    upMbps: speedField(client?.upBps ?? 0),
    expiresAt: toDateTimeInput(client?.expiresAt ?? null),
  };
}

function ClientForm({
  client,
  subnetCidr,
  serverDns,
  defaultAllowedIps,
  onCreated,
  onSaved,
  onCancel,
}: ClientFormProps): JSX.Element {
  const { t } = useTranslation();
  const create = useCreateClient();
  const update = useUpdateClient();
  // Whether this server shapes at all, and in which directions. Read here rather
  // than passed down because it is the same cached query the settings page
  // already made, and because the answer decides what this field is allowed to
  // offer: an upload limit on a server that shapes no upload would be refused
  // outright, and the whole section is meaningless while limits are switched off.
  const settings = useSettings();
  const shapingOn = settings.data?.shaperOn === "1";
  const uploadOn = shapingOn && settings.data?.shaperUpload === "1";
  // What a client added from here starts with, which is the same number the API
  // would apply if the field were left out - shown rather than implied, so the
  // operator can see it before saving and can clear it if they mean to.
  const defaultDown = shapingOn ? Number(settings.data?.shaperDefaultDownMbps ?? "0") || 0 : 0;
  const defaultUp = uploadOn ? Number(settings.data?.shaperDefaultUpMbps ?? "0") || 0 : 0;

  // Mounted by the dialog only while it is open, so the initialiser runs once
  // per opening. A 2 s stats poll re-rendering the page cannot reset the form.
  const [form, setForm] = React.useState<FormState>(() =>
    initialState(client, subnetCidr, defaultAllowedIps),
  );
  const [errors, setErrors] = React.useState<FieldErrorMap>({});
  const [formError, setFormError] = React.useState<string>("");
  /**
   * The suggestion currently in the name box, so a name the panel chose can be
   * told from one somebody meant. It stops holding the box's value the moment
   * the operator types, which is what makes "replace it and say so" safe to do
   * when the server refuses it as taken: the name being replaced is one nobody
   * chose. A ref rather than state - only the handlers below read it, and the
   * box's own value is what the form renders from.
   */
  const suggested = React.useRef(client === null ? form.name : "");

  const editing = client !== null;
  const pending = create.isPending || update.isPending;
  const splitValue = splitTunnelValue(subnetCidr);

  /**
   * Carry the server's default for new clients into the boxes, once.
   *
   * The settings query usually resolves after this form was built, so the
   * initialiser above cannot see the numbers - and the whole speed section is
   * hidden until it does, which means filling them in when they land shows the
   * section already carrying what this client is about to get rather than
   * changing under anybody's cursor. Only when adding, only into a box nobody
   * has typed in, and never again for the life of the dialog: an operator who
   * clears the box means no limit and must not have the default put back.
   */
  const prefilled = React.useRef(false);
  React.useEffect(() => {
    if (editing || prefilled.current || !settings.isSuccess) {
      return;
    }
    prefilled.current = true;
    if (defaultDown <= 0 && defaultUp <= 0) {
      return;
    }
    setForm((old) => ({
      ...old,
      downMbps: old.downMbps || (defaultDown > 0 ? String(defaultDown) : ""),
      upMbps: old.upMbps || (defaultUp > 0 ? String(defaultUp) : ""),
    }));
  }, [editing, settings.isSuccess, defaultDown, defaultUp]);

  const effectiveAllowedIps =
    form.allowedIpsMode === "full"
      ? FULL_TUNNEL
      : form.allowedIpsMode === "split"
        ? splitValue
        : form.allowedIpsCustom.trim();

  const quotaBytes = React.useMemo(() => {
    const value = Number(form.quotaValue.trim());
    if (!form.quotaValue.trim() || !Number.isFinite(value) || value <= 0) {
      return 0;
    }
    return Math.round(value * (form.quotaUnit === "TB" ? TB : GB));
  }, [form.quotaValue, form.quotaUnit]);

  const downBps = speedBits(form.downMbps);
  const upBps = uploadOn ? speedBits(form.upMbps) : 0;

  const expiryIso = fromDateTimeInput(form.expiresAt);
  const expiry = expiryParts(expiryIso);
  const expiryPast = expiry.tense === "expired";
  // How long that leaves, in the largest unit that still says something: an
  // expiry a fortnight out is "in 14d", one this afternoon is "in 3h".
  const countdown =
    expiry.msLeft < HOUR_MS
      ? String(t("time.inMinutes", { count: expiry.value }))
      : expiry.msLeft < DAY_MS
        ? String(t("time.inHours", { count: Math.floor(expiry.msLeft / HOUR_MS) }))
        : String(t("time.inDays", { count: expiry.daysLeft }));

  // A message about what the operator has just changed is stale by definition,
  // whether it came from here or from the API.
  function clearError(slot: ErrorField): void {
    setErrors((current) => {
      if (current[slot] === undefined) {
        return current;
      }
      const next = { ...current };
      delete next[slot];
      return next;
    });
  }

  function setField<K extends keyof FormState>(
    key: K,
    value: FormState[K],
    slot: ErrorField,
  ): void {
    setForm((current) => ({ ...current, [key]: value }));
    clearError(slot);
  }

  /**
   * The same idea on the data limit: each click adds its size to what is there,
   * so +10 GB three times is 30 GB and an empty box takes the size itself.
   */
  function addQuota(gb: number): void {
    setForm((current) => ({
      ...current,
      quotaValue: addGigabytes(current.quotaValue, current.quotaUnit, gb),
      quotaUnit: "GB",
    }));
    clearError("quota");
  }

  function clearQuota(): void {
    // The unit is left where the operator put it. It means nothing while the
    // box is empty, and changing it under them would be an edit they did not
    // make to a field they can see.
    setField("quotaValue", "", "quota");
  }

  /**
   * One control, so the day and the hour are one edit rather than two pickers
   * with a close in between.
   *
   * What used to be here as well was the half-filled case: a segmented native
   * box can hold "2027-__-01T18:30", reports itself as empty, and empty means
   * "never expires" - so the two had to be told apart by `badInput` and said out
   * loud under the field. A calendar cannot be half picked. The value is a whole
   * moment or it is absent, and the warning it needed went with the box.
   */
  function chooseExpiry(value: string): void {
    setField("expiresAt", value, "expiresAt");
  }

  /**
   * A quick offset: that many days on top of the date already in the box, at the
   * time of day it already holds. Clicks stack, so +30d twice is two months and
   * the four buttons between them reach any period.
   *
   * An empty box starts from now, and so does one whose date has already gone:
   * renewing a client that lapsed last month by extending its old date is how a
   * renewal lands in the past again, still expired, with nothing on screen
   * saying why. From now, +30d always means thirty days of service.
   */
  function addExpiry(days: number): void {
    setForm((current) => {
      const typed = parseDateTimeInput(current.expiresAt);
      const base = typed !== null && typed.getTime() > Date.now() ? typed : new Date();
      return { ...current, expiresAt: dateTimeValue(shiftDays(base, days)) };
    });
    clearError("expiresAt");
  }

  function clearExpiry(): void {
    setField("expiresAt", "", "expiresAt");
  }

  function chooseMode(mode: AllowedIpsMode): void {
    setForm((current) => ({
      ...current,
      allowedIpsMode: mode,
      // Switching to custom starts from what was in force, so the operator
      // edits a working value instead of an empty box.
      allowedIpsCustom:
        mode === "custom" && !current.allowedIpsCustom.trim()
          ? current.allowedIpsMode === "split"
            ? splitValue
            : FULL_TUNNEL
          : current.allowedIpsCustom,
    }));
    setErrors((current) => {
      if (current.allowedIps === undefined) {
        return current;
      }
      const next = { ...current };
      delete next.allowedIps;
      return next;
    });
  }

  function validate(): FieldErrorMap {
    const found: FieldErrorMap = {};
    const name = form.name.trim();

    // Empty is not a mistake here: it asks the server for a name, which is the
    // same name this form would have suggested and one it can guarantee is
    // free. Renaming, though, has nothing to fall back to - an edit that
    // cleared the box would be asking to make the client nameless.
    if (!name && editing) {
      found.name = String(t("validation.required"));
    } else if (name && !NAME_PATTERN.test(name)) {
      found.name = String(t("validation.name"));
    }

    if (form.email.trim().length > EMAIL_MAX) {
      found.email = String(t("validation.tooLong", { max: EMAIL_MAX }));
    }

    if (form.allowedIpsMode === "custom") {
      const parts = listParts(form.allowedIpsCustom);
      if (parts.length === 0) {
        found.allowedIps = String(t("validation.required"));
      } else if (!parts.every(isCidr)) {
        found.allowedIps = String(t("validation.cidr"));
      }
    }

    const dnsParts = listParts(form.dns);
    if (dnsParts.length > 0 && !dnsParts.every(isAddress)) {
      found.dns = String(t("validation.ipList"));
    }

    const quotaText = form.quotaValue.trim();
    if (quotaText) {
      const value = Number(quotaText);
      if (!Number.isFinite(value) || value < 0) {
        found.quota = String(t("validation.number"));
      }
    }

    for (const typed of [form.downMbps, form.upMbps]) {
      const text = typed.trim();
      if (text && (!Number.isFinite(Number(text)) || Number(text) < 0)) {
        found.speed = String(t("validation.number"));
      }
    }
    if (form.expiresAt && expiryIso === null) {
      found.expiresAt = String(t("validation.invalid"));
    }

    return found;
  }

  function showApiError(error: ApiError): void {
    const fields: FieldErrorMap = {};
    const unmapped: string[] = [];

    for (const [key, message] of Object.entries(error.errors)) {
      const slot = SERVER_FIELDS[key];
      if (slot) {
        fields[slot] = message;
      } else {
        unmapped.push(message);
      }
    }
    // A duplicate name comes back as a 409 with the explanation in the detail
    // and no field map, and the name box is where the operator has to look.
    // Only that 409, though: the other one says the address pool is full, which
    // is a fact about the server and reads as nonsense under the name box.
    if (error.nameInUse && fields.name === undefined) {
      fields.name = error.detail;
    }

    // Unless the refused name was one this form invented, in which case there
    // is nothing for the operator to decide: they never chose that word, and
    // being asked to pick another is being asked to clean up after the panel.
    // Another is drawn and the box says what happened. Not resubmitted on its
    // own - the operator presses the button, as they did the first time.
    //
    // An emptied box is not a suggestion, whatever the "" it and a spent
    // suggestion have in common: it asked the server to do the naming, and
    // answering that with a name in the box would undo the request.
    if (error.nameInUse && suggested.current && form.name.trim() === suggested.current) {
      shuffleName();
      fields.name = String(t("clients.nameCollision"));
      setErrors(fields);
      setFormError("");
      return;
    }

    setErrors(fields);
    setFormError([error.detail, ...unmapped].filter(Boolean).join(" "));
  }

  /** Draw another suggestion, and let the box go on knowing it is one. */
  function shuffleName(): void {
    const next = randomName();
    suggested.current = next;
    setField("name", next, "name");
  }

  function payload(): CreateClientInput {
    const name = form.name.trim();
    const input: CreateClientInput = {
      // Left out when the box is empty, which is how the server is asked to
      // name the client. Sending "" would be a name, and an illegal one.
      ...(name ? { name } : {}),
      allowedIps: effectiveAllowedIps,
      quotaBytes,
      downBps,
      upBps,
      expiresAt: expiryIso,
      email: form.email.trim(),
      note: form.note.trim(),
    };
    // An empty override would replace the server default with nothing, so it is
    // left out entirely instead.
    const dns = form.dns.trim();
    if (dns) {
      input.dns = dns;
    }
    return input;
  }

  /** Only what actually moved, so an edit cannot rewrite a field it never showed. */
  function changes(current: Client): UpdateClientInput {
    const next: UpdateClientInput = {};
    const name = form.name.trim();
    const email = form.email.trim();
    const note = form.note.trim();
    const dns = form.dns.trim();

    if (name !== current.name) {
      next.name = name;
    }
    if (normalize(effectiveAllowedIps) !== normalize(current.allowedIps)) {
      next.allowedIps = effectiveAllowedIps;
    }
    if (dns !== (current.dns ?? "").trim()) {
      next.dns = dns;
    }
    if (quotaBytes !== current.quotaBytes) {
      next.quotaBytes = quotaBytes;
    }
    if (downBps !== current.downBps) {
      next.downBps = downBps;
    }
    // Only when this server can carry one. On a server that shapes no upload the
    // box is not shown, so `upBps` is 0 here whatever the client is holding -
    // and sending that would clear a limit the operator was never offered the
    // chance to see, the moment they edited anything else about the client.
    if (uploadOn && upBps !== current.upBps) {
      next.upBps = upBps;
    }
    if (!sameInstant(expiryIso, current.expiresAt)) {
      next.expiresAt = expiryIso;
    }
    if (email !== current.email.trim()) {
      next.email = email;
    }
    if (note !== current.note.trim()) {
      next.note = note;
    }
    return next;
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const found = validate();
    if (Object.keys(found).length > 0) {
      setErrors(found);
      setFormError("");
      return;
    }
    setErrors({});
    setFormError("");

    if (client) {
      const diff = changes(client);
      if (Object.keys(diff).length === 0) {
        onCancel();
        return;
      }
      update.mutate(
        { name: client.name, changes: diff },
        { onSuccess: onSaved, onError: showApiError },
      );
      return;
    }
    create.mutate(payload(), { onSuccess: onCreated, onError: showApiError });
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-5">
      {formError ? (
        <div
          role="alert"
          className="flex gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-3"
        >
          <Warning
            weight="fill"
            className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
            aria-hidden="true"
          />
          <p className="text-sm leading-relaxed">{formError}</p>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="client-name"
          label={String(t("clients.name"))}
          // Editing says what a name is; adding says where the one in the box
          // came from and that nothing is committed to it yet.
          hint={String(editing ? t("clients.nameHint") : t("clients.nameHintSuggested"))}
          error={errors.name}
          required={editing}
        >
          <div className="flex gap-2">
            <Input
              id="client-name"
              value={form.name}
              onChange={(event) => {
                // Whatever is typed is the operator's, including a keystroke
                // that happens to leave the suggestion as it was: from here on
                // this is a name somebody chose.
                suggested.current = "";
                setField("name", event.target.value, "name");
              }}
              // The first character typed or pasted at an untouched suggestion
              // takes the place of all nine, so accepting one and typing
              // another stay the same single action - without the box having
              // to sit there highlighted from the moment the dialog opens.
              onKeyDown={(event) => {
                if (typedCharacter(event)) {
                  replaceSuggestion(event.currentTarget, suggested.current);
                }
              }}
              onPaste={(event) => replaceSuggestion(event.currentTarget, suggested.current)}
              autoComplete="off"
              maxLength={32}
              aria-invalid={errors.name !== undefined}
              aria-describedby={describedBy("client-name", true, errors.name !== undefined)}
            />
            {/*
              Only when adding. A rename is a decision about a device somebody
              owns, and a button that throws nine random characters at it is not
              a thing that dialog should offer.
            */}
            {editing ? null : (
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="shrink-0"
                title={String(t("clients.nameShuffle"))}
                aria-label={String(t("clients.nameShuffle"))}
                onClick={shuffleName}
              >
                <Shuffle aria-hidden="true" />
              </Button>
            )}
          </div>
        </Field>

        <Field
          id="client-email"
          label={`${String(t("clients.email"))} (${String(t("common.optional"))})`}
          hint={String(t("clients.emailHint"))}
          error={errors.email}
        >
          <Input
            id="client-email"
            type="email"
            value={form.email}
            onChange={(event) => setField("email", event.target.value, "email")}
            autoComplete="off"
            maxLength={EMAIL_MAX}
            aria-invalid={errors.email !== undefined}
            aria-describedby={describedBy("client-email", true, errors.email !== undefined)}
          />
        </Field>
      </div>

      <Field
        id="client-note"
        label={`${String(t("clients.note"))} (${String(t("common.optional"))})`}
        hint={String(t("clients.noteHint"))}
        error={errors.note}
      >
        <Textarea
          id="client-note"
          value={form.note}
          onChange={(event) => setField("note", event.target.value, "note")}
          rows={2}
          className="min-h-16"
          aria-invalid={errors.note !== undefined}
          aria-describedby={describedBy("client-note", true, errors.note !== undefined)}
        />
      </Field>

      <div className="rounded-lg border border-border bg-muted/30 p-3">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">{t("clients.ip")}</p>
        {editing && client ? (
          <p className="mt-1 font-mono text-sm">{client.ip}</p>
        ) : (
          <>
            <p className="mt-1 text-sm font-medium">{t("clients.ipAuto")}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t("clients.ipAutoHint", { subnet: subnetCidr })}
            </p>
          </>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="client-allowed-ips"
          label={String(t("clients.allowedIps"))}
          hint={String(t("clients.allowedIpsHint"))}
          error={errors.allowedIps}
        >
          <Select
            value={form.allowedIpsMode}
            onValueChange={(value) => chooseMode(value as AllowedIpsMode)}
          >
            <SelectTrigger
              id="client-allowed-ips"
              aria-invalid={errors.allowedIps !== undefined}
              aria-describedby={describedBy(
                "client-allowed-ips",
                true,
                errors.allowedIps !== undefined,
              )}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="full">{t("clients.allowedIpsFull")}</SelectItem>
              <SelectItem value="split">{t("clients.allowedIpsSplit")}</SelectItem>
              <SelectItem value="custom">{t("clients.allowedIpsCustom")}</SelectItem>
            </SelectContent>
          </Select>

          {form.allowedIpsMode === "custom" ? (
            <Input
              value={form.allowedIpsCustom}
              onChange={(event) => setField("allowedIpsCustom", event.target.value, "allowedIps")}
              placeholder={splitValue || FULL_TUNNEL}
              autoComplete="off"
              className="mt-2 font-mono text-xs"
              aria-label={String(t("clients.allowedIpsCustom"))}
              aria-invalid={errors.allowedIps !== undefined}
            />
          ) : (
            <p className="mt-2 font-mono text-xs text-muted-foreground">{effectiveAllowedIps}</p>
          )}
        </Field>

        <Field
          id="client-dns"
          label={`${String(t("clients.dns"))} (${String(t("common.optional"))})`}
          hint={String(t("clients.dnsHint"))}
          error={errors.dns}
        >
          <Input
            id="client-dns"
            value={form.dns}
            onChange={(event) => setField("dns", event.target.value, "dns")}
            placeholder={serverDns}
            autoComplete="off"
            className="font-mono text-xs"
            aria-invalid={errors.dns !== undefined}
            aria-describedby={describedBy("client-dns", true, errors.dns !== undefined)}
          />
        </Field>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="client-quota"
          label={String(t("clients.quota"))}
          // What this client has already moved, which is the fact the limit is
          // chosen against: set one below it and the client is switched off on
          // the next poll. A figure, and no more than that - the bar and the
          // percentage belong to the client list, where they are read at a
          // glance across every client; here they would dress up one number.
          aside={
            editing && client
              ? String(
                  t("clients.quotaUsedSoFar", {
                    used: formatBytes(client.rxBytes + client.txBytes),
                  }),
                )
              : undefined
          }
          hint={String(t("clients.quotaHint"))}
          error={errors.quota}
        >
          <div className="flex gap-2">
            <Input
              id="client-quota"
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={form.quotaValue}
              onChange={(event) => setField("quotaValue", event.target.value, "quota")}
              placeholder="0"
              className="tabular-nums"
              aria-invalid={errors.quota !== undefined}
              aria-describedby={describedBy("client-quota", true, errors.quota !== undefined)}
            />
            <Select
              value={form.quotaUnit}
              onValueChange={(value) => setField("quotaUnit", value as QuotaUnit, "quota")}
            >
              <SelectTrigger className="w-28" aria-label={String(t("clients.quotaUnit"))}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="GB">{t("units.gigabyte")}</SelectItem>
                <SelectItem value="TB">{t("units.terabyte")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div
            role="group"
            aria-label={String(t("clients.quotaQuick"))}
            className="mt-2 flex flex-wrap items-center gap-1.5"
          >
            {QUOTA_PRESET_GB.map((gb) => (
              <Button
                key={gb}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium"
                onClick={() => addQuota(gb)}
              >
                {t("units.plusGigabytes", { count: gb })}
              </Button>
            ))}
            {form.quotaValue.trim() ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium text-muted-foreground"
                onClick={clearQuota}
              >
                {t("common.clear")}
              </Button>
            ) : null}
          </div>

          {/*
            Only when there is no limit. A number that has just been typed does
            not need saying back - the box is right there - but an empty box
            says nothing about what empty means, and this does.
          */}
          {quotaBytes > 0 ? null : (
            <p className="mt-2 text-xs font-medium">{t("clients.quotaNone")}</p>
          )}
        </Field>

        <Field
          id="client-expiry"
          label={String(t("clients.expiry"))}
          hint={String(t("clients.expiryHint"))}
          error={errors.expiresAt}
        >
          <DatePicker
            id="client-expiry"
            // One control for both halves: the calendar keeps the clock under
            // it, so naming a moment is one visit to one field rather than a
            // date picker, a close, and a time picker.
            time
            value={form.expiresAt}
            onChange={chooseExpiry}
            placeholder={String(t("clients.expiryNone"))}
            // No ariaLabel: the field's own <label> names it, and a second name
            // here would replace "Expires" with something the operator cannot
            // see.
            invalid={errors.expiresAt !== undefined}
            describedBy={describedBy("client-expiry", true, errors.expiresAt !== undefined)}
          />

          <div
            role="group"
            aria-label={String(t("clients.expiryQuick"))}
            className="mt-2 flex flex-wrap items-center gap-1.5"
          >
            {EXPIRY_PRESET_DAYS.map((days) => (
              <Button
                key={days}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium"
                onClick={() => addExpiry(days)}
              >
                {t("time.plusDays", { count: days })}
              </Button>
            ))}
            {form.expiresAt ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium text-muted-foreground"
                onClick={clearExpiry}
              >
                {t("common.clear")}
              </Button>
            ) : null}
          </div>

          {expiryPast ? (
            <p className="mt-2 flex items-start gap-1.5 text-xs font-medium text-warning">
              <Warning weight="fill" className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              {t("clients.expiryPast")}
            </p>
          ) : expiryIso ? (
            // How long the chosen moment leaves, and only that. The date itself
            // is already on the control above, in the same words; what the
            // control cannot say is how far away it is.
            <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Clock weight="fill" className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="font-medium tabular-nums text-foreground">{countdown}</span>
            </p>
          ) : (
            <p className="mt-2 text-xs text-muted-foreground">{t("clients.expiryNone")}</p>
          )}
        </Field>
      </div>

      {/*
        Only on a server that can enforce one. A box that stores a number nothing
        acts on is worse than no box: the operator sets a limit, watches the
        client exceed it, and has nothing on screen to explain why. The hint says
        where to turn it on instead.
      */}
      {shapingOn ? (
        <Field
          id="client-speed"
          label={String(t("clients.speed"))}
          hint={String(
            editing
              ? t("clients.speedHint")
              : uploadOn && defaultUp > 0
                ? t("clients.speedHintDefaultBoth", { down: defaultDown, up: defaultUp })
                : defaultDown > 0
                  ? t("clients.speedHintDefaultDown", { mbps: defaultDown })
                  : t("clients.speedHint"),
          )}
          error={errors.speed}
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <span className="block text-xs text-muted-foreground">{t("clients.speedDown")}</span>
              <div className="flex items-center gap-2">
                <Input
                  id="client-speed"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="any"
                  value={form.downMbps}
                  onChange={(event) => setField("downMbps", event.target.value, "speed")}
                  placeholder="0"
                  className="w-28 tabular-nums"
                  aria-invalid={errors.speed !== undefined}
                  aria-describedby={describedBy("client-speed", true, errors.speed !== undefined)}
                />
                <span className="text-xs text-muted-foreground">
                  {t("units.megabitsPerSecond")}
                </span>
              </div>
            </div>

            {uploadOn ? (
              <div className="space-y-1">
                <span className="block text-xs text-muted-foreground">{t("clients.speedUp")}</span>
                <div className="flex items-center gap-2">
                  <Input
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="any"
                    value={form.upMbps}
                    onChange={(event) => setField("upMbps", event.target.value, "speed")}
                    placeholder="0"
                    className="w-28 tabular-nums"
                    aria-label={String(t("clients.speedUp"))}
                    aria-invalid={errors.speed !== undefined}
                  />
                  <span className="text-xs text-muted-foreground">
                    {t("units.megabitsPerSecond")}
                  </span>
                </div>
              </div>
            ) : null}
          </div>

          <div
            role="group"
            aria-label={String(t("clients.speedQuick"))}
            className="mt-2 flex flex-wrap items-center gap-1.5"
          >
            {SPEED_PRESET_MBPS.map((mbps) => (
              <Button
                key={mbps}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium"
                // Set, not added: a data allowance is a thing you buy more of and
                // a speed is a tier you pick, so two clicks on 50 still means 50.
                onClick={() => setField("downMbps", String(mbps), "speed")}
              >
                {t("units.megabits", { count: mbps })}
              </Button>
            ))}
            {form.downMbps.trim() || form.upMbps.trim() ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px] font-medium text-muted-foreground"
                onClick={() => {
                  setField("downMbps", "", "speed");
                  setField("upMbps", "", "speed");
                }}
              >
                {t("common.clear")}
              </Button>
            ) : null}
          </div>

          {/*
            As with the data limit: the boxes above are already labelled with
            their direction and their unit, so reading them back adds nothing.
            Both empty is the one state they do not describe.
          */}
          {downBps > 0 || upBps > 0 ? null : (
            <p className="mt-2 text-xs font-medium">{t("clients.speedNone")}</p>
          )}
        </Field>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" loading={pending}>
          {pending ? <Spinner size="sm" decorative /> : null}
          {editing ? t("common.save") : t("clients.createSubmit")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/* -------------------------------------------------------------------------- */
/* Dialog                                                                      */
/* -------------------------------------------------------------------------- */

export interface ClientDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * null opens the create form. Anything else names the client to edit - the
   * row is only read for its name here, and the client the form is built from is
   * fetched, because a row does not carry `dns`.
   *
   * Still a snapshot taken when the dialog opened rather than a live object: the
   * list re-renders every couple of seconds, and the title changing under an
   * operator mid-edit is the same fight it always was.
   */
  client: Client | null;
  subnetCidr: string;
  serverDns?: string;
  defaultAllowedIps?: string;
  /** The new client. The page opens its QR straight away. */
  onCreated: (client: Client) => void;
  onSaved: (client: Client) => void;
}

export function ClientDialog({
  open,
  onOpenChange,
  client,
  subnetCidr,
  serverDns = "",
  defaultAllowedIps = FULL_TUNNEL,
  onCreated,
  onSaved,
}: ClientDialogProps): JSX.Element {
  const { t } = useTranslation();
  const editing = client !== null;

  // The row the page handed over is a list row, and a list row has no `dns`:
  // the field is read out of the client's own config file, so the list stopped
  // carrying it rather than open one file per client on every poll. This form is
  // the only thing that ever wanted it, so it asks for the client it is about.
  //
  // Only while the dialog is open, and never for the create form, which has no
  // client to ask about.
  const detail = useClient(open && client ? client.name : null);
  // Not `detail.data ?? client`: falling back to the row would put a blank DNS
  // box in front of an operator whose client has one, which reads as "no DNS
  // override" and is a different statement from "not loaded yet".
  const loaded = editing ? (detail.data ?? null) : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Nothing inside is focused on opening. The first field is the name, and
        when a client is being added it already holds a suggestion - a caret
        parked in it, with the nine characters selected, asks for an answer to
        a question that has one.
      */}
      <DialogContent className="sm:max-w-2xl" focusSelf>
        <DialogHeader>
          <DialogTitle>
            {editing ? t("clients.editTitle", { name: client.name }) : t("clients.addTitle")}
          </DialogTitle>
          <DialogDescription>
            {editing ? t("clients.editSubtitle") : t("clients.addSubtitle")}
          </DialogDescription>
        </DialogHeader>

        {editing && detail.isPending ? (
          <div className="space-y-4 py-2" aria-busy="true">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-2/3" />
          </div>
        ) : editing && !loaded ? (
          <ErrorState variant="inline" error={detail.error} onRetry={() => void detail.refetch()} />
        ) : (
          <ClientForm
            // Remounting per client (and per opening, since Radix unmounts the
            // content when closed) is what keeps the form state honest without a
            // reset effect that a background poll could trip. The fetch above
            // resolves before this mounts, so the initial state is built once,
            // from the whole client, and nothing arrives later to fight it.
            key={loaded?.publicKey ?? "new"}
            client={loaded}
            subnetCidr={subnetCidr}
            serverDns={serverDns}
            defaultAllowedIps={defaultAllowedIps}
            onCreated={onCreated}
            onSaved={onSaved}
            onCancel={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
