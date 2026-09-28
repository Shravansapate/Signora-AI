from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import convert
from src.pipeline.batch_runner import ProcessAttempt
from src.qc.production_gate import evaluate_production_gate
from src.qc.signer_approval import canonical_approval_bytes, verify_signer_approval


SOURCE_HASH = "a" * 64
GLB_HASH = "b" * 64
COMPARISON_HASH = "c" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_verified_release(project_root: Path) -> tuple[dict, Path, dict[str, Path], dict]:
    """Create a minimal, genuinely signed release bundle for orchestration tests."""

    stem = "Change"
    run_id = "9" * 32
    output_dir = project_root / "output" / "CHANGE"
    evidence_dir = output_dir / "runs" / run_id / "evidence"
    evidence_dir.mkdir(parents=True)
    stable_glb = output_dir / f"{stem}.glb"
    run_glb = output_dir / "runs" / run_id / f"{stem}.glb"
    run_glb.write_bytes(b"verified glb")
    stable_glb.write_bytes(run_glb.read_bytes())

    paths = {
        "source_video": evidence_dir / "source_video.mp4",
        "avatar": evidence_dir / "avatar.fbx",
        "pose": evidence_dir / "pose.npz",
        "motion": evidence_dir / "motion.npz",
        "qc": evidence_dir / "qc.json",
        "glb_validation": evidence_dir / "glb_validation.json",
        "khronos_validation": evidence_dir / "khronos_validation.json",
        "source_avatar_validation": evidence_dir / "source_avatar_validation.json",
        "signer_review": evidence_dir / "signer_review.json",
        "comparison_video": evidence_dir / "comparison.mp4",
        "avatar_profile": evidence_dir / "avatar_profile.json",
        "bone_map": evidence_dir / "bone_map.json",
        "neutral_hand_pose": evidence_dir / "neutral_hand_pose.json",
        "catalog_record": evidence_dir / "catalog_record.json",
    }
    for role in ("source_video", "avatar", "pose", "motion", "comparison_video"):
        paths[role].write_bytes((role + " bytes").encode("utf-8"))
    paths["avatar_profile"].write_text('{"status":"PASS"}', encoding="utf-8")
    paths["bone_map"].write_text('{"status":"PASS"}', encoding="utf-8")
    neutral_validation = {"status": "PASS"}
    paths["neutral_hand_pose"].write_text(
        json.dumps({"validation": neutral_validation}), encoding="utf-8"
    )

    source_hash = _sha256(paths["source_video"])
    glb_hash = _sha256(run_glb)
    comparison_hash = _sha256(paths["comparison_video"])
    motion_hash = _sha256(paths["motion"])
    khronos = {
        "schema_version": "1.0",
        "status": "PASS",
        "validator_version": "2.0.0-dev.3.10",
        "validated_glb_sha256": glb_hash,
        "counts": {"numErrors": 0},
    }
    glb_report = {
        "status": "PASS",
        "validation_run_id": "validator-run",
        "validated_glb_sha256": glb_hash,
        "motion_sha256": motion_hash,
        "source_frame_count": 1,
        "khronos_validation": khronos,
        "full_clip_collision": {
            "status": "PASS",
            "evaluated_every_input_frame": True,
            "frames": {"total": 1, "evaluated": 1, "evaluated_coverage": 1.0},
        },
    }
    source_report = {
        "status": "PASS",
        "validation_run_id": run_id,
        "source_video_sha256": source_hash,
        "comparison_video_sha256": comparison_hash,
        "source_frame_count": 1,
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    approval = {
        "schema_version": "1.0",
        "gloss": "CHANGE",
        "signer_verdict": "PASS",
        "isl_verified": True,
        "reviewer": "Signer One",
        "reviewed_at": "2026-09-05T10:00:00+05:30",
        "collision_review": {"status": "PASS", "method": "full clip"},
        "hash_binding": {
            "source_video_sha256": source_hash,
            "glb_sha256": glb_hash,
            "comparison_video_sha256": comparison_hash,
        },
    }
    approval["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "signer-one",
        "value_base64": base64.b64encode(
            private_key.sign(canonical_approval_bytes(approval))
        ).decode("ascii"),
    }
    registry = {
        "keys": {
            "signer-one": {
                "reviewer": "Signer One",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "active": True,
            }
        }
    }
    registry_path = project_root / "config" / "trusted_signers.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    verified_approval = verify_signer_approval(approval, registry)
    signer_report = convert.build_signer_review_record(
        gloss=stem,
        technical_qc="PASS",
        source_video=paths["source_video"],
        glb_path=stable_glb,
        comparison_video=paths["comparison_video"],
        approval=verified_approval,
        run_id=run_id,
    )
    catalog = {
        "meaning": "change",
        "license": "licensed fixture",
        "source_reference": "catalog://change/01",
        "provenance_verified": True,
    }
    paths["catalog_record"].write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "logical_identity_stem": stem,
                "catalog": catalog,
            }
        ),
        encoding="utf-8",
    )
    gate = evaluate_production_gate(
        technical_qc="PASS",
        glb_validation=glb_report,
        source_avatar_validation=source_report,
        signer_review=signer_report,
        catalog=catalog,
        source_video_sha256=source_hash,
        glb_sha256=glb_hash,
        comparison_video_sha256=comparison_hash,
        neutral_hand_validation=neutral_validation,
    )
    assert gate["production_eligible"] is True
    qc_report = {"technical_qc": "PASS", "run_id": run_id, "production_gate": gate}
    for role, payload in (
        ("qc", qc_report),
        ("glb_validation", glb_report),
        ("khronos_validation", khronos),
        ("source_avatar_validation", source_report),
        ("signer_review", signer_report),
    ):
        paths[role].write_text(json.dumps(payload), encoding="utf-8")

    evidence = {
        role: convert.release_evidence_record(path)
        for role, path in paths.items()
    }
    evidence["stable_glb"] = convert.release_evidence_record(stable_glb)
    evidence["run_glb"] = convert.release_evidence_record(run_glb)
    metadata = {
        "run_id": run_id,
        "technical_qc": "PASS",
        "motion_identity": {"gloss": "CHANGE"},
        "source": {
            "license": catalog["license"],
            "source_reference": catalog["source_reference"],
            "provenance_verified": True,
        },
        "linguistic": {"meaning": catalog["meaning"]},
        "file_integrity": {
            "glb_sha256": glb_hash,
            "glb_size_bytes": run_glb.stat().st_size,
        },
        "technical_validation": {
            "technical_qc": "PASS",
            "glb_validation": glb_report,
            "source_avatar_validation": source_report,
            "neutral_hand_validation": neutral_validation,
        },
        "signer_review": signer_report,
        "production": {
            "production_status": "APPROVED",
            "production_eligible": True,
            "engineering_candidate": True,
            "release_gate": gate,
        },
        "release_evidence": evidence,
    }
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "status": "APPROVED",
        "releaseable": True,
        "engineering_candidate": True,
        "artifact": {
            "path": convert.relative_display(run_glb),
            "sha256": glb_hash,
            "size_bytes": run_glb.stat().st_size,
        },
        "stable_alias": {
            "path": convert.relative_display(stable_glb),
            "sha256": glb_hash,
        },
        "integrity_checks": {"release_evidence_matches": True},
        "evidence": evidence,
        "release_gate": gate,
    }
    (output_dir / f"{stem}.metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (output_dir / f"{stem}.release.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return metadata, registry_path, paths, verified_approval


def _approved_gate_inputs(*, isl_verified: object = True) -> dict:
    return {
        "technical_qc": "PASS",
        "glb_validation": {
            "status": "PASS",
            "validation_run_id": "glb-validation-run-001",
            "validated_glb_sha256": GLB_HASH,
            "khronos_validation": {
                "status": "PASS",
                "validated_glb_sha256": GLB_HASH,
                "validator_version": "2.0.0-dev.3.10",
                "counts": {"numErrors": 0},
            },
            "full_clip_collision": {
                "status": "PASS",
                "evaluated_every_input_frame": True,
                "frames": {"total": 1, "evaluated": 1, "evaluated_coverage": 1.0},
            },
        },
        "source_avatar_validation": {
            "status": "PASS",
            "validation_run_id": "source-avatar-validation-run-001",
            "source_video_sha256": SOURCE_HASH,
            "comparison_video_sha256": COMPARISON_HASH,
            "source_frame_count": 1,
        },
        "neutral_hand_validation": {"status": "PASS"},
        "signer_review": {
            "signer_verdict": "PASS",
            "isl_verified": isl_verified,
            "reviewer": "Qualified reviewer",
            "reviewed_at": "2026-09-03T08:30:00+05:30",
            "hash_binding": {
                "source_video_sha256": SOURCE_HASH,
                "glb_sha256": GLB_HASH,
                "comparison_video_sha256": COMPARISON_HASH,
            },
            "collision_review": {"status": "PASS"},
            "signature_verification": {"status": "PASS", "verified": True},
        },
        "catalog": {
            "meaning": "change",
            "license": "Licensed for production use",
            "source_reference": "catalog://isl/change/01",
            "provenance_verified": True,
        },
        "source_video_sha256": SOURCE_HASH,
        "glb_sha256": GLB_HASH,
        "comparison_video_sha256": COMPARISON_HASH,
    }


def _write_conversion_state(
    project_root: Path,
    stem: str,
    *,
    legacy_isl_verified: object,
    production_eligible: object,
) -> Path:
    video = project_root / "input" / f"{stem}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video fixture")

    output_dir = project_root / "output" / stem.upper()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = "1" * 32
    immutable_glb = output_dir / "runs" / run_id / f"{stem}.glb"
    immutable_glb.parent.mkdir(parents=True, exist_ok=True)
    immutable_glb.write_bytes(b"immutable glb fixture")
    stable_glb = output_dir / f"{stem}.glb"
    stable_glb.write_bytes(immutable_glb.read_bytes())
    glb_hash = _sha256(immutable_glb)
    qc_path = output_dir / f"{stem}.qc.json"
    qc_path.write_text(
        json.dumps(
            {
                "technical_qc": "PASS",
                # This duplicated legacy field must not decide release status.
                "isl_verified": legacy_isl_verified,
            }
        ),
        encoding="utf-8",
    )
    required_evidence = {
        "source_video",
        "avatar",
        "stable_glb",
        "run_glb",
        "pose",
        "motion",
        "qc",
        "glb_validation",
        "khronos_validation",
        "source_avatar_validation",
        "signer_review",
        "comparison_video",
        "avatar_profile",
        "bone_map",
        "neutral_hand_pose",
    }
    evidence = {
        name: {
            "path": str(immutable_glb.relative_to(project_root)),
            "exists": True,
            "size_bytes": immutable_glb.stat().st_size,
            "sha256": glb_hash,
        }
        for name in required_evidence
    }
    (output_dir / f"{stem}.metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "file_integrity": {
                    "glb_sha256": glb_hash,
                    "glb_size_bytes": immutable_glb.stat().st_size,
                },
                "production": {
                    "production_eligible": production_eligible,
                    "engineering_candidate": production_eligible in {True, False, "true"},
                    "production_status": (
                        "APPROVED" if production_eligible is True else "NOT_ELIGIBLE"
                    ),
                },
                "release_evidence": evidence,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / f"{stem}.release.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "APPROVED" if production_eligible is True else "NOT_ELIGIBLE",
                "releaseable": production_eligible is True,
                "artifact": {
                    "path": str(immutable_glb.relative_to(project_root)),
                    "sha256": glb_hash,
                },
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )
    return video


