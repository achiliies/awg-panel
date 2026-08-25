/*
 * The panel's icon vocabulary, in one file.
 *
 * Every glyph the interface draws is named here and nowhere else. Two screens
 * that mean the same thing therefore cannot drift onto two different glyphs,
 * and swapping a choice later is one line here rather than a grep across the
 * pages. Nothing outside this module imports from the icon package directly.
 *
 * Phosphor draws each glyph in six weights, and the panel spends four of them
 * as a hierarchy rather than as decoration:
 *
 *   duotone   Identity. The page header, a nav item that is current, the head
 *             of a section card. Two tones, so it carries the accent colour and
 *             sits a layer above the text beside it.
 *   fill      State the operator did not ask for: a toast, a live badge, the
 *             mark on a supported/unsupported row. Solid shapes survive at the
 *             sizes state marks are drawn.
 *   bold      Anything under about 14px, where a hairline stroke thins out to
 *             nothing on a low-DPI screen, plus the sort arrows in a table head.
 *   regular   Everything else, which is most of it: buttons, menu rows, form
 *             affordances. The quiet default that the three above stand out of.
 *
 * Weight is a prop, so a call site that needs to break the rule can, but the
 * rule is the reason a screen reads as one thing rather than eighty.
 */

import * as React from "react";
import { IconBase, type Icon, type IconProps, type IconWeight } from "@phosphor-icons/react";

export type { Icon, IconProps, IconWeight } from "@phosphor-icons/react";

export {
  /* Navigation and page identity. */
  SquaresFour, // Dashboard
  Users, // Clients
  Faders, // Server config
  GearSix, // Settings
  Code, // The API: what a script talks to rather than what a person clicks
  Info, // About, and inline explanations
  Heart, // Support the project. Drawn filled: an outline reads as "favourite"
  GithubLogo, // Where the project itself lives, which is not on this machine
  List, // The drawer toggle below md

  /* State and feedback. */
  Warning, // The triangle: a risk the operator should read before acting
  WarningCircle, // A toast that failed
  CheckCircle, // A toast that worked, a supported feature
  XCircle, // An unsupported feature
  Question, // Support for a feature could not be determined
  Check,
  X,
  Tray, // Empty state: nothing here yet, which is normal on a fresh install
  // No spinner glyph here: components/ui/spinner.tsx draws its own ring, so
  // that the track behind the arc and the boot copy in index.html can be the
  // same two circles.

  /* Reachability. The tunnel's whole job, so these carry real weight. */
  WifiHigh, // Peers with a recent handshake
  WifiSlash, // The browser cannot reach the panel at all
  Plugs, // The collector stopped writing; the numbers on screen are stale
  Network,
  Globe,

  /*
   * Traffic and direction.
   *
   * The clients table sets three of these side by side - what a peer is moving
   * this second, what it has moved altogether, and how fast it is allowed to
   * move - and one pair of arrows on all three left every figure to be traced
   * back up to its column head before it said anything.
   *
   * Two of the three are told apart by their glyph. The arrow is cumulative
   * transfer, wherever it appears: the clients table's Usage column, the
   * dashboard's traffic splits, the statistics page's per-client totals. The
   * caret is a live rate, and it is the lighter of the two on purpose - a rate
   * is a needle that moves, a total is a quantity that has piled up.
   *
   * The third is not a glyph at all. A speed limit is neither of those things -
   * it is a number nobody is measuring, and the row it sits on says down and up
   * twice already - so that column writes the two words out. See
   * SpeedLimitCell: an arrow there was a third pair to learn for the one column
   * that has the room to say what it means.
   */
  ArrowUp, // Uploaded, and sorted ascending
  ArrowDown, // Downloaded, and sorted descending
  ArrowRight,
  ArrowsDownUp, // Sortable, currently unsorted
  ChartBar, // Traffic by day and by month, on the dashboard and per client
  Speedometer, // How fast a client may go, which is a limit rather than a load
  TrendUp, // A stat that moved up since the last window
  TrendDown,
  CaretUp, // Uploading now, and a disclosure that is open
  CaretDown, // Downloading now, and a disclosure that is shut
  CaretLeft, // Back a page, in a list that is walked rather than scrolled
  CaretRight,

  /* What the machine is spending. */
  Cpu,
  Memory,
  Swap, // Disk standing in for memory, which is what swapping is
  Gauge,
  HardDrive, // What the disks are doing, which is not the same as HardDrives
  Database, // How full the disk is, which is not what it is doing
  ChartPieSlice, // The split between the kernel module and the panel itself

  /* Actions. */
  Plus,
  MagnifyingGlass,
  Funnel, // Narrowing a list by state, and a list narrowed down to nothing
  Copy,
  DownloadSimple,
  UploadSimple,
  FolderOpen, // Pick a file off the machine the browser is running on
  FloppyDisk, // Save
  PencilSimple, // Rename
  Shuffle, // Draw another random name, where the panel suggested the one in the box
  Trash,
  DotsThree, // The row menu
  ArrowsClockwise, // Retry a failed request
  ArrowClockwise, // Restart the service
  ArrowCounterClockwise, // Reset a client's counters
  ArrowUUpLeft, // Put one field back to its default
  Broom, // Clear out whatever is no longer wanted, in one sweep
  Eraser, // Empty a whole group of settings at once
  ArrowSquareOut, // Leaves the panel
  QrCode,
  Note, // A client's free-text note
  Power, // Enable, and "the interface is switched off"
  Prohibit, // Disable

  /* Who is asking, and what proves it. */
  SignIn,
  SignOut,
  UserCircle,
  Devices, // The list of browsers signed in to this account
  Monitor, // One of them, on a computer
  DeviceMobile, // One of them, on a phone or a tablet
  LockKey, // The password field on the login card
  Lock, // TLS
  Key, // Keypairs, both the server's and a peer's
  ShieldCheck, // The authentication tab, and the recommended preset
  Eye,
  EyeSlash,

  /* The server's configuration, and the words about it. */
  BookOpenText, // The parameter help drawer, and a worked example to follow
  BracketsCurly, // A JSON body, and the machine-readable API description
  Lightning, // The shortest path to a working request
  Signpost, // The endpoint reference: the routes on offer, and where each leads
  TerminalWindow, // A command to paste into a shell
  Package, // Versions: what this build is and what it can do
  Circuitry, // The kernel module, which is the one part that is not a program
  TreeStructure, // The files on disk that are the source of truth
  HardDrives, // The server itself
  MaskHappy, // Obfuscation: traffic dressed up as another protocol
  SlidersHorizontal, // Advanced parameters
  // The two claims the About page makes that no other page has a glyph for.
  BatteryHigh, // A phone that sleeps between packets, not the server's power draw
  SealCheck, // Cryptography left exactly as it was found
  Archive, // Backup and restore
  Clock, // A time of day, and the field that takes one
  CalendarBlank, // A date, and the button that opens a calendar to pick one

  /* Chrome: language and theme. */
  Translate,
  Desktop, // Follow the system theme
  Sun,
  Moon,
} from "@phosphor-icons/react";

