/* Walk the Razorpay test checkout end-to-end, dumping DOM at each step. */
import { chromium } from '@playwright/test';

const URL = process.argv[2];
const OUTCOME = process.argv[3] || 'fail'; // 'success' | 'fail'

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.setDefaultTimeout(15000);

const dump = async (tag) => {
  await page.screenshot({ path: `rz_${tag}.png`, fullPage: true });
  for (const f of page.frames()) {
    if (!f.url().includes('razorpay')) continue;
    let els = [];
    try {
      els = await f.$$eval('input, button, select, [role="button"], [role="radio"], [role="option"]', (xs) =>
        xs.slice(0, 60).map((e) => ({
          t: e.tagName.toLowerCase(), id: e.id || null, n: e.getAttribute('name'),
          ph: e.getAttribute('placeholder'), x: (e.textContent || '').trim().slice(0, 32),
        })).filter((e) => e.x || e.ph || e.n || e.id)
      );
    } catch {}
    if (els.length) console.log(`[${tag}]`, JSON.stringify(els));
  }
};

try {
  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);

  const co = page.frames().find((f) => f.url().includes('checkout'));
  if (!co) throw new Error('no checkout frame');

  await co.locator('input[name="contact"]').fill('9000000001');
  await co.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.waitForTimeout(4000);

  // Payment Options: choose the "Cards" row, then the big Continue button.
  await co.getByText('Cards', { exact: true }).click();
  await page.waitForTimeout(1000);
  await co.getByTestId('bottom-cta-button').click();
  await page.waitForTimeout(4000);
  await dump('cardform');

  // Fill the card form (labels/placeholders discovered from the dump).
  // Test-mode cards auto-resolve: 4111… succeeds, 4000…0002 fails (declined).
  const cardNumber = OUTCOME === 'success' ? '4111111111111111' : '4000000000000002';
  await co.locator('input[name="card.number"]').fill(cardNumber);
  await co.locator('input[name="card.expiry"]').fill('1230');
  await co.locator('input[name="card.cvv"]').fill('123');
  // name + email fields appear only after the number/expiry/cvv are valid —
  // wait for them before filling.
  await co.locator('input[name="card.name"]').waitFor({ state: 'visible', timeout: 8000 });
  await co.locator('input[name="card.name"]').fill('Bot Probe');
  await co.locator('input[name="email"]').fill('probe@example.com');
  await page.waitForTimeout(800);
  await dump('cardform_filled');

  await co.getByTestId('bottom-cta-button').click();
  await page.waitForTimeout(5000);

  // Save-card interstitial (appears for fresh contacts): decline politely.
  const maybeLater = co.locator('button[name="pay_without_saving_card"]');
  if (await maybeLater.count()) {
    await maybeLater.click();
  }
  // The payment auto-resolves (test-mode cards); wait for the result page.
  await page.waitForTimeout(12000);
  await dump('result');
  console.log('FINAL URL:', page.url());
  const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 400));
  console.log('RESULT TEXT:', bodyText.replace(/\s+/g, ' ').slice(0, 300));
} catch (e) {
  console.error('FATAL', e.message);
} finally {
  await browser.close();
}
