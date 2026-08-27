import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Badge / status pill — spec: mono 9.5px uppercase micro-label, rounded-sm,
 * hairline border; semantic variants use their dim washes, accent variant is
 * the amber wash. `rounded-full` is reserved for status dots.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-sm border px-[7px] py-[3px] font-mono text-[9.5px] uppercase tracking-[0.07em] transition-colors duration-150 ease-apple",
  {
    variants: {
      variant: {
        default: "border-border-strong bg-transparent text-text-2",
        secondary: "border-border-strong bg-transparent text-text-2",
        accent: "border-transparent bg-accent-dim text-accent",
        success: "border-transparent bg-success-dim text-success",
        warning: "border-transparent bg-accent-dim text-accent",
        danger: "border-transparent bg-danger-dim text-danger",
        info: "border-transparent bg-info-dim text-info",
        outline: "border-border-strong text-text-2",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
