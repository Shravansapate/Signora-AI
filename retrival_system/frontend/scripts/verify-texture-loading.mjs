/** Real supplied GLB, browser decoders, fault injection and resource cleanup. No backend writes. */
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const origin = 'http://signora-texture.test';
const output = new URL('../../artifacts/texture-verification/', import.meta.url);
const files = new Map([
  ['/asset.glb', new URL('../../metadata_json and glb/glb/Train.glb', import.meta.url)],
  ...['asset-store.mjs', 'rig-validation.mjs', 'texture-loader.mjs'].map(name => [`/motion/${name}`, new URL(`../src/motion/${name}`, import.meta.url)]),
  ...['build/three.module.js', 'build/three.core.js', 'examples/jsm/loaders/GLTFLoader.js', 'examples/jsm/utils/BufferGeometryUtils.js', 'examples/jsm/utils/SkeletonUtils.js'].map(name => [`/three/${name}`, new URL(`../node_modules/three/${name}`, import.meta.url)]),
]);
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const page = await browser.newPage();
await page.route(`${origin}/**`, async route => {
  const path = new URL(route.request().url()).pathname;
  if (path === '/') return route.fulfill({ contentType: 'text/html', body: '<script type="importmap">{"imports":{"three":"/three/build/three.module.js","three/addons/":"/three/examples/jsm/"}}</script>' });
  if (!files.has(path)) { console.error('Unexpected test request:', path); return route.abort(); }
  await route.fulfill({ body: await readFile(files.get(path)), contentType: path.endsWith('.glb') ? 'model/gltf-binary' : 'text/javascript' });
});
try {
  await page.goto(origin);
  const result = await page.evaluate(async () => {
    const { parseBrowserGlb, disposeScenes, inspectSelfContainedGlb } = await import('/motion/asset-store.mjs');
    const bytes = await (await fetch('/asset.glb')).arrayBuffer();
    const document = inspectSelfContainedGlb(bytes);
    const native = window.createImageBitmap;
    let activeDecodes = 0, peakDecodes = 0;
    const observedBitmap = async (...args) => {
      activeDecodes++; peakDecodes = Math.max(peakDecodes, activeDecodes);
      try { return await native(...args); } finally { activeDecodes--; }
    };
    const created = new Set();
    const makeURL = URL.createObjectURL.bind(URL), revokeURL = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = blob => { const url = makeURL(blob); created.add(url); return url; };
    URL.revokeObjectURL = url => { created.delete(url); revokeURL(url); };
    const outcomes = [];
    for (const mode of ['normal', 'bitmap-rejects', 'bitmap-resize-rejects', 'bitmap-unavailable', 'normal-after-reset']) {
      window.createImageBitmap = mode === 'bitmap-rejects' ? async () => { throw new DOMException('Injected decoder failure', 'InvalidStateError'); }
        : mode === 'bitmap-resize-rejects' ? async (...args) => {
          if (args[0] instanceof ImageBitmap) throw new DOMException('Injected resize failure', 'InvalidStateError');
          return observedBitmap(...args);
        } : mode === 'bitmap-unavailable' ? undefined : observedBitmap;
      let loaded;
      try {
        loaded = await parseBrowserGlb(bytes);
        const dimensions = loaded.textures.map(t => [t.image.width, t.image.height]);
        if (loaded.textures.length !== document.textures.length || dimensions.some(([w, h]) => w <= 0 || h <= 0 || Math.max(w, h) > 1024)) throw new Error('Missing or unbounded avatar textures');
        outcomes.push({ mode, textures: dimensions, animations: loaded.animations.length });
      } finally { if (loaded) disposeScenes(loaded.scenes, loaded.textures); }
      if (created.size) throw new Error(`Leaked ${created.size} embedded image URLs`);
    }
    window.createImageBitmap = native;
    const corrupt = bytes.slice(0);
    const jsonLength = new DataView(corrupt).getUint32(12, true);
    const view = document.bufferViews[document.images[0].bufferView];
    new Uint8Array(corrupt, jsonLength + 28 + (view.byteOffset ?? 0), view.byteLength).fill(0);
    let corruptCode;
    try { await parseBrowserGlb(corrupt); } catch (error) { corruptCode = error.code; }
    if (corruptCode !== 'TEXTURE_DECODE') throw new Error('Corrupted embedded image was not rejected');
    if (created.size) throw new Error('Failed decode leaked embedded image URLs');
    const motion = await parseBrowserGlb(bytes, { motionOnly: true });
    const motionTextures = motion.textures.length;
    disposeScenes(motion.scenes, motion.textures);
    return { outcomes, peak_parallel_bitmap_decodes: peakDecodes, corrupt_image_rejected: true, leaked_urls: created.size, motion_textures: motionTextures };
  });
  assert.equal(result.motion_textures, 0);
  assert.equal(result.leaked_urls, 0);
  assert.equal(result.peak_parallel_bitmap_decodes, 1);
  await mkdir(output, { recursive: true });
  await writeFile(new URL('browser.json', output), JSON.stringify({ status: 'PASSED', browser: browser.version(), ...result }, null, 2));
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
