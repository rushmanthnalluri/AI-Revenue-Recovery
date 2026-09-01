import { expect, test } from "@playwright/test";

import { switchToResearchLab } from "./environment";
import { readSeedState } from "./seed-state";

/**
 * Recovery: pipeline lists the opportunities built from the seeded incident;
 * the Approval Center holds the policy-gated PENDING_APPROVAL action created
 * in global setup — approve it with a note and watch it leave the queue and
 * land in the APPROVED slice of the pipeline.
 */
test("recovery pipeline and human approval flow", async ({ page }) => {
  const seed = readSeedState();
  expect(
    seed.pendingOpportunityId,
    "global setup must create a PENDING_APPROVAL action",
  ).toBeTruthy();
  const opportunityId = seed.pendingOpportunityId as string;

  await page.goto("/recovery");
  await expect(page.getByRole("heading", { level: 1, name: "Recovery" })).toBeVisible();

  // The seeded opportunities are research data — switch the environment.
  await switchToResearchLab(page);

  // Pipeline tab (default): seeded opportunities render as clickable rows.
  await expect(page.getByText("Recovery pipeline", { exact: true })).toBeVisible();
  await expect(page.locator('tr[role="link"]').first()).toBeVisible();

  // Approval center: the pending card for our opportunity.
  await page.getByRole("tab", { name: /approval center/i }).click();
  const card = page.locator("article", { hasText: opportunityId });
  await expect(card).toBeVisible();
  await expect(card.getByText("PENDING APPROVAL", { exact: true })).toBeVisible();

  // Approve with a decision note.
  await card.getByLabel(/decision note/i).fill("e2e: policy gate satisfied, approving");
  await card.getByRole("button", { name: "Approve", exact: true }).click();

  // The settled card leaves the awaiting-decision queue…
  await expect(card).toHaveCount(0, { timeout: 30_000 });

  // …and the opportunity now sits in the APPROVED slice of the pipeline.
  await page.getByRole("tab", { name: "Pipeline" }).click();
  await page.getByLabel("Filter by status").selectOption("APPROVED");
  const approvedRow = page.locator("tr", { hasText: opportunityId });
  await expect(approvedRow).toBeVisible({ timeout: 30_000 });
  await expect(approvedRow.getByText("APPROVED", { exact: true })).toBeVisible();

  // Build affordance on Incident Intelligence — the UI path that replaced
  // the old curl step. Global setup already built this incident's
  // opportunities, so this two-step build is an idempotent re-run:
  // everything must come back under "already existed", never duplicated.
  const incidentId = seed.incidentId as string;
  expect(incidentId, "global setup must seed an incident").toBeTruthy();
  await page.goto(`/incidents/${incidentId}`);
  const buildTrigger = page.getByRole("button", { name: "Build recovery opportunities" });
  await expect(buildTrigger).toBeVisible();
  await buildTrigger.click();
  await expect(page.getByText(/idempotent/i)).toBeVisible();
  await page.getByRole("button", { name: "Confirm build" }).click();
  const buildResult = page.getByRole("status").filter({ hasText: "Build complete" });
  await expect(buildResult).toContainText("already existed", { timeout: 60_000 });
  await expect(
    buildResult.getByRole("link", { name: /open recovery pipeline/i }),
  ).toBeVisible();
});
