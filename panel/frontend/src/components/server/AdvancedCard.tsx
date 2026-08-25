import * as React from "react";
import { Eraser, SlidersHorizontal, type Icon } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { ParamField } from "@/components/server/ParamField";
import { ParamGroup } from "@/components/server/ParamGroup";
import { ProfileDialog } from "@/components/server/ProfileDialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useReconfigureObfuscation } from "@/api/hooks";
import type { ObfuscationProfile, ParamPreviewResult, ParamSpec } from "@/api/types";

/*
 * The AmneziaWG 3.0 group: header protection, content padding and the timers.
 *
 * Everything above this card works on every client. Everything in it needs 3.0
 * at the far end, and a peer that is not there fails silently - it negotiates
 * without the setting, the server refuses it, and neither end says why. That is
 * what the badge and the note are for, and why the card starts closed.
 *
 * It had no generator until now, which left the strongest settings the server
 * offers as seven empty boxes to be filled in from the protocol specification.
 * That is the same mistake a fixed obfuscation profile would be: a value
 * everybody copies out of one document is a constant, not a setting, and the
 * timers say precisely how often this server handshakes. So the group is drawn
 * as one consistent set - the timers interlock, and a header protection key is
 * only worth anything if it was never anybody else's.
 *
 * Beside it is the way back. Turning the group off means clearing all seven,
 * and doing that one field at a time is how an admin ends up with a half-set
 * group that breaks 2.x clients for no benefit at all.
 */

/** The whole group, cleared in one go. Empty is how the save removes a line. */
function cleared(specs: ParamSpec[]): Record<string, string> {
  return Object.fromEntries(specs.map((spec) => [spec.key, ""]));
}

export interface AdvancedCardProps {
  /** The whole catalog. Only the advanced group is rendered. */
  specs: ParamSpec[];
  values: Readonly<Record<string, string>>;
  changed: readonly string[];
  errorFor: (key: string) => string | undefined;
  onChange: (key: string, value: string) => void;
  onRevert: (key: string) => void;
  /** Fills the form with a freshly drawn set. The page stays dirty until saved. */
  onGenerate: (preview: ParamPreviewResult) => void;
  onGenerateFailed: (message: string) => void;
  /** Empties every field in the group, ready to save. */
  onClear: (values: Record<string, string>) => void;
  icon?: Icon;
  disabled?: boolean;
}

export function AdvancedCard({
  specs,
  values,
  changed,
  errorFor,
  onChange,
  onRevert,
  onGenerate,
  onGenerateFailed,
  onClear,
  icon = SlidersHorizontal,
  disabled = false,
}: AdvancedCardProps): JSX.Element | null {
  const { t } = useTranslation();
  const generate = useReconfigureObfuscation();

  const group = React.useMemo(() => specs.filter((spec) => spec.group === "advanced"), [specs]);
  const keys = React.useMemo(() => new Set(group.map((spec) => spec.key)), [group]);
  const changedCount = changed.filter((key) => keys.has(key)).length;
  const errorCount = group.filter((spec) => errorFor(spec.key)).length;
  const anySet = group.some((spec) => (values[spec.key] ?? "").trim() !== "");

  if (group.length === 0) {
    return null;
  }

  const run = (profile: ObfuscationProfile): void => {
    generate.mutate(
      { profile, scope: "advanced" },
      {
        onSuccess: onGenerate,
        onError: (error) => onGenerateFailed(error.detail),
      },
    );
  };

  return (
    <ParamGroup
      group="advanced"
      icon={icon}
      badge={
        <Badge variant="warning" size="sm">
          {t("server.beta")}
        </Badge>
      }
      collapsible
      defaultOpen={anySet}
      changedCount={changedCount}
      errorCount={errorCount}
      noteTone="warning"
      note={<BetaNote />}
      actions={
        <>
          <ProfileDialog
            title={String(t("server.advancedGenerateConfirm"))}
            description={String(t("server.advancedGenerateConfirmBody"))}
            triggerLabel={String(t("server.advancedGenerate"))}
            confirmLabel={String(t("server.advancedGenerate"))}
            pending={generate.isPending}
            disabled={disabled}
            onConfirm={run}
          />

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button type="button" variant="ghost" size="sm" disabled={disabled || !anySet}>
                <Eraser aria-hidden="true" />
                {t("server.advancedClear")}
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>{t("server.advancedClearConfirm")}</AlertDialogTitle>
                <AlertDialogDescription>
                  {t("server.advancedClearConfirmBody")}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
                <AlertDialogAction onClick={() => onClear(cleared(group))}>
                  {t("server.advancedClear")}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      }
    >
      {group.map((spec) => (
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
    </ParamGroup>
  );
}

/**
 * What "beta" means for this group, in the card rather than in a manual.
 *
 * The badge on the heading is the warning; this is the part that stops it being
 * a shrug. Three things an admin needs before touching anything below it: these
 * are AmneziaWG 3.0 settings, a peer that is not on 3.0 fails without saying
 * why, and leaving the whole group empty costs nothing - the obfuscation above
 * is what does the hiding, and it works on every client.
 *
 * Its first line is set in the page's own text colour rather than in amber. The
 * amber box it sits in already says which kind of note this is, and the line
 * was one more shade of the same warning in a card that had three of them
 * competing - the chip on the heading, this, and a chip on every field.
 */
function BetaNote(): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="space-y-2">
      <p className="font-medium text-foreground">{t("server.advancedBeta")}</p>
      <p>{t("server.advancedBetaBody")}</p>
      <p>{t("server.advancedBetaSafe")}</p>
    </div>
  );
}
