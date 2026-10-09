/** Production UI workflows. All identities/content/review records belong to a disposable fixture. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN, operatorToken = process.env.SIGNORA_BROWSER_TOKEN;
const adminToken = process.env.SIGNORA_BROWSER_ADMIN_TOKEN, reviewerToken = process.env.SIGNORA_BROWSER_REVIEW_TOKEN;
const displayToken = process.env.SIGNORA_BROWSER_DISPLAY_TOKEN, did = process.env.SIGNORA_BROWSER_DISPLAY_ID;
const output = process.env.SIGNORA_BROWSER_OUTPUT, fixture = JSON.parse(process.env.SIGNORA_BROWSER_WORKSPACE_DATA || 'null');
if (!origin || !adminToken || !fixture) throw new Error('Use the isolated backend workspace harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--disable-renderer-backgrounding', '--disable-background-timer-throttling'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const page = await context.newPage(), errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
page.setDefaultTimeout(30000);
async function api(path, token = adminToken) {
  const response = await context.request.get(`${origin}/api/v1${path}`, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(response.status(), 200, await response.text()); return response.json();
}
async function click(name) { await page.getByRole('button', { name, exact: true }).click(); }
async function status(text, timeout = 30000) { await page.getByRole('status').filter({ hasText: text }).first().waitFor({ timeout }); }
async function management(token, tab = 'library') {
  await page.goto(`${origin}/admin${tab ? `?tab=${tab}` : ''}`);
  await page.getByLabel('Management access token', { exact: true }).fill(token);
  await click('Connect management');
  await page.getByRole('button', { name: 'Disconnect management', exact: true }).waitFor();
}
async function searchTrain() {
  await page.getByLabel('Search library', { exact: true }).fill('TRAIN'); await click('Search');
  await page.getByRole('button').filter({ has: page.getByText('TRAIN', { exact: true }) }).click();
  await page.getByLabel('Motion version', { exact: true }).waitFor();
}
async function acknowledge(reason) {
  await page.getByLabel('Change / review reason', { exact: true }).fill(reason);
  await page.getByLabel('I checked the selected exact version, intended change, evidence and affected templates.', { exact: true }).check();
}
async function operatorPreview() {
  await click('Prepare complete preview');
  await page.getByRole('button', { name: 'Play preview', exact: true }).waitFor();
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.textContent === 'Play preview' && !b.disabled), null, { timeout: 90000 });
  await click('Play preview');
  await page.waitForFunction(() => document.querySelector('.player-state')?.textContent === 'COMPLETE', null, { timeout: 180000 });
}
try {
  // Operator publishes only after complete preview and explicit intent.
  await page.goto(`${origin}/announcements`);
  await page.getByLabel('Access token', { exact: true }).fill(operatorToken); await click('Connect');
  await page.getByLabel('Broadcast to all displays', { exact: true }).check();
  await page.getByLabel('Announcement text', { exact: true }).fill('Train number 00110 is arriving on platform 1.');
  await operatorPreview();
  await page.getByLabel('Publication reason', { exact: true }).fill('Synthetic operator browser publication');
  await page.getByLabel('I checked the current station, complete announcement and intended action.', { exact: true }).check();
  // Commit the first publication, then lose only its response. A retry must recover
  // that exact publication without a second message or revision.
  let lostResponse = false;
  await page.route('**/api/v1/announcements', async route => {
    if (route.request().method() !== 'POST' || lostResponse) return route.continue();
    const response = await route.fetch(); assert.equal(response.status(), 200);
    lostResponse = true; await route.abort('failed');
  });
  await click('Publish announcement');
  await page.getByRole('alert').waitFor();
  await page.waitForFunction(() => document.querySelector('input[name="audience"]')?.closest('fieldset')?.disabled);
  await click('Retry same publication request'); await status('Announcement published.');
  await page.unroute('**/api/v1/announcements');
  const published = (await api('/announcements?station_id=TEST', operatorToken)).items;
  assert(lostResponse); assert.equal(published.length, 1);
  const first = published[0]; assert.equal(first.revision, 1);
  checks.push('lost_publication_response_recovers_without_duplicate');
  await click('Review / correct'); await operatorPreview();
  await page.getByLabel('Publication reason', { exact: true }).fill('Synthetic operator correction');
  await page.getByLabel('I checked the current station, complete announcement and intended action.', { exact: true }).check();
  await click('Publish correction'); await status('Announcement published.');
  assert.equal((await api(`/announcements/${first.message_id}`, operatorToken)).current_revision, 2);
  await click('Review / correct');
  await page.getByLabel('Publication reason', { exact: true }).fill('Synthetic operator withdrawal');
  await page.getByLabel('I checked the current station, complete announcement and intended action.', { exact: true }).check();
  await click('Withdraw announcement'); await status('Announcement withdrawn');
  assert.equal((await api(`/announcements/${first.message_id}`, operatorToken)).state, 'CANCELLED');
  await page.getByRole('listitem').filter({ hasText: 'Revision 3: CANCELLED' }).waitFor();
  await page.screenshot({ path: `${output}/operator-history.png`, fullPage: true });
  checks.push('operator_preview_publish_correct_withdraw');

  await page.goto(`${origin}/admin`); await page.getByLabel('Management access token', { exact: true }).fill(operatorToken);
  await click('Connect management'); await page.getByRole('alert').filter({ hasText: 'reviewer or administrator' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Imports', exact: true }).count(), 0);
  checks.push('operator_management_denied');

  await management(reviewerToken, 'templates');
  assert.equal(await page.getByRole('button', { name: 'Imports', exact: true }).count(), 0);
  await click('Review template');
  await page.getByLabel('Example train identifier', { exact: true }).fill('00110');
  await page.getByLabel('Example platform identifier', { exact: true }).fill('1');
  await click('Prepare construction example'); await status('Construction playback: READY', 90000);
  await click('Play construction review'); await status('Construction playback: COMPLETE', 180000);
  await page.getByLabel('Template review evidence', { exact: true }).fill('Synthetic browser evidence only: complete recipe rendered');
  await page.getByLabel('Template change reason', { exact: true }).fill('Synthetic construction re-review');
  await page.getByLabel('I reviewed the complete rendered examples, exact values, transitions and safe boundaries.', { exact: true }).check();
  await click('Record construction review'); await status('Construction review recorded.');
  await page.screenshot({ path: `${output}/template-review.png`, fullPage: true });
  checks.push('reviewer_rendered_construction_review');

  await management(adminToken, 'templates'); await click('Review template');
  await page.getByLabel('Template change reason', { exact: true }).fill('Synthetic activation after recorded review');
  await page.getByLabel('I reviewed the complete rendered examples, exact values, transitions and safe boundaries.', { exact: true }).check();
  await click('Enable template'); await status('Construction availability updated.');

  await click('Library'); await searchTrain();
  const original = (await api('/review/signs?q=TRAIN')).items[0];
  const originalActive = original.active_motion_version_id;
  await page.getByLabel('Upload target', { exact: true }).selectOption('selected');
  await page.getByLabel('Metadata file', { exact: true }).setInputFiles(fixture.metadata);
  await page.getByLabel('GLB file', { exact: true }).setInputFiles(fixture.motion);
  await click('Validate and stage upload'); await status('Upload validated and staged.', 90000);
  const staged = (await api(`/review/signs/${original.id}`)).versions[0];
  assert.notEqual(staged.id, originalActive);
  await acknowledge('Synthetic attempt before approval'); await click('Activate selected version');
  await page.getByRole('alert').waitFor();
  assert.equal((await api(`/review/signs/${original.id}`)).concept.active_motion_version_id, originalActive);
  checks.push('upload_and_unapproved_activation_rejected');

  await management(reviewerToken); await searchTrain();
  await page.getByLabel('Motion version', { exact: true }).selectOption(staged.id);
  await click('Preview selected version'); await status('Review playback: READY', 90000);
  await click('Play review'); await status('Review playback: COMPLETE', 90000);
  await page.getByLabel('Composition for exact content', { exact: true }).selectOption('APPROVED');
  await page.getByLabel('Review evidence', { exact: true }).fill('Synthetic browser review only: exact candidate rendered on canonical avatar');
  await acknowledge('Synthetic exact-version approval'); await click('Record motion review'); await status('Exact-version review recorded.');
  assert.equal((await api(`/review/signs/${original.id}`)).versions[0].linguistic_review_status, 'APPROVED');
  checks.push('reviewer_exact_version_playback_and_approval');

  await management(adminToken); await searchTrain(); await page.getByLabel('Motion version', { exact: true }).selectOption(staged.id);
  await acknowledge('Synthetic reviewed replacement'); await click('Activate selected version'); await status('Version change recorded.');
  assert.equal((await api(`/review/signs/${original.id}`)).concept.active_motion_version_id, staged.id);
  await page.getByLabel('Motion version', { exact: true }).selectOption(originalActive);
  await acknowledge('Synthetic retained rollback'); await click('Roll back to selected version'); await status('Version change recorded.');
  assert.equal((await api(`/review/signs/${original.id}`)).concept.active_motion_version_id, originalActive);
  await page.screenshot({ path: `${output}/version-history.png`, fullPage: true });
  checks.push('admin_activation_and_retained_rollback');

  await click('Imports'); await page.getByRole('button', { name: 'Inspect import', exact: true }).first().click();
  await page.getByText('broken.metadata.json', { exact: true }).waitFor();
  await click('Pause import'); await status('Import pause recorded.');
  await click('Resume import'); await status('Import resume recorded.');
  await click('Start / recover import request'); await status('Import recorded.');
  const jobCount = (await api('/admin/imports')).items.length;
  await click('Start / recover import request'); await status('Import recorded.');
  assert.equal((await api('/admin/imports')).items.length, jobCount);
  checks.push('import_findings_pause_resume_idempotency');

  await click('Displays'); await page.getByLabel('Monitor station', { exact: true }).selectOption('TEST');
  await click('New display identity'); await page.getByLabel('Registered display ID', { exact: true }).fill(did);
  await page.getByLabel('Display name', { exact: true }).fill('Synthetic workspace display');
  await page.getByLabel('Configured display credential subject', { exact: true }).fill('workspace-display');
  await page.getByLabel('I checked the station and device identity. Updating registration ends the existing display session.', { exact: true }).check();
  await click('Save display registration'); await status('Display registration recorded.');
  const display = await context.newPage(); display.on('pageerror', error => errors.push(error.message));
  await display.goto(`${origin}/display`); await display.getByLabel('Display ID', { exact: true }).fill(did);
  await display.getByLabel('Display access token', { exact: true }).fill(displayToken); await display.getByRole('button', { name: 'Connect display', exact: true }).click();
  await display.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: CONNECTED'));
  await click('Refresh displays'); await page.getByRole('cell', { name: 'Fresh lease', exact: true }).waitFor();
  await page.screenshot({ path: `${output}/display-monitor.png`, fullPage: true }); await display.close();
  checks.push('admin_registration_and_real_display_monitor');
  await click('Audit'); await page.getByRole('heading', { name: 'Audit history', exact: true }).waitFor();
  await page.locator('.audit-entry').filter({ hasText: did }).getByText('DISPLAY CONFIGURED', { exact: true }).waitFor(); checks.push('audit_history');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/management-mobile.png`, fullPage: true });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), 'Mobile page must not overflow');
  const persisted = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join('\n'));
  assert(![adminToken,operatorToken,reviewerToken,displayToken].some(value => persisted.includes(value)));
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(), checks, page_errors: errors, stored_credentials: false },null,2));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  process.stderr.write(`${JSON.stringify({ checks, alerts: await page.getByRole('alert').allTextContents(), statuses: await page.getByRole('status').allTextContents() })}\n`);
  throw error;
} finally { await context.close(); await browser.close(); }
