/*
 * The panel's own mark, and the one place its artwork lives.
 *
 * The mark is the AmneziaWG shield with the letters carried inside it, drawn as
 * a single silhouette with no colour of its own: every path here fills with
 * `currentColor`, so the login page's icon and anything else drawn from
 * MARK_PATHS directly inherit the colour of whatever they sit on rather than
 * carrying a hard-coded one that only survives one of the two themes.
 *
 * The panel's default icon - the tab favicon and the mark in the sidebar - is
 * the same drawing in its original colours instead, via MARK_GRADIENTS below.
 * The two are kept side by side rather than one replacing the other: the
 * monochrome silhouette is still what a login page or a dark-on-light UI
 * chrome wants, the coloured one is what identifies the product at a glance.
 *
 * The artwork is flat geometry, so it is a module rather than an .svg file on
 * disk. Vite would hash a file into dist/assets and hand back a URL, which is
 * exactly what the tab icon cannot use: the panel is mounted under a secret
 * base path that is only known at install time. Inline paths need no URL and
 * cannot 404.
 */

/**
 * What the panel calls itself, everywhere it says so.
 *
 * A constant rather than a setting, and it is the product's name rather than
 * this installation's: the sidebar, the browser title and the label an
 * authenticator files the TOTP entry under all read it from here, and letting
 * one server rename its copy only ever made those disagree with every
 * screenshot in the documentation. `PANEL_NAME` in awgui/settings.py is the
 * same string on the server side, for the one place the server has to spell it.
 */
export const PANEL_NAME = "AWG Panel";

/**
 * The window onto the artwork.
 *
 * The drawing arrives on a 1039x1016 canvas with the shield pushed into the
 * bottom-right of it - 131 units of empty space on the left against 81 on the
 * right, and 81 above against 15 below. Drawn as-is at 36px the mark reads as
 * something that slipped out of position. This is the same square recentred on
 * the shield's own bounding box, with about 3% of margin left around it so the
 * silhouette has somewhere to breathe.
 */
export const MARK_VIEW_BOX = "57 53 976 976";

/**
 * The silhouette, as the fill paths that compose it.
 *
 * Whitespace and repeated command letters are stripped from the source drawing;
 * every coordinate is otherwise untouched, to the digit.
 */
