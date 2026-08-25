import * as React from "react";
import * as ProgressPrimitive from "@radix-ui/react-progress";

import { clamp, cn } from "@/lib/utils";

export interface ProgressProps extends React.ComponentPropsWithoutRef<
  typeof ProgressPrimitive.Root
> {
  /** Recolour the filled part, e.g. amber near a quota and red over it. */
  indicatorClassName?: string;
}

/*
 * The indicator is translated rather than width-animated so the bar does not
 * relayout on every 2 s stats poll. The value is clamped because a quota can be
 * exceeded, and a peer's counters can outrun its own limit between polls.
 */
const Progress = React.forwardRef<React.ElementRef<typeof ProgressPrimitive.Root>, ProgressProps>(
  ({ className, value, indicatorClassName, ...props }, ref) => {
    const pct = clamp(Number(value ?? 0), 0, 100);
    return (
      <ProgressPrimitive.Root
        ref={ref}
        value={pct}
        className={cn("relative h-2 w-full overflow-hidden rounded-full bg-secondary", className)}
        {...props}
      >
        <ProgressPrimitive.Indicator
          className={cn("h-full w-full flex-1 bg-primary transition-transform", indicatorClassName)}
          style={{ transform: `translateX(-${(100 - pct).toFixed(2)}%)` }}
        />
      </ProgressPrimitive.Root>
    );
  },
);
Progress.displayName = ProgressPrimitive.Root.displayName;

export { Progress };