def test_pending_signer_record_is_bound_to_exact_current_artifacts(tmp_path: Path):
    source = tmp_path / "source.mp4"
    glb = tmp_path / "avatar.glb"
    comparison = tmp_path / "comparison.mp4"
    source.write_bytes(b"source bytes")
    glb.write_bytes(b"glb bytes")
    comparison.write_bytes(b"comparison bytes")

    review = convert.build_signer_review_record(
        gloss="change",
        technical_qc="PASS",
        source_video=source,
        glb_path=glb,
        comparison_video=comparison,
    )

    binding = review.get("hash_binding", {})
    assert binding.get("source_video_sha256") == _sha256(source)
    assert binding.get("glb_sha256") == _sha256(glb)
    assert review["isl_verified"] is False
    assert review["signer_verdict"] == "PENDING"


def test_approved_signer_record_preserves_exact_signed_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    _metadata, _registry, _paths, verified = _write_verified_release(tmp_path)
    review = json.loads(
        (tmp_path / "output" / "CHANGE" / "runs" / ("9" * 32) / "evidence" / "signer_review.json").read_text(
            encoding="utf-8"
        )
    )

    expected = dict(verified)
    expected.pop("signature_verification")
    assert review["signed_approval"] == expected


def test_input_hash_guard_detects_mutation_before_publication(tmp_path: Path):
    source = tmp_path / "source.mp4"
    avatar = tmp_path / "avatar.fbx"
    source.write_bytes(b"source-v1")
    avatar.write_bytes(b"avatar-v1")
    inputs = {"source video": source, "avatar": avatar}
    hashes = convert.capture_input_hashes(inputs)

    source.write_bytes(b"source-v2")

    with pytest.raises(RuntimeError, match="source video"):
        convert.assert_input_hashes_unchanged(inputs, hashes)


