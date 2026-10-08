"""Explicit model provisioning; serving never downloads weights or executes remote code."""

import argparse
import hashlib
import json
from pathlib import Path

MODELS = {
    "base.en": ("Systran/faster-whisper-base.en", "3d3d5dee26484f91867d81cb899cfcf72b96be6c"),
    "small.en": ("Systran/faster-whisper-small.en", "d1d751a5f8271d482d14ca55d9e2deeebbae577f"),
}
MODEL_ID, MODEL_REVISION = MODELS["small.en"]
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_model(directory):
    root = Path(directory).resolve(strict=True)
    manifest = json.loads((root / "signora-model.json").read_text(encoding="utf-8"))
    if (manifest.get("model_id"), manifest.get("revision")) not in MODELS.values():
        raise ValueError("Unsupported model identity or revision")
    if set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Incomplete model snapshot")
    for name in FILES:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or checksum(path) != manifest["files"][name]:
            raise ValueError("Model snapshot failed integrity validation")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Provision the pinned local English ASR model")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--model", choices=MODELS, default="small.en")
    arguments = parser.parse_args()
    model_id, revision = MODELS[arguments.model]
    from huggingface_hub import snapshot_download

    directory = arguments.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=model_id, revision=revision, allow_patterns=list(FILES), local_dir=directory
    )
    manifest = {
        "model_id": model_id,
        "revision": revision,
        "files": {name: checksum(directory / name) for name in FILES},
    }
    (directory / "signora-model.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify_model(directory)
    print(json.dumps({"model_id": model_id, "revision": revision, "verified_files": len(FILES)}))


if __name__ == "__main__":
    main()
