import { ArrowUUpLeft, Info } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { paramText } from "@/lib/paramText";
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
 * The badges are the part people act on:
 *   amber edge  - the setting arrived with AmneziaWG 3.0, so a peer that has
 *                 not got there yet negotiates without it and the handshake
 *                 fails with nothing said at either end. This is the only one
 *                 that can cost you a working client, and the only one that
 *                 gets any colour at all. Amber rather than red because the
 *                 setting is not broken, it is early: the same value works
 *                 perfectly once the clients catch up. The chip is outlined
 *                 rather than filled, because every field in the AmneziaWG 3.0
 *                 group carries it - seven solid amber chips down one card
 *                 shout at an admin who has already read the first one, and
 *                 they drown out the beta chip on the heading, which is the
 *                 one thing there saying it about the group as a whole;
 *   small grey  - either the client has to carry the same value, or the value
 *                 never leaves the server. Both are facts about the parameter,
 *                 true of most of the page, and neither is a problem. Colour
 *                 them and the page reads as a wall of warnings with the one
 *                 real warning lost in it, so they are quiet chips and the
 *                 consequence is spelled out in the info popover instead.
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

/** The badge row. Must-match and amber can both apply; grey only when neither does. */
function ParamBadges({ spec }: { spec: ParamSpec }): JSX.Element {
  const { t } = useTranslation();

  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {spec.mustMatchClient ? (
        <Badge variant="muted" size="sm">
          {t("server.mustMatch")}
        </Badge>
      ) : null}
      {!spec.importerSafe ? (
        <Badge variant="caution" size="sm">
          {t("server.importerUnsafe")}
        </Badge>
      ) : null}
      {spec.mustMatchClient || !spec.importerSafe ? null : (
        <Badge variant="muted" size="sm">
          {t("server.serverOnly")}
        </Badge>
      )}
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
    notes.push(String(t("server.rangeHint")));
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
