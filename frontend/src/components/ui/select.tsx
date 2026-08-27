import * as React from "react";

import { cn } from "@/lib/utils";

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

/** Same treatment as Input — bg-bg, strong hairline, mono 13px, amber focus. */
function Select({ className, children, ...props }: SelectProps) {
  return (
    <select
      className={cn(
        "flex h-9 w-full appearance-none rounded-md border border-border-strong bg-bg px-3 py-2 font-mono text-[13px] text-text transition-colors duration-150 ease-apple hover:border-text-3 focus:border-accent disabled:cursor-not-allowed disabled:opacity-60",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}

export { Select };
