"""Deterministic release gates for generated animation assets.

This module deliberately does not read files or mutate report payloads.  It
separates a technically sound engineering candidate from an asset that is
allowed to be released to production.  Human language review, provenance, and
hash binding are release requirements, not warnings.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime
from typing import Any, Mapping


GATE_SCHEMA_VERSION = "1.1"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ENGINEERING_CHECKS = (
    "technical_qc_pass",
    "glb_validation_pass",
    "glb_validation_run_id_present",
    "glb_validation_hash_matches",
    "khronos_validation_pass",
    "khronos_validation_hash_matches",
    "khronos_validator_version_present",
    "khronos_num_errors_zero",
    "source_avatar_validation_pass",
    "source_avatar_validation_run_id_present",
    "source_avatar_source_hash_matches",
    "source_avatar_comparison_hash_matches",
    "neutral_hand_calibration_pass",
    "collision_metric_pass",
    "collision_metric_full_clip",
    "collision_metric_evaluated_coverage_complete",
    "collision_metric_frame_count_matches",
    "source_sha256_valid",
    "glb_sha256_valid",
)

_PRODUCTION_ONLY_CHECKS = (
    "signer_verdict_pass",
    "isl_verified",
    "reviewer_present",
    "reviewed_at_valid",
    "signer_signature_verified",
    "review_source_hash_matches",
    "review_glb_hash_matches",
    "comparison_sha256_valid",
    "review_comparison_hash_matches",
    "collision_review_pass",
    "provenance_verified",
    "meaning_present",
    "license_present",
    "source_reference_present",
)

_BLOCKER_MESSAGES = {
    "technical_qc_pass": "Technical QC must be PASS.",
    "glb_validation_pass": "Fresh-import GLB validation must be PASS.",
    "glb_validation_run_id_present": (
        "Fresh-import GLB validation must identify its validation run."
    ),
    "glb_validation_hash_matches": (
        "Fresh-import GLB validation must be bound to the current GLB SHA-256."
    ),
    "khronos_validation_pass": "Official Khronos glTF validation must be PASS.",
    "khronos_validation_hash_matches": (
        "Khronos validation must be bound to the current GLB SHA-256."
    ),
    "khronos_validator_version_present": (
        "Khronos validation must identify the validator version."
    ),
    "khronos_num_errors_zero": (
        "Khronos validation must report an integer numErrors value of zero."
    ),
    "source_avatar_validation_pass": "Source/avatar validation must be PASS.",
    "source_avatar_validation_run_id_present": (
        "Source/avatar validation must identify its validation run."
    ),
    "source_avatar_source_hash_matches": (
        "Source/avatar validation must be bound to the current source-video SHA-256."
    ),
    "source_avatar_comparison_hash_matches": (
        "Source/avatar validation must be bound to the current comparison-video SHA-256."
    ),
    "neutral_hand_calibration_pass": "Neutral-hand calibration must have an explicit PASS status.",
    "collision_metric_pass": "The full-clip collision metric must be present and PASS.",
    "collision_metric_full_clip": "The collision metric must explicitly evaluate every input frame.",
    "collision_metric_evaluated_coverage_complete": (
        "Every collision-report frame must be evaluable; partial landmark coverage is not release-safe."
    ),
    "collision_metric_frame_count_matches": "The collision metric frame count must match the source clip.",
    "source_sha256_valid": "A valid current source-video SHA-256 is required.",
    "glb_sha256_valid": "A valid current GLB SHA-256 is required.",
    "signer_verdict_pass": "A qualified signer must record signer_verdict=PASS.",
    "isl_verified": "The signer review must explicitly set isl_verified=true.",
    "reviewer_present": "The signer review must identify the reviewer.",
    "reviewed_at_valid": "The signer review needs a valid timezone-aware reviewed_at timestamp.",
    "signer_signature_verified": (
        "The signer approval must have a valid Ed25519 signature from an active trusted reviewer key."
    ),
    "review_source_hash_matches": "The reviewed source-video hash must match the current source-video hash.",
    "review_glb_hash_matches": "The reviewed GLB hash must match the current GLB hash.",
    "comparison_sha256_valid": "A valid current source/avatar comparison-video SHA-256 is required.",
    "review_comparison_hash_matches": (
        "The reviewed comparison-video hash must match the current comparison-video hash."
    ),
    "collision_review_pass": "A full-clip collision review must be PASS.",
    "provenance_verified": "Source provenance must be explicitly verified.",
    "meaning_present": "A verified sign meaning is required.",
    "license_present": "A source license or usage-rights statement is required.",
    "source_reference_present": "A traceable source reference is required.",
}


def evaluate_production_gate(
    *,
    technical_qc: str | Mapping[str, Any] | None,
    glb_validation: Mapping[str, Any] | None,
    source_avatar_validation: Mapping[str, Any] | None,
    signer_review: Mapping[str, Any] | None,
    catalog: Mapping[str, Any] | None,
    source_video_sha256: str | None,
    glb_sha256: str | None,
    comparison_video_sha256: str | None = None,
    neutral_hand_validation: Mapping[str, Any] | None = None,
    collision_review: str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate engineering-candidate and strict production-release gates.

    ``glb_validation`` must contain the automated full-clip collision report
    under ``full_clip_collision`` (several migration aliases are accepted).
    The report must be PASS, declare ``evaluated_every_input_frame=true``, and
    cover the same number of frames as the source/avatar validation report.
    ``neutral_hand_validation.status`` must be the exact string ``PASS``;
    missing, lower-case, padded, or non-string values fail closed.

    ``collision_review`` is an independent release decision.  When omitted it
    may be supplied as ``signer_review.collision_review`` or
    ``glb_validation.collision_review``.  This prevents the automated
    landmark-space proxy from silently standing in for a human or mesh-aware
    collision review.

    Fresh-import and source/avatar validation reports must carry a nonblank
    ``validation_run_id`` and hashes that bind their evidence to the current
    artifacts.  Nested ``glb_validation.khronos_validation`` must be PASS,
    name its validator version, report integer ``numErrors=0``, and bind to
    that same current GLB.  The signer review must separately bind the source,
    exact generated GLB, and comparison video by SHA-256.  Review hashes can
    be stored in ``signer_review.hash_binding`` or as compatible flat fields.
    """

    glb_report = _mapping(glb_validation)
    source_avatar_report = _mapping(source_avatar_validation)
    neutral_hand_report = _mapping(neutral_hand_validation)
    review = _mapping(signer_review)
    catalog_record = _mapping(catalog)

    collision_metric = _extract_collision_metric(glb_report)
    khronos_report = _mapping(glb_report.get("khronos_validation"))
    khronos_counts = _mapping(khronos_report.get("counts"))
    metric_frames = _mapping(collision_metric.get("frames"))
    metric_total = _positive_int(metric_frames.get("total"))
    metric_evaluated = _positive_int(metric_frames.get("evaluated"))
    metric_coverage = _coverage_value(metric_frames.get("evaluated_coverage"))
    expected_total = _expected_source_frame_count(source_avatar_report, glb_report)

    current_source_hash = _normalize_sha256(source_video_sha256)
    current_glb_hash = _normalize_sha256(glb_sha256)
    current_comparison_hash = _normalize_sha256(comparison_video_sha256)
    reviewed_source_hash = _review_hash(review, "source")
    reviewed_glb_hash = _review_hash(review, "glb")
    reviewed_comparison_hash = _review_hash(review, "comparison")
    validated_glb_hash = _normalize_sha256(glb_report.get("validated_glb_sha256"))
    khronos_validated_glb_hash = _normalize_sha256(
        khronos_report.get("validated_glb_sha256")
    )
    validated_source_hash = _normalize_sha256(source_avatar_report.get("source_video_sha256"))
    validated_comparison_hash = _normalize_sha256(
        source_avatar_report.get("comparison_video_sha256")
    )

    if collision_review is None:
        collision_review = review.get("collision_review")
    if collision_review is None:
        collision_review = glb_report.get("collision_review")

    checks = {
        "technical_qc_pass": _status(technical_qc, "technical_qc") == "PASS",
        "glb_validation_pass": _status(glb_report) == "PASS",
        "glb_validation_run_id_present": _nonblank_string(
            glb_report.get("validation_run_id")
        ),
        "glb_validation_hash_matches": _hashes_match(
            current_glb_hash, validated_glb_hash
        ),
        "khronos_validation_pass": _status(khronos_report) == "PASS",
        "khronos_validation_hash_matches": _hashes_match(
            current_glb_hash, khronos_validated_glb_hash
        ),
        "khronos_validator_version_present": _nonblank_string(
            khronos_report.get("validator_version")
        ),
        "khronos_num_errors_zero": _exact_zero_int(khronos_counts.get("numErrors")),
        "source_avatar_validation_pass": _status(source_avatar_report) == "PASS",
        "source_avatar_validation_run_id_present": _nonblank_string(
            source_avatar_report.get("validation_run_id")
        ),
        "source_avatar_source_hash_matches": _hashes_match(
            current_source_hash, validated_source_hash
        ),
        "source_avatar_comparison_hash_matches": _hashes_match(
            current_comparison_hash, validated_comparison_hash
        ),
        "neutral_hand_calibration_pass": neutral_hand_report.get("status") == "PASS",
        "collision_metric_pass": _status(collision_metric) == "PASS",
        "collision_metric_full_clip": collision_metric.get("evaluated_every_input_frame") is True,
        "collision_metric_evaluated_coverage_complete": (
            metric_total is not None
            and metric_evaluated == metric_total
            and metric_coverage == 1.0
        ),
        "collision_metric_frame_count_matches": (
            metric_total is not None
            and expected_total is not None
            and metric_total == expected_total
        ),
        "source_sha256_valid": current_source_hash is not None,
        "glb_sha256_valid": current_glb_hash is not None,
        "signer_verdict_pass": _status(review, "signer_verdict") == "PASS",
        "isl_verified": review.get("isl_verified") is True,
        "reviewer_present": _present(review.get("reviewer")),
        "reviewed_at_valid": _valid_review_timestamp(review.get("reviewed_at")),
        "signer_signature_verified": (
            _mapping(review.get("signature_verification")).get("verified") is True
            and _status(_mapping(review.get("signature_verification"))) == "PASS"
        ),
        "review_source_hash_matches": _hashes_match(current_source_hash, reviewed_source_hash),
        "review_glb_hash_matches": _hashes_match(current_glb_hash, reviewed_glb_hash),
        "comparison_sha256_valid": current_comparison_hash is not None,
        "review_comparison_hash_matches": _hashes_match(
            current_comparison_hash, reviewed_comparison_hash
        ),
        "collision_review_pass": _status(collision_review) == "PASS",
        "provenance_verified": _provenance_verified(catalog_record),
        "meaning_present": _catalog_present(catalog_record, "meaning", "linguistic"),
        "license_present": _catalog_present(catalog_record, "license", "source"),
        "source_reference_present": _catalog_present(catalog_record, "source_reference", "source"),
    }

    engineering_candidate = all(checks[name] for name in _ENGINEERING_CHECKS)
    production_eligible = engineering_candidate and all(
        checks[name] for name in _PRODUCTION_ONLY_CHECKS
    )

    blockers = []
    for name in (*_ENGINEERING_CHECKS, *_PRODUCTION_ONLY_CHECKS):
        if checks[name]:
            continue
        blockers.append(
            {
                "code": name.upper(),
                "gate": (
                    "ENGINEERING_CANDIDATE"
                    if name in _ENGINEERING_CHECKS
                    else "PRODUCTION_RELEASE"
                ),
                "message": _BLOCKER_MESSAGES[name],
            }
        )

    if production_eligible:
        status = "APPROVED"
    elif engineering_candidate:
        status = "ENGINEERING_CANDIDATE"
    else:
        status = "NOT_ELIGIBLE"

    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "checks": checks,
        "blockers": blockers,
        "status": status,
        "engineering_candidate": engineering_candidate,
        "production_eligible": production_eligible,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status(value: Any, preferred_key: str = "status") -> str:
    if isinstance(value, Mapping):
        value = value.get(preferred_key)
    return str(value or "").strip().upper()


