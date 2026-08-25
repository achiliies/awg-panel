import type { TFunction } from "i18next";

import type { ApiGroup, ApiOperation } from "@/api/types";

/*
 * The words an endpoint is described with, in the language the admin is reading.
 *
 * The same arrangement the parameter catalog uses, and for the same reason. GET
 * openapi.json carries one English summary and one English description per
 * operation, written in apps/panel/apidocs.py beside the entry that names the
 * route, so the description and the thing described cannot drift apart. That is
 * the right place for them and they stay there: the panel picks its language in
 * the browser and never tells the server which one, so the API has no language
 * to answer in.
 *
 * So the document supplies the English and the UI catalogs override it per
 * language, keyed by operation id - `clientList`, `serverUpdate`. An id with no
 * entry falls back to what the API sent, which is what keeps the promise the
 * document makes: an endpoint added to the catalog appears here with its own
 * English and no change to the frontend, translated later or never.
 */

export interface ApiOperationText {
  summary: string;
  description: string;
}

export function operationText(t: TFunction, operation: ApiOperation): ApiOperationText {
  return {
    summary: String(t(`api.op.${operation.id}.summary`, { defaultValue: operation.summary })),
    description: String(
      t(`api.op.${operation.id}.description`, { defaultValue: operation.description }),
    ),
  };
}

/** The same, for a group heading. Keyed by tag with the spaces taken out. */
export function groupText(t: TFunction, group: ApiGroup): { name: string; description: string } {
  const key = group.name.replace(/\s+/g, "");
  return {
    name: String(t(`api.group.${key}.name`, { defaultValue: group.name })),
    description: String(t(`api.group.${key}.description`, { defaultValue: group.description })),
  };
}
