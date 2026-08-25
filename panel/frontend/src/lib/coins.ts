/*
 * The coin marks, in one file, the way lib/brand.ts holds the panel's own.
 *
 * These are logotypes rather than glyphs, so they are not in lib/icons.ts:
 * nothing here answers to that module's weight hierarchy, and none of it can be
 * swapped for a different drawing that means the same thing. Bitcoin's mark is
 * Bitcoin's mark.
 *
 * They are also the one place in the panel where a hue means something other
 * than state. index.css is explicit that the palette carries no colour outside
 * the state ramp and the chart series, and this is the same exception the
 * sidebar's own mark takes (see ColorBrandMark): a mark drawn in the panel's
 * ink is not the mark, and on a page whose whole job is "find your wallet's
 * row at a glance" the colour is what does the finding. It is confined to a
 * 40px disc, and the address beside it is the page's ink like everything else.
 *
 * Every drawing is the glyph alone on a 32x32 box, knocked out in white over a
 * disc filled with `color` - so the marks that ship with their own circle
 * (Bitcoin, Litecoin, Monero, Bitcoin Cash) and the ones that do not (Ethereum,
 * Solana, XRP) come out the same size, in the same shape, on the same baseline.
 */

export interface CoinMarkPath {
  d: string;
  /** Ethereum's facets are one shape at three opacities. Solid unless set. */
  opacity?: number;
}

export interface CoinMark {
  /** The disc behind the glyph. The brand's own colour, not a palette token. */
  color: string;
  paths: readonly CoinMarkPath[];
}

export type CoinId = "btc" | "eth" | "xmr" | "usdt" | "sol" | "trx" | "xrp" | "ltc" | "bch" | "bnb";

/** Every mark is drawn on this box, which is what makes them interchangeable. */
export const COIN_VIEW_BOX = "0 0 32 32";

