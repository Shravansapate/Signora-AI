from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.qc.collision_safety import evaluate_torso_hand_clearance  # noqa: E402
from src.qc.motion_stability import evaluate_direction_continuity, evaluate_quaternion_jitter  # noqa: E402
from src.qc.root_drift import evaluate_root_drift  # noqa: E402
from src.qc.animation_channels import evaluate_required_rotation_channels  # noqa: E402
from src.qc.neutral_shape import evaluate_neutral_finger_shape  # noqa: E402
from src.qc.mesh_clearance import supported_face_projection  # noqa: E402
from src.motion.neutral_hand import anatomical_palm_normal  # noqa: E402
from src.blender.blender_utils import suspend_mesh_deformation, restore_mesh_deformation  # noqa: E402


def main() -> int:
    args = parse_args()
    result = validate(args.glb, args.motion, args.profile, args.bone_map, args.ik_report)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["status"] == "FAIL":
        raise RuntimeError(result["reasons"])
    return 0


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--glb", required=True)
    parser.add_argument("--motion", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bone-map", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--ik-report")
    return parser.parse_args(argv)


def validate(glb: str, motion: str, profile: str, bone_map: str, ik_report: str | None = None) -> dict:
    reasons: list[str] = []
    review_reasons: list[str] = []
    path = Path(glb)
    if not path.exists() or path.stat().st_size <= 0:
        return {
            "status": "FAIL",
            "reasons": ["GLB is missing or empty."],
            "review_reasons": [],
            "source_frame_count": None,
            "blender_version": bpy.app.version_string,
            "full_clip_collision": _unavailable_collision_report(
                "FAIL", "GLB is missing or empty, so collision sampling was not possible.", 0
            ),
        }

    motion_data = np.load(motion)
    source_fps = float(motion_data["fps"])
    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(source_fps)))
    scene.render.fps_base = scene.render.fps / max(source_fps, 1e-8)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.import_scene.gltf(filepath=str(path))
    meshes = [obj.name for obj in bpy.context.scene.objects if obj.type == "MESH"]
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    actions = list(bpy.data.actions)
    expected_bones = [str(name) for name in motion_data["avatar_bone_names"]]

    if not meshes:
        reasons.append("No mesh found after GLB import.")
    if len(armatures) != 1:
        reasons.append(f"Expected exactly one armature, found {len(armatures)}.")
    if len(actions) != 1:
        reasons.append(f"Expected exactly one animation, found {len(actions)}.")
    if armatures:
        armature = armatures[0]
        mesh_states = suspend_mesh_deformation(armature)
        if (
            armature.animation_data is None
            or armature.animation_data.action is None
            or (len(actions) == 1 and armature.animation_data.action != actions[0])
        ):
            reasons.append("The imported armature does not own the single active animation.")
        existing = set(armature.pose.bones.keys())
        missing = [bone for bone in expected_bones if bone not in existing]
        required_arms = [bone for bone in expected_bones if any(token in bone.lower() for token in ["arm", "forearm", "hand"])]
        if missing:
            reasons.append(f"Missing animated bones: {missing[:10]}")
        if not required_arms:
            reasons.append("No required arm/hand bones present in motion map.")
        hand_visibility = _hand_visibility_metrics(armature, motion_data)
        if hand_visibility["status"] == "FAIL":
            reasons.extend(hand_visibility.get("reasons", []))
        elif hand_visibility["status"] == "REVIEW":
            review_reasons.extend(hand_visibility.get("review_reasons", hand_visibility.get("reasons", [])))
        finger_motion = _finger_motion_metrics(armature, motion_data)
        finger_retargeting = _finger_target_metrics(armature, motion_data)
        palm_orientation = _palm_orientation_metrics(armature, motion_data)
        retargeting = _wrist_target_metrics(armature, motion_data, _load_profile(profile))
        initial_hand_pose = _initial_hand_pose_metrics(armature, motion_data)
        ending_hand_pose = _ending_hand_pose_metrics(armature, motion_data)
        full_clip_collision = _full_clip_collision_metrics(armature, motion_data)
        root_drift = _root_drift_metrics(armature, motion_data)
        motion_stability = _motion_stability_metrics(armature, motion_data)
        required_channel_coverage = _required_channel_metrics(armature, motion_data)
        neutral_finger_shape = _neutral_finger_shape_metrics(armature, motion_data)
        restore_mesh_deformation(mesh_states)
        mesh_collision = _mesh_collision_metrics(armature, motion_data)
    else:
        hand_visibility = {"status": "FAIL", "reasons": ["No armature for hand visibility check."]}
        finger_motion = {"status": "FAIL", "reasons": ["No armature for finger-motion validation."]}
        finger_retargeting = {"status": "FAIL", "reasons": ["No armature for finger-target validation."]}
        palm_orientation = {"status": "FAIL", "reasons": ["No armature for palm-orientation validation."]}
        retargeting = {"status": "FAIL", "reasons": ["No armature for wrist-target validation."]}
        initial_hand_pose = {"status": "FAIL", "reasons": ["No armature for initial-hand validation."]}
        ending_hand_pose = {"status": "FAIL", "reasons": ["No armature for ending-hand validation."]}
        full_clip_collision = _unavailable_collision_report(
            "FAIL",
            "No armature was available for full-clip collision sampling.",
            int(motion_data["frame_count"]),
        )
        root_drift = {
            "status": "FAIL",
            "reasons": ["No armature was available for root-drift validation."],
        }
        motion_stability = {
            "status": "FAIL",
            "reasons": ["No armature was available for motion-stability validation."],
        }
        required_channel_coverage = {"status": "FAIL", "reasons": ["No armature for animation channel coverage."]}
        neutral_finger_shape = {"status": "FAIL", "reasons": ["No armature for neutral finger shape."]}
        mesh_collision = {"status": "FAIL", "reasons": ["No armature for mesh collision sampling."]}

    for label, metric in (("Required animation channels", required_channel_coverage), ("Neutral finger shape", neutral_finger_shape), ("Animated mesh collision", mesh_collision)):
        if metric["status"] == "FAIL":
            reasons.extend(f"{label}: {reason}" for reason in metric.get("reasons", []))
        elif metric["status"] != "PASS":
            review_reasons.extend(f"{label}: {reason}" for reason in metric.get("reasons", []))

    if finger_motion["status"] != "PASS":
        if finger_motion["status"] == "FAIL":
            reasons.extend(finger_motion["reasons"])
        else:
            review_reasons.extend(finger_motion["reasons"])
    if finger_retargeting["status"] != "PASS":
        review_reasons.extend(finger_retargeting["reasons"])
    if palm_orientation["status"] == "FAIL":
        reasons.extend(palm_orientation["reasons"])
    elif palm_orientation["status"] == "REVIEW":
        review_reasons.extend(palm_orientation["reasons"])
    if retargeting["status"] != "PASS":
        review_reasons.extend(retargeting["reasons"])
    if initial_hand_pose["status"] == "FAIL":
        reasons.extend(initial_hand_pose["reasons"])
    elif initial_hand_pose["status"] != "PASS":
        review_reasons.extend(initial_hand_pose["reasons"])
    if ending_hand_pose["status"] == "FAIL":
        reasons.extend(ending_hand_pose["reasons"])
    elif ending_hand_pose["status"] != "PASS":
        review_reasons.extend(ending_hand_pose["reasons"])
    if full_clip_collision["status"] == "FAIL":
        reasons.extend(
            f"Full-clip collision proxy: {reason}"
            for reason in full_clip_collision.get("reasons", [])
        )
    elif full_clip_collision["status"] == "REVIEW":
        review_reasons.extend(
            f"Full-clip collision proxy: {reason}"
            for reason in full_clip_collision.get("reasons", [])
        )
    if root_drift["status"] != "PASS":
        reasons.extend(f"Root drift: {reason}" for reason in root_drift.get("reasons", []))
    if motion_stability["status"] == "FAIL":
        reasons.extend(
            f"Motion stability: {reason}" for reason in motion_stability.get("reasons", [])
        )
    elif motion_stability["status"] == "REVIEW":
        review_reasons.extend(f"Motion stability: {reason}" for reason in motion_stability.get("reasons", []))

    rotations = motion_data["rotations"]
    ik_evidence = json.loads(Path(ik_report).read_text(encoding="utf-8")) if ik_report else {}
    mesh_correction = ik_evidence.get("mesh_contact_correction")
    finger_correction = ik_evidence.get("finger_continuity_correction")
    finger_direction_conditioning = (
        json.loads(str(motion_data["finger_direction_conditioning_json"]))
        if "finger_direction_conditioning_json" in motion_data.files else None
    )
    if isinstance(finger_direction_conditioning, dict) and finger_direction_conditioning.get("status") == "REVIEW":
        review_reasons.append("Isolated source finger-direction outliers were corrected; compare affected frames with the source.")
    arm_conditioning = ik_evidence.get("arm_temporal_conditioning")
    if isinstance(arm_conditioning, dict) and arm_conditioning.get("status") != "PASS":
        review_reasons.extend(arm_conditioning.get("reasons", []))
    if isinstance(finger_correction, dict) and finger_correction.get("status") != "PASS":
        review_reasons.extend(finger_correction.get("reasons", []))
    if isinstance(mesh_correction, dict) and mesh_correction.get("status") != "PASS":
        review_reasons.extend(mesh_correction.get("reasons", []))
    ik_diagnostics = json.loads(str(motion_data["ik_diagnostics_json"])) if "ik_diagnostics_json" in motion_data.files else {}
    source_depth = ik_diagnostics.get("source_depth", {"status": "REVIEW", "reasons": ["No source-depth retargeting evidence is available."]})
    if source_depth.get("status") == "FAIL":
        reasons.extend(source_depth.get("reasons", []))
    elif source_depth.get("status") != "PASS":
        review_reasons.extend(source_depth.get("reasons", []))
    if not np.isfinite(rotations).all():
        reasons.append("Motion file contains NaN or Infinity.")
    if np.max(np.abs(np.linalg.norm(rotations, axis=2) - 1.0)) > 1e-3:
        reasons.append("Motion quaternions are not unit length.")

    frame_count = int(motion_data["frame_count"])
    expected_duration = (frame_count - 1) / source_fps if source_fps else 0.0
    actual_duration = 0.0
    imported_timeline_fps = float(bpy.context.scene.render.fps) / max(float(bpy.context.scene.render.fps_base), 1e-8)
    if actions:
        start, end = actions[0].frame_range
        # glTF stores keyframe times in seconds. On import Blender maps those
        # seconds onto the current scene timeline (normally 24 FPS), which is
        # independent from the source video's FPS. Dividing by source_fps here
        # falsely rejects every valid 25/29.97-FPS GLB as too short.
        actual_duration = (end - start) / imported_timeline_fps if imported_timeline_fps else 0.0
        if abs(actual_duration - expected_duration) > max(1.0 / source_fps, 0.05):
            reasons.append("Animation duration does not match motion duration.")

    status = "FAIL" if reasons else ("REVIEW" if review_reasons else "PASS")
    return {
        "status": status,
        "reasons": reasons,
        "review_reasons": review_reasons,
        "mesh_count": len(meshes),
        "armature_count": len(armatures),
        "animation_count": len(actions),
        "expected_duration_seconds": expected_duration,
        "actual_duration_seconds": actual_duration,
        "source_fps": source_fps,
        "source_frame_count": frame_count,
        "imported_timeline_fps": imported_timeline_fps,
        "blender_version": bpy.app.version_string,
        "file_size": path.stat().st_size,
        "hand_visibility": hand_visibility,
        "finger_motion": finger_motion,
        "finger_retargeting": finger_retargeting,
        "palm_orientation": palm_orientation,
        "retargeting": retargeting,
        "initial_hand_pose": initial_hand_pose,
        "ending_hand_pose": ending_hand_pose,
        "full_clip_collision": full_clip_collision,
        "root_drift": root_drift,
        "motion_stability": motion_stability,
        "required_channel_coverage": required_channel_coverage,
        "neutral_finger_shape": neutral_finger_shape,
        "mesh_collision": mesh_collision,
        "mesh_contact_correction": mesh_correction,
        "finger_continuity_correction": finger_correction,
        "finger_direction_conditioning": finger_direction_conditioning,
        "arm_temporal_conditioning": arm_conditioning,
        "skin_weight_preparation": ik_evidence.get("skin_weight_preparation"),
        "source_depth": source_depth,
        "arm_pole_conditioning": ik_diagnostics.get("arm_pole_conditioning"),
    }


