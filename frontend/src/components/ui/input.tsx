import * as React from "react";

import { cn } from "@/lib/utils";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

/** Spec input: bg-bg, strong hairline, mono 13px; hover → text-3 border,
    focus → amber border (plus the global focus outline). No shadow. */
function Input({ className, type, ...props }: InputProps) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-9 w-full rounded-md border border-border-strong bg-bg px-3 py-2 font-mono text-[13px] text-text transition-colors duration-150 ease-apple placeholder:font-sans placeholder:text-text-3 hover:border-text-3 focus:border-accent disabled:cursor-not-allowed disabled:opacity-60 aria-[invalid=true]:border-danger aria-[invalid=true]:bg-danger-dim",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
