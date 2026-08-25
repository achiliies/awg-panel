import { BookOpenText } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { paramText } from "@/lib/paramText";
import { cn } from "@/lib/utils";
import type { ParamGroup as ParamGroupId, ParamSpec } from "@/api/types";

/*
 * "How AmneziaWG obfuscation works", in the order the packets actually leave
 * the machine. Four steps, one per group of settings, each naming the
 * parameters from the live catalog rather than a list written here - so the
 * walkthrough cannot drift from the form beside it.
 *
 * Nothing in this panel adds encryption. The whole point of the feature is that
 * a filter cannot tell what the traffic is, and the text says so, because an
 * admin who believes otherwise makes worse decisions everywhere else.
 */

/** The handshake in the order it happens: decoys, junk, headers, sizes. */
const STEPS: readonly ParamGroupId[] = ["imitation", "junk", "headers", "sizes"];

/** Fallback English for the step sentences, keyed by group. */
const STEP_TEXT: Readonly<Record<string, string>> = {
  imitation:
    "Nothing WireGuard-shaped goes out first. The tunnel opens with complete decoy packets that " +
    "copy ordinary traffic - a DNS answer, a clock sync, a STUN probe, the start of a QUIC " +
    "session - so the flow begins the way any other connection on the network begins.",
  junk:
    "Then the real handshake leaves buried in a burst of random packets. The far end cannot " +
    "parse them and throws them away, so they cost only a little bandwidth, but a filter " +
    "watching for one clean 148-byte initiation sees a burst of unrelated traffic instead.",
  headers:
    "The handshake itself no longer carries WireGuard's 1, 2, 3 and 4 packet type markers, which " +
    "are the easiest way there is to recognise the protocol. Each type gets its own value, or a " +
    "range that picks a fresh number for every packet, leaving no constant byte to match on.",
  sizes:
    "Finally the sizes change. A WireGuard handshake is always exactly 148 bytes answered by 92, " +
    "which identifies it on its own; padding moves each packet type onto a size that means " +
    "nothing. The padding on data packets applies to every packet, so it costs usable MTU.",
};

export interface HelpDrawerProps {
  /** The live catalog, so each step can name the settings that belong to it. */
  specs: ParamSpec[];
  className?: string;
}

export function HelpDrawer({ specs, className }: HelpDrawerProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm" className={className}>
          <BookOpenText aria-hidden="true" />
          {t("server.howItWorks")}
        </Button>
      </DialogTrigger>

      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("server.howItWorks")}</DialogTitle>
          <DialogDescription>{t("server.howItWorksBody")}</DialogDescription>
        </DialogHeader>

        <ol className="space-y-5">
          {STEPS.map((group, index) => (
            <Step key={group} group={group} number={index + 1} specs={specs} />
          ))}
        </ol>

        <p className="border-t border-border pt-4 text-sm leading-relaxed text-muted-foreground">
          {t("server.help.closing", {
            defaultValue:
              "Everything marked as matching is copied into each client config. Change one and " +
              "hand out the new files: when the two ends disagree, the handshake fails with no " +
              "error message on either side.",
          })}
        </p>
      </DialogContent>
    </Dialog>
  );
}

interface StepProps {
  group: ParamGroupId;
  number: number;
  specs: ParamSpec[];
}

function Step({ group, number, specs }: StepProps): JSX.Element {
  const { t } = useTranslation();

  const inGroup = specs.filter((spec) => spec.group === group);
  const matching = inGroup.filter((spec) => spec.mustMatchClient).map((spec) => spec.key);

  return (
    <li className="flex gap-3">
      <span
        aria-hidden="true"
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
          "bg-primary/10 text-sm font-semibold text-primary",
        )}
      >
        {number}
      </span>

      <div className="min-w-0 space-y-2">
        <h3 className="text-sm font-semibold leading-tight">{t(`server.${group}`)}</h3>

        <p className="text-sm leading-relaxed text-muted-foreground">
          {t(`server.help.step.${group}`, { defaultValue: STEP_TEXT[group] ?? "" })}
        </p>

        {inGroup.length > 0 ? (
          <>
            <ul className="flex flex-wrap gap-1.5">
              {inGroup.map((spec) => (
                <li key={spec.key}>
                  <Badge variant="outline" className="font-mono text-[11px]">
                    {spec.key}
                  </Badge>
                  <span className="sr-only">{paramText(t, spec).label}</span>
                </li>
              ))}
            </ul>

            <p className="text-xs leading-relaxed text-muted-foreground">
              {matching.length > 0
                ? t("server.help.mustMatch", {
                    keys: matching.join(", "),
                    defaultValue: "Both ends must use the same {{keys}}.",
                  })
                : t("server.help.noMatch", {
                    defaultValue: "None of these has to match on the client.",
                  })}
            </p>
          </>
        ) : null}
      </div>
    </li>
  );
}