@pytest.mark.parametrize(
    "mutation",
    ("forged_gate", "role_alias", "tampered_report", "revoked_signer", "truthy_integrity"),
)
def test_published_release_is_recomputed_from_trusted_role_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    metadata, registry_path, paths, _verified = _write_verified_release(tmp_path)
    output_dir = tmp_path / "output" / "CHANGE"
    manifest_path = output_dir / "Change.release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if mutation == "forged_gate":
        metadata["production"]["release_gate"]["checks"]["technical_qc_pass"] = False
        manifest["release_gate"] = metadata["production"]["release_gate"]
    elif mutation == "role_alias":
        metadata["release_evidence"]["khronos_validation"] = dict(
            metadata["release_evidence"]["glb_validation"]
        )
        manifest["evidence"] = metadata["release_evidence"]
    elif mutation == "tampered_report":
        paths["glb_validation"].write_text('{"status":"PASS"}', encoding="utf-8")
    elif mutation == "revoked_signer":
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["keys"]["signer-one"]["active"] = False
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
    else:
        manifest["integrity_checks"]["release_evidence_matches"] = "true"

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ready, reasons = convert.evaluate_published_release(
        output_dir,
        "Change",
        metadata,
        trusted_signers_path=registry_path,
    )

    assert ready is False
    assert reasons


