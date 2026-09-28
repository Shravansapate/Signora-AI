from copy import deepcopy

import pytest

from src.qc.gltf_compliance import classify_gltf_validator_report


def report(*messages, errors=0, warnings=0, infos=0, hints=0, truncated=False):
    return {
        "uri": "Change.glb",
        "validatorVersion": "2.0.0-dev.3.10",
        "issues": {
            "numErrors": errors,
            "numWarnings": warnings,
            "numInfos": infos,
            "numHints": hints,
            "messages": list(messages),
            "truncated": truncated,
        },
    }


def message(code, severity, text="validator message"):
    return {"code": code, "message": text, "severity": severity, "pointer": "/meshes/0"}


def reason_codes(result):
    return {reason["code"] for reason in result["reasons"]}


def test_zero_errors_and_warnings_passes():
    source = report(infos=1, *[message("ACCESSOR_UNUSED", 2)])

    result = classify_gltf_validator_report(source)

    assert result["status"] == "PASS"
    assert result["report_readable"] is True
    assert result["counts"] == {
        "numErrors": 0,
        "numWarnings": 0,
        "numInfos": 1,
        "numHints": 0,
    }
    assert result["messages"] == source["issues"]["messages"]


def test_any_declared_or_message_error_fails():
    declared = classify_gltf_validator_report(report(errors=1))
    undeclared = classify_gltf_validator_report(
        report(message("INVALID_GLTF", 0), errors=0)
    )

    assert declared["status"] == "FAIL"
    assert undeclared["status"] == "FAIL"
    assert "VALIDATOR_ERRORS" in reason_codes(declared)
    assert "VALIDATOR_ERRORS" in reason_codes(undeclared)


def test_unallowlisted_warning_routes_to_review_and_is_preserved():
    warning = message("NODE_EMPTY", 1, "Empty node found")
    source = report(warning, warnings=1)

    result = classify_gltf_validator_report(source)

    assert result["status"] == "REVIEW"
    assert result["unallowlisted_warning_codes"] == ["NODE_EMPTY"]
    assert result["warning_codes"] == {"NODE_EMPTY": 1}
    assert result["messages"] == [warning]


def test_only_documented_allowlisted_warnings_pass_and_explain_exception():
    source = report(
        message("NODE_EMPTY", 1),
        message("NODE_EMPTY", 1),
        warnings=2,
    )

    result = classify_gltf_validator_report(
        source,
        allowlisted_warnings={
            "node_empty": "Intentional named attachment locator with no renderable payload."
        },
    )

    assert result["status"] == "PASS"
    assert result["unallowlisted_warning_codes"] == []
    assert result["allowlisted_warning_explanations"] == [
        {
            "code": "NODE_EMPTY",
            "occurrences": 2,
            "explanation": "Intentional named attachment locator with no renderable payload.",
        }
    ]


def test_blank_allowlist_explanation_does_not_suppress_warning():
    result = classify_gltf_validator_report(
        report(message("NODE_EMPTY", 1), warnings=1),
        allowlisted_warnings={"NODE_EMPTY": "   "},
    )

    assert result["status"] == "REVIEW"
    assert result["invalid_allowlist_codes"] == ["NODE_EMPTY"]
    assert result["unallowlisted_warning_codes"] == ["NODE_EMPTY"]


@pytest.mark.parametrize("unreadable", [None, "", "not JSON", "[]", 42])
def test_missing_or_unreadable_report_fails_closed(unreadable):
    result = classify_gltf_validator_report(unreadable)

    assert result["status"] == "FAIL"
    assert result["report_readable"] is False
    assert reason_codes(result) == {"UNREADABLE_OR_MISSING_REPORT"}


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"issues": None},
        {"issues": {"numErrors": 0, "numWarnings": 0, "numInfos": 0, "numHints": 0}},
        {
            "issues": {
                "numErrors": False,
                "numWarnings": 0,
                "numInfos": 0,
                "numHints": 0,
                "messages": [],
            }
        },
        report({"code": "BAD", "severity": "1"}, warnings=1),
    ],
)
def test_malformed_issue_schema_fails_closed(broken):
    result = classify_gltf_validator_report(broken)

    assert result["status"] == "FAIL"
    assert result["report_readable"] is False


def test_warning_count_mismatch_or_truncation_requires_review():
    mismatch = classify_gltf_validator_report(report(warnings=1))
    truncated = classify_gltf_validator_report(report(truncated=True))

    assert mismatch["status"] == "REVIEW"
    assert "WARNING_COUNT_MISMATCH" in reason_codes(mismatch)
    assert truncated["status"] == "REVIEW"
    assert "MESSAGES_TRUNCATED" in reason_codes(truncated)


def test_json_text_is_accepted_and_inputs_are_not_mutated():
    source = report()
    before = deepcopy(source)

    mapping_result = classify_gltf_validator_report(source)
    text_result = classify_gltf_validator_report(
        '{"validatorVersion":"2","issues":{"numErrors":0,"numWarnings":0,'
        '"numInfos":0,"numHints":0,"messages":[],"truncated":false}}'
    )

    assert mapping_result["status"] == "PASS"
    assert text_result["status"] == "PASS"
    assert source == before
