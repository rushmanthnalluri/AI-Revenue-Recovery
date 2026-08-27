import { expect, test } from "@playwright/test";

import { readSeedState } from "./seed-state";

/**
 * Evaluation lab: the stored runs table renders, and selecting the seeded
 * run opens its stored metric cards (detection quality with real figures).
 */
test("evaluation lab lists stored runs and renders run metrics", async ({ page }) => {
  const seed = readSeedState();
  expect(
    seed.evaluationRunName,
    "global setup must store an evaluation run",
  ).toBeTruthy();

  await page.goto("/evaluation");
  await expect(page.getByRole("heading", { level: 1, name: "Evaluation Lab" })).toBeVisible();

  const row = page.locator('tr[role="link"]', { hasText: seed.evaluationRunName as string });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await expect(row.getByText("completed", { exact: true })).toBeVisible();

  // Select the run (keyboard path is the contract for these rows).
  await row.press("Enter");
  await page.waitForURL("**/evaluation?run=*");

  // Stored metric cards render from the persisted payload.
  await expect(page.getByText("Detection quality", { exact: true })).toBeVisible();
  await expect(page.getByText("Diagnosis accuracy", { exact: true })).toBeVisible();
  await expect(page.getByText("Intervention cost", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("img", { name: /detection precision/i }),
  ).toBeVisible();
});