def _load_profile(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(bone["name"]): bone for bone in payload.get("bones", [])}


def _required_channel_metrics(armature, motion_data) -> dict:
    names = [str(name) for name in motion_data["bone_names"]]
    mapped = dict(zip(names, [str(name) for name in motion_data["avatar_bone_names"]]))
    action = armature.animation_data.action if armature.animation_data else None
    animated: set[str] = set()
    if action:
        paths = {curve.data_path for curve in action.fcurves}
        animated = {canonical for canonical, avatar in mapped.items() if avatar in armature.pose.bones and armature.pose.bones[avatar].path_from_id("rotation_quaternion") in paths}
    procedural: set[str] = set()
    if "ik_targets" in motion_data.files:
        targets = np.asarray(motion_data["ik_targets"], dtype=np.float64)
        for side_index, side in enumerate(("Left", "Right")):
            samples = targets[:, side_index * 2:side_index * 2 + 2]
            if len(samples) > 1 and np.max(np.abs(samples - samples[:1])) > 1e-5:
                procedural.update((f"{side}UpperArm", f"{side}ForeArm"))
    for field, valid_field, suffixes in (
        ("palm_basis", "palm_basis_influence", ("Hand",)),
        ("finger_directions", "finger_direction_influence", tuple(f"{finger}{joint}" for finger in ("Thumb", "Index", "Middle", "Ring", "Little") for joint in (1, 2, 3))),
    ):
        if field not in motion_data.files:
            continue
        samples = np.asarray(motion_data[field], dtype=np.float64)
        influences = np.asarray(motion_data[valid_field], dtype=np.float64) if valid_field in motion_data.files else None
        for side_index, side in enumerate(("Left", "Right")):
            side_values = samples[:, side_index].reshape(len(samples), len(suffixes), -1)
            for index, suffix in enumerate(suffixes):
                finite = side_values[:, index][np.isfinite(side_values[:, index]).all(axis=1)]
                varying = len(finite) > 1 and float(np.max(np.abs(finite - finite[:1]))) > 1e-5
                if influences is not None:
                    active = influences[:, side_index].reshape(len(samples), len(suffixes))[:, index]
                    varying = varying or (len(active) > 1 and float(np.max(active) - np.min(active)) > 1e-5)
                if varying:
                    procedural.add(f"{side}{suffix}")
    return evaluate_required_rotation_channels(np.asarray(motion_data["rotations"]).copy(), names, animated, procedural)


def _neutral_finger_shape_metrics(armature, motion_data) -> dict:
    if "neutral_finger_weights" not in motion_data.files:
        return {"status": "REVIEW", "reasons": ["No neutral finger activation evidence is available."]}
    count = int(motion_data["frame_count"])
    weights = np.asarray(motion_data["neutral_finger_weights"], dtype=np.float64)
    if weights.shape != (count, 2) or not np.isfinite(weights).all():
        return {"status": "FAIL", "reasons": ["Neutral finger activation does not match the animation timeline."]}
    mapped = dict(zip([str(name) for name in motion_data["bone_names"]], [str(name) for name in motion_data["avatar_bone_names"]]))
    reports = []
    for frame, side_index in np.argwhere(weights >= 0.999):
        side = ("Left", "Right")[int(side_index)]
        bpy.context.scene.frame_set(int(frame) + 1)
        directions = np.empty((5, 3, 3), dtype=np.float64)
        for finger_index, finger in enumerate(("Thumb", "Index", "Middle", "Ring", "Little")):
            names = [mapped.get(f"{side}{finger}{joint}") for joint in (1, 2, 3)]
            if any(name not in armature.pose.bones for name in names):
                return {"status": "FAIL", "reasons": [f"Missing {side}{finger} geometry for neutral shape validation."]}
            chain = [armature.pose.bones[name] for name in names]
            directions[finger_index] = [tuple(chain[1].head - chain[0].head), tuple(chain[2].head - chain[1].head), tuple(chain[2].tail - chain[2].head)]
        report = evaluate_neutral_finger_shape(directions)
        report.update({"frame": int(frame) + 1, "side": side})
        reports.append(report)
    failing = [r for r in reports if r["status"] == "FAIL"]
    review = [r for r in reports if r["status"] == "REVIEW"]
    worst = max(reports, key=lambda r: r.get("maximum_adjacent_bend_degrees", 180.0), default=None)
    return {
        "status": "FAIL" if failing else ("REVIEW" if review else "PASS"),
        "reasons": list(dict.fromkeys(reason for r in failing + review for reason in r["reasons"])),
        "applicable": bool(reports),
        "evaluated_all_fully_neutral_frames": True,
        "sample_count": len(reports),
        "worst_sample": worst,
        "fail_frame_indices": sorted({r["frame"] for r in failing}),
        "review_frame_indices": sorted({r["frame"] for r in review}),
    }


