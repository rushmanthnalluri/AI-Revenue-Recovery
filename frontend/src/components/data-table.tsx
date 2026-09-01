import * as React from "react";

import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface ColumnDef<T> {
  key: string;
  header: React.ReactNode;
  render: (row: T) => React.ReactNode;
  className?: string;
}

interface DataTableProps<T> {
  columns: ColumnDef<T>[];
  rows: T[] | undefined;
  getRowId: (row: T) => string;
  isLoading?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  /** Row click handler — rows become keyboard-focusable buttons. */
  onRowClick?: (row: T) => void;
  /**
   * ARIA role for clickable rows: "link" when the click navigates (default),
   * "button" when it opens an in-page layer such as a drawer or dialog.
   */
  rowRole?: "link" | "button";
  skeletonRows?: number;
  className?: string;
}

/**
 * Dense data table with built-in loading skeleton and empty state.
 * Error states are handled by the caller via <ErrorPanel /> so each page can
 * place the error where it makes sense.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowId,
  isLoading = false,
  emptyTitle = "Nothing here yet",
  emptyDescription,
  onRowClick,
  rowRole = "link",
  skeletonRows = 5,
  className,
}: DataTableProps<T>) {
  if (isLoading) {
    return (
      <div className={cn("space-y-2", className)} aria-busy="true" aria-label="Loading">
        <Skeleton className="h-9 w-full" />
        {Array.from({ length: skeletonRows }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} className={className} />;
  }

  return (
    <Table className={className}>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {columns.map((col) => (
            <TableHead key={col.key} className={col.className}>
              {col.header}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          const id = getRowId(row);
          const clickable = Boolean(onRowClick);
          return (
            <TableRow
              key={id}
              tabIndex={clickable ? 0 : undefined}
              role={clickable ? rowRole : undefined}
              className={cn(clickable && "cursor-pointer")}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={
                onRowClick
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
            >
              {columns.map((col) => (
                <TableCell key={col.key} className={col.className}>
                  {col.render(row)}
                </TableCell>
              ))}
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
