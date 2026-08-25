import * as React from "react";
import type { Icon } from "@/lib/icons";

import { CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/*
 * The head of a settings card: icon, title, the sentence about the group, and
 * the buttons that act on the whole of it.
 *
 * It is its own component because the Obfuscation page stacks two of these
 * cards and they have to read as one page. They did not: each card had written
 * its own header, and only one of them had been taught to stack. On a phone the
 * Obfuscation card put its title and sentence across the full width with
 * Reconfigure underneath, while the card below it kept the buttons on the same
 * line all the way down - which left the title, the beta chip and a
 * three-line sentence sharing about a hundred pixels beside them.
 *
 * So the layout lives here once. Above `lg` the words take the room they need
 * and the buttons sit at the right; below it the buttons drop to their own row,
 * which is the only arrangement that leaves a sentence readable on a phone.
 * `actions` is a plain row of children so a card can put its badges, its
 * generator and an info button in it and have them wrap together.
 */

export interface GroupHeaderProps {
  icon?: Icon;
  /**
   * Usually a string. The collapsible card passes the button that carries the
   * title and the caret, so the whole width of the heading stays clickable.
   */
  title: React.ReactNode;
  /** One or two lines on what the group does, before any field is read. */
  description: React.ReactNode;
  /** Badges and buttons for the whole group; wraps onto its own row on a phone. */
  actions?: React.ReactNode;
}

export function GroupHeader({
  icon: Icon,
  title,
  description,
  actions,
}: GroupHeaderProps): JSX.Element {
  return (
    <CardHeader>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          {Icon ? (
            <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
              <Icon weight="duotone" className="h-4 w-4" aria-hidden="true" />
            </span>
          ) : null}

          <div className="min-w-0 flex-1">
            <CardTitle>{title}</CardTitle>
            <CardDescription className="mt-1.5">{description}</CardDescription>
          </div>
        </div>

        {actions ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2 lg:justify-end">{actions}</div>
        ) : null}
      </div>
    </CardHeader>
  );
}
