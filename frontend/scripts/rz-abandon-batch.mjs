/* Abandonment burst: start netbanking on a test payment link and abandon the
 * attempt — leaves the payment in `created` state (checkout-abandonment
 * signal). Usage: node scripts/rz-abandon-batch.mjs <linkUrl> <count> [gapMs]
 */
import { chromium } from '@playwright/test';

const [url, countArg, gapArg] = process.argv.slice(2);
const COUNT = Number(countArg ?? 15);
const GAP_MS = Number(gapArg ?? 20000);

async function abandonOnce(page, attempt) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3500);
  const co = page.frames().find((f) => f.url().includes('checkout'));
  if (!co) throw new Error('no checkout frame');
  await co.locator('input[name="contact"]').fill('9000000001');
  await co.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.waitForTimeout(3000);
  await co.getByText(/canara bank/i).first().click();
  // Choosing the bank starts "Processing your payment" — the attempt is now
  // registered as a `created` payment. Abandon here.
  await page.waitForTimeout(8000);
  console.log(`attempt ${attempt}: abandoned (created)`);
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.setDefaultTimeout(20000);

for (let i = 1; i <= COUNT; i++) {
  try {
    await abandonOnce(page, i);
  } catch (e) {
    console.log(`attempt ${i}: ERROR ${e.message.slice(0, 100)}`);
  }
  if (i < COUNT) await page.waitForTimeout(GAP_MS);
}
await browser.close();
console.log('abandon batch complete:', COUNT);
