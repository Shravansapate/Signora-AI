import { readFile, stat, writeFile } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';
import { LoadingManager, REVISION } from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { captureRigProfile, compareRigProfiles, validateClipBindings } from '../src/motion/rig-validation.mjs';
import { probeSequence } from './mixer-probe.mjs';

function disposeScene(root) {
  const geometries = new Set();
  const materials = new Set();
  const skeletons = new Set();
  root.traverse((node) => {
    if (node.geometry) geometries.add(node.geometry);
    if (node.material) for (const material of Array.isArray(node.material) ? node.material : [node.material]) materials.add(material);
    if (node.skeleton) skeletons.add(node.skeleton);
  });
  for (const geometry of geometries) geometry.dispose();
  for (const material of materials) material.dispose();
  for (const skeleton of skeletons) skeleton.dispose();
}

/** Headless inspection only: no image decoding, WebGL rendering, resource substitution, or source writes. */
export async function loadTechnicalGlb(path) {
  const info = await stat(path);
  if (info.size < 20 || info.size > 256 * 1024 * 1024) throw new Error(`Unsupported GLB size: ${path}`);
  const bytes = await readFile(path);
  if (bytes.readUInt32LE(0) !== 0x46546c67 || bytes.readUInt32LE(4) !== 2 || bytes.readUInt32LE(8) !== bytes.length || bytes.readUInt32LE(16) !== 0x4e4f534a) {
    throw new Error(`Invalid GLB header: ${path}`);
  }
  const jsonLength = bytes.readUInt32LE(12);
  if (jsonLength > bytes.length - 20) throw new Error(`Truncated GLB JSON: ${path}`);
  const document = JSON.parse(bytes.subarray(20, 20 + jsonLength).toString('utf8'));
  for (const resource of [...(document.buffers ?? []), ...(document.images ?? [])]) {
    if (resource.uri !== undefined) throw new Error('The headless verifier accepts embedded resources only.');
  }
  const manager = new LoadingManager();
  manager.setURLModifier(() => { throw new Error('External resource loading is forbidden in technical verification.'); });
  const loader = new GLTFLoader(manager);
  loader.register(() => ({
    name: 'SIGNORA_HEADLESS_SKIP_TEXTURE_DECODING',
    loadTexture: async () => null,
  }));
  const started = performance.now();
  const gltf = await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength), '');
  if (gltf.animations.length !== 1) {
    disposeScene(gltf.scene);
    throw new Error('Select an explicit clip before verifying a GLB with zero or multiple animations.');
  }
  return {
    gltf,
    sha256: createHash('sha256').update(bytes).digest('hex'),
    byteLength: bytes.length,
    parseMilliseconds: Number((performance.now() - started).toFixed(2)),
    textureImagesNotDecoded: document.images?.length ?? 0,
  };
}

