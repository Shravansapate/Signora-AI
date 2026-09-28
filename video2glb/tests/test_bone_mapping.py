from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.avatar.bone_mapping import (
    load_avatar_profile,
    load_bone_map,
    validate_avatar_bone_map_topology,
)


def test_current_avatar_mapping_has_unique_values_and_valid_hand_topology():
    mapping = load_bone_map("config/avatar_bone_map.json")
    profile = load_avatar_profile("config/avatar_profile.json")

    validate_avatar_bone_map_topology(mapping, profile)


def test_bone_map_rejects_duplicate_avatar_bone_assignments(tmp_path: Path):
    bone_map_path = tmp_path / "bone-map.json"
    bone_map_path.write_text(
        json.dumps(
            {
                "required_missing": [],
                "map": {"LeftHand": "shared-hand", "RightHand": "shared-hand"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="multiple canonical bones"):
        load_bone_map(bone_map_path)


def test_bone_map_rejects_out_of_order_finger_chain():
    mapping = load_bone_map("config/avatar_bone_map.json")
    profile = deepcopy(load_avatar_profile("config/avatar_profile.json"))
    index_two = mapping["LeftIndex2"]
    hand = mapping["LeftHand"]
    next(item for item in profile["bones"] if item["name"] == index_two)["parent"] = hand

    with pytest.raises(ValueError, match="LeftIndex1->LeftIndex2"):
        validate_avatar_bone_map_topology(mapping, profile)


def test_bone_map_fails_closed_when_profile_lacks_parent_hierarchy_data():
    mapping = load_bone_map("config/avatar_bone_map.json")
    profile = deepcopy(load_avatar_profile("config/avatar_profile.json"))
    mapped_bone = mapping["RightMiddle2"]
    next(item for item in profile["bones"] if item["name"] == mapped_bone).pop("parent")

    with pytest.raises(ValueError, match="lacks parent hierarchy data"):
        validate_avatar_bone_map_topology(mapping, profile)
