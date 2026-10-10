/** Mutating UI checks run only against the disposable Python fixture and worker. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = process.env.SIGNORA_BROWSER_ORIGIN, token = process.env.SIGNORA_BROWSER_ADMIN_TOKEN;
const fixture = JSON.parse(process.env.SIGNORA_BROWSER_WORKSPACE_DATA || 'null'), output = process.env.SIGNORA_BROWSER_OUTPUT;
if (!origin || !token || !fixture?.replacement || !output) throw new Error('Use test_library_browser isolated harness.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
page.setDefaultTimeout(30000);
const errors = [], checks = [], deleted = [];
page.on('pageerror', error => errors.push(error.message));
const click = name => page.getByRole('button', { name, exact: true }).click();
const status = value => page.getByRole('status').filter({ hasText: value }).first().waitFor({ timeout: 120000 });
async function management(tab) {
  await page.goto(`${origin}/admin?tab=${tab}`);
  await page.getByLabel('Management access token', { exact: true }).fill(token);
  await click('Connect management');
}
async function api(path, body, method) {
  const response = await page.request.fetch(`${origin}/api/v1${path}`, {
    method: method || (body ? 'POST' : 'GET'), headers: { Authorization: `Bearer ${token}` }, ...(body ? { data: body } : {}),
  });
  assert(response.ok(), `${path}: ${response.status()} ${await response.text()}`);
  return response.json();
}
async function translate(text) { return api('/translate', { request_id: crypto.randomUUID(), station_id: 'TEST', input_type: 'TEXT', text }); }
async function motions(text) { return ((await translate(text)).manifest?.items || []).map(item => item.motion_version_id); }
async function acknowledge() {
  await page.getByLabel('Change / review reason', { exact: true }).fill('Isolated browser maintenance verification');
  await page.getByLabel('I checked the selected exact version, intended change, evidence and affected templates.', { exact: true }).check();
}
async function action(name) { await acknowledge(); await click(name); await status('Version change recorded.'); }
async function upload(which, newConcept = true) {
  const files = fixture[which];
  await page.getByLabel('Upload target', { exact: true }).selectOption(newConcept ? 'new' : 'selected');
  await page.getByLabel('Metadata file', { exact: true }).setInputFiles(files.metadata);
  await page.getByLabel('GLB file', { exact: true }).setInputFiles(files.motion);
  const response = page.waitForResponse(r => r.request().method() === 'POST' && /\/motions(?:\/stage)?$/u.test(new URL(r.url()).pathname), { timeout: 180000 });
  await click('Validate and stage upload');
  const result = await response; assert.equal(result.status(), 201, await result.text());
  await status('Upload validated and staged.');
  return result.json();
}
async function remove(data, key) {
  await page.getByLabel('Motion version', { exact: true }).selectOption(data.motion_version_id);
  await click('Delete GLB + metadata');
  await page.getByLabel('Deletion reason', { exact: true }).fill('Delete disposable browser test content');
  await page.getByLabel(`Type ${key} to confirm deletion`, { exact: true }).fill(key);
  await click('Permanently delete selected GLB + metadata');
  await status('Version and metadata deleted.');
  await page.getByRole('status').filter({ hasText: 'Managed GLB cleanup: COMPLETE' }).waitFor();
  deleted.push(data.motion_version_id);
}
try {
  await management('library');
  const icon = await page.locator('link[rel="icon"]').getAttribute('href');
  assert(icon?.includes('/icon.png'));
  const iconResponse = await page.request.get(new URL(icon, origin).href);
  assert.equal(iconResponse.status(), 200);
  assert.match(iconResponse.headers()['content-type'], /image\/png/u);
  checks.push('provided_logo_served_as_browser_favicon');
  await page.getByLabel('GLB file', { exact: true }).setInputFiles(fixture.word.motion);
  await status('Metadata is required.');
  assert(await page.getByRole('button', { name: 'Validate and stage upload', exact: true }).isDisabled());
  checks.push('missing_metadata_explained_before_upload');

  const first = await upload('word');
  assert.deepEqual(await motions(fixture.word.label), []);
  await action('Activate selected version');
  assert.deepEqual(await motions(fixture.word.label), [first.motion_version_id]);
  await click('Preview selected version');
  await status('Review playback: READY');
  await click('Play review'); await status('Review playback: COMPLETE');
  checks.push('new_word_upload_activation_retrieval_and_real_glb_playback');

  await page.getByLabel('New library alias', { exact: true }).fill('browserharbouralias');
  await acknowledge(); await click('Add library alias'); await status('Library alias added.');
  assert.deepEqual(await motions('browserharbouralias'), [first.motion_version_id]);
  await acknowledge(); await click('Remove alias browserharbouralias'); await status('Alias removed from retrieval.');
  assert.deepEqual(await motions('browserharbouralias'), []);
  checks.push('frontend_alias_add_and_remove_immediately_change_retrieval');

  const second = await upload('replacement', false);
  assert.deepEqual(await motions(fixture.word.label), [first.motion_version_id]);
  await action('Activate selected version');
  assert.deepEqual(await motions(fixture.word.label), [second.motion_version_id]);
  await page.getByLabel('Motion version', { exact: true }).selectOption(first.motion_version_id);
  await action('Roll back to selected version');
  assert.deepEqual(await motions(fixture.word.label), [first.motion_version_id]);
  await page.getByLabel('Motion version', { exact: true }).selectOption(second.motion_version_id);
  await action('Activate selected version');
  const oldPlan = (await translate(fixture.word.label)).manifest;
  await remove(second, fixture.word.key);
  assert.deepEqual(await motions(fixture.word.label), []);
  assert.equal((await page.request.get(`${origin}/api/v1/playback/${oldPlan.manifest_id}`, { headers: { Authorization: `Bearer ${token}` } })).status(), 409);
  const third = await upload('replacement', false);
  assert.notEqual(third.motion_version_id, second.motion_version_id);
  await action('Activate selected version');
  assert.deepEqual(await motions(fixture.word.label), [third.motion_version_id]);
  checks.push('replace_rollback_delete_worker_cleanup_and_identical_reupload_without_restart');

  await page.getByText('Metadata file and revisions', { exact: true }).click();
  await click('Edit metadata here');
  const editor = page.getByLabel('Registered metadata JSON', { exact: true });
  await editor.waitFor();
  const metadata = JSON.parse(await editor.inputValue());
  metadata.motion_identity.canonical_text = 'browserhaven'; metadata.motion_identity.gloss = 'BROWSERHAVEN';
  await editor.fill(JSON.stringify(metadata)); await acknowledge(); await click('Save metadata revision');
  await status('Metadata revision saved.');
  assert.deepEqual(await motions('browserhaven'), []);
  assert.deepEqual(await motions(fixture.word.label), []);
  await action('Activate selected version');
  assert.deepEqual(await motions('browserhaven'), [third.motion_version_id]);
  await action('Deactivate concept'); assert.deepEqual(await motions('browserhaven'), []);
  await action('Reactivate concept'); assert.deepEqual(await motions('browserhaven'), [third.motion_version_id]);
  await remove(third, fixture.word.key);
  checks.push('metadata_edited_in_browser_and_old_labels_excluded_until_reactivation');

  for (const kind of ['phrase', 'place']) {
    const added = await upload(kind);
    await action('Activate selected version');
    assert.deepEqual(await motions(fixture[kind].label), [added.motion_version_id]);
    await remove(added, fixture[kind].key);
    assert.deepEqual(await motions(fixture[kind].label), []);
  }
  checks.push('phrase_and_place_motion_add_activate_delete_without_stale_results');

  await management('stations'); await click('Test station');
  await click('Add place');
  await page.getByLabel('Place 1 ID', { exact: true }).fill('PORT');
  await page.getByLabel('Place 1 name', { exact: true }).fill('Browserport');
  await page.getByLabel('Place 1 aliases (one per line)', { exact: true }).fill('Browserquay');
  await click('Add train');
  await page.getByLabel('Train 1 ID', { exact: true }).fill('EXPRESS');
  await page.getByLabel('Train 1 name', { exact: true }).fill('Browser Express');
  async function saveStation() {
    await page.getByLabel('Station change reason', { exact: true }).fill('Isolated station vocabulary maintenance');
    await page.getByLabel('I checked the station definition. Updating it withdraws affected live announcements.', { exact: true }).check();
    await click('Save station configuration'); await status('Station configuration recorded.');
  }
  await saveStation();
  let definition = (await api('/input/capabilities')).stations.find(s => s.id === 'TEST').definition;
  assert.equal(definition.places[0].aliases[0], 'Browserquay'); assert.equal(definition.trains[0].id, 'EXPRESS');
  let receipt = await translate('Train arrives from Browserquay');
  assert.equal(receipt.meaning.slots.source.value, 'PORT'); assert.equal(receipt.meaning.slots.source.valid, true);
  await page.getByLabel('Place 1 name', { exact: true }).fill('New Browserport');
  await page.getByLabel('Place 1 aliases (one per line)', { exact: true }).fill('New Browserquay');
  await saveStation();
  receipt = await translate('Train arrives from Browserquay');
  assert.equal(receipt.meaning.slots.source.valid, false);
  receipt = await translate('Train arrives from New Browserquay');
  assert.equal(receipt.meaning.slots.source.value, 'PORT');
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: `${output}/station-vocabulary-mobile.png`, fullPage: true });
  await click('Remove place 1'); await click('Remove train 1'); await saveStation();
  definition = (await api('/input/capabilities')).stations.find(s => s.id === 'TEST').definition;
  assert.deepEqual(definition.places, []); assert.deepEqual(definition.trains, []);
  assert.equal((await translate('Train arrives from New Browserquay')).meaning.slots.source.valid, false);
  checks.push('place_and_train_forms_add_edit_remove_with_immediate_entity_resolution');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/verification.json`, JSON.stringify({ checks, deleted_versions: deleted, errors }, null, 2));
  console.log(JSON.stringify({ checks }));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  console.error(await page.getByRole('alert').allTextContents()); throw error;
} finally { await browser.close(); }
