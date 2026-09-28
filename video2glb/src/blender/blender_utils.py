from __future__ import annotations

import bmesh
import bpy
import math


def normalize_export_skin_weights(armature) -> dict:
    """Evaluate the same four-influence skin that the portable GLB will contain.

    The FBX on disk is untouched. Truncating only inside the glTF exporter
    changes the surface after collision correction, invalidating that solve.
    """
    bone_names = set(armature.data.bones.keys())
    reduced = 0
    for obj in bpy.context.scene.objects:
        if not _is_skinned_mesh(obj, armature):
            continue
        groups = {group.index: group for group in obj.vertex_groups if group.name in bone_names}
        for vertex in obj.data.vertices:
            weights = [(entry.group, float(entry.weight)) for entry in vertex.groups if entry.group in groups and entry.weight > 0]
            if not weights:
                continue
            if any(not math.isfinite(weight) for _, weight in weights):
                raise RuntimeError("Avatar contains non-finite skin weights.")
            weights.sort(key=lambda pair: (-pair[1], pair[0]))
            if len(weights) > 4:
                reduced += 1
                for index, _ in weights[4:]:
                    groups[index].remove([vertex.index])
            kept = weights[:4]
            total = sum(weight for _, weight in kept)
            for index, weight in kept:
                groups[index].add([vertex.index], weight / total, "REPLACE")
    bpy.context.view_layer.update()
    return {"maximum_influences": 4, "vertices_reduced": reduced,
            "normalized_before_contact_correction": True, "source_avatar_modified": False}


def suspend_mesh_deformation(armature) -> list:
    """Disable only this rig's skin evaluation during bone-only calculations."""
    states = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.object == armature:
                states.append((modifier, modifier.show_viewport))
                modifier.show_viewport = False
    bpy.context.view_layer.update()
    return states


def restore_mesh_deformation(states: list) -> None:
    for modifier, visible in states:
        modifier.show_viewport = visible
    bpy.context.view_layer.update()


def reset_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def purge_orphans() -> None:
    for _ in range(3):
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)


def import_avatar(path: str):
    bpy.ops.import_scene.fbx(filepath=path)
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not armatures:
        raise RuntimeError("No armature found in avatar FBX.")
    return armatures[0]


def isolate_target_animation(armature, action) -> None:
    for obj in bpy.context.scene.objects:
        if obj.animation_data and obj != armature:
            obj.animation_data_clear()
    for existing in list(bpy.data.actions):
        if existing != action:
            bpy.data.actions.remove(existing)
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.action = action


def validate_animation_state(armature, required_bones: list[str]) -> None:
    if armature.type != "ARMATURE":
        raise RuntimeError("Animation target is not an armature.")
    missing = [name for name in required_bones if name not in armature.pose.bones]
    if missing:
        raise RuntimeError(f"Required avatar bones missing: {missing}")
    actions = list(bpy.data.actions)
    if len(actions) != 1:
        raise RuntimeError(f"Expected exactly one action, found {len(actions)}.")
    action = actions[0]
    if not action.fcurves:
        raise RuntimeError("Action has no animation channels.")


def export_avatar_glb(path: str, armature) -> None:
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    export_state = _prepare_meshes_for_portable_gltf_export(meshes, armature)
    try:
        bpy.ops.object.select_all(action="DESELECT")
        armature.select_set(True)
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = armature
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB",
            use_selection=True,
            export_animations=True,
            export_frame_range=True,
            export_force_sampling=True,
            export_nla_strips=False,
            export_lights=False,
            export_cameras=False,
            export_tangents=True,
        )
    finally:
        _restore_meshes_after_gltf_export(export_state)


def _prepare_meshes_for_portable_gltf_export(meshes, armature) -> list[dict]:
    """Temporarily make Blender's glTF output portable without changing the source scene.

    glTF runtimes ignore transforms above a skinned mesh node, so each skinned mesh is
    exported at the scene root while retaining its world transform. Blender also cannot
    emit a complete tangent basis for polygons with more than four vertices; temporary
    mesh copies triangulate only those n-gons before tangent export.
    """
    states: list[dict] = []
    try:
        for obj in meshes:
            state = {
                "object": obj,
                "data": obj.data,
                "temporary_data": None,
                "parent": obj.parent,
                "parent_type": obj.parent_type,
                "parent_bone": obj.parent_bone,
                "matrix_parent_inverse": obj.matrix_parent_inverse.copy(),
                "matrix_basis": obj.matrix_basis.copy(),
                "matrix_world": obj.matrix_world.copy(),
            }
            states.append(state)

            if _is_skinned_mesh(obj, armature) and obj.parent is not None:
                world_matrix = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world_matrix

            if any(len(polygon.vertices) > 4 for polygon in obj.data.polygons):
                temporary_data = obj.data.copy()
                state["temporary_data"] = temporary_data
                edit_mesh = bmesh.new()
                try:
                    edit_mesh.from_mesh(temporary_data)
                    bmesh.ops.triangulate(
                        edit_mesh,
                        faces=[face for face in edit_mesh.faces if len(face.verts) > 4],
                    )
                    edit_mesh.to_mesh(temporary_data)
                finally:
                    edit_mesh.free()
                temporary_data.update()
                obj.data = temporary_data
    except BaseException:
        _restore_meshes_after_gltf_export(states)
        raise

    return states


def _restore_meshes_after_gltf_export(states: list[dict]) -> None:
    for state in states:
        obj = state["object"]
        temporary_data = state["temporary_data"]
        obj.data = state["data"]
        obj.parent = state["parent"]
        obj.parent_type = state["parent_type"]
        obj.parent_bone = state["parent_bone"]
        obj.matrix_parent_inverse = state["matrix_parent_inverse"]
        obj.matrix_basis = state["matrix_basis"]
        obj.matrix_world = state["matrix_world"]
        if temporary_data is not None:
            bpy.data.meshes.remove(temporary_data)


def _is_skinned_mesh(obj, armature) -> bool:
    return any(
        modifier.type == "ARMATURE" and modifier.object == armature
        for modifier in obj.modifiers
    )
