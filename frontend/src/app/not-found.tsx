import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex flex-col items-start gap-3 py-16">
      <p className="text-sm font-semibold text-foreground">404 — page not found</p>
      <p className="text-xs text-muted-foreground">
        This console route does not exist. Head back to the Command Center.
      </p>
      <Link href="/" className={buttonVariants({ variant: "outline", size: "sm" })}>
        Back to Command Center
      </Link>
    </div>
  );
}