export const MARK_PATHS: readonly string[] = [
  "M455.5,134.7c-20.5,10.5-54.9,25.1-86,36.3-64.7,23.3-142,41.7-218.4,51.9-8.5,1.2-15.6,2.3-15.8,2.5-3.3,3.3-5.6,113.9-3.3,155.6 14.9,267.4,129.2,465.6,336.1,583.3 27.1,15.4,68.8,35.8,75.4,36.9 1.9,.3,2-.3,2.3-17.5 .4-24.7,2.7-21.6-28.1-37-42.3-21.1-71.9-39.8-108.2-68.6-28.8-22.7-41.5-39.3-46.1-59.8-2-9-1.6-19.6,.9-22 1.6-1.6,1.5-1.8-.4-3.9l-2.1-2.3 6.5-17.3c8.2-21.8,18.7-48.7,38.2-98.3 21-53.2,50.8-129.6,53.5-137.3 1.5-4.2,2.8-6.2,3.7-6 .9,.2,4.3,8.2,8.7,20.8 20.9,59,34.4,83.7,59.2,108.3 40.8,40.3,106.8,54.6,165,35.6 31.9-10.4,57.1-27.5,77.6-52.9l6.8-8.5-4.1-1.7c-2.2-1-7.8-5.2-12.2-9.4-4.5-4.2-10.9-9.6-14.2-12.1-3.3-2.5-7.5-6.3-9.3-8.4-4.1-4.8-4.1-4.8-7.2-.2-14.6,21.6-41.2,38.9-67.7,43.9-10.5,2-32.8,1.5-43.3-1-14.4-3.4-31-10.4-31-13.1 0-.7,3.3-9.1,7.4-18.6 4-9.6,9-21.9,11-27.4 2-5.5,4.8-12.7,6-16l2.4-6-3.6-2c-2.3-1.3-4.7-3.9-6.2-6.7-5-9.3-35.9-38.8-39.2-37.5-.7,.3-5.8,11.8-11.3,25.6-5.5,13.8-10.4,25.1-11,25.1-1,0-12.6-26.8-16.8-39-.9-2.5-5-13.5-9.1-24.5-4-11-8.3-22.9-9.4-26.5-1.1-3.6-4-12.1-6.5-19-6-17.2-7.1-23-6.4-34.7 .5-9.7,3.1-18.7,7.2-24.9 1.5-2.3,1.3-2.5-3.7-6.2-8-6-9.5-7.9-7.9-10.4 .8-1.2,1.2-4.7,1-9.1-.1-4,.6-11,1.5-15.7 2.2-10.5,1.8-14-2.5-21-5.9-9.6-10.8-14.8-15.8-16.5-7.6-2.6-8.4-2.2-10.8,4.2-9,24.1-16.8,44.1-27.5,71.3-11.9,30.2-17.1,43.3-41.8,107-17.6,45.4-18.5,47.5-20.2,47.8-1.6,.3-3.6-4.8-31.5-80.8-7.4-20.1-16.4-44.6-19.9-54.5-3.6-9.9-8.2-22.5-10.2-28l-3.8-10-34.8-.3-34.8-.2 .5,2.2c.3,1.3,3.4,9.9,6.9,19.3 3.6,9.3,8.3,21.9,10.6,28 2.2,6,13.1,34.6,24.2,63.5 11.1,28.9,26.2,68.5,33.5,88 7.3,19.5,15.6,41.3,18.4,48.5 2.8,7.1,5.1,13.8,5.1,14.8 0,.9-5.6,15.8-12.4,33-6.8,17.1-13.7,34.7-15.2,38.9-2,5.3-3.4,7.8-4.5,7.8-5.3,0-41.4-67.5-56.2-105-34.3-86.6-51-180.8-51.1-287.4l-.1-42 16-2.8c117.9-20.6,235-61.4,310.7-108.5l8.6-5.4 .6-10.2c.3-5.6,.8-12.7,1.1-15.7 .9-8-.5-9.5-8.7-9.5-6.1,.1-7,.5-27.4,11.3-23.2,12.4-33.2,16.8-34.6,15.4-.5-.5-.9-6-.9-12.2 0-9.2-.3-12-2-15.1-2.4-4.7-2.6-4.7-11.4-.2z",
  "M835.1,211.4c-3.7,20-4.5,22.4-7.9,23.3-3.3,.8-12.8-.8-38.2-6.4-31.5-6.9-36.3-6.8-37.4,.9-.2,1.8-1.2,8.4-2.1,14.6-.9,6.2-1.5,11.5-1.2,11.8 .5,.5,19.6,6.2,31.7,9.5 24.8,6.7,48.9,12.3,73,17 11.3,2.2,23,4.5,26,5.2l5.5,1.2-.1,42.5c0,43.8-.7,55.5-5.5,97-5,44.2-14.4,93.6-22.8,120.5-1.5,4.9-3.8,12.4-5.1,16.5-15.5,52.2-37.8,101.7-65.3,144.7-7.3,11.5-9.1,13.1-10.2,9-.3-1.2-1-3.1-1.5-4.2-.4-1.1-2.9-7.4-5.5-14-6.5-16.8-8.6-20.7-10.6-20.2-.9,.2-8.1,4.8-16,10-13.8,9.2-31.2,17.8-41.8,20.7-2.3,.6-4.4,1.7-4.7,2.5-.3,.7,2.5,9.1,6.1,18.7 13.2,34,21.5,56.3,21.5,57.6 0,6.2-45.4,48-79.1,73-28.2,20.8-92.2,58.2-99.6,58.2-2.8,0-30.3-13.8-46.8-23.5-44.3-25.9-82.7-55.1-117.8-89.4-21.3-20.9-21-20.8-21.5-3.5-1,29,11.2,49,47.3,77.5 36.3,28.8,65.9,47.5,108.2,68.6 30.4,15.2,28.3,12.6,28.3,34.6 0,17.8-.1,17.7,8.1,14.2 28.3-11.9,48-21.6,72.4-35.8 38.3-22.1,39.6-23,75-49.7 92.3-69.5,161-160.6,205.5-272.5 23.2-58.2,40.3-128.8,48.4-200 7.4-63.9,8.4-136.5,3-206.8-.6-7.8-.9-9-2.8-9.7-1.1-.4-12.7-2.2-25.6-4-33.5-4.5-58.8-8.8-81.6-13.9l-8.1-1.8-1.2,6.1z",
  "M530.7,89.5c-15.3,10.9-36.4,24.1-55.2,34.5-8.2,4.6-15.2,8.5-15.4,8.7-.3,.2,.6,2.1,1.8,4.4 1.9,3.4,2.3,5.9,2.7,16.1 .2,6.6,.7,12.5,1,13 .4,.6,3.1,.8,6.3,.6 6-.4,15.6-4.7,45.8-20.7 8.7-4.5,14-6.7,15.3-6.4 3.3,.9,4.2,3.6,2.5,8.2-1.3,3.8-1.6,23.3-.4,24.6 .3,.2,2.2-.7,4.2-2 4.6-3.2,5.2-3.2,11.2,.9 13.8,9.2,55.1,31,77,40.6 7.7,3.3,14.9,6.5,16,7 21.1,9.9,107.1,39.2,108.5,37 .3-.5,1.2-5.2,1.9-10.2 .7-5.1,1.4-10.7,1.7-12.5 .5-3.7,3.8-6.5,6.8-5.8 51.2,11.3,62.3,13.2,67.5,11.6 3.9-1.1,6.1-4.2,6.1-8.5 0-1.2,.7-4.5,1.5-7.2 .8-2.7,1.8-7.6,2.1-11l.6-6.1-4.3-1.1c-2.4-.6-13.8-3.4-25.4-6.2-33.6-8.3-57.9-15.7-92.5-28-11.6-4.2-13-4.7-25-9.3-46.1-17.5-102.1-47.7-141-76-3.5-2.6-7.1-4.7-8-4.6-.8,0-6.8,3.8-13.3,8.4z",
  "M624,348c-42.2,5.9-82,26.4-98.2,50.7-5.2,7.7-8,13.1-9.8,18.7-1.4,4.5-1,5,5.2,6 3.2,.5,4.5,1.5,8.1,6.3 2.3,3.1,5.5,6.8,7.1,8.2 1.6,1.5,5.9,5.5,9.7,9 3.8,3.5,7.1,6.2,7.2,6 .2-.2,2-3,4.1-6.1 4.3-6.6,18.5-20.4,27.7-27 36-25.6,86.9-24.3,123.8,3.2 10.9,8.2,19.6,17.9,31,34.7 2.3,3.5,4.5,6.3,5,6.3 1,0,45.7-29.5,46.4-30.7 .6-1-3.2-7.5-11.2-19.3-14.7-21.6-50.3-48-76.8-56.9-24.6-8.2-58-12-79.3-9.1z",
  "M505.5,231.2c-2.2,5-11.7,29.5-17.9,46.3-3,8.2-8.5,22-12,30.5l-6.5,15.5 5.2,1.8c5.7,1.9,10.5,6.7,16.5,16.6 3.8,6.3,4,7.2,2.1,15.6-.7,3.3-1.4,9.4-1.4,13.7 0,4.2-.3,8-.7,8.4-.5,.4-.8,3-.8,5.9 0,5.5,1.4,7.4,10.2,13.8 4.6,3.4,5,3.4,7.2-.3 .9-1.7,4.7-6.5,8.2-10.7 8-9.3,8.1-9.5,15.5-29.6 3.3-8.9,7-18.9,8.2-22.2 3.3-8.8,4.6-8.3,9.6,3.9 6,14.8,5.6,14.4,12.2,11.1 14.7-7.5,46.5-17.5,55.6-17.5 1.2,0,2.4-.4,2.8-1 .7-1.1-6.3-20.8-21.9-62-6.4-16.8-12.7-33.2-13.9-36.5l-2.3-6-37.2-.3-37.2-.2-1.5,3.2z",
  "M692.7,514.2c-1,.6-1.2,50.5-.2,51.6 .1,.1,13.1,.2,28.9,.2 23.2,0,28.6,.3,28.6,1.4 0,3.7-8.5,23.7-13.4,31.4l-2.7,4.3 3.3,3.8c1.8,2.1,6,5.9,9.3,8.4 3.3,2.5,9.9,8.2,14.7,12.6 10,9.2,15.7,11.9,18.3,8.5 20.1-25.7,32.8-65.8,32.9-104.1 .1-9.4-.1-17.4-.4-17.6-.8-.8-118.1-1.3-119.3-.5z",
  "M629.8,417.5c-8.3,1.4-12.8,2.7-20.1,5.7-5.3,2.3-6.2,3.8-12.3,20.5-1.8,5.1-6.5,17.3-10.5,27-8.1,20.2-18.9,49.2-18.9,50.9 0,.6,2.1,2.6,4.7,4.4 7.1,5.1,29.5,28.5,32.3,33.8 2.3,4.3,9.1,9.6,11.3,8.9 .8-.2,3.6-7.1,19.9-48.2 10.6-27.1,26.8-70.1,27.5-73.1 .9-4.1-9.3-29.6-12.2-30.8-2.6-1-13.3-.5-21.7,.9z",
  "M666.2,482.7c-3.3,8.1-8.3,21-11.1,28.5-2.8,7.5-6.6,17-8.5,21-1.8,4-4,9.3-4.9,11.8-.8,2.5-2.5,7-3.7,10-3.6,9-3.8,8.3,17.4,62 7.2,18.2,6.6,17.8,20.5,13.5 15.7-4.9,27.8-12.8,38.1-24.9 9.2-10.7,9.6-11.6,8-16.6-2.6-7.8-1.6-7.4-24.2-8l-20.3-.5-.3-38c-.3-43.3-.4-42.3,4.1-42.7 2.3-.2,3.3-.8,3.4-2.3 .3-2.5-9.8-27.7-11.3-28.2-.6-.2-3.8,6.1-7.2,14.4z",
  "M912.5,850.6c-5.8,4.8-46.1,31.3-58.5,38.4-47,27-106.8,50.5-157.8,62.1-19.2,4.3-18.8,4.2-35.1,14.4-13.4,8.4-37.8,22.3-50.1,28.5-9.6,4.9-9.5,4.8-8.9,5.4 .5,.6,37.5-5.5,50.4-8.3 93-20.1,172.5-60,240.4-120.7 16.7-15,30.6-29,19.6-19.8z",
  "M179,855.1c63.2,60.9,140.1,104.7,223.4,127.4 20.9,5.7,79.2,17.7,84.5,17.5 1.5-.1-4.2-3.6-13.4-8.2-11-5.5-18.4-9.8-44.5-25.9l-17.5-10.8-17-3.6c-69.9-15-145.1-48.8-207.5-93.2-16.3-11.6-16.9-11.8-8-3.2z",
  "M515.5,419c-1.9,3.1-1.7,18.6,.3,26.6 3.2,12.8,17.6,50.4,19.3,50.4 .4,0,2-3.9,3.4-8.8 3.1-9.9,11.3-28.8,15.1-34.6l2.6-3.8-5.4-5.5c-2.9-3-6.6-6.2-8.1-7.1-1.6-.9-5-4.7-7.8-8.5-2.7-3.7-6-7.2-7.2-7.6-4.6-1.8-11.4-2.4-12.2-1.1z",
];