def _extract_collision_metric(glb_validation: Mapping[str, Any]) -> Mapping[str, Any]:
    aliases = (
        "full_clip_collision",
        "full_clip_collision_metric",
        "collision_metric",
        "collision_validation",
        "torso_hand_clearance",
        "full_clip_torso_hand_clearance_proxy",
    )
    for key in aliases:
        value = glb_validation.get(key)
        if isinstance(value, Mapping):
            return value

    # Permit a named report during schema migration without treating the
    # independent ``collision_review`` decision as the automated metric.
    for value in glb_validation.values():
        if not isinstance(value, Mapping):
            continue
        check_name = str(value.get("check_name") or "").strip().lower()
        if check_name == "full_clip_torso_hand_clearance_proxy":
            return value
    return {}


def _expected_source_frame_count(
    source_avatar_validation: Mapping[str, Any],
    glb_validation: Mapping[str, Any],
) -> int | None:
    for value in (
        source_avatar_validation.get("source_frame_count"),
        glb_validation.get("source_frame_count"),
        glb_validation.get("frame_count"),
    ):
        parsed = _positive_int(value)
        if parsed is not None:
            return parsed
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _coverage_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _normalize_sha256(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text.lower() if _SHA256_RE.fullmatch(text) else None


def _review_hash(review: Mapping[str, Any], asset: str) -> str | None:
    nested_candidates = (
        _mapping(review.get("hash_binding")),
        _mapping(review.get("reviewed_assets")),
        _mapping(review.get("asset_hashes")),
    )
    if asset == "source":
        names = (
            "source_video_sha256",
            "reviewed_source_video_sha256",
            "source_sha256",
        )
        nested_asset_names = ("source_video", "source")
    elif asset == "glb":
        names = ("glb_sha256", "reviewed_glb_sha256")
        nested_asset_names = ("glb", "final_glb")
    else:
        names = (
            "comparison_video_sha256",
            "reviewed_comparison_video_sha256",
            "comparison_sha256",
        )
        nested_asset_names = ("comparison_video", "comparison")

    for container in (*nested_candidates, review):
        for name in names:
            normalized = _normalize_sha256(container.get(name))
            if normalized is not None:
                return normalized
        for asset_name in nested_asset_names:
            asset_record = _mapping(container.get(asset_name))
            normalized = _normalize_sha256(asset_record.get("sha256"))
            if normalized is not None:
                return normalized
    return None


def _hashes_match(current: str | None, reviewed: str | None) -> bool:
    return bool(current and reviewed and hmac.compare_digest(current, reviewed))


def _present(value: Any) -> bool:
    return bool(str(value).strip()) if value is not None else False


def _nonblank_string(value: Any) -> bool:
    """Return true only for a non-empty string, never for truthy coercions."""

    return isinstance(value, str) and bool(value.strip())


def _exact_zero_int(value: Any) -> bool:
    """Reject booleans, floats, and string coercions even when numerically zero."""

    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _valid_review_timestamp(value: Any) -> bool:
    if not _present(value):
        return False
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _provenance_verified(catalog: Mapping[str, Any]) -> bool:
    direct = catalog.get("provenance_verified")
    nested = _mapping(catalog.get("source")).get("provenance_verified")
    return direct is True or nested is True


def _catalog_present(catalog: Mapping[str, Any], key: str, section: str) -> bool:
    return _present(catalog.get(key)) or _present(_mapping(catalog.get(section)).get(key))
