import json
import struct
from pathlib import Path

import pytest

from convert import write_reports
from src.metadata.production_metadata import (
    atomic_write_json,
    build_production_metadata,
    load_catalog_row,
    merge_metadata,
    normalize_motion_identity,
    sha256_file,
    write_review_delivery,
)


def test_motion_identity_normalizes_dataset_variants_without_inventing_domain():
    assert normalize_motion_identity("DELAY")["motion_code"] == "ISL_DELAY_01"
    assert normalize_motion_identity("DELAY (1)")["motion_code"] == "ISL_DELAY_02"
    assert normalize_motion_identity("BEFORE_(SIGN_2)")["motion_code"] == "ISL_BEFORE_02"
    train = normalize_motion_identity("ARRIVE (TRAIN)")
    assert train["motion_code"] == "ISL_ARRIVE_01"
    assert train["context_marker"] == "TRAIN"


def test_merge_preserves_human_values_when_generated_value_is_unknown():
    existing = {"linguistic": {"meaning": "human supplied", "custom": 7}}
    generated = {"linguistic": {"meaning": None, "context": None}}
    assert merge_metadata(existing, generated) == {
        "linguistic": {"meaning": "human supplied", "custom": 7, "context": None}
    }


def test_staged_metadata_merge_refreshes_legacy_and_schema_v3_statuses():
    existing = {
        "technical_qc": "REVIEW",
        "glb_validation": {"status": "REVIEW", "old": True},
        "custom_human_field": "keep me",
    }
    fresh_summary = {
        "technical_qc": "PASS",
        "glb_validation": {"status": "PASS", "file_size": 123},
    }
    schema_v3 = {
        "metadata_schema_version": "3.0",
        "technical_validation": {"technical_qc": "PASS"},
    }

    merged = merge_metadata(merge_metadata(existing, fresh_summary), schema_v3)

    assert merged["technical_qc"] == "PASS"
    assert merged["glb_validation"]["status"] == "PASS"
    assert merged["technical_validation"]["technical_qc"] == "PASS"
    assert merged["custom_human_field"] == "keep me"


def test_atomic_json_write_leaves_no_temporary_file(tmp_path: Path):
    target = tmp_path / "record.json"
    atomic_write_json(target, {"production_eligible": False})
    assert target.read_text(encoding="utf-8").endswith("\n")
    assert list(tmp_path.glob("*.tmp")) == []


def test_catalog_loader_normalizes_declared_boolean_columns(tmp_path: Path):
    catalog_path = tmp_path / "catalog.csv"
    catalog_path.write_text(
        "gloss,provenance_verified,two_handed\nCHANGE,true,false\n",
        encoding="utf-8",
    )

    row = load_catalog_row(catalog_path, "Change")

    assert row["provenance_verified"] is True
    assert row["two_handed"] is False


def test_write_reports_compacts_constant_rate_timestamps(tmp_path: Path):
    class VideoInfo:
        def to_json_dict(self):
            return {
                "fps": 25.0,
                "frame_count": 3,
                "timestamps_ms": [0, 40, 80],
            }

    class QCResult:
        technical_qc = "PASS"

        def to_json_dict(self):
            return {"technical_qc": self.technical_qc, "metrics": {}}

    metadata_path = tmp_path / "sample.metadata.json"
    write_reports(metadata_path, tmp_path / "sample.qc.json", VideoInfo(), QCResult())
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert "timestamps_ms" not in payload["video"]
    assert payload["video"]["timestamp_start_ms"] == 0
    assert payload["video"]["timestamp_end_ms"] == 80
    assert payload["video"]["timestamp_interval_ms"] == 40.0


