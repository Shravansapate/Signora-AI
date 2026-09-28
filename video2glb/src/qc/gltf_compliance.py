"""Fail-closed classification of Khronos glTF Validator JSON reports."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from typing import Any, Mapping


CLASSIFIER_SCHEMA_VERSION = "1.0"

_COUNT_KEYS = ("numErrors", "numWarnings", "numInfos", "numHints")
_SEVERITY_ERROR = 0
_SEVERITY_WARNING = 1
_MISSING_WARNING_CODE = "<MISSING_CODE>"


def classify_gltf_validator_report(
    report: Mapping[str, Any] | str | bytes | bytearray | None,
    *,
    allowlisted_warnings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Classify one official Khronos glTF Validator result.

    The function is pure: callers provide either an already-decoded mapping or
    JSON text/bytes.  ``None``, invalid JSON, and malformed issue structures
    fail closed.  An allowlist maps an exact warning code to a non-empty reason
    explaining why that warning is accepted for this deployment.

    Errors always produce ``FAIL``.  With zero errors, an unseen,
    unallowlisted, uninspectable, or truncated warning produces ``REVIEW``.
    ``PASS`` is possible only when all reported warning codes have documented
    allowlist entries.
    """

    parsed, read_error = _decode_report(report)
    if read_error is not None:
        return _unreadable_result(read_error)

    assert parsed is not None
    issues = parsed.get("issues")
    if not isinstance(issues, Mapping):
        return _unreadable_result("The report does not contain an issues object.", parsed)

    raw_counts = {key: issues.get(key) for key in _COUNT_KEYS}
    counts = _validated_counts(raw_counts)
    raw_messages = issues.get("messages")
    preserved_messages = deepcopy(raw_messages) if isinstance(raw_messages, list) else []

    if counts is None:
        return _unreadable_result(
            "The issues object has missing or invalid non-negative integer counts.",
            parsed,
            counts=raw_counts,
            messages=preserved_messages,
        )
    if not isinstance(raw_messages, list) or not all(
        isinstance(message, Mapping) for message in raw_messages
    ):
        return _unreadable_result(
            "The issues.messages value must be a list of message objects.",
            parsed,
            counts=counts,
            messages=preserved_messages,
        )

    truncated = issues.get("truncated", False)
    if not isinstance(truncated, bool):
        return _unreadable_result(
            "The issues.truncated value must be a JSON boolean when present.",
            parsed,
            counts=counts,
            messages=preserved_messages,
        )

    message_severities: list[int] = []
    for message in raw_messages:
        severity = message.get("severity")
        if isinstance(severity, bool) or not isinstance(severity, int) or severity not in range(4):
            return _unreadable_result(
                "Every validator message must contain an integer severity from 0 through 3.",
                parsed,
                counts=counts,
                messages=preserved_messages,
            )
        message_severities.append(severity)

    allowlist, invalid_allowlist_codes = _normalize_allowlist(allowlisted_warnings)
    warning_codes = [
        _normalize_code(message.get("code"))
        for message, severity in zip(raw_messages, message_severities)
        if severity == _SEVERITY_WARNING
    ]
    warning_counts = Counter(warning_codes)
    allowlisted_codes = sorted(code for code in warning_counts if code in allowlist)
    unallowlisted_codes = sorted(code for code in warning_counts if code not in allowlist)
    allowlisted_explanations = [
        {
            "code": code,
            "occurrences": warning_counts[code],
            "explanation": allowlist[code],
        }
        for code in allowlisted_codes
    ]

    declared_errors = counts["numErrors"]
    message_errors = sum(severity == _SEVERITY_ERROR for severity in message_severities)
    declared_warnings = counts["numWarnings"]
    message_warnings = len(warning_codes)
    reasons: list[dict[str, Any]] = []

    if declared_errors or message_errors:
        reasons.append(
            {
                "code": "VALIDATOR_ERRORS",
                "message": "Khronos glTF Validator reported one or more errors.",
                "declared_count": declared_errors,
                "message_count": message_errors,
            }
        )
        status = "FAIL"
    else:
        if unallowlisted_codes:
            reasons.append(
                {
                    "code": "UNALLOWLISTED_WARNINGS",
                    "message": "One or more validator warning codes are not explicitly allowlisted.",
                    "warning_codes": unallowlisted_codes,
                }
            )
        if declared_warnings != message_warnings:
            reasons.append(
                {
                    "code": "WARNING_COUNT_MISMATCH",
                    "message": "The declared warning count does not match inspectable warning messages.",
                    "declared_count": declared_warnings,
                    "message_count": message_warnings,
                }
            )
        if truncated:
            reasons.append(
                {
                    "code": "MESSAGES_TRUNCATED",
                    "message": "The validator marked its issue-message list as truncated.",
                }
            )
        status = "REVIEW" if reasons else "PASS"

    return {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": status,
        "report_readable": True,
        "validator_version": parsed.get("validatorVersion"),
        "counts": counts,
        "messages": preserved_messages,
        "messages_truncated": truncated,
        "warning_codes": dict(sorted(warning_counts.items())),
        "allowlisted_warning_explanations": allowlisted_explanations,
        "unallowlisted_warning_codes": unallowlisted_codes,
        "invalid_allowlist_codes": invalid_allowlist_codes,
        "reasons": reasons,
    }


def _decode_report(
    report: Mapping[str, Any] | str | bytes | bytearray | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if report is None:
        return None, "The Khronos glTF Validator report is missing."
    if isinstance(report, Mapping):
        return report, None
    if isinstance(report, (str, bytes, bytearray)):
        if not report:
            return None, "The Khronos glTF Validator report is missing."
        try:
            parsed = json.loads(report)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            return None, f"The Khronos glTF Validator report is unreadable JSON: {exc}."
        if not isinstance(parsed, Mapping):
            return None, "The Khronos glTF Validator report root must be a JSON object."
        return parsed, None
    return None, "The Khronos glTF Validator report must be a mapping or JSON text."


def _validated_counts(raw_counts: Mapping[str, Any]) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for key in _COUNT_KEYS:
        value = raw_counts.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts[key] = value
    return counts


def _normalize_allowlist(
    allowlisted_warnings: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    if allowlisted_warnings is None:
        return {}, []
    if not isinstance(allowlisted_warnings, Mapping):
        return {}, ["<INVALID_ALLOWLIST>"]

    normalized: dict[str, str] = {}
    invalid: list[str] = []
    for raw_code, raw_explanation in allowlisted_warnings.items():
        code = _normalize_code(raw_code)
        explanation = str(raw_explanation).strip() if raw_explanation is not None else ""
        if code == _MISSING_WARNING_CODE or not explanation or code in normalized:
            invalid.append(code)
            continue
        normalized[code] = explanation
    return normalized, sorted(set(invalid))


def _normalize_code(value: Any) -> str:
    code = str(value).strip().upper() if value is not None else ""
    return code or _MISSING_WARNING_CODE


def _unreadable_result(
    message: str,
    report: Mapping[str, Any] | None = None,
    *,
    counts: Mapping[str, Any] | None = None,
    messages: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": "FAIL",
        "report_readable": False,
        "validator_version": report.get("validatorVersion") if report is not None else None,
        "counts": deepcopy(dict(counts)) if counts is not None else None,
        "messages": deepcopy(messages) if messages is not None else [],
        "messages_truncated": None,
        "warning_codes": {},
        "allowlisted_warning_explanations": [],
        "unallowlisted_warning_codes": [],
        "invalid_allowlist_codes": [],
        "reasons": [{"code": "UNREADABLE_OR_MISSING_REPORT", "message": message}],
    }
