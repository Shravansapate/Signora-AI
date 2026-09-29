import sys
import io
sys.path.insert(0, r'd:\Signora_AI\video2glb')
from src.bvh.bvh_reader import parse_bvh
from pathlib import Path
import tempfile, os

# --- Write a minimal valid BVH file ---
BVH_CONTENT = """\
HIERARCHY
ROOT Hips
{
    OFFSET 0.00 0.00 0.00
    CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
    JOINT Spine
    {
        OFFSET 0.00 5.21 0.00
        CHANNELS 3 Zrotation Xrotation Yrotation
        JOINT Chest
        {
            OFFSET 0.00 11.35 0.00
            CHANNELS 3 Zrotation Xrotation Yrotation
            JOINT Neck
            {
                OFFSET 0.00 5.00 0.00
                CHANNELS 3 Zrotation Xrotation Yrotation
                JOINT Head
                {
                    OFFSET 0.00 3.00 0.00
                    CHANNELS 3 Zrotation Xrotation Yrotation
                    End Site
                    {
                        OFFSET 0.00 5.00 0.00
                    }
                }
            }
            JOINT LeftArm
            {
                OFFSET 5.00 0.00 0.00
                CHANNELS 3 Zrotation Xrotation Yrotation
                JOINT LeftForeArm
                {
                    OFFSET 8.00 0.00 0.00
                    CHANNELS 3 Zrotation Xrotation Yrotation
                    JOINT LeftHand
                    {
                        OFFSET 7.00 0.00 0.00
                        CHANNELS 3 Zrotation Xrotation Yrotation
                        End Site
                        {
                            OFFSET 5.00 0.00 0.00
                        }
                    }
                }
            }
            JOINT RightArm
            {
                OFFSET -5.00 0.00 0.00
                CHANNELS 3 Zrotation Xrotation Yrotation
                JOINT RightForeArm
                {
                    OFFSET -8.00 0.00 0.00
                    CHANNELS 3 Zrotation Xrotation Yrotation
                    JOINT RightHand
                    {
                        OFFSET -7.00 0.00 0.00
                        CHANNELS 3 Zrotation Xrotation Yrotation
                        End Site
                        {
                            OFFSET -5.00 0.00 0.00
                        }
                    }
                }
            }
        }
    }
}
MOTION
Frames: 3
Frame Time: 0.033333
0.00 0.00 0.00 0.00 0.00 0.00 5.00 -2.00 1.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 10.00 0.00 0.00 0.00 0.00 0.00 -10.00 0.00 0.00 0.00 0.00 0.00
0.00 1.00 0.00 1.00 0.00 0.00 6.00 -2.00 1.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 11.00 0.00 0.00 0.00 0.00 0.00 -11.00 0.00 0.00 0.00 0.00 0.00
0.00 2.00 0.00 2.00 0.00 0.00 7.00 -2.00 1.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 12.00 0.00 0.00 0.00 0.00 0.00 -12.00 0.00 0.00 0.00 0.00 0.00
"""

# Write to a temp file
with tempfile.NamedTemporaryFile(mode='w', suffix='.bvh', delete=False, encoding='utf-8') as f:
    f.write(BVH_CONTENT)
    tmp_path = f.name

try:
    bvh = parse_bvh(tmp_path)
    print(f"Joints: {len(bvh.joints)}")
    for j in bvh.joints:
        print(f"  [{j.parent_index:2d}] {j.name:<20} ch_offset={j.channel_offset} channels={j.channels}")
    print(f"Frame count: {bvh.frame_count}")
    print(f"FPS: {bvh.fps:.4f}")
    print(f"Frame shape: {bvh.frames.shape}")
    trans = bvh.get_root_translation()
    print(f"Root translation frame 0: {trans[0]}")
    print(f"Root translation frame 2: {trans[2]}")
    # Test joint rotation
    spine_rots = bvh.get_joint_rotations('Spine')
    print(f"Spine rotations frame 0: {spine_rots[0]}")
    print("PARSE TEST: PASSED")
except Exception as e:
    import traceback
    print(f"PARSE TEST: FAILED — {e}")
    traceback.print_exc()
finally:
    os.unlink(tmp_path)
