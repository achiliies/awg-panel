import { COIN_MARKS, COIN_VIEW_BOX, type CoinId } from "@/lib/coins";
import { cn } from "@/lib/utils";

/*
 * A coin's mark on its own disc, and optionally a second disc on the corner
 * for the chain it arrived on.
 *
 * The disc is what makes ten logotypes from ten different design systems sit
 * on one page without any of them being the loud one. Bitcoin and Litecoin
 * ship as a circle with a letter cut out of it, Ethereum and XRP as a bare
 * shape; drawing every glyph knocked out in white over a circle of the brand's
 * own colour puts all of them in the same 40px hole at the same weight.
 */

export interface CoinMarkProps {
  coin: CoinId;
  /** The chain badge, for a token that exists on more than one. */
  chain?: CoinId;
  /** Sizes the whole thing; the chain badge is a fraction of it. */
  className?: string;
}

/** One disc. The ring is the caller's, because the two here want different ones. */
function Disc({ coin, className }: { coin: CoinId; className?: string }): JSX.Element {
  const mark = COIN_MARKS[coin];
  return (
    <svg
      viewBox={COIN_VIEW_BOX}
      // Decorative: the coin's name is written beside it in every place this
      // is drawn, so announcing the mark as well reads the coin out twice.
      aria-hidden="true"
      focusable="false"
      className={cn("rounded-full", className)}
    >
      <circle cx="16" cy="16" r="16" fill={mark.color} />
      {mark.paths.map((path) => (
        <path key={path.d} d={path.d} fill="#fff" fillOpacity={path.opacity} />
      ))}
    </svg>
  );
}

export function CoinMark({ coin, chain, className }: CoinMarkProps): JSX.Element {
  return (
    <span className={cn("relative inline-block h-10 w-10 shrink-0", className)}>
      {/* The hairline is not decoration. XRP's brand colour is very nearly
          black, and on the dark theme's card a disc that dark has no edge at
          all - the mark reads as three white strokes floating on the card. It
          costs nothing on the nine discs that do not need it. */}
      <Disc coin={coin} className="h-full w-full ring-1 ring-black/10 dark:ring-white/15" />
      {chain ? (
        // Ringed in the card's own colour rather than a hairline, so the badge
        // punches a hole in the disc behind it instead of overlapping it - two
        // circles touching at an edge read as one smudged shape.
        <Disc
          coin={chain}
          className="absolute -bottom-0.5 -end-0.5 h-[45%] w-[45%] ring-2 ring-card"
        />
      ) : null}
    </span>
  );
}
