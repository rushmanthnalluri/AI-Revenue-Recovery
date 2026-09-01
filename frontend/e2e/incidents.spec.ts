import { expect, test } from "@playwright/test";

import { switchToResearchLab } from "./environment";
import { readSeedState } from "./seed-state";

/**
 * Incidents register, env-scoped: the seeded incident is RESEARCH data, so
 * the real merchant register stays empty even with the seed present; the
 * Research Lab environment lists it and drills through to Incident
 * Intelligence (severity pill, stat band, diagnosis card, provenance chip).
 */
test("incidents list is env-scoped and opens the seeded research incident", async ({ page }) => {
  const seed = readSeedState();
  expect(seed.incidentId, "global setup must seed an incident").toBeTruthy();
  const incidentId = seed.incidentId as string;

  await page.goto("/incidents");
  await expect(page.getByRole("heading", { level: 1, name: "Incidents" })).toBeVisible();

  // Isolation: the real merchant register does not show the research seed.
  await expect(page.getByText("No incidents detected")).toBeVisible();
  await expect(page.locator("tr", { hasText: incidentId })).toHaveCount(0);

  // Switch to the Research Lab environment — the seeded incident lives there.
  await switchToResearchLab(page);
  const row = page.locator("tr", { hasText: incidentId });
  await expect(row).toBeVisible();
  await expect(page.locator('tr[role="link"]').first()).toBeVisible();
  await row.click();

  await page.waitForURL(`**/incidents/${incidentId}`);

  // Severity pill (one of the four severities) in the detail header.
  await expect(
    page.locator("header").getByText(/^(LOW|MEDIUM|HIGH|CRITICAL)$/),
  ).toBeVisible();

  // Provenance chip — this is synthetic research data, labeled as such.
  await expect(page.getByText(/Synthetic Research Dataset/).first()).toBeVisible();

  // Metric stat band.
  for (const label of ["Deviation", "Affected payments", "Revenue at risk"]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }

  // Diagnosis card (model output + ranked alternatives).
  await expect(page.getByText("Diagnosis", { exact: true })).toBeVisible();
});
