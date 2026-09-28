from copy import deepcopy

import pytest

from src.qc.production_gate import evaluate_production_gate


SOURCE_HASH = "a" * 64
GLB_HASH = "b" * 64
COMPARISON_HASH = "c" * 64


def valid_inputs():
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
                "check_name": "full_clip_torso_hand_clearance_proxy",
                "status": "PASS",
                "mesh_aware": False,
                "evaluated_every_input_frame": True,
                "frames": {
                    "total": 91,
                    "evaluated": 91,
                    "evaluated_coverage": 1.0,
                },
            },
        },
        "source_avatar_validation": {
            "status": "PASS",
            "validation_run_id": "source-avatar-validation-run-001",
            "source_video_sha256": SOURCE_HASH,
            "comparison_video_sha256": COMPARISON_HASH,
            "source_frame_count": 91,
        },
        "neutral_hand_validation": {"status": "PASS"},
        "signer_review": {
            "signer_verdict": "PASS",
            "isl_verified": True,
            "reviewer": "Qualified ISL reviewer",
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
            "meaning": "change from one state to another",
            "license": "Internal licensed dataset",
            "source_reference": "catalog://isl/change/01",
            "provenance_verified": True,
        },
        "source_video_sha256": SOURCE_HASH,
        "glb_sha256": GLB_HASH,
        "comparison_video_sha256": COMPARISON_HASH,
    }


def blocker_codes(result):
    return {item["code"] for item in result["blockers"]}


def test_strict_gate_approves_only_when_all_checks_pass():
    result = evaluate_production_gate(**valid_inputs())

    assert result["schema_version"] == "1.1"
    assert result["status"] == "APPROVED"
    assert result["engineering_candidate"] is True
    assert result["production_eligible"] is True
    assert result["blockers"] == []
    assert all(result["checks"].values())


def test_technical_pass_without_human_and_provenance_is_only_candidate():
    values = valid_inputs()
    values["signer_review"] = {
        "signer_verdict": "PENDING",
        "isl_verified": False,
        "reviewer": None,
        "reviewed_at": None,
    }
    values["catalog"] = {}

    result = evaluate_production_gate(**values)

    assert result["status"] == "ENGINEERING_CANDIDATE"
    assert result["engineering_candidate"] is True
    assert result["production_eligible"] is False
    assert blocker_codes(result) == {
        "SIGNER_VERDICT_PASS",
        "ISL_VERIFIED",
        "REVIEWER_PRESENT",
        "REVIEWED_AT_VALID",
        "SIGNER_SIGNATURE_VERIFIED",
        "REVIEW_SOURCE_HASH_MATCHES",
        "REVIEW_GLB_HASH_MATCHES",
        "REVIEW_COMPARISON_HASH_MATCHES",
        "COLLISION_REVIEW_PASS",
        "PROVENANCE_VERIFIED",
        "MEANING_PRESENT",
        "LICENSE_PRESENT",
        "SOURCE_REFERENCE_PRESENT",
    }


def test_generic_review_status_does_not_replace_explicit_signer_verdict():
    values = valid_inputs()
    values["signer_review"].pop("signer_verdict")
    values["signer_review"]["status"] = "PASS"

    result = evaluate_production_gate(**values)

    assert result["checks"]["signer_verdict_pass"] is False
    assert result["production_eligible"] is False


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        ("technical_qc", "REVIEW", "TECHNICAL_QC_PASS"),
        ("technical_qc", "FAIL", "TECHNICAL_QC_PASS"),
        ("source_video_sha256", "not-a-sha", "SOURCE_SHA256_VALID"),
        ("glb_sha256", None, "GLB_SHA256_VALID"),
    ],
)
def test_failed_engineering_requirement_is_not_eligible(field, value, failed_check):
    values = valid_inputs()
    values[field] = value

    result = evaluate_production_gate(**values)

    assert result["status"] == "NOT_ELIGIBLE"
    assert result["engineering_candidate"] is False
    assert result["production_eligible"] is False
    assert failed_check in blocker_codes(result)


@pytest.mark.parametrize(
    "neutral_hand_validation",
    [
        None,
        {},
        {"status": "REVIEW"},
        {"status": "FAIL"},
        {"status": "pass"},
        {"status": " PASS "},
        {"status": True},
    ],
)
def test_neutral_hand_calibration_requires_pass_for_engineering_candidate(
    neutral_hand_validation,
):
    values = valid_inputs()
    values["neutral_hand_validation"] = neutral_hand_validation

    result = evaluate_production_gate(**values)

    assert result["checks"]["neutral_hand_calibration_pass"] is False
    assert result["engineering_candidate"] is False
    assert result["production_eligible"] is False
    assert "NEUTRAL_HAND_CALIBRATION_PASS" in blocker_codes(result)


