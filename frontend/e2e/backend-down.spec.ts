import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./stack";

/**
 * Backend-down resilience: with every API call aborted at the network layer,
 * the console shows the honest "Backend unreachable" panel instead of
 * fabricated data.
 */
test("backend outage surfaces the unreachable error panel", async ({ page }) => {
  await page.route(`${API_BASE_URL}/**`, (route) => route.abort());

  await page.goto("/incidents");

  const alert = page.getByRole("alert").filter({ hasText: "Backend unreachable" }).first();
  await expect(alert).toBeVisible({ timeout: 30_000 });
  await expect(alert).toContainText("The PulseRecover API is not responding");
});