export const COIN_MARKS: Record<CoinId, CoinMark> = {
  btc: {
    color: "#F7931A",
    paths: [
      {
        d: "M23.189 14.02c.314-2.096-1.283-3.223-3.465-3.975l.708-2.84-1.728-.43-.69 2.765c-.454-.114-.92-.22-1.385-.326l.695-2.783L15.596 6l-.708 2.839c-.376-.086-.746-.17-1.104-.26l.002-.009-2.384-.595-.46 1.846s1.283.294 1.256.312c.7.175.826.638.805 1.006l-.806 3.235c.048.012.11.03.18.057l-.183-.045-1.13 4.532c-.086.212-.303.531-.793.41.018.025-1.256-.313-1.256-.313l-.858 1.978 2.25.561c.418.105.828.215 1.231.318l-.715 2.872 1.727.43.708-2.84c.472.127.93.245 1.378.357l-.706 2.828 1.728.43.715-2.866c2.948.558 5.164.333 6.097-2.333.752-2.146-.037-3.385-1.588-4.192 1.13-.26 1.98-1.003 2.207-2.538zm-3.95 5.538c-.533 2.147-4.148.986-5.32.695l.95-3.805c1.172.293 4.929.872 4.37 3.11zm.535-5.569c-.487 1.953-3.495.96-4.47.717l.86-3.45c.975.243 4.118.696 3.61 2.733z",
      },
    ],
  },
  eth: {
    color: "#627EEA",
    paths: [
      { d: "M16.498 4v8.87l7.497 3.35z", opacity: 0.602 },
      { d: "M16.498 4L9 16.22l7.498-3.35z" },
      { d: "M16.498 21.968v6.027L24 17.616z", opacity: 0.602 },
      { d: "M16.498 27.995v-6.028L9 17.616z" },
      { d: "M16.498 20.573l7.497-4.353-7.497-3.348z", opacity: 0.2 },
      { d: "M9 16.22l7.498 4.353v-7.701z", opacity: 0.602 },
    ],
  },
  xmr: {
    color: "#FF6600",
    paths: [
      {
        d: "M15.97 5.235c5.985 0 10.825 4.84 10.825 10.824a11.07 11.07 0 01-.558 3.432h-3.226v-9.094l-7.04 7.04-7.04-7.04v9.094H5.704a11.07 11.07 0 01-.557-3.432c0-5.984 4.84-10.824 10.824-10.824zM14.358 19.02L16 20.635l1.613-1.614 3.051-3.08v5.72h4.547a10.806 10.806 0 01-9.24 5.192c-3.902 0-7.334-2.082-9.24-5.192h4.546v-5.72l3.08 3.08z",
      },
    ],
  },
  usdt: {
    color: "#26A17B",
    paths: [
      {
        d: "M17.922 17.383v-.002c-.11.008-.677.042-1.942.042-1.01 0-1.721-.03-1.971-.042v.003c-3.888-.171-6.79-.848-6.79-1.658 0-.809 2.902-1.486 6.79-1.66v2.644c.254.018.982.061 1.988.061 1.207 0 1.812-.05 1.925-.06v-2.643c3.88.173 6.775.85 6.775 1.658 0 .81-2.895 1.485-6.775 1.657m0-3.59v-2.366h5.414V7.819H8.595v3.608h5.414v2.365c-4.4.202-7.709 1.074-7.709 2.118 0 1.044 3.309 1.915 7.709 2.118v7.582h3.913v-7.584c4.393-.202 7.694-1.073 7.694-2.116 0-1.043-3.301-1.914-7.694-2.117",
      },
    ],
  },
  sol: {
    // Solana's own mark is a two-stop gradient the panel has no way to carry
    // into a 40px disc without a defs block per instance. The purple is the
    // half of it people recognise.
    color: "#9945FF",
    paths: [
      {
        d: "M9.925 19.687a.59.59 0 01.415-.17h14.366a.29.29 0 01.207.497l-2.838 2.815a.59.59 0 01-.415.171H7.294a.291.291 0 01-.207-.498l2.838-2.815zm0-10.517A.59.59 0 0110.34 9h14.366c.261 0 .392.314.207.498l-2.838 2.815a.59.59 0 01-.415.17H7.294a.291.291 0 01-.207-.497L9.925 9.17zm12.15 5.225a.59.59 0 00-.415-.17H7.294a.291.291 0 00-.207.498l2.838 2.815c.11.109.26.17.415.17h14.366a.291.291 0 00.207-.498l-2.838-2.815z",
      },
    ],
  },
  trx: {
    color: "#EF0027",
    paths: [
      {
        d: "M21.932 9.913L7.5 7.257l7.595 19.112 10.583-12.894-3.746-3.562zm-.232 1.17l2.208 2.099-6.038 1.093 3.83-3.192zm-5.142 2.973l-6.364-5.278 10.402 1.914-4.038 3.364zm-.453.934l-1.038 8.58L9.472 9.487l6.633 5.502zm.96.455l6.687-1.21-7.67 9.343.983-8.133z",
      },
    ],
  },
  xrp: {
    // The darkest disc on the page, and the only one that needs the hairline
    // ring CoinMark draws: near-black on the dark theme's card is a shape you
    // can only see because the glyph inside it is white.
    color: "#23292F",
    paths: [
      {
        d: "M23.07 8h2.89l-6.015 5.957a5.621 5.621 0 01-7.89 0L6.035 8H8.93l4.57 4.523a3.556 3.556 0 004.996 0L23.07 8zM8.895 24.563H6l6.055-5.993a5.621 5.621 0 017.89 0L26 24.562h-2.895L18.5 20a3.556 3.556 0 00-4.996 0l-4.61 4.563z",
      },
    ],
  },
  ltc: {
    color: "#345D9D",
    paths: [
      {
        d: "M10.427 19.214L9 19.768l.688-2.759 1.444-.58L13.213 8h5.129l-1.519 6.196 1.41-.571-.68 2.75-1.427.571-.848 3.483H23L22.127 24H9.252z",
      },
    ],
  },
  bch: {
    color: "#8DC351",
    paths: [
      {
        d: "M21.207 10.534c-.776-1.972-2.722-2.15-4.988-1.71l-.807-2.813-1.712.491.786 2.74c-.45.128-.908.27-1.363.41l-.79-2.758-1.711.49.805 2.813c-.368.114-.73.226-1.085.328l-.003-.01-2.362.677.525 1.83s1.258-.388 1.243-.358c.694-.199 1.035.139 1.2.468l.92 3.204c.047-.013.11-.029.184-.04l-.181.052 1.287 4.49c.032.227.004.612-.48.752.027.013-1.246.356-1.246.356l.247 2.143 2.228-.64c.415-.117.825-.227 1.226-.34l.817 2.845 1.71-.49-.807-2.815a65.74 65.74 0 001.372-.38l.802 2.803 1.713-.491-.814-2.84c2.831-.991 4.638-2.294 4.113-5.07-.422-2.234-1.724-2.912-3.471-2.836.848-.79 1.213-1.858.642-3.3zm-.65 6.77c.61 2.127-3.1 2.929-4.26 3.263l-1.081-3.77c1.16-.333 4.704-1.71 5.34.508zm-2.322-5.09c.554 1.935-2.547 2.58-3.514 2.857l-.98-3.419c.966-.277 3.915-1.455 4.494.563z",
      },
    ],
  },
  bnb: {
    color: "#F0B90B",
    paths: [
      {
        d: "M12.116 14.404L16 10.52l3.886 3.886 2.26-2.26L16 6l-6.144 6.144 2.26 2.26zM6 16l2.26-2.26L10.52 16l-2.26 2.26L6 16zm6.116 1.596L16 21.48l3.886-3.886 2.26 2.259L16 26l-6.144-6.144-.003-.003 2.263-2.257zM21.48 16l2.26-2.26L26 16l-2.26 2.26L21.48 16zm-3.188-.002h.002V16L16 18.294l-2.291-2.29-.004-.004.004-.003.401-.402.195-.195L16 13.706l2.293 2.293z",
      },
    ],
  },
};
