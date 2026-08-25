import * as React from "react";
import { ArrowsClockwise } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import type { ObfuscationProfile } from "@/api/types";

/*
 * Pick a profile, then draw a set from it.
 *
 * The generator used to have one setting and no choice, which quietly decided
 * a trade on the operator's behalf: how much of the protocol's shape to hide,
 * against what sending it costs. The four options here are that trade made
 * visible, and each one says what it spends rather than only what it buys - an
 * admin on a metered mobile link and an admin behind a filter that is actively
 * classifying traffic do not want the same draw.
 *
 * Random is not a fourth flavour. Three published bands are three things to
 * test for, so it spans all three and makes the choice itself unremarkable.
 *
 * Standard is marked as the recommendation rather than merely arriving
 * selected. A pre-ticked radio reads as "where the list happens to start", so
 * an admin with no reason to prefer one band is left comparing four paragraphs
 * about trades they cannot measure yet. The chip says which one the project
 * stands behind, and the other three stay one click away for whoever knows
 * their link is metered or their network is classifying traffic.
 *
 * It sits at the far end of its row rather than against the name, so the four
 * names start and end alike and the chip is the only thing breaking the column;
 * beside the name it read as part of the title, which is where a qualifier like
 * "Standard (recommended)" would go and is not what this is.
 *
 * Nothing is saved here. The values land in the form and are saved like any
 * other edit, because applying them costs every client a re-import and that is
 * not a thing to do on one click.
 */

const PROFILES: readonly ObfuscationProfile[] = ["standard", "dpi", "fast", "random"];

/** Both the default selection and the chip, so the two cannot drift apart. */
const RECOMMENDED: ObfuscationProfile = "standard";

export interface ProfileDialogProps {
  /** Words for this generator. The advanced group spends different things. */
  title: string;
  description: string;
  /** The button that opens it, and the one that confirms. */
  triggerLabel: string;
  confirmLabel: string;
  pending: boolean;
  disabled?: boolean;
  onConfirm: (profile: ObfuscationProfile) => void;
}

export function ProfileDialog({
  title,
  description,
  triggerLabel,
  confirmLabel,
  pending,
  disabled = false,
  onConfirm,
}: ProfileDialogProps): JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [profile, setProfile] = React.useState<ObfuscationProfile>(RECOMMENDED);
  const name = React.useId();

  const run = (): void => {
    setOpen(false);
    onConfirm(profile);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="secondary" size="sm" disabled={disabled} loading={pending}>
          {pending ? <Spinner aria-hidden="true" /> : <ArrowsClockwise aria-hidden="true" />}
          {triggerLabel}
        </Button>
      </DialogTrigger>

      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <fieldset className="space-y-2">
          <legend className="sr-only">{t("server.profile.legend")}</legend>
          {PROFILES.map((option) => (
            <label
              key={option}
              className={cn(
                "flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors",
                "focus-within:ring-2 focus-within:ring-ring",
                option === profile
                  ? "border-primary bg-primary/5"
                  : "border-border hover:bg-muted/50",
              )}
            >
              <input
                type="radio"
                name={name}
                value={option}
                checked={option === profile}
                onChange={() => setProfile(option)}
                className="mt-1 h-4 w-4 shrink-0 accent-primary focus:outline-none"
              />
              <span className="min-w-0 flex-1 space-y-1">
                <span className="flex items-center gap-2">
                  <span className="text-sm font-medium leading-tight">
                    {t(`server.profile.${option}`)}
                  </span>
                  {option === RECOMMENDED ? (
                    <Badge variant="contrast" className="ml-auto shrink-0">
                      {t("server.profile.recommended", { defaultValue: "Recommended" })}
                    </Badge>
                  ) : null}
                </span>
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  {t(`server.profile.${option}Hint`)}
                </span>
              </span>
            </label>
          ))}
        </fieldset>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
            {t("common.cancel")}
          </Button>
          <Button type="button" onClick={run}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