def _mesh_collision_metrics(armature, motion_data) -> dict:
    """Sample the evaluated hand surface against skin-weight-selected torso triangles.

    Open selection borders and inward-facing surfaces are never used as proof
    of penetration.  Unsupported near-surface samples require review.  Contact
    is allowed; this diagnostic does not push hands away from intentional signs.
    """
    count = int(motion_data["frame_count"])
    mapped = dict(zip([str(name) for name in motion_data["bone_names"]], [str(name) for name in motion_data["avatar_bone_names"]]))
    torso_names = {mapped[name] for name in ("Hips", "Spine", "Chest") if name in mapped}
    hand_names = {mapped[name] for name in mapped if name.endswith("Hand") or any(finger in name for finger in ("Thumb", "Index", "Middle", "Ring", "Little"))}
    limb_names = {mapped[name] for name in mapped if any(part in name for part in ("Shoulder", "UpperArm", "ForeArm", "Hand", "Thumb", "Index", "Middle", "Ring", "Little"))}
    if any(mapped.get(name) not in armature.pose.bones for name in ("Hips", "LeftUpperArm", "RightUpperArm")):
        return {"status": "FAIL", "reasons": ["Torso landmarks are missing for mesh normalization."], "mesh_aware": True}
    selections = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or not any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers):
            continue
        groups = {group.index: group.name for group in obj.vertex_groups}
        torso_vertices, hand_vertices = set(), []
        for vertex in obj.data.vertices:
            weights = {groups.get(group.group): float(group.weight) for group in vertex.groups}
            if sum(value for name, value in weights.items() if name in torso_names) >= 0.65 and sum(value for name, value in weights.items() if name in limb_names) <= 0.10:
                torso_vertices.add(vertex.index)
            if sum(value for name, value in weights.items() if name in hand_names) >= 0.65:
                hand_vertices.append(vertex.index)
        obj.data.calc_loop_triangles()
        triangles = [tuple(triangle.vertices) for triangle in obj.data.loop_triangles if all(index in torso_vertices for index in triangle.vertices)]
        if triangles or hand_vertices:
            # BVH only needs torso vertices; retain source indices for sampling
            # the evaluated mesh and use compact local triangle indices.
            source_indices = np.unique(np.asarray(triangles, dtype=np.int64).reshape(-1))
            index_map = {int(value): index for index, value in enumerate(source_indices)}
            triangles = [tuple(index_map[index] for index in triangle) for triangle in triangles]
            edge_counts: dict[tuple[int, int], int] = {}
            for triangle in triangles:
                for a, b in zip(triangle, triangle[1:] + triangle[:1]):
                    edge = tuple(sorted((a, b)))
                    edge_counts[edge] = edge_counts.get(edge, 0) + 1
            selections.append({"object": obj, "triangles": triangles, "source_indices": source_indices,
                               "hands": hand_vertices, "borders": {edge for edge, number in edge_counts.items() if number == 1}})
    if not any(item["triangles"] for item in selections) or not any(item["hands"] for item in selections):
        return {"status": "REVIEW", "reasons": ["Skin weights do not identify both torso surfaces and hand vertices."], "mesh_aware": True, "evaluated_every_input_frame": False}
    failed_frames, review_frames = [], []
    minimum = None
    evaluated = 0
    tested_points = 0
    unsupported_points = 0
    for frame in range(1, count + 1):
        bpy.context.scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hip = armature.matrix_world @ armature.pose.bones[mapped["Hips"]].head
        left = armature.matrix_world @ armature.pose.bones[mapped["LeftUpperArm"]].head
        right = armature.matrix_world @ armature.pose.bones[mapped["RightUpperArm"]].head
        width = float((left - right).length)
        up = (left + right) * 0.5 - hip
        if width < 1e-8 or up.length < 1e-8:
            return {"status": "FAIL", "reasons": ["Degenerate animated torso axes during mesh sampling."], "mesh_aware": True}
        up.normalize()
        surfaces, points = [], []
        for item in selections:
            obj = item["object"].evaluated_get(depsgraph)
            mesh = obj.to_mesh()
            try:
                if len(mesh.vertices) != len(item["object"].data.vertices):
                    return {"status": "REVIEW", "reasons": ["An evaluated mesh changes vertex topology; collision selections cannot be transferred safely."], "mesh_aware": True, "evaluated_every_input_frame": False}
                coordinates = np.empty((len(mesh.vertices), 3), dtype=np.float32)
                mesh.vertices.foreach_get("co", coordinates.reshape(-1))
                transform = np.asarray(obj.matrix_world, dtype=np.float64)
                if item["hands"]:
                    points.append(coordinates[item["hands"]] @ transform[:3, :3].T + transform[:3, 3])
                if item["triangles"]:
                    used = coordinates[item["source_indices"]] @ transform[:3, :3].T + transform[:3, 3]
                    vertices = [Vector(value) for value in used]
                    surfaces.append((BVHTree.FromPolygons(vertices, item["triangles"], all_triangles=True), vertices, item, np.min(used, axis=0), np.max(used, axis=0)))
            finally:
                obj.to_mesh_clear()
        frame_min = None
        uncertain = False
        all_points = np.concatenate(points) if points else np.empty((0, 3))
        for tree, vertices, item, lower, upper in surfaces:
            nearby = ((all_points >= lower - width * 0.01) & (all_points <= upper + width * 0.01)).all(axis=1)
            for raw_point in all_points[nearby]:
                point = Vector(raw_point)
                location, normal, face_index, distance = tree.find_nearest(point)
                if location is None:
                    uncertain = True
                    continue
                radial = location - hip - up * (location - hip).dot(up)
                if radial.length < 1e-8 or normal.dot(radial.normalized()) < 0.2:
                    unsupported_points += 1
                    uncertain = True
                    continue
                triangle = item["triangles"][face_index]
                on_border = False
                for a, b in zip(triangle, triangle[1:] + triangle[:1]):
                    if tuple(sorted((a, b))) not in item["borders"]:
                        continue
                    edge = vertices[b] - vertices[a]
                    t = min(1.0, max(0.0, (location - vertices[a]).dot(edge) / max(edge.length_squared, 1e-12)))
                    if (location - vertices[a] - edge * t).length < width * 1e-4:
                        on_border = True
                if on_border:
                    unsupported_points += 1
                    uncertain = True
                    continue
                if not supported_face_projection(point, location, normal, width):
                    unsupported_points += 1
                    uncertain = True
                    continue
                signed = float((point - location).dot(normal) / width)
                tested_points += 1
                frame_min = signed if frame_min is None else min(frame_min, signed)
        if frame_min is not None:
            minimum = frame_min if minimum is None else min(minimum, frame_min)
        if frame_min is not None and frame_min < -0.025:
            failed_frames.append(frame)
        elif uncertain or (frame_min is not None and frame_min < -0.005):
            review_frames.append(frame)
        evaluated += 1
    reasons = []
    if failed_frames:
        reasons.append(f"Animated hand surface penetrates the torso beyond tolerance in {len(failed_frames)} frame(s).")
    if review_frames:
        reasons.append(f"Shallow penetration or unsupported surface boundaries require review in {len(review_frames)} frame(s).")
    return {
        # In monocular video tracking, depth estimation near the torso has inherent ambiguity.
        # Report penetrating frames for comparison and qualified human review rather than failing the technical gate.
        "status": "REVIEW" if (failed_frames or review_frames) else "PASS",
        "reasons": reasons,
        "mesh_aware": True,
        "check_name": "evaluated_hand_vertices_to_weighted_torso_surface",
        "evaluated_every_input_frame": evaluated == count,
        "source_frame_count": count,
        "evaluated_frames": evaluated,
        "hand_vertex_count": sum(len(item["hands"]) for item in selections),
        "torso_triangle_count": sum(len(item["triangles"]) for item in selections),
        "tested_near_torso_samples": tested_points,
        "unsupported_near_torso_samples": unsupported_points,
        "minimum_signed_clearance_shoulder_widths": minimum,
        "fail_frame_indices": failed_frames,
        "review_frame_indices": review_frames,
        "thresholds": {"review_penetration_shoulder_widths": 0.005, "fail_penetration_shoulder_widths": 0.025},
        "limitations": ["Checks exported hand vertices against selected torso triangles, not continuous triangle-triangle collision or sign-language meaning.", "Intentional surface contact is allowed; uncertain open borders require review."],
    }


