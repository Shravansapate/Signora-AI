import { readFile } from "node:fs/promises";
import { validateBytes } from "gltf-validator";

const inputPath = process.argv[2];
if (!inputPath) {
  process.stderr.write("Usage: node tools/validate_glb.mjs <asset.glb>\n");
  process.exit(2);
}

try {
  const bytes = await readFile(inputPath);
  const report = await validateBytes(new Uint8Array(bytes), {
    uri: inputPath,
    maxIssues: 10000,
    externalResourceFunction: async () => {
      throw new Error("Production GLBs must not reference external resources.");
    },
  });
  process.stdout.write(`${JSON.stringify(report)}\n`);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exit(1);
}