def test_build_metadata_derives_approved_production_state_from_release_gate(tmp_path: Path):
    source = _write_asset(tmp_path / "Change.mp4", b"source")
    avatar = _write_asset(tmp_path / "character.fbx", b"avatar")
    glb = _write_asset(tmp_path / "Change.glb", b"glb")
    comparison = _write_asset(tmp_path / "comparison.mp4", b"comparison")
    source_hash = sha256_file(source)
    glb_hash = sha256_file(glb)
    comparison_hash = sha256_file(comparison)

    glb_validation = {
        "status": "PASS",
        "validation_run_id": "glb-validation-run-001",
        "validated_glb_sha256": glb_hash,
        "khronos_validation": {
            "status": "PASS",
            "validated_glb_sha256": glb_hash,
            "validator_version": "2.0.0-dev.3.10",
            "counts": {"numErrors": 0},
        },
        "animation_count": 1,
        "full_clip_collision": {
            "status": "PASS",
            "evaluated_every_input_frame": True,
            "frames": {"total": 3, "evaluated": 3, "evaluated_coverage": 1.0},
        },
    }
    source_avatar_validation = {
        "status": "PASS",
        "validation_run_id": "source-avatar-validation-run-001",
        "source_video_sha256": source_hash,
        "comparison_video_sha256": comparison_hash,
        "source_frame_count": 3,
    }
    signer_review = {
        "signer_verdict": "PASS",
        "isl_verified": True,
        "reviewer": "Qualified reviewer",
        "reviewed_at": "2026-09-03T09:00:00Z",
        "hash_binding": {
            "source_video_sha256": source_hash,
            "glb_sha256": glb_hash,
            "comparison_video_sha256": comparison_hash,
        },
        "collision_review": {"status": "PASS"},
        "signature_verification": {"status": "PASS", "verified": True},
    }
    catalog = {
        "meaning": "change",
        "license": "licensed",
        "source_reference": "catalog://change/1",
        "provenance_verified": True,
    }

    metadata = _build_metadata(
        tmp_path,
        source=source,
        avatar=avatar,
        glb=glb,
        comparison=comparison,
        glb_validation=glb_validation,
        source_avatar_validation=source_avatar_validation,
        signer_review=signer_review,
        catalog=catalog,
    )

    production = metadata["production"]
    assert production["production_status"] == "APPROVED"
    assert production["production_eligible"] is True
    assert production["engineering_candidate"] is True
    assert production["release_gate"]["production_eligible"] is True
    assert production["release_gate"]["blockers"] == []
    assert metadata["file_integrity"]["comparison_video_sha256"] == comparison_hash

    calibration_review = _build_metadata(
        tmp_path,
        source=source,
        avatar=avatar,
        glb=glb,
        comparison=comparison,
        glb_validation=glb_validation,
        source_avatar_validation=source_avatar_validation,
        signer_review=signer_review,
        catalog=catalog,
        neutral_hand_validation={"status": "REVIEW"},
    )
    calibration_gate = calibration_review["production"]["release_gate"]
    assert calibration_gate["checks"]["neutral_hand_calibration_pass"] is False
    assert calibration_review["production"]["engineering_candidate"] is False


def test_build_metadata_cannot_be_approved_by_truthy_boolean_strings(tmp_path: Path):
    source = _write_asset(tmp_path / "Change.mp4", b"source")
    avatar = _write_asset(tmp_path / "character.fbx", b"avatar")
    glb = _write_asset(tmp_path / "Change.glb", b"glb")
    comparison = _write_asset(tmp_path / "comparison.mp4", b"comparison")

    glb_validation = {
        "status": "PASS",
        "full_clip_collision": {
            "status": "PASS",
            "evaluated_every_input_frame": True,
            "frames": {"total": 3, "evaluated": 3, "evaluated_coverage": 1.0},
        },
    }
    signer_review = {
        "signer_verdict": "PASS",
        "isl_verified": "true",
        "reviewer": "Reviewer",
        "reviewed_at": "2026-09-03T09:00:00Z",
        "hash_binding": {
            "source_video_sha256": sha256_file(source),
            "glb_sha256": sha256_file(glb),
            "comparison_video_sha256": sha256_file(comparison),
        },
        "collision_review": {"status": "PASS"},
    }
    catalog = {
        "meaning": "change",
        "license": "licensed",
        "source_reference": "catalog://change/1",
        "provenance_verified": "true",
    }

    metadata = _build_metadata(
        tmp_path,
        source=source,
        avatar=avatar,
        glb=glb,
        comparison=comparison,
        glb_validation=glb_validation,
        source_avatar_validation={"status": "PASS", "source_frame_count": 3},
        signer_review=signer_review,
        catalog=catalog,
    )

    release_gate = metadata["production"]["release_gate"]
    assert metadata["isl_validation"]["isl_verified"] is False
    assert metadata["source"]["provenance_verified"] is False
    assert release_gate["checks"]["isl_verified"] is False
    assert release_gate["checks"]["provenance_verified"] is False
    assert metadata["production"]["production_eligible"] is False