def _root_drift_metrics(armature, motion_data) -> dict:
    frame_count = int(motion_data["frame_count"])
    expected = np.asarray(motion_data["root_translation"], dtype=np.float64)
    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    mapped = dict(zip(canonical_names, avatar_names))
    hips_name = mapped.get("Hips")
    if hips_name not in armature.pose.bones:
        return {"status": "FAIL", "reasons": ["Mapped Hips bone is unavailable."]}

    rest_points = [
        np.asarray(armature.matrix_world @ bone.head_local, dtype=np.float64)
        for bone in armature.data.bones
    ]
    if not rest_points:
        return {"status": "FAIL", "reasons": ["Armature has no bones for scale normalization."]}
    rest_array = np.vstack(rest_points)
    normalization_scale = float(np.linalg.norm(np.max(rest_array, axis=0) - np.min(rest_array, axis=0)))

    scene = bpy.context.scene
    original_frame = scene.frame_current
    origins: list[list[float]] = []
    hips: list[list[float]] = []
    try:
        for frame_index in range(frame_count):
            scene.frame_set(frame_index + 1)
            origin = armature.matrix_world.translation
            hip = armature.matrix_world @ armature.pose.bones[hips_name].head
            origins.append([float(origin.x), float(origin.y), float(origin.z)])
            hips.append([float(hip.x), float(hip.y), float(hip.z)])
    finally:
        scene.frame_set(original_frame)
    return evaluate_root_drift(
        expected,
        np.asarray(origins, dtype=np.float64),
        np.asarray(hips, dtype=np.float64),
        normalization_scale,
    )


def _motion_stability_metrics(armature, motion_data) -> dict:
    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    mapped = dict(zip(canonical_names, avatar_names))
    selected = [
        name
        for name in (
            "Hips",
            "Spine",
            "Chest",
            "Neck",
            "Head",
            "LeftShoulder",
            "LeftUpperArm",
            "LeftForeArm",
            "RightShoulder",
            "RightUpperArm",
            "RightForeArm",
        )
        if mapped.get(name) in armature.pose.bones
    ]
    if len(selected) != 11:
        missing = sorted(set((
            "Hips", "Spine", "Chest", "Neck", "Head",
            "LeftShoulder", "LeftUpperArm", "LeftForeArm",
            "RightShoulder", "RightUpperArm", "RightForeArm",
        )) - set(selected))
        return {"status": "FAIL", "reasons": [f"Missing stability channels: {missing}"]}

    frame_count = int(motion_data["frame_count"])
    fps = float(motion_data["fps"])
    scene = bpy.context.scene
    original_frame = scene.frame_current
    hand_channels = [f"{side}Hand" for side in ("Left", "Right")]
    hand_channels += [f"{side}{finger}{joint}" for side in ("Left", "Right")
                      for finger in ("Thumb", "Index", "Middle", "Ring", "Little") for joint in (1, 2, 3)]
    missing_hands = [name for name in hand_channels if mapped.get(name) not in armature.pose.bones]
    if missing_hands:
        return {"status": "FAIL", "reasons": [f"Missing hand stability channels: {missing_hands}"]}
    body_count = len(selected)
    selected += hand_channels
    samples = np.empty((frame_count, len(selected), 4), dtype=np.float64)
    try:
        for frame_index in range(frame_count):
            scene.frame_set(frame_index + 1)
            for channel_index, canonical in enumerate(selected):
                bone = armature.pose.bones[mapped[canonical]]
                matrix = bone.matrix
                if channel_index >= body_count and bone.parent is not None:
                    matrix = bone.parent.matrix.inverted_safe() @ matrix
                quaternion = matrix.to_quaternion()
                samples[frame_index, channel_index] = (
                    float(quaternion.w),
                    float(quaternion.x),
                    float(quaternion.y),
                    float(quaternion.z),
                )
    finally:
        scene.frame_set(original_frame)
    body = evaluate_quaternion_jitter(samples[:, :body_count], selected[:body_count], fps)
    states = np.full((frame_count, len(hand_channels)), "MISSING", dtype="<U16")
    for side in range(2):
        for palm in (True, False):
            influence_key = "palm_basis_influence" if palm else "finger_direction_influence"
            observed_key = "palm_basis_observed" if palm else "finger_direction_valid"
            neutral_key = "neutral_wrist_weights" if palm else "neutral_finger_weights"
            if not all(key in motion_data.files for key in (influence_key, observed_key, neutral_key)):
                continue
            influence = np.asarray(motion_data[influence_key])[:, side].reshape(frame_count, -1)
            observed = np.asarray(motion_data[observed_key], dtype=bool)[:, side].reshape(frame_count, -1)
            neutral = np.asarray(motion_data[neutral_key])[:, side, None]
            labels = np.full(influence.shape, "MISSING", dtype="<U16")
            labels[observed & (influence >= 0.999) & (neutral <= 1e-6)] = "SOURCE_ACTIVE"
            labels[((influence > 0.0) & (influence < 0.999)) | np.broadcast_to((neutral > 0.0) & (neutral < 0.999), influence.shape)] = "TRANSITION"
            labels[np.broadcast_to(neutral >= 0.999, influence.shape)] = "NEUTRAL"
            if palm:
                states[:, side] = labels[:, 0]
            else:
                states[:, 2 + side * 15:2 + (side + 1) * 15] = labels
    hands = evaluate_quaternion_jitter(samples[:, body_count:], hand_channels, fps, sample_states=states)
    # Body and hand acceleration may be intentional in natural sign language:
    # report them for comparison with the source rather than failing the technical gate.
    # Severe unsupported turns are separately checked by continuity QC.
    status = "REVIEW" if (body["status"] != "PASS" or hands["status"] != "PASS") else "PASS"
    return {"status": status, "reasons": body["reasons"] + hands["reasons"],
            "evaluated_every_frame": True, "frame_count": frame_count,
            "channel_count": len(selected), "body": body, "hands_and_fingers": hands}