def test_intact_release_recomputes_as_approved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    metadata, registry_path, _paths, _verified = _write_verified_release(tmp_path)

    ready, reasons = convert.evaluate_published_release(
        tmp_path / "output" / "CHANGE",
        "Change",
        metadata,
        trusted_signers_path=registry_path,
    )

    assert ready is True, reasons


def test_neutral_run_enrichment_preserves_immutable_calibration_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    metadata, registry_path, _paths, _verified = _write_verified_release(tmp_path)
    metadata["technical_validation"]["neutral_hand_validation"] = {
        "status": "PASS", "applied": True, "run_check": {"status": "PASS"},
    }
    ready, reasons = convert.evaluate_published_release(
        tmp_path / "output" / "CHANGE", "Change", metadata, trusted_signers_path=registry_path,
    )
    assert ready, reasons
    metadata["technical_validation"]["neutral_hand_validation"]["status"] = "REVIEW"
    ready, reasons = convert.evaluate_published_release(
        tmp_path / "output" / "CHANGE", "Change", metadata, trusted_signers_path=registry_path,
    )
    assert not ready
    assert "neutral-hand calibration evidence does not match metadata" in reasons


def test_prepared_video_roles_are_required_and_bound_to_source_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    metadata, _registry, paths, _verified = _write_verified_release(tmp_path)
    preparation_path = paths["source_video"].parent / "video_preparation.json"
    working_path = paths["source_video"].parent / "working_video.mp4"
    working_path.write_bytes(b"working frames")
    preparation_path.write_text(json.dumps({
        "source": {"frame_count": 1}, "working": {"frame_count": 1},
        "normalized": False, "timing_mapping": [{"working_frame": 0}],
    }), encoding="utf-8")
    metadata["preparation"] = convert.preparation_summary(preparation_path, working_path)
    valid, reasons = convert.verify_release_evidence(metadata)
    assert not valid
    assert any("missing release evidence" in reason and "working_video" in reason for reason in reasons)
    for role, path in (("video_preparation", preparation_path), ("working_video", working_path)):
        metadata["release_evidence"][role] = convert.release_evidence_record(path)
    validation = metadata["technical_validation"]["source_avatar_validation"]
    validation.update(working_video_sha256=_sha256(working_path), original_source_frame_count=1,
                      timing_normalized=False)
    valid, reasons = convert.verify_release_evidence(metadata)
    assert valid, reasons
    validation["working_video_sha256"] = "0" * 64
    valid, reasons = convert.verify_release_evidence(metadata)
    assert not valid
    assert "source/avatar validation is not bound to the working video" in reasons