def test_logical_identity_survives_role_named_source_snapshot(tmp_path: Path):
    source = _write_asset(tmp_path / "source_video.mp4", b"source")
    avatar = _write_asset(tmp_path / "character.fbx", b"avatar")
    glb = _write_asset(tmp_path / "Change.glb", b"glb")
    comparison = _write_asset(tmp_path / "comparison.mp4", b"comparison")

    metadata = _build_metadata(
        tmp_path,
        source=source,
        avatar=avatar,
        glb=glb,
        comparison=comparison,
        glb_validation={},
        source_avatar_validation={},
        signer_review={},
        catalog={},
        logical_identity_stem="Change",
    )

    assert metadata["motion_identity"]["gloss"] == "CHANGE"
    assert metadata["motion_identity"]["motion_code"] == "ISL_CHANGE_01"
    assert metadata["animation"]["animation_name"] == "CHANGE"


def test_metadata_records_real_execution_timing_and_unknown_review_fields(tmp_path: Path):
    source = _write_asset(tmp_path / "Arrive.mp4", b"source")
    avatar = _write_asset(tmp_path / "character.fbx", b"avatar")
    glb = _write_asset(tmp_path / "Arrive.glb", b"glb")
    comparison = _write_asset(tmp_path / "comparison.mp4", b"comparison")
    execution = {
        "run_id": "a" * 32, "batch_id": "b" * 32, "attempt": 2,
        "original_filename": "Arrive.mp4", "context_fingerprint": "c" * 64,
        "stage_cache": [{"stage": "tracking", "reused": True}],
        "stages": [{"name": "Tracking", "status": "COMPLETED", "elapsed_seconds": 1.2}],
        "calibration_hashes": {"profile": "d" * 64},
    }
    preparation = {"normalized": True, "source": {"frame_count": 4}, "working": {"frame_count": 5}}
    metadata = _build_metadata(tmp_path, source=source, avatar=avatar, glb=glb,
        comparison=comparison, glb_validation={"root_drift": {"status": "PASS"},
            "neutral_finger_shape": {"status": "PASS", "evaluated_frames": 3}},
        source_avatar_validation={}, signer_review={}, catalog={},
        execution_context=execution, preparation=preparation)
    assert metadata["metadata_schema_version"] == "3.1"
    assert metadata["asset_identity"]["video_id"] == sha256_file(source)
    assert metadata["asset_identity"]["batch_id"] == "b" * 32
    assert metadata["processing"]["elapsed_seconds"] == 60
    assert metadata["processing"]["attempt"] == 2
    assert metadata["processing"]["stage_cache"][0]["reused"] is True
    assert metadata["preparation"]["source"]["frame_count"] == 4
    assert metadata["preparation"]["working"]["frame_count"] == 5
    assert metadata["technical_validation"]["motion_quality"]["root_drift"]["status"] == "PASS"
    assert metadata["technical_validation"]["motion_quality"]["neutral_finger_shape"] == {
        "status": "PASS", "evaluated_frames": 3,
    }
    assert metadata["source"]["license"] is None
    assert metadata["linguistic"]["meaning"] is None
    assert metadata["isl_validation"]["reviewer"] is None
    assert metadata["animation"]["has_finger_motion"] is False