def _full_clip_collision_metrics(armature, motion_data) -> dict:
    """Sample rig landmarks on every imported animation frame.

    This deliberately remains a landmark-plane proxy.  It evaluates the
    reimported GLB's animated bones rather than trusting source tracking, but it
    does not inspect the skinned mesh triangles or their thickness.
    """

    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    mapped = dict(zip(canonical_names, avatar_names))
    frame_count = int(motion_data["frame_count"])

    # Upper-arm heads are the anatomical shoulder joints.  Imported glTF pose
    # bone tails can be reconstructed at exaggerated lengths, so this validator
    # intentionally samples bone heads only.
    torso_required = ("Hips", "LeftUpperArm", "RightUpperArm")
    hand_required = ("LeftHand", "RightHand")
    missing_structural = [
        name
        for name in torso_required + hand_required
        if mapped.get(name) not in armature.pose.bones
    ]
    if missing_structural:
        return _unavailable_collision_report(
            "FAIL",
            f"Missing mapped torso/hand bones required for collision sampling: {missing_structural}",
            frame_count,
        )

    side_specs: dict[str, list[str]] = {}
    missing_fingers: list[str] = []
    for side in ("Left", "Right"):
        specs: list[str] = [f"{side}Hand"]
        for finger in ("Thumb", "Index", "Middle", "Ring", "Little"):
            for segment in range(1, 4):
                canonical = f"{side}{finger}{segment}"
                if mapped.get(canonical) in armature.pose.bones:
                    specs.append(canonical)
                else:
                    missing_fingers.append(canonical)
        side_specs[side] = specs

    pose_points = np.full((frame_count, 33, 3), np.nan, dtype=np.float64)
    left_points = np.full((frame_count, len(side_specs["Left"]), 3), np.nan, dtype=np.float64)
    right_points = np.full((frame_count, len(side_specs["Right"]), 3), np.nan, dtype=np.float64)
    hips_name = mapped["Hips"]
    left_shoulder_name = mapped["LeftUpperArm"]
    right_shoulder_name = mapped["RightUpperArm"]

    for frame_index in range(frame_count):
        bpy.context.scene.frame_set(frame_index + 1)
        bpy.context.view_layer.update()

        hips = _pose_bone_world_head(armature, hips_name)
        left_shoulder = _pose_bone_world_head(armature, left_shoulder_name)
        right_shoulder = _pose_bone_world_head(armature, right_shoulder_name)
        pose_points[frame_index, 11] = left_shoulder
        pose_points[frame_index, 12] = right_shoulder
        # collision_safety uses the hip midpoint; the mapped Hips bone is the
        # most stable rig proxy, so both virtual hip landmarks share its head.
        pose_points[frame_index, 23] = hips
        pose_points[frame_index, 24] = hips

        for point_index, canonical in enumerate(side_specs["Left"]):
            left_points[frame_index, point_index] = _pose_bone_world_head(
                armature, mapped[canonical]
            )
        for point_index, canonical in enumerate(side_specs["Right"]):
            right_points[frame_index, point_index] = _pose_bone_world_head(
                armature, mapped[canonical]
            )

    report = evaluate_torso_hand_clearance(
        pose_points,
        left_points,
        right_points,
        frame_indices=np.arange(1, frame_count + 1, dtype=np.int64),
    )
    report["sampling_source"] = "reimported_glb_evaluated_pose_bone_heads"
    report["source_frame_count"] = frame_count
    report["blender_frame_start"] = 1
    report["blender_frame_end"] = frame_count
    report["sampled_points_per_side"] = {
        "left": len(side_specs["Left"]),
        "right": len(side_specs["Right"]),
    }
    report["missing_finger_bones"] = missing_fingers

    metric_reasons: list[str] = []
    if report["status"] == "FAIL":
        metric_reasons.append(
            "Deep hand/finger landmark penetration was detected in "
            f"{report['frames']['fail']} frame(s); minimum normalized clearance "
            f"was {report['minimum_normalized_clearance']}."
        )
    elif report["status"] == "REVIEW":
        if report["frames"]["review"]:
            metric_reasons.append(
                "Low torso-front clearance requires visual review in "
                f"{report['frames']['review']} frame(s)."
            )
        if report["frames"]["evaluated_coverage"] < report["thresholds"]["minimum_evaluated_coverage"]:
            metric_reasons.append(
                "Full-clip collision coverage is below the configured minimum."
            )

    if missing_fingers:
        metric_reasons.append(
            f"Collision sampling is incomplete because mapped finger bones are missing: {missing_fingers[:10]}"
        )
        if report["status"] == "PASS":
            report["status"] = "REVIEW"
    report["reasons"] = metric_reasons
    return report


def _pose_bone_world_head(armature, bone_name: str) -> np.ndarray:
    bone = armature.pose.bones[bone_name]
    point = armature.matrix_world @ bone.head
    return np.asarray(point[:], dtype=np.float64)


def _unavailable_collision_report(status: str, reason: str, frame_count: int) -> dict:
    return {
        "schema_version": "1.0",
        "check_name": "full_clip_torso_hand_clearance_proxy",
        "status": status,
        "mesh_aware": False,
        "evaluated_every_input_frame": False,
        "source_frame_count": int(frame_count),
        "frames": {
            "total": int(frame_count),
            "evaluated": 0,
            "evaluated_coverage": 0.0,
        },
        "minimum_normalized_clearance": None,
        "risky_frame_indices": [],
        "review_frame_indices": [],
        "fail_frame_indices": [],
        "reasons": [reason],
        "limitations": [
            "This is an anatomical landmark proxy, not a mesh-aware collision test.",
            "A PASS cannot by itself certify a production GLB as collision free.",
        ],
    }


def _has_structural_motion_error(metric: dict) -> bool:
    """Keep malformed animation data as FAIL; route quality concerns to REVIEW."""
    structural_markers = ("no finger rotation channels", "missing finger bones", "zero-length", "non-finite")
    return any(marker in reason.lower() for reason in metric.get("reasons", []) for marker in structural_markers)


def _initial_hand_pose_metrics(armature, motion_data) -> dict:
    return _boundary_hand_pose_metrics(armature, motion_data, 0, 1, "Initial")


def _ending_hand_pose_metrics(armature, motion_data) -> dict:
    frame_count = int(motion_data["frame_count"])
    return _boundary_hand_pose_metrics(armature, motion_data, -1, frame_count, "Ending")


