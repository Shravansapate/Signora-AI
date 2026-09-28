import json

import numpy as np
import pytest

from src.motion.neutral_hand import (
    apply_boundary_neutral_pose,
    apply_neutral_pose,
    ensure_neutral_hand_pose,
    generate_neutral_hand_pose,
    neutral_rotations_by_canonical,
    neutral_wrist_rotations_by_canonical,
    validate_neutral_hand_pose,
)


def test_avatar_derived_neutral_pose_has_complete_normalized_finger_map():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    rotations = neutral_rotations_by_canonical(payload)
    assert payload["validation"]["status"] in {"PASS", "REVIEW"}
    assert len(rotations) == 30
    assert all(np.isclose(np.linalg.norm(value), 1.0) for value in rotations.values())
    wrists = neutral_wrist_rotations_by_canonical(payload)
    assert set(wrists) == {"LeftHand", "RightHand"}
    # Each side is calibrated from its own geometry; a real avatar need not be exactly symmetric.
    left_roll = payload["wrist_pose"]["left_hand"]["roll_degrees"]
    right_roll = payload["wrist_pose"]["right_hand"]["roll_degrees"]
    assert left_roll * right_roll < 0
    assert abs(left_roll + right_roll) < 5.0
    assert payload["validation"]["left_hand"]["maximum_fk_straightness_error_degrees"] < 0.01
    assert payload["validation"]["right_hand"]["maximum_fk_straightness_error_degrees"] < 0.01


def test_neutral_pose_requires_exact_canonical_keys_and_values():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    payload["left_hand"]["unexpected_1"] = payload["left_hand"].pop("thumb_1")

    with pytest.raises(ValueError, match="exact 15-key canonical map"):
        validate_neutral_hand_pose(payload)


def test_matching_cached_neutral_pose_rejects_duplicate_canonical_mapping(tmp_path):
    output = tmp_path / "neutral-hand.json"
    ensure_neutral_hand_pose(
        "config/avatar_profile.json", "config/avatar_bone_map.json", output
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["left_hand"]["thumb_1"]["canonical_bone"] = "LeftIndex1"
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must map exactly to LeftThumb1"):
        ensure_neutral_hand_pose(
            "config/avatar_profile.json", "config/avatar_bone_map.json", output
        )


def test_neutral_pose_rejects_duplicate_avatar_mapping():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    payload["left_hand"]["thumb_1"]["avatar_bone"] = payload["left_hand"]["index_1"][
        "avatar_bone"
    ]

    with pytest.raises(ValueError, match="duplicate avatar bone mappings"):
        validate_neutral_hand_pose(payload)


def test_neutral_pose_changes_only_selected_side_and_frame():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    neutral = neutral_rotations_by_canonical(payload)
    names = list(neutral)
    rotations = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (3, len(names), 1))
    original = rotations.copy()
    applied = apply_neutral_pose(rotations, names, neutral, np.array([True, False, False]), np.array([False, False, True]))
    assert applied == 30
    assert np.array_equal(rotations[1], original[1])
    assert np.array_equal(rotations[0, [i for i, name in enumerate(names) if name.startswith("Right")]], original[0, [i for i, name in enumerate(names) if name.startswith("Right")]])


def test_observed_opening_is_preserved_and_short_tail_does_not_snap_to_neutral():
    rotations = np.tile([1.0, 0.0, 0.0, 0.0], (10, 2, 1))
    hands = np.zeros((10, 21, 3))
    hands[-1] = np.nan
    report = apply_boundary_neutral_pose(
        rotations, ["LeftHand", "LeftIndex1"], {"LeftIndex1": np.array([1., 0., 0., 0.])},
        {"LeftHand": np.array([1., 0., 0., 0.])}, hands, hands,
        wrist_transition_frames=6, finger_transition_frames=6,
    )
    assert not report["finger_weights"][:-1].any()
    np.testing.assert_allclose(report["finger_weights"][-1], 1 / 6)
    np.testing.assert_allclose(report["wrist_weights"][-1], 1 / 6)


def test_untracked_opening_gets_neutral_pose_then_returns_exactly_to_tracking():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    fingers = neutral_rotations_by_canonical(payload)
    wrists = neutral_wrist_rotations_by_canonical(payload)
    names = ["LeftHand", "RightHand", *fingers]
    rotations = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (30, len(names), 1))
    tracked = np.full((30, 21, 3), np.nan)
    tracked[9:13] = 0.1
    tracked[14:20] = 0.1
    result = apply_boundary_neutral_pose(rotations, names, fingers, wrists, tracked, tracked)
    assert np.allclose(rotations[0, names.index("LeftHand")], wrists["LeftHand"])
    assert np.allclose(rotations[0, names.index("RightHand")], wrists["RightHand"])
    assert np.allclose(rotations[12], np.tile([1.0, 0.0, 0.0, 0.0], (len(names), 1)))
    assert result["details"]["left"]["first_valid_hand_frame"] == 10
    assert result["details"]["left"]["first_stable_hand_frame"] == 10
    assert np.allclose(rotations[-1, names.index("LeftHand")], wrists["LeftHand"])


def test_short_untracked_opening_starts_fully_neutral():
    payload = generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")
    fingers = neutral_rotations_by_canonical(payload)
    wrists = neutral_wrist_rotations_by_canonical(payload)
    names = ["LeftHand", "RightHand", *fingers]
    rotations = np.tile([1.0, 0.0, 0.0, 0.0], (12, len(names), 1))
    raw = np.full((12, 21, 3), np.nan)
    raw[5:] = 0.1
    result = apply_boundary_neutral_pose(rotations, names, fingers, wrists, raw, raw)
    np.testing.assert_array_equal(result["wrist_weights"][0], [1.0, 1.0])
    np.testing.assert_array_equal(result["wrist_weights"][5:], 0.0)
