"""Blender script: BVH → GLB direct export.

This script is executed by Blender in background mode:

    blender --background --python src/blender/blender_bvh_to_glb.py -- \\
        --bvh path/to/input.bvh \\
        --avatar path/to/character.fbx \\
        --bone-map path/to/avatar_bone_map.json \\
        --output path/to/output.glb

It:
1. Imports the avatar FBX.
2. Parses the BVH file (using the bundled bvh_reader module via sys.path).
3. Builds a BVH-to-avatar bone mapping using the canonical map.
4. Creates a Blender action by converting BVH Euler rotations to quaternions.
5. Sets root translation from the BVH root position channels.
6. Bakes the animation and exports a GLB.

All existing retargeting helpers (blender_utils, normalize_export_skin_weights, etc.)
are reused without modification.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Ensure the project root is on the Python path so src.* imports work.
_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector
import numpy as np

from src.blender.blender_utils import (
    export_avatar_glb,
    import_avatar,
    isolate_target_animation,
    purge_orphans,
    reset_scene,
    suspend_mesh_deformation,
    restore_mesh_deformation,
    normalize_export_skin_weights,
)
from src.bvh.bvh_reader import (
    BVHData,
    parse_bvh,
    build_bvh_bone_mapping,
    validate_bvh_skeleton,
)


# ---------------------------------------------------------------------------
# Euler → Quaternion helper (respects BVH channel order)
# ---------------------------------------------------------------------------

_BLENDER_ORDER_MAP: dict[str, str] = {
    "XYZ": "XYZ", "XZY": "XZY",
    "YXZ": "YXZ", "YZX": "YZX",
    "ZXY": "ZXY", "ZYX": "ZYX",
}


def _euler_to_quat(angles_deg: tuple[float, float, float], order: str) -> Quaternion:
    """Convert BVH channel-ordered Euler values to a Blender quaternion."""
    blender_order = _BLENDER_ORDER_MAP.get(order.upper(), "ZXY")
    combined = Matrix.Identity(4)
    for axis, angle_deg in zip(blender_order, angles_deg):
        combined = combined @ Matrix.Rotation(math.radians(angle_deg), 4, axis)
    return combined.to_quaternion()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()

    bvh_path = Path(args.bvh)
    avatar_path = Path(args.avatar)
    bone_map_path = Path(args.bone_map)
    output_path = Path(args.output)

    if not bvh_path.is_file():
        print(f"[ERROR] BVH file not found: {bvh_path}", flush=True)
        return 1
    if not avatar_path.is_file():
        print(f"[ERROR] Avatar FBX not found: {avatar_path}", flush=True)
        return 1
    if not bone_map_path.is_file():
        print(f"[ERROR] Bone map not found: {bone_map_path}", flush=True)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Parse BVH
    # ------------------------------------------------------------------
    print(f"[BVH] Parsing: {bvh_path.name}", flush=True)
    bvh = parse_bvh(bvh_path)
    print(
        f"[BVH] {len(bvh.joints)} joints, {bvh.frame_count} frames @ {bvh.fps:.4f} fps",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 2. Load bone map and validate skeleton compatibility
    # ------------------------------------------------------------------
    raw_map_payload = json.loads(bone_map_path.read_text(encoding="utf-8"))
    canonical_to_avatar: dict[str, str] = raw_map_payload.get("map", {})
    direct_bvh_map: dict[str, str] = raw_map_payload.get("bvh_map", {})
    if not canonical_to_avatar:
        print(f"[ERROR] Bone map has no 'map' entries: {bone_map_path}", flush=True)
        return 1

    issues = validate_bvh_skeleton(bvh, canonical_to_avatar)
    if issues:
        for issue in issues:
            print(f"[WARN] {issue}", flush=True)
        if any("missing canonical joints" in i for i in issues):
            print("[ERROR] BVH skeleton incompatible with avatar — aborting.", flush=True)
            return 1

    bvh_to_avatar = build_bvh_bone_mapping(
        bvh,
        canonical_to_avatar,
        direct_map=direct_bvh_map,
    )
    if not bvh_to_avatar:
        print("[ERROR] No BVH joints could be mapped to avatar bones.", flush=True)
        return 1
    print(f"[BVH] Mapped {len(bvh_to_avatar)} BVH joints to avatar bones.", flush=True)

    # ------------------------------------------------------------------
    # 3. Import avatar into Blender
    # ------------------------------------------------------------------
    reset_scene()
    purge_orphans()
    armature = import_avatar(str(avatar_path))
    print(f"[BVH] Armature: {armature.name}", flush=True)
    skin_weight_report = normalize_export_skin_weights(armature)
    mesh_states = suspend_mesh_deformation(armature)

    # ------------------------------------------------------------------
    # 4. Validate avatar bones exist in the armature
    # ------------------------------------------------------------------
    avatar_pose_bones = set(armature.pose.bones.keys())
    missing_in_armature = [
        (bvh_name, av_name)
        for bvh_name, av_name in bvh_to_avatar.items()
        if av_name not in avatar_pose_bones
    ]
    if missing_in_armature:
        print(
            f"[WARN] These mapped avatar bones are absent from the armature: "
            f"{[av for _, av in missing_in_armature]}",
            flush=True,
        )
        # Remove invalid mappings rather than aborting
        for bvh_name, _ in missing_in_armature:
            bvh_to_avatar.pop(bvh_name, None)

    # ------------------------------------------------------------------
    # 5. Set scene FPS
    # ------------------------------------------------------------------
    fps = bvh.fps
    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(fps)))
    scene.render.fps_base = scene.render.fps / max(fps, 1e-8)
    scene.frame_start = 0
    scene.frame_end = max(0, bvh.frame_count - 1)
    print(f"[BVH] Scene FPS set to {scene.render.fps} (exact: {fps:.6f})", flush=True)

    # ------------------------------------------------------------------
    # 6. Build Blender action from BVH channels
    # ------------------------------------------------------------------
    action_name = bvh_path.stem
    action = bpy.data.actions.new(action_name)
    armature.animation_data_create()
    armature.animation_data.action = action

    # Index BVH joints by name for fast lookup. Target bone matrices carry
    # arbitrary roll/rest axes, so raw BVH Euler values cannot be assigned to
    # rotation_quaternion directly. Conjugating each BVH local rotation by the
    # target bone's global rest basis expresses the same rotation in the
    # target bone's local pose coordinates.
    bvh_joint_index: dict[str, int] = {j.name: i for i, j in enumerate(bvh.joints)}
    target_rest_rotations = {
        target_name: armature.data.bones[target_name].matrix_local.to_3x3().normalized()
        for target_name in bvh_to_avatar.values()
    }
    previous_quaternions: dict[str, Quaternion] = {}

    # Process root translation separately
    root_joint = bvh.joints[0] if bvh.joints else None
    has_root_translation = root_joint is not None and any(
        "position" in ch.lower() for ch in root_joint.channels
    )

    for frame_idx in range(bvh.frame_count):
        blender_frame = frame_idx
        bpy.context.scene.frame_set(blender_frame)

        source_local_rotations: list[Matrix] = []
        for joint in bvh.joints:
            rot_indices = [
                joint.channel_offset + i
                for i, channel in enumerate(joint.channels)
                if "rotation" in channel.lower()
            ]
            rot_channels = [
                channel for channel in joint.channels
                if "rotation" in channel.lower()
            ]
            if len(rot_indices) == 3:
                values = tuple(float(bvh.frames[frame_idx, i]) for i in rot_indices)
                order = "".join(channel[0].upper() for channel in rot_channels)
                local_rotation = _euler_to_quat(values, order).to_matrix()
            else:
                local_rotation = Matrix.Identity(3)
            source_local_rotations.append(local_rotation)

        # Root translation (location keyframe on armature object)
        if has_root_translation and args.apply_root_translation:
            root_trans = bvh.get_root_translation()
            if root_trans is not None:
                tx, ty, tz = root_trans[frame_idx]
                # BVH Y-up → Blender Z-up: remap (X, Y, Z) → (X, Z, -Y)
                # Scale from cm to meters if configured (BVH is typically in cm)
                scale = args.bvh_scale
                armature.location = Vector((tx * scale, tz * scale, -ty * scale))
                armature.keyframe_insert(data_path="location", frame=blender_frame)

        # Per-joint rotations. This direct basis conversion is independent for
        # every bone and avoids stale parent matrices in Blender's depsgraph.
        for bvh_name, avatar_bone_name in bvh_to_avatar.items():
            source_index = bvh_joint_index[bvh_name]
            pose_bone = armature.pose.bones.get(avatar_bone_name)
            if pose_bone is None:
                continue
            rest_rotation = target_rest_rotations[avatar_bone_name]
            basis_rotation = (
                rest_rotation.inverted()
                @ source_local_rotations[source_index]
                @ rest_rotation
            )
            quat = basis_rotation.to_quaternion()
            previous = previous_quaternions.get(avatar_bone_name)
            if previous is not None and previous.dot(quat) < 0.0:
                quat.negate()
            previous_quaternions[avatar_bone_name] = quat.copy()
            pose_bone.rotation_mode = "QUATERNION"
            pose_bone.rotation_quaternion = quat
            pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=blender_frame)

        bpy.context.view_layer.update()

    # ------------------------------------------------------------------
    # 7. Isolate and validate
    # ------------------------------------------------------------------
    bpy.context.view_layer.update()
    isolate_target_animation(armature, action)
    restore_mesh_deformation(mesh_states)

    if not action.fcurves:
        print("[ERROR] No animation curves were written — check bone mapping.", flush=True)
        return 1

    for curve in action.fcurves:
        for keyframe in curve.keyframe_points:
            keyframe.interpolation = "LINEAR"

    print(
        f"[BVH] Action '{action_name}': {len(action.fcurves)} fcurves, "
        f"{bvh.frame_count} frames.",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 8. Export GLB
    # ------------------------------------------------------------------
    print(f"[BVH] Exporting GLB → {output_path}", flush=True)
    export_avatar_glb(str(output_path), armature)

    if not output_path.is_file() or output_path.stat().st_size < 1000:
        print("[ERROR] GLB export produced an empty or missing file.", flush=True)
        return 1

    size_kb = output_path.stat().st_size / 1024
    print(f"[BVH] SUCCESS — {output_path.name} ({size_kb:.1f} KB)", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    # Blender passes its own args before '--'; everything after belongs to our script.
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Convert a BVH motion file to a GLB via Blender retargeting."
    )
    parser.add_argument("--bvh", required=True, help="Input .bvh file path.")
    parser.add_argument("--avatar", required=True, help="Character FBX path.")
    parser.add_argument("--bone-map", required=True, help="avatar_bone_map.json path.")
    parser.add_argument("--output", required=True, help="Output .glb file path.")
    parser.add_argument(
        "--apply-root-translation",
        action="store_true",
        default=False,
        help="Apply BVH root position channels to the armature location.",
    )
    parser.add_argument(
        "--bvh-scale",
        type=float,
        default=0.01,
        help="Scale factor applied to BVH translation values (default: 0.01 = cm→m).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
