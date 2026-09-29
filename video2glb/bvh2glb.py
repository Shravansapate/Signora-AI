"""bvh2glb.py — BVH → GLB batch pipeline.

Primary workflow:
    bvh_input/*.bvh  →  Blender retargeting  →  glb_output/*.glb

Usage (single file):
    python bvh2glb.py --bvh bvh_input/my_animation.bvh

Usage (batch — all BVH files in bvh_input/):
    python bvh2glb.py --batch

Usage (batch with custom input/output):
    python bvh2glb.py --batch --input-dir path/to/bvh --output-dir path/to/glb

Each conversion runs Blender in a fully isolated subprocess so a crash in one
file never stops the batch. Results are logged with clear SUCCESS / FAILED lines.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BVH_INPUT  = PROJECT_ROOT / "bvh_input"
DEFAULT_GLB_OUTPUT = PROJECT_ROOT / "glb_output"
DEFAULT_CONFIG     = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_BONE_MAP   = PROJECT_ROOT / "config" / "avatar_bone_map.json"
DEFAULT_LOG_DIR    = PROJECT_ROOT / "logs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def discover_bvh_files(input_dir: Path, *, recursive: bool = False) -> list[Path]:
    """Return sorted list of .bvh files from input_dir."""
    if not input_dir.is_dir():
        raise ValueError(f"BVH input directory does not exist: {input_dir}")
    pattern = "**/*.bvh" if recursive else "*.bvh"
    files = sorted(
        {p.resolve() for p in input_dir.glob(pattern) if p.is_file()},
        key=lambda p: p.name.casefold(),
    )
    return files


def resolve_blender_path(config: dict) -> Path:
    """Resolve Blender executable from config or common install locations."""
    configured = config.get("blender", {}).get("executable")
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
    # Common Windows locations
    candidates = [
        Path("C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.4/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.3/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.2/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.1/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.0/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 3.6/blender.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return Path("blender")  # hope it's on PATH


# ---------------------------------------------------------------------------
# Single-file conversion
# ---------------------------------------------------------------------------


def convert_single(
    bvh_path: Path,
    output_path: Path,
    *,
    avatar_path: Path,
    bone_map_path: Path,
    blender_path: Path,
    apply_root_translation: bool = False,
    bvh_scale: float = 0.01,
    log_path: Path | None = None,
    timeout: float = 3600.0,
) -> tuple[bool, str]:
    """Run a single BVH → GLB conversion in an isolated Blender subprocess.

    Returns (success: bool, message: str).
    """
    blender_script = PROJECT_ROOT / "src" / "blender" / "blender_bvh_to_glb.py"
    if not blender_script.is_file():
        return False, f"Blender script not found: {blender_script}"

    cmd = [
        str(blender_path),
        "--background",
        "--python", str(blender_script),
        "--",
        "--bvh", str(bvh_path),
        "--avatar", str(avatar_path),
        "--bone-map", str(bone_map_path),
        "--output", str(output_path),
        "--bvh-scale", str(bvh_scale),
    ]
    if apply_root_translation:
        cmd.append("--apply-root-translation")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log_fh = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "w", encoding="utf-8")

    try:
        t0 = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=(log_fh is None),
            stdout=log_fh,
            stderr=log_fh,
            timeout=timeout,
            env=env,
            cwd=PROJECT_ROOT,
        )
        elapsed = time.monotonic() - t0

        if log_fh:
            log_fh.flush()

        if proc.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 100:
            return True, f"OK ({elapsed:.1f}s, {output_path.stat().st_size // 1024} KB)"

        stderr_tail = ""
        if not log_fh and proc.stderr:
            tail = proc.stderr.decode("utf-8", errors="replace").strip()
            stderr_tail = tail[-400:] if len(tail) > 400 else tail

        return False, (
            f"Blender exited with code {proc.returncode} after {elapsed:.1f}s. "
            + (f"stderr: {stderr_tail}" if stderr_tail else "")
        )

    except subprocess.TimeoutExpired:
        return False, f"Conversion timed out after {timeout:.0f}s."
    except FileNotFoundError:
        return False, f"Blender not found at: {blender_path}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if log_fh:
            log_fh.close()


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def run_batch(args: argparse.Namespace) -> int:
    """Process all BVH files in --input-dir, write GLBs to --output-dir."""
    config = load_yaml(resolve_project_path(args.config))
    input_dir  = resolve_project_path(args.input_dir  or DEFAULT_BVH_INPUT)
    output_dir = resolve_project_path(args.output_dir or DEFAULT_GLB_OUTPUT)
    bone_map_path = resolve_project_path(args.bone_map or DEFAULT_BONE_MAP)
    avatar_path   = resolve_project_path(
        args.avatar or config.get("avatar", {}).get("path") or "character.fbx"
    )
    blender_path  = resolve_blender_path(config)
    log_dir       = resolve_project_path(args.log_dir or DEFAULT_LOG_DIR)

    # Validate shared dependencies
    missing = []
    if not avatar_path.is_file():
        missing.append(f"Avatar FBX not found: {avatar_path}")
    if not bone_map_path.is_file():
        missing.append(f"Bone map not found: {bone_map_path}")
    if missing:
        for m in missing:
            print(f"[ERROR] {m}", flush=True)
        return 1

    bvh_files = discover_bvh_files(input_dir, recursive=bool(getattr(args, "recursive", False)))
    if not bvh_files:
        print(f"[ERROR] No .bvh files found in: {input_dir}", flush=True)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    batch_id = uuid.uuid4().hex[:8]
    bvh_scale = float(getattr(args, "bvh_scale", 0.01))
    apply_root = bool(getattr(args, "apply_root_translation", False))
    timeout = float(getattr(args, "timeout", 3600.0))

    print(f"\n{'='*64}", flush=True)
    print(f"  BVH → GLB Batch Pipeline", flush=True)
    print(f"  Batch ID   : {batch_id}", flush=True)
    print(f"  Input dir  : {input_dir}", flush=True)
    print(f"  Output dir : {output_dir}", flush=True)
    print(f"  Avatar     : {avatar_path}", flush=True)
    print(f"  Bone map   : {bone_map_path}", flush=True)
    print(f"  Blender    : {blender_path}", flush=True)
    print(f"  Files found: {len(bvh_files)}", flush=True)
    print(f"{'='*64}\n", flush=True)

    results: list[dict] = []
    success_count = 0
    fail_count = 0

    for idx, bvh_path in enumerate(bvh_files, start=1):
        stem = bvh_path.stem
        output_glb = output_dir / f"{stem}.glb"
        log_path = log_dir / f"{batch_id}_{stem}.log"

        print(
            f"[{idx:03d}/{len(bvh_files):03d}] INPUT:  {bvh_path.name}",
            flush=True,
        )
        print(
            f"            OUTPUT: {output_glb.name}",
            flush=True,
        )

        started_at = utc_now_iso()
        success, message = convert_single(
            bvh_path=bvh_path,
            output_path=output_glb,
            avatar_path=avatar_path,
            bone_map_path=bone_map_path,
            blender_path=blender_path,
            apply_root_translation=apply_root,
            bvh_scale=bvh_scale,
            log_path=log_path,
            timeout=timeout,
        )
        completed_at = utc_now_iso()

        status = "SUCCESS" if success else "FAILED"
        if success:
            success_count += 1
            print(f"            STATUS: {status} — {message}", flush=True)
        else:
            fail_count += 1
            print(f"            STATUS: {status}", flush=True)
            print(f"            REASON: {message}", flush=True)
            print(f"            LOG:    {log_path}", flush=True)

        results.append({
            "index": idx,
            "input": str(bvh_path),
            "output": str(output_glb),
            "status": status,
            "message": message,
            "log": str(log_path),
            "started_at": started_at,
            "completed_at": completed_at,
        })
        print("", flush=True)

    # Write batch summary
    summary_path = output_dir / f"batch_summary_{batch_id}.json"
    summary = {
        "batch_id": batch_id,
        "total": len(bvh_files),
        "success": success_count,
        "failed": fail_count,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "avatar": str(avatar_path),
        "blender": str(blender_path),
        "items": results,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"{'='*64}", flush=True)
    print(f"  BATCH COMPLETE", flush=True)
    print(f"  Total  : {len(bvh_files)}", flush=True)
    print(f"  SUCCESS: {success_count}", flush=True)
    print(f"  FAILED : {fail_count}", flush=True)
    print(f"  Summary: {summary_path}", flush=True)
    print(f"{'='*64}\n", flush=True)

    return 0 if fail_count == 0 else 1


# ---------------------------------------------------------------------------
# Single-file mode entry
# ---------------------------------------------------------------------------


def run_single(args: argparse.Namespace) -> int:
    config = load_yaml(resolve_project_path(args.config))
    bvh_path    = resolve_project_path(args.bvh)
    output_dir  = resolve_project_path(args.output_dir or DEFAULT_GLB_OUTPUT)
    bone_map_path = resolve_project_path(args.bone_map or DEFAULT_BONE_MAP)
    avatar_path   = resolve_project_path(
        args.avatar or config.get("avatar", {}).get("path") or "character.fbx"
    )
    blender_path  = resolve_blender_path(config)
    log_dir       = resolve_project_path(args.log_dir or DEFAULT_LOG_DIR)

    if not bvh_path.is_file():
        print(f"[ERROR] BVH file not found: {bvh_path}", flush=True)
        return 1
    if not avatar_path.is_file():
        print(f"[ERROR] Avatar FBX not found: {avatar_path}", flush=True)
        return 1
    if not bone_map_path.is_file():
        print(f"[ERROR] Bone map not found: {bone_map_path}", flush=True)
        return 1

    output_glb = output_dir / f"{bvh_path.stem}.glb"
    log_path   = log_dir / f"{bvh_path.stem}.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    bvh_scale  = float(getattr(args, "bvh_scale", 0.01))
    apply_root = bool(getattr(args, "apply_root_translation", False))
    timeout    = float(getattr(args, "timeout", 3600.0))

    print(f"\n{'='*64}", flush=True)
    print(f"  BVH → GLB Single Conversion", flush=True)
    print(f"  INPUT : {bvh_path}", flush=True)
    print(f"  OUTPUT: {output_glb}", flush=True)
    print(f"  Avatar: {avatar_path}", flush=True)
    print(f"  Blender: {blender_path}", flush=True)
    print(f"{'='*64}\n", flush=True)

    success, message = convert_single(
        bvh_path=bvh_path,
        output_path=output_glb,
        avatar_path=avatar_path,
        bone_map_path=bone_map_path,
        blender_path=blender_path,
        apply_root_translation=apply_root,
        bvh_scale=bvh_scale,
        log_path=log_path,
        timeout=timeout,
    )

    if success:
        print(f"\nSUCCESS", flush=True)
        print(f"  Input : {bvh_path}", flush=True)
        print(f"  Output: {output_glb}", flush=True)
        print(f"  Detail: {message}", flush=True)
        return 0
    else:
        print(f"\nFAILED", flush=True)
        print(f"  Input : {bvh_path}", flush=True)
        print(f"  Output: {output_glb} (not created or empty)", flush=True)
        print(f"  Reason: {message}", flush=True)
        print(f"  Log   : {log_path}", flush=True)
        return 1


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BVH → GLB pipeline. Converts BVH motion files to animated GLB via Blender.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert a single BVH file:
  python bvh2glb.py --bvh bvh_input/walk.bvh

  # Process all BVH files in bvh_input/ → glb_output/:
  python bvh2glb.py --batch

  # Batch with custom directories:
  python bvh2glb.py --batch --input-dir /data/bvh --output-dir /data/glb
""",
    )
    # Mode
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bvh", help="Single BVH file to convert.")
    group.add_argument("--batch", action="store_true", help="Process all BVH files in --input-dir.")

    # Paths
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_BVH_INPUT),
        help=f"Directory of .bvh input files for batch mode (default: {DEFAULT_BVH_INPUT}).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_GLB_OUTPUT),
        help=f"Directory to write .glb files (default: {DEFAULT_GLB_OUTPUT}).",
    )
    parser.add_argument(
        "--avatar",
        help="Character FBX path. Overrides config avatar.path.",
    )
    parser.add_argument(
        "--bone-map",
        default=str(DEFAULT_BONE_MAP),
        help=f"avatar_bone_map.json path (default: {DEFAULT_BONE_MAP}).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"settings.yaml path (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"Directory for per-file Blender logs (default: {DEFAULT_LOG_DIR}).",
    )

    # BVH options
    parser.add_argument(
        "--apply-root-translation",
        action="store_true",
        default=False,
        help="Apply BVH root position channels as armature location keyframes.",
    )
    parser.add_argument(
        "--bvh-scale",
        type=float,
        default=0.01,
        help="Scale factor for BVH translations (default: 0.01 = centimetres → metres).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="Maximum seconds for each Blender conversion (default: 3600).",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        default=False,
        help="Scan --input-dir recursively for .bvh files.",
    )
    return parser.parse_args()


def resolve_project_path(value: str | Path | None) -> Path:
    if not value:
        return PROJECT_ROOT
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    if args.batch:
        return run_batch(args)
    return run_single(args)


if __name__ == "__main__":
    sys.exit(main())
