import { expect, test } from "@playwright/test";

import { switchToResearchLab } from "./environment";

/**
 * Command Center — REAL MERCHANT default: with no Razorpay keys configured on
 * the scratch backend, the console must show the truthful NOT CONNECTED badge
 * and the premium connect empty state — and none of the seeded RESEARCH data
 * may leak onto the real merchant surface.
 */
test("real merchant mode shows the not-connected empty state and truthful badge", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1, name: "Command Center" })).toBeVisible();

  // Truthful topbar badge — the scratch backend has no Razorpay keys.
  await expect(page.getByText("Razorpay Test Mode · Not connected")).toBeVisible();

  // The NOT CONNECTED empty state replaces the data chrome…
  await expect(page.getByText("Connect Razorpay Test Mode to begin")).toBeVisible();
  await expect(page.getByRole("link", { name: "Go to Settings" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Explore Research Lab" })).toBeVisible();

  // …and the seeded research dataset must NOT leak into the real surface:
  // no revenue hero, no KPI band, no fabricated zeros dressed up as data.
  await expect(page.getByRole("region", { name: "Revenue at risk" })).toHaveCount(0);
  await expect(page.getByText("Recoverable revenue", { exact: true })).toHaveCount(0);
});

/**
 * Environment isolation, the other direction: switching to RESEARCH LAB
 * surfaces the seeded synthetic dataset — under the persistent slim
 * SYNTHETIC RESEARCH banner and a truthful slate badge.
 */
test("research mode surfaces the seeded dataset under the synthetic banner", async ({ page }) => {
  await page.goto("/");
  await switchToResearchLab(page);

  // Persistent banner + truthful badge.
  await expect(
    page.getByText("Synthetic research — simulator data, not merchant activity"),
  ).toBeVisible();
  await expect(page.getByText("Synthetic Research", { exact: true })).toBeVisible();

  // The seeded data renders: non-zero revenue hero + full KPI band.
  const hero = page.getByRole("region", { name: "Revenue at risk" });
  await expect(hero).toBeVisible();
  const value = hero.locator("p", { hasText: "₹" }).first();
  await expect(value).toBeVisible();
  const text = (await value.textContent())?.trim() ?? "";
  expect(text).toMatch(/^₹[1-9][\d,]*/); // non-zero INR figure, en-IN grouping

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

  // Provenance chip marks the KPI surface as synthetic research.
  await expect(page.getByText(/Synthetic Research Dataset/).first()).toBeVisible();
});

/**
 * The new merchant surfaces: /payments is env-scoped (empty real surface,
 * seeded research rows with provenance badges) and /settings tells the
 * connection truth — including positive proof that webhook signature
 * verification rejects a forged signature.
 */
test("payments and settings reflect the real merchant connection state", async ({ page }) => {
  // Payments — the real merchant surface is empty (no keys, nothing observed).
  await page.goto("/payments");
  await expect(page.getByRole("heading", { level: 1, name: "Payments" })).toBeVisible();
  await expect(page.getByText("No payments observed yet")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open Settings" })).toBeVisible();

  // Research Lab environment shows the seeded synthetic rows + provenance.
  await switchToResearchLab(page);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await expect(page.getByText("research", { exact: true }).first()).toBeVisible();

  // Settings — the connection card tells the truth about the missing link.
  await page.goto("/settings");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await expect(page.getByText("Not connected", { exact: true })).toBeVisible();
  // The key secret is never rendered — only the static mask.
  await expect(page.getByText("••••••••")).toBeVisible();
  // Sync is honestly unavailable while disconnected.
  await expect(page.getByRole("button", { name: "Sync now" })).toBeDisabled();

  // Webhook probe: a forged signature must be rejected — that IS the proof.
  await page.getByRole("button", { name: "Test webhook" }).click();
  await expect(page.getByText(/Signature verification active/)).toBeVisible();
});
