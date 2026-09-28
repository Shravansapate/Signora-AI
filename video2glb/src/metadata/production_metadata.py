from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import struct
import tempfile
import platform
import tomllib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.qc.production_gate import evaluate_production_gate


METADATA_SCHEMA_VERSION = "3.1"
COORDINATE_SYSTEM_VERSION = "canonical-image-aspect-corrected-v2"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: str | Path) -> str | None:
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_record(path: str | Path | None, project_root: str | Path) -> dict[str, Any] | None:
    if path is None:
        return None
    target = Path(path).resolve()
    root = Path(project_root).resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError:
        relative = None
    return {
        "absolute_path": target.as_posix(),
        "relative_path": relative,
        "exists": target.exists(),
        "sha256": sha256_file(target),
        "size_bytes": target.stat().st_size if target.is_file() else None,
    }


def normalize_motion_identity(stem: str, domain: str | None = None) -> dict[str, Any]:
    raw = stem.strip()
    variant_no = 1
    context_marker: str | None = None

    numeric_suffix = re.search(r"\s*\((\d+)\)\s*$", raw)
    sign_suffix = re.search(r"[_\s]*\(SIGN[_\s-]*(\d+)\)\s*$", raw, re.IGNORECASE)
    context_suffix = re.search(r"[_\s]*\(([A-Za-z][A-Za-z0-9 _-]*)\)\s*$", raw)
    if sign_suffix:
        variant_no = max(1, int(sign_suffix.group(1)))
        raw = raw[: sign_suffix.start()]
    elif numeric_suffix:
        # Existing datasets use NAME, NAME (1), NAME (2) as variants 1, 2, 3.
        variant_no = int(numeric_suffix.group(1)) + 1
        raw = raw[: numeric_suffix.start()]
    elif context_suffix:
        marker = context_suffix.group(1).strip()
        if not re.fullmatch(r"SIGN[_\s-]*\d+", marker, re.IGNORECASE):
            context_marker = re.sub(r"[^A-Za-z0-9]+", "_", marker).strip("_").upper() or None
            raw = raw[: context_suffix.start()]

    gloss = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper() or "UNKNOWN"
    canonical_text = gloss.replace("_", " ").lower()
    normalized_domain = _nullable_upper(domain)
    parts = ["ISL"]
    if normalized_domain:
        parts.append(normalized_domain)
    parts.extend([gloss, f"{variant_no:02d}"])
    return {
        "motion_code": "_".join(parts),
        "gloss": gloss,
        "canonical_text": canonical_text,
        "variant_no": variant_no,
        "context_marker": context_marker,
    }


def load_catalog_row(catalog_path: str | Path | None, stem: str) -> dict[str, Any]:
    if catalog_path is None:
        return {}
    path = Path(catalog_path)
    if not path.is_file():
        return {}
    target = normalize_motion_identity(stem)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            row_stem = str(row.get("folder") or row.get("gloss") or "").strip()
            if not row_stem:
                continue
            candidate = normalize_motion_identity(row_stem)
            row_variant = _safe_int(row.get("variant_no")) or candidate["variant_no"]
            if candidate["gloss"] == target["gloss"] and row_variant == target["variant_no"]:
                normalized = {str(key): _empty_to_none(value) for key, value in row.items()}
                for key in ("provenance_verified", "two_handed"):
                    if key in normalized:
                        normalized[key] = _safe_bool(normalized[key])
                return normalized
    return {}


def parse_aliases(value: Any, canonical_text: str) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        values = [item.strip().lower() for item in re.split(r"[|;,]", value) if item.strip()]
    elif isinstance(value, list):
        values = [str(item).strip().lower() for item in value if str(item).strip()]
    if canonical_text:
        values.insert(0, canonical_text.lower())
    return list(dict.fromkeys(values))


