from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def main() -> int:
    args = parse_args()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.import_scene.gltf(filepath=args.glb)

    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not armatures:
        raise RuntimeError("No armature in GLB.")

    setup_camera_and_light(args.focus)
    bpy.context.scene.render.resolution_x = 768
    bpy.context.scene.render.resolution_y = 768
    bpy.context.scene.eevee.taa_render_samples = 16
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    for frame in [1, 75, 150, 225, 300, 374]:
        bpy.context.scene.frame_set(frame)
        bpy.context.scene.render.filepath = str(Path(args.output_dir) / f"preview_{frame:04d}.png")
        bpy.ops.render.render(write_still=True)
    return 0


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--glb", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--focus", choices=["full", "upper-body"], default="full")
    return parser.parse_args(argv)


def setup_camera_and_light(focus: str) -> None:
    for obj in bpy.context.scene.objects:
        if obj.type in {"CAMERA", "LIGHT"}:
            bpy.data.objects.remove(obj, do_unlink=True)

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    centers = []
    for obj in meshes:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        centers.extend(corners)
    min_x = min((point.x for point in centers), default=-1.0)
    max_x = max((point.x for point in centers), default=1.0)
    min_y = min((point.y for point in centers), default=-0.5)
    max_y = max((point.y for point in centers), default=0.5)
    min_z = min((point.z for point in centers), default=0.0)
    max_z = max((point.z for point in centers), default=2.0)
    center = Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5))
    height = max(max_z - min_z, 1.0)

    if focus == "upper-body":
        center.z = min_z + height * 0.65
        ortho_scale = height * 0.78
    else:
        ortho_scale = height * 1.15

    bpy.ops.object.light_add(type="AREA", location=(center.x - height * 0.35, min_y - height, max_z + height * 0.25))
    light = bpy.context.object
    light.data.energy = 900
    light.data.size = height * 1.2

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


if __name__ == "__main__":
    raise SystemExit(main())
