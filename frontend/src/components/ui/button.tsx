import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Buttons — spec: primary is flat amber (bg-accent, accent-ink text, 6px
 * radius, 13px medium); secondary is a hairline outline on transparent.
 * 150ms ease-apple transitions, no shadows, no pill shapes, no hover-lift.
 * Keyboard focus comes from the global 2px amber :focus-visible outline.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-[13px] font-medium transition-colors duration-150 ease-apple disabled:pointer-events-none disabled:opacity-60 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-accent text-accent-ink hover:bg-accent-hover",
        secondary: "border border-border-strong bg-transparent text-text hover:bg-surface",
        outline: "border border-border bg-transparent text-text-2 hover:bg-raised hover:text-text",
        ghost: "text-text-2 hover:bg-raised hover:text-text",
        destructive: "bg-danger text-[#21100E] hover:bg-danger/90",
        success: "border border-transparent bg-success-dim text-success hover:bg-success/20",
      },
      size: {
        default: "h-9 px-4",
        sm: "h-7 rounded-md px-2.5 text-xs",
        lg: "h-10 rounded-md px-6",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

function Button({ className, variant, size, type = "button", ...props }: ButtonProps) {
  return (
    <button type={type} className={cn(buttonVariants({ variant, size, className }))} {...props} />
  );
}

export { Button, buttonVariants };
