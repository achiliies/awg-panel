import * as React from "react";
import { Tray, type Icon } from "@/lib/icons";

import { cn } from "@/lib/utils";

/*
 * The panel starts empty on a fresh install, so "no clients yet" is a normal
 * state and not a failure: it names the thing that is missing and offers the
 * action that fills it, rather than leaving a blank card.
 */

export interface EmptyStateProps {
  /** Defaults to an inbox glyph; pass the icon of the thing that is missing. */
  icon?: Icon;
  title: string;
  /** One line. Say what would appear here and how to make it appear. */
  description?: string;
  /** A Button, or a couple of them. */
  action?: React.ReactNode;
  /** "page" fills a route body, "inline" fits inside a card or a table. */
  variant?: "page" | "inline";
  className?: string;
}

export function EmptyState({
  icon: Icon = Tray,
  title,
  description,
  action,
  variant = "page",
  className,
}: EmptyStateProps): JSX.Element {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-border",
        "bg-card/50 text-center",
        variant === "page" ? "px-6 py-14" : "px-4 py-8",
        className,
      )}
    >
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Icon weight="duotone" className="h-5 w-5" aria-hidden="true" />
      </span>
      <p className="mt-4 text-base font-medium text-foreground">{title}</p>
      {description ? (
        <p className="mt-1.5 max-w-md text-sm leading-relaxed text-muted-foreground">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-5 flex flex-wrap justify-center gap-2">{action}</div> : null}
    </div>
  );
}
