import * as React from "react";

import { cn } from "@/lib/utils";

/*
 * The small amount of Markdown the API's descriptions are written in.
 *
 * Blank-line paragraphs, `code`, and **bold**. That is the whole grammar, and
 * it is deliberately not more: the alternative is a Markdown library in the
 * bundle to render sentences the panel itself wrote, and every feature it would
 * add - images, links, raw HTML - is one this page has no use for and would
 * then have to sanitise.
 *
 * Nothing here interprets HTML. The text is split on the two markers and the
 * pieces become React elements, so a description containing a stray angle
 * bracket is rendered as that character rather than as markup, whatever it is
 * next to.
 */

export interface ProseProps {
  text: string;
  className?: string;
  /** Muted by default, which is right inside a card. Set for a lead paragraph. */
  tone?: "muted" | "normal";
}

export function Prose({ text, className, tone = "muted" }: ProseProps): JSX.Element | null {
  const paragraphs = text.split(/\n{2,}/).filter((part) => part.trim() !== "");
  if (paragraphs.length === 0) {
    return null;
  }

  return (
    <div
      className={cn(
        "space-y-2 text-sm leading-relaxed",
        tone === "muted" ? "text-muted-foreground" : "text-foreground",
        className,
      )}
    >
      {paragraphs.map((paragraph, index) => (
        // Index keys: this is a static split of one immutable string, so the
        // list has no identity beyond its order and nothing is ever reordered.
        <p key={index}>{inline(paragraph.replace(/\n/g, " "))}</p>
      ))}
    </div>
  );
}

/** `code` and **bold**, in one pass so neither can swallow the other. */
function inline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    if (match[1] !== undefined) {
      parts.push(
        <code
          key={match.index}
          className="rounded bg-muted px-1 py-0.5 font-mono text-[0.8125em] text-foreground"
        >
          {match[1]}
        </code>,
      );
    } else {
      parts.push(
        <strong key={match.index} className="font-medium text-foreground">
          {match[2]}
        </strong>,
      );
    }
    last = pattern.lastIndex;
  }

  if (last < text.length) {
    parts.push(text.slice(last));
  }
  return parts;
}
