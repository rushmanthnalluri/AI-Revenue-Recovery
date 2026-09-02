/* Discover the Razorpay hosted-checkout DOM for one payment link (test mode). */
const { chromium } = require('@playwright/test');

const URL = process.argv[2] || 'https://rzp.io/rzp/sth8eOh';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(URL, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: 'rz_step1.png', fullPage: true });

  console.log('URL:', page.url());
  console.log('TITLE:', await page.title());
  for (const frame of page.frames()) {
    const inputs = await frame.$$eval('input, button, select', (els) =>
      els.slice(0, 40).map((e) => ({
        tag: e.tagName.toLowerCase(),
        type: e.getAttribute('type'),
        id: e.id || null,
        name: e.getAttribute('name'),
        placeholder: e.getAttribute('placeholder'),
        text: (e.textContent || '').trim().slice(0, 40),
        testid: e.getAttribute('data-testid') || e.getAttribute('data-test-id'),
      }))
    );
    console.log('FRAME', frame.url().slice(0, 90));
    console.log(JSON.stringify(inputs, null, 1).slice(0, 4000));
  }
  await browser.close();
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