/**
 * The cell the login page's backdrop repeats, in CSS pixels, and the size the
 * mark is drawn at inside it.
 *
 * The spacing is the one the old dot grid used, so the texture keeps its
 * rhythm; the mark is small enough that at a normal zoom each cell reads as a
 * speck rather than as artwork, and only resolves into the shield on zooming
 * in. Most of the cell is therefore empty, which is what holds the marks apart.
 */
export const MARK_TILE_SIZE = 22;
const MARK_TILE_MARK_SIZE = 7;

/**
 * That cell as a CSS `url()`, to be used as a mask rather than as a background.
 *
 * A background image would have to carry its own colour, and this one sits on a
 * page that has two themes, so it would need a copy per theme and would miss
 * any later change to the grey ramp. As a mask it contributes only its shape:
 * the colour is whatever the element under it is painted with, which on the
 * login page is a `--foreground` tint like every other neutral there.
 *
 * Built once at module load rather than per render - the backdrop re-renders on
 * every keystroke in the form, and re-encoding ten kilobytes of path data each
 * time to arrive at the same string would be work for nothing.
 */
export const MARK_TILE_MASK: string = (() => {
  const inset = (MARK_TILE_SIZE - MARK_TILE_MARK_SIZE) / 2;
  // No fill on the paths: a mask reads alpha, and the default black is opaque.
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${MARK_TILE_SIZE}" height="${MARK_TILE_SIZE}" ` +
    `viewBox="0 0 ${MARK_TILE_SIZE} ${MARK_TILE_SIZE}">` +
    `<svg x="${inset}" y="${inset}" width="${MARK_TILE_MARK_SIZE}" height="${MARK_TILE_MARK_SIZE}" ` +
    `viewBox="${MARK_VIEW_BOX}">` +
    MARK_PATHS.map((d) => `<path d="${d}"/>`).join("") +
    `</svg></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
})();

/**
 * A `<linearGradient>`, as data rather than markup, so the same list can be
 * turned into an SVG string for the favicon and into JSX for ColorBrandMark.
 */
export interface MarkGradient {
  readonly id: string;
  readonly x1: number;
  readonly y1: number;
  readonly x2: number;
  readonly y2: number;
  readonly stops: readonly { readonly offset: number; readonly color: string }[];
}

/**
 * One gradient per path in MARK_PATHS, same order, from the source artwork's
 * own colours. `gradientUnits="userSpaceOnUse"` means these coordinates are
 * positions on the same 1039x1016 canvas the paths are drawn on, not
 * fractions of each path's own bounding box - so they still line up correctly
 * under MARK_VIEW_BOX's crop, exactly as the paths do.
 */
export const MARK_GRADIENTS: readonly MarkGradient[] = [
  {
    id: "g1",
    x1: -293.9,
    y1: 577.5,
    x2: 54.5,
    y2: -107.0,
    stops: [
      { offset: 0.0, color: "#46559a" },
      { offset: 0.21, color: "#425da2" },
      { offset: 0.36, color: "#3b6aaa" },
      { offset: 0.5, color: "#3483b4" },
      { offset: 0.64, color: "#3992bf" },
      { offset: 0.79, color: "#3d9dc6" },
      { offset: 1.0, color: "#46a8bf" },
    ],
  },
  {
    id: "g2",
    x1: 805.6,
    y1: 813.9,
    x2: 487.1,
    y2: 492.1,
    stops: [
      { offset: 0.0, color: "#f16c2e" },
      { offset: 0.21, color: "#f48433" },
      { offset: 0.36, color: "#eb9341" },
      { offset: 0.5, color: "#d89b52" },
      { offset: 0.64, color: "#d39f58" },
      { offset: 0.79, color: "#c9ad5f" },
      { offset: 1.0, color: "#abab69" },
    ],
  },
  {
    id: "g3",
    x1: 265.6,
    y1: -300.0,
    x2: 124.2,
    y2: -140.3,
    stops: [
      { offset: 0.0, color: "#a0bd80" },
      { offset: 0.21, color: "#8ebe89" },
      { offset: 0.36, color: "#80b990" },
      { offset: 0.5, color: "#6fb59c" },
      { offset: 0.64, color: "#5aafaa" },
      { offset: 0.79, color: "#59b4ba" },
      { offset: 1.0, color: "#58b8c1" },
    ],
  },
  {
    id: "g4",
    x1: 149.4,
    y1: -164.1,
    x2: 24.8,
    y2: -27.2,
    stops: [
      { offset: 0.0, color: "#7cbc85" },
      { offset: 0.21, color: "#6fb98b" },
      { offset: 0.36, color: "#66b794" },
      { offset: 0.5, color: "#55b29b" },
      { offset: 0.64, color: "#47ada2" },
      { offset: 0.79, color: "#3da4a4" },
      { offset: 1.0, color: "#35a0ab" },
    ],
  },
  {
    id: "g5",
    x1: 461.0,
    y1: -151.3,
    x2: 329.2,
    y2: -108.0,
    stops: [
      { offset: 0.0, color: "#6eb890" },
      { offset: 0.21, color: "#63b58f" },
      { offset: 0.36, color: "#52b29d" },
      { offset: 0.5, color: "#56b4a9" },
      { offset: 0.64, color: "#54b4b1" },
      { offset: 0.79, color: "#4dafb4" },
      { offset: 1.0, color: "#45a9b4" },
    ],
  },
  {
    id: "g6",
    x1: 66.4,
    y1: -82.9,
    x2: -7.2,
    y2: 9.0,
    stops: [
      { offset: 0.0, color: "#66b999" },
      { offset: 0.21, color: "#57b497" },
      { offset: 0.36, color: "#4db19c" },
      { offset: 0.5, color: "#43aea1" },
      { offset: 0.64, color: "#3ca7a4" },
      { offset: 0.79, color: "#39a2a6" },
      { offset: 1.0, color: "#369ea9" },
    ],
  },
  {
    id: "g7",
    x1: 115.0,
    y1: -115.9,
    x2: 21.7,
    y2: -21.8,
    stops: [
      { offset: 0.0, color: "#75bb94" },
      { offset: 0.21, color: "#66b795" },
      { offset: 0.36, color: "#58b298" },
      { offset: 0.5, color: "#4eb1a0" },
      { offset: 0.64, color: "#43aba4" },
      { offset: 0.79, color: "#3ba4a7" },
      { offset: 1.0, color: "#36a2ad" },
    ],
  },
  {
    id: "g8",
    x1: 32.2,
    y1: -30.7,
    x2: 119.4,
    y2: -113.7,
    stops: [
      { offset: 0.0, color: "#f49847" },
      { offset: 0.21, color: "#f79135" },
      { offset: 0.36, color: "#f79033" },
      { offset: 0.5, color: "#f5983f" },
      { offset: 0.64, color: "#f49e48" },
      { offset: 0.79, color: "#f59d44" },
      { offset: 1.0, color: "#f0a85a" },
    ],
  },
  {
    id: "g9",
    x1: 338.2,
    y1: 1036.2,
    x2: 355.9,
    y2: 1090.6,
    stops: [
      { offset: 0.0, color: "#6f8496" },
      { offset: 0.21, color: "#4c7090" },
      { offset: 0.36, color: "#37628a" },
      { offset: 0.5, color: "#3c678f" },
      { offset: 0.64, color: "#1c5183" },
      { offset: 0.79, color: "#1d5183" },
      { offset: 1.0, color: "#315f8b" },
    ],
  },
  {
    id: "g10",
    x1: -237.9,
    y1: 710.8,
    x2: -255.4,
    y2: 763.0,
    stops: [
      { offset: 0.0, color: "#7d92a4" },
      { offset: 0.21, color: "#4d7092" },
      { offset: 0.36, color: "#38638b" },
      { offset: 0.5, color: "#3a658e" },
      { offset: 0.64, color: "#1e5185" },
      { offset: 0.79, color: "#205386" },
      { offset: 1.0, color: "#325f8c" },
    ],
  },
  {
    id: "g11",
    x1: 150.1,
    y1: -97.3,
    x2: 184.1,
    y2: -119.3,
    stops: [
      { offset: 0.0, color: "#61a6b7" },
      { offset: 0.21, color: "#439eb3" },
      { offset: 0.36, color: "#3497ad" },
      { offset: 0.5, color: "#3199ad" },
      { offset: 0.64, color: "#2f9aac" },
      { offset: 0.79, color: "#2e9bac" },
      { offset: 1.0, color: "#359fad" },
    ],
  },
];

/**
 * Draw the mark into the browser tab.
 *
 * index.html ships no icon of its own. It cannot: a `<link rel="icon">` needs a
 * URL, and the panel's base path is not known when that file is built. Building
 * the icon here costs nothing on the wire - the paths are already in the bundle
 * - and the tab is blank for only as long as the app itself is.
 *
 * Coloured rather than the monochrome silhouette: a tab strip is small and
 * busy with other tabs, and the panel's own colours pick it out of that row
 * faster than a shape that has to be read against a theme-matched fill first.
 */
export function installFavicon(): void {
  const defs = MARK_GRADIENTS.map(
    (g) =>
      `<linearGradient id="${g.id}" gradientUnits="userSpaceOnUse" x1="${g.x1}" y1="${g.y1}" x2="${g.x2}" y2="${g.y2}">` +
      g.stops.map((s) => `<stop offset="${s.offset}" stop-color="${s.color}"/>`).join("") +
      `</linearGradient>`,
  ).join("");
  const paths = MARK_PATHS.map(
    (d, i) => `<path fill="url(#${MARK_GRADIENTS[i].id})" d="${d}"/>`,
  ).join("");
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${MARK_VIEW_BOX}">` +
    `<defs>${defs}</defs>${paths}</svg>`;

  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (link === null) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.append(link);
  }
  link.type = "image/svg+xml";
  // encodeURIComponent rather than base64: it keeps the digits legible in the
  // devtools element pane, and there is nothing here outside ASCII.
  link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
