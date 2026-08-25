import * as React from "react";

import { useBottomInset } from "@/lib/toast-space";
import { cn } from "@/lib/utils";

/*
 * The bar a form grows on its bottom edge while it has unsaved changes: what
 * changed and what applying it costs on one side, Discard and Save on the
 * other.
 *
 * Server Config and Settings had the same bar written out twice, which is how
 * they came to have the same bug twice. It lives here now, and so does the one
 * thing about it that is not layout: it measures itself and reserves that
 * height, because the toast stack is pinned to the same corner and would
 * otherwise cover Save while telling the operator to press it.
 *
 * `sticky`, not `fixed`: AppShell makes the page a column so a short tab still
 * pushes this onto the bottom edge of the window, and staying in the flow means
 * the end of a long form is reachable rather than parked under the bar.
 */

export interface StickyActionBarProps {
  /** The message half - what changed, and what it will cost to apply. */
  children: React.ReactNode;
  /** The buttons the form ends with. */
  actions: React.ReactNode;
  /** For the gap above it, which differs by page. */
  className?: string;
}

export function StickyActionBar({
  children,
  actions,
  className,
}: StickyActionBarProps): JSX.Element {
  const ref = React.useRef<HTMLDivElement>(null);
  useBottomInset(ref);

  return (
    <div
      ref={ref}
      className={cn(
        // The negative margins undo the page padding so the bar reaches both
        // edges of the window while its contents stay on the page grid.
        "sticky bottom-0 z-30 -mx-4 -mb-6 border-t border-border bg-card/95 px-4 py-3",
        "shadow-[0_-4px_12px_-6px_hsl(var(--foreground)/0.25)] backdrop-blur",
        "sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8",
        className,
      )}
    >
      <div className="mx-auto flex max-w-[100rem] flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 space-y-1">{children}</div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
      </div>
    </div>
  );
}
