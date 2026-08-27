import { expect, test } from "@playwright/test";

/**
 * Audit trail: append-only rows with mono timestamps, and the entity-type
 * filter actually narrows the stream. The filter uses recovery_opportunity
 * because global setup always writes those rows (opportunity build), while
 * incident-type rows only appear later (the dashboard lazily audits
 * revenue-at-risk refreshes on change), so they are not a stable fixture.
 */
test("audit trail renders mono timestamps and filters by entity type", async ({ page }) => {
  await page.goto("/audit");
  await expect(page.getByRole("heading", { level: 1, name: "Audit Trail" })).toBeVisible();

  // Rows render with mono, machine-readable timestamps.
  const timestamp = page.locator("time[datetime]").first();
  await expect(timestamp).toBeVisible();
  await expect(timestamp).toHaveClass(/font-mono/);
  await expect(timestamp).toHaveAttribute("datetime", /.+/);

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