def test_approval_report_refresh_preserves_original_batch_execution_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    metadata, _registry, paths, _verified = _write_verified_release(tmp_path)
    run_id = metadata["run_id"]
    output = tmp_path / "output" / "CHANGE"
    metadata["asset_identity"] = {
        "video_id": _sha256(paths["source_video"]), "original_filename": "Change.mp4",
        "batch_id": "b" * 32, "run_id": run_id,
    }
    metadata["processing"] = {
        "run_id": run_id, "batch_id": "b" * 32, "attempt": 3,
        "context_fingerprint": "c" * 64, "stage_cache": [{"stage": "tracking", "reused": True}],
    }
    convert.atomic_write_json(output / "Change.metadata.json", metadata)
    monkeypatch.setattr(convert, "load_avatar_profile", lambda _path: {})
    monkeypatch.setattr(convert, "load_bone_map", lambda _path: {})
    video_info = SimpleNamespace(to_json_dict=lambda: {"fps": 25.0, "frame_count": 1, "timestamps_ms": [0]})
    refreshed = convert.write_reports(
        output / "Change.metadata.json", output / "Change.qc.json", video_info,
        convert.SavedQCResult({"technical_qc": "PASS"}),
        metadata["technical_validation"]["glb_validation"], "PASS",
        metadata["technical_validation"]["source_avatar_validation"], metadata["signer_review"],
        source_video=paths["source_video"], avatar_file=paths["avatar"],
        glb_path=output / "Change.glb", pose_path=paths["pose"], motion_path=paths["motion"],
        glb_validation_path=paths["glb_validation"], khronos_validation_path=paths["khronos_validation"],
        source_avatar_validation_path=paths["source_avatar_validation"], signer_review_path=paths["signer_review"],
        comparison_video_path=paths["comparison_video"], profile_path=paths["avatar_profile"],
        bone_map_path=paths["bone_map"], neutral_hand_pose_path=paths["neutral_hand_pose"],
        neutral_hand_validation={"status": "PASS"}, run_id=run_id, logical_identity_stem="Change",
    )
    assert refreshed["asset_identity"] == metadata["asset_identity"]
    assert refreshed["processing"]["attempt"] == 3
    assert refreshed["processing"]["context_fingerprint"] == "c" * 64
    assert refreshed["processing"]["stage_cache"] == metadata["processing"]["stage_cache"]


def test_approve_existing_restores_prior_reports_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    _metadata, registry_path, evidence_paths, verified = _write_verified_release(tmp_path)
    output_dir = tmp_path / "output" / "CHANGE"
    stable_reports = [
        output_dir / name
        for name in (
            "Change.metadata.json",
            "Change.qc.json",
            "Change.glb_validation.json",
            "Change.khronos_validation.json",
            "Change.source_avatar_validation.json",
            "Change.match_validation.json",
            "Change.signer_review.json",
            "Change.release.json",
        )
    ]
    for path in stable_reports:
        if not path.exists():
            path.write_text(json.dumps({"prior": path.name}), encoding="utf-8")
    protected = stable_reports + [
        evidence_paths[name]
        for name in (
            "qc",
            "glb_validation",
            "khronos_validation",
            "source_avatar_validation",
            "signer_review",
            "catalog_record",
        )
    ]
    before = {path: path.read_bytes() for path in protected}
    glb_report = json.loads(evidence_paths["glb_validation"].read_text(encoding="utf-8"))
    khronos_report = json.loads(
        evidence_paths["khronos_validation"].read_text(encoding="utf-8")
    )

    monkeypatch.setattr(
        convert,
        "load_yaml",
        lambda _path: {"avatar": {"path": "unused.fbx"}, "output": {"uppercase_output_dir": True}},
    )
    monkeypatch.setattr(convert, "load_signer_approval", lambda *_args: verified)
    monkeypatch.setattr(convert, "run_blender_validate", lambda *_args, **_kwargs: glb_report)
    monkeypatch.setattr(convert, "run_khronos_validate", lambda *_args, **_kwargs: khronos_report)
    monkeypatch.setattr(
        convert,
        "inspect_video",
        lambda _path: SimpleNamespace(
            fps=25.0,
            to_json_dict=lambda: {
                "fps": 25.0,
                "frame_count": 1,
                "timestamps_ms": [0],
            },
        ),
    )

    def fail_late(*_args, **_kwargs):
        for path in protected:
            path.write_bytes(b"mutated during failed approval")
        raise RuntimeError("simulated late approval failure")

    monkeypatch.setattr(convert, "write_reports", fail_late)
    args = SimpleNamespace(
        video=str(tmp_path / "input" / "Change.mp4"),
        signer_approval=str(tmp_path / "approval.json"),
        trusted_signers=str(registry_path),
        config=str(tmp_path / "settings.yaml"),
        avatar=None,
        motion_catalog=None,
    )

    assert convert.approve_existing_output(args) == 1
    assert {path: path.read_bytes() for path in protected} == before


