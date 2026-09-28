/** Two real browser pages -> database publication -> socket -> GLBs -> rendered avatar -> persisted ACK. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN;
const output = process.env.SIGNORA_BROWSER_OUTPUT;
const operator = process.env.SIGNORA_BROWSER_TOKEN;
const displayToken = process.env.SIGNORA_DISPLAY_TOKEN;
const displayId = process.env.SIGNORA_DISPLAY_ID;
const admin = process.env.SIGNORA_ADMIN_TOKEN;
if (![origin, output, operator, displayToken, displayId, admin].every(Boolean)) throw new Error('Provide browser, operator and registered display environment settings.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--disable-renderer-backgrounding', '--disable-background-timer-throttling'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
const input = await context.newPage(), display = await context.newPage();
const errors = [], assets = [];
for (const page of [input, display]) page.on('pageerror', error => errors.push(error.message));
display.on('response', response => { if (response.url().endsWith('.glb')) assets.push({ url: response.url(), status: response.status() }); });
async function connectDisplay() {
  await display.getByLabel('Display ID', { exact: true }).fill(displayId);
  await display.getByLabel('Display access token', { exact: true }).fill(displayToken);
  await display.getByRole('button', { name: 'Connect display', exact: true }).click();
  await display.waitForFunction(() => document.querySelector('[role=status]')?.textContent.includes('Connection: CONNECTED'), null, { timeout: 30000 });
}
try {
  await display.goto(`${origin}/display`);
  await connectDisplay();
  await input.goto(`${origin}/announcements`);
  await input.locator('#announcement-token').fill(operator);
  await input.getByRole('button', { name: 'Connect', exact: true }).click();
  await input.getByRole('button', { name: 'Disconnect', exact: true }).waitFor();
  await input.getByLabel('Broadcast to all displays', { exact: true }).check();
  await input.locator('#announcement-text').fill('Train 1201 arrives at platform 2');
  const translation = input.waitForResponse(response => response.url().endsWith('/api/v1/translate'), { timeout: 180000 });
  const publication = input.waitForResponse(response => response.url().endsWith('/api/v1/development/announcements'), { timeout: 180000 });
  await input.getByRole('button', { name: 'Play announcement', exact: true }).click();
  const response = await publication;
  assert.equal(response.status(), 200, await response.text());
  const receipt = await response.json();
  const translated = await translation;
  assert.equal(translated.status(), 200);
  const body = await translated.json();
  assert.deepEqual(body.retrieval.gloss_tokens, ['TRAIN', '1201', 'PLATFORM', '2', 'ARRIVE']);
  assert.deepEqual(body.retrieval.retrieval_tokens, body.retrieval.gloss_tokens);
  assert.deepEqual(body.retrieval.fingerspelled, []);
  await writeFile(`${output}/mandatory-response.json`, JSON.stringify(body, null, 2));
  console.log(`Published to station: ${receipt.manifest_id}`);
  await input.getByText('Sent to assigned station displays.', { exact: false }).waitFor();
  await display.waitForFunction(() => document.querySelector('canvas')?.dataset.playbackState === 'PLAYING'
    || document.querySelector('[role=alert]'), null, { timeout: 240000 });
  const canvas = display.locator('canvas');
  assert((await canvas.boundingBox()).height >= 320, 'The live avatar needs a visible display stage');
  assert.equal(await canvas.getAttribute('data-playback-state'), 'PLAYING', await display.locator('[role=alert]').allTextContents());
  assert.match(await display.locator('.caption-area').innerText(), /Train 1201 arrives at platform 2/);
  assert.equal(await canvas.getAttribute('data-clip-count'), '8');
  const stop = input.getByRole('button', { name: 'Stop', exact: true });
  if (await stop.isEnabled()) await stop.click(); // The local preview stop must not stop independent display playback.
  await display.bringToFront();
  const samples = [];
  for (let i = 0; i < 8; i++) {
    samples.push(await canvas.evaluate(element => ({ ...element.dataset })));
    await display.waitForTimeout(400);
  }
  assert(new Set(samples.map(sample => sample.poseHash)).size > 2);
  assert(Number(samples.at(-1).mixerTime) > Number(samples[0].mixerTime));
  await display.screenshot({ path: `${output}/live-playing.png`, fullPage: true });
  console.log('Live display received 8 gloss-derived clips; rendered bones moving and mixer advancing');
  // Reconciliation can clear a completed live plan immediately after its ACK.
  // Capture the terminal snapshot in the same browser task that observes it.
  const terminal = await display.waitForFunction(() => {
    const element = document.querySelector('canvas');
    return ['COMPLETE', 'ERROR'].includes(element?.dataset.playbackState) ? { ...element.dataset } : null;
  }, null, { timeout: 360000 });
  const completed = await terminal.jsonValue();
  assert.equal(completed.playbackState, 'COMPLETE');
  assert.equal(Number(completed.completedClips), 8);
  let device;
  for (let i = 0; i < 20; i++) {
    const status = await context.request.get(`${origin}/api/v1/admin/displays/${displayId}`, { headers: { Authorization: `Bearer ${admin}` } });
    assert.equal(status.status(), 200);
    device = await status.json();
    if (device.deliveries.some(row => row.manifest_id === receipt.manifest_id && row.state === 'COMPLETED')) break;
    await display.waitForTimeout(1000);
  }
  const delivery = device.deliveries.find(row => row.manifest_id === receipt.manifest_id);
  assert.equal(delivery.state, 'COMPLETED');
  console.log('Live display completed 8/8; backend persisted COMPLETED acknowledgement');
  const assetCount = assets.length;
  await display.reload();
  await connectDisplay();
  await display.waitForTimeout(3500);
  assert.match(await display.locator('[role=status]').innerText(), /Signing: IDLE/);
  assert.equal(assets.length, assetCount, 'A completed delivery must not download/replay after reload');
  assert.deepEqual(errors, []);
  assert(assets.length > 0 && assets.every(asset => asset.status === 200));
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(), input: 'Train 1201 arrives at platform 2', receipt,
    samples, completed, delivery, assets, reload_did_not_replay: true, errors }, null, 2));
} catch (error) {
  await display.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  console.error('Display:', await display.locator('[role=status], [role=alert]').allTextContents());
  console.error('Operator:', await input.locator('[role=status], [role=alert]').allTextContents());
  throw error;
} finally { await context.close(); await browser.close(); }
