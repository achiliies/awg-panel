import * as React from "react";
import type { Icon } from "@/lib/icons";

import { cn } from "@/lib/utils";

/*
 * A calm block for something that needs doing, not an alarm.
 *
 * Used for the things a save leaves behind - clients to re-issue, a restart
 * that did not happen, whatever the config is unhappy about - which are jobs
 * rather than failures. The tone carries that: amber when the operator has to
 * act, teal when it is a fact about what just happened.
 */

export interface NoticeProps {
  icon: Icon;
  title: string;
  tone?: "warning" | "info";
  action?: React.ReactNode;
  children: React.ReactNode;
}

export function Notice({
  icon: Icon,
  title,
  tone = "warning",
  action,
  children,
}: NoticeProps): JSX.Element {
  return (
    <div
      role="status"
      className={cn(
        "rounded-lg border p-4 sm:p-5",
        tone === "warning" ? "border-warning/40 bg-warning/5" : "border-info/40 bg-info/5",
      )}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <span
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
              tone === "warning" ? "bg-warning/15 text-warning" : "bg-info/15 text-info",
            )}
          >
            <Icon weight="duotone" className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0 space-y-1.5">
            <p className="text-sm font-medium leading-tight">{title}</p>
            <div className="space-y-1.5 text-sm leading-relaxed text-muted-foreground">
              {children}
            </div>
          </div>
        </div>
        {action ? <div className="flex shrink-0 flex-wrap gap-2">{action}</div> : null}
      </div>
    </div>
  );
}