def test_supplied_approval_without_hashes_never_inherits_current_hashes(tmp_path: Path):
    source = tmp_path / "source.mp4"
    glb = tmp_path / "avatar.glb"
    source.write_bytes(b"source bytes")
    glb.write_bytes(b"glb bytes")

    review = convert.build_signer_review_record(
        gloss="change",
        technical_qc="PASS",
        source_video=source,
        glb_path=glb,
        comparison_video=None,
        approval={
            "gloss": "CHANGE",
            "signer_verdict": "PASS",
            "isl_verified": True,
            "reviewer": "Qualified reviewer",
            "reviewed_at": "2026-09-03T08:30:00+05:30",
            "collision_review": {"status": "PASS"},
        },
    )

    assert review["hash_binding"] == {}
    assert review["current_artifact_hashes"]["source_video_sha256"] == _sha256(source)


def test_release_manifest_fails_closed_for_missing_artifact(tmp_path: Path):
    manifest = tmp_path / "sample.release.json"
    metadata = {
        "run_id": "run-1",
        "production": {"production_status": "APPROVED", "production_eligible": True},
        "file_integrity": {"glb_sha256": "a" * 64, "glb_size_bytes": 12},
        "technical_validation": {
            "glb_validation": {"validated_glb_sha256": "a" * 64}
        },
    }

    with pytest.raises(RuntimeError, match="integrity commit failed"):
        convert.write_release_manifest(
            manifest,
            metadata,
            tmp_path / "missing.glb",
            "run-1",
            immutable_glb_path=tmp_path / "runs" / "run-1" / "missing.glb",
        )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["releaseable"] is False
    assert payload["status"] == "INTEGRITY_FAILURE"


def test_output_lock_rejects_concurrent_writer_and_releases_cleanly(tmp_path: Path):
    first = convert.acquire_output_lock(tmp_path / "output", "run-one")
    try:
        with pytest.raises(RuntimeError, match="Another conversion"):
            convert.acquire_output_lock(tmp_path / "output", "run-two")
    finally:
        convert.release_output_lock(first)

    second = convert.acquire_output_lock(tmp_path / "output", "run-three")
    convert.release_output_lock(second)


def test_shared_calibration_lock_waits_for_other_worker_then_releases(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    first = convert.acquire_output_lock(tmp_path, "first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(convert.acquire_output_lock, tmp_path, "second", wait_seconds=2.0)
        convert.release_output_lock(first)
        second = future.result(timeout=3)
        convert.release_output_lock(second)


def test_calibration_lock_wait_is_bounded(tmp_path: Path):
    first = convert.acquire_output_lock(tmp_path, "first")
    try:
        with pytest.raises(RuntimeError, match="Another conversion"):
            convert.acquire_output_lock(tmp_path, "second", wait_seconds=0.02)
    finally:
        convert.release_output_lock(first)


def test_failure_bundle_preserves_and_verifies_exact_revalidation_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    candidate = tmp_path / "output" / "CHANGE" / "runs" / ("1" * 32) / "Change.glb"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"failed glb")
    evidence = {}
    for name in (
        "source_video",
        "avatar",
        "pose",
        "motion",
        "avatar_profile",
        "bone_map",
        "neutral_hand_pose",
        "glb_validation",
        "khronos_validation",
        "settings",
        "qc_thresholds",
    ):
        path = tmp_path / "inputs" / f"{name}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
        evidence[name] = path

    convert.quarantine_failed_candidate(
        candidate,
        "CHANGE",
        "1" * 32,
        evidence_paths=evidence,
    )

    failed_glb = tmp_path / "failed" / "CHANGE" / ("1" * 32) / "Change.glb"
    loaded = convert.load_failure_bundle(failed_glb)
    assert set(loaded) == set(evidence)
    assert all(path.is_file() for path in loaded.values())


def test_failure_bundle_rejects_tampered_motion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    candidate = tmp_path / "candidate.glb"
    candidate.write_bytes(b"failed glb")
    evidence = {}
    for name in (
        "source_video",
        "avatar",
        "pose",
        "motion",
        "avatar_profile",
        "bone_map",
        "neutral_hand_pose",
        "glb_validation",
        "khronos_validation",
        "settings",
        "qc_thresholds",
    ):
        path = tmp_path / "inputs" / f"{name}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
        evidence[name] = path
    convert.quarantine_failed_candidate(
        candidate,
        "CHANGE",
        "2" * 32,
        evidence_paths=evidence,
    )
    failed_glb = tmp_path / "failed" / "CHANGE" / ("2" * 32) / "candidate.glb"
    manifest = json.loads((failed_glb.parent / "failure_bundle.json").read_text(encoding="utf-8"))
    motion = convert.resolve_project_path(manifest["evidence"]["motion"]["path"])
    motion.write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="motion"):
        convert.load_failure_bundle(failed_glb)