def _palm_orientation_metrics(armature, motion_data) -> dict:
    """Check tracked palm target continuity and the orientation baked into the GLB."""
    required_arrays = ("palm_basis", "palm_basis_influence")
    missing_arrays = [name for name in required_arrays if name not in motion_data.files]
    if missing_arrays:
        return {
            "status": "REVIEW",
            "reasons": [f"Motion has no complete palm-orientation data: {missing_arrays}"],
            "applicable": False,
        }

    bases = np.asarray(motion_data["palm_basis"], dtype=np.float64)
    influence = np.asarray(motion_data["palm_basis_influence"], dtype=np.float64)
    frame_count = min(int(motion_data["frame_count"]), len(bases), len(influence))
    if bases.ndim != 4 or bases.shape[1:] != (2, 3, 3) or influence.ndim != 2 or influence.shape[1] != 2:
        return {"status": "FAIL", "reasons": ["Palm-orientation arrays have invalid shapes."], "applicable": True}

    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    bone_map = dict(zip(canonical_names, avatar_names))
    required = [
        "LeftHand", "LeftIndex1", "LeftMiddle1", "LeftLittle1",
        "RightHand", "RightIndex1", "RightMiddle1", "RightLittle1",
    ]
    missing_bones = [name for name in required if bone_map.get(name) not in armature.pose.bones]
    if missing_bones:
        return {
            "status": "FAIL",
            "reasons": [f"Cannot validate palm orientation; missing mapped bones: {missing_bones}"],
            "applicable": True,
        }

    errors: list[tuple[float, str, int]] = []
    target_steps: list[tuple[float, str, int]] = []
    actual_steps: list[tuple[float, str, int]] = []
    previous_target: list[np.ndarray | None] = [None, None]
    previous_actual: list[np.ndarray | None] = [None, None]
    invalid_basis_count = 0

    for frame_index in range(frame_count):
        bpy.context.scene.frame_set(frame_index + 1)
        bpy.context.view_layer.update()
        for side_index, side in enumerate(("Left", "Right")):
            if float(influence[frame_index, side_index]) < 0.999:
                previous_target[side_index] = None
                previous_actual[side_index] = None
                continue
            target = bases[frame_index, side_index]
            if not np.isfinite(target).all():
                invalid_basis_count += 1
                previous_target[side_index] = None
                previous_actual[side_index] = None
                continue
            orthonormal_error = float(np.max(np.abs(target.T @ target - np.eye(3))))
            determinant = float(np.linalg.det(target))
            if orthonormal_error > 1e-3 or determinant < 0.999 or determinant > 1.001:
                invalid_basis_count += 1
                previous_target[side_index] = None
                previous_actual[side_index] = None
                continue

            actual = _animated_palm_basis(armature, bone_map, side)
            if actual is None:
                invalid_basis_count += 1
                previous_target[side_index] = None
                previous_actual[side_index] = None
                continue
            errors.append((_rotation_delta_degrees(target, actual), side, frame_index + 1))
            if previous_target[side_index] is not None:
                target_steps.append((_rotation_delta_degrees(previous_target[side_index], target), side, frame_index + 1))
            if previous_actual[side_index] is not None:
                actual_steps.append((_rotation_delta_degrees(previous_actual[side_index], actual), side, frame_index + 1))
            previous_target[side_index] = target
            previous_actual[side_index] = actual

    reasons: list[str] = []
    status = "PASS"
    if invalid_basis_count:
        status = "FAIL"
        reasons.append(f"Palm tracking contains {invalid_basis_count} invalid or non-orthonormal active bases.")
    if not errors or not target_steps or not actual_steps:
        if status != "FAIL":
            status = "REVIEW"
        reasons.append("There were not enough fully active palm samples to validate orientation continuity.")
        return {
            "status": status,
            "reasons": reasons,
            "applicable": True,
            "sample_count": len(errors),
            "invalid_active_basis_count": invalid_basis_count,
        }

    error_values = np.asarray([item[0] for item in errors], dtype=np.float64)
    target_values = np.asarray([item[0] for item in target_steps], dtype=np.float64)
    actual_values = np.asarray([item[0] for item in actual_steps], dtype=np.float64)
    mean_error = float(np.mean(error_values))
    p95_error = float(np.percentile(error_values, 95))
    max_target_step = float(np.max(target_values))
    p95_target_step = float(np.percentile(target_values, 95))
    max_actual_step = float(np.max(actual_values))
    p95_actual_step = float(np.percentile(actual_values, 95))

    if mean_error > 20.0 or p95_error > 35.0:
        status = "FAIL"
        reasons.append("Baked palms deviate substantially from the conditioned source palm orientation.")
    elif (mean_error > 5.0 or p95_error > 10.0) and status != "FAIL":
        status = "REVIEW"
        reasons.append("Baked palm orientation should be visually reviewed against the source.")

    continuity_max = max(max_target_step, max_actual_step)
    continuity_p95 = max(p95_target_step, p95_actual_step)
    if continuity_max > 90.0 or continuity_p95 > 45.0:
        status = "FAIL"
        reasons.append("Palm orientation contains an implausibly large frame-to-frame spike.")
    elif (continuity_max > 45.0 or continuity_p95 > 25.0) and status != "FAIL":
        status = "REVIEW"
        reasons.append("Palm orientation contains a sharp transition that requires visual review.")

    worst_error = max(errors, key=lambda item: item[0])
    worst_target = max(target_steps, key=lambda item: item[0])
    worst_actual = max(actual_steps, key=lambda item: item[0])
    return {
        "status": status,
        "reasons": reasons,
        "applicable": True,
        "sample_count": len(errors),
        "invalid_active_basis_count": invalid_basis_count,
        "mean_baked_target_error_degrees": mean_error,
        "p95_baked_target_error_degrees": p95_error,
        "max_baked_target_error_degrees": float(worst_error[0]),
        "max_baked_target_error_side": worst_error[1],
        "max_baked_target_error_frame": int(worst_error[2]),
        "target_step_sample_count": len(target_steps),
        "max_target_step_degrees": max_target_step,
        "p95_target_step_degrees": p95_target_step,
        "max_target_step_side": worst_target[1],
        "max_target_step_frame": int(worst_target[2]),
        "max_baked_step_degrees": max_actual_step,
        "p95_baked_step_degrees": p95_actual_step,
        "max_baked_step_side": worst_actual[1],
        "max_baked_step_frame": int(worst_actual[2]),
    }


def _animated_palm_basis(armature, bone_map: dict[str, str], side: str) -> np.ndarray | None:
    hand = armature.pose.bones[bone_map[f"{side}Hand"]]
    index = armature.pose.bones[bone_map[f"{side}Index1"]]
    middle = armature.pose.bones[bone_map[f"{side}Middle1"]]
    little = armature.pose.bones[bone_map[f"{side}Little1"]]
    wrist_position = np.asarray(hand.head[:], dtype=np.float64)
    across = np.asarray((index.head - little.head)[:], dtype=np.float64)
    forward = np.asarray(middle.head[:], dtype=np.float64) - wrist_position
    across_length = float(np.linalg.norm(across))
    forward_length = float(np.linalg.norm(forward))
    if across_length < 1e-8 or forward_length < 1e-8:
        return None
    across /= across_length
    forward /= forward_length
    normal = np.cross(across, forward)
    normal_length = float(np.linalg.norm(normal))
    if normal_length < 1e-8:
        return None
    normal /= normal_length
    forward = np.cross(normal, across)
    forward_length = float(np.linalg.norm(forward))
    if forward_length < 1e-8:
        return None
    forward /= forward_length
    return np.stack([across, forward, normal], axis=1)


def _rotation_delta_degrees(first: np.ndarray, second: np.ndarray) -> float:
    relative = np.asarray(first, dtype=np.float64).T @ np.asarray(second, dtype=np.float64)
    cosine = np.clip((float(np.trace(relative)) - 1.0) * 0.5, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosine)))


def _boundary_hand_pose_metrics(armature, motion_data, weight_row: int, blender_frame: int, label: str) -> dict:
    """Verify untracked boundary palms are medial rather than camera-facing."""
    if "neutral_wrist_weights" not in motion_data.files:
        return {"status": "PASS", "reasons": [], "applicable": False}
    weights = np.asarray(motion_data["neutral_wrist_weights"], dtype=float)
    if weights.shape != (int(motion_data["frame_count"]), 2) or not np.isfinite(weights).all():
        return {"status": "FAIL", "reasons": ["Neutral wrist weights do not match the timeline."], "applicable": False}
    if not np.any(weights[weight_row] >= 0.99):
        return {"status": "PASS", "reasons": [], "applicable": False}

    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    bone_map = dict(zip(canonical_names, avatar_names))
    required = [
        "Hips", "LeftHand", "LeftIndex1", "LeftMiddle1", "LeftLittle1",
        "RightHand", "RightIndex1", "RightMiddle1", "RightLittle1",
    ]
    missing = [name for name in required if bone_map.get(name) not in armature.pose.bones]
    if missing:
        return {"status": "FAIL", "reasons": [f"Cannot validate initial hands; missing bones: {missing}"]}

    bpy.context.scene.frame_set(blender_frame)
    bpy.context.view_layer.update()
    body_center = armature.matrix_world @ armature.pose.bones[bone_map["Hips"]].head
    alignments: dict[str, float] = {}
    for side_index, side in enumerate(("Left", "Right")):
        if weights[weight_row, side_index] < 0.99:
            continue
        wrist = armature.matrix_world @ armature.pose.bones[bone_map[f"{side}Hand"]].head
        index = armature.matrix_world @ armature.pose.bones[bone_map[f"{side}Index1"]].head
        middle = armature.matrix_world @ armature.pose.bones[bone_map[f"{side}Middle1"]].head
        little = armature.matrix_world @ armature.pose.bones[bone_map[f"{side}Little1"]].head
        forward = middle - wrist
        across = index - little
        if forward.length < 1e-8 or across.length < 1e-8:
            return {"status": "FAIL", "reasons": [f"{side} initial palm basis is degenerate."]}
        forward.normalize()
        palm_normal = Vector(anatomical_palm_normal(np.asarray(across), np.asarray(forward), side))
        inward = body_center - wrist
        inward = inward - forward * inward.dot(forward)
        if palm_normal.length < 1e-8 or inward.length < 1e-8:
            return {"status": "FAIL", "reasons": [f"{side} initial palm orientation is degenerate."]}
        alignments[side.lower()] = float(palm_normal.normalized().dot(inward.normalized()))

    minimum = min(alignments.values())
    reasons: list[str] = []
    if minimum < 0.0:
        status = "FAIL"
        reasons.append(f"{label} neutral palm faces away from the body.")
    elif minimum < 0.45:
        status = "REVIEW"
        reasons.append(f"{label} neutral palms are not sufficiently oriented toward the thighs.")
    else:
        status = "PASS"
    return {
        "status": status,
        "reasons": reasons,
        "applicable": True,
        "left_medial_alignment": alignments.get("left"),
        "right_medial_alignment": alignments.get("right"),
        "signed_alignment": True,
        "minimum_medial_alignment": minimum,
        "frame": int(blender_frame),
        "neutral_weight": [float(value) for value in weights[weight_row]],
    }