/*
 * ListDots: statistics and logs.
 *
 * The one glyph here that Phosphor does not draw. A bar chart said "numbers"
 * and nothing about the log beside them, and that page is both: a run of
 * entries, each a marker and a line of record. So it is three rows, each a dot
 * and a rule, which is what a log looks like from far enough away.
 *
 * It is built on Phosphor's own IconBase rather than as a loose <svg>, so it
 * takes the same 256-unit box, the same `size`/`color`/`mirrored` props and the
 * same context defaults as every glyph above, and answers to the weight
 * hierarchy this file documents. That last part is why the drawing is generated
 * from a table instead of six hand-written path strings: a weight is a rule
 * thickness and a dot radius, and nothing else varies between them.
 *
 * The dots run heavier against the rules than Phosphor's own list glyphs do,
 * which is the drawing this was traced from and is what keeps it distinct from
 * ListBullets at 16px in the rail.
 */

/** Baselines of the three rows, centres of both the dot and the rule. */
const LIST_DOTS_ROWS = [64, 128, 192] as const;
/** The rule: from `x` to the right edge of the box, less the usual padding. */
const LIST_DOTS_RULE_X = 83;
const LIST_DOTS_RULE_WIDTH = 144;
/** Centre of the dot column, left of where the rules begin. */
const LIST_DOTS_DOT_X = 41;

const LIST_DOTS_SIZES: Record<IconWeight, { rule: number; dot: number }> = {
  thin: { rule: 8, dot: 12 },
  light: { rule: 12, dot: 13 },
  regular: { rule: 16, dot: 14 },
  bold: { rule: 24, dot: 17 },
  fill: { rule: 24, dot: 18 },
  duotone: { rule: 16, dot: 14 },
};

function listDotsGlyph(weight: IconWeight): React.ReactElement {
  const { rule, dot } = LIST_DOTS_SIZES[weight];
  const parts: React.ReactElement[] = [];

  // Duotone is a flat wash under the drawing, the way every Phosphor duotone
  // is: the block the rules run across, at the accent colour, with the outline
  // on top of it. It spans the first rule's centre to the last one's.
  if (weight === "duotone") {
    parts.push(
      React.createElement("rect", {
        key: "wash",
        x: LIST_DOTS_RULE_X,
        y: LIST_DOTS_ROWS[0],
        width: LIST_DOTS_RULE_WIDTH,
        height: LIST_DOTS_ROWS[LIST_DOTS_ROWS.length - 1] - LIST_DOTS_ROWS[0],
        opacity: "0.2",
      }),
    );
  }

  for (const y of LIST_DOTS_ROWS) {
    // No `fill` on either shape: they inherit the one IconBase puts on the
    // <svg>, so the `color` prop and a text-colour class both still reach them.
    parts.push(
      React.createElement("circle", { key: `dot-${y}`, cx: LIST_DOTS_DOT_X, cy: y, r: dot }),
      React.createElement("rect", {
        key: `rule-${y}`,
        x: LIST_DOTS_RULE_X,
        y: y - rule / 2,
        width: LIST_DOTS_RULE_WIDTH,
        height: rule,
        rx: rule / 2,
      }),
    );
  }

  return React.createElement(React.Fragment, null, ...parts);
}

const LIST_DOTS_WEIGHTS = new Map<IconWeight, React.ReactElement>(
  (Object.keys(LIST_DOTS_SIZES) as IconWeight[]).map((weight) => [weight, listDotsGlyph(weight)]),
);

export const ListDots: Icon = React.forwardRef<SVGSVGElement, IconProps>((props, ref) =>
  React.createElement(IconBase, { ref, ...props, weights: LIST_DOTS_WEIGHTS }),
);
ListDots.displayName = "ListDots";
