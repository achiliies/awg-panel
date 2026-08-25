import * as React from "react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/EmptyState";
import { Funnel, MagnifyingGlass } from "@/lib/icons";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { OperationRow } from "@/components/apidocs/OperationRow";
import { Prose } from "@/components/apidocs/Prose";
import { cn } from "@/lib/utils";
import { groupText, operationText } from "@/lib/apiText";
import type { ApiOperation, ApiSpec } from "@/api/types";

/*
 * Every endpoint the panel serves, grouped the way the API groups them.
 *
 * Forty-odd operations is more than anybody scrolls, so the page is built
 * around finding one rather than reading all of them. The search runs over the
 * path, the method, the summary and the description at once, because the three
 * ways somebody arrives here are knowing the URL, knowing the verb, and knowing
 * only what they want to do - "quota", "restart", "expired".
 *
 * A match opens the row it is in. Searching for a word that appears in a
 * description and being shown a collapsed row that does not visibly contain it
 * reads as a broken filter, and closing them again is one click.
 */

export interface EndpointReferenceProps {
  spec: ApiSpec;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

export function EndpointReference({ spec, text }: EndpointReferenceProps): JSX.Element {
  const { t } = useTranslation();
  const [query, setQuery] = React.useState("");
  const [group, setGroup] = React.useState<string>("");

  const needle = query.trim().toLowerCase();

  /** Does this operation match what was typed, in any of the four fields? */
  const matches = React.useCallback(
    (operation: ApiOperation): boolean => {
      if (needle === "") {
        return true;
      }
      const words = operationText(t, operation);
      return [
        operation.method,
        operation.path,
        operation.id,
        words.summary,
        words.description,
      ].some((field) => field.toLowerCase().includes(needle));
    },
    [needle, t],
  );

  const groups = spec.groups
    .filter((entry) => group === "" || entry.name === group)
    .map((entry) => ({ ...entry, operations: entry.operations.filter(matches) }))
    .filter((entry) => entry.operations.length > 0);

  const found = groups.reduce((count, entry) => count + entry.operations.length, 0);
  // Without the trailing slash: OperationRow joins a path onto it.
  const baseUrl = spec.serverUrl.replace(/\/$/, "");

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative min-w-0 flex-1">
          <MagnifyingGlass
            aria-hidden="true"
            className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="search"
            value={query}
            spellCheck={false}
            className="ps-9"
            placeholder={text("api.searchPlaceholder", "Search paths, verbs and words")}
            aria-label={text("api.search", "Search the reference")}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <p className="shrink-0 text-sm text-muted-foreground" aria-live="polite">
          {text("api.matchCount", "{{found}} of {{total}}", {
            found,
            total: spec.operations.length,
          })}
        </p>
      </div>

      {/* Chips rather than a select: there are eight groups, the whole set fits
          on two rows, and which one is current is then a fact on screen rather
          than something to open a menu to find out. */}
      <div className="flex flex-wrap gap-1.5">
        <GroupChip
          label={text("api.allGroups", "Everything")}
          count={spec.operations.length}
          active={group === ""}
          onClick={() => setGroup("")}
        />
        {spec.groups.map((entry) => (
          <GroupChip
            key={entry.name}
            label={groupText(t, entry).name}
            count={entry.operations.length}
            active={group === entry.name}
            onClick={() => setGroup(group === entry.name ? "" : entry.name)}
          />
        ))}
      </div>

      {groups.length === 0 ? (
        <EmptyState
          icon={Funnel}
          title={text("api.noMatches", "Nothing matches that")}
          description={text(
            "api.noMatchesHint",
            'Try a path, a verb, or a word from what you are trying to do - "quota", "restart", "expired".',
          )}
        />
      ) : (
        groups.map((entry) => {
          const words = groupText(t, entry);
          return (
            <Card key={entry.name}>
              <CardHeader>
                <CardTitle>{words.name}</CardTitle>
                {/* Prose rather than CardDescription, which is a <p>: a group's
                    words come from the document and may be more than one. */}
                <Prose text={words.description} />
              </CardHeader>
              <CardContent>
                <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
                  {entry.operations.map((operation) => (
                    <OperationRow
                      key={operation.id}
                      operation={operation}
                      baseUrl={baseUrl}
                      forceOpen={needle !== ""}
                      text={text}
                    />
                  ))}
                </ul>
              </CardContent>
            </Card>
          );
        })
      )}
    </div>
  );
}

interface GroupChipProps {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}

function GroupChip({ label, count, active, onClick }: GroupChipProps): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium",
        "transition-colors focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background",
        active
          ? "border-transparent bg-primary text-primary-foreground"
          : "border-border bg-background text-muted-foreground hover:bg-muted",
      )}
    >
      {label}
      <span className={cn("tabular-nums", active ? "opacity-80" : "opacity-70")}>{count}</span>
    </button>
  );
}