def _wrist_target_metrics(armature, motion_data, profile: dict[str, dict]) -> dict:
    if "ik_targets" not in motion_data.files:
        return {
            "status": "REVIEW",
            "reasons": ["Motion has no wrist targets, so endpoint retargeting could not be checked."],
        }

    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    bone_map = dict(zip(canonical_names, avatar_names))
    required = ["LeftUpperArm", "LeftForeArm", "LeftHand", "RightUpperArm", "RightForeArm", "RightHand"]
    missing = [name for name in required if bone_map.get(name) not in armature.pose.bones or bone_map.get(name) not in profile]
    if missing:
        return {"status": "FAIL", "reasons": [f"Cannot validate wrist targets; missing mapped bones: {missing}"]}

    targets = motion_data["ik_targets"].astype(float)
    frame_count = min(int(motion_data["frame_count"]), len(targets))
    pairs = [
        ("LeftUpperArm", "LeftForeArm", "LeftHand", 0),
        ("RightUpperArm", "RightForeArm", "RightHand", 2),
    ]
    object_scale = float(np.mean(np.abs(armature.matrix_world.to_scale())))
    errors: list[float] = []
    for frame_index in range(frame_count):
        bpy.context.scene.frame_set(frame_index + 1)
        bpy.context.view_layer.update()
        for upper_name, fore_name, hand_name, target_index in pairs:
            target_local = targets[frame_index, target_index]
            if not np.isfinite(target_local).all():
                continue
            upper = bone_map[upper_name]
            fore = bone_map[fore_name]
            hand = bone_map[hand_name]
            chain_length = (float(profile[upper]["length"]) + float(profile[fore]["length"])) * object_scale
            if chain_length <= 1e-6:
                continue
            target_world = armature.matrix_world @ Vector(target_local)
            actual_world = armature.matrix_world @ armature.pose.bones[hand].head
            errors.append(float((actual_world - target_world).length / chain_length))

    if not errors:
        return {"status": "FAIL", "reasons": ["No finite wrist targets were available for retarget validation."]}

    values = np.asarray(errors, dtype=np.float64)
    mean_error = float(np.mean(values))
    p95_error = float(np.percentile(values, 95))
    max_error = float(np.max(values))
    reasons: list[str] = []
    if mean_error > 0.20 or p95_error > 0.35:
        status = "FAIL"
        reasons.append("Avatar wrist paths deviate substantially from the solved source wrist targets.")
    elif mean_error > 0.08 or p95_error > 0.15:
        status = "REVIEW"
        reasons.append("Avatar wrist paths need review because endpoint tracking is not close enough.")
    else:
        status = "PASS"

    return {
        "status": status,
        "reasons": reasons,
        "sample_count": len(errors),
        "mean_normalized_wrist_error": mean_error,
        "p95_normalized_wrist_error": p95_error,
        "max_normalized_wrist_error": max_error,
    }


def _finger_motion_metrics(armature, motion_data) -> dict:
    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    finger_pairs = [
        (canonical, avatar)
        for canonical, avatar in zip(canonical_names, avatar_names)
        if any(finger in canonical for finger in ["Thumb", "Index", "Middle", "Ring", "Little"])
    ]
    if not finger_pairs:
        return {"status": "FAIL", "reasons": ["Motion file has no finger rotation channels."]}
    missing = [canonical for canonical, avatar in finger_pairs if avatar not in armature.pose.bones]
    if missing:
        return {"status": "FAIL", "reasons": [f"GLB is missing finger bones: {missing[:10]}"]}

    if "finger_direction_valid" in motion_data.files:
        return _finger_local_direction_motion_metrics(armature, motion_data, finger_pairs)

    frame_count = int(motion_data["frame_count"])
    directions = np.empty((frame_count, len(finger_pairs), 3), dtype=np.float64)
    for frame in range(frame_count):
        bpy.context.scene.frame_set(frame + 1)
        bpy.context.view_layer.update()
        for index, (_, avatar) in enumerate(finger_pairs):
            bone = armature.pose.bones[avatar]
            direction = (armature.matrix_world @ bone.tail) - (armature.matrix_world @ bone.head)
            if direction.length < 1e-8:
                return {"status": "FAIL", "reasons": [f"Baked GLB has a zero-length finger direction for {avatar}."]}
            directions[frame, index] = direction.normalized()
    if not np.isfinite(directions).all():
        return {"status": "FAIL", "reasons": ["Baked GLB has non-finite finger directions."]}
    if len(directions) < 2:
        return {"status": "REVIEW", "reasons": ["Motion is too short to measure finger angular continuity."]}
    dots = np.sum(directions[1:] * directions[:-1], axis=2)
    deltas = np.degrees(np.arccos(np.clip(dots, -1.0, 1.0)))
    max_delta = float(np.max(deltas))
    p99_delta = float(np.percentile(deltas, 99))
    max_frame_index, max_bone_index = np.unravel_index(int(np.argmax(deltas)), deltas.shape)
    reasons: list[str] = []
    if max_delta > 60.0 or p99_delta > 40.0:
        status = "FAIL"
        reasons.append("Visible finger direction contains an implausibly large frame-to-frame spike.")
    elif max_delta > 35.0 or p99_delta > 25.0:
        status = "REVIEW"
        reasons.append("Visible finger direction has a sharp transition that should be reviewed.")
    else:
        status = "PASS"
    return {
        "status": status,
        "reasons": reasons,
        "finger_channel_count": len(finger_pairs),
        "max_visible_direction_delta_degrees": max_delta,
        "p99_visible_direction_delta_degrees": p99_delta,
        "max_delta_frame": int(max_frame_index + 2),
        "max_delta_bone": finger_pairs[int(max_bone_index)][0],
    }


def _finger_local_direction_motion_metrics(armature, motion_data, finger_pairs: list[tuple[str, str]]) -> dict:
    """Check all visible finger transitions, including neutral/tracking fades."""
    fingers = ("Thumb", "Index", "Middle", "Ring", "Little")
    pair_index = {canonical: index for index, (canonical, _) in enumerate(finger_pairs)}
    avatar_map = dict(finger_pairs)
    full_bone_map = dict(zip([str(name) for name in motion_data["bone_names"]], [str(name) for name in motion_data["avatar_bone_names"]]))
    valid = motion_data["finger_direction_valid"].astype(bool)
    influence = motion_data["finger_direction_influence"].astype(float) if "finger_direction_influence" in motion_data.files else valid.astype(float)
    frame_count = int(motion_data["frame_count"])
    if valid.shape != (frame_count, 2, 5, 3) or influence.shape != valid.shape:
        return {"status": "FAIL", "reasons": ["Finger influence/validity arrays do not match the complete timeline."]}
    actual = np.full((frame_count, len(finger_pairs), 3), np.nan)
    source = np.full_like(actual, np.nan)
    supported = np.zeros(actual.shape[:2], dtype=bool)
    source_directions = np.asarray(motion_data["finger_directions"], dtype=np.float64)

    for frame in range(frame_count):
        bpy.context.scene.frame_set(frame + 1)
        bpy.context.view_layer.update()
        for side_index, side in enumerate(("Left", "Right")):
            hand_name = full_bone_map.get(f"{side}Hand", "")
            if hand_name not in armature.pose.bones:
                continue
            hand_inverse = (armature.matrix_world @ armature.pose.bones[hand_name].matrix).to_3x3().inverted()
            axes = _hand_rest_palm_axes_local(armature, hand_name, full_bone_map, side)
            if axes is None:
                return {"status": "FAIL", "reasons": [f"Missing {side} palm basis for finger continuity."]}
            for finger_index, finger in enumerate(fingers):
                for segment_index in range(3):
                    canonical = f"{side}{finger}{segment_index + 1}"
                    if canonical not in pair_index:
                        continue
                    index = pair_index[canonical]
                    pose_bone = armature.pose.bones[avatar_map[canonical]]
                    world_direction = (armature.matrix_world @ pose_bone.tail) - (armature.matrix_world @ pose_bone.head)
                    local_direction = hand_inverse @ world_direction
                    if local_direction.length < 1e-8:
                        continue
                    local_direction.normalize()
                    actual[frame, index] = tuple(local_direction)
                    components = source_directions[frame, side_index, finger_index, segment_index]
                    source[frame, index] = tuple(sum((axes[axis] * float(components[axis]) for axis in range(3)), Vector((0, 0, 0))))
                    supported[frame, index] = bool(valid[frame, side_index, finger_index, segment_index]) and float(influence[frame, side_index, finger_index, segment_index]) >= 0.999

    return evaluate_direction_continuity(
        actual, [canonical for canonical, _ in finger_pairs], float(motion_data["fps"]),
        source_directions=source, source_valid=supported,
    )


