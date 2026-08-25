import * as React from "react";
import { useTranslation } from "react-i18next";

import { useToast } from "@/components/ui/toast";
import { useSaveSettings, useSettings } from "@/api/hooks";
import { usePendingIndicator } from "@/lib/pending";
import { SETTINGS_DEFAULTS, type Settings as PanelSettings, type SettingsInput } from "@/api/types";

/*
 * The draft and the save behind the bandwidth card on the server page.
 *
 * It is a hook rather than state inside the card because the card is not what
 * saves it. The server page has one save bar along the bottom, it appears the
 * moment anything on the page is edited, and it is where an operator has
 * learned to look; a second Save of its own in the card head was a second
 * answer to a question the page had already answered. So the draft lives out
 * here, the page reads `dirty` off it alongside the server form's, and one bar
 * drives both.
 *
 * The two halves are still two saves. These five settings are stored in the
 * panel's database behind PUT settings, while everything above them is the
 * tunnel's config behind PUT server, and they are sent by whichever of them was
 * actually changed - a save with only a speed limit edited never touches the
 * server config, and cannot restart the interface.
 */

/** The settings this form owns. Nothing else is ever read or sent from here. */
export const BANDWIDTH_KEYS = [
  "shaperOn",
  "shaperDefaultDownMbps",
  "shaperDefaultUpMbps",
  "shaperUpload",
  "shaperWanIface",
] as const;

export type BandwidthKey = (typeof BANDWIDTH_KEYS)[number];
export type BandwidthValues = Pick<PanelSettings, BandwidthKey>;
export type BandwidthErrors = Partial<Record<BandwidthKey, string>>;

export interface BandwidthForm {
  /** The stored settings with the draft over the top of them. */
  values: BandwidthValues;
  errors: BandwidthErrors;
  /** Keys that differ from what is stored. Nothing else is ever sent. */
  changed: readonly BandwidthKey[];
  dirty: boolean;
  saving: boolean;
  /** The settings have not arrived yet, so the controls have nothing to show. */
  loading: boolean;
  change: (key: BandwidthKey, value: string) => void;
  discard: () => void;
  save: () => void;
}

/** A whole number of megabits. 0 is a value here - it is how "no limit" is spelled. */
function isMbps(value: string): boolean {
  const trimmed = value.trim();
  return /^\d+$/.test(trimmed) && Number(trimmed) <= 1_000_000;
}

export function useBandwidthForm(): BandwidthForm {
  const { t } = useTranslation();
  const { toast } = useToast();

  /** t() with an English original, so a key the catalog lacks never shows raw. */
  const text = React.useCallback(
    (key: string, fallback: string): string => String(t(key, { defaultValue: fallback })),
    [t],
  );

  const settings = useSettings();
  const save = useSaveSettings();
  /* Held open past the response: onSuccess drops the draft, and the bar the
     spinner lives in goes with it. See usePendingIndicator. */
  const saving = usePendingIndicator(save.isPending);

  const [draft, setDraft] = React.useState<Partial<BandwidthValues>>({});
  const [errors, setErrors] = React.useState<BandwidthErrors>({});

  const stored = settings.data ?? SETTINGS_DEFAULTS;
  const values = React.useMemo<BandwidthValues>(() => {
    const next = {} as Record<BandwidthKey, string>;
    for (const key of BANDWIDTH_KEYS) {
      next[key] = draft[key] ?? stored[key];
    }
    return next;
  }, [draft, stored]);

  const changed = React.useMemo(
    () => BANDWIDTH_KEYS.filter((key) => draft[key] !== undefined && draft[key] !== stored[key]),
    [draft, stored],
  );

  const change = React.useCallback((key: BandwidthKey, value: string): void => {
    setDraft((current) => ({ ...current, [key]: value }));
    // The old message described the old value; keeping it on screen while the
    // field changes underneath is worse than saying nothing.
    setErrors((current) => {
      if (!(key in current)) {
        return current;
      }
      const next = { ...current };
      delete next[key];
      return next;
    });
  }, []);

  const discard = React.useCallback((): void => {
    setDraft({});
    setErrors({});
  }, []);

  const runSave = React.useCallback((): void => {
    const problems: BandwidthErrors = {};
    for (const key of ["shaperDefaultDownMbps", "shaperDefaultUpMbps"] as const) {
      if (changed.includes(key) && !isMbps(values[key])) {
        problems[key] = text("validation.number", "Enter a whole number.");
      }
    }
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

    const payload = Object.fromEntries(changed.map((key) => [key, values[key]])) as SettingsInput;

    save.mutate(payload, {
      onSuccess: () => {
        setDraft({});
        setErrors({});
        /*
         * A plain "this is now true", because it now is: the API rebuilds the
         * shaping structure inside the same request, so by the time this lands
         * every client is being held to whatever the form said. The save used
         * to answer with a paragraph about queueing costs under the heading
         * "Saved, but not in effect", which described neither the save nor the
         * server and taught people to distrust a working button.
         */
        toast({
          title: text("settings.bandwidthSaved", "Bandwidth limits saved"),
          description: text("settings.savedBody", "The change is in effect."),
          variant: "success",
        });
      },
      onError: (error) => {
        const mapped: BandwidthErrors = {};
        for (const [field, message] of Object.entries(error.errors)) {
          if ((BANDWIDTH_KEYS as readonly string[]).includes(field)) {
            mapped[field as BandwidthKey] = message;
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
  }, [changed, save, text, toast, values]);

  return {
    values,
    errors,
    changed,
    dirty: changed.length > 0,
    saving,
    loading: settings.isPending,
    change,
    discard,
    save: runSave,
  };
}
