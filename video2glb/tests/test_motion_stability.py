from __future__ import annotations

import numpy as np

from src.qc.motion_stability import evaluate_quaternion_jitter


def identity(frames: int = 7) -> np.ndarray:
    values = np.zeros((frames, 2, 4), dtype=np.float64)
    values[..., 0] = 1.0
    return values


def test_constant_rotations_have_no_jitter():
    result = evaluate_quaternion_jitter(identity(), ["LeftUpperArm", "RightUpperArm"], 25.0)
    assert result["status"] == "PASS"
    assert result["maximum_degrees"] == 0.0


def test_quaternion_sign_flip_is_not_false_jitter():
    rotations = identity()
    rotations[3, 0] *= -1.0
    result = evaluate_quaternion_jitter(rotations, ["LeftUpperArm", "RightUpperArm"], 25.0)
    assert result["status"] == "PASS"


def test_isolated_rotation_spike_fails():
    rotations = identity()
    angle = np.radians(20.0) / 2.0
    rotations[3, 1] = [np.cos(angle), 0.0, np.sin(angle), 0.0]
    result = evaluate_quaternion_jitter(rotations, ["LeftUpperArm", "RightUpperArm"], 25.0)
    assert result["status"] == "FAIL"
    assert result["worst_frame"] == 4
    assert result["worst_channel"] == "RightUpperArm"


def test_invalid_shapes_fail_closed():
    result = evaluate_quaternion_jitter(np.zeros((2, 4)), ["Hips"], 25.0)
    assert result["status"] == "FAIL"


def test_transition_label_does_not_exempt_exported_rotation_spike():
    rotations = identity()
    rotations[3, 1] = [np.cos(np.radians(20) / 2), 0, np.sin(np.radians(20) / 2), 0]
    states = np.full((7, 2), "SOURCE_ACTIVE", dtype="<U16")
    states[3:, 1] = "NEUTRAL"
    result = evaluate_quaternion_jitter(rotations, ["LeftIndex3", "RightIndex3"], 25, sample_states=states)
    assert result["status"] == "FAIL"
    assert result["worst_samples"][0]["state"] == "TRANSITION"
    assert result["worst_samples"][0]["frame"] == 4
    assert result["state_metrics"]["TRANSITION"]["maximum_limit_exceeded_count"] == 1
    assert result["maximum_limit_exceeded_frame_indices"] == [4]


def test_source_active_label_does_not_claim_that_source_explains_a_spike():
    rotations = identity()
    rotations[3, 0] = [np.cos(np.radians(20) / 2), np.sin(np.radians(20) / 2), 0, 0]
    result = evaluate_quaternion_jitter(rotations, ["LeftIndex3", "RightIndex3"], 25,
                                        sample_states=np.full((7, 2), "SOURCE_ACTIVE"))
    assert result["status"] == "FAIL"
    assert result["worst_samples"][0]["state"] == "SOURCE_ACTIVE"


def test_bad_sample_state_timeline_fails_closed():
    result = evaluate_quaternion_jitter(identity(), ["LeftIndex3", "RightIndex3"], 25,
                                        sample_states=np.full((6, 2), "NEUTRAL"))
    assert result["status"] == "FAIL"


def test_nonfinite_threshold_cannot_silently_disable_jitter_gate():
    result = evaluate_quaternion_jitter(identity(), ["LeftIndex3", "RightIndex3"], 25,
                                        maximum_residual_degrees=float("nan"))
    assert result["status"] == "FAIL"
