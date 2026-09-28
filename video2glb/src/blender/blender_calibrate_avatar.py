from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import bpy


REQUIRED_CANONICAL_BONES = [
    "Hips",
    "Spine",
    "Chest",
    "Neck",
    "Head",
    "LeftShoulder",
    "LeftUpperArm",
    "LeftForeArm",
    "LeftHand",
    "RightShoulder",
    "RightUpperArm",
    "RightForeArm",
    "RightHand",
    "LeftThumb1",
    "LeftThumb2",
    "LeftThumb3",
    "LeftIndex1",
    "LeftIndex2",
    "LeftIndex3",
    "LeftMiddle1",
    "LeftMiddle2",
    "LeftMiddle3",
    "LeftRing1",
    "LeftRing2",
    "LeftRing3",
    "LeftLittle1",
    "LeftLittle2",
    "LeftLittle3",
    "RightThumb1",
    "RightThumb2",
    "RightThumb3",
    "RightIndex1",
    "RightIndex2",
    "RightIndex3",
    "RightMiddle1",
    "RightMiddle2",
    "RightMiddle3",
    "RightRing1",
    "RightRing2",
    "RightRing3",
    "RightLittle1",
    "RightLittle2",
    "RightLittle3",
]


def main() -> int:
    args = parse_args()
    reset_scene()
    bpy.ops.import_scene.fbx(filepath=args.avatar)
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not armatures:
        raise RuntimeError("No armature found after importing FBX.")
    armature = armatures[0]

    profile = build_profile(armature)
    bone_map, missing = suggest_bone_map(profile["bones"])
    profile["status"] = "PASS" if not missing else "REVIEW"
    profile["missing_required_canonical_bones"] = missing

    Path(args.profile).parent.mkdir(parents=True, exist_ok=True)
    Path(args.profile).write_text(json.dumps(profile, indent=2), encoding="utf-8")
    bone_map_payload = {
        "status": "PASS" if not missing else "REVIEW",
        "note": "Automatically suggested from FBX bone names. Review before production use.",
        "required_missing": missing,
        "map": bone_map,
    }
    Path(args.bone_map).parent.mkdir(parents=True, exist_ok=True)
    Path(args.bone_map).write_text(json.dumps(bone_map_payload, indent=2), encoding="utf-8")
    if missing:
        raise RuntimeError(f"Required bones could not be mapped: {missing}")
    return 0


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--avatar", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bone-map", required=True)
    return parser.parse_args(argv)


def reset_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def build_profile(armature) -> dict:
    bpy.context.view_layer.objects.active = armature
    bones = []
    for bone in armature.data.bones:
        bones.append(
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "children": [child.name for child in bone.children],
                "head_local": list(bone.head_local),
                "tail_local": list(bone.tail_local),
                "length": float(bone.length),
                "matrix_local": [[float(v) for v in row] for row in bone.matrix_local],
            }
        )
    meshes = [obj.name for obj in bpy.context.scene.objects if obj.type == "MESH"]
    return {
        "status": "PENDING",
        "armature_name": armature.name,
        "mesh_names": meshes,
        "bone_count": len(bones),
        "bones": bones,
    }


def suggest_bone_map(bones: list[dict]) -> tuple[dict[str, str], list[str]]:
    names = [bone["name"] for bone in bones]
    normalized = {name: normalize(name) for name in names}
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for canonical in REQUIRED_CANONICAL_BONES:
        match = find_match(canonical, names, normalized)
        if match:
            mapping[canonical] = match
        else:
            missing.append(canonical)
    return mapping, missing


def find_match(canonical: str, names: list[str], normalized: dict[str, str]) -> str | None:
    candidates = candidate_tokens(canonical)
    for candidate in candidates:
        for name in names:
            if normalized[name] == candidate:
                return name
    for candidate in candidates:
        for name in names:
            if candidate in normalized[name]:
                return name
    return None


def candidate_tokens(canonical: str) -> list[str]:
    side = ""
    base = canonical
    if canonical.startswith("Left"):
        side = "left"
        base = canonical[4:]
    elif canonical.startswith("Right"):
        side = "right"
        base = canonical[5:]
    base_norm = normalize(base)
    side_tokens = [side] if side else [""]
    if side == "left":
        side_tokens.extend(["l", "mixamorigleft"])
    if side == "right":
        side_tokens.extend(["r", "mixamorigright"])
    base_aliases = aliases(base_norm)
    tokens = []
    for side_token in side_tokens:
        for alias in base_aliases:
            tokens.append(f"{side_token}{alias}" if side_token else alias)
    return list(dict.fromkeys(tokens))


def aliases(base: str) -> list[str]:
    table = {
        "chest": ["chest", "spine2", "spine02", "spine_02"],
        "forearm": ["forearm", "lowerarm", "lower_arm"],
        "upperarm": ["upperarm", "arm"],
        "thumb1": ["thumb1", "handthumb1"],
        "thumb2": ["thumb2", "handthumb2"],
        "thumb3": ["thumb3", "handthumb3"],
        "index1": ["index1", "handindex1"],
        "index2": ["index2", "handindex2"],
        "index3": ["index3", "handindex3"],
        "middle1": ["middle1", "handmiddle1"],
        "middle2": ["middle2", "handmiddle2"],
        "middle3": ["middle3", "handmiddle3"],
        "ring1": ["ring1", "handring1"],
        "ring2": ["ring2", "handring2"],
        "ring3": ["ring3", "handring3"],
        "little1": ["little1", "pinky1", "handpinky1"],
        "little2": ["little2", "pinky2", "handpinky2"],
        "little3": ["little3", "pinky3", "handpinky3"],
    }
    if base in table:
        return [normalize(value) for value in table[base]]
    return [base]


def normalize(value: str) -> str:
    value = value.lower()
    value = value.replace("mixamorig:", "mixamorig")
    return re.sub(r"[^a-z0-9]", "", value)


if __name__ == "__main__":
    raise SystemExit(main())
