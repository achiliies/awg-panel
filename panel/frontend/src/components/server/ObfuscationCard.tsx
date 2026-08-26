import * as React from "react";
import { Info, MaskHappy, type Icon } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { GroupHeader } from "@/components/server/GroupHeader";
import { ParamField } from "@/components/server/ParamField";
import { ProfileDialog } from "@/components/server/ProfileDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useReconfigureObfuscation } from "@/api/hooks";
import type {
  ObfuscationProfile,
  ParamGroup as ParamGroupId,
  ParamPreviewResult,
  ParamSpec,
} from "@/api/types";

/*
 * Everything that disguises the traffic, in one card.
 *
 * These were four cards - junk, sizes, headers, imitation - and splitting them
 * up was the wrong unit. Nobody tunes the padding without also thinking about
 * the header ranges, they are saved together, they break every client config
 * together, and above all they are generated together: the whole set is drawn
 * per server, and a value from one group is only as private as the rest.
 *
 * The advanced group is the fifth section, and it was a card of its own until
 * the clients caught up. It sat below this one because it was a beta: header
 * protection, content padding and the timers all arrived in AmneziaWG 3.0, and
 * a peer that did not speak 3.0 failed silently, so the group started closed,
 * carried a warning on its face and had a generator and a Clear of its own. A
 * current client speaks 3.0. What is left is settings that belong to the same
 * server as the four sections above them, drawn from the same profile, saved by
 * the same button - so they are a section, and the page has one generator.
 *
 * The groups survive as headings inside one card rather than as five cards with
 * five separate save costs. They still come from the catalog, and their titles
 * and sentences still come from i18n by group id, so a group the API adds later
 * lands here with its own words and no change to this file.
 *
 * Reconfigure fills the form; it does not save. Applying costs every client a
 * re-import, which is not a thing to do on one click, and the save bar already
 * says so in the words it uses for every other change.
 */

const SUBSECTIONS: readonly ParamGroupId[] = ["junk", "sizes", "headers", "imitation", "advanced"];

export interface ObfuscationCardProps {
  /** The whole catalog. Only the five obfuscation groups are rendered. */
  specs: ParamSpec[];
  values: Readonly<Record<string, string>>;
  changed: readonly string[];
  errorFor: (key: string) => string | undefined;
  onChange: (key: string, value: string) => void;
  onRevert: (key: string) => void;
  /** Fills the form with a freshly drawn set. The page stays dirty until saved. */
  onReconfigure: (preview: ParamPreviewResult) => void;
  onReconfigureFailed: (message: string) => void;
  icon?: Icon;
  disabled?: boolean;
}

export function ObfuscationCard({
  specs,
  values,
  changed,
  errorFor,
  onChange,
  onRevert,
  onReconfigure,
  onReconfigureFailed,
  icon: HeadingIcon = MaskHappy,
  disabled = false,
}: ObfuscationCardProps): JSX.Element | null {
  const { t } = useTranslation();
  const reconfigure = useReconfigureObfuscation();

  const sections = React.useMemo(
    () =>
      SUBSECTIONS.map((group) => ({
        id: group,
        specs: specs.filter((spec) => spec.group === group),
      })).filter((section) => section.specs.length > 0),
    [specs],
  );

  const keys = React.useMemo(
    () => new Set(sections.flatMap((section) => section.specs.map((spec) => spec.key))),
    [sections],
  );
  const changedCount = changed.filter((key) => keys.has(key)).length;
  const errorCount = [...keys].filter((key) => errorFor(key)).length;

  if (sections.length === 0) {
    return null;
  }

  const run = (profile: ObfuscationProfile): void => {
    reconfigure.mutate(
      { profile },
      {
        onSuccess: onReconfigure,
        onError: (error) => onReconfigureFailed(error.detail),
      },
    );
  };

  return (
    <Card>
      <GroupHeader
        icon={HeadingIcon}
        title={t("server.obfuscation")}
        description={t("server.obfuscationHint")}
        actions={
          <>
            {errorCount > 0 ? (
              <Badge variant="destructive">
                {t("server.groupErrors", {
                  count: errorCount,
                  defaultValue: "{{count}} to fix",
                })}
              </Badge>
            ) : null}
            {changedCount > 0 ? (
              <Badge>
                {t("server.groupChanged", {
                  count: changedCount,
                  defaultValue: "{{count}} changed",
                })}
              </Badge>
            ) : null}

            <ProfileDialog
              title={String(t("server.reconfigureConfirm"))}
              description={String(t("server.reconfigureConfirmBody"))}
              triggerLabel={String(t("server.reconfigure"))}
              confirmLabel={String(t("server.reconfigure"))}
              pending={reconfigure.isPending}
              disabled={disabled}
              onConfirm={run}
            />

            <Popover>
              <PopoverTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground"
                  aria-label={String(t("server.reconfigureExplain"))}
                >
                  <Info aria-hidden="true" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-96">
                <div className="space-y-2.5 text-sm leading-relaxed">
                  <p className="font-medium">{t("server.reconfigureExplain")}</p>
                  <p className="text-muted-foreground">{t("server.reconfigureExplainBody")}</p>
                  <p className="text-muted-foreground">{t("server.reconfigureExplainCost")}</p>
                </div>
              </PopoverContent>
            </Popover>
          </>
        }
      />

      <CardContent className="space-y-8">
        {sections.map((section) => (
          <section key={section.id} className="space-y-4">
            <div className="space-y-1 border-b border-border pb-3">
              <h3 className="text-sm font-medium leading-tight">{t(`server.${section.id}`)}</h3>
              <p className="text-xs leading-relaxed text-muted-foreground">
                {t(`server.${section.id}Hint`)}
              </p>
            </div>
            <div className="grid gap-x-6 gap-y-6 sm:grid-cols-2">
              {section.specs.map((spec) => (
                <ParamField
                  key={spec.key}
                  spec={spec}
                  value={values[spec.key] ?? ""}
                  onChange={onChange}
                  onRevert={onRevert}
                  changed={changed.includes(spec.key)}
                  error={errorFor(spec.key)}
                  disabled={disabled}
                />
              ))}
            </div>
          </section>
        ))}
      </CardContent>
    </Card>
  );
}
