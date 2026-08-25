import * as React from "react";
import { CaretDown, type Icon } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { GroupHeader } from "@/components/server/GroupHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { useReveal } from "@/lib/reveal";
import type { ParamGroup as ParamGroupId } from "@/api/types";

/*
 * One card per group of parameters.
 *
 * The group is the unit an admin thinks in - "the junk packet settings", not
 * "Jc, Jmin and Jmax" - so the card leads with a sentence about what the whole
 * group does before showing a single field. Title and sentence come from the UI
 * catalog by group id, so a group added to the API arrives here with its own
 * words and no change to this file.
 *
 * The head of the card is GroupHeader, which the Obfuscation card beside it
 * uses as well: two cards on one page that lay their titles and buttons out
 * differently is a difference an admin reads as a mistake, and it was one.
 */

export interface ParamGroupProps {
  group: ParamGroupId;
  icon?: Icon;
  /** Overrides the title from i18n. */
  title?: string;
  /** Overrides the one-line description from i18n. */
  description?: string;
  /**
   * A chip on the heading, stating what kind of group this is rather than what
   * state it is in - "Beta feature", not "3 changed". It sits beside the title
   * because it qualifies the title, and it stays visible while the card is
   * closed, which is the only time most people will see this group at all.
   */
  badge?: React.ReactNode;
  /**
   * Buttons that act on the whole group - generate a set, clear it. They stay
   * in the header rather than moving inside, so a collapsed card still offers
   * them: a group nobody has opened is exactly the one to fill in one go.
   */
  actions?: React.ReactNode;
  /** Advanced settings start closed: they are a trap for anyone browsing. */
  collapsible?: boolean;
  defaultOpen?: boolean;
  /** Unsaved edits in this group, badged so a closed card still says so. */
  changedCount?: number;
  /** Fields the server or the form rejected. A closed card opens itself for these. */
  errorCount?: number;
  /** A note above the fields, e.g. that the hooks are managed by the installer. */
  note?: React.ReactNode;
  /** Teal for a fact about the group, amber when the note is a caution. */
  noteTone?: "info" | "warning";
  children: React.ReactNode;
  className?: string;
}

export function ParamGroup({
  group,
  icon: Icon,
  title,
  description,
  badge,
  actions,
  collapsible = false,
  defaultOpen = true,
  changedCount = 0,
  errorCount = 0,
  note,
  noteTone = "info",
  children,
  className,
}: ParamGroupProps): JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(defaultOpen || !collapsible);

  // A collapsible card is the last thing on its page, so opening one from the
  // heading unfolds the fields below the bottom of the window: the caret turns
  // over, a line of note appears, and everything that was asked for is off
  // screen. So the card brings itself up to the top of the page as it opens,
  // and the counter is what says "opened again" to a card that was closed and
  // reopened without the page having moved in between.
  const [opened, setOpened] = React.useState(0);
  const cardRef = useReveal<HTMLDivElement>(opened);

  const toggle = (): void => {
    const next = !open;
    setOpen(next);
    if (next) {
      setOpened((count) => count + 1);
    }
  };

  // A message nobody can see is a message that did not happen: a rejected field
  // inside a collapsed card would leave the save bar complaining about
  // something with no visible cause. An edited one is the same problem one step
  // earlier - a card that fills itself in and stays shut, because the values
  // came from a button in its header rather than from typing, offers Save on
  // settings nobody has been shown.
  React.useEffect(() => {
    if (errorCount > 0 || changedCount > 0) {
      setOpen(true);
    }
  }, [errorCount, changedCount]);

  const contentId = `param-group-${group}`;
  const headingText = title ?? String(t(`server.${group}`));
  const descriptionText = description ?? String(t(`server.${group}Hint`));
  const expanded = open || !collapsible;

  // Title and badge travel together so the caret still lands on the right edge
  // of a collapsible header rather than being pushed off by the chip.
  const heading = (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <span className="min-w-0">{headingText}</span>
      {badge}
    </span>
  );

  return (
    <Card ref={cardRef} className={cn("scroll-mt-20", className)}>
      <GroupHeader
        icon={Icon}
        title={
          collapsible ? (
            <button
              type="button"
              onClick={toggle}
              aria-expanded={expanded}
              aria-controls={contentId}
              className={cn(
                "-m-1 flex w-full items-center justify-between gap-2 rounded-md p-1 text-start",
                "transition-colors hover:text-primary focus-visible:outline-none",
                "focus-visible:ring-2 focus-visible:ring-ring",
              )}
            >
              {heading}
              <CaretDown
                aria-hidden="true"
                className={cn(
                  "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                  expanded && "rotate-180",
                )}
              />
            </button>
          ) : (
            heading
          )
        }
        description={descriptionText}
        actions={
          errorCount > 0 || changedCount > 0 || actions ? (
            <>
              {errorCount > 0 ? (
                <Badge variant="destructive">
                  {t("server.groupErrors", { count: errorCount, defaultValue: "{{count}} to fix" })}
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
              {actions}
            </>
          ) : null
        }
      />

      {expanded ? (
        <CardContent id={contentId}>
          {/* Same box, same type size as the page-level Notice above these
              cards: it is the same kind of thing being said, and a second
              scale for it made the card read as a footnote to itself. */}
          {note ? (
            <div
              className={cn(
                "mb-6 rounded-lg border p-4 text-sm leading-relaxed text-muted-foreground",
                noteTone === "warning"
                  ? "border-warning/40 bg-warning/5"
                  : "border-info/40 bg-info/5",
              )}
            >
              {note}
            </div>
          ) : null}
          <div className="grid gap-x-6 gap-y-6 sm:grid-cols-2">{children}</div>
        </CardContent>
      ) : null}
    </Card>
  );
}