def _finger_target_metrics(armature, motion_data) -> dict:
    if "finger_directions" not in motion_data.files or "finger_direction_valid" not in motion_data.files:
        return {
            "status": "REVIEW",
            "reasons": ["Motion has no local hand-world finger directions for source-to-avatar validation."],
        }

    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    bone_map = dict(zip(canonical_names, avatar_names))
    fingers = ("Thumb", "Index", "Middle", "Ring", "Little")
    directions = motion_data["finger_directions"].astype(np.float64)
    valid = motion_data["finger_direction_valid"].astype(bool)
    observed_count = int(valid.sum())
    if "finger_direction_influence" not in motion_data.files or "neutral_finger_weights" not in motion_data.files:
        return {"status": "FAIL", "reasons": ["Finger target accuracy requires tracking influence and neutral activation evidence."]}
    influence = np.asarray(motion_data["finger_direction_influence"], dtype=float)
    neutral = np.asarray(motion_data["neutral_finger_weights"], dtype=float)
    if influence.shape != valid.shape or neutral.shape != valid.shape[:2] or not np.isfinite(influence).all() or not np.isfinite(neutral).all():
        return {"status": "FAIL", "reasons": ["Finger accuracy masks do not match the source timeline."]}
    valid &= (influence >= 0.999) & (neutral[:, :, None, None] <= 1e-6)
    errors: list[float] = []

    for side_index, side in enumerate(("Left", "Right")):
        hand_name = bone_map.get(f"{side}Hand", "")
        if hand_name not in armature.pose.bones:
            return {"status": "FAIL", "reasons": [f"Missing mapped {side} hand for finger-target validation."]}
        axes_local = _hand_rest_palm_axes_local(armature, hand_name, bone_map, side)
        if axes_local is None:
            return {"status": "FAIL", "reasons": [f"Could not derive a rest palm basis for {side}."]}
        for frame in range(min(int(motion_data["frame_count"]), len(directions))):
            bpy.context.scene.frame_set(frame + 1)
            bpy.context.view_layer.update()
            hand_world = armature.matrix_world @ armature.pose.bones[hand_name].matrix
            for finger_index, finger in enumerate(fingers):
                for segment_index in range(3):
                    if not valid[frame, side_index, finger_index, segment_index]:
                        continue
                    bone_name = bone_map.get(f"{side}{finger}{segment_index + 1}", "")
                    if bone_name not in armature.pose.bones:
                        continue
                    components = directions[frame, side_index, finger_index, segment_index]
                    expected_local = sum((axes_local[axis] * float(components[axis]) for axis in range(3)), Vector((0.0, 0.0, 0.0)))
                    expected_world = hand_world.to_3x3() @ expected_local
                    pose_bone = armature.pose.bones[bone_name]
                    actual_world = (armature.matrix_world @ pose_bone.tail) - (armature.matrix_world @ pose_bone.head)
                    if expected_world.length < 1e-8 or actual_world.length < 1e-8:
                        continue
                    dot = max(-1.0, min(1.0, expected_world.normalized().dot(actual_world.normalized())))
                    errors.append(float(np.degrees(np.arccos(dot))))

    if not errors:
        return {"status": "FAIL", "reasons": ["No valid finger directions were available for source-to-avatar validation."]}
    values = np.asarray(errors, dtype=np.float64)
    mean_error = float(np.mean(values))
    p95_error = float(np.percentile(values, 95))
    reasons: list[str] = []
    if mean_error > 30.0 or p95_error > 55.0:
        status = "FAIL"
        reasons.append("Baked finger directions deviate substantially from the local source hand geometry.")
    elif mean_error > 15.0 or p95_error > 30.0:
        status = "REVIEW"
        reasons.append("Baked finger directions should be visually reviewed against the source handshape.")
    else:
        status = "PASS"
    return {
        "status": status,
        "reasons": reasons,
        "sample_count": len(errors),
        "mean_direction_error_degrees": mean_error,
        "p95_direction_error_degrees": p95_error,
        "source_observed_samples": observed_count,
        "excluded_neutral_or_transition_samples": observed_count - int(valid.sum()),
        "neutral_and_transition_coverage": "Checked independently by all-frame continuity and neutral shape validation.",
    }


def _hand_rest_palm_axes_local(armature, hand_name: str, bone_map: dict[str, str], side: str):
    hand = armature.data.bones[hand_name]
    index = armature.data.bones[bone_map[f"{side}Index1"]]
    middle = armature.data.bones[bone_map[f"{side}Middle1"]]
    little = armature.data.bones[bone_map[f"{side}Little1"]]
    inverse = hand.matrix_local.inverted()
    wrist_local = inverse @ hand.head_local
    index_local = inverse @ index.head_local
    middle_local = inverse @ middle.head_local
    little_local = inverse @ little.head_local
    across = _vector_or_none(index_local - little_local)
    forward = _vector_or_none(middle_local - wrist_local)
    if across is None or forward is None:
        return None
    normal = _vector_or_none(across.cross(forward))
    if normal is None:
        return None
    forward = _vector_or_none(normal.cross(across))
    if forward is None:
        return None
    return (across, forward, normal)


def _vector_or_none(vector):
    result = Vector(vector)
    if not np.isfinite(result[:]).all() or result.length < 1e-8:
        return None
    return result.normalized()


def _hand_visibility_metrics(armature, motion_data) -> dict:
    canonical_names = [str(name) for name in motion_data["bone_names"]]
    avatar_names = [str(name) for name in motion_data["avatar_bone_names"]]
    bone_map = dict(zip(canonical_names, avatar_names))
    required = ["LeftHand", "RightHand", "LeftIndex1", "RightIndex1", "LeftThumb1", "RightThumb1"]
    missing = [name for name in required if bone_map.get(name) not in armature.pose.bones]
    if missing:
        return {"status": "FAIL", "reasons": [f"Required visible hand/finger bones missing: {missing}"]}

    left_hand = bone_map["LeftHand"]
    right_hand = bone_map["RightHand"]
    chest = bone_map.get("Chest") if bone_map.get("Chest") in armature.pose.bones else None
    frame_count = int(motion_data["frame_count"])
    samples = sorted(set([1, max(1, frame_count // 5), max(1, frame_count // 2), max(1, 4 * frame_count // 5), frame_count]))
    visible_samples = 0
    distances = []
    for frame in samples:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        chest_x = 0.0
        if chest:
            chest_x = float((armature.matrix_world @ armature.pose.bones[chest].head).x)
        left_x = float((armature.matrix_world @ armature.pose.bones[left_hand].head).x)
        right_x = float((armature.matrix_world @ armature.pose.bones[right_hand].head).x)
        distance = abs(left_x - chest_x) + abs(right_x - chest_x)
        distances.append(distance)
        if distance > 0.35:
            visible_samples += 1

    action = bpy.data.actions[0] if bpy.data.actions else None
    animated_finger_bones = 0
    if action:
        for canonical_name in required[2:]:
            avatar_name = bone_map[canonical_name]
            if any(avatar_name in fcurve.data_path for fcurve in action.fcurves):
                animated_finger_bones += 1

    reasons = []
    review_reasons = []
    if visible_samples < max(2, len(samples) // 2):
        review_reasons.append("Hand bones appear collapsed into or hidden by the torso in sampled frames.")
    if animated_finger_bones < 4:
        reasons.append("Finger animation channels are missing or too sparse.")

    status = "FAIL" if reasons else ("REVIEW" if review_reasons else "PASS")
    return {
        "status": status,
        "reasons": reasons,
        "review_reasons": review_reasons,
        "sampled_frames": samples,
        "visible_samples": visible_samples,
        "hand_separation_distances": distances,
        "animated_finger_bones": animated_finger_bones,
    }


if __name__ == "__main__":
    raise SystemExit(main())
