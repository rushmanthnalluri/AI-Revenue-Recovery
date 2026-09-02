import * as React from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface SectionCardProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Right-aligned header actions (buttons, filters, links). */
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  contentClassName?: string;
}

/**
 * Titled card section — the standard content container for every screen.
 * The title is a real h2 (screen-reader section navigation); heavy action
 * clusters (filters + result strips) wrap BELOW the title instead of
 * squeezing it into a narrow column.
 */
export function SectionCard({
  title,
  description,
  actions,
  children,
  className,
  contentClassName,
}: SectionCardProps) {
  return (
    <Card className={className}>
      <CardHeader className="flex-col items-start gap-3 space-y-0 sm:flex-row sm:flex-wrap sm:justify-between">
        <div className="min-w-0 flex-1 space-y-1 sm:basis-64">
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        {actions ? (
          <div className="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
            {actions}
          </div>
        ) : null}
      </CardHeader>
      <CardContent className={contentClassName}>{children}</CardContent>
    </Card>
  );
}
