/** Real WebSocket/HTTP/GLB path in the disposable PostgreSQL harness. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN, token = process.env.SIGNORA_BROWSER_TOKEN;
const did = process.env.SIGNORA_BROWSER_DISPLAY_ID, displayToken = process.env.SIGNORA_BROWSER_DISPLAY_TOKEN;
const output = process.env.SIGNORA_BROWSER_OUTPUT;
if (!origin || !token || !did || !displayToken || !output) throw new Error('Use the isolated backend browser harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--disable-renderer-backgrounding', '--disable-background-timer-throttling'] });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage(), errors = [], states = [], playbackSamples = [];
page.on('pageerror', e => errors.push(e.message));
page.on('websocket', socket => socket.on('framesent', e => {
  try { const data = JSON.parse(e.payload); if (data.state) states.push(data.state); } catch { /* binary not used */ }
}));
async function post(path, data) {
  const response = await context.request.post(`${origin}${path}`, { headers: { Authorization: `Bearer ${token}` }, data });
  assert.equal(response.status(), 200, await response.text()); return response.json();
}
async function publish(source, revision, expected, message) {
  const preview = await post('/api/v1/translate', { request_id: randomUUID(), station_id: 'TEST',
    input_type: 'TEXT', text: 'Train number 00110 is arriving on platform 1.' });
  assert.equal(preview.status, 'READY');
  return post(message ? `/api/v1/announcements/${message}/revisions` : '/api/v1/announcements', {
    station_id: 'TEST', source_event_id: source, source_revision: revision, expected_revision: expected,
    preview_manifest_id: preview.manifest.manifest_id, preview_manifest_hash: preview.manifest.manifest_hash,
    reason: 'Synthetic browser publication only',
  });
}
async function connect() {
  await page.getByLabel('Display ID', { exact: true }).fill(did);
  await page.getByLabel('Display access token', { exact: true }).fill(displayToken);
  await page.getByRole('button', { name: 'Connect display', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: CONNECTED'), null, { timeout: 30000 });
}
try {
  // Record actual rendered progress: a longer software-rendering budget must
  // still demonstrate completion, not merely a connected socket.
  await page.exposeFunction('recordPlaybackSample', sample => playbackSamples.push(sample));
  await page.addInitScript(() => {
    setInterval(() => {
      const canvas = document.querySelector('canvas');
      if (canvas) window.recordPlaybackSample({ at: Date.now(), ...canvas.dataset });
    }, 5000);
  });
  const source = randomUUID(), first = await publish(source, 1, 0);
  await page.goto(`${origin}/display`); await connect();
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Signing: PLAYING'), null, { timeout: 90000 });
  const avatar = await page.locator('canvas').getAttribute('data-avatar-instance');
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Signing: COMPLETE'), null, { timeout: 180000 });
  await page.waitForTimeout(2500);
  for (const state of ['RECEIVED', 'ASSETS_READY', 'STARTED', 'COMPLETED']) assert(states.includes(state), state);
  await page.reload(); await connect();
  await page.waitForTimeout(2500);
  assert(!await page.getByRole('status').innerText().then(text => text.includes('PLAYING')), 'Completed revision must not replay after reload');
  const second = await publish(source, 2, 1, first.message_id);
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Signing: PLAYING'), null, { timeout: 90000 });
  const secondAvatar = await page.locator('canvas').getAttribute('data-avatar-instance');
  await post(`/api/v1/announcements/${first.message_id}/revisions`, { station_id: 'TEST', source_event_id: source,
    source_revision: 3, expected_revision: 2, cancel: true, reason: 'Synthetic cancellation during playback' });
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Signing: INTERRUPTED')
    || document.querySelector('[role=status]')?.textContent.includes('Signing: COMPLETE'), null, { timeout: 90000 });
  assert.equal(await page.locator('canvas').getAttribute('data-avatar-instance'), secondAvatar);
  assert.equal(await page.locator('canvas').count(), 1);
  await page.waitForTimeout(2500);
  assert((await page.getByRole('status').innerText()).includes('Connection: CONNECTED'), 'Cancellation must preserve the authenticated display session');
  const saved = await page.evaluate(() => Object.values(localStorage));
  assert(!saved.some(value => value.includes('Train number') || value.includes('Bearer')));
  await page.screenshot({ path: `${output}/live-display.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(),
    real_websocket: true, complete_sequence: true, acknowledgements: states,
    completed_revision_suppressed_on_reload: true, cancellation_during_playback: true,
    one_avatar_per_session: true, initial_avatar: avatar, corrected_manifest: second.manifest_id,
    playback_samples: playbackSamples, page_errors: errors }, null, 2));
} catch (error) {
  await writeFile(`${output}/failure.json`, JSON.stringify({ playback_samples: playbackSamples, acknowledgements: states, page_errors: errors }, null, 2));
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  process.stderr.write(`Display status: ${await page.locator('[role=status], [role=alert]').allTextContents()}\n`);
  throw error;
} finally { await context.close(); await browser.close(); }
