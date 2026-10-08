/** Runs against the disposable PostgreSQL/API environment owned by backend's integration test. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN;
const token = process.env.SIGNORA_BROWSER_TOKEN;
const output = process.env.SIGNORA_BROWSER_OUTPUT;
if (!origin || !token || !output) throw new Error('Use the isolated backend browser-test harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const bitmapFailure = process.env.SIGNORA_TEST_BITMAP_FAILURE === '1';
if (bitmapFailure) await context.addInitScript(() => {
  window.createImageBitmap = async () => { throw new DOMException('Injected ImageBitmap failure', 'InvalidStateError'); };
});
const page = await context.newPage();
const errors = [], requests = [], measurements = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.url().endsWith('.glb')) requests.push(request.url()); });
async function state(expected, timeout = 60000) {
  await page.waitForFunction(value => document.querySelector('.player-state')?.textContent === value,
    expected.toLowerCase(), { timeout });
}
try {
  await page.goto(origin);
  await page.getByLabel('Access token', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect workspace' }).click();
  await page.waitForFunction(() => document.querySelectorAll('.motion-row').length === 3);
  const response = await context.request.get(`${origin}/api/v1/review/motions`, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(response.status(), 200);
  const rows = (await response.json()).items;
  const train = rows.find(item => item.semantic_key === 'ISL_TRAIN_01');
  assert(train);
  const others = rows.filter(item => item !== train);
  await page.getByLabel('Persistent avatar source').selectOption(train.motion_version_id);
  const add = motion => page.getByRole('button', { name: `Add ${motion.gloss} version ${motion.version_no} to sequence`, exact: true }).click();
  let canonical;
  for (const count of [1, 3, 12]) {
    const startCount = count === 1 ? 0 : count === 3 ? 1 : 3;
    for (let index = startCount; index < count; index++) await add([train, ...others][index % 3]);
    const began = performance.now(), before = requests.length;
    await page.getByRole('button', { name: 'Prepare sequence', exact: true }).click();
    await state('READY');
    const readyMs = performance.now() - began;
    const canvas = page.locator('canvas');
    assert.equal(await canvas.count(), 1);
    const diagnostics = await canvas.evaluate(element => ({ ...element.dataset }));
    assert.equal(Number(diagnostics.avatarMeshes), 7);
    assert.equal(Number(diagnostics.avatarTextures), 5);
    canonical ??= diagnostics.avatarInstance;
    assert.equal(diagnostics.avatarInstance, canonical);
    if (count === 12) assert.equal(requests.length - before, 0, 'Warm motions must not be downloaded again');
    const playbackBegan = performance.now();
    await page.getByRole('button', { name: 'Play sequence', exact: true }).click();
    await state('PLAYING');
    if (count === 1) {
      await page.waitForTimeout(1500); // Capture the actual motion, beyond the entry pose.
      await page.screenshot({ path: `${output}/avatar-playing.png`, fullPage: true });
    }
    await state('COMPLETE', 120000);
    assert.equal(await page.getByRole('progressbar', { name: 'Completed clips' }).getAttribute('value'), '100');
    assert.equal(await canvas.getAttribute('data-avatar-instance'), canonical);
    measurements.push({ count, ready_ms: Math.round(readyMs), playback_ms: Math.round(performance.now() - playbackBegan), asset_requests: requests.length - before, ...diagnostics });
    process.stdout.write(`Browser sequence ${count}: complete, one avatar, ${requests.length - before} asset requests\n`);
  }
  await page.screenshot({ path: `${output}/twelve-complete.png`, fullPage: true });
  // A fresh player must reject a missing required later asset before enabling signing.
  await page.getByRole('button', { name: 'Reset session', exact: true }).click();
  await page.route(`**/assets/${others[0].motion_version_id}/*.glb`, route => route.abort('failed'));
  await page.getByRole('button', { name: 'Prepare sequence', exact: true }).click();
  await state('ERROR');
  assert(await page.getByRole('button', { name: 'Play sequence', exact: true }).isDisabled());
  await page.unrouteAll();
  await page.getByRole('button', { name: 'Exact text', exact: true }).click();
  await page.getByLabel('Exact source text', { exact: true }).fill('Train has not departed from platform 0011');
  await page.getByLabel('Avatar profile ID', { exact: true }).fill(train.avatar_profile_id);
  await page.getByRole('button', { name: 'Find exact content' }).click();
  await page.locator('.review-notice').waitFor();
  assert.match(await page.locator('.review-notice').innerText(), /unsupported/);
  assert.match(await page.locator('.caption-area').innerText(), /0011/);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: `${output}/mobile-review.png`, fullPage: true });
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  assert.equal(await page.getByLabel('Access token', { exact: true }).inputValue(), '');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/browser-verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(),
    viewport: '1440x1100; 390x844', measurements, image_bitmap_failure_injected: bitmapFailure, missing_asset_blocks_start: true,
    unsupported_text_preserved: true, persisted_browser_credentials: false, page_errors: errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  const notices = await page.locator('[role=alert], .player-state').allTextContents();
  process.stderr.write(`Browser state: ${JSON.stringify(notices)}\n`);
  throw error;
} finally { await context.close(); await browser.close(); }
