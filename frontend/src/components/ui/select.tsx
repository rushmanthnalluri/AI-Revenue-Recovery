import * as React from "react";

import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

function Select({ className, children, ...props }: SelectProps) {
  return (
    <div className="relative inline-flex items-center">
      <select
        className={cn(
          "h-9 w-full appearance-none rounded-md border border-border-strong bg-bg pl-3 pr-8 py-2 font-mono text-[13px] text-text transition-colors duration-150 ease-apple hover:border-text-3 focus:border-accent disabled:cursor-not-allowed disabled:opacity-60",
          className
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute right-2 size-3.5 text-text-3"
      />
    </div>
  );
}

export { Select };
