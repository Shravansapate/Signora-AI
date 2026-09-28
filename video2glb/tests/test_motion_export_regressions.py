import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from src.motion.neutral_hand import generate_neutral_hand_pose
from src.motion.skeleton_solver import solve_motion_from_pose


def test_finger_reacquisition_ramps_even_when_gap_is_inside_release_hold():
    from src.motion.skeleton_solver import _ramp_finger_direction_activation
    observed = np.ones((12, 1, 1, 1), dtype=bool)
    observed[7:9] = False
    result = _ramp_finger_direction_activation(observed, np.ones_like(observed), ramp_frames=4)
    assert result[6, 0, 0, 0] == 1.0
    assert result[9, 0, 0, 0] == 0.25
    assert result[10, 0, 0, 0] == 0.5


@pytest.mark.parametrize("observed_edges", [False, True])
def test_neutral_fingers_cannot_be_overridden_by_tracking(tmp_path, observed_edges):
    count = 30
    pose = np.zeros((count, 33, 3), dtype=float)
    pose[:, :, :2] = [0.5, 0.5]
    pose[:, 11, :2], pose[:, 12, :2] = [0.65, 0.25], [0.35, 0.25]
    pose[:, 23, :2], pose[:, 24, :2] = [0.6, 0.65], [0.4, 0.65]
    pose[:, 13, :2], pose[:, 14, :2] = [0.7, 0.4], [0.3, 0.4]
    pose[:, 15, :2], pose[:, 16, :2] = [0.65, 0.5], [0.35, 0.5]
    hands = np.full((count, 21, 3), np.nan)
    # Isolated early observations precede stable tracking, the exact case that
    # previously let a direction constraint overwrite a fully neutral pose.
    frames = [*range(0, 10), *range(12, 30)] if observed_edges else [2, 3, *range(7, 22)]
    for frame in frames:
        hands[frame, 0] = [0.5, 0.5, 0.0]
        for finger in range(5):
            for joint in range(4):
                hands[frame, 1 + finger * 4 + joint] = [0.4 + finger * 0.04, 0.45 - joint * 0.03, 0.0]
    source = tmp_path / "pose.npz"
    np.savez(source, fps=25.0, frame_count=count, width=640, height=480,
             pose_world=pose, pose_image=pose, left_hand_image=hands,
             right_hand_image=hands, left_hand_world=hands, right_hand_world=hands)
    neutral = tmp_path / "neutral.json"
    neutral.write_text(json.dumps(generate_neutral_hand_pose("config/avatar_profile.json", "config/avatar_bone_map.json")), encoding="utf-8")
    target = tmp_path / "motion.npz"
    solve_motion_from_pose(source, "config/avatar_profile.json", "config/avatar_bone_map.json", target,
                           "TEST", neutral_hand_pose_path=neutral)
    with np.load(target) as motion:
        weights = motion["neutral_finger_weights"]
        influence = motion["finger_direction_influence"]
        if observed_edges:
            # Quiet observed tail frames must not toggle neutral mid-sign.
            assert np.all(weights == 0.0)
        else:
            assert np.any(weights >= 0.999)
            # Keep the existing six-frame release/contact timing. Blender's
            # anchored return must reach this declared endpoint exactly.
            np.testing.assert_allclose(weights[22:28, 0], np.arange(1, 7) / 6.0, atol=1e-7)
            assert np.all(weights[27:] == 1.0)
        assert np.all(influence[weights >= 0.999] == 0.0)
        assert np.any(influence[weights == 0.0] > 0.0)
        raw_observed = np.isfinite(hands).all(axis=(1, 2))
        assert not np.any(motion["finger_direction_valid"][~raw_observed])
        assert np.any(motion["finger_direction_usable"][~raw_observed])


