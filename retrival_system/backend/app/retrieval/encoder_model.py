"""Pinned E5 ONNX snapshot; provisioning is explicit and serving stays local."""

import argparse
import hashlib
import json
from pathlib import Path

MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
ENCODING_POLICY = "e5-query-passage-mean-mask-l2-384-onnx-fp32-v1"
FILES = ("onnx/model.onnx", "onnx/tokenizer.json", "onnx/config.json")


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify(directory):
    root = Path(directory).resolve(strict=True)
    manifest = json.loads((root / "signora-encoder.json").read_text())
    if (manifest.get("model_id"), manifest.get("revision"), manifest.get("policy")) != (
        MODEL_ID,
        MODEL_REVISION,
        ENCODING_POLICY,
    ) or set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Incompatible E5 snapshot")
    for name in FILES:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or checksum(path) != manifest["files"][name]:
            raise ValueError("Encoder snapshot integrity failure")


def main():
    parser = argparse.ArgumentParser(description="Provision the pinned E5 encoder")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    from huggingface_hub import snapshot_download

    root = args.directory.resolve()
    snapshot_download(
        repo_id=MODEL_ID, revision=MODEL_REVISION, allow_patterns=list(FILES), local_dir=root
    )
    (root / "signora-encoder.json").write_text(
        json.dumps(
            {
                "model_id": MODEL_ID,
                "revision": MODEL_REVISION,
                "policy": ENCODING_POLICY,
                "files": {name: checksum(root / name) for name in FILES},
            },
            indent=2,
        )
    )
    verify(root)
    print(json.dumps({"model_id": MODEL_ID, "revision": MODEL_REVISION, "verified": True}))


if __name__ == "__main__":
    main()
