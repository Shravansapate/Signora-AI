/** Five real WebSocket clients and real GLBs; disposable DB supplied by pytest. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN, token = process.env.SIGNORA_BROWSER_TOKEN;
const devices = JSON.parse(process.env.SIGNORA_BROWSER_DISPLAYS), output = process.env.SIGNORA_BROWSER_OUTPUT;
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--disable-renderer-backgrounding', '--disable-background-timer-throttling'] });
const context = await browser.newContext({ viewport: { width: 1280, height: 960 } });
await context.addInitScript(() => {
  window.playbackEvidence = {};
  setInterval(() => {
    const canvas = document.querySelector('canvas');
    if (!canvas?.dataset.manifestId || canvas.dataset.playbackState !== 'PLAYING') return;
    const id = canvas.dataset.manifestId, entry = window.playbackEvidence[id] ??= { firstPlayingAt: Date.now(), samples: [] };
    if (entry.samples.length < 20) entry.samples.push({ ...canvas.dataset });
  }, 300);
});
const errors = [], pages = [], reports = [], received = new Map();
const operator = await context.newPage();
operator.on('pageerror', e => errors.push(e.message));
async function api(path, body) {
  const r = await context.request.fetch(`${origin}/api/v1${path}`, { method: body ? 'POST' : 'GET', headers: { Authorization: `Bearer ${token}` }, ...(body ? { data: body } : {}) });
  assert.equal(r.status(), 200, await r.text()); return r.json();
}
async function state() { return api('/control-room/displays?station_id=TEST'); }
async function send(text, ids, all = false) {
  // The operator UI supplies actual targets and invokes the existing translation/publication pipeline.
  if (all) await operator.getByRole('button', { name: 'Emergency to all displays', exact: true }).click();
  else {
    await operator.getByLabel('Selected displays', { exact: true }).check();
    for (const [i, d] of devices.entries()) await operator.getByLabel(`Select Platform screen ${i + 1}`, { exact: true }).setChecked(ids.includes(d.id));
  }
  await operator.getByLabel('Emergency priority', { exact: true }).setChecked(all);
  await operator.locator('#announcement-text').fill(text);
  const published = operator.waitForResponse(r => r.url().endsWith('/development/announcements'), { timeout: 180000 });
  await operator.getByRole('button', { name: 'Play announcement', exact: true }).click();
  const response = await published; assert.equal(response.status(), 200, await response.text());
  const receipt = await response.json();
  await operator.waitForFunction(() => !document.querySelector('#announcement-text').disabled, null, { timeout: 240000 });
  const stop = operator.getByRole('button', { name: 'Stop', exact: true });
  if (await stop.isEnabled()) await stop.click();
  return receipt;
}
async function movement(page, name, manifestId) {
  // Observe from page startup: a fast screen may finish while another screen is still loading.
  await page.waitForFunction(id => {
    const samples = window.playbackEvidence[id]?.samples ?? [];
    return samples.length >= 5 && new Set(samples.map(s => s.poseHash)).size > 1;
  }, manifestId, { timeout: 240000 });
  const { samples, firstPlayingAt } = await page.evaluate(id => window.playbackEvidence[id], manifestId);
  assert(new Set(samples.map(s => s.poseHash)).size > 1, `${name}: bones must move`);
  assert(Number(samples.at(-1).mixerTime) > Number(samples[0].mixerTime));
  reports.push({ name, firstPlayingAt, samples });
}
try {
  // Remove assignments left by integration verification, without altering the user's local database.
  const initial = await state();
  await api('/control-room/stop', { request_id: crypto.randomUUID(), station_id: 'TEST', display_ids: devices.map(d => d.id), expected_routes: Object.fromEntries(initial.items.map(d => [d.id, d.route_revision])), reason: 'Isolated browser setup' });
  for (const d of devices) {
    const page = await context.newPage(); pages.push(page); page.on('pageerror', e => errors.push(e.message));
    page.on('websocket', socket => socket.on('framereceived', event => {
      const frame = JSON.parse(String(event.payload));
      for (const item of frame.active ?? []) {
        const key = `${d.id}:${item.manifest_id}`;
        if (!received.has(key)) received.set(key, Date.now());
      }
    }));
    await page.goto(`${origin}/display`);
    await page.getByLabel('Display ID', { exact: true }).fill(d.id);
    await page.getByLabel('Display access token', { exact: true }).fill(d.token);
    await page.getByRole('button', { name: 'Connect display', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: CONNECTED'));
  }
  await operator.goto(`${origin}/announcements`);
  await operator.getByLabel('Access token', { exact: true }).fill(token);
  await operator.getByRole('button', { name: 'Connect', exact: true }).click();
  await operator.getByLabel(`Select Platform screen ${devices.length}`, { exact: true }).waitFor();
  // Deselecting one screen from ALL must retain every other screen.
  await operator.getByLabel('Broadcast to all displays', { exact: true }).check();
  await operator.getByLabel('Select Platform screen 1', { exact: true }).uncheck();
  for (let i = 1; i < devices.length; i++) assert(await operator.getByLabel(`Select Platform screen ${i + 1}`, { exact: true }).isChecked());
  // One operator can group physical screens by platform and select the group in one action.
  for (let i = 0; i < 3; i++) {
    const changed = operator.waitForResponse(r => r.url().endsWith(`/displays/${devices[i].id}/platform`));
    await operator.getByLabel(`Platform for Platform screen ${i + 1}`, { exact: true }).selectOption(i === 0 ? '1' : '2');
    assert.equal((await changed).status(), 200);
  }
  await operator.getByRole('button', { name: 'Select platform 2', exact: true }).click();
  assert(!(await operator.getByLabel('Select Platform screen 1', { exact: true }).isChecked()));
  for (let i = 1; i < 3; i++) assert(await operator.getByLabel(`Select Platform screen ${i + 1}`, { exact: true }).isChecked());
  const first = await send('Train 1201 arrives at platform 2', [devices[0].id]);
  await movement(pages[0], 'independent-first', first.manifest_id);
  for (const p of pages.slice(1)) assert.match(await p.locator('.caption-area').innerText(), /No current announcement/);
  const second = await send('Train ACCIDENT', [devices[1].id, devices[2].id]);
  await Promise.all([movement(pages[1], 'independent-second', second.manifest_id), movement(pages[2], 'independent-third', second.manifest_id)]);
  const assigned = await state();
  assert.equal(assigned.items.find(d => d.id === devices[0].id).manifest_id, first.manifest_id);
  assert.equal(assigned.items.find(d => d.id === devices[1].id).manifest_id, second.manifest_id);
  console.log('Independent selection: first display retained its assignment; second and third played a different announcement.');
  const broadcast = await send('Train ACCIDENT', [], true);
  assert.equal(broadcast.priority, 0);
  await Promise.all(pages.map((p, i) => movement(p, `broadcast-${i + 1}`, broadcast.manifest_id)));
  for (const d of (await state()).items) assert.equal(d.manifest_id, broadcast.manifest_id);
  const receivedAt = devices.map(d => received.get(`${d.id}:${broadcast.manifest_id}`));
  assert(receivedAt.every(Number.isFinite));
  const broadcastDeliverySpreadMs = Math.max(...receivedAt) - Math.min(...receivedAt);
  await operator.screenshot({ path: `${output}/control-room.png`, fullPage: true });
  await pages.at(-1).screenshot({ path: `${output}/display-last-playing.png`, fullPage: true });
  console.log(`All ${devices.length} real display avatars moved after one emergency broadcast.`);
  // A stale command is a definite rejection; the UI must allow a fresh command.
  await operator.route('**/api/v1/control-room/stop', async route => {
    await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Display assignments changed. Refresh and review the targets.' }) });
    await operator.unroute('**/api/v1/control-room/stop');
  });
  await operator.getByRole('button', { name: 'Stop all displays', exact: true }).click();
  await operator.getByText('Display assignments changed. Refresh and review the targets.', { exact: true }).waitFor();
  assert.equal(await operator.getByRole('button', { name: 'Retry same stop command', exact: true }).count(), 0);
  await operator.getByLabel('Selected displays', { exact: true }).check();
  for (let i = 0; i < devices.length; i++) await operator.getByLabel(`Select Platform screen ${i + 1}`, { exact: true }).setChecked(i === 0);
  const stopped = operator.waitForResponse(r => r.url().endsWith('/control-room/stop'));
  await operator.getByRole('button', { name: 'Stop selected displays', exact: true }).click();
  const stopResponse = await stopped;
  assert.equal(stopResponse.status(), 200, await stopResponse.text());
  await pages[0].waitForFunction(() => document.querySelector('.caption-area')?.textContent === 'No current announcement.', null, { timeout: 30000 });
  assert.equal((await state()).items.find(d => d.id === devices[0].id).manifest_id, null);
  for (const d of (await state()).items.filter(d => d.id !== devices[0].id)) assert.equal(d.manifest_id, broadcast.manifest_id);
  await pages[0].reload();
  await pages[0].getByLabel('Display ID', { exact: true }).fill(devices[0].id);
  await pages[0].getByLabel('Display access token', { exact: true }).fill(devices[0].token);
  await pages[0].getByRole('button', { name: 'Connect display', exact: true }).click();
  await pages[0].waitForTimeout(2000);
  assert.match(await pages[0].locator('.caption-area').innerText(), /No current announcement/);
  // A disconnected display receives only the latest assignment when it reconnects.
  const offline = pages.at(-1), offlineDevice = devices.at(-1);
  await offline.getByRole('button', { name: 'Disconnect display', exact: true }).click();
  const latest = await send('Train ACCIDENT', [offlineDevice.id]);
  await offline.getByLabel('Display ID', { exact: true }).fill(offlineDevice.id);
  await offline.getByLabel('Display access token', { exact: true }).fill(offlineDevice.token);
  await offline.getByRole('button', { name: 'Connect display', exact: true }).click();
  await movement(offline, 'offline-latest-assignment', latest.manifest_id);
  // Stop every screen through the operator UI; no assignment reappears on the next sync.
  await operator.getByLabel('Broadcast to all displays', { exact: true }).check();
  // Lose the response after the server commits, then retry the exact command.
  await operator.route('**/api/v1/control-room/stop', async route => {
    const committed = await route.fetch();
    assert.equal(committed.status(), 200);
    await route.abort('connectionfailed');
    await operator.unroute('**/api/v1/control-room/stop');
  });
  await operator.getByRole('button', { name: 'Stop all displays', exact: true }).click();
  await operator.getByRole('button', { name: 'Retry same stop command', exact: true }).waitFor();
  await operator.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === 'Retry same stop command' && !button.disabled));
  assert(await operator.getByLabel('Broadcast to all displays', { exact: true }).isDisabled());
  const committedHistory = (await state()).history;
  const stopAll = operator.waitForResponse(r => r.url().endsWith('/control-room/stop'));
  await operator.getByRole('button', { name: 'Retry same stop command', exact: true }).click();
  assert.equal((await stopAll).status(), 200);
  assert.deepEqual((await state()).history, committedHistory);
  await Promise.all(pages.map(p => p.waitForFunction(() => document.querySelector('.caption-area')?.textContent === 'No current announcement.', null, { timeout: 30000 })));
  for (const d of (await state()).items) assert.equal(d.manifest_id, null);
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', displays: devices.length, first, second, broadcast, broadcastDeliverySpreadMs, reports, stopped_display_stays_stopped_on_reload: true, platform_selection: true, stale_command_recovery: true, offline_latest_assignment: true, stop_all: true, lost_response_idempotent_retry: true, errors }, null, 2));
} catch (error) {
  const displays = await Promise.all(pages.map(async (page, i) => ({ display: i + 1,
    text: await page.locator('main').innerText().catch(() => 'unavailable'),
    canvas: await page.locator('canvas').evaluate(c => ({ ...c.dataset })).catch(() => null),
    evidence: await page.evaluate(() => window.playbackEvidence).catch(() => null) })));
  await writeFile(`${output}/failure.json`, JSON.stringify({ error: error.message, displays, reports, errors }, null, 2));
  throw error;
} finally { await context.close(); await browser.close(); }
