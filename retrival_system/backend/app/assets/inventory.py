"""Read-only, bounded, failure-isolated inventory of supplied GLB/metadata pairs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from app.assets.khronos import validate_glb
from app.assets.metadata import load_metadata, metadata_findings, resolve_asset


def inspect_item(path: Path, glb_root: Path, run_khronos: bool = True) -> dict:
    result = {"metadata_file": path.name, "status": "FAILED", "findings": []}
    try:
        metadata, metadata_hash, raw = load_metadata(path)
        asset = resolve_asset(metadata, glb_root)
        result.update(
            metadata_sha256=metadata_hash,
            identity=metadata.motion_identity.model_dump(),
            asset_file=asset.name,
            source_status={
                "technical_qc": metadata.technical_qc,
                "isl_verified": metadata.isl_validation.isl_verified,
                "signer_verdict": metadata.isl_validation.signer_verdict,
                "production_eligible": metadata.production_eligible,
            },
        )
        from app.assets.glb import inspect_glb

        technical = inspect_glb(asset)
        result["technical"] = technical
        digest = technical["sha256"]
        size = technical["size_bytes"]
        result["findings"].extend(metadata_findings(metadata, raw, digest, size))
        clips = technical["clips"]
        selected = [clip for clip in clips if clip["name"] == metadata.animation.animation_name]
        if not selected and len(clips) == 1:
            # A sole clip has an unambiguous technical identity. Retain the source
            # disagreement; this derives a selector, not meaning or approval.
            selected = clips
            result["findings"].append(
                {
                    "code": "SOURCE_CLIP_NAME_DIFFERS",
                    "severity": "warning",
                    "source_clip": metadata.animation.animation_name,
                    "actual_clip": clips[0]["name"],
                }
            )
        if len(selected) != 1:
            result["findings"].append({"code": "SELECTED_CLIP_NOT_UNIQUE", "severity": "error"})
        elif abs(selected[0]["duration_seconds"] - metadata.animation.duration_seconds) > 0.001:
            result["findings"].append(
                {
                    "code": "METADATA_DURATION_DIFFERS",
                    "severity": "warning",
                    "source_seconds": metadata.animation.duration_seconds,
                    "glb_seconds": selected[0]["duration_seconds"],
                }
            )
        if len(selected) == 1:
            result["selected_clip"] = {
                "name": selected[0]["name"],
                "duration_seconds": selected[0]["duration_seconds"],
                "selection_method": "SOURCE_NAME"
                if selected[0]["name"] == metadata.animation.animation_name
                else "SOLE_EMBEDDED_ANIMATION",
            }
        if run_khronos:
            official = validate_glb(asset)
            result["khronos"] = official
            if official["sha256"] != digest:
                result["findings"].append(
                    {
                        "code": "ASSET_CHANGED_DURING_VALIDATION",
                        "severity": "error",
                    }
                )
            if official["errors"] or official["truncated"]:
                result["findings"].append(
                    {
                        "code": "KHRONOS_ERRORS_OR_INCOMPLETE",
                        "severity": "error",
                    }
                )
        else:
            result["khronos"] = {"status": "NOT_RUN"}
        has_error = any(f["severity"] == "error" for f in result["findings"])
        result["status"] = "FAILED" if has_error else "INSPECTED"
    except Exception as exc:
        # One corrupt asset, metadata record or unavailable tool must not lose other results.
        result["findings"].append(
            {
                "code": "INSPECTION_FAILED",
                "severity": "error",
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
        )
    return result


def build_inventory(library: Path, workers: int = 2, run_khronos: bool = True) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError("Use between one and four workers")
    root = library.resolve(strict=True)
    metadata_root, glb_root = root / "metadata", root / "glb"
    if not metadata_root.is_dir() or not glb_root.is_dir():
        raise ValueError("Library must contain metadata/ and glb/ directories")
    files = sorted(metadata_root.glob("*.metadata.json"), key=lambda p: p.name.casefold())
    if not files:
        raise ValueError("No .metadata.json files found")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        items = list(executor.map(lambda path: inspect_item(path, glb_root, run_khronos), files))
    identities = Counter(item["identity"]["motion_code"] for item in items if "identity" in item)
    asset_references = Counter(item["asset_file"] for item in items if "asset_file" in item)
    for item in items:
        identity = item.get("identity", {}).get("motion_code")
        if identity and identities[identity] > 1:
            item["findings"].append({"code": "DUPLICATE_MOTION_IDENTITY", "severity": "error"})
            item["status"] = "FAILED"
        if asset_references.get(item.get("asset_file"), 0) > 1:
            item["findings"].append({"code": "DUPLICATE_ASSET_REFERENCE", "severity": "error"})
            item["status"] = "FAILED"
    orphans = sorted(p.name for p in glb_root.glob("*.glb") if p.name not in asset_references)
    glosses: dict[str, list[str]] = {}
    for item in items:
        identity = item.get("identity", {})
        if identity.get("gloss"):
            glosses.setdefault(identity["gloss"], []).append(identity["motion_code"])
    summary = {
        "metadata_count": len(files),
        "glb_count": len(list(glb_root.glob("*.glb"))),
        "inspected": sum(item["status"] == "INSPECTED" for item in items),
        "failed": sum(item["status"] == "FAILED" for item in items),
        "total_glb_bytes": sum(item.get("technical", {}).get("size_bytes", 0) for item in items),
        "source_isl_verified": sum(
            item.get("source_status", {}).get("isl_verified", False) for item in items
        ),
        "source_production_eligible": sum(
            item.get("source_status", {}).get("production_eligible", False) for item in items
        ),
        "levels": dict(Counter(item["identity"]["level"] for item in items if "identity" in item)),
        "finding_counts": dict(Counter(f["code"] for item in items for f in item["findings"])),
        "unpaired_glbs": orphans,
        "shared_glosses": {gloss: codes for gloss, codes in glosses.items() if len(codes) > 1},
        "rig_fingerprints": dict(
            Counter(
                item["technical"]["rig_fingerprint"]
                for item in items
                if item.get("technical", {}).get("rig_fingerprint")
            )
        ),
    }
    content_digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    avatar_candidate = next(
        (
            item
            for item in items
            if item.get("identity", {}).get("motion_code") == "ISL_TRAIN_01"
            and item["status"] == "INSPECTED"
        ),
        None,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "inventory_sha256": content_digest,
        "validation_scope": "BYTE_STRUCTURE_AND_SOURCE_METADATA",
        "khronos_requested": run_khronos,
        "summary": summary,
        "avatar_candidate": {
            "asset_file": avatar_candidate["asset_file"],
            "sha256": avatar_candidate["technical"]["sha256"],
            "rig_fingerprint": avatar_candidate["technical"]["rig_fingerprint"],
            "selection_scope": "TECHNICAL_BINDING_BASELINE",
        }
        if avatar_candidate
        else None,
        "first_template_requirements": {
            "intent": "TRAIN_ARRIVAL",
            "temporal_state": "ARRIVING_NOW",
            "polarity": "POSITIVE",
            "source_text_language": "en",
            "output_language": "ISL",
            "train_identifier": "Exact text; preserve leading zeros and repeated digits",
            "platform_identifier": "Exact text validated against station configuration",
            "composition": "Requires supplied reviewed ISL recipe, boundaries and number policy",
            "recipe_supplied": False,
        },
        "items": items,
    }


def write_report(report: dict, output: Path, library: Path) -> None:
    destination = output.resolve()
    if destination.is_relative_to(library.resolve()):
        raise ValueError("Write reports outside the input library")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, suffix=".tmp", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(report, temporary, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            temporary_path.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument(
        "--skip-khronos",
        action="store_true",
        help="Inspection only; never establishes a structural validation pass",
    )
    args = parser.parse_args()
    try:
        if args.output.resolve().is_relative_to(args.library.resolve()):
            raise ValueError("Write reports outside the input library")
        report = build_inventory(args.library, args.workers, not args.skip_khronos)
        write_report(report, args.output, args.library)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], sort_keys=True))
    return 1 if report["summary"]["failed"] or report["summary"]["unpaired_glbs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
