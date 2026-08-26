import { ArrowUUpLeft, Info } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { paramText, rangeBounds } from "@/lib/paramText";
import { cn } from "@/lib/utils";
import type { ParamSpec } from "@/api/types";

/*
 * One row of GET server/params, rendered as something an admin can actually
 * reason about.
 *
 * Every word here comes from the catalog: label, one-line help, the paragraph
 * behind the info button and the bounds on the number input. This file knows
 * about `kind` and about the three badges, and about nothing else - a new
 * parameter appears with no change to the UI, and a renamed one cannot leave a
 * stale description behind. The prose goes through paramText on the way, which
 * is where a translation of it replaces the English the API sent.
 *
 * What the catalog recommends is deliberately not rendered. A "Recommended: X"
 * line under every field is a wall of numbers that says nothing about why, and
 * the preset picker at the top of the page already applies a whole coherent
 * set; the reasoning lives in the info popover and in docs/PANEL.md.
 *
 * The badges say what kind of parameter this is: either the client has to carry
 * the same value, or the value never leaves the server. Both are facts, true of
 * most of the page, and neither is a problem - so they are quiet grey chips and
 * the consequence is spelled out in the info popover instead. Colour them and
 * the page reads as a wall of warnings with the one real warning lost in it.
 *
 * There was a third, in amber: "Needs AmneziaWG 3.0+", on every field of the
 * advanced group. It was the truest badge on the page while the clients were
 * behind - a peer without the release fails silently, which is the one thing
 * here that can cost a working client. The clients caught up. What is left is
 * eight amber chips down one card, naming a version that is now simply what an
 * AmneziaWG client is, and a page that shouts everywhere cannot shout anywhere.
 * Which release a setting arrived in is still in the popover, on the field it
 * belongs to, because 3.1 is recent enough that random packet trailers are a
 * decision rather than a default.
 */

/** Kinds whose value is long enough that half a row would truncate it. */
const WIDE_KINDS: ReadonlySet<string> = new Set(["imitation", "key", "text", "iplist"]);

/** Kinds typed as free text where a monospace font makes the value readable. */
const MONO_KINDS: ReadonlySet<string> = new Set(["imitation", "key"]);

/** Kinds that are whole numbers, so the browser can offer a numeric keyboard. */
const NUMERIC_KINDS: ReadonlySet<string> = new Set(["int", "port"]);

/** Kinds rendered as a switch rather than a text input. */
const BOOL_KINDS: ReadonlySet<string> = new Set(["bool"]);

export interface ParamFieldProps {
  spec: ParamSpec;
  /** Current value as it would be written to the config; "" means unset. */
  value: string;
  onChange: (key: string, value: string) => void;
  /** Message for this field, from the API or from the form's own check. */
  error?: string;
  /** The value differs from what is on the server right now. */
  changed?: boolean;
  /** Put the value back to what the server has. Only used when `changed`. */
  onRevert?: (key: string) => void;
  disabled?: boolean;
  className?: string;
}