def build_production_metadata(
    *,
    project_root: str | Path,
    source_video: str | Path,
    avatar_file: str | Path,
    glb_path: str | Path,
    pose_path: str | Path,
    motion_path: str | Path,
    qc_path: str | Path,
    glb_validation_path: str | Path,
    source_avatar_validation_path: str | Path,
    signer_review_path: str | Path,
    comparison_video_path: str | Path | None,
    video_info: Any,
    tracking_qc: dict[str, Any],
    glb_validation: dict[str, Any],
    source_avatar_validation: dict[str, Any],
    signer_review: dict[str, Any],
    avatar_profile: dict[str, Any],
    bone_map: dict[str, str],
    neutral_hand_validation: dict[str, Any],
    neutral_hand_pose_path: str | Path | None,
    started_at: str,
    completed_at: str,
    catalog_row: dict[str, Any] | None = None,
    logical_identity_stem: str | None = None,
    execution_context: dict[str, Any] | None = None,
    preparation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(source_video).resolve()
    avatar = Path(avatar_file).resolve()
    glb = Path(glb_path).resolve()
    pose = Path(pose_path).resolve()
    motion = Path(motion_path).resolve()
    comparison = Path(comparison_video_path).resolve() if comparison_video_path is not None else None
    catalog = catalog_row or {}
    execution = deepcopy(execution_context or {})
    # Evidence snapshots intentionally use role-based names such as
    # ``source_video.mp4``.  Never derive the database identity from those
    # storage names: callers must be able to preserve the logical input stem
    # across full conversion, approval-only, and failed-run recovery paths.
    identity = normalize_motion_identity(
        logical_identity_stem or source.stem,
        catalog.get("domain"),
    )
    canonical_text = str(catalog.get("canonical_text") or identity["canonical_text"])
    context = catalog.get("context") or identity["context_marker"]
    tags = [_nullable_lower(catalog.get("domain")), _nullable_lower(catalog.get("category")), _nullable_lower(identity["context_marker"])]
    tags = [value for value in dict.fromkeys(tags) if value]

    video_payload = video_info.to_json_dict() if hasattr(video_info, "to_json_dict") else dict(video_info)
    timestamps = video_payload.pop("timestamps_ms", []) or []
    fps = float(video_payload.get("fps") or 0.0)
    frame_count = int(video_payload.get("frame_count") or 0)
    video_payload["timestamp_start_ms"] = int(timestamps[0]) if timestamps else 0
    video_payload["timestamp_end_ms"] = int(timestamps[-1]) if timestamps else (int(round((frame_count - 1) / fps * 1000)) if fps and frame_count else 0)
    video_payload["timestamp_interval_ms"] = (1000.0 / fps) if fps else None
    video_payload["timestamps_explicit"] = False

    pipeline_version = _pipeline_version(root)
    mapping_values = set(bone_map)
    finger_names = [f"{side}{finger}{joint}" for side in ("Left", "Right") for finger in ("Thumb", "Index", "Middle", "Ring", "Little") for joint in (1, 2, 3)]
    hand_bones_present = all(name in mapping_values for name in ("LeftHand", "RightHand"))
    finger_bones_present = all(name in mapping_values for name in finger_names)
    technical_qc = str(tracking_qc.get("technical_qc") or signer_review.get("technical_qc") or "FAIL").upper()
    isl_verified = signer_review.get("isl_verified") is True
    signer_verdict = str(signer_review.get("signer_verdict") or "PENDING").upper()

    source_hash = sha256_file(source)
    glb_hash = sha256_file(glb)
    comparison_hash = sha256_file(comparison) if comparison is not None else None
    release_gate = evaluate_production_gate(
        technical_qc=technical_qc,
        glb_validation=glb_validation,
        source_avatar_validation=source_avatar_validation,
        signer_review=signer_review,
        catalog=catalog,
        source_video_sha256=source_hash,
        glb_sha256=glb_hash,
        comparison_video_sha256=comparison_hash,
        neutral_hand_validation=neutral_hand_validation,
    )

    assets = {
        "source_video": path_record(source, root),
        "final_glb": path_record(glb, root),
        "pose_npz": path_record(pose, root),
        "motion_npz": path_record(motion, root),
        "qc_report": path_record(qc_path, root),
        "glb_validation": path_record(glb_validation_path, root),
        "source_avatar_validation": path_record(source_avatar_validation_path, root),
        "signer_review": path_record(signer_review_path, root),
        "comparison_video": path_record(comparison, root),
        "neutral_hand_pose": path_record(neutral_hand_pose_path, root),
        "intermediate_assets": {
            "classification": "DEBUG_INTERMEDIATE",
            "production_glb_selected": False,
        },
    }
    motion_frames = frame_count
    motion_fps = fps
    if motion.is_file():
        try:
            import numpy as np
            with np.load(motion) as data:
                motion_frames = int(data["frame_count"])
                motion_fps = float(data["fps"])
        except Exception:
            pass
    duration = (motion_frames - 1) / motion_fps if motion_fps and motion_frames else 0.0

    return {
        "metadata_schema_version": METADATA_SCHEMA_VERSION,
        "asset_identity": {
            "video_id": source_hash,
            "original_filename": execution.get("original_filename") or source.name,
            "batch_id": execution.get("batch_id"),
            "run_id": execution.get("run_id"),
        },
        "motion_identity": {
            "motion_code": identity["motion_code"],
            "gloss": identity["gloss"],
            "canonical_text": canonical_text,
            "language_code": "ISL",
            "language_name": "Indian Sign Language",
            "domain": _nullable_upper(catalog.get("domain")),
            "level": _nullable_upper(catalog.get("level")) or "WORD",
            "category": _nullable_upper(catalog.get("category")),
            "variant_no": _safe_int(catalog.get("variant_no")) or identity["variant_no"],
        },
        "linguistic": {
            "meaning": catalog.get("meaning"),
            "context": context,
            "dominant_hand": catalog.get("dominant_hand"),
            "two_handed": _safe_bool(catalog.get("two_handed")),
            "non_manual_features_available": False,
            "linguistic_notes": catalog.get("linguistic_notes"),
        },
        "retrieval": {
            "aliases": parse_aliases(catalog.get("aliases"), canonical_text),
            "tags": tags,
            "exact_lookup_enabled": True,
            "fuzzy_lookup_enabled": True,
            "semantic_search_enabled": True,
        },
        "source": {
            "dataset": catalog.get("dataset"),
            "source_name": catalog.get("source_name"),
            "source_type": catalog.get("source_type"),
            "source_reference": catalog.get("source_reference"),
            "source_video_path": assets["source_video"],
            "license": catalog.get("license"),
            "provenance_verified": catalog.get("provenance_verified") is True,
        },
        "video": video_payload,
        "preparation": deepcopy(preparation or {}),
        "processing": {
            "pipeline_name": "video2glb",
            "pipeline_version": pipeline_version,
            "pipeline_stage": "video_to_pose_to_motion_to_glb",
            "pose_backend": "mediapipe_holistic",
            "coordinate_system_version": COORDINATE_SYSTEM_VERSION,
            "avatar_profile_version": avatar_profile.get("schema_version"),
            "bone_map_version": None,
            "neutral_hand_pose_version": neutral_hand_validation.get("schema_version"),
            "conversion_started_at": started_at,
            "conversion_completed_at": completed_at,
            "elapsed_seconds": _elapsed_seconds(started_at, completed_at),
            "run_id": execution.get("run_id"),
            "batch_id": execution.get("batch_id"),
            "attempt": execution.get("attempt", 1),
            "context_fingerprint": execution.get("context_fingerprint"),
            "input_hashes": execution.get("input_hashes", {}),
            "dependency_versions": runtime_versions(),
            "stage_cache": execution.get("stage_cache", []),
            "stages": execution.get("stages", []),
            "execution_record": execution.get("execution_record"),
        },
        "avatar": {
            "avatar_file": path_record(avatar, root),
            "armature_name": avatar_profile.get("armature_name"),
            "mesh_count": len(avatar_profile.get("mesh_names") or []),
            "bone_count": avatar_profile.get("bone_count"),
            "hand_bones_present": hand_bones_present,
            "finger_bones_present": finger_bones_present,
            "calibration_hashes": execution.get("calibration_hashes", {}),
            "neutral_hand_pose_applied": bool(neutral_hand_validation.get("applied", False)),
        },
        "animation": {
            "fps": motion_fps,
            "frame_count": motion_frames,
            "duration_seconds": duration,
            "animation_count": glb_validation.get("animation_count"),
            "animation_name": identity["gloss"],
            "has_body_motion": glb_validation.get("animation_count") == 1,
            "has_wrist_motion": _metric_pass(glb_validation, "retargeting"),
            "has_palm_motion": _metric_pass(glb_validation, "palm_orientation"),
            "has_finger_motion": _metric_pass(glb_validation, "finger_motion"),
            "root_translation_enabled": False,
        },
        "assets": assets,
        "file_integrity": {
            "glb_size_bytes": glb.stat().st_size if glb.is_file() else None,
            "glb_sha256": glb_hash,
            "source_video_size_bytes": source.stat().st_size if source.is_file() else None,
            "source_video_sha256": source_hash,
            "comparison_video_size_bytes": (
                comparison.stat().st_size if comparison is not None and comparison.is_file() else None
            ),
            "comparison_video_sha256": comparison_hash,
            "avatar_size_bytes": avatar.stat().st_size if avatar.is_file() else None,
            "avatar_sha256": sha256_file(avatar),
        },
        "technical_validation": {
            "technical_qc": technical_qc,
            "glb_validation": deepcopy(glb_validation),
            "source_avatar_validation": deepcopy(source_avatar_validation),
            "neutral_hand_validation": deepcopy(neutral_hand_validation),
            "tracking_summary": deepcopy(tracking_qc.get("metrics") or {}),
            "motion_quality": {
                name: deepcopy(glb_validation.get(name)) for name in (
                    "initial_hand_pose", "ending_hand_pose", "neutral_finger_shape", "finger_motion",
                    "finger_retargeting", "palm_orientation", "retargeting",
                    "motion_stability", "root_drift", "full_clip_collision",
                    "mesh_collision", "mesh_contact_correction", "finger_continuity_correction", "finger_direction_conditioning", "skin_weight_preparation",
                    "required_channel_coverage", "source_depth", "arm_pole_conditioning", "arm_temporal_conditioning",
                )
            },
        },
        "isl_validation": {
            "isl_verified": isl_verified,
            "signer_verdict": signer_verdict,
            "reviewer": signer_review.get("reviewer"),
            "reviewed_at": signer_review.get("reviewed_at"),
            "notes": signer_review.get("notes"),
        },
        "production": {
            "production_status": release_gate["status"],
            "production_eligible": release_gate["production_eligible"],
            "engineering_candidate": release_gate["engineering_candidate"],
            "release_gate": release_gate,
            "is_active": True,
            "database_indexed": False,
        },
        "database_ingestion": {
            "motion_table_source": "motion_identity",
            "aliases_table_source": "retrieval.aliases",
            "sources_table_source": "source",
            "assets_table_source": "assets",
            "technical_jsonb_source": "technical_validation",
            "embedding_stored_in_metadata": False,
        },
    }


def runtime_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for name in ("mediapipe", "numpy", "opencv-contrib-python", "PyYAML", "cryptography"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def write_review_delivery(
    *,
    project_root: Path,
    delivery_dir: Path,
    source_video: Path,
    original_glb: Path,
    run_id: str,
    batch_id: str,
    technical_qc: str,
    validation: dict,
    metadata: dict | None = None,
    evidence: dict[str, Path] | None = None,
    execution: dict | None = None,
    context_fingerprint: str | None = None,
    debug_paths: dict[str, Path] | None = None,
) -> dict:
    """Copy a verified conversion for human inspection, without approving it.

    The caller verifies the original release/failure manifest before calling.
    This boundary independently checks source/GLB binding and container shape.
    Quality failures remain failures; review availability is a separate state.
    """
    source_video, original_glb = Path(source_video), Path(original_glb)
    delivery_dir = Path(delivery_dir)
    if technical_qc not in {"PASS", "REVIEW", "FAIL"}:
        raise ValueError("Review delivery requires an actual PASS/REVIEW/FAIL technical result.")
    source_hash, glb_hash = sha256_file(source_video), sha256_file(original_glb)
    if not source_hash or not glb_hash:
        raise ValueError("Review delivery source video or GLB is missing.")
    if validation.get("validated_glb_sha256") != glb_hash:
        raise ValueError("Review delivery GLB does not match its validation SHA256.")
    _check_review_glb(original_glb)
    execution = deepcopy(execution or {})
    if execution.get("run_id") not in (None, run_id):
        raise ValueError("Review delivery execution run does not match conversion run.")
    assembled_at = utc_now_iso()
    if metadata is not None and technical_qc != "FAIL":
        generated = deepcopy(metadata)
        integrity = generated.get("file_integrity", {})
        identity = generated.get("asset_identity", {})
        saved_qc = generated.get("technical_validation", {}).get("technical_qc")
        if (integrity.get("glb_sha256") != glb_hash
                or integrity.get("source_video_sha256") != source_hash
                or identity.get("run_id") != run_id
                or identity.get("original_filename") != source_video.name
                or saved_qc != technical_qc):
            raise ValueError("Review delivery metadata source, GLB, run, filename or QC binding differs.")
        saved_validation = generated.get("technical_validation", {}).get("glb_validation", {})
        if saved_validation != validation:
            raise ValueError("Review delivery metadata validation differs from the bound report.")
    elif technical_qc == "FAIL":
        if (execution.get("run_id") != run_id or not execution.get("started_at")
                or not execution.get("completed_at")):
            raise ValueError("Review delivery requires the original completed execution record for a failed run.")
        evidence = {role: Path(path) for role, path in (evidence or {}).items()}
        required = {"source_video", "avatar", "pose", "motion", "neutral_hand_pose",
                    "avatar_profile", "bone_map", "video_preparation", "glb_validation"}
        missing = sorted(role for role in required if role not in evidence or not evidence[role].is_file())
        if missing:
            raise ValueError("Review delivery is missing immutable evidence: " + ", ".join(missing))
        if sha256_file(evidence["source_video"]) != source_hash:
            raise ValueError("Review delivery source does not match immutable conversion evidence.")
        def read_evidence(role: str) -> dict:
            value = json.loads(evidence[role].read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"Review delivery {role} evidence is not a JSON object.")
            return value
        if read_evidence("glb_validation") != validation:
            raise ValueError("Review delivery validation differs from immutable conversion evidence.")
        preparation = read_evidence("video_preparation")
        neutral = read_evidence("neutral_hand_pose")
        neutral_validation = deepcopy(neutral.get("validation") or {})
        neutral_validation["schema_version"] = neutral.get("schema_version")
        # Do not infer application, tracking results, or approvals from static calibration.
        neutral_validation["applied"] = None
        execution.setdefault("run_id", run_id)
        execution.setdefault("original_filename", source_video.name)
        execution.setdefault("context_fingerprint", context_fingerprint)
        source_avatar_path = evidence.get("source_avatar_validation")
        comparison_path = evidence.get("comparison_video")
        if source_avatar_path is not None and not source_avatar_path.is_file():
            raise ValueError("Review delivery source-avatar evidence is missing.")
        if comparison_path is not None and not comparison_path.is_file():
            raise ValueError("Review delivery comparison evidence is missing.")
        source_avatar_report = (
            read_evidence("source_avatar_validation") if source_avatar_path is not None else
            {"status": "NOT_RUN", "reasons": ["Not available in failure evidence."]}
        )
        generated = build_production_metadata(
            project_root=project_root, source_video=evidence["source_video"],
            avatar_file=evidence["avatar"], glb_path=original_glb,
            pose_path=evidence["pose"], motion_path=evidence["motion"],
            qc_path=None, glb_validation_path=evidence["glb_validation"],
            source_avatar_validation_path=source_avatar_path, signer_review_path=None,
            comparison_video_path=comparison_path,
            video_info=preparation.get("working") or preparation.get("source") or {},
            tracking_qc={"technical_qc": "FAIL", "metrics": {}},
            glb_validation=validation,
            source_avatar_validation=source_avatar_report,
            signer_review={"signer_verdict": "PENDING", "isl_verified": False},
            avatar_profile=read_evidence("avatar_profile"),
            bone_map=read_evidence("bone_map").get("map", {}),
            neutral_hand_validation=neutral_validation,
            neutral_hand_pose_path=evidence["neutral_hand_pose"],
            started_at=execution.get("started_at"), completed_at=execution.get("completed_at"),
            logical_identity_stem=source_video.stem, execution_context=execution,
            preparation=preparation,
        )
        generated["technical_validation"]["tracking_summary"] = {
            "status": "UNAVAILABLE", "reason": "Tracking report was not preserved in immutable failure evidence."
        }
        generated["avatar"]["neutral_hand_pose_applied"] = None
        # The builder runs today, but these fields describe the original conversion.
        generated["processing"]["pipeline_version"] = execution.get("pipeline_version")
        generated["processing"]["dependency_versions"] = deepcopy(execution.get("dependency_versions"))
        generated["processing"]["version_provenance"] = (
            "Original execution record; unavailable versions are null, not the delivery runtime."
        )
    else:
        raise ValueError("PASS/REVIEW delivery requires complete bound conversion metadata.")

    target_glb = delivery_dir / (source_video.stem + ".glb")
    target_metadata = delivery_dir / (source_video.stem + ".metadata.json")
    if target_glb.resolve() == original_glb.resolve():
        raise ValueError("Review delivery must not overwrite original conversion assets.")
    original_production = deepcopy(generated.get("production"))
    original_signer = deepcopy(generated.get("isl_validation"))
    original_retrieval = deepcopy(generated.get("retrieval"))
    generated.update(technical_qc=technical_qc, review_status="PENDING", review_available=True)
    generated["technical_validation"]["technical_qc"] = technical_qc
    generated["isl_validation"] = {
        "isl_verified": False, "signer_verdict": "PENDING", "reviewer": None,
        "reviewed_at": None, "notes": "This inspection copy awaits manual review.",
    }
    generated["production"] = {
        "production_status": "INSPECTION_ONLY", "production_eligible": False,
        "engineering_candidate": False, "is_active": False, "database_indexed": False,
        "release_gate": {"status": "INSPECTION_ONLY", "production_eligible": False,
                         "blockers": ["Inspection delivery is not a production release."]},
    }
    for key in ("exact_lookup_enabled", "fuzzy_lookup_enabled", "semantic_search_enabled"):
        generated.setdefault("retrieval", {})[key] = False
    generated["delivery"] = {
        "batch_id": batch_id, "conversion_run_id": run_id, "assembled_at": assembled_at,
        "title": source_video.stem, "original_filename": source_video.name,
        "classification": "FAILED_FOR_INSPECTION_ONLY" if technical_qc == "FAIL" else "REVIEW_REQUIRED",
        "original_glb": path_record(original_glb, project_root),
        "original_source": path_record(source_video, project_root),
        "original_production": original_production, "original_isl_validation": original_signer,
        "original_retrieval": original_retrieval,
        "immutable_evidence": {role: path_record(path, project_root) for role, path in (evidence or {}).items()},
        "assembly_runtime_versions": runtime_versions(),
        "assembly_pipeline_version": _pipeline_version(Path(project_root)),
        "context_fingerprint": context_fingerprint,
        "notes": "Review availability does not change technical QC or approve production use.",
    }
    existed = target_metadata.exists()
    if existed:
        existing = json.loads(target_metadata.read_text(encoding="utf-8"))
        # Preserve human edits and original assembly time on an identical retry.
        if (existing.get("file_integrity", {}).get("glb_sha256") != glb_hash
                or existing.get("file_integrity", {}).get("source_video_sha256") != source_hash
                or existing.get("delivery", {}).get("conversion_run_id") != run_id
                or existing.get("technical_qc") != technical_qc
                or existing.get("technical_validation", {}).get("technical_qc") != technical_qc
                or existing.get("technical_validation", {}).get("glb_validation") != validation
                or existing.get("asset_identity", {}).get("original_filename") != source_video.name
                or existing.get("asset_identity", {}).get("run_id") != run_id
                or existing.get("production", {}).get("production_eligible") is not False
                or existing.get("production", {}).get("is_active") is not False
                or not existing.get("review_status")
                or existing.get("review_available") is not True):
            raise ValueError("Review delivery metadata already exists for different conversion evidence.")
        if sha256_file(target_glb) != glb_hash:
            raise ValueError("Existing review delivery GLB is missing or changed.")
        generated = existing
    else:
        _copy_review_glb(original_glb, target_glb, glb_hash)
        generated["assets"]["final_glb"] = path_record(target_glb, project_root)
    previous_debug = deepcopy(generated.get("review_debug"))
    generated["review_debug"] = _write_review_debug(
        project_root=Path(project_root), delivery_dir=delivery_dir, stem=source_video.stem,
        debug_paths=debug_paths or {}, existing=previous_debug,
    )
    if not existed or previous_debug != generated["review_debug"]:
        atomic_write_json(target_metadata, generated)
    return {
        "glb_path": target_glb.resolve().as_posix(),
        "metadata_path": target_metadata.resolve().as_posix(),
        "glb_sha256": glb_hash, "metadata_sha256": sha256_file(target_metadata),
        "review_status": generated["review_status"], "review_available": True,
        "technical_qc": technical_qc,
        "review_debug": deepcopy(generated["review_debug"]),
    }


def _write_review_debug(
    *, project_root: Path, delivery_dir: Path, stem: str,
    debug_paths: dict[str, Path], existing: dict | None,
) -> dict:
    """Add hash-checked debug copies without replacing prior evidence or review notes."""
    suffixes = {
        "comparison_video": "_source_avatar_comparison.mp4",
        "avatar_preview": "_avatar_preview.mp4",
        "source_avatar_validation": ".source_avatar_validation.json",
        "pose_overlay": "_pose_overlay.mp4",
    }
    unknown = set(debug_paths) - set(suffixes)
    if unknown:
        raise ValueError("Unknown review debug role: " + ", ".join(sorted(unknown)))
    assets = deepcopy((existing or {}).get("assets", {}))
    originals = deepcopy((existing or {}).get("original_assets", {}))
    pending = []
    # Check every old and proposed file before copying anything.
    for role, record in assets.items():
        if role not in suffixes or not isinstance(record, dict):
            raise ValueError("Existing review debug metadata contains an unknown asset.")
        target = delivery_dir / "debug" / (stem + suffixes[role])
        if (record.get("absolute_path") != target.resolve().as_posix()
                or not record.get("sha256") or sha256_file(target) != record["sha256"]):
            raise ValueError(f"Existing review debug {role} is missing or changed.")
    for role, path in debug_paths.items():
        source = Path(path)
        source_hash = sha256_file(source)
        if not source_hash:
            raise ValueError(f"Review debug {role} is missing.")
        target = delivery_dir / "debug" / (stem + suffixes[role])
        if target.exists() and sha256_file(target) != source_hash:
            raise ValueError(f"Review debug {role} already exists with different bytes.")
        if role in assets and assets[role]["sha256"] != source_hash:
            raise ValueError(f"Review debug {role} differs from recorded evidence.")
        if role not in assets:
            pending.append((role, source, target, source_hash))
    for role, source, target, source_hash in pending:
        _copy_review_glb(source, target, source_hash)
        assets[role] = path_record(target, project_root)
        originals[role] = path_record(source, project_root)
    return {
        "available": bool(assets), "comparison_available": "comparison_video" in assets,
        "assets": assets, "original_assets": originals,
    }


def _copy_review_glb(source: Path, target: Path, expected_hash: str) -> None:
    if target.exists():
        if sha256_file(target) != expected_hash:
            raise ValueError("Review delivery GLB already exists with different bytes.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, out)
            out.flush()
            os.fsync(out.fileno())
        if sha256_file(temporary) != expected_hash:
            raise ValueError("Review delivery GLB changed while being copied.")
        if target.exists() and sha256_file(target) != expected_hash:
            raise ValueError("Review delivery GLB was replaced by different bytes during copying.")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_review_glb(path: Path) -> None:
    """Bounded container/animation/skin sanity check, not a replacement for QC."""
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError("Review GLB header is truncated.")
        magic, version, length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or length != path.stat().st_size:
            raise ValueError("Review GLB is not an intact GLB v2 container.")
        document = None
        binary_length = 0
        while stream.tell() < length:
            chunk_header = stream.read(8)
            if len(chunk_header) != 8:
                raise ValueError("Review GLB chunk header is truncated.")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            if chunk_length % 4 or stream.tell() + chunk_length > length:
                raise ValueError("Review GLB chunk length is invalid.")
            if document is None:
                if chunk_type != 0x4E4F534A or chunk_length > 64 * 1024 * 1024:
                    raise ValueError("Review GLB must begin with a bounded JSON chunk.")
                document = json.loads(stream.read(chunk_length).decode("utf-8"))
            else:
                if chunk_type == 0x4E4F534A:
                    raise ValueError("Review GLB contains duplicate JSON chunks.")
                if chunk_type == 0x004E4942 and binary_length:
                    raise ValueError("Review GLB contains duplicate binary chunks.")
                if chunk_type == 0x004E4942:
                    binary_length += chunk_length
                stream.seek(chunk_length, os.SEEK_CUR)
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise ValueError("Review GLB does not declare glTF 2.0.")
    animations, skins, nodes = document.get("animations", []), document.get("skins", []), document.get("nodes", [])
    if not binary_length or not animations or not skins or not nodes:
        raise ValueError("Review GLB has no embedded animated, skinned avatar.")
    if not any(isinstance(node.get("skin"), int) and 0 <= node["skin"] < len(skins)
               and isinstance(node.get("mesh"), int) and 0 <= node["mesh"] < len(document.get("meshes", []))
               for node in nodes):
        raise ValueError("Review GLB has no mesh bound to a skin.")
    if not all(skin.get("joints") and all(isinstance(joint, int) and 0 <= joint < len(nodes)
                                        for joint in skin["joints"]) for skin in skins):
        raise ValueError("Review GLB has invalid skin joints.")
    for animation in animations:
        samplers = animation.get("samplers", [])
        if not animation.get("channels") or not samplers:
            raise ValueError("Review GLB has an empty animation.")
        for channel in animation["channels"]:
            sampler, node = channel.get("sampler"), channel.get("target", {}).get("node")
            if (not isinstance(sampler, int) or not 0 <= sampler < len(samplers)
                    or not isinstance(node, int) or not 0 <= node < len(nodes)):
                raise ValueError("Review GLB has an invalid animation channel.")


def _elapsed_seconds(start: str, end: str) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(end.replace("Z", "+00:00"))
                         - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    except (ValueError, TypeError):
        return None


def _metric_pass(report: dict[str, Any], name: str) -> bool:
    metric = report.get(name)
    return isinstance(metric, dict) and metric.get("status") == "PASS"


def merge_metadata(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Preserve unknown keys and human values while refreshing automatic fields."""
    result = deepcopy(existing)
    for key, value in generated.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_metadata(result[key], value)
        elif value is not None or key not in result:
            result[key] = deepcopy(value)
    return result


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _pipeline_version(root: Path) -> str | None:
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as stream:
            return str(tomllib.load(stream).get("project", {}).get("version") or "") or None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _nullable_upper(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or None


def _nullable_lower(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text.lower() or None


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    return None
