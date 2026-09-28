import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';

const MAX_ASSET_BYTES = 128 * 1024 * 1024;
const MAX_ISSUES = 30;
const EXPECTED_VERSION = '2.0.0-dev.3.10';

// Read from one descriptor with a hard byte limit, including if the file grows.
// Neither this wrapper nor the validator may read a referenced file or URL.
async function readAsset(path) {
  const handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) throw new Error('Asset must be a regular file.');
    if (stat.size > MAX_ASSET_BYTES) throw new Error('Asset exceeds 128 MiB.');
    const chunks = [];
    let total = 0;
    while (true) {
      const chunk = Buffer.allocUnsafe(Math.min(65536, MAX_ASSET_BYTES - total + 1));
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (bytesRead === 0) break;
      total += bytesRead;
      if (total > MAX_ASSET_BYTES) throw new Error('Asset exceeds 128 MiB.');
      chunks.push(chunk.subarray(0, bytesRead));
    }
    return Buffer.concat(chunks, total);
  } finally {
    await handle.close();
  }
}

async function main() {
  if (process.argv.length !== 3) throw new Error('Exactly one GLB path is required.');
  const { default: validator } = await import('gltf-validator');
  if (validator.version() !== EXPECTED_VERSION) {
    throw new Error('Unexpected gltf-validator version. Run npm ci in backend/tools.');
  }
  const bytes = await readAsset(process.argv[2]);
  const report = await validator.validateBytes(bytes, {
    format: 'glb',
    maxIssues: MAX_ISSUES,
    writeTimestamp: false,
    externalResourceFunction: () => Promise.reject(new Error('External resources are forbidden.')),
  });
  let truncated = report.issues.truncated === true;
  function boundedText(value, limit) {
    const text = typeof value === 'string' ? value : '';
    if (text.length > limit) truncated = true;
    return text.slice(0, limit);
  }
  const messages = report.issues.messages.slice(0, MAX_ISSUES).map((issue) => ({
    code: boundedText(issue.code, 96),
    message: boundedText(issue.message, 512),
    severity: issue.severity,
    pointer: boundedText(issue.pointer, 256),
  }));
  if (report.issues.messages.length > MAX_ISSUES) truncated = true;
  process.stdout.write(JSON.stringify({
    validator_version: report.validatorVersion,
    sha256: createHash('sha256').update(bytes).digest('hex'),
    errors: report.issues.numErrors,
    warnings: report.issues.numWarnings,
    truncated,
    messages,
  }) + '\n');
}

main().catch((error) => {
  // Keep stdout machine-readable for every outcome and do not emit stack traces.
  process.stdout.write(JSON.stringify({ error: String(error.message ?? error).slice(0, 512) }) + '\n');
  process.exitCode = 2;
});