export function ParamField({
  spec,
  value,
  onChange,
  error,
  changed = false,
  onRevert,
  disabled = false,
  className,
}: ParamFieldProps): JSX.Element {
  const { t } = useTranslation();
  const text = paramText(t, spec);

  const id = `param-${spec.key}`;
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const unsupported = spec.supported === false;
  const locked = disabled || unsupported;
  const numeric = NUMERIC_KINDS.has(spec.kind);
  const wide = WIDE_KINDS.has(spec.kind);
  const bool = BOOL_KINDS.has(spec.kind);

  const describedBy = [helpId, error ? errorId : null].filter(Boolean).join(" ");

  return (
    <div className={cn("min-w-0 space-y-2", wide && "sm:col-span-2", className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <Label htmlFor={id} className="leading-snug">
          {text.label}
        </Label>
        {/* The config key itself: this is the line an admin greps for in
            awg0.conf, and every guide on the internet names it. */}
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] leading-none text-muted-foreground">
          {spec.key}
        </code>

        <Popover>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground hover:text-foreground"
              aria-label={String(
                t("server.explain", { label: text.label, defaultValue: "Explain {{label}}" }),
              )}
            >
              <Info aria-hidden="true" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-80">
            <ParamHelp spec={spec} />
          </PopoverContent>
        </Popover>

        {changed ? (
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-primary" />
            <span className="sr-only">
              {t("server.fieldChanged", { defaultValue: "Changed, not saved yet" })}
            </span>
            {onRevert ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-foreground"
                onClick={() => onRevert(spec.key)}
                aria-label={String(t("server.revertField", { defaultValue: "Undo this change" }))}
              >
                <ArrowUUpLeft aria-hidden="true" />
              </Button>
            ) : null}
          </span>
        ) : null}

        <ParamBadges spec={spec} />
      </div>

      {bool ? (
        <div className="flex h-9 items-center gap-2">
          <Switch
            id={id}
            checked={value === "on"}
            onCheckedChange={(checked) => onChange(spec.key, checked ? "on" : "")}
            disabled={locked}
            aria-invalid={error ? true : undefined}
            aria-describedby={describedBy || undefined}
          />
          <span className="text-xs text-muted-foreground">
            {value === "on" ? t("common.on") : t("common.off")}
          </span>
        </div>
      ) : (
        <Input
          id={id}
          value={value}
          onChange={(event) => onChange(spec.key, event.target.value)}
          disabled={locked}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy || undefined}
          // The catalog carries the bounds, so the browser enforces exactly the
          // same numbers the server does.
          type={numeric ? "number" : "text"}
          inputMode={numeric ? "numeric" : undefined}
          min={numeric && spec.min !== null ? spec.min : undefined}
          max={numeric && spec.max !== null ? spec.max : undefined}
          step={numeric ? 1 : undefined}
          placeholder={spec.default ?? undefined}
          spellCheck={MONO_KINDS.has(spec.kind) ? false : undefined}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          className={cn(MONO_KINDS.has(spec.kind) && "font-mono text-xs")}
        />
      )}

      <p id={helpId} className="text-xs leading-relaxed text-muted-foreground">
        {text.helpShort}
      </p>

      {error ? (
        <p id={errorId} className="text-xs font-medium leading-relaxed text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The badge row.
 *
 * "Server only" is not the complement of "must match": it says the value never
 * leaves this machine, which is true of DisableCookies and not of
 * MaxHandshakeAttempts - that one is each end's own retry behaviour, so the two
 * do not have to agree and a client still has one of its own. The importer flag
 * is what separates them, the same way it does in the popover below, so a
 * parameter that is neither gets no chip rather than a wrong one.
 */
function ParamBadges({ spec }: { spec: ParamSpec }): JSX.Element {
  const { t } = useTranslation();

  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {spec.mustMatchClient ? (
        <Badge variant="muted" size="sm">
          {t("server.mustMatch")}
        </Badge>
      ) : null}
      {!spec.mustMatchClient && spec.importerSafe ? (
        <Badge variant="muted" size="sm">
          {t("server.serverOnly")}
        </Badge>
      ) : null}
      {spec.supported === false ? (
        <Badge variant="outline" size="sm">
          {t("server.unsupported")}
        </Badge>
      ) : null}
    </span>
  );
}

/**
 * The info popover: the full explanation, then the sentence behind each badge.
 * People open this exactly when they are about to change something, so what
 * breaks if it is wrong belongs here rather than in a manual.
 */
function ParamHelp({ spec }: { spec: ParamSpec }): JSX.Element {
  const { t } = useTranslation();
  const text = paramText(t, spec);

  const notes: string[] = [];
  if (spec.kind === "range") {
    // The example is the field's own bounds rather than a fixed pair, because
    // one hint is shown on fields whose maximums are three orders of magnitude
    // apart: a "range like 10-500" under RekeyTimeout names a number that same
    // field then rejects, its maximum being 60. This is the mirror of what
    // _check_range quotes in its malformed-value message.
    notes.push(String(t("server.rangeHint", rangeBounds(spec))));
  }
  if (spec.kind === "imitation") {
    notes.push(String(t("server.imitationHelp")));
  }
  if (spec.mustMatchClient) {
    notes.push(String(t("server.mustMatchHint")));
  }
  if (!spec.importerSafe) {
    notes.push(String(t("server.importerUnsafeBody")));
  }
  if (!spec.mustMatchClient && spec.importerSafe) {
    notes.push(String(t("server.serverOnlyHint")));
  }
  if (spec.supported === false) {
    notes.push(String(t("server.unsupportedHint")));
  }

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-semibold leading-tight">{text.label}</p>
        <p className="mt-1 font-mono text-[11px] text-muted-foreground">{spec.key}</p>
      </div>

      <p className="text-sm leading-relaxed text-muted-foreground">{text.helpLong}</p>

      {notes.length > 0 ? (
        <ul className="space-y-2 border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}

      {/* The value the installer writes, and nothing more. What a good value
          would be is a paragraph, not a chip, so it lives in help_long and in
          docs/PANEL.md rather than as a second number beside every field. */}
      {spec.default ? (
        <div className="border-t border-border pt-3 text-xs text-muted-foreground">
          <p className="break-words">{t("server.defaultValue", { value: spec.default })}</p>
        </div>
      ) : null}
    </div>
  );
}
