import { useTranslation } from "react-i18next";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ClientStatus } from "@/api/types";

/*
 * Purely presentational: the Clients table and the Dashboard both render this,
 * and neither passes anything but a status, so the component never reads a
 * query or knows which peer it is describing.
 *
 * Colour alone would fail a colour-blind operator and a monochrome screenshot,
 * so every state also differs in shape (filled, haloed, hollow) and always
 * carries its name in text.
 */

const DOT_CLASSES: Record<ClientStatus, string> = {
  online: "bg-success ring-4 ring-success/20",
  idle: "bg-info",
  offline: "bg-muted-foreground/50",
  disabled: "border-2 border-destructive bg-transparent",
  quota: "bg-warning ring-4 ring-warning/25",
};

/*
 * The word carries the same colour as the dot beside it. A green dot next to
 * plain grey text makes the reader do the join themselves, and in a table of
 * twenty rows the colour is what they are actually scanning.
 *
 * Offline is the one state left grey: it is the resting state of most peers,
 * and a column of coloured text says everything is worth looking at.
 */
const LABEL_CLASSES: Record<ClientStatus, string> = {
  online: "text-success",
  idle: "text-info",
  offline: "text-muted-foreground",
  disabled: "text-destructive",
  quota: "text-warning",
};

export interface StatusDotProps {
  status: ClientStatus;
  /** Overrides the translated state name. Use for a count, e.g. "3 online". */
  label?: string;
  className?: string;
}

export function StatusDot({ status, label, className }: StatusDotProps): JSX.Element {
  const { t } = useTranslation();
  const name = label ?? String(t(`status.${status}`));
  const help = String(t(`status.help.${status}`));

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          // Focusable so the explanation is reachable without a pointer; the
          // tooltip is the only place the state is defined in words.
          tabIndex={0}
          className={cn(
            "inline-flex max-w-full items-center gap-2 rounded-sm text-sm leading-tight",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            className,
          )}
        >
          <span
            aria-hidden="true"
            className={cn("inline-block h-2.5 w-2.5 shrink-0 rounded-full", DOT_CLASSES[status])}
          />
          <span className={cn("truncate font-medium", LABEL_CLASSES[status])}>{name}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>{help}</TooltipContent>
    </Tooltip>
  );
}
