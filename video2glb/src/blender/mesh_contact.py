"""Length-preserving correction of measured skinned hand/torso intersections."""
from __future__ import annotations

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
import numpy as np

from src.motion.arm_ik import solve_two_bone_endpoint
from src.qc.mesh_clearance import minimum_front_surface_correction, clearance_envelope, supported_face_projection


class TorsoContactSampler:
    def __init__(self, armature, mapping):
        self.armature, self.mapping = armature, mapping
        self.selections = []
        fingers = ("Thumb", "Index", "Middle", "Ring", "Little")
        torso = {mapping[name] for name in ("Hips", "Spine", "Chest")}
        hands = [{avatar for name, avatar in mapping.items() if name.startswith(side)
                  and (name.endswith("Hand") or any(f in name for f in fingers))} for side in ("Left", "Right")]
        limbs = {avatar for name, avatar in mapping.items()
                 if any(part in name for part in ("Shoulder", "UpperArm", "ForeArm", "Hand", *fingers))}
        for obj in bpy.context.scene.objects:
            if obj.type != "MESH" or not any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers):
                continue
            groups = {group.index: group.name for group in obj.vertex_groups}
            torso_indices, hand_indices = set(), [[], []]
            for vertex in obj.data.vertices:
                weights = {groups.get(group.group): float(group.weight) for group in vertex.groups}
                total = lambda names: sum(weight for name, weight in weights.items() if name in names)
                if total(torso) >= 0.65 and total(limbs) <= 0.10:
                    torso_indices.add(vertex.index)
                for side in range(2):
                    if total(hands[side]) >= 0.65:
                        hand_indices[side].append(vertex.index)
            obj.data.calc_loop_triangles()
            triangles = [tuple(tri.vertices) for tri in obj.data.loop_triangles if all(i in torso_indices for i in tri.vertices)]
            if not triangles and not any(hand_indices):
                continue
            selected = np.unique(np.asarray(triangles, dtype=np.int64).reshape(-1))
            remap = {int(value): index for index, value in enumerate(selected)}
            faces = [tuple(remap[index] for index in triangle) for triangle in triangles]
            edges = {}
            for face in faces:
                for a, b in zip(face, face[1:] + face[:1]):
                    key = tuple(sorted((a, b)))
                    edges[key] = edges.get(key, 0) + 1
            self.selections.append({"object": obj, "indices": selected, "triangles": faces,
                                    "borders": {edge for edge, count in edges.items() if count == 1},
                                    "hands": hand_indices})

    def sample(self):
        arm, mapping = self.armature, self.mapping
        world_head = lambda name: arm.matrix_world @ arm.pose.bones[mapping[name]].head
        hip, left, right = (world_head(name) for name in ("Hips", "LeftUpperArm", "RightUpperArm"))
        up = ((left + right) * 0.5 - hip).normalized()
        width = float((left - right).length)
        front = (left - right).cross(up).normalized()
        if width <= 1e-8 or front.length < 1e-8:
            raise RuntimeError("Degenerate torso basis for collision correction.")
        surfaces, hand_parts = [], [[], []]
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for item in self.selections:
            obj = item["object"].evaluated_get(depsgraph)
            mesh = obj.to_mesh()
            try:
                if len(mesh.vertices) != len(item["object"].data.vertices):
                    raise RuntimeError("Cannot transfer hand collision selections across a topology-changing modifier.")
                coords = np.empty((len(mesh.vertices), 3), dtype=np.float32)
                mesh.vertices.foreach_get("co", coords.reshape(-1))
                transform = np.asarray(obj.matrix_world, dtype=np.float64)
                world = lambda indices: coords[indices] @ transform[:3, :3].T + transform[:3, 3]
                for side in range(2):
                    if item["hands"][side]:
                        hand_parts[side].append(world(item["hands"][side]))
                if item["triangles"]:
                    coords_world = world(item["indices"])
                    vertices = [Vector(point) for point in coords_world]
                    tree = BVHTree.FromPolygons(vertices, item["triangles"], all_triangles=True)
                    surfaces.append((tree, vertices, coords_world, item))
            finally:
                obj.to_mesh_clear()
        if not surfaces or not all(hand_parts):
            raise RuntimeError("Skinned torso and both hands are required for mesh contact correction.")
        all_vertices = np.concatenate([surface[2] for surface in surfaces])
        all_faces, offset = [], 0
        for _, _, coords, item in surfaces:
            all_faces.extend(tuple(index + offset for index in face) for face in item["triangles"])
            offset += len(coords)
        reports = []
        for side in range(2):
            points = np.concatenate(hand_parts[side])
            penetrating = np.zeros(len(points), dtype=bool)
            for tree, vertices, coords, item in surfaces:
                nearby = ((points >= np.min(coords, axis=0) - width * 0.01)
                          & (points <= np.max(coords, axis=0) + width * 0.01)).all(axis=1)
                for index in np.flatnonzero(nearby):
                    point = Vector(points[index])
                    location, normal, face_index, _ = tree.find_nearest(point)
                    if location is None or normal.dot(front) < 0.2 or (point - hip).dot(front) < 0.0:
                        continue
                    radial = location - hip - up * (location - hip).dot(up)
                    if radial.length < 1e-8 or normal.dot(radial.normalized()) < 0.2:
                        continue
                    if (point - location).dot(normal) >= -width * 0.001:
                        continue
                    if not supported_face_projection(point, location, normal, width):
                        continue
                    face = item["triangles"][face_index]
                    border = False
                    for a, b in zip(face, face[1:] + face[:1]):
                        if tuple(sorted((a, b))) in item["borders"]:
                            edge = vertices[b] - vertices[a]
                            t = np.clip((location - vertices[a]).dot(edge) / max(edge.length_squared, 1e-12), 0.0, 1.0)
                            border |= (location - vertices[a] - edge * float(t)).length < width * 1e-4
                    if not border:
                        penetrating[index] = True
            reports.append(minimum_front_surface_correction(
                points, all_vertices, np.asarray(all_faces, dtype=np.int64), np.asarray(front), width,
                penetrating_mask=penetrating, maximum_automatic_shift_ratio=0.30, clearance_ratio=0.015,
            ))
        return reports, np.asarray(front), width


