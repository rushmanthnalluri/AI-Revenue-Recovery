/* Probe the netbanking simulator flow on a Razorpay test payment link. */
import { chromium } from '@playwright/test';

const URL = process.argv[2];

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.setDefaultTimeout(15000);

const dump = async (tag) => {
  await page.screenshot({ path: `rz_nb_${tag}.png`, fullPage: true });
  for (const f of page.frames()) {
    if (!/razorpay|bank|sim/i.test(f.url())) continue;
    let els = [];
    try {
      els = await f.$$eval('input, button, select, [role="button"]', (xs) =>
        xs.slice(0, 60).map((e) => ({
          t: e.tagName.toLowerCase(), id: e.id || null, n: e.getAttribute('name'),
          ph: e.getAttribute('placeholder'), x: (e.textContent || '').trim().slice(0, 40),
        })).filter((e) => e.x || e.ph || e.n || e.id)
      );
    } catch {}
    if (els.length) console.log(`[${tag}] ${f.url().slice(0, 60)}:`, JSON.stringify(els).slice(0, 1500));
  }
};

try {
  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const co = page.frames().find((f) => f.url().includes('checkout'));
  if (!co) throw new Error('no checkout frame');
  await co.locator('input[name="contact"]').fill('9000000001');
  await co.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.waitForTimeout(3500);
  // Choose a specific bank from the Recommended list.
  const bank = co.getByText(/canara bank/i).first();
  await bank.click();
  await page.waitForTimeout(2500);
  await dump('after_bank_click');
  // Try the CTA if present, then dump again regardless.
  try {
    await co.getByTestId('bottom-cta-button').click({ timeout: 5000 });
  } catch { console.log('CTA not clickable after bank click'); }
  await page.waitForTimeout(15000);
  await dump('bank_page');
  const urlNow = page.url();
  console.log('URL NOW:', urlNow);
  const bodyText = (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ');
  console.log('TEXT:', bodyText.slice(0, 400));
} catch (e) {
  console.error('FATAL', e.message);
} finally {
  await browser.close();
}
