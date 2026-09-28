from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_bone_map(path: str | Path) -> dict[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = payload.get("required_missing") or []
    if missing:
        raise ValueError(f"Avatar bone map is incomplete: {missing}")
    mapping = payload.get("map")
    if not isinstance(mapping, dict):
        raise ValueError(f"Avatar bone map has no 'map' object: {path}")
    normalized: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Avatar bone map contains a blank canonical bone name.")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Avatar bone map has no avatar bone for {key!r}.")
        normalized[key] = value
    duplicate_values = _duplicate_values(normalized)
    if duplicate_values:
        raise ValueError(
            "Avatar bone map assigns one avatar bone to multiple canonical bones: "
            f"{duplicate_values}"
        )
    return normalized


def load_avatar_profile(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_avatar_bone_map_topology(
    mapping: dict[str, str], avatar_profile: dict[str, Any]
) -> None:
    """Validate mapped arm/hand chains against an avatar profile hierarchy.

    Descendant checks intentionally allow unmapped twist or metacarpal bones
    between mapped joints while still enforcing side and joint ordering.
    """

    duplicate_values = _duplicate_values(mapping)
    if duplicate_values:
        raise ValueError(
            "Avatar bone map assigns one avatar bone to multiple canonical bones: "
            f"{duplicate_values}"
        )

    raw_bones = avatar_profile.get("bones")
    if not isinstance(raw_bones, list) or not raw_bones:
        raise ValueError("Avatar profile has no bone hierarchy for mapping validation.")

    bones: dict[str, dict[str, Any]] = {}
    for item in raw_bones:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Avatar profile contains an invalid bone record.")
        name = item["name"]
        if name in bones:
            raise ValueError(f"Avatar profile contains duplicate bone name: {name}")
        bones[name] = item

    missing_avatar_bones = sorted(set(mapping.values()) - set(bones))
    if missing_avatar_bones:
        raise ValueError(f"Avatar bone map references bones absent from the profile: {missing_avatar_bones}")

    required = {
        f"{side}{finger}{joint}"
        for side in ("Left", "Right")
        for finger in ("Thumb", "Index", "Middle", "Ring", "Little")
        for joint in (1, 2, 3)
    } | {"LeftHand", "RightHand"}
    missing_canonical = sorted(required - set(mapping))
    if missing_canonical:
        raise ValueError(f"Avatar bone map is missing required hand bones: {missing_canonical}")

    relevant_avatar_bones = {mapping[name] for name in required}
    for avatar_name in relevant_avatar_bones:
        if "parent" not in bones[avatar_name]:
            raise ValueError(
                f"Avatar profile lacks parent hierarchy data for mapped bone: {avatar_name}"
            )

    parents = {name: item.get("parent") for name, item in bones.items()}
    for name, parent in parents.items():
        if parent is not None and parent not in bones:
            raise ValueError(f"Avatar profile bone {name!r} references missing parent {parent!r}.")

    for side in ("Left", "Right"):
        arm_chain = [f"{side}{part}" for part in ("Shoulder", "UpperArm", "ForeArm", "Hand")]
        if all(name in mapping for name in arm_chain):
            _require_mapped_descendant_chain(mapping, parents, arm_chain)
        for finger in ("Thumb", "Index", "Middle", "Ring", "Little"):
            finger_chain = [f"{side}Hand", *(f"{side}{finger}{joint}" for joint in (1, 2, 3))]
            _require_mapped_descendant_chain(mapping, parents, finger_chain)

    left_hand = mapping["LeftHand"]
    right_hand = mapping["RightHand"]
    if _is_descendant(left_hand, right_hand, parents) or _is_descendant(
        right_hand, left_hand, parents
    ):
        raise ValueError("LeftHand and RightHand mappings must be on separate hierarchy branches.")


def _require_mapped_descendant_chain(
    mapping: dict[str, str], parents: dict[str, Any], canonical_chain: list[str]
) -> None:
    for parent_canonical, child_canonical in zip(canonical_chain, canonical_chain[1:]):
        parent_avatar = mapping[parent_canonical]
        child_avatar = mapping[child_canonical]
        if not _is_descendant(child_avatar, parent_avatar, parents):
            raise ValueError(
                "Avatar mapping breaks canonical hierarchy "
                f"{parent_canonical}->{child_canonical}: "
                f"{parent_avatar!r} is not an ancestor of {child_avatar!r}."
            )


def _is_descendant(child: str, ancestor: str, parents: dict[str, Any]) -> bool:
    seen: set[str] = set()
    current: Any = child
    while isinstance(current, str) and current not in seen:
        seen.add(current)
        current = parents.get(current)
        if current == ancestor:
            return True
    return False


def _duplicate_values(mapping: dict[str, str]) -> list[str]:
    return sorted(value for value, count in Counter(mapping.values()).items() if count > 1)
