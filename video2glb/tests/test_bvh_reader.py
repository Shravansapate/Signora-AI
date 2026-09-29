from __future__ import annotations

from pathlib import Path

from src.bvh.bvh_reader import build_bvh_bone_mapping, parse_bvh


BVH_WITH_SIBLINGS = """\
HIERARCHY
ROOT Hips
{
  OFFSET 0 0 0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
  JOINT Chest
  {
    OFFSET 0 1 0
    CHANNELS 3 Zrotation Xrotation Yrotation
    JOINT LeftHand
    {
      OFFSET 1 0 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      JOINT LeftIndexProximal
      {
        OFFSET 0.2 0 0
        CHANNELS 3 Zrotation Xrotation Yrotation
        End Site
        {
          OFFSET 0.1 0 0
        }
      }
      JOINT LeftLittleProximal
      {
        OFFSET 0.2 0 -0.1
        CHANNELS 3 Zrotation Xrotation Yrotation
        End Site
        {
          OFFSET 0.1 0 0
        }
      }
    }
    JOINT RightHand
    {
      OFFSET -1 0 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      End Site
      {
        OFFSET -0.1 0 0
      }
    }
  }
}
MOTION
Frames: 1
Frame Time: 0.0333333333
0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
"""


def _write_bvh(tmp_path: Path) -> Path:
    path = tmp_path / "siblings.bvh"
    path.write_text(BVH_WITH_SIBLINGS, encoding="utf-8")
    return path


def test_end_site_does_not_consume_parent_closing_brace(tmp_path: Path) -> None:
    bvh = parse_bvh(_write_bvh(tmp_path))
    index = {joint.name: position for position, joint in enumerate(bvh.joints)}

    assert bvh.joints[index["LeftIndexProximal"]].parent_index == index["LeftHand"]
    assert bvh.joints[index["LeftLittleProximal"]].parent_index == index["LeftHand"]
    assert bvh.joints[index["RightHand"]].parent_index == index["Chest"]


def test_direct_bvh_map_can_split_chest_chain_and_map_unhandled_names(
    tmp_path: Path,
) -> None:
    bvh = parse_bvh(_write_bvh(tmp_path))
    mapping = build_bvh_bone_mapping(
        bvh,
        {
            "Hips": "Rig:Hips",
            "Chest": "Rig:Spine2",
            "LeftHand": "Rig:LeftHand",
            "RightHand": "Rig:RightHand",
            "LeftIndex1": "Rig:LeftIndex1",
            "LeftLittle1": "Rig:LeftPinky1",
        },
        direct_map={"Chest": "Rig:Spine1"},
    )

    assert mapping["Chest"] == "Rig:Spine1"
    assert mapping["LeftLittleProximal"] == "Rig:LeftPinky1"
    assert len(mapping.values()) == len(set(mapping.values()))