export async function verifyBindings({ assets, candidate = 'Train.glb', motions = ['Arrive.glb', 'Platform.glb', '1_One.glb', 'A.glb', 'Help.glb', 'Tomorrow.glb'] }) {
  for (const name of [candidate, ...motions]) {
    if (!name.endsWith('.glb') || name.includes('/') || name.includes('\\') || name.includes(':')) throw new Error('Asset names must be plain GLB filenames.');
  }
  const loadedCandidate = await loadTechnicalGlb(join(assets, candidate));
  const root = loadedCandidate.gltf.scene;
  const profile = captureRigProfile(root);
  let meshCount = 0;
  root.traverse((node) => { if (node.isMesh) meshCount++; });
  const clips = [];
  const assetsReport = [];
  try {
    for (const filename of [...new Set([candidate, ...motions])]) {
      let loaded;
      try {
        loaded = filename === candidate ? loadedCandidate : await loadTechnicalGlb(join(assets, filename));
        const clip = loaded.gltf.animations[0];
        const rig = compareRigProfiles(profile, captureRigProfile(loaded.gltf.scene));
        const bindings = validateClipBindings(root, clip);
        // Clips are detached data. Never attach a motion's duplicate meshes to the candidate scene.
        clips.push(clip);
        assetsReport.push({
          filename,
          sha256: loaded.sha256,
          bytes: loaded.byteLength,
          parseMilliseconds: loaded.parseMilliseconds,
          textureImagesNotDecoded: loaded.textureImagesNotDecoded,
          clip: clip.name,
          durationSeconds: clip.duration,
          boundTrackCount: bindings.length,
          animatedTargets: new Set(bindings.map((binding) => binding.node.uuid)).size,
          rig,
          sampledPlayback: probeSequence(root, [clip]),
        });
      } catch (error) {
        assetsReport.push({ filename, error: { code: error.code ?? 'LOAD_OR_PROBE_FAILED', message: error.message } });
      } finally {
        if (loaded && loaded !== loadedCandidate) disposeScene(loaded.gltf.scene);
      }
    }
    const failures = assetsReport.filter((asset) => asset.error);
    const sequences = failures.length ? [] : [
      probeSequence(root, [clips[0]]),
      probeSequence(root, [clips[0], clips[1] ?? clips[0], clips[0]]),
      probeSequence(root, Array.from({ length: 12 }, (_, index) => clips[index % clips.length])),
      probeSequence(root, Array(12).fill(clips[0])),
    ];
    let finalMeshCount = 0;
    root.traverse((node) => { if (node.isMesh) finalMeshCount++; });
    if (finalMeshCount !== meshCount) throw new Error('Candidate scene mesh count changed.');
    return {
      schemaVersion: 1,
      threeRevision: REVISION,
      status: failures.length ? 'BINDING_PROBE_FAILED' : 'BINDING_PROBE_PASSED',
      canonicalAvatarApproved: false,
      productionEligible: false,
      candidate: { filename: candidate, sha256: loadedCandidate.sha256, bones: profile.bones.length, skins: profile.skins.length, meshCount, finalMeshCount },
      compatibleWithCandidate: assetsReport.every((asset) => asset.rig?.compatible),
      limitations: [
        'Candidate chosen only for engineering inspection; no canonical avatar approval is asserted.',
        'Headless probe skips texture decoding and WebGL rendering; it cannot validate appearance, collisions, visual transitions, or signing meaning.',
        'Diagnostic mixer sampling does not bypass incompatibility: initial-transform or bind-pose differences require resolution before activation.',
        'Sequential clips use no reviewed transition/composition policy; these are technical occurrences, not railway announcements.',
        'Parse timings are local headless measurements, not display-device download, decode, frame-rate, or memory benchmarks.',
      ],
      assets: assetsReport,
      sequences,
    };
  } finally {
    disposeScene(root);
  }
}

async function main() {
  const { values } = parseArgs({ options: {
    assets: { type: 'string', default: resolve(dirname(fileURLToPath(import.meta.url)), '../../metadata_json and glb/glb') },
    candidate: { type: 'string', default: 'Train.glb' },
    motion: { type: 'string', multiple: true },
    report: { type: 'string' },
  } });
  const report = await verifyBindings({ assets: resolve(values.assets), candidate: values.candidate, ...(values.motion ? { motions: values.motion } : {}) });
  const content = `${JSON.stringify(report, null, 2)}\n`;
  if (values.report) await writeFile(resolve(values.report), content, { flag: 'wx' });
  process.stdout.write(content);
  if (report.status !== 'BINDING_PROBE_PASSED') process.exitCode = 1;
  // 2 means bindings execute, but the separate compatibility gate has not passed.
  else if (!report.compatibleWithCandidate) process.exitCode = 2;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${JSON.stringify({ status: 'FAILED', code: error.code ?? 'VERIFICATION_FAILED', message: error.message })}\n`);
    process.exitCode = 1;
  });
}
