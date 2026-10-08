import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN, token = process.env.SIGNORA_BROWSER_TOKEN;
const output = process.env.SIGNORA_BROWSER_OUTPUT, audio = process.env.SIGNORA_BROWSER_AUDIO;
if (!origin || !token || !output || !audio) throw new Error('Use the isolated backend browser harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${audio}`] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, permissions: ['microphone'] });
const page = await context.newPage(), errors = [];
page.on('pageerror', error => errors.push(error.message));
const source = 'Train number 00110 is arriving on platform 1.';
async function translate(button) {
  const response = page.waitForResponse(response => response.url().endsWith('/api/v1/translate') && response.request().method() === 'POST');
  await page.getByRole('button', { name: button, exact: true }).click();
  const result = await response;
  assert.equal(result.status(), 200);
  return result.json();
}
async function state(value, timeout = 90000) {
  await page.waitForFunction(expected => document.querySelector('.player-state')?.textContent === expected, value, { timeout });
}
try {
  const document = await page.goto(`${origin}/announcements`);
  assert.match(document.headers()['permissions-policy'], /microphone=\(self\)/);
  const recorderSource = await readFile(new URL('../src/services/recorder.mjs', import.meta.url), 'utf8');
  const resampling = await page.evaluate(async source => {
    const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
    try {
      const { resampleRecording, pcmWav } = await import(url);
      const results = [];
      for (const rate of [44100, 48000]) {
        const samples = Float32Array.from({ length: rate }, (_, i) => 0.5 * Math.sin(2 * Math.PI * 1000 * i / rate));
        const output = await resampleRecording([samples], rate);
        const wav = new DataView(await pcmWav([output]).arrayBuffer());
        let crossings = 0;
        for (let i = 1; i < output.length; i++) if (output[i - 1] <= 0 && output[i] > 0) crossings++;
        results.push({ input_rate: rate, output_frames: output.length, wav_rate: wav.getUint32(24, true), crossings });
      }
      return results;
    } finally { URL.revokeObjectURL(url); }
  }, recorderSource);
  for (const result of resampling) {
    assert.equal(result.output_frames, 16000);
    assert.equal(result.wav_rate, 16000);
    assert(Math.abs(result.crossings - 1000) <= 1, 'Resampling must preserve duration and pitch');
  }
  await page.getByLabel('Access token', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await page.getByText('Credentials held in memory for this session').waitFor();
  await page.getByLabel('Announcement text', { exact: true }).fill(source);
  const began = performance.now();
  const typed = await translate('Prepare complete preview');
  const typedPlanMs = Math.round(performance.now() - began);
  assert.equal(typed.status, 'READY');
  assert.equal(typed.meaning.slots.train_identifier.value, '00110');
  await state('READY');
  const readyMs = Math.round(performance.now() - began);
  const canvas = page.locator('canvas');
  const avatar = await canvas.getAttribute('data-avatar-instance');
  await page.getByRole('button', { name: 'Play preview', exact: true }).click();
  await state('PLAYING');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${output}/announcement-playing.png`, fullPage: true });
  await state('COMPLETE');
  assert.equal(await canvas.getAttribute('data-avatar-instance'), avatar);
  await page.getByRole('button', { name: 'Structured fields', exact: true }).click();
  await page.getByLabel('Train identifier (keep leading zeros)', { exact: true }).fill('00110');
  await page.getByLabel('Platform', { exact: true }).fill('1');
  const structuredBegan = performance.now();
  const structured = await translate('Prepare complete preview');
  const structuredPlanMs = Math.round(performance.now() - structuredBegan);
  assert.equal(structured.meaning_hash, typed.meaning_hash);
  await state('READY');
  await page.getByRole('button', { name: 'Record voice', exact: true }).click();
  // Startup readiness is independent of deterministic service readiness.
  for (let index = 0; index < 60; index++) {
    const response = await context.request.get(`${origin}/api/v1/input/capabilities`, { headers: { Authorization: `Bearer ${token}` } });
    if ((await response.json()).asr_state === 'READY') break;
    if (index === 59) throw new Error('Actual ASR model did not become ready');
    await page.waitForTimeout(1000);
  }
  await page.getByRole('button', { name: 'Start recording', exact: true }).click();
  await page.getByText('Microphone recording', { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('meter[aria-label="Microphone level"]')?.value > 0);
  await page.waitForTimeout(6500);
  const speechResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/voice/transcribe'), { timeout: 120000 });
  await page.getByRole('button', { name: 'Stop and transcribe', exact: true }).click();
  const speech = await speechResponse;
  assert.equal(speech.status(), 201);
  const transcript = await speech.json();
  assert.equal(transcript.metadata.final, true);
  assert(Number.isFinite(transcript.metadata.audio_quality.rms_dbfs));
  assert(Array.isArray(transcript.metadata.audio_quality.warnings));
  assert.match(transcript.text.toLowerCase(), /train/);
  const correction = page.getByLabel('Final transcript — check and correct', { exact: true });
  await correction.fill(source);
  const unconfirmed = await translate('Check transcript and fields');
  assert.equal(unconfirmed.status, 'NEEDS_CONFIRMATION');
  assert.equal(unconfirmed.manifest, null);
  assert(await page.getByRole('button', { name: 'Play preview', exact: true }).isDisabled());
  await page.getByRole('checkbox', { name: 'I checked the transcript, identifiers, platforms, status, negation and times.', exact: true }).check();
  const voice = await translate('Prepare complete preview');
  assert.equal(voice.status, 'READY');
  assert.equal(voice.meaning_hash, typed.meaning_hash);
  await state('READY');
  assert.equal(await canvas.getAttribute('data-avatar-instance'), avatar);
  await correction.fill('Train number 00110 is not arriving on platform 1.');
  const negative = await translate('Check transcript and fields');
  assert.equal(negative.meaning.polarity, 'NEGATIVE');
  assert.equal(negative.status, 'NEEDS_CONFIRMATION');
  assert.equal(negative.manifest, null);
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: `${output}/announcement-mobile.png`, fullPage: true });
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  assert.equal(await page.getByLabel('Access token', { exact: true }).inputValue(), '');
  const reviewToken = process.env.SIGNORA_BROWSER_REVIEW_TOKEN;
  assert(reviewToken);
  const headers = { Authorization: `Bearer ${reviewToken}` };
  const templates = await context.request.get(`${origin}/api/v1/review/templates`, { headers });
  const template = (await templates.json()).items.find(row => row.template_key === 'synthetic-arrival-fixture');
  const reviewResponse = await context.request.post(`${origin}/api/v1/review/templates/${template.id}/prepare`, {
    headers, data: { station_id: 'TEST', example: { intent: 'TRAIN_ARRIVAL', temporal_state: 'ARRIVING_NOW', slots: { train_identifier: '00110', platform_identifier: '1' } } },
  });
  assert.equal(reviewResponse.status(), 200);
  const reviewPlan = (await reviewResponse.json()).manifest;
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto(origin);
  await page.getByLabel('Access token', { exact: true }).fill(reviewToken);
  await page.getByRole('button', { name: 'Connect workspace', exact: true }).click();
  await page.getByRole('button', { name: 'Prepared review', exact: true }).click();
  await page.getByLabel('Prepared review plan ID', { exact: true }).fill(reviewPlan.manifest_id);
  await page.getByRole('button', { name: 'Open review plan', exact: true }).click();
  await state('ready');
  await page.getByRole('button', { name: 'Play sequence', exact: true }).click();
  await state('playing');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${output}/template-review-playing.png`, fullPage: true });
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  assert.deepEqual(errors, []);
  await writeFile(`${output}/browser-verification.json`, JSON.stringify({ status: 'PASSED', browser: browser.version(), ready_ms: readyMs, typed_plan_ms: typedPlanMs, structured_plan_ms: structuredPlanMs,
    motions: typed.manifest.items.length, one_avatar: true, actual_microphone_capture: 'Chromium fake device using synthetic speech WAV',
    actual_local_asr: true, model_id: transcript.metadata.model_id, resampling, microphone_meter: true,
    audio_quality: transcript.metadata.audio_quality, transcript: transcript.text, common_meaning_hash: typed.meaning_hash,
    confirmation_required: true, edited_transcript_invalidates_confirmation: true, prepared_template_review_rendered: true, page_errors: errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  process.stderr.write(JSON.stringify(await page.locator('[role=alert], .player-state').allTextContents()));
  throw error;
} finally { await context.close(); await browser.close(); }
