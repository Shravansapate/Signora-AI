"""Fail-closed subprocess adapter for the pinned official Khronos validator."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

VALIDATOR_VERSION = "2.0.0-dev.3.10"
MAX_ASSET_BYTES = 128 * 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "validate-glb.mjs"


class KhronosValidationError(RuntimeError):
    """Validation could not run reliably; this never indicates an approved asset."""


def _check_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise KhronosValidationError("Khronos validator returned a non-object report.")
    if report.get("validator_version") != VALIDATOR_VERSION:
        raise KhronosValidationError("Khronos validator returned an unexpected version.")
    if not isinstance(report.get("sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", report["sha256"]
    ):
        raise KhronosValidationError("Khronos validator returned an invalid checksum.")
    if any(type(report.get(key)) is not int or report[key] < 0 for key in ("errors", "warnings")):
        raise KhronosValidationError("Khronos validator returned invalid issue counts.")
    if type(report.get("truncated")) is not bool:
        raise KhronosValidationError("Khronos validator returned an invalid truncation flag.")
    messages = report.get("messages")
    if not isinstance(messages, list) or len(messages) > 30:
        raise KhronosValidationError("Khronos validator returned an invalid message list.")
    for message in messages:
        if not isinstance(message, dict) or any(
            not isinstance(message.get(key), str) for key in ("code", "message", "pointer")
        ):
            raise KhronosValidationError("Khronos validator returned an invalid issue.")
        if type(message.get("severity")) is not int or message["severity"] not in range(4):
            raise KhronosValidationError("Khronos validator returned an invalid issue severity.")
    return report


def validate_glb(path: Path, timeout_seconds: float = 120) -> dict[str, Any]:
    """Validate actual GLB bytes without resolving any external resource.

    Structural errors are returned in the report. Tooling, I/O, and timeout errors
    raise ``KhronosValidationError``. The checksum identifies the bytes read and
    validated by the child, not a separate read. A truncated report is incomplete:
    its counts are lower bounds and must not be treated as a successful gate.
    This report establishes neither avatar compatibility nor ISL approval.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a finite positive number.")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a finite positive number.")
    node = shutil.which("node")
    if node is None:
        raise KhronosValidationError("Node.js is unavailable; install Node.js to validate assets.")
    if not _SCRIPT.is_file():
        raise KhronosValidationError("The bundled Khronos validation script is missing.")
    try:
        asset_path = Path(path).resolve(strict=True)
        if not asset_path.is_file():
            raise KhronosValidationError("Asset must be a regular file.")
        if asset_path.stat().st_size > MAX_ASSET_BYTES:
            raise KhronosValidationError("Asset exceeds 128 MiB.")
    except OSError as exc:
        raise KhronosValidationError(f"Cannot access the GLB asset: {exc.strerror}.") from exc

    # Avoid inherited Node hooks or search paths modifying the validation process.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"NODE_OPTIONS", "NODE_PATH"}
    }
    try:
        result = subprocess.run(
            [node, "--max-old-space-size=512", str(_SCRIPT), str(asset_path)],
            cwd=_SCRIPT.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise KhronosValidationError(
            f"Khronos validation timed out after {timeout_seconds:g}s."
        ) from exc
    except OSError as exc:
        raise KhronosValidationError(f"Cannot start Khronos validator: {exc.strerror}.") from exc
    if len(result.stdout) > MAX_REPORT_BYTES:
        raise KhronosValidationError("Khronos validator exceeded its report size limit.")
    if result.returncode != 0:
        detail = ""
        try:
            failure = json.loads(result.stdout)
            if isinstance(failure, dict) and isinstance(failure.get("error"), str):
                detail = f" {failure['error'][:512]}"
        except (ValueError, UnicodeError):
            pass
        raise KhronosValidationError(
            f"Khronos validator failed (exit {result.returncode}).{detail} "
            "Check Node.js and run npm ci in backend/tools."
        )
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise KhronosValidationError("Khronos validator returned malformed JSON.") from exc
    return _check_report(report)
