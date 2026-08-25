import * as React from "react";
import { MARK_GRADIENTS, MARK_PATHS, MARK_VIEW_BOX } from "@/lib/brand";
import { cn } from "@/lib/utils";

/*
 * The panel's mark, drawn in the current text colour.
 *
 * It is deliberately not in lib/icons.ts. Everything named there is a Phosphor
 * glyph chosen to mean something - a shield for security, a globe for reach -
 * and the weights that file documents are the hierarchy those glyphs are read
 * by. This is not a glyph with a meaning; it is the product's own logotype, and
 * it has one weight because it is one drawing.
 *
 * The mark is a shield in outline with fine lettering inside it, and it wants
 * room: under about 28px the counters in the letters close up and it reads as a
 * smudge. Draw it at the size of the container rather than as a small glyph
 * inside a coloured tile.
 */

export type BrandMarkProps = React.SVGProps<SVGSVGElement>;

export function BrandMark({ className, ...props }: BrandMarkProps): JSX.Element {
  return (
    <svg
      viewBox={MARK_VIEW_BOX}
      fill="currentColor"
      // Decorative wherever the panel draws it: the name is always the text
      // beside it, so announcing the mark as well would read the product twice.
      aria-hidden="true"
      focusable="false"
      className={cn("h-9 w-9 shrink-0", className)}
      {...props}
    >
      {MARK_PATHS.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/**
 * The same mark, in its own colours rather than the current text colour - the
 * panel's default icon: the sidebar and the browser tab (see
 * `installFavicon` in lib/brand.ts) both draw from here. The monochrome
 * `BrandMark` above stays as it was for everywhere a single-colour silhouette
 * is what's wanted, the login page included.
 *
 * Each gradient needs an id unique to this instance of the SVG: the mark is
 * mounted twice at once (the desktop rail and the mobile drawer), and two
 * `<linearGradient id="g1">` elements in the same document would collide.
 */
export type ColorBrandMarkProps = Omit<React.SVGProps<SVGSVGElement>, "fill">;

export function ColorBrandMark({ className, ...props }: ColorBrandMarkProps): JSX.Element {
  const uid = React.useId();
  return (
    <svg
      viewBox={MARK_VIEW_BOX}
      aria-hidden="true"
      focusable="false"
      className={cn("h-9 w-9 shrink-0", className)}
      {...props}
    >
      <defs>
        {MARK_GRADIENTS.map((g) => (
          <linearGradient
            key={g.id}
            id={`${uid}-${g.id}`}
            gradientUnits="userSpaceOnUse"
            x1={g.x1}
            y1={g.y1}
            x2={g.x2}
            y2={g.y2}
          >
            {g.stops.map((s) => (
              <stop key={s.offset} offset={s.offset} stopColor={s.color} />
            ))}
          </linearGradient>
        ))}
      </defs>
      {MARK_PATHS.map((d, i) => (
        <path key={d} fill={`url(#${uid}-${MARK_GRADIENTS[i].id})`} d={d} />
      ))}
    </svg>
  );
}
