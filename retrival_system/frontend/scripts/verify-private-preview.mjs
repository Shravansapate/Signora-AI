/** Real local API, database, GLBs and rendered skeleton; no mocked network responses. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_BROWSER_TOKEN: token, SIGNORA_BROWSER_OUTPUT: output } = process.env;
if (!origin || !token || !output) throw new Error('Set browser origin, operator token and output directory.');
await mkdir(output, { recursive: true });
const args = ['--enable-unsafe-swiftshader'];
if (process.env.SIGNORA_BROWSER_AUDIO) args.push('--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-audio-capture=${process.env.SIGNORA_BROWSER_AUDIO}`);
const browser = await chromium.launch({ headless: true, args });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, permissions: ['microphone'] });
const page = await context.newPage();
const errors = [], assets = [], measurements = [];
let avatarInstance;
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {
  if (response.url().endsWith('.glb')) assets.push({ url: response.url(), status: response.status() });
});
page.on('requestfinished', request => {
  if (request.url().endsWith('.glb')) console.log(`GLB downloaded: ${request.url().split('/').at(-2)}`);
});
async function verifyPlayback(name, response) {
  assert.equal(response.status(), 200);
  const body = await response.json();
  await writeFile(`${output}/${name}-response.json`, JSON.stringify(body, null, 2));
  assert.equal(body.status, 'READY', JSON.stringify(body.issues));
  assert(body.manifest.items.length > 0);
  if (name === 'mandatory') {
    assert.deepEqual(body.retrieval.gloss_tokens, ['TRAIN', '1201', 'PLATFORM', '2', 'ARRIVE']);
    assert.deepEqual(body.retrieval.retrieval_tokens, body.retrieval.gloss_tokens);
    assert.deepEqual(body.retrieval.fingerspelled, []);
    assert.equal(body.manifest.items.length, 8);
  }
  console.log(`${name}: frontend received ${body.manifest.items.length} clips`);
  await page.waitForFunction(() => ['PLAYING', 'ERROR'].includes(document.querySelector('.player-state')?.textContent), null, { timeout: 180000 });
  assert.equal(await page.locator('.player-state').innerText(), 'PLAYING', await page.locator('[role=alert]').allTextContents());
  const canvas = page.locator('canvas');
  const samples = [];
  for (let index = 0; index < 8; index++) {
    samples.push(await canvas.evaluate(element => ({ ...element.dataset })));
    await page.waitForTimeout(350);
  }
  assert(new Set(samples.map(sample => sample.poseHash)).size > 2, 'Actual rendered bone transforms must change');
  assert(Number(samples.at(-1).mixerTime) > Number(samples[0].mixerTime), 'Animation mixer must advance');
  assert.equal(Number(samples[0].avatarMeshes), 7);
  assert.equal(Number(samples[0].avatarTextures), 5);
  assert(Number(samples[0].maxTextureSize) <= 1024);
  avatarInstance ??= samples[0].avatarInstance;
  assert.equal(samples[0].avatarInstance, avatarInstance, 'Persistent avatar must survive sequential announcements');
  await page.screenshot({ path: `${output}/${name}-playing.png`, fullPage: true });
  console.log(`${name}: rendered skeleton moving, mixer advancing`);
  await page.waitForFunction(() => ['COMPLETE', 'ERROR'].includes(document.querySelector('.player-state')?.textContent), null, { timeout: Math.max(180000, body.manifest.estimated_duration_seconds * 5000) });
  assert.equal(await page.locator('.player-state').innerText(), 'COMPLETE', await page.locator('[role=alert]').allTextContents());
  const completed = await canvas.evaluate(element => ({ ...element.dataset }));
  assert.equal(Number(completed.completedClips), body.manifest.items.length);
  measurements.push({ name, normalized: body.retrieval.normalized_input, tokens: body.retrieval.tokens, clip_count: body.manifest.items.length, samples, completed });
  console.log(`${name}: all ${body.manifest.items.length} clips completed`);
  return body;
}
try {
  await page.goto(`${origin}/announcements`);
  await page.locator('#announcement-token').fill(token);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await page.getByRole('button', { name: 'Disconnect', exact: true }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: /development|demo/i }).count(), 0);
  for (const [name, text] of [['mandatory', 'Train 1201 arrives at platform 2'], ['typo', 'Train 1201 Arrives at platfrm 2'], ['lexical', 'Train ACCIDENT']]) {
    await page.locator('#announcement-text').fill(text);
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/v1/translate') && response.request().method() === 'POST', { timeout: 180000 });
    await page.getByRole('button', { name: 'Play announcement', exact: true }).click();
    const body = await verifyPlayback(name, await responsePromise);
    assert.equal(body.retrieval.last_resort, false);
    if (name === 'typo') {
      assert.equal(body.manifest.items.length, 8);
      assert.equal(body.retrieval.normalized_input, 'train 1201 arrives at platform 2');
    }
    if (name === 'lexical') {
      assert.equal(body.manifest.items.length, 2);
      assert.deepEqual(body.manifest.items.map(item => item.semantic_key), ['ISL_TRAIN_01', 'ISL_ACCIDENT_01']);
      assert.equal(body.retrieval.translation_status, 'LEXICAL_RECOVERY');
    }
  }
  if (process.env.SIGNORA_BROWSER_AUDIO) {
    await page.getByRole('button', { name: 'Record voice', exact: true }).click();
    await page.getByRole('button', { name: 'Start recording', exact: true }).click();
    await page.getByRole('button', { name: 'Stop and transcribe', exact: true }).waitFor();
    await page.waitForTimeout(5500);
    const translated = page.waitForResponse(response => response.url().endsWith('/api/v1/translate') && response.request().method() === 'POST', { timeout: 180000 });
    await page.getByRole('button', { name: 'Stop and transcribe', exact: true }).click();
    await verifyPlayback('voice', await translated);
  }
  assert.deepEqual(errors, []);
  assert(assets.length > 0);
  assert(assets.every(asset => asset.status === 200), JSON.stringify(assets));
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(), measurements, assets, errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  console.error('Browser state:', await page.locator('[role=alert], .player-state, [role=status]').allTextContents());
  await writeFile(`${output}/failure.json`, JSON.stringify({ error: error.message, assets, errors, measurements }, null, 2));
  throw error;
} finally {
  await context.close();
  await browser.close();
}
