import * as React from "react";
import { Check } from "@/lib/icons";

import { cn } from "@/lib/utils";

/*
 * A checkbox, drawn on the browser's own input rather than on a div.
 *
 * Every other control in this folder wraps a Radix primitive, and this one
 * deliberately does not. A checkbox is the one thing the platform already gets
 * right on its own - role, keyboard, form participation, the way a screen
 * reader announces it - and the Radix package that would supply it is not among
 * the panel's dependencies, which are vendored and locked. `appearance-none`
 * removes the platform's paint and nothing else, so what is left is a real
 * checkbox wearing the panel's colours.
 *
 * The tick is drawn over the box rather than inside it: an input has no
 * children, so the glyph is a sibling positioned on top and revealed by the
 * input's own :checked state.
 *
 * Which is why `className` dresses the wrapper and not the input. The two are
 * one control drawn in two elements, and only one of them is in the flow: a
 * caller nudging the box down to sit with the first line of its label - the
 * `mt-0.5` the bulk-remove dialog passes - moved the input and left the tick
 * behind at the top of a wrapper that had not moved, so the glyph sat two
 * pixels high in its own box. The input fills the wrapper instead, and both
 * the box and the tick take whatever position, size or spacing the caller
 * asked for.
 */
const Checkbox = React.forwardRef<HTMLInputElement, React.ComponentPropsWithoutRef<"input">>(
  ({ className, ...props }, ref) => (
    <span className={cn("relative inline-flex h-4 w-4 shrink-0", className)}>
      <input
        ref={ref}
        type="checkbox"
        className={cn(
          "peer h-full w-full cursor-pointer appearance-none rounded border border-input bg-background shadow-sm",
          "transition-colors checked:border-primary checked:bg-primary",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          "disabled:cursor-not-allowed disabled:opacity-50",
        )}
        {...props}
      />
      <Check
        // Bold: at 16px a hairline tick thins out to nothing on a low-DPI
        // screen, which is the rule lib/icons states for anything this size.
        weight="bold"
        className={cn(
          "pointer-events-none absolute inset-0 hidden h-full w-full p-0.5",
          "text-primary-foreground peer-checked:block",
          // The box behind it fades when the control is switched off, and a
          // tick at full strength on a faded box is the two halves disagreeing.
          "peer-disabled:opacity-50",
        )}
        aria-hidden="true"
      />
    </span>
  ),
);
Checkbox.displayName = "Checkbox";

export { Checkbox };
