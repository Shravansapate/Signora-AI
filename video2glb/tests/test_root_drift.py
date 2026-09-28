from __future__ import annotations

import numpy as np

from src.qc.root_drift import evaluate_root_drift


def samples() -> np.ndarray:
    return np.zeros((5, 3), dtype=np.float64)


def test_in_place_root_motion_passes():
    result = evaluate_root_drift(samples(), samples(), samples(), 2.0)
    assert result["status"] == "PASS"
    assert result["maximum_normalized_drift"]["hips"] == 0.0


def test_expected_translation_is_rejected_for_in_place_policy():
    expected = samples()
    expected[-1, 0] = 0.01
    result = evaluate_root_drift(expected, samples(), samples(), 2.0)
    assert result["status"] == "FAIL"
    assert any("expected root drift" in reason for reason in result["reasons"])


def test_baked_hip_drift_is_rejected():
    hips = samples()
    hips[-1, 2] = 0.02
    result = evaluate_root_drift(samples(), samples(), hips, 2.0)
    assert result["status"] == "FAIL"
    assert any("hips root drift" in reason for reason in result["reasons"])


def test_invalid_or_mismatched_samples_fail_closed():
    result = evaluate_root_drift(
        np.zeros((0, 3)),
        np.zeros((2, 3)),
        np.zeros((3, 3)),
        0.0,
    )
    assert result["status"] == "FAIL"
    assert len(result["reasons"]) >= 3
