import { expect, test } from "@playwright/test";

/**
 * Audit trail, env-scoped: the default scope is the real merchant trail
 * (research rows — scenario runs, dataset resets — never leak in); switching
 * the environment filter to research shows the seeded rows with per-row
 * environment badges, and the entity-type filter narrows the stream. The
 * filter uses recovery_opportunity because global setup always writes those
 * rows (opportunity build), while incident-type rows only appear later (the
 * dashboard lazily audits revenue-at-risk refreshes on change), so they are
 * not a stable fixture.
 */
test("audit trail is env-scoped with row badges and entity-type filtering", async ({ page }) => {
  await page.goto("/audit");
  await expect(page.getByRole("heading", { level: 1, name: "Audit Trail" })).toBeVisible();

  // Default scope = real merchant trail; no research badges may appear.
  await expect(page.getByLabel("Filter by environment")).toHaveValue("real_test");
  await expect(page.locator('[title="Synthetic research data"]')).toHaveCount(0);

  // Switch to the research trail — the seeded rows live there.
  await page.getByLabel("Filter by environment").selectOption("research");

  // Rows render with mono, machine-readable timestamps.
  const timestamp = page.locator("time[datetime]").first();
  await expect(timestamp).toBeVisible();
  await expect(timestamp).toHaveClass(/font-mono/);
  await expect(timestamp).toHaveAttribute("datetime", /.+/);

  // Every row carries its environment badge.
  await expect(page.locator('[title="Synthetic research data"]').first()).toBeVisible();

  // Unfiltered total spans several entity types (opportunities, actions,
  // policy decisions…), so filtering must shrink it.
  const counter = page.getByText(/events · page \d+ of \d+/);
  await expect(counter).toBeVisible();
  const unfiltered = await counter.textContent();

  // Filter to recovery_opportunity — the counter changing proves the
  // filtered response arrived (keepPreviousData keeps stale rows otherwise).
  await page.getByLabel("Filter by entity type").selectOption("recovery_opportunity");
  await expect(counter).not.toHaveText(unfiltered ?? "", { timeout: 30_000 });

  // Every rendered row is now a recovery_opportunity chip…
  await expect(page.locator('span[title^="recovery_opportunity "]').first()).toBeVisible();
  // …and no other entity type leaks into the filtered stream.
  await expect(page.locator('a[title^="Open incident"]')).toHaveCount(0);
  await expect(page.locator('span[title^="recovery_action "]')).toHaveCount(0);
  await expect(page.locator('span[title^="policy_decision "]')).toHaveCount(0);
});
