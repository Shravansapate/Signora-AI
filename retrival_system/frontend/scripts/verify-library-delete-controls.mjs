/** Inspect the real A deletion form without deleting the user's live library. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_ADMIN_TOKEN: token, SIGNORA_BROWSER_OUTPUT: output } = process.env;
if (!origin || !token || !output) throw new Error('Set origin, admin token and output directory.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = [], mutations = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.method() === 'DELETE') mutations.push(request.url()); });
try {
  await page.goto(`${origin}/admin?tab=library`);
  await page.getByLabel('Management access token', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect management', exact: true }).click();
  await page.getByLabel('Search library', { exact: true }).fill('ISL_A_01');
  const search = page.waitForResponse(response => response.url().includes('/review/signs?q=ISL_A_01'));
  await page.getByRole('button', { name: 'Search', exact: true }).click(); await search;
  await page.locator('.library-results button').filter({ has: page.locator('strong', { hasText: /^A$/u }) }).click();
  const remove = page.getByRole('button', { name: 'Delete GLB + metadata', exact: true });
  await remove.click();
  const form = page.getByRole('region', { name: 'Delete selected content', exact: true });
  await form.waitFor();
  const confirm = page.getByRole('button', { name: 'Permanently delete selected GLB + metadata', exact: true });
  assert(await confirm.isDisabled());
  await page.getByLabel('Deletion reason', { exact: true }).fill('Test deletion and re-upload of A');
  await page.getByLabel('Type ISL_A_01 to confirm deletion', { exact: true }).fill('wrong sign');
  assert(await confirm.isDisabled());
  await page.getByLabel('Type ISL_A_01 to confirm deletion', { exact: true }).fill('ISL_A_01');
  assert(await confirm.isEnabled());
  await form.screenshot({ path: `${output}/a-delete-form.png` });
  await page.getByRole('button', { name: 'Cancel deletion', exact: true }).click();
  await form.waitFor({ state: 'hidden' });
  assert.deepEqual(mutations, []);
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', sign: 'ISL_A_01',
    delete_button_visible: true, confirmation_requires_exact_sign: true, cancellation_passed: true,
    actual_deletions: 0, errors }, null, 2));
  console.log('A deletion controls, exact confirmation and cancellation passed. Live A was not deleted.');
} finally { await browser.close(); }
