import { expect, test } from "@playwright/test";

import { readSeedState } from "./seed-state";

/**
 * Incidents register: the seeded incident is listed and drills through to
 * Incident Intelligence (severity pill, stat band, diagnosis card).
 */
test("incidents list opens seeded incident detail", async ({ page }) => {
  const seed = readSeedState();
  expect(seed.incidentId, "global setup must seed an incident").toBeTruthy();
  const incidentId = seed.incidentId as string;

  await page.goto("/incidents");
  await expect(page.getByRole("heading", { level: 1, name: "Incidents" })).toBeVisible();

  // The seeded incident appears as a clickable row (role=link, id in the mono sub-line).
  const row = page.locator("tr", { hasText: incidentId });
  await expect(row).toBeVisible();
  await expect(page.locator('tr[role="link"]').first()).toBeVisible();
  await row.click();

  await page.waitForURL(`**/incidents/${incidentId}`);

  // Severity pill (one of the four severities) in the detail header.
  await expect(
    page.locator("header").getByText(/^(LOW|MEDIUM|HIGH|CRITICAL)$/),
  ).toBeVisible();

  // Metric stat band.
  for (const label of ["Deviation", "Affected payments", "Revenue at risk"]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }

  // Diagnosis card (model output + ranked alternatives).
  await expect(page.getByText("Diagnosis", { exact: true })).toBeVisible();
});
