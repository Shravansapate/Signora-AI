"""Verified stage artifacts and compact execution records for resumable conversion."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Callable

from src.metadata.production_metadata import atomic_write_json, sha256_file, utc_now_iso


def dependency_fingerprint(paths: dict[str, Path], settings: dict[str, Any]) -> str:
    hashes = {}
    for name, path in sorted(paths.items()):
        digest = sha256_file(path)
        if digest is None:
            raise FileNotFoundError(f"Missing stage dependency {name}: {path}")
        hashes[name] = digest
    payload = {"schema": 1, "inputs": hashes, "settings": settings}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _copy_atomic(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StageCache:
    """Cache only exact input/code dependencies; validate every artifact before reuse.

    The caller holds the per-video conversion lock. Failed or interrupted stages
    never commit a cache manifest. Validation and human approval are never cached.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.events: list[dict[str, Any]] = []

    def execute(
        self,
        stage: str,
        *,
        inputs: dict[str, Path],
        settings: dict[str, Any],
        outputs: dict[str, Path],
        action: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any]:
        if not stage.isidentifier() or not outputs or any(not role.isidentifier() for role in outputs):
            raise ValueError("Stage and output roles must be identifiers, with at least one output.")
        dependencies = {**inputs, "stage_cache_implementation": Path(__file__)}
        fingerprint = dependency_fingerprint(dependencies, settings)
        entry = self.root / stage / fingerprint
        manifest_path = entry / "manifest.json"
        manifest: dict[str, Any] = {}
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                manifest = candidate
        except (OSError, ValueError):
            pass
        records = manifest.get("outputs", {})
        reusable = (
            manifest.get("fingerprint") == fingerprint
            and isinstance(manifest.get("metadata"), dict)
            and isinstance(records, dict)
            and set(records) == set(outputs)
            and all(
                isinstance(records[role], dict)
                and records[role].get("sha256")
                and sha256_file(entry / role) == records[role]["sha256"]
                for role in outputs
            )
        )
        if reusable:
            for role, destination in outputs.items():
                _copy_atomic(entry / role, destination)
                if sha256_file(destination) != records[role]["sha256"]:
                    raise RuntimeError(f"Restored {stage}/{role} does not match its cache hash.")
            if dependency_fingerprint(dependencies, settings) != fingerprint:
                raise RuntimeError(f"Dependencies changed while restoring {stage}.")
            self.events.append({"stage": stage, "fingerprint": fingerprint, "reused": True})
            return deepcopy(manifest["metadata"])

        metadata = action() or {}
        if not isinstance(metadata, dict):
            raise TypeError("Stage cache metadata must be a JSON object.")
        if dependency_fingerprint(dependencies, settings) != fingerprint:
            raise RuntimeError(f"Dependencies changed while running {stage}.")
        records = {}
        entry.mkdir(parents=True, exist_ok=True)
        for role, path in outputs.items():
            digest = sha256_file(path)
            if digest is None or path.stat().st_size <= 0:
                raise RuntimeError(f"Stage {stage} did not produce a nonempty {role} artifact.")
            _copy_atomic(path, entry / role)
            if sha256_file(entry / role) != digest:
                raise RuntimeError(f"Stage {stage}/{role} changed while being cached.")
            records[role] = {"sha256": digest, "size_bytes": path.stat().st_size}
        atomic_write_json(manifest_path, {
            "schema_version": "1.0", "fingerprint": fingerprint,
            "outputs": records, "metadata": metadata,
        })
        self.events.append({"stage": stage, "fingerprint": fingerprint, "reused": False})
        return metadata


class RunRecorder:
    def __init__(self, path: Path, *, run_id: str, batch_id: str | None, attempt: int) -> None:
        self.path = path
        self.started = time.monotonic()
        self.payload: dict[str, Any] = {
            "schema_version": "1.0", "run_id": run_id, "batch_id": batch_id,
            "attempt": attempt, "status": "RUNNING", "started_at": utc_now_iso(),
            "completed_at": None, "failure_stage": None, "stages": [],
        }

    def execute(self, index: int, label: str, action: Callable[[], Any], guard: Callable[[], None]) -> Any:
        stage = {"index": index, "name": label, "status": "RUNNING", "started_at": utc_now_iso()}
        self.payload["stages"].append(stage)
        atomic_write_json(self.path, self.payload)
        started = time.monotonic()
        try:
            guard()
            result = action()
            guard()
            stage["status"] = "COMPLETED"
            return result
        except BaseException as exc:
            stage["status"] = "FAILED"
            stage["error"] = f"{type(exc).__name__}: {exc}"
            self.payload["failure_stage"] = label
            raise
        finally:
            stage["completed_at"] = utc_now_iso()
            stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
            atomic_write_json(self.path, self.payload)

    def finish(self, status: str, *, error: str | None = None) -> dict[str, Any]:
        self.payload.update(status=status, completed_at=utc_now_iso(),
                            elapsed_seconds=round(time.monotonic() - self.started, 3))
        if error is not None:
            self.payload["error"] = error
        atomic_write_json(self.path, self.payload)
        return deepcopy(self.payload)
