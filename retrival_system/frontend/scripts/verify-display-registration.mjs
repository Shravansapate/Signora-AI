/** Display credentials through the production UI and real sockets; isolated database only. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN;
const adminToken = process.env.SIGNORA_BROWSER_ADMIN_TOKEN;
const output = process.env.SIGNORA_BROWSER_OUTPUT;
if (!origin || !adminToken || !output) throw new Error('Use the isolated backend browser harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--disable-renderer-backgrounding', '--disable-background-timer-throttling'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, permissions: ['clipboard-read', 'clipboard-write'] });
const page = await context.newPage(), display = await context.newPage(), checks = [], errors = [];
for (const tab of [page, display]) { tab.setDefaultTimeout(30000); tab.on('pageerror', error => errors.push(error.message)); }
const confirmation = 'I checked the station and device identity. Updating registration ends the existing display session.';
const click = name => page.getByRole('button', { name, exact: true }).click();
async function management() {
  await page.goto(`${origin}/admin?tab=displays`);
  await page.getByLabel('Management access token', { exact: true }).fill(adminToken);
  await click('Connect management');
  await page.getByLabel('Monitor station', { exact: true }).selectOption('TEST');
}
async function register(name) {
  await click('New display identity');
  assert.equal(await page.getByLabel('Display access token', { exact: true }).count(), 0);
  const id = await page.getByLabel('Registered display ID', { exact: true }).inputValue();
  const subject = await page.getByLabel('Display identity', { exact: true }).inputValue();
  assert.match(id, /^[0-9a-f-]{36}$/);
  assert.equal(subject, `display_${id}`);
  await page.getByLabel('Display name', { exact: true }).fill(name);
  await page.getByLabel(confirmation, { exact: true }).check();
  await click('Save display registration');
  await page.getByRole('heading', { name: 'Display access token ready', exact: true }).waitFor();
  const token = await page.getByLabel('Display access token', { exact: true }).inputValue();
  assert(token.startsWith('sgd_'));
  return { id, token };
}
async function connect(device) {
  await display.goto(`${origin}/display`);
  await display.getByLabel('Display ID', { exact: true }).fill(device.id);
  await display.getByLabel('Display access token', { exact: true }).fill(device.token);
  await display.getByRole('button', { name: 'Connect display', exact: true }).click();
  await display.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: CONNECTED'));
}
async function sessionStatus(token) {
  return (await context.request.get(`${origin}/api/v1/session`, { headers: { Authorization: `Bearer ${token}` } })).status();
}
try {
  await management();
  const first = await register('Credential browser platform 1');
  await click('Copy access token');
  await page.getByRole('status').filter({ hasText: 'Token copied.' }).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), first.token);
  assert.equal(await page.getByLabel('Display access token', { exact: true }).getAttribute('type'), 'password');
  await page.getByLabel('Show token', { exact: true }).check();
  assert.equal(await page.getByLabel('Display access token', { exact: true }).getAttribute('type'), 'text');
  await page.getByLabel('Show token', { exact: true }).uncheck();
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
  assert(!stored.includes(first.token));
  checks.push('automatic_unique_identity_and_immediate_copyable_token_without_browser_storage');
  await connect(first);
  checks.push('new_token_connects_real_display_without_backend_restart');

  await page.getByLabel(confirmation, { exact: true }).check();
  await click('Generate new access token');
  await page.getByRole('status').filter({ hasText: 'New display token created.' }).waitFor();
  const replacement = await page.getByLabel('Display access token', { exact: true }).inputValue();
  assert(replacement !== first.token);
  assert.equal(await sessionStatus(first.token), 401);
  assert.equal(await sessionStatus(replacement), 200);
  await display.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: REJECTED'));
  await display.getByRole('button', { name: 'Disconnect display', exact: true }).click();
  await connect({ id: first.id, token: replacement });
  checks.push('replacement_revokes_previous_token_and_live_session_and_reconnects');

  const second = await register('Credential browser platform 2');
  assert.notEqual(first.id, second.id);
  assert.notEqual(replacement, second.token);
  await display.getByRole('button', { name: 'Disconnect display', exact: true }).click();
  await connect(second);
  checks.push('second_new_display_connects_in_same_running_backend');

  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  await page.getByRole('heading', { name: 'Display access token ready', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/display-token-mobile.png` });
  checks.push('mobile_token_controls_fit_viewport');
  await management();
  await click('Refresh displays');
  await page.getByRole('row').filter({ hasText: 'Credential browser platform 2' }).getByRole('button', { name: 'Inspect display' }).click();
  await page.getByRole('heading', { name: 'Update registered display', exact: true }).waitFor();
  assert.equal(await page.getByLabel('Display access token', { exact: true }).count(), 0);
  await page.getByLabel(confirmation, { exact: true }).check();
  assert(await page.getByRole('button', { name: 'Generate new access token', exact: true }).isEnabled());
  checks.push('reloaded_admin_offers_replacement_without_reexposing_token');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ checks, page_errors: errors }, null, 2));
  console.log(JSON.stringify({ passed: checks }));
} finally { await context.close(); await browser.close(); }
