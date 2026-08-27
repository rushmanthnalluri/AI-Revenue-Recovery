import { expect, test } from "@playwright/test";

import { readSeedState } from "./seed-state";

/**
 * AI investigation: trigger the reasoner from the incident page and verify
 * the three honesty zones render — OBSERVED FACTS / AI INFERENCE /
 * RECOMMENDED ACTION. (If a previous suite run already stored a report for
 * this incident, the panel renders it directly and there is nothing to click.)
 */
test("AI investigation renders facts / inference / recommended action zones", async ({ page }) => {
  const seed = readSeedState();
  expect(seed.incidentId, "global setup must seed an incident").toBeTruthy();

  await page.goto(`/incidents/${seed.incidentId as string}`);

  const factsZone = page.getByRole("region", { name: /observed facts/i });
  const runButton = page.getByRole("button", { name: /run ai investigation/i });

  // Either the empty-state trigger button or an already-stored report.
  await expect(runButton.or(factsZone)).toBeVisible({ timeout: 60_000 });
  if (await runButton.isVisible()) {
    await runButton.click();
  }

  // The heuristic reasoner is offline but still takes a few seconds.
  await expect(factsZone).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("region", { name: /ai inference/i })).toBeVisible();
  await expect(page.getByRole("region", { name: /recommended action/i })).toBeVisible();

  // Zones carry real content, not empty shells.
  await expect(factsZone.locator("li").first()).toBeVisible();
});
