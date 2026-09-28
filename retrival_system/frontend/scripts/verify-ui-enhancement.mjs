/** Read-only visual checks against the running local application. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_ADMIN_TOKEN: admin, SIGNORA_BROWSER_OUTPUT: output } = process.env;
if (!origin || !admin || !output) throw new Error('Provide origin, admin token and output directory.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
async function capture(name) {
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1050 });
    await page.waitForTimeout(200);
    const dimensions = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert(dimensions.page <= dimensions.viewport + 1, `${name}: overflow ${JSON.stringify(dimensions)}`);
    await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    checks.push(`${name}-${width}`);
  }
  await page.setViewportSize({ width: 1440, height: 1050 });
}
async function click(name) { await page.getByRole('button', { name, exact: true }).click(); }
try {
  await page.goto(`${origin}/admin?tab=library`);
  await capture('management-access');
  await page.getByLabel('Management access token', { exact: true }).fill(admin);
  await click('Connect management');
  await page.getByRole('button', { name: 'Disconnect management', exact: true }).waitFor();
  for (const [tab, name] of [['Library', 'library'], ['Templates & coverage', 'templates'], ['Imports', 'imports'], ['Stations', 'stations'], ['Displays', 'displays'], ['Audit', 'audit']]) {
    await click(tab);
    await page.waitForTimeout(800);
    if (tab === 'Stations') {
      await click('Nagpur');
      assert.equal(await page.getByLabel('Number of platforms', { exact: true }).inputValue(), '8');
    }
    if (tab === 'Displays') { await click('Refresh displays'); await page.waitForTimeout(500); }
    await capture(name);
  }
  for (const [path, name] of [['/', 'content-review'], ['/announcements', 'announcements'], ['/display', 'live-display']]) {
    await page.goto(`${origin}${path}`);
    await capture(name);
    if (path === '/') {
      await page.getByLabel('Access token', { exact: true }).fill(admin);
      await click('Connect workspace');
      await page.locator('.motion-row').first().waitFor();
      for (const tab of ['Motion library', 'Exact text', 'Prepared review']) {
        await click(tab); await capture(`review-${tab.replaceAll(' ', '-').toLowerCase()}`);
      }
    }
    if (path === '/announcements') {
      await page.locator('#announcement-token').fill(process.env.SIGNORA_BROWSER_TOKEN);
      await click('Connect');
      await page.getByRole('button', { name: 'Disconnect', exact: true }).waitFor();
      for (const tab of ['Type', 'Structured fields', 'Record voice']) {
        await click(tab); await capture(`announcement-${tab.replaceAll(' ', '-').toLowerCase()}`);
      }
    }
  }
  assert.deepEqual(errors, []);
  await writeFile(`${output}/visual-verification.json`, JSON.stringify({ status: 'PASSED', checks, errors }, null, 2));
  console.log(`Verified ${checks.length} desktop/mobile views without page errors or horizontal overflow.`);
} finally { await browser.close(); }
