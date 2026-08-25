import type { Client, LivePeer } from "@/api/types";

/**
 * The highest usage already shown for one client, and the reset it was under.
 *
 * Carried between renders so a figure that has been on screen cannot be taken
 * back off it - see `liveUsage` for the one moment that would otherwise happen.
 * The offsets travel with it because they are what makes the pair meaningful:
 * the same client under a different pair of offsets is a different question,
 * not a lower answer to this one.
 */
export interface UsageFloor {
  rx: number;
  tx: number;
  offsetRx: number;
  offsetTx: number;
}

/** Floors for a set of clients, keyed by public key. */
export type UsageFloors = ReadonlyMap<string, UsageFloor>;

/** What to remember about a row that has just been shown. */
export function usageFloor(client: Client, rx: number, tx: number): UsageFloor {
  return { rx, tx, offsetRx: client.offsetRx, offsetTx: client.offsetTx };
}

/**
 * One client's all-time totals, taken from the collector's live view when it
 * has one.
 *
 * The clients endpoint reads usage out of traffic.db, which the collector
 * rewrites every ten seconds; the live blob carries the same totals out of the
 * memory that file is written from, two seconds old. They are the same
 * accounting either way - and the fresher of the two is the one enforcement
 * acts on, so it is the one the bar under a client's name should be filling.
 *
 * The blob knows nothing about a counter having been cleared: it reports the
 * peer's whole life, and the record of what an admin cleared lives in the panel
 * database. The row carries that figure along for exactly this, so the
 * subtraction happens here rather than in the collector, which would learn
 * about a reset a minute late.
 *
 * Never lower than the endpoint's own answer, which covers the one way the two
 * can disagree. The collector's memory is seeded from traffic.db when it starts
 * and accumulated from its own polls after that, so anything the PreDown hook
 * sync` folded into the file in between is in the file and not in the memory.
 * Both are counters that only ever climb, so the higher is the later, and a
 * usage figure that walked backwards on screen would be the more alarming way to
 * be briefly wrong.
 *
 * Never lower than what has already been shown either, which covers the moment
 * that rule was written for and did not reach. A client that crosses its data
 * limit has its key revoked, and the blob describes only the peers the kernel is
 * carrying - so the peer this was reading vanishes from it in the same instant
 * the bar reaches the end, and the fallback below is the endpoint's figure from
 * a list poll up to thirty seconds old. Without a floor the bar fills, the limit
 * is enforced, and the number then drops back to where it was half a minute ago,
 * which reads as the enforcement having been imagined.
 *
 * The floor is dropped, not clamped against, when the offsets move. Reset usage
 * is the one thing that is *supposed* to take the figure back to nothing, and a
 * high water mark that outlived it would pin the client at its old total for as
 * long as the page stayed open - a worse bug than the flicker this prevents, and
 * a silent one.
 */
export function liveUsage(
  client: Client,
  peer: LivePeer | undefined,
  held?: UsageFloor,
): [rx: number, tx: number] {
  const floor =
    held && held.offsetRx === client.offsetRx && held.offsetTx === client.offsetTx
      ? held
      : undefined;
  // No peer: switched off, or never seen. The blob holds only the peers the
  // kernel is carrying this instant, and the client that just hit its limit is
  // the first one it stops mentioning.
  return [
    Math.max(peer ? peer.rx - client.offsetRx : 0, client.rxBytes, floor?.rx ?? 0),
    Math.max(peer ? peer.tx - client.offsetTx : 0, client.txBytes, floor?.tx ?? 0),
  ];
}