def correct_baked_torso_contacts(armature, mapping, frame_count, fps):
    # A weighted wrist/forearm seam is not a rigidly translated hand. Re-measure
    # the actual deformed surface, bounded to three passes and the original
    # cumulative correction budget; validation remains independent afterwards.
    total = np.zeros((frame_count, 2))
    passes = []
    for _ in range(3):
        report = _correct_contact_pass(armature, mapping, frame_count, fps, total)
        passes.append(report)
        for sample in report["corrected_samples"]:
            total[sample["frame"] - 1, ("Left", "Right").index(sample["side"])] += sample["depth_shift_shoulder_widths"]
        if not report["corrected_samples"]:
            break
    result = dict(passes[-1])
    for name in ("corrected_samples", "unsupported_samples", "unreachable_samples", "projected_endpoint_samples"):
        result[name] = [dict(sample, correction_pass=index + 1) for index, report in enumerate(passes) for sample in report[name]]
    result.update(
        status="REVIEW" if any(report["status"] != "PASS" for report in passes) else "PASS",
        reasons=list(dict.fromkeys(reason for report in passes for reason in report["reasons"])),
        maximum_shift_shoulder_widths=float(np.max(total)),
        maximum_endpoint_error_avatar_units=max(report["maximum_endpoint_error_avatar_units"] for report in passes),
        correction_pass_count=len(passes), maximum_cumulative_shift_shoulder_widths=0.30,
    )
    return result