def _write_asset(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _review_fixture(tmp_path: Path, *, failed: bool = False) -> dict:
    source = _write_asset(tmp_path / "Coach_(Train).mp4", b"original video")
    document = {
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": 4}],
        "nodes": [{"mesh": 0, "skin": 0}, {}], "meshes": [{"primitives": []}],
        "skins": [{"joints": [1]}], "animations": [{
            "samplers": [{"input": 0, "output": 1}],
            "channels": [{"sampler": 0, "target": {"node": 1, "path": "rotation"}}],
        }],
    }
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    glb_bytes = (struct.pack("<4sII", b"glTF", 2, 32 + len(encoded))
                 + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
                 + struct.pack("<II", 4, 0x004E4942) + b"\x00" * 4)
    glb = _write_asset(tmp_path / "original.glb", glb_bytes)
    validation = {"status": "FAIL" if failed else "REVIEW", "animation_count": 1,
                  "validated_glb_sha256": sha256_file(glb), "reasons": ["Collision"] if failed else []}
    execution = {"run_id": "a" * 32, "batch_id": "b" * 32,
                 "started_at": "2026-09-03T08:00:00Z", "completed_at": "2026-09-03T08:01:00Z"}
    evidence_dir = tmp_path / "immutable"
    evidence_dir.mkdir()
    evidence = {
        "source_video": _write_asset(evidence_dir / "source_video.mp4", source.read_bytes()),
        "avatar": _write_asset(evidence_dir / "avatar.fbx", b"avatar"),
        "pose": _write_asset(evidence_dir / "pose.npz", b"pose"),
        "motion": _write_asset(evidence_dir / "motion.npz", b"motion"),
    }
    for role, value in {
        "neutral_hand_pose": {"schema_version": "1.3", "validation": {"status": "PASS"}},
        "avatar_profile": {"armature_name": "Avatar", "mesh_names": ["Body"]},
        "bone_map": {"map": {"LeftHand": "Hand_L", "RightHand": "Hand_R"}},
        "video_preparation": {"source": {"fps": 29.97, "frame_count": 4},
                              "working": {"fps": 25.0, "frame_count": 3}},
        "glb_validation": validation,
    }.items():
        evidence[role] = evidence_dir / (role + ".json")
        atomic_write_json(evidence[role], value)
    metadata = _build_metadata(
        tmp_path, source=source, avatar=evidence["avatar"], glb=glb,
        comparison=tmp_path / "absent.mp4", glb_validation=validation,
        source_avatar_validation={}, signer_review={}, catalog={},
        execution_context={**execution, "original_filename": source.name},
    )
    metadata["technical_validation"]["technical_qc"] = validation["status"]
    return {
        "project_root": tmp_path, "delivery_dir": tmp_path / "delivery" / "COACH_(TRAIN)",
        "source_video": source, "original_glb": glb, "run_id": "a" * 32,
        "batch_id": "c" * 32, "technical_qc": validation["status"],
        "validation": validation, "metadata": None if failed else metadata,
        "evidence": evidence if failed else None, "execution": execution,
        "context_fingerprint": "d" * 64,
    }


@pytest.mark.parametrize("failed", [False, True])
def test_review_delivery_preserves_bytes_identity_and_actual_qc(tmp_path: Path, failed: bool):
    args = _review_fixture(tmp_path, failed=failed)
    result = write_review_delivery(**args)
    payload = json.loads(Path(result["metadata_path"]).read_text())
    assert Path(result["glb_path"]).name == "Coach_(Train).glb"
    assert Path(result["glb_path"]).read_bytes() == args["original_glb"].read_bytes()
    assert payload["asset_identity"]["original_filename"] == "Coach_(Train).mp4"
    assert payload["motion_identity"]["gloss"] == "COACH"
    assert payload["delivery"]["title"] == "Coach_(Train)"
    assert payload["asset_identity"]["batch_id"] == "b" * 32
    assert payload["delivery"]["batch_id"] == "c" * 32
    assert result["technical_qc"] == ("FAIL" if failed else "REVIEW")
    assert payload["technical_validation"]["technical_qc"] == result["technical_qc"]
    assert result["review_status"] == "PENDING"
    assert payload["review_available"] is True
    assert payload["production"]["production_eligible"] is False
    assert payload["production"]["is_active"] is False
    assert payload["retrieval"]["semantic_search_enabled"] is False
    assert payload["isl_validation"]["signer_verdict"] == "PENDING"
    assert payload["assets"]["final_glb"]["sha256"] == result["glb_sha256"]
    if failed:
        assert payload["assets"]["qc_report"] is None
        assert payload["assets"]["source_avatar_validation"] is None
        assert payload["assets"]["signer_review"] is None
        assert payload["technical_validation"]["tracking_summary"]["status"] == "UNAVAILABLE"
        assert payload["processing"]["dependency_versions"] is None
        assert payload["processing"]["pipeline_version"] is None
        assert payload["delivery"]["assembly_runtime_versions"]["python"]
        assert payload["delivery"]["classification"] == "FAILED_FOR_INSPECTION_ONLY"
        assert payload["source"]["source_video_path"]["absolute_path"].endswith("immutable/source_video.mp4")
        assert payload["video"]["fps"] == 25.0
        assert payload["video"]["frame_count"] == 3


def test_review_delivery_preserves_original_approval_only_as_provenance(tmp_path: Path):
    args = _review_fixture(tmp_path)
    args["metadata"]["production"].update(production_status="APPROVED", production_eligible=True)
    args["metadata"]["isl_validation"].update(isl_verified=True, signer_verdict="PASS")
    result = write_review_delivery(**args)
    payload = json.loads(Path(result["metadata_path"]).read_text())
    assert payload["delivery"]["original_production"]["production_eligible"] is True
    assert payload["delivery"]["original_isl_validation"]["isl_verified"] is True
    assert payload["production"]["production_eligible"] is False
    assert args["metadata"]["production"]["production_eligible"] is True


def test_review_delivery_retry_is_idempotent(tmp_path: Path):
    args = _review_fixture(tmp_path, failed=True)
    first = write_review_delivery(**args)
    before = Path(first["metadata_path"]).read_bytes()
    second = write_review_delivery(**args)
    assert first == second
    assert Path(second["metadata_path"]).read_bytes() == before
    assert list(args["delivery_dir"].glob("*.tmp")) == []


def test_review_delivery_new_batch_preserves_original_assembly_and_manual_review(tmp_path: Path):
    args = _review_fixture(tmp_path, failed=True)
    first = write_review_delivery(**args)
    path = Path(first["metadata_path"])
    payload = json.loads(path.read_text())
    payload["review_status"] = "CHANGES_REQUESTED"
    payload["review_notes"] = "Please correct wrist rotation."
    atomic_write_json(path, payload)
    before = path.read_bytes()
    args["batch_id"] = "e" * 32
    second = write_review_delivery(**args)
    assert second["review_status"] == "CHANGES_REQUESTED"
    assert path.read_bytes() == before
    assert json.loads(path.read_text())["delivery"]["batch_id"] == "c" * 32


def _review_debug_fixture(args: dict) -> dict[str, Path]:
    root = args["project_root"] / "verified_debug"
    root.mkdir()
    comparison = _write_asset(root / "comparison.mp4", b"side by side")
    avatar = _write_asset(root / "avatar.mp4", b"avatar rendering")
    report = root / "source_avatar_validation.json"
    atomic_write_json(report, {
        "status": "REVIEW", "source_video_sha256": sha256_file(args["source_video"]),
        "comparison_video_sha256": sha256_file(comparison), "source_frame_count": 3,
    })
    return {"comparison_video": comparison, "avatar_preview": avatar,
            "source_avatar_validation": report}


@pytest.mark.parametrize("failed", [False, True])
def test_review_delivery_copies_available_debug_with_logical_names_and_hashes(tmp_path: Path, failed: bool):
    args = _review_fixture(tmp_path, failed=failed)
    args["debug_paths"] = _review_debug_fixture(args)
    result = write_review_delivery(**args)
    debug = result["review_debug"]
    assert debug["available"] is True
    assert debug["comparison_available"] is True
    assert len(debug["assets"]) == 3
    comparison = debug["assets"]["comparison_video"]
    assert Path(comparison["absolute_path"]).name == "Coach_(Train)_source_avatar_comparison.mp4"
    assert Path(comparison["absolute_path"]).parent.name == "debug"
    assert comparison["sha256"] == sha256_file(args["debug_paths"]["comparison_video"])
    payload = json.loads(Path(result["metadata_path"]).read_text())
    assert payload["review_debug"] == debug
    assert payload["technical_qc"] == args["technical_qc"]
    first_bytes = Path(result["metadata_path"]).read_bytes()
    assert write_review_delivery(**args) == result
    assert Path(result["metadata_path"]).read_bytes() == first_bytes


def test_review_delivery_debug_upgrade_preserves_human_review_and_original_qc(tmp_path: Path):
    args = _review_fixture(tmp_path, failed=True)
    first = write_review_delivery(**args)
    path = Path(first["metadata_path"])
    payload = json.loads(path.read_text())
    del payload["review_debug"]  # Simulate metadata produced before debug delivery support.
    payload["review_status"] = "CHANGES_REQUESTED"
    payload["review_notes"] = "Keep this human note."
    atomic_write_json(path, payload)
    old_technical = payload["technical_validation"]
    old_processing = payload["processing"]
    old_delivery = payload["delivery"]
    args["debug_paths"] = _review_debug_fixture(args)
    args["batch_id"] = "e" * 32
    upgraded = write_review_delivery(**args)
    after = json.loads(path.read_text())
    assert upgraded["review_status"] == "CHANGES_REQUESTED"
    assert after["review_notes"] == "Keep this human note."
    assert after["technical_validation"] == old_technical
    assert after["processing"] == old_processing
    assert after["delivery"] == old_delivery
    assert after["review_debug"]["comparison_available"] is True
    args["debug_paths"] = None
    before = path.read_bytes()
    write_review_delivery(**args)
    assert path.read_bytes() == before


def test_review_delivery_missing_debug_is_not_marked_available(tmp_path: Path):
    args = _review_fixture(tmp_path)
    result = write_review_delivery(**args)
    assert result["review_debug"]["available"] is False
    assert result["review_debug"]["comparison_available"] is False
    assert result["review_debug"]["assets"] == {}
    args["debug_paths"] = {"comparison_video": tmp_path / "missing.mp4"}
    with pytest.raises(ValueError, match="debug comparison_video is missing"):
        write_review_delivery(**args)
    assert json.loads(Path(result["metadata_path"]).read_text())["review_debug"]["available"] is False


@pytest.mark.parametrize("changed", ["copy", "source"])
def test_review_delivery_debug_refuses_changed_bytes_without_overwriting(tmp_path: Path, changed: str):
    args = _review_fixture(tmp_path)
    args["debug_paths"] = _review_debug_fixture(args)
    result = write_review_delivery(**args)
    path = Path(result["metadata_path"])
    metadata_before = path.read_bytes()
    target = Path(result["review_debug"]["assets"]["comparison_video"]["absolute_path"])
    if changed == "copy":
        target.write_bytes(b"changed copy")
    else:
        args["debug_paths"]["comparison_video"].write_bytes(b"changed source")
    copy_before = target.read_bytes()
    with pytest.raises(ValueError, match="debug"):
        write_review_delivery(**args)
    assert target.read_bytes() == copy_before
    assert path.read_bytes() == metadata_before


def test_failed_review_uses_real_immutable_source_avatar_evidence_when_available(tmp_path: Path):
    args = _review_fixture(tmp_path, failed=True)
    args["debug_paths"] = _review_debug_fixture(args)
    args["evidence"].update(args["debug_paths"])
    result = write_review_delivery(**args)
    payload = json.loads(Path(result["metadata_path"]).read_text())
    expected = json.loads(args["debug_paths"]["source_avatar_validation"].read_text())
    assert payload["technical_validation"]["source_avatar_validation"] == expected
    assert payload["assets"]["comparison_video"]["sha256"] == expected["comparison_video_sha256"]
    assert payload["assets"]["source_avatar_validation"]["exists"] is True
    assert payload["file_integrity"]["comparison_video_sha256"] == expected["comparison_video_sha256"]
    assert payload["technical_qc"] == "FAIL"


@pytest.mark.parametrize("changed", ["nested_qc", "nested_validation", "production"])
def test_review_delivery_does_not_accept_tampered_qc_or_approval(tmp_path: Path, changed: str):
    args = _review_fixture(tmp_path, failed=True)
    result = write_review_delivery(**args)
    path = Path(result["metadata_path"])
    payload = json.loads(path.read_text())
    if changed == "nested_qc":
        payload["technical_validation"]["technical_qc"] = "PASS"
    elif changed == "nested_validation":
        payload["technical_validation"]["glb_validation"]["reasons"] = []
    else:
        payload["production"]["production_eligible"] = True
    atomic_write_json(path, payload)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_review_delivery(**args)
    assert path.read_bytes() == before


def test_failed_review_delivery_requires_completed_execution_evidence(tmp_path: Path):
    args = _review_fixture(tmp_path, failed=True)
    args["execution"] = None
    with pytest.raises(ValueError, match="completed execution"):
        write_review_delivery(**args)


@pytest.mark.parametrize("changed", ["source", "glb", "run", "qc", "validation"])
def test_review_delivery_rejects_unbound_metadata(tmp_path: Path, changed: str):
    args = _review_fixture(tmp_path)
    if changed == "source":
        args["source_video"].write_bytes(b"changed")
    elif changed == "glb":
        args["validation"]["validated_glb_sha256"] = "0" * 64
    elif changed == "run":
        args["metadata"]["asset_identity"]["run_id"] = "wrong"
    elif changed == "qc":
        args["metadata"]["technical_validation"]["technical_qc"] = "PASS"
    else:
        args["validation"]["reasons"].append("changed")
    with pytest.raises(ValueError):
        write_review_delivery(**args)
    assert not args["delivery_dir"].exists()


@pytest.mark.parametrize("changed", ["missing", "source", "validation"])
def test_review_delivery_rejects_incomplete_or_mismatched_failure_evidence(tmp_path: Path, changed: str):
    args = _review_fixture(tmp_path, failed=True)
    if changed == "missing":
        del args["evidence"]["pose"]
    elif changed == "source":
        args["evidence"]["source_video"].write_bytes(b"changed")
    else:
        atomic_write_json(args["evidence"]["glb_validation"], {"status": "PASS"})
    with pytest.raises(ValueError):
        write_review_delivery(**args)
    assert not args["delivery_dir"].exists()


def test_review_delivery_rejects_invalid_container_even_with_matching_hash(tmp_path: Path):
    args = _review_fixture(tmp_path)
    args["original_glb"].write_bytes(b"not an animated GLB")
    args["validation"]["validated_glb_sha256"] = sha256_file(args["original_glb"])
    with pytest.raises(ValueError, match="GLB"):
        write_review_delivery(**args)


@pytest.mark.parametrize("missing", ["animations", "skins"])
def test_review_delivery_rejects_glb_without_skeletal_animation(tmp_path: Path, missing: str):
    args = _review_fixture(tmp_path)
    original = args["original_glb"].read_bytes()
    json_length = struct.unpack("<I", original[12:16])[0]
    document = json.loads(original[20:20 + json_length])
    del document[missing]
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    args["original_glb"].write_bytes(
        struct.pack("<4sII", b"glTF", 2, 32 + len(encoded))
        + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
        + struct.pack("<II", 4, 0x004E4942) + b"\x00" * 4
    )
    args["validation"]["validated_glb_sha256"] = sha256_file(args["original_glb"])
    with pytest.raises(ValueError, match="animated, skinned"):
        write_review_delivery(**args)


@pytest.mark.parametrize("target", ["glb", "metadata"])
def test_review_delivery_does_not_overwrite_other_evidence(tmp_path: Path, target: str):
    args = _review_fixture(tmp_path)
    result = write_review_delivery(**args)
    if target == "glb":
        path = Path(result["glb_path"])
        path.write_bytes(b"different output")
    else:
        path = Path(result["metadata_path"])
        value = json.loads(path.read_text())
        value["delivery"]["conversion_run_id"] = "another run"
        atomic_write_json(path, value)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_review_delivery(**args)
    assert path.read_bytes() == before


def _build_metadata(
    root: Path,
    *,
    source: Path,
    avatar: Path,
    glb: Path,
    comparison: Path,
    glb_validation: dict,
    source_avatar_validation: dict,
    signer_review: dict,
    catalog: dict,
    neutral_hand_validation: dict | None = None,
    logical_identity_stem: str | None = None,
    execution_context: dict | None = None,
    preparation: dict | None = None,
) -> dict:
    return build_production_metadata(
        project_root=root,
        source_video=source,
        avatar_file=avatar,
        glb_path=glb,
        pose_path=root / "Change.pose.npz",
        motion_path=root / "Change.motion.npz",
        qc_path=root / "Change.qc.json",
        glb_validation_path=root / "Change.glb_validation.json",
        source_avatar_validation_path=root / "Change.source_avatar_validation.json",
        signer_review_path=root / "Change.signer_review.json",
        comparison_video_path=comparison,
        video_info={"fps": 25.0, "frame_count": 3, "timestamps_ms": [0, 40, 80]},
        tracking_qc={"technical_qc": "PASS", "metrics": {}},
        glb_validation=glb_validation,
        source_avatar_validation=source_avatar_validation,
        signer_review=signer_review,
        avatar_profile={},
        bone_map={},
        neutral_hand_validation=(
            {"status": "PASS"}
            if neutral_hand_validation is None
            else neutral_hand_validation
        ),
        neutral_hand_pose_path=None,
        started_at="2026-09-03T08:00:00Z",
        completed_at="2026-09-03T08:01:00Z",
        catalog_row=catalog,
        logical_identity_stem=logical_identity_stem,
        execution_context=execution_context,
        preparation=preparation,
    )
