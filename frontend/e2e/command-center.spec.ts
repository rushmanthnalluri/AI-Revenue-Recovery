import { expect, test } from "@playwright/test";

/**
 * Command Center: brand, KPI strip, and the revenue-at-risk hero showing a
 * real non-zero figure after the demo seed.
 */
test("command center renders brand, KPI strip and non-zero revenue at risk", async ({ page }) => {
  await page.goto("/");

  // Brand (sidebar tile + page kicker both carry the wordmark).
  await expect(page.getByText("PulseRecover", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Command Center" })).toBeVisible();

  // KPI strip — every cell label of the hairline band.
  for (const label of [
    "Recoverable revenue",
    "Recovered revenue",
    "Recovery rate",
    "Active incidents",
    "Payment success rate",
    "Recoveries in flight",
  ]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }

  // Revenue hero: big tabular figure, non-zero after seeding.
  const hero = page.getByRole("region", { name: "Revenue at risk" });
  await expect(hero).toBeVisible();
  const value = hero.locator("p", { hasText: "₹" }).first();
  await expect(value).toBeVisible();
  const text = (await value.textContent())?.trim() ?? "";
  expect(text).toMatch(/^₹[1-9][\d,]*/); // non-zero INR figure, en-IN grouping
});
