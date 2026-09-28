/** Real Library UI and backend. Reuses existing TRAIN bytes; never deletes source assets. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_ADMIN_TOKEN: token, SIGNORA_BROWSER_OUTPUT: output,
  SIGNORA_LIBRARY_ROOT: library } = process.env;
if (![origin, token, output, library].every(Boolean)) throw new Error('Set origin, admin token, output and supplied library root.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = []; page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(`${origin}/admin?tab=library`);
  await page.getByLabel('Management access token', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect management', exact: true }).click();
  await page.getByLabel('Search library', { exact: true }).fill('ISL_TRAIN_01');
  const search = page.waitForResponse(response => response.url().includes('/review/signs?q=ISL_TRAIN_01'));
  await page.getByRole('button', { name: 'Search', exact: true }).click(); await search;
  await page.locator('.library-results button').filter({ has: page.locator('strong', { hasText: /^TRAIN$/u }) }).click();
  await page.getByRole('button', { name: 'Activate selected version', exact: true }).waitFor();
  await page.getByLabel('Upload target').selectOption('selected');
  await page.getByLabel('Metadata file', { exact: true }).setInputFiles(`${library}/metadata/Train.metadata.json`);
  await page.getByLabel('GLB file', { exact: true }).setInputFiles(`${library}/glb/Train.glb`);
  const uploaded = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/motions'), { timeout: 180000 });
  await page.getByRole('button', { name: 'Validate and stage upload', exact: true }).click();
  const uploadResponse = await uploaded; assert.equal(uploadResponse.status(), 201);
  const upload = await uploadResponse.json(); assert.equal(upload.status, 'UNCHANGED');
  await page.getByRole('button', { name: 'Refresh concept', exact: true }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Refresh concept')?.disabled);
  await page.getByText('Metadata file and revisions', { exact: true }).click();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download current metadata.json', exact: true }).click();
  const download = await downloaded; assert.match(download.suggestedFilename(), /ISL_TRAIN_01.*metadata.json/u);
  await page.getByLabel('Change / review reason', { exact: true }).fill('Verify current TRAIN selection for the local development library');
  await page.getByRole('checkbox', { name: /I checked the selected exact version/u }).check();
  const activate = page.getByRole('button', { name: 'Activate selected version', exact: true });
  if (await activate.isEnabled()) {
    const activated = page.waitForResponse(response => response.url().endsWith('/activate') && response.request().method() === 'POST');
    await activate.click(); assert.equal((await activated).status(), 200);
  }
  const translated = await page.request.post(`${origin}/api/v1/translate`, {
    headers: { Authorization: `Bearer ${token}` }, data: { request_id: randomUUID(), station_id: 'NAGPUR', input_type: 'TEXT', text: 'Train' },
  });
  assert.equal(translated.status(), 200); const retrieval = await translated.json();
  assert.equal(retrieval.manifest.items[0].motion_version_id, upload.motion_version_id);
  assert.equal(retrieval.manifest.items.length, 1);
  await page.screenshot({ path: `${output}/library.png`, fullPage: true });
  await page.getByRole('button', { name: 'Templates & coverage', exact: true }).click();
  await page.getByText('Try a template and understand coverage', { exact: true }).click();
  await page.getByRole('button', { name: 'Load arrival example from library', exact: true }).click();
  const definition = page.getByLabel('Construction definition JSON', { exact: true });
  await page.waitForFunction(() => Array.from(document.querySelectorAll('textarea')).some(t => t.value.includes('ARRIVING_NOW')));
  const example = JSON.parse(await definition.inputValue());
  assert.equal(example.intent, 'TRAIN_ARRIVAL');
  assert.deepEqual(Object.keys(example.recipe[1].policy.units), ['1201', '1202']);
  await writeFile(`${output}/arrival-example.json`, JSON.stringify(example, null, 2));
  const staged = page.waitForResponse(response => response.url().endsWith('/admin/templates') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Stage template version', exact: true }).click();
  const stageResponse = await staged;
  assert.equal(stageResponse.status(), 201, await stageResponse.text());
  const template = await stageResponse.json();
  await page.getByRole('heading', { name: 'Exact dependency bindings', exact: true }).waitFor();
  const coverageResponse = await page.request.get(`${origin}/api/v1/review/templates/${template.id}`, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(coverageResponse.status(), 200);
  const coverage = await coverageResponse.json();
  assert.equal(coverage.dependencies.length, 7);
  assert.equal(coverage.enabled, false);
  await page.screenshot({ path: `${output}/template-example.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ status: 'PASSED', upload, selected_motion: retrieval.manifest.items[0].motion_version_id,
    metadata_download: download.suggestedFilename(), template_example: example, staged_template: template.id,
    template_key: coverage.template_key, dependencies: coverage.dependencies, errors }, null, 2));
  console.log('Library upload, metadata download, development activation, immediate retrieval and template example passed.');
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  console.error(await page.locator('[role=alert]').allTextContents()); throw error;
} finally { await browser.close(); }
