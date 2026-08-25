import * as React from "react";

import { cn } from "@/lib/utils";

/*
 * The table owns its own horizontal scroller so a wide client list never makes
 * the page itself scroll sideways. Pages still swap to stacked cards below md;
 * this is the desktop fallback for very narrow windows.
 *
 * Separated borders, spaced by nothing, rather than the collapsed borders a
 * table like this would normally want. Collapsing them changes how the table is
 * painted: the cells go down in the table's own layer order, which has no place
 * for a z-index on a cell and no reliable one for a cell that has been taken out
 * of the flow. A column pinned to the end of the row with `position: sticky`
 * therefore lost its background under the browser, and the rest of the row
 * scrolled straight through it - the expiry of one client sitting on top of the
 * actions of another. Separating the borders puts the cells back to being
 * ordinary boxes that paint in order and honour z-index.
 *
 * The cost is that a `<tr>` can no longer draw a border - in this mode nothing
 * on a row paints one - so the rules between rows are drawn by the cells, which
 * is what TableRow and TableBody set up below. With no spacing between cells the
 * result is the line it always was.
 */
const Table = React.forwardRef<HTMLTableElement, React.TableHTMLAttributes<HTMLTableElement>>(
  ({ className, ...props }, ref) => (
    <div className="relative w-full overflow-x-auto">
      <table
        ref={ref}
        className={cn("w-full caption-bottom border-separate border-spacing-0 text-sm", className)}
        {...props}
      />
    </div>
  ),
);
Table.displayName = "Table";

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn("[&_tr>th]:border-b [&_tr>th]:border-border", className)}
    {...props}
  />
));
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn("[&_tr:last-child>td]:border-0", className)} {...props} />
));
TableBody.displayName = "TableBody";

const TableRow = React.forwardRef<HTMLTableRowElement, React.HTMLAttributes<HTMLTableRowElement>>(
  ({ className, ...props }, ref) => (
    <tr
      ref={ref}
      className={cn(
        // The rule under the row is drawn by its own cells: see Table above for
        // why the borders are separated, and why a border here would not paint.
        "transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted",
        "[&>td]:border-b [&>td]:border-border",
        className,
      )}
      {...props}
    />
  ),
);
TableRow.displayName = "TableRow";

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      "h-10 whitespace-nowrap px-3 text-start align-middle text-xs font-semibold uppercase tracking-wide text-muted-foreground",
      className,
    )}
    {...props}
  />
));
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td ref={ref} className={cn("px-3 py-2.5 align-middle", className)} {...props} />
));
TableCell.displayName = "TableCell";

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell };
