import { expect, test } from "@playwright/test";

import { readSeedState } from "./seed-state";

/**
 * The Evaluation Lab lives inside the Research Lab as a tab: the legacy
 * /evaluation route redirects there, the stored runs table renders, and
 * selecting the seeded run opens its stored metric cards (detection quality
 * with real figures). The scenario runner is the sibling tab.
 */
test("evaluation lab lives in the research lab and renders run metrics", async ({ page }) => {
  const seed = readSeedState();
  expect(
    seed.evaluationRunName,
    "global setup must store an evaluation run",
  ).toBeTruthy();

  await page.goto("/evaluation");

  // Legacy route redirects into the Research Lab's evaluation tab.
  await page.waitForURL("**/research?tab=evaluation**");
  await expect(page.getByRole("heading", { level: 1, name: "Research Lab" })).toBeVisible();
  await expect(page.getByText(/Research simulator/)).toBeVisible();
  await expect(page.getByRole("tab", { name: "Evaluation" })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  const row = page.locator('tr[role="link"]', { hasText: seed.evaluationRunName as string });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await expect(row.getByText("completed", { exact: true })).toBeVisible();

  // Select the run (keyboard path is the contract for these rows); the tab
  // param survives the selection deep link.
  await row.press("Enter");
  await page.waitForURL("**/research?tab=evaluation&run=*");

  // Stored metric cards render from the persisted payload.
  await expect(page.getByText("Detection quality", { exact: true })).toBeVisible();
  await expect(page.getByText("Diagnosis accuracy", { exact: true })).toBeVisible();
  await expect(page.getByText("Intervention cost", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("img", { name: /detection precision/i }),
  ).toBeVisible();

  // The scenario runner (moved out of the Command Center) is the sibling tab.
  await page.getByRole("tab", { name: "Scenarios" }).click();
  await expect(page.getByText("Scenario runner", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Reset research data/ })).toBeVisible();
});
