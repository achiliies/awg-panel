import type { TFunction } from "i18next";

import type { Settings as PanelSettings } from "@/api/types";
import { LANGUAGES } from "@/i18n";
import { formatDuration } from "@/lib/utils";

/*
 * What each panel setting is called on screen.
 *
 * The API names them in camelCase - `shaperOn`, `webBasePath` - and that is the
 * right name in a request and in the docs, where it is the word the caller
 * types. It is the wrong name everywhere an admin reads it. Nobody has seen
 * `shaperOn`: they moved a switch labelled "Speed limits", and a line naming the
 * key asks them to translate back through the API before they can tell what the
 * line is about. `shaperOn` is worse than merely unfamiliar, because it reads as
 * a statement - a log saying "shaperOn" beside the moment somebody switched
 * shaping off is telling them the opposite of what happened.
 *
 * So one map, and it lives here rather than beside either of the two places that
 * read it: the settings page names the fields waiting to be saved, and the
 * activity log names the fields a save moved. Those are the same list a moment
 * apart, and in different words they read as being about different things.
 *
 * Keyed by `keyof Settings`, so a setting added to the API and to the type
 * cannot arrive here without a label.
 */
export const SETTING_LABELS: Readonly<Record<keyof PanelSettings, string>> = {
  webListen: "settings.listen",
  webPort: "settings.port",
  webBasePath: "settings.basePath",
  tlsCertPath: "settings.tlsCert",
  tlsKeyPath: "settings.tlsKey",
  configEndpointMode: "settings.configEndpoint",
  configEndpointHost: "settings.configEndpointHost",
  sessionMaxAge: "settings.sessionLength",
  loginRateLimit: "settings.loginRateLimit",
  theme: "settings.theme",
  language: "settings.language",
  trafficPollSec: "settings.pollInterval",
  onlineThresholdSec: "settings.onlineThreshold",
  enforceIntervalSec: "settings.enforceInterval",
  shaperOn: "settings.shapingOn",
  shaperDefaultDownMbps: "settings.defaultDown",
  shaperDefaultUpMbps: "settings.defaultUp",
  shaperUpload: "settings.uploadShaping",
  shaperWanIface: "settings.wanIface",
};

/**
 * One setting's name, or the API's own spelling where this build has no label.
 *
 * The fallback is for a log written by a newer panel than the one drawing it,
 * which is the same case the event catalog already handles: a key with no entry
 * here is still the word that appears in the request that changed it, which is
 * a worse name than the label and a much better one than a gap in the sentence.
 */
export function settingLabel(t: TFunction, key: string): string {
  const label = SETTING_LABELS[key as keyof PanelSettings];
  return label ? String(t(label)) : key;
}

/** Settings stored as "1" and "0", which are read as a switch rather than a number. */
const BOOLEAN_KEYS: ReadonlySet<string> = new Set(["shaperOn", "shaperUpload"]);

/** Settings stored as a count of seconds, which nobody counts past a hundred of. */
const SECONDS_KEYS: ReadonlySet<string> = new Set([
  "sessionMaxAge",
  "trafficPollSec",
  "onlineThresholdSec",
  "enforceIntervalSec",
]);

/** Settings stored as whole megabits per second, where 0 is how "no limit" is spelled. */
const MEGABIT_KEYS: ReadonlySet<string> = new Set(["shaperDefaultDownMbps", "shaperDefaultUpMbps"]);

/** Settings stored as one of a fixed set of words, each with a phrase of its own. */
const CHOICE_LABELS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  theme: { system: "theme.system", light: "theme.light", dark: "theme.dark" },
  configEndpointMode: {
    ip: "settings.configEndpointIp",
    domain: "settings.configEndpointDomain",
  },
};

/**
 * What a setting was set to, said the way the page that sets it says it.
 *
 * Every setting is stored as text, and half of them are text an admin never
 * sees: `86400` is what the box offering "1 day" writes, `1` is what a switch
 * writes, and `0` on a speed limit means the opposite of nothing - it means no
 * limit at all. A log quoting the stored value would be accurate and would
 * still need decoding, which is the thing this whole catalog exists to avoid.
 *
 * A key with no rule of its own is quoted as it is stored, which is right for
 * the ones that are already a plain answer - an interface name, a count of
 * failed sign-ins - and is the safe way for a value this build has never heard
 * of to arrive on screen.
 */
export function settingValue(t: TFunction, key: string, raw: string): string {
  if (BOOLEAN_KEYS.has(key)) {
    return String(t(raw === "1" ? "common.on" : "common.off"));
  }
  if (SECONDS_KEYS.has(key)) {
    const seconds = Number(raw);
    return Number.isFinite(seconds) && seconds > 0 ? formatDuration(seconds) : raw;
  }
  if (MEGABIT_KEYS.has(key)) {
    const mbps = Number(raw);
    if (!Number.isFinite(mbps) || mbps <= 0) {
      return String(t("common.unlimited"));
    }
    return `${mbps} ${String(t("units.megabitsPerSecond"))}`;
  }
  const choice = CHOICE_LABELS[key]?.[raw];
  if (choice) {
    return String(t(choice));
  }
  if (key === "language") {
    return LANGUAGES.find((entry) => entry.code === raw)?.nativeName ?? raw;
  }
  // Empty is a value here rather than a gap: it is how the settings page spells
  // "work the interface out from the default route".
  if (key === "shaperWanIface" && raw === "") {
    return String(t("settings.wanIfaceAuto"));
  }
  return raw;
}
