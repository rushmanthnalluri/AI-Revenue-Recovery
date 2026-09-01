import type { Page } from "@playwright/test";

/**
 * Switch the console's data environment via the sidebar's two-segment
 * switcher (role=group "Data environment"). The nav LINK to /research shares
 * the "Research Lab" name — the switcher control is a BUTTON, so the role
 * selector disambiguates them. Selection persists in localStorage for the
 * rest of the test's page context.
 */
export async function switchToResearchLab(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Research Lab", exact: true }).click();
}

export async function switchToRealMerchant(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Real Merchant", exact: true }).click();
}
