import { cn } from "@/lib/utils";
import type { WalletCode as Code } from "@/lib/walletCodes";

/*
 * One generated symbol, drawn.
 *
 * Black on white in both themes, exactly as the client QR is, and for the same
 * reason: a code with its tones swapped, or sitting on a card two shades off
 * white, is the usual reason a phone camera will not lock on. The white box is
 * also the quiet zone - the path itself is generated with no border, so the
 * padding here is what a scanner reads as the margin.
 *
 * `shapeRendering="crispEdges"` turns off antialiasing. At a size where one
 * module lands between two device pixels, a smoothed edge is a grey module,
 * and a grey module is a module the decoder has to guess at.
 */

export interface WalletCodeProps {
  code: Code;
  /** Announced in place of the drawing, which has nothing readable in it. */
  label: string;
  className?: string;
}

export function WalletCode({ code, label, className }: WalletCodeProps): JSX.Element {
  return (
    <div className={cn("rounded-lg border border-border bg-white p-3", className)}>
      <svg
        viewBox={`0 0 ${code.size} ${code.size}`}
        role="img"
        aria-label={label}
        shapeRendering="crispEdges"
        className="aspect-square w-full"
      >
        {/* One stroked path down the middle of each run of dark modules, which
            is how segno draws a symbol - hence the half-pixel offsets in the
            generated data and the width of exactly one module here. */}
        <path d={code.path} fill="none" stroke="#000" strokeWidth={1} />
      </svg>
    </div>
  );
}