@pytest.mark.parametrize("mutation", [None, "stripped_role", "mismatched_report"])
def test_failure_recovery_requires_the_ik_report_bound_by_validation(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    candidate = tmp_path / "Train.glb"
    candidate.write_bytes(b"corrected GLB")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    evidence = {}
    for role in (
        "source_video", "avatar", "pose", "motion", "avatar_profile", "bone_map",
        "neutral_hand_pose", "khronos_validation", "settings", "qc_thresholds",
    ):
        path = inputs / f"{role}.bin"
        path.write_bytes(role.encode("utf-8"))
        evidence[role] = path
    ik_report = inputs / "ik_report.json"
    ik_report.write_text(json.dumps({"mesh_correction": {"applied": True}}), encoding="utf-8")
    validation = inputs / "glb_validation.json"
    validation.write_text(json.dumps({"status": "REVIEW", "ik_report_sha256": _sha256(ik_report)}), encoding="utf-8")
    evidence.update(ik_report=ik_report, glb_validation=validation)
    run_id = "7" * 32
    convert.quarantine_failed_candidate(candidate, "TRAIN", run_id, evidence_paths=evidence)
    failed_glb = tmp_path / "failed" / "TRAIN" / run_id / "Train.glb"
    manifest_path = failed_glb.parent / "failure_bundle.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "stripped_role":
        del manifest["evidence"]["ik_report"]
    elif mutation == "mismatched_report":
        stored_ik = convert.resolve_project_path(manifest["evidence"]["ik_report"]["path"])
        stored_ik.write_text(json.dumps({"mesh_correction": {"applied": False}}), encoding="utf-8")
        # This is a self-consistent bundle entry, but it is not the report used
        # to validate this GLB. The validation-to-IK binding must reject it.
        manifest["evidence"]["ik_report"] = convert.release_evidence_record(stored_ik)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    if mutation is None:
        loaded = convert.load_failure_bundle(failed_glb)
        assert _sha256(loaded["ik_report"]) == _sha256(ik_report)
    else:
        with pytest.raises(RuntimeError, match="[Ii][Kk]|correction report"):
            convert.load_failure_bundle(failed_glb)


def test_release_evidence_invalid_path_type_fails_without_throwing():
    metadata = {
        "release_evidence": {
            name: {"path": 42, "sha256": "a" * 64, "size_bytes": 1}
            for name in {
                "source_video",
                "avatar",
                "stable_glb",
                "run_glb",
                "pose",
                "motion",
                "qc",
                "glb_validation",
                "khronos_validation",
                "source_avatar_validation",
                "signer_review",
                "comparison_video",
                "avatar_profile",
                "bone_map",
                "neutral_hand_pose",
            }
        }
    }

    passed, reasons = convert.verify_release_evidence(metadata)

    assert passed is False
    assert any("path is invalid" in reason for reason in reasons)


def test_signer_boolean_must_be_a_real_json_boolean():
    values = _approved_gate_inputs(isl_verified="true")

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is True
    assert result["checks"]["isl_verified"] is False
    assert result["production_eligible"] is False


def test_failed_validator_process_cannot_reuse_a_preexisting_pass_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    report = tmp_path / "clip.glb_validation.json"
    stale_report = {"status": "PASS", "sentinel": "from an earlier run"}
    report.write_text(json.dumps(stale_report), encoding="utf-8")

    error = subprocess.CalledProcessError(1, ["blender"])

    def fail_without_writing(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(convert, "run_blender_validate", fail_without_writing)

    try:
        result = convert.validate_existing_glb(
            tmp_path / "blender.exe",
            tmp_path / "clip.glb",
            tmp_path / "clip.motion.npz",
            tmp_path / "profile.json",
            tmp_path / "bone-map.json",
            report,
        )
    except subprocess.CalledProcessError:
        # Propagating the process failure is safe; returning the stale PASS is not.
        return

    assert result.get("status") == "FAIL"
    assert result != stale_report


def test_failed_validator_process_may_return_a_fail_report_written_this_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    report = tmp_path / "clip.glb_validation.json"
    report.write_text(json.dumps({"status": "PASS", "generation": "old"}), encoding="utf-8")

    current_report = {
        "status": "FAIL",
        "generation": "current",
        "reasons": ["Current GLB failed validation."],
    }

    def fail_after_writing(*args, **_kwargs):
        Path(args[-1]).write_text(json.dumps(current_report), encoding="utf-8")
        raise subprocess.CalledProcessError(1, ["blender"])

    monkeypatch.setattr(convert, "run_blender_validate", fail_after_writing)

    result = convert.validate_existing_glb(
        tmp_path / "blender.exe",
        tmp_path / "clip.glb",
        tmp_path / "clip.motion.npz",
        tmp_path / "profile.json",
        tmp_path / "bone-map.json",
        report,
    )

    assert result == current_report


@pytest.mark.parametrize(
    ("legacy_isl_verified", "nested_eligible", "expected_ready"),
    [
        (True, False, 0),
        (False, True, 1),
        (False, "true", 0),
    ],
)
def test_existing_batch_readiness_uses_strict_nested_production_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_isl_verified: object,
    nested_eligible: object,
    expected_ready: int,
):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        convert,
        "evaluate_published_release",
        lambda _output_dir, _stem, metadata, **_kwargs: (
            metadata.get("production", {}).get("production_eligible") is True,
            [],
        ),
    )
    video = _write_conversion_state(
        tmp_path,
        "Change",
        legacy_isl_verified=legacy_isl_verified,
        production_eligible=nested_eligible,
    )

    summary = convert.summarize_existing_batch([video])

    assert summary["production_ready"] == expected_ready


