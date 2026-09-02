/* Batch driver for Razorpay test-mode hosted checkout (see rz-discover.mjs for
 * the DOM discovery). Pays one payment link N times with a mix of outcomes.
 * Test cards auto-resolve: 4111… succeeds, 4000…0002 is declined.
 *
 * Usage: node scripts/rz-pay-batch.mjs <linkUrl> <failCount> <successCount> [gapMs]
 */
import { chromium } from '@playwright/test';

const [url, failArg, okArg, gapArg] = process.argv.slice(2);
const FAILS = Number(failArg ?? 16);
const SUCCESSES = Number(okArg ?? 4);
const GAP_MS = Number(gapArg ?? 65000);

const FAIL_CARD = '4000000000000002';
const OK_CARD = '4111111111111111';

async function payOnce(page, outcome, attempt) {
  const card = outcome === 'success' ? OK_CARD : FAIL_CARD;
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const co = page.frames().find((f) => f.url().includes('checkout'));
  if (!co) throw new Error('no checkout frame');
  await co.locator('input[name="contact"]').fill('9000000001');
  await co.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.waitForTimeout(3500);
  await co.getByText('Cards', { exact: true }).click();
  await page.waitForTimeout(800);
  await co.getByTestId('bottom-cta-button').click();
  await page.waitForTimeout(3500);
  await co.locator('input[name="card.number"]').fill(card);
  await co.locator('input[name="card.expiry"]').fill('1230');
  await co.locator('input[name="card.cvv"]').fill('123');
  await co.locator('input[name="card.name"]').waitFor({ state: 'visible', timeout: 8000 });
  await co.locator('input[name="card.name"]').fill('Bot Probe');
  await co.locator('input[name="email"]').fill('probe@example.com');
  await co.getByTestId('bottom-cta-button').click();
  await page.waitForTimeout(4000);
  const maybeLater = co.locator('button[name="pay_without_saving_card"]');
  if (await maybeLater.count()) await maybeLater.click();
  await page.waitForTimeout(12000);
  const text = (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ');
  const failed = /could not be completed|failed|declined/i.test(text);
  const succeeded = /success|paid|thank/i.test(text);
  console.log(
    `attempt ${attempt} [${outcome}]: resolved failed=${failed} succeeded=${succeeded} :: ${text.slice(90, 200)}`
  );
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.setDefaultTimeout(20000);

// Interleave: successes spread through the failure wave.
const plan = [];
for (let i = 0; i < FAILS; i++) plan.push('fail');
for (let i = 0; i < SUCCESSES; i++) plan.splice(Math.min(plan.length, 2 + i * 5), 0, 'success');

let done = 0;
for (const outcome of plan) {
  done += 1;
  try {
    await payOnce(page, outcome, done);
  } catch (e) {
    console.log(`attempt ${done} [${outcome}]: ERROR ${e.message.slice(0, 120)}`);
  }
  if (done < plan.length) await page.waitForTimeout(GAP_MS);
}
await browser.close();
console.log('batch complete:', plan.length, 'attempts');
