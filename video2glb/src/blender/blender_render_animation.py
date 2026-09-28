from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def main() -> int:
    args = parse_args()
    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(args.fps)))
    scene.render.fps_base = scene.render.fps / max(args.fps, 1e-8)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.import_scene.gltf(filepath=args.glb)

    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not armatures or not armatures[0].animation_data or not armatures[0].animation_data.action:
        raise RuntimeError("GLB must contain an armature with one active animation.")

    action = armatures[0].animation_data.action
    scene.frame_start = int(round(action.frame_range[0]))
    scene.frame_end = int(round(action.frame_range[1]))
    scene.render.resolution_x = args.size
    scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    # This is a diagnostic preview, not the delivered material/render. Studio
    # shading exposes the silhouette and contacts without costly path effects.
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.filepath = args.output
    apply_diagnostic_material()
    setup_camera_and_light(args.focus)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.render.render(animation=True)
    return 0


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--glb", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fps", required=True, type=float)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--focus", choices=["full", "upper-body"], default="upper-body")
    return parser.parse_args(argv)


def setup_camera_and_light(focus: str) -> None:
    for obj in list(bpy.context.scene.objects):
        if obj.type in {"CAMERA", "LIGHT"}:
            bpy.data.objects.remove(obj, do_unlink=True)

    corners = []
    bpy.context.view_layer.update()
    for obj in render_visible_meshes():
        corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not corners:
        raise RuntimeError("GLB has no render-visible mesh for the diagnostic preview.")
    min_x = min((point.x for point in corners), default=-1.0)
    max_x = max((point.x for point in corners), default=1.0)
    min_y = min((point.y for point in corners), default=-0.5)
    max_y = max((point.y for point in corners), default=0.5)
    min_z = min((point.z for point in corners), default=0.0)
    max_z = max((point.z for point in corners), default=2.0)
    center = Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5))
    height = max(max_z - min_z, 1.0)
    if focus == "upper-body":
        center.z = min_z + height * 0.65
        ortho_scale = height * 0.78
    else:
        ortho_scale = height * 1.15

    bpy.ops.object.light_add(type="AREA", location=(center.x - height * 0.35, min_y - height, max_z + height * 0.25))
    key = bpy.context.object
    key.data.energy = 900
    key.data.shape = "DISK"
    key.data.size = height * 1.2

    bpy.ops.object.light_add(type="AREA", location=(center.x + height * 0.45, max_y + height * 0.5, center.z + height * 0.1))
    fill = bpy.context.object
    fill.data.energy = 350
    fill.data.size = height

    bpy.ops.object.camera_add(location=(center.x, min_y - height * 1.5, center.z))
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = ortho_scale
    camera.data.clip_end = 10000
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera


def render_visible_meshes():
    """Exclude importer bone-shape helpers and hidden collection descendants."""
    visible = set()

    def visit(collection):
        if collection.hide_render:
            return
        visible.update(obj for obj in collection.objects if obj.type == "MESH" and not obj.hide_render)
        for child in collection.children:
            visit(child)

    visit(bpy.context.scene.collection)
    return [obj for obj in bpy.context.scene.objects if obj in visible]


def apply_diagnostic_material() -> None:
    """Make the debug-only preview readable without altering the exported GLB."""
    material = bpy.data.materials.get("ISL_Debug_Contrast") or bpy.data.materials.new("ISL_Debug_Contrast")
    material.use_nodes = True
    material.diffuse_color = (0.08, 0.42, 0.78, 1.0)
    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled:
        principled.inputs["Base Color"].default_value = (0.08, 0.42, 0.78, 1.0)
        principled.inputs["Metallic"].default_value = 0.0
        principled.inputs["Roughness"].default_value = 0.42
    for obj in render_visible_meshes():
        obj.data.materials.clear()
        obj.data.materials.append(material)
    world = bpy.context.scene.world
    if world:
        world.color = (0.025, 0.025, 0.035)


if __name__ == "__main__":
    raise SystemExit(main())