def _correct_contact_pass(armature, mapping, frame_count, fps, already_shifted):
    """Adjust depth only, then independently re-import and validate the export.

    The measured correction is tapered across neighboring frames to avoid a
    one-frame pop. Large/unreachable/unsupported corrections are not hidden.
    Every applied correction remains review evidence, not a language verdict.
    """
    sampler = TorsoContactSampler(armature, mapping)
    required = np.zeros((frame_count, 2))
    fronts = np.empty((frame_count, 3))
    widths = np.empty(frame_count)
    unsupported = []
    for index in range(frame_count):
        bpy.context.scene.frame_set(index + 1)
        reports, fronts[index], widths[index] = sampler.sample()
        for side, report in enumerate(reports):
            if report["automatic_correction_allowed"]:
                required[index, side] = report["required_shift"] / widths[index]
            elif report["correction_required"] or report["confirmed_penetrating_point_count"]:
                unsupported.append({"frame": index + 1, "side": ("Left", "Right")[side], "reasons": report["reasons"]})
    envelope = clearance_envelope(required, radius=max(2, round(float(fps) * 0.32)))
    budget = np.maximum(0.0, 0.30 - already_shifted)
    for frame, side in np.argwhere(envelope > budget + 1e-8):
        unsupported.append({"frame": int(frame) + 1, "side": ("Left", "Right")[side],
                            "reasons": ["Cumulative contact correction budget exhausted; mesh validation must decide the residual."]})
    envelope = np.minimum(envelope, budget)
    changed, unreachable, projected = [], [], []
    worst_endpoint_error = 0.0
    for index in range(frame_count):
        if not np.any(envelope[index] > 1e-8):
            continue
        bpy.context.scene.frame_set(index + 1)
        inverse = armature.matrix_world.to_3x3().inverted()
        for side_index, side in enumerate(("Left", "Right")):
            amount = float(envelope[index, side_index] * widths[index])
            if amount <= 1e-8:
                continue
            upper, forearm, hand = [armature.pose.bones[mapping[side + part]] for part in ("UpperArm", "ForeArm", "Hand")]
            shoulder, elbow, wrist = upper.head.copy(), forearm.head.copy(), hand.head.copy()
            shift = inverse @ Vector(fronts[index] * amount)
            upper_matrix, fore_matrix, hand_matrix = (bone.matrix.copy() for bone in (upper, forearm, hand))
            result = solve_two_bone_endpoint(np.array(shoulder), np.array(elbow), np.array(wrist),
                                            np.array(wrist + shift), (elbow - shoulder).length, (wrist - elbow).length)
            if not result["reachable"]:
                record = {"frame": index + 1, "side": side, "endpoint_error": result["endpoint_error"],
                          "endpoint_error_normalized": result["endpoint_error_normalized"]}
                projected.append(record)
                if result["endpoint_error_normalized"] > 0.08:
                    unreachable.append(record)
                    continue
            desired_elbow, desired_wrist = Vector(result["elbow"]), Vector(result["wrist"])
            upper_swing = (elbow - shoulder).rotation_difference(desired_elbow - shoulder)
            target = (upper_swing.to_matrix() @ upper_matrix.to_3x3()).to_4x4()
            target.translation = shoulder
            upper.matrix = target
            bpy.context.view_layer.update()
            fore_swing = (wrist - elbow).rotation_difference(desired_wrist - desired_elbow)
            target = (fore_swing.to_matrix() @ fore_matrix.to_3x3()).to_4x4()
            target.translation = forearm.head.copy()
            forearm.matrix = target
            bpy.context.view_layer.update()
            hand_matrix.translation = hand.head.copy()
            hand.matrix = hand_matrix
            for bone in (upper, forearm, hand):
                bone.rotation_mode = "QUATERNION"
                bone.keyframe_insert(data_path="rotation_quaternion", frame=index + 1)
            bpy.context.view_layer.update()
            error = float((hand.head - desired_wrist).length)
            worst_endpoint_error = max(worst_endpoint_error, error)
            if error > max((wrist - shoulder).length, 1.0) * 1e-4:
                raise RuntimeError(f"Length-preserving contact correction failed for {side} frame {index + 1}.")
            changed.append({"frame": index + 1, "side": side, "depth_shift_shoulder_widths": float(envelope[index, side_index])})
    return {"status": "REVIEW" if changed or unsupported or unreachable else "PASS",
            "method": "measured_torso_clearance_with_length_preserving_arm_IK",
            "reasons": ["Mesh-derived depth corrections require source comparison."] if changed else [],
            "source_frame_count": frame_count, "evaluated_every_input_frame": True,
            "corrected_samples": changed, "unsupported_samples": unsupported, "unreachable_samples": unreachable,
            "projected_endpoint_samples": projected,
            "maximum_shift_shoulder_widths": float(np.max(envelope)),
            "maximum_endpoint_error_avatar_units": worst_endpoint_error,
            "preserves_finger_pose": True, "preserves_arm_lengths": True,
            "post_export_mesh_validation_required": True}