@pytest.mark.parametrize(
    ("legacy_isl_verified", "nested_eligible", "expected_ready"),
    [
        (True, False, 0),
        (False, True, 1),
    ],
)
def test_new_batch_readiness_uses_nested_production_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_isl_verified: object,
    nested_eligible: object,
    expected_ready: int,
):
    input_dir = tmp_path / "input"
    _write_conversion_state(
        tmp_path,
        "Change",
        legacy_isl_verified=legacy_isl_verified,
        production_eligible=nested_eligible,
    )
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    # The fixture mocks conversion/classification; delivery integrity is tested separately.
    monkeypatch.setattr(convert, "attach_batch_review", lambda item, *args, **kwargs:
                        item.update(review_available=True, review_status="PENDING"))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for dependency in (
        config_dir / "settings.yaml",
        config_dir / "qc_thresholds.yaml",
        tmp_path / "avatar.fbx",
        tmp_path / "model.task",
    ):
        dependency.write_bytes(b"fixture")
    monkeypatch.setattr(
        convert,
        "evaluate_published_release",
        lambda _output_dir, _stem, metadata, **_kwargs: (
            metadata.get("production", {}).get("production_eligible") is True,
            [],
        ),
    )
    monkeypatch.setattr(
        convert,
        "load_yaml",
        lambda _path: {
            "output": {"uppercase_output_dir": True},
            "avatar": {"path": "./avatar.fbx"},
            "pose": {"model_path": "./model.task"},
        },
    )
    monkeypatch.setattr(
        convert,
        "build_batch_context_fingerprint",
        lambda **_kwargs: "f" * 64,
    )
    monkeypatch.setattr(
        convert,
        "classify_batch_artifact",
        lambda **_kwargs: {
            "status": "PASS",
            "isl_verified": False,
            "production_status": "APPROVED" if nested_eligible is True else "NOT_ELIGIBLE",
            "production_eligible": nested_eligible is True,
            "engineering_candidate": True,
            "release_integrity_reasons": [],
            "returncode": 0,
            "timed_out": False,
            "attempt_count": 1,
            "attempts": [],
        },
    )

    def run_jobs(jobs, *, on_result, **_kwargs):
        results = []
        for job in jobs:
            result = convert.BatchProcessResult(
                key=job.key,
                attempts=(
                    ProcessAttempt(
                        attempt=1,
                        returncode=0,
                        timed_out=False,
                        duration_seconds=0.0,
                        log_path=tmp_path / "worker.log",
                    ),
                ),
            )
            on_result(result)
            results.append(result)
        return results

    monkeypatch.setattr(
        convert,
        "run_isolated_jobs",
        run_jobs,
    )
    args = SimpleNamespace(
        input_dir=str(input_dir),
        config="./config/settings.yaml",
        qc_thresholds="./config/qc_thresholds.yaml",
        avatar=None,
        motion_catalog=None,
        save_debug=False,
    )

    exit_code = convert.run_batch(args)
    summary = json.loads((tmp_path / "output" / "batch_summary.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["production_ready"] == expected_ready
