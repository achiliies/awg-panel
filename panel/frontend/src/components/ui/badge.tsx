/* eslint-disable react-refresh/only-export-components -- badgeVariants lets a
   caller put badge styling on an element that is not a Badge. */
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/*
 * Every variant pairs a token with its own foreground token, so contrast holds
 * in both themes without a per-theme override here.
 *
 * `muted` is for a label that states a fact about a thing rather than warning
 * about it. It borrows no signal colour at all, because any signal colour on a
 * chip that appears beside most fields on a page turns the whole page into a
 * warning; `sm` shrinks it further so it sits under the field label rather than
 * competing with it.
 *
 * `contrast` is the page's own ink and paper swapped round: black on white in
 * the light theme, white on black in the dark one. It is for a chip that has to
 * be seen without claiming to mean anything - a recommendation is not a success,
 * a warning, or a state, so borrowing any of those colours would say something
 * about the thing it marks that is not true.
 *
 * `caution` sits between the two, for a warning that repeats down a page. A
 * filled amber chip is right when there is one of it and wrong when there are
 * seven, one per row, because at that point the colour has stopped marking
 * anything out and is just the loudest thing on screen. So the wording keeps
 * the page's own text colour, which is black on the light theme and white on
 * the dark one, and the amber is spent on the edge and a wash behind it instead.
 */
const badgeVariants = cva(
  [
    "inline-flex items-center gap-1 rounded-full border",
    "font-medium leading-tight transition-colors",
    "focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 focus:ring-offset-background",
    "[&_svg]:pointer-events-none [&_svg]:h-3 [&_svg]:w-3 [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        destructive: "border-transparent bg-destructive text-destructive-foreground",
        outline: "border-border text-foreground",
        warning: "border-transparent bg-warning text-warning-foreground",
        caution: "border-warning/50 bg-warning/10 text-foreground",
        success: "border-transparent bg-success text-success-foreground",
        muted: "border-transparent bg-muted text-muted-foreground",
        contrast: "border-transparent bg-foreground text-background",
      },
      size: {
        default: "px-2.5 py-0.5 text-xs",
        // Same box as the <code> chip that carries the config key, so a field
        // label and everything after it reads as one line of small print.
        sm: "px-1.5 py-0.5 text-[11px]",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant, size, ...props }, ref) => (
    <span ref={ref} className={cn(badgeVariants({ variant, size }), className)} {...props} />
  ),
);
Badge.displayName = "Badge";

export { Badge, badgeVariants };
