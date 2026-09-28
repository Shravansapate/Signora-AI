"""Behavioral checks using the real Khronos engine and isolated failure injection."""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest

from app.assets import khronos
from app.assets.khronos import KhronosValidationError, validate_glb


def _glb(path: Path, document: dict, binary: bytes | None = None) -> Path:
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    chunks = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    if binary is not None:
        binary += b"\x00" * (-len(binary) % 4)
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, len(chunks) + 12) + chunks)
    return path


@pytest.fixture
def valid_glb(tmp_path: Path) -> Path:
    # A real two-key translation animation. No production asset or metadata is fabricated.
    binary = struct.pack("<8f", 0, 1, 0, 0, 0, 1, 0, 0)
    return _glb(
        tmp_path / "two keys with spaces.glb",
        {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"name": "Joint"}],
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": 8},
                {"buffer": 0, "byteOffset": 8, "byteLength": 24},
            ],
            "accessors": [
                {
                    "bufferView": 0,
                    "componentType": 5126,
                    "count": 2,
                    "type": "SCALAR",
                    "min": [0],
                    "max": [1],
                },
                {"bufferView": 1, "componentType": 5126, "count": 2, "type": "VEC3"},
            ],
            "animations": [
                {
                    "samplers": [{"input": 0, "output": 1}],
                    "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}],
                }
            ],
        },
        binary,
    )


def test_real_validator_accepts_animation_and_hashes_validated_bytes(valid_glb: Path) -> None:
    report = validate_glb(valid_glb)
    assert report["validator_version"] == "2.0.0-dev.3.10"
    assert report["sha256"] == hashlib.sha256(valid_glb.read_bytes()).hexdigest()
    assert report["errors"] == 0
    assert report["warnings"] == 0
    assert report["truncated"] is False


def test_real_validator_reports_invalid_glb(tmp_path: Path) -> None:
    asset = tmp_path / "malformed.glb"
    asset.write_bytes(b"this is not a GLB")
    report = validate_glb(asset)
    assert report["errors"] > 0
    assert any(issue["severity"] == 0 for issue in report["messages"])


@pytest.mark.parametrize(
    "uri", ["secret.bin", "https://127.0.0.1:9/forbidden.bin", "file:///etc/passwd"]
)
def test_real_validator_rejects_external_resources(tmp_path: Path, uri: str) -> None:
    # A referenced local file exists and is valid, so rejection cannot be explained
    # by the file being absent. Network and file URIs take the same denial path.
    (tmp_path / "secret.bin").write_bytes(b"\x00" * 4)
    asset = _glb(
        tmp_path / "external.glb",
        {
            "asset": {"version": "2.0"},
            "buffers": [{"byteLength": 4, "uri": uri}],
        },
    )
    report = validate_glb(asset)
    assert report["errors"] > 0
    assert any(
        "External resources are forbidden" in issue["message"] for issue in report["messages"]
    )


def test_real_validator_bounds_many_issues(tmp_path: Path) -> None:
    asset = _glb(
        tmp_path / "many-issues.glb",
        {
            "asset": {"version": "2.0"},
            "nodes": [{"rotation": [0, 0, 0, 0]} for _ in range(100)],
        },
    )
    report = validate_glb(asset)
    assert report["truncated"] is True
    assert report["errors"] > 0
    assert len(report["messages"]) <= 30


def test_missing_node_is_explicit(monkeypatch: pytest.MonkeyPatch, valid_glb: Path) -> None:
    monkeypatch.setattr(khronos.shutil, "which", lambda _: None)
    with pytest.raises(KhronosValidationError, match="Node.js is unavailable"):
        validate_glb(valid_glb)


def test_missing_script_is_explicit(
    monkeypatch: pytest.MonkeyPatch, valid_glb: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(khronos, "_SCRIPT", tmp_path / "missing.mjs")
    with pytest.raises(KhronosValidationError, match="script is missing"):
        validate_glb(valid_glb)


def test_missing_asset_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(KhronosValidationError, match="Cannot access"):
        validate_glb(tmp_path / "missing.glb")


def test_oversized_asset_rejected_before_child(tmp_path: Path) -> None:
    path = tmp_path / "oversized.glb"
    with path.open("wb") as stream:
        stream.truncate(khronos.MAX_ASSET_BYTES + 1)
    with pytest.raises(KhronosValidationError, match="128 MiB"):
        validate_glb(path)


def test_real_child_timeout_is_explicit(valid_glb: Path) -> None:
    with pytest.raises(KhronosValidationError, match="timed out"):
        validate_glb(valid_glb, timeout_seconds=0.001)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "120"])
def test_invalid_timeout_rejected(valid_glb: Path, timeout: object) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        validate_glb(valid_glb, timeout_seconds=timeout)


@pytest.mark.parametrize(
    "returncode,stdout,match",
    [
        (2, b'{"error":"Cannot find package gltf-validator"}', "Cannot find package"),
        (1, b"crash", "failed"),
        (0, b"not json", "malformed JSON"),
        (0, b"[]", "non-object"),
        (0, b"{}", "unexpected version"),
        (0, b" " * (khronos.MAX_REPORT_BYTES + 1), "report size limit"),
    ],
    ids=[
        "missing-dependency",
        "crash",
        "malformed-json",
        "non-object",
        "wrong-version",
        "oversized-report",
    ],
)
def test_child_failure_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch, valid_glb: Path, returncode: int, stdout: bytes, match: str
) -> None:
    monkeypatch.setattr(
        khronos.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout, b""),
    )
    with pytest.raises(KhronosValidationError, match=match):
        validate_glb(valid_glb)


def test_node_environment_hooks_are_not_inherited(
    monkeypatch: pytest.MonkeyPatch, valid_glb: Path
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require=does-not-exist.js")
    monkeypatch.setenv("NODE_PATH", "/nonexistent/injected/path")
    assert validate_glb(valid_glb)["errors"] == 0
