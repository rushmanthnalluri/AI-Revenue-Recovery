import { redirect } from "next/navigation";

/**
 * The Evaluation Lab moved into the Research Lab as a tab. Keep the old URL
 * working: /evaluation → /research?tab=evaluation, preserving the ?run= deep
 * link used by stored-run selections.
 */
export default async function EvaluationRedirectPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const qs = new URLSearchParams();
  qs.set("tab", "evaluation");
  const run = params.run;
  if (typeof run === "string" && run) qs.set("run", run);
  redirect(`/research?${qs.toString()}`);
}
