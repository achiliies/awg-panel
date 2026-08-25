import * as React from "react";
import type { Icon } from "@/lib/icons";

import { cn } from "@/lib/utils";

/*
 * The only <h1> on a page. The top bar repeats the route name as plain text so
 * a scrolled page still says where it is, but the document outline starts here.
 */

export interface PageHeaderProps {
  title: string;
  /** One or two lines saying what this page changes on the server. */
  description?: string;
  icon?: Icon;
  /** Status shown beside the title, e.g. a Badge or a StatusDot. */
  badge?: React.ReactNode;
  /** Primary actions for the page. Wraps under the title on narrow screens. */
  actions?: React.ReactNode;
  /** A filter row or tab strip that belongs to the header block. */
  children?: React.ReactNode;
  className?: string;
}

export function PageHeader({
  title,
  description,
  icon: Icon,
  badge,
  actions,
  children,
  className,
}: PageHeaderProps): JSX.Element {
  return (
    <header className={cn("mb-6 space-y-4", className)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
            {Icon ? (
              <Icon
                // Duotone: this glyph is the page's identity, and it has to
                // hold its own beside a 24px semibold <h1>.
                weight="duotone"
                className="h-5 w-5 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            ) : null}
            <h1 className="min-w-0 text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
            {badge}
          </div>
          {description ? (
            /*
             * text-balance because these run to a line and a half more often
             * than not, and the half is what it looks like: the API page's
             * subtitle broke after "by a script" and left "instead." sitting on
             * a line of its own, which reads as a sentence that ran out of room
             * rather than one that was written. Balanced, the same words come
             * out as two even lines with nothing stranded on either.
             */
            <p className="mt-1.5 max-w-2xl text-balance text-sm leading-relaxed text-muted-foreground">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">{actions}</div>
        ) : null}
      </div>
      {children}
    </header>
  );
}