@pytest.mark.skipif(os.environ.get("VIDEO2GLB_BLENDER_TESTS") != "1", reason="Opt-in actual avatar Blender regression")
def test_actual_avatar_neutral_wrist_uses_evaluated_arm_frame():
    blender = Path(os.environ.get("BLENDER_PATH", r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"))
    assert blender.is_file()
    script = '''
import sys, json, numpy as np, bpy
from pathlib import Path
from mathutils import Quaternion, Vector
sys.path.insert(0, str(Path.cwd()))
from src.blender.blender_utils import reset_scene, import_avatar, suspend_mesh_deformation, normalize_export_skin_weights
from src.blender.blender_apply_motion import update_neutral_wrist_orientation, setup_finger_tracking, update_finger_targets, condition_baked_arm_rotations
from src.motion.neutral_hand import generate_neutral_hand_pose, anatomical_palm_normal
from src.avatar.bone_mapping import load_bone_map
from src.qc.neutral_shape import evaluate_neutral_finger_shape
reset_scene()
armature = import_avatar('assets/character.fbx')
skin_report=normalize_export_skin_weights(armature)
assert skin_report['maximum_influences'] == 4
assert skin_report['vertices_reduced'] > 0
assert normalize_export_skin_weights(armature)['vertices_reduced'] == 0
suspend_mesh_deformation(armature)
mapping = load_bone_map('config/avatar_bone_map.json')
neutral = generate_neutral_hand_pose('config/avatar_profile.json', 'config/avatar_bone_map.json')
for side, key in [('Left', 'left_hand'), ('Right', 'right_hand')]:
    for entry in neutral[key].values():
        bone = armature.pose.bones[entry['avatar_bone']]
        bone.rotation_mode = 'QUATERNION'
        bone.rotation_quaternion = Quaternion(entry['rotation_wxyz'])
    hand = armature.pose.bones[mapping[side+'Hand']]
    hand.rotation_mode = 'QUATERNION'
    hand.rotation_quaternion = Quaternion((0, 1, 0), 2.5)
bpy.context.view_layer.update()
update_neutral_wrist_orientation(armature, mapping, np.ones(2), 1)
center = armature.pose.bones[mapping['Hips']].head
for side in ['Left', 'Right']:
    head = lambda key: armature.pose.bones[mapping[side+key]].head
    forward = (head('Middle1')-head('Hand')).normalized()
    normal = Vector(anatomical_palm_normal(np.array(head('Index1')-head('Little1')), np.array(forward), side))
    inward = center-head('Hand'); inward -= forward*inward.dot(forward)
    assert normal.dot(inward.normalized()) > 0.999, side
    directions=[]
    for finger in ['Thumb','Index','Middle','Ring','Little']:
        chain=[armature.pose.bones[mapping[side+finger+str(j)]] for j in [1,2,3]]
        directions.append([tuple(chain[1].head-chain[0].head), tuple(chain[2].head-chain[1].head), tuple(chain[2].tail-chain[2].head)])
    report=evaluate_neutral_finger_shape(np.array(directions))
    assert report['status']=='PASS', report
directions=np.zeros((2,5,3,3)); directions[...,1]=1
objects,constraints=setup_finger_tracking(armature,directions,mapping)
for details in constraints.values():
    details['maximum_rotation_step']=np.radians(20.)
update_finger_targets(armature,objects,constraints,directions,None,np.ones((2,5,3)),1,np.zeros(2))
previous={segment['bone_name']:segment['previous_final_rotation'].copy() for details in constraints.values() for segments in details['fingers'].values() for segment in segments}
update_finger_targets(armature,objects,constraints,-directions,None,np.ones((2,5,3)),2,np.zeros(2))
for side,details in constraints.items():
    hand=armature.pose.bones[details['hand_name']].matrix.to_quaternion()
    assert details['continuity_corrections']
    for segments in details['fingers'].values():
        for segment in segments:
            actual=hand.inverted() @ armature.pose.bones[segment['bone_name']].matrix.to_quaternion()
            angle=2*np.arccos(np.clip(abs(previous[segment['bone_name']].dot(actual)),0,1))
            assert np.degrees(angle) <= 20.01, (segment['bone_name'],np.degrees(angle))
# A six-frame planned 170deg return needs 28.33deg per frame. It is a
# continuous anchored SLERP, not an unobserved tracking reversal to rate-limit.
for details in constraints.values():
    hand=armature.pose.bones[details['hand_name']].matrix.to_quaternion()
    for segments in details['fingers'].values():
        for segment in segments:
            neutral_rotation=segment['previous_final_rotation'].copy()
            start=(Quaternion((1,0,0),np.radians(170.)) @ neutral_rotation).normalized()
            segment['neutral_palm_rotation']=neutral_rotation
            segment['previous_final_rotation']=start
            segment['previous_palm_rotation']=start.copy()
            segment['previous_neutral_weight']=0.
            segment.pop('neutral_exit_anchor',None)
            bone=armature.pose.bones[segment['bone_name']]
            matrix=(hand @ start).to_matrix().to_4x4(); matrix.translation=bone.head.copy()
            bone.matrix=matrix
            bpy.context.view_layer.update()
for step in range(1,7):
    previous={segment['bone_name']:segment['previous_final_rotation'].copy() for details in constraints.values() for segments in details['fingers'].values() for segment in segments}
    update_finger_targets(armature,objects,constraints,directions,np.zeros((2,5,3),dtype=bool),np.zeros((2,5,3)),step+2,np.full(2,step/6.))
    for details in constraints.values():
        hand=armature.pose.bones[details['hand_name']].matrix.to_quaternion()
        for segments in details['fingers'].values():
            for segment in segments:
                actual=(hand.inverted() @ armature.pose.bones[segment['bone_name']].matrix.to_quaternion()).normalized()
                angle=2*np.arccos(np.clip(abs(previous[segment['bone_name']].dot(actual)),0,1))
                assert abs(np.degrees(angle)-170./6.) < .05, (step,segment['bone_name'],np.degrees(angle))
                if step==6:
                    error=2*np.arccos(np.clip(abs(actual.dot(segment['neutral_palm_rotation'])),0,1))
                    assert np.degrees(error)<.1, (segment['bone_name'],np.degrees(error))
assert sum(len(details['planned_neutral_transitions']) for details in constraints.values())==180
# A sudden unobserved neutral reversal is not a planned return and must still
# obey the same 20deg test cap; neither an anchor alone nor weight=1 exempts it.
for details in constraints.values():
    for segments in details['fingers'].values():
        for segment in segments:
            segment['neutral_palm_rotation']=(Quaternion((1,0,0),np.radians(170.)) @ segment['previous_final_rotation']).normalized()
            segment['previous_neutral_weight']=0.
            segment.pop('neutral_exit_anchor',None)
previous={segment['bone_name']:segment['previous_final_rotation'].copy() for details in constraints.values() for segments in details['fingers'].values() for segment in segments}
update_finger_targets(armature,objects,constraints,directions,np.zeros((2,5,3),dtype=bool),np.zeros((2,5,3)),9,np.ones(2))
for details in constraints.values():
    hand=armature.pose.bones[details['hand_name']].matrix.to_quaternion()
    for segments in details['fingers'].values():
        for segment in segments:
            actual=(hand.inverted() @ armature.pose.bones[segment['bone_name']].matrix.to_quaternion()).normalized()
            angle=2*np.arccos(np.clip(abs(previous[segment['bone_name']].dot(actual)),0,1))
            assert np.degrees(angle)<=20.01, (segment['bone_name'],np.degrees(angle))
assert sum(len(details['planned_neutral_transitions']) for details in constraints.values())==180
# A stale plan flag cannot exempt a pose which has already fallen off its arc.
for details in constraints.values():
    for segments in details['fingers'].values():
        for segment in segments:
            segment['previous_neutral_weight']=.5
previous={segment['bone_name']:segment['previous_final_rotation'].copy() for details in constraints.values() for segments in details['fingers'].values() for segment in segments}
update_finger_targets(armature,objects,constraints,directions,np.zeros((2,5,3),dtype=bool),np.zeros((2,5,3)),10,np.full(2,2./3.))
for details in constraints.values():
    for segments in details['fingers'].values():
        for segment in segments:
            angle=2*np.arccos(np.clip(abs(previous[segment['bone_name']].dot(segment['previous_final_rotation'])),0,1))
            assert np.degrees(angle)<=20.01, (segment['bone_name'],np.degrees(angle))
assert sum(len(details['planned_neutral_transitions']) for details in constraints.values())==180
for frame in range(1,6):
    bpy.context.scene.frame_set(frame)
    for side in ['Left','Right']:
        for part in ['UpperArm','ForeArm']:
            bone=armature.pose.bones[mapping[side+part]]
            bone.rotation_mode='QUATERNION'
            bone.rotation_quaternion=Quaternion((1,0,0),np.radians(13) if frame==3 and side+part=='LeftForeArm' else 0)
            bone.keyframe_insert(data_path='rotation_quaternion',frame=frame)
bpy.context.scene.frame_set(3)
palms=[armature.pose.bones[mapping[side+'Hand']].matrix.to_quaternion().copy() for side in ['Left','Right']]
body_report=condition_baked_arm_rotations(armature,mapping,5)
assert body_report['status']=='REVIEW',body_report
bpy.context.scene.frame_set(3)
for side,prior in zip(['Left','Right'],palms):
    actual=armature.pose.bones[mapping[side+'Hand']].matrix.to_quaternion()
    assert abs(actual.dot(prior)) > .99999
from src.blender.blender_render_animation import render_visible_meshes, setup_camera_and_light
visible_before=set(render_visible_meshes())
hidden=bpy.data.collections.new('hidden_import_helper')
bpy.context.scene.collection.children.link(hidden)
hidden.hide_render=True
nested=bpy.data.collections.new('hidden_child')
hidden.children.link(nested)
bpy.ops.mesh.primitive_cube_add(size=1000)
helper=bpy.context.object
for collection in list(helper.users_collection):
    collection.objects.unlink(helper)
nested.objects.link(helper)
assert set(render_visible_meshes()) == visible_before
setup_camera_and_light('upper-body')
assert bpy.context.scene.camera.data.ortho_scale < 1000
print('ACTUAL_AVATAR_NEUTRAL_PASS')
'''
    result = subprocess.run([str(blender), "--factory-startup", "--background", "--python-exit-code", "17",
                             "--python-expr", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTUAL_AVATAR_NEUTRAL_PASS" in result.stdout
