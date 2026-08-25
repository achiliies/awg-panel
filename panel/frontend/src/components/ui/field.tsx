import * as React from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/*
 * One labelled control, with its line of help and whatever is wrong with it.
 *
 * The settings page invented this and the bandwidth card on the server page
 * needs the same thing: label, control, hint, error, in that order and at that
 * type size. Two files drawing the row themselves is how the hint under one box
 * ends up a size larger than the hint under the next one, so the row lives here
 * and both import it.
 *
 * Not to be confused with ParamField beside it, which draws a row of the server
 * parameter catalog - badges, key name, help popover and all. This is the plain
 * one, for a control the panel wrote itself.
 */

export interface FieldProps {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  /** Rendered to the right of the label, e.g. a Regenerate button. */
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function Field({
  id,
  label,
  hint,
  error,
  action,
  children,
  className,
}: FieldProps): JSX.Element {
  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Label htmlFor={id}>{label}</Label>
        {action}
      </div>
      {children}
      {hint ? <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p> : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium leading-relaxed text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
