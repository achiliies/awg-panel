import * as React from "react";

import { cn } from "@/lib/utils";

/*
 * Placeholders are announced as busy rather than left silent: a screen reader
 * user otherwise hears an empty page while data loads.
 */
const Skeleton = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  ),
);
Skeleton.displayName = "Skeleton";

export { Skeleton };
