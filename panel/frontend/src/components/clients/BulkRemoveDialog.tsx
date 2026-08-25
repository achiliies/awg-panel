import * as React from "react";
import { useTranslation } from "react-i18next";
import { Trash } from "@/lib/icons";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { BulkRemoveOptions } from "@/api/types";

/*
 * Clearing out the clients that have stopped being clients.
 *
 * The two categories are the ones the server sweeps by and the list filters by,
 * so what is offered here can be looked at first: an operator who wants to know
 * which clients "disabled" means can pick that filter on the table behind this
 * dialog and see the rows.
 *
 * Nothing is removed from here. The button hands the selection back and the
 * page asks for confirmation with the number attached, because that number is
 * the whole of what makes this decision reviewable - "remove the disabled ones"
 * is an intention, "remove these six clients" is what will actually happen.
 */

export interface BulkRemoveCounts {
  /** Clients whose date has passed, minus any an admin has switched back on. */
  expired: number;
  /** Clients that are switched off, or over their data limit and about to be. */
  disabled: number;
  /**
   * What both options together take, which is the union rather than the sum:
   * a lapsed client the collector has already switched off is in both of the
   * numbers above and is removed once.
   */
  either: number;
}

export interface BulkRemoveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  counts: BulkRemoveCounts;
  /** A sweep is already running; the button stays shut until it settles. */
  pending?: boolean;
  /**
   * The operator has chosen. The page confirms before anything is deleted.
   *
   * The count goes with the selection rather than being worked out again on the
   * other side: the number in the confirmation has to be the number that was on
   * screen when the button was pressed, and the rule for which of the three
   * figures applies lives here.
   */
  onRemove: (options: BulkRemoveOptions, count: number) => void;
}

/** How many clients the chosen boxes cover. Zero when nothing is ticked. */
function selectedCount(options: BulkRemoveOptions, counts: BulkRemoveCounts): number {
  if (options.expired && options.disabled) {
    return counts.either;
  }
  if (options.expired) {
    return counts.expired;
  }
  return options.disabled ? counts.disabled : 0;
}

interface OptionRowProps {
  id: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: string;
  hint: string;
  count: number;
}

/** One tickable category: what it is, what it covers, and how many it holds. */
function OptionRow({
  id,
  checked,
  onCheckedChange,
  label,
  hint,
  count,
}: OptionRowProps): JSX.Element {
  const { t } = useTranslation();
  return (
    // The whole block is the label, so the hint and the count are part of what
    // the box is called rather than text sitting near it - which is also what
    // makes the second line clickable instead of a dead zone in the middle of
    // the row.
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 transition-colors hover:bg-accent/40"
    >
      <Checkbox
        id={id}
        checked={checked}
        onChange={(event) => onCheckedChange(event.target.checked)}
        className="mt-0.5"
      />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium">{label}</span>
          <span className="text-xs text-muted-foreground">
            {t("clients.bulkRemoveCount", { count })}
          </span>
        </span>
        <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">{hint}</span>
      </span>
    </label>
  );
}

export function BulkRemoveDialog({
  open,
  onOpenChange,
  counts,
  pending = false,
  onRemove,
}: BulkRemoveDialogProps): JSX.Element {
  const { t } = useTranslation();
  const [options, setOptions] = React.useState<BulkRemoveOptions>({
    expired: false,
    disabled: false,
  });

  // Both boxes start clear every time it opens. A destructive selection that
  // survives from the last visit is one an operator can act on without having
  // read it.
  React.useEffect(() => {
    if (open) {
      setOptions({ expired: false, disabled: false });
    }
  }, [open]);

  const total = selectedCount(options, counts);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("clients.bulkRemove")}</DialogTitle>
          <DialogDescription>{t("clients.bulkRemoveIntro")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <OptionRow
            id="bulk-remove-expired"
            checked={options.expired}
            onCheckedChange={(checked) =>
              setOptions((current) => ({ ...current, expired: checked }))
            }
            label={String(t("clients.bulkRemoveExpired"))}
            hint={String(t("clients.bulkRemoveExpiredHint"))}
            count={counts.expired}
          />
          <OptionRow
            id="bulk-remove-disabled"
            checked={options.disabled}
            onCheckedChange={(checked) =>
              setOptions((current) => ({ ...current, disabled: checked }))
            }
            label={String(t("clients.bulkRemoveDisabled"))}
            hint={String(t("clients.bulkRemoveDisabledHint"))}
            count={counts.disabled}
          />
        </div>

        {/* The number the two boxes add up to, which is not their sum wherever
            they overlap. It is here as well as in the confirmation so that
            ticking a box has a visible consequence before anything is
            committed to. */}
        <p aria-live="polite" className="text-sm text-muted-foreground">
          {total > 0
            ? t("clients.bulkRemoveSummary", { count: total })
            : t("clients.bulkRemoveNothing")}
        </p>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            variant="destructive"
            // Nothing ticked, or nothing to take: either way the sweep would be
            // a request that deletes nothing, and the server refuses the first
            // of them outright.
            disabled={total === 0 || pending}
            onClick={() => onRemove(options, total)}
          >
            <Trash aria-hidden="true" />
            {t("common.remove")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
