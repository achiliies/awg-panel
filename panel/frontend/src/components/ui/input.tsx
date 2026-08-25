import * as React from "react";

import { cn } from "@/lib/utils";

/*
 * aria-invalid drives the error colour instead of a prop, so a form only has to
 * set the attribute the screen reader already needs.
 *
 * There is nothing here for type="file". The browser draws that control itself,
 * in its own words and its own colours, and a border around it only made the
 * mismatch look deliberate. The one place that takes a file - restoring a
 * backup, in Settings - hides the input and drives it from a Button instead.
 */
const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        ref={ref}
        className={cn(
          "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm",
          "transition-colors placeholder:text-muted-foreground",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive",
          className,
        )}
        {...props}
      />
    );
  },
);
Input.displayName = "Input";

export { Input };
