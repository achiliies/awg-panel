import * as React from "react";

import { CopyButton } from "@/components/CopyButton";
import { cn } from "@/lib/utils";

/*
 * A block of something meant to be copied rather than read: a shell command, a
 * request body, an answer.
 *
 * Every one of them gets a copy button, because the whole point of this page is
 * that somebody leaves it with a working command rather than a retyped
 * approximation of one. The label above says what the block is - "Request",
 * "202 Accepted", "curl" - so a reader scanning down a long endpoint knows
 * which of the four blocks they are looking at without reading the contents.
 *
 * It scrolls sideways and never wraps. A curl line with a URL in it wraps into
 * four ragged lines that no longer look like a command, and a JSON body that
 * reflows loses the indentation that was the reason it is shown at all.
 */

export interface CodeBlockProps {
  code: string;
  /** What this block is. Rendered above it, in small caps-ish muted text. */
  label?: string;
  /** Something to say beside the label - a media type, a status word. */
  note?: React.ReactNode;
  /** Off for a block nobody would paste, e.g. a placeholder for a binary body. */
  copy?: boolean;
  className?: string;
}

export function CodeBlock({
  code,
  label,
  note,
  copy = true,
  className,
}: CodeBlockProps): JSX.Element {
  return (
    <div className={cn("space-y-1.5", className)}>
      {label || note ? (
        <div className="flex flex-wrap items-center gap-2">
          {label ? (
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {label}
            </span>
          ) : null}
          {note}
        </div>
      ) : null}
      <div className="relative rounded-md border border-border bg-muted/50">
        {copy ? (
          // Floated over the code rather than in a row above it: the block is
          // often only two lines, and a header bar per block turned a page of
          // examples into a page of chrome.
          <div className="absolute end-1.5 top-1.5 z-10">
            <CopyButton value={code} />
          </div>
        ) : null}
        <pre className="overflow-x-auto p-3 pe-12 text-xs leading-relaxed">
          <code className="font-mono">{code}</code>
        </pre>
      </div>
    </div>
  );
}
