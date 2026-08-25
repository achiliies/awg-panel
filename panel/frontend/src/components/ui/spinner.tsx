/* eslint-disable react-refresh/only-export-components -- spinnerVariants keeps
   inline "loading" affordances the same size as a standalone Spinner. */
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/*
 * animate-spinner rather than Tailwind's animate-spin: see the reduced-motion
 * block in index.css, which has to be able to tell a progress indicator apart
 * from decoration.
 *
 * text-current looks like a no-op, and is: it is here so tailwind-merge treats
 * a caller's text-primary as a conflict and drops this one, instead of leaving
 * two colour classes on the element and letting source order decide.
 */
const spinnerVariants = cva("animate-spinner text-current", {
  variants: {
    size: {
      sm: "h-4 w-4",
      default: "h-5 w-5",
      lg: "h-8 w-8",
    },
  },
  defaultVariants: {
    size: "default",
  },
});

/* r=9 in a 24 box, so the 2.5-wide stroke still clears the edge. The arc is a
   quarter of the circumference (2*PI*9 = 56.55): long enough to read as motion
   rather than a dot, short enough that the gap stays obvious. */
const CIRCUMFERENCE = 56.55;
const ARC_GAP = CIRCUMFERENCE * 0.75;

export interface SpinnerProps
  extends Omit<React.SVGAttributes<SVGSVGElement>, "role">, VariantProps<typeof spinnerVariants> {
  /** Announced while the spinner is on screen. Defaults to the t('common.loading') text. */
  label?: string;
  /** Set when the spinner sits next to text that already says what is loading. */
  decorative?: boolean;
}

/*
 * Never rendered alone on an empty page: pages pair it with the label of what
 * they are waiting for, or use Skeleton for content that has a known shape.
 * The one exception is the boot ring in index.html, which paints before i18n
 * exists and so has no label to pair with.
 *
 * Two circles, not one: the faint track turns a travelling arc into something
 * going around a ring. Without it the arc reads as a stray mark, and at 16px
 * inside a button it is easy to miss altogether. The stroke scales with the
 * box, so every size is the same drawing.
 */
const Spinner = React.forwardRef<SVGSVGElement, SpinnerProps>(
  ({ className, size, label, decorative = false, ...props }, ref) => {
    const { t } = useTranslation();
    const text = label ?? String(t("common.loading", { defaultValue: "Loading" }));

    return (
      <svg
        ref={ref}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        className={cn(spinnerVariants({ size }), className)}
        role={decorative ? undefined : "status"}
        aria-hidden={decorative ? true : undefined}
        aria-label={decorative ? undefined : text}
        {...props}
      >
        {/* 25%, not less: on the light theme the track is a near-black mixed
            most of the way into a near-white card, and below this it stops
            being visible at the 16px these are usually drawn at. */}
        <circle cx="12" cy="12" r="9" className="opacity-25" />
        <circle
          cx="12"
          cy="12"
          r="9"
          strokeLinecap="round"
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={ARC_GAP}
        />
      </svg>
    );
  },
);
Spinner.displayName = "Spinner";

export { Spinner, spinnerVariants };
