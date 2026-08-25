import type { TFunction } from "i18next";

import type { ParamSpec } from "@/api/types";

/*
 * The words a parameter is described with, in the language the admin is reading.
 *
 * GET server/params carries one English label and two English explanations per
 * setting, written beside the validation rules in awg/validate.py so a rule and
 * the sentence explaining it cannot drift apart. That is the right place for
 * them and they stay there: the panel picks its language in the browser and
 * never tells the server which one, so the API has no language to answer in.
 *
 * So the catalog supplies the rules and the English, and the UI catalogs
 * override the prose per language, keyed by the config key itself - `Jc`,
 * `S1`, `HeaderProtectionKey`. A key with no entry falls back to what the API
 * sent, which is what keeps the promise the API makes: a parameter added to
 * validate.py shows up here with its own English and no change to the frontend,
 * translated later or never.
 */

export interface ParamText {
  label: string;
  helpShort: string;
  helpLong: string;
}

export function paramText(t: TFunction, spec: ParamSpec): ParamText {
  return {
    label: String(t(`params.${spec.key}.label`, { defaultValue: spec.label })),
    helpShort: String(t(`params.${spec.key}.helpShort`, { defaultValue: spec.helpShort })),
    helpLong: String(t(`params.${spec.key}.helpLong`, { defaultValue: spec.helpLong })),
  };
}