@pytest.mark.parametrize(
    ("report", "field", "value", "failed_check"),
    [
        ("glb_validation", "validation_run_id", "", "GLB_VALIDATION_RUN_ID_PRESENT"),
        ("glb_validation", "validation_run_id", "   ", "GLB_VALIDATION_RUN_ID_PRESENT"),
        ("glb_validation", "validation_run_id", 123, "GLB_VALIDATION_RUN_ID_PRESENT"),
        (
            "glb_validation",
            "validated_glb_sha256",
            "d" * 64,
            "GLB_VALIDATION_HASH_MATCHES",
        ),
        (
            "source_avatar_validation",
            "validation_run_id",
            None,
            "SOURCE_AVATAR_VALIDATION_RUN_ID_PRESENT",
        ),
        (
            "source_avatar_validation",
            "source_video_sha256",
            "d" * 64,
            "SOURCE_AVATAR_SOURCE_HASH_MATCHES",
        ),
        (
            "source_avatar_validation",
            "comparison_video_sha256",
            "d" * 64,
            "SOURCE_AVATAR_COMPARISON_HASH_MATCHES",
        ),
    ],
)
def test_automated_validation_evidence_must_be_fresh_and_hash_bound(
    report, field, value, failed_check
):
    values = valid_inputs()
    values[report][field] = value

    result = evaluate_production_gate(**values)

    assert result["status"] == "NOT_ELIGIBLE"
    assert result["engineering_candidate"] is False
    assert result["production_eligible"] is False
    assert failed_check in blocker_codes(result)


def test_validation_evidence_hashes_must_match_current_artifacts_not_each_other_only():
    values = valid_inputs()
    replacement_hash = "d" * 64
    values["glb_validation"]["validated_glb_sha256"] = replacement_hash
    values["source_avatar_validation"]["source_video_sha256"] = replacement_hash
    values["source_avatar_validation"]["comparison_video_sha256"] = replacement_hash

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is False
    assert {
        "GLB_VALIDATION_HASH_MATCHES",
        "SOURCE_AVATAR_SOURCE_HASH_MATCHES",
        "SOURCE_AVATAR_COMPARISON_HASH_MATCHES",
    }.issubset(blocker_codes(result))


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        ("status", "REVIEW", "KHRONOS_VALIDATION_PASS"),
        ("status", None, "KHRONOS_VALIDATION_PASS"),
        ("validated_glb_sha256", "d" * 64, "KHRONOS_VALIDATION_HASH_MATCHES"),
        ("validated_glb_sha256", "invalid", "KHRONOS_VALIDATION_HASH_MATCHES"),
        ("validator_version", "", "KHRONOS_VALIDATOR_VERSION_PRESENT"),
        ("validator_version", "   ", "KHRONOS_VALIDATOR_VERSION_PRESENT"),
        ("validator_version", 2, "KHRONOS_VALIDATOR_VERSION_PRESENT"),
    ],
)
def test_khronos_validation_must_pass_be_versioned_and_match_current_glb(
    field, value, failed_check
):
    values = valid_inputs()
    values["glb_validation"]["khronos_validation"][field] = value

    result = evaluate_production_gate(**values)

    assert result["status"] == "NOT_ELIGIBLE"
    assert result["engineering_candidate"] is False
    assert failed_check in blocker_codes(result)


@pytest.mark.parametrize("num_errors", [1, -1, "0", False, 0.0, None])
def test_khronos_num_errors_requires_an_explicit_integer_zero(num_errors):
    values = valid_inputs()
    values["glb_validation"]["khronos_validation"]["counts"]["numErrors"] = num_errors

    result = evaluate_production_gate(**values)

    assert result["checks"]["khronos_num_errors_zero"] is False
    assert result["engineering_candidate"] is False
    assert "KHRONOS_NUM_ERRORS_ZERO" in blocker_codes(result)


def test_missing_khronos_report_fails_all_khronos_engineering_checks_closed():
    values = valid_inputs()
    values["glb_validation"].pop("khronos_validation")

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is False
    assert {
        "KHRONOS_VALIDATION_PASS",
        "KHRONOS_VALIDATION_HASH_MATCHES",
        "KHRONOS_VALIDATOR_VERSION_PRESENT",
        "KHRONOS_NUM_ERRORS_ZERO",
    }.issubset(blocker_codes(result))


def test_collision_metric_must_pass_cover_full_clip_and_match_source_count():
    values = valid_inputs()
    metric = values["glb_validation"]["full_clip_collision"]
    metric["status"] = "REVIEW"
    metric["evaluated_every_input_frame"] = False
    metric["frames"]["total"] = 90
    metric["frames"]["evaluated"] = 89
    metric["frames"]["evaluated_coverage"] = 89 / 90

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is False
    assert {
        "COLLISION_METRIC_PASS",
        "COLLISION_METRIC_FULL_CLIP",
        "COLLISION_METRIC_EVALUATED_COVERAGE_COMPLETE",
        "COLLISION_METRIC_FRAME_COUNT_MATCHES",
    }.issubset(blocker_codes(result))


def test_automated_collision_metric_does_not_replace_collision_review():
    values = valid_inputs()
    values["signer_review"].pop("collision_review")

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is True
    assert result["production_eligible"] is False
    assert "COLLISION_REVIEW_PASS" in blocker_codes(result)


@pytest.mark.parametrize(
    ("evaluated", "coverage"),
    [(90, 1.0), (91, 0.99), (None, 1.0), (91, None)],
)
def test_collision_metric_requires_complete_evaluated_frame_coverage(evaluated, coverage):
    values = valid_inputs()
    frames = values["glb_validation"]["full_clip_collision"]["frames"]
    frames["evaluated"] = evaluated
    frames["evaluated_coverage"] = coverage

    result = evaluate_production_gate(**values)

    assert result["checks"]["collision_metric_evaluated_coverage_complete"] is False
    assert result["engineering_candidate"] is False


def test_signer_review_is_bound_to_exact_source_and_glb_hashes():
    values = valid_inputs()
    values["signer_review"]["hash_binding"]["source_video_sha256"] = "c" * 64
    values["signer_review"]["hash_binding"]["glb_sha256"] = "d" * 64

    result = evaluate_production_gate(**values)

    assert result["engineering_candidate"] is True
    assert result["production_eligible"] is False
    assert {
        "REVIEW_SOURCE_HASH_MATCHES",
        "REVIEW_GLB_HASH_MATCHES",
    }.issubset(blocker_codes(result))


def test_comparison_evidence_must_exist_and_match_signer_review():
    values = valid_inputs()
    values["signer_review"]["hash_binding"]["comparison_video_sha256"] = "d" * 64

    mismatch = evaluate_production_gate(**values)
    assert mismatch["engineering_candidate"] is True
    assert mismatch["production_eligible"] is False
    assert "REVIEW_COMPARISON_HASH_MATCHES" in blocker_codes(mismatch)

    values["comparison_video_sha256"] = None
    missing = evaluate_production_gate(**values)
    assert missing["engineering_candidate"] is False
    assert missing["checks"]["comparison_sha256_valid"] is False
    assert missing["production_eligible"] is False
    assert "SOURCE_AVATAR_COMPARISON_HASH_MATCHES" in blocker_codes(missing)


@pytest.mark.parametrize(
    "reviewed_at",
    ["", "yesterday", "2026-09-03T08:30:00"],
)
def test_review_timestamp_must_be_iso_8601_and_timezone_aware(reviewed_at):
    values = valid_inputs()
    values["signer_review"]["reviewed_at"] = reviewed_at

    result = evaluate_production_gate(**values)

    assert result["checks"]["reviewed_at_valid"] is False
    assert result["production_eligible"] is False


def test_nested_metadata_catalog_and_flat_review_hashes_are_supported():
    values = valid_inputs()
    values["catalog"] = {
        "linguistic": {"meaning": "change"},
        "source": {
            "license": "CC BY 4.0",
            "source_reference": "dataset:item-42",
            "provenance_verified": True,
        },
    }
    binding = values["signer_review"].pop("hash_binding")
    values["signer_review"].update(binding)

    result = evaluate_production_gate(**values)

    assert result["status"] == "APPROVED"


@pytest.mark.parametrize("truthy_value", ["true", "yes", "1", 1])
def test_release_boolean_checks_require_actual_true(truthy_value):
    values = valid_inputs()
    values["signer_review"]["isl_verified"] = truthy_value
    values["catalog"]["provenance_verified"] = truthy_value

    result = evaluate_production_gate(**values)

    assert result["checks"]["isl_verified"] is False
    assert result["checks"]["provenance_verified"] is False
    assert result["production_eligible"] is False


def test_gate_is_pure_and_does_not_mutate_input_reports():
    values = valid_inputs()
    before = deepcopy(values)

    evaluate_production_gate(**values)

    assert values == before
