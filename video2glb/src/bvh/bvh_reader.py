"""BVH (Biovision Hierarchy) file parser.

Reads a .bvh file and exposes:
  - skeleton  : list of joint dicts (name, parent_index, offset, channels)
  - frame_time: seconds per frame  (1 / fps)
  - frames    : np.ndarray shape (F, total_channels)  -- raw channel data
  - fps       : derived from frame_time

Supports both standard BVH naming (ForeArm, HandThumb1) and
Unity Humanoid naming (LowerArm, ThumbProximal/Intermediate/Distal).

This module stays dependency-free (stdlib + numpy only) so it can be
imported both inside Blender's bundled Python and in the host environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BVHJoint:
    name: str
    parent_index: int  # -1 for root
    offset: tuple[float, float, float]
    channels: list[str]          # e.g. ["Xposition", "Yposition", "Zposition", "Zrotation", ...]
    channel_offset: int          # index into the per-frame channel vector


@dataclass
class BVHData:
    joints: list[BVHJoint]
    frame_count: int
    frame_time: float            # seconds per frame
    frames: np.ndarray           # shape (frame_count, total_channels), float64
    source_path: Path | None = None

    @property
    def fps(self) -> float:
        if self.frame_time <= 0:
            raise ValueError("BVH frame_time must be positive.")
        return 1.0 / self.frame_time

    @property
    def total_channels(self) -> int:
        return self.frames.shape[1] if self.frames.ndim == 2 else 0

    def joint_by_name(self, name: str) -> BVHJoint | None:
        for j in self.joints:
            if j.name == name:
                return j
        return None

    def rotation_channels_for(self, joint_name: str) -> list[str]:
        j = self.joint_by_name(joint_name)
        if j is None:
            return []
        return [ch for ch in j.channels if "rotation" in ch.lower()]

    def translation_channels_for(self, joint_name: str) -> list[str]:
        j = self.joint_by_name(joint_name)
        if j is None:
            return []
        return [ch for ch in j.channels if "position" in ch.lower()]

    def get_joint_rotations(self, joint_name: str) -> np.ndarray | None:
        """Return shape (F, 3) Euler angles in degrees for this joint's rotation channels."""
        j = self.joint_by_name(joint_name)
        if j is None:
            return None
        rot_indices = [
            j.channel_offset + i
            for i, ch in enumerate(j.channels)
            if "rotation" in ch.lower()
        ]
        if not rot_indices:
            return None
        return self.frames[:, rot_indices]

    def get_root_translation(self) -> np.ndarray | None:
        """Return shape (F, 3) root translation from root joint's position channels."""
        if not self.joints:
            return None
        root = self.joints[0]
        pos_indices = [
            root.channel_offset + i
            for i, ch in enumerate(root.channels)
            if "position" in ch.lower()
        ]
        if len(pos_indices) != 3:
            return None
        return self.frames[:, pos_indices]

    def channel_order_for(self, joint_name: str) -> list[str]:
        """Return the rotation channel order (e.g. ['Z', 'X', 'Y']) for this joint."""
        j = self.joint_by_name(joint_name)
        if j is None:
            return []
        return [ch[0].upper() for ch in j.channels if "rotation" in ch.lower()]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_bvh(path: str | Path) -> BVHData:
    """Parse a .bvh file and return a BVHData instance."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines()]
    it = iter(lines)

    joints: list[BVHJoint] = []
    stack: list[int] = []   # stack of joint indices (parent chain)
    channel_cursor = 0

    # -----------------------------------------------------------------------
    # HIERARCHY section
    # -----------------------------------------------------------------------
    line = _next_non_empty(it)
    if line.upper() != "HIERARCHY":
        raise ValueError(f"BVH file does not start with HIERARCHY: {path}")

    while True:
        line = _next_non_empty(it)
        upper = line.upper()

        if upper == "MOTION":
            break

        if upper.startswith("ROOT") or upper.startswith("JOINT"):
            parts = line.split()
            joint_name = parts[1] if len(parts) > 1 else f"Joint{len(joints)}"
            parent_idx = stack[-1] if stack else -1
            joint = BVHJoint(
                name=joint_name,
                parent_index=parent_idx,
                offset=(0.0, 0.0, 0.0),
                channels=[],
                channel_offset=channel_cursor,
            )
            joints.append(joint)
            stack.append(len(joints) - 1)

        elif upper == "{":
            pass  # block open

        elif upper == "}":
            if stack:
                stack.pop()

        elif upper.startswith("OFFSET"):
            parts = line.split()
            if stack and len(parts) >= 4:
                idx = stack[-1]
                joints[idx].offset = (float(parts[1]), float(parts[2]), float(parts[3]))

        elif upper.startswith("CHANNELS"):
            parts = line.split()
            if stack and len(parts) >= 2:
                idx = stack[-1]
                count = int(parts[1])
                channels = parts[2: 2 + count]
                joints[idx] = BVHJoint(
                    name=joints[idx].name,
                    parent_index=joints[idx].parent_index,
                    offset=joints[idx].offset,
                    channels=channels,
                    channel_offset=channel_cursor,
                )
                channel_cursor += count

        elif upper.startswith("END"):
            _skip_block(it)

    # -----------------------------------------------------------------------
    # MOTION section
    # -----------------------------------------------------------------------
    line = _next_non_empty(it)
    frame_count = int(line.split(":")[1].strip()) if ":" in line else int(line.split()[-1])

    line = _next_non_empty(it)
    frame_time_str = line.split(":")[1].strip() if ":" in line else line.split()[-1]
    frame_time = float(frame_time_str)

    if channel_cursor == 0:
        raise ValueError("BVH file has no channels defined.")

    raw_values: list[list[float]] = []
    for _ in range(frame_count):
        line = _next_non_empty(it)
        if line is None:
            break
        values = [float(v) for v in line.split()]
        if len(values) < channel_cursor:
            values.extend([0.0] * (channel_cursor - len(values)))
        raw_values.append(values[:channel_cursor])

    frames = np.array(raw_values, dtype=np.float64)
    if frames.ndim == 1:
        frames = frames.reshape(1, -1)

    return BVHData(
        joints=joints,
        frame_count=len(raw_values),
        frame_time=frame_time,
        frames=frames,
        source_path=path,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_non_empty(it) -> str | None:
    for line in it:
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _skip_block(it) -> None:
    """Skip lines until a matching closing brace is consumed."""
    depth = 0
    for line in it:
        line = line.strip()
        if line == "{":
            depth += 1
        elif line == "}":
            if depth <= 1:
                return
            depth -= 1


# ---------------------------------------------------------------------------
# BVH -> canonical joint name mapper
# ---------------------------------------------------------------------------
#
# Supports two naming conventions in the same table:
#   1. Standard BVH:    LeftForeArm, LeftHandThumb1/2/3, LeftHandIndex1/2/3 ...
#   2. Unity Humanoid:  LeftLowerArm, LeftThumbProximal/Intermediate/Distal ...
#
# IMPORTANT: more specific patterns MUST appear before general ones.
# Fingers must be listed before Hand/Arm, ForeArm before UpperArm.
#
_BVH_TO_CANONICAL: list[tuple[re.Pattern, str]] = [

    # Root / Hips
    (re.compile(r"hips?|pelvis", re.I), "Hips"),

    # Spine -- exact "Spine" needs ^$ so it doesn't swallow "SpineChest" etc.
    (re.compile(r"^spine$|spine1|spine_01|spine01|lowerback|abdomen", re.I), "Spine"),
    (re.compile(r"^chest$|spine2|spine_02|spine02|spine3|spine_03|thorax", re.I), "Chest"),
    (re.compile(r"upperchest|upper_chest|spine4|spine_04", re.I), "Chest"),

    # Neck / Head
    (re.compile(r"^neck\d*$", re.I), "Neck"),
    (re.compile(r"^head$", re.I), "Head"),

    # ---- LEFT FINGERS (before LeftHand / LeftArm patterns) ------------------
    # Thumb
    (re.compile(r"leftthumbproximal|left.*thumb.*(proximal|_1\b|\b1\b)|lefthandthumb1|lthumb1|leftthumb1", re.I), "LeftThumb1"),
    (re.compile(r"leftthumbintermediate|left.*thumb.*(intermediate|_2\b|\b2\b)|lefthandthumb2|lthumb2|leftthumb2", re.I), "LeftThumb2"),
    (re.compile(r"leftthumbdistal|left.*thumb.*(distal|_3\b|\b3\b)|lefthandthumb3|lthumb3|leftthumb3", re.I), "LeftThumb3"),
    # Index
    (re.compile(r"leftindexproximal|left.*index.*(proximal|_1\b|\b1\b)|lefthandindex1|lindex1|leftindex1", re.I), "LeftIndex1"),
    (re.compile(r"leftindexintermediate|left.*index.*(intermediate|_2\b|\b2\b)|lefthandindex2|lindex2|leftindex2", re.I), "LeftIndex2"),
    (re.compile(r"leftindexdistal|left.*index.*(distal|_3\b|\b3\b)|lefthandindex3|lindex3|leftindex3", re.I), "LeftIndex3"),
    # Middle
    (re.compile(r"leftmiddleproximal|left.*middle.*(proximal|_1\b|\b1\b)|lefthandmiddle1|lmiddle1|leftmiddle1", re.I), "LeftMiddle1"),
    (re.compile(r"leftmiddleintermediate|left.*middle.*(intermediate|_2\b|\b2\b)|lefthandmiddle2|lmiddle2|leftmiddle2", re.I), "LeftMiddle2"),
    (re.compile(r"leftmiddledistal|left.*middle.*(distal|_3\b|\b3\b)|lefthandmiddle3|lmiddle3|leftmiddle3", re.I), "LeftMiddle3"),
    # Ring
    (re.compile(r"leftringproximal|left.*ring.*(proximal|_1\b|\b1\b)|lefthandring1|lring1|leftring1", re.I), "LeftRing1"),
    (re.compile(r"leftringintermediate|left.*ring.*(intermediate|_2\b|\b2\b)|lefthandring2|lring2|leftring2", re.I), "LeftRing2"),
    (re.compile(r"leftringdistal|left.*ring.*(distal|_3\b|\b3\b)|lefthandring3|lring3|leftring3", re.I), "LeftRing3"),
    # Little / Pinky
    (re.compile(r"leftlittleproximal|leftpinkyproximal|left.*(little|pinky).*(proximal|_1\b|\b1\b)|lefthandpinky1|leftlittle1|leftpinky1", re.I), "LeftLittle1"),
    (re.compile(r"leftlittleintermediate|leftpinkyintermediate|left.*(little|pinky).*(intermediate|_2\b|\b2\b)|lefthandpinky2|leftlittle2|leftpinky2", re.I), "LeftLittle2"),
    (re.compile(r"leftlittledistal|leftpinkydistal|left.*(little|pinky).*(distal|_3\b|\b3\b)|lefthandpinky3|leftlittle3|leftpinky3", re.I), "LeftLittle3"),

    # ---- RIGHT FINGERS -------------------------------------------------------
    # Thumb
    (re.compile(r"rightthumbproximal|right.*thumb.*(proximal|_1\b|\b1\b)|righthandthumb1|rthumb1|rightthumb1", re.I), "RightThumb1"),
    (re.compile(r"rightthumbintermediate|right.*thumb.*(intermediate|_2\b|\b2\b)|righthandthumb2|rthumb2|rightthumb2", re.I), "RightThumb2"),
    (re.compile(r"rightthumbdistal|right.*thumb.*(distal|_3\b|\b3\b)|righthandthumb3|rthumb3|rightthumb3", re.I), "RightThumb3"),
    # Index
    (re.compile(r"rightindexproximal|right.*index.*(proximal|_1\b|\b1\b)|righthandindex1|rindex1|rightindex1", re.I), "RightIndex1"),
    (re.compile(r"rightindexintermediate|right.*index.*(intermediate|_2\b|\b2\b)|righthandindex2|rindex2|rightindex2", re.I), "RightIndex2"),
    (re.compile(r"rightindexdistal|right.*index.*(distal|_3\b|\b3\b)|righthandindex3|rindex3|rightindex3", re.I), "RightIndex3"),
    # Middle
    (re.compile(r"rightmiddleproximal|right.*middle.*(proximal|_1\b|\b1\b)|righthandmiddle1|rmiddle1|rightmiddle1", re.I), "RightMiddle1"),
    (re.compile(r"rightmiddleintermediate|right.*middle.*(intermediate|_2\b|\b2\b)|righthandmiddle2|rmiddle2|rightmiddle2", re.I), "RightMiddle2"),
    (re.compile(r"rightmiddledistal|right.*middle.*(distal|_3\b|\b3\b)|righthandmiddle3|rmiddle3|rightmiddle3", re.I), "RightMiddle3"),
    # Ring
    (re.compile(r"rightringproximal|right.*ring.*(proximal|_1\b|\b1\b)|righthandring1|rring1|rightring1", re.I), "RightRing1"),
    (re.compile(r"rightringintermediate|right.*ring.*(intermediate|_2\b|\b2\b)|righthandring2|rring2|rightring2", re.I), "RightRing2"),
    (re.compile(r"rightringdistal|right.*ring.*(distal|_3\b|\b3\b)|righthandring3|rring3|rightring3", re.I), "RightRing3"),
    # Little / Pinky
    (re.compile(r"rightlittleproximal|rightpinkyproximal|right.*(little|pinky).*(proximal|_1\b|\b1\b)|righthandpinky1|rightlittle1|rightpinky1", re.I), "RightLittle1"),
    (re.compile(r"rightlittleintermediate|rightpinkyintermediate|right.*(little|pinky).*(intermediate|_2\b|\b2\b)|righthandpinky2|rightlittle2|rightpinky2", re.I), "RightLittle2"),
    (re.compile(r"rightlittledistal|rightpinkydistal|right.*(little|pinky).*(distal|_3\b|\b3\b)|righthandpinky3|rightlittle3|rightpinky3", re.I), "RightLittle3"),

    # ---- LEFT ARM -- ForeArm/LowerArm before UpperArm, Hand last ------------
    (re.compile(r"^leftshoulder$|left.*shoulder", re.I), "LeftShoulder"),
    # ForeArm: BVH = "LeftForeArm", Unity = "LeftLowerArm"
    (re.compile(r"^leftforearm$|^leftlowerarm$|left.*(forearm|lowerarm|lower_arm|fore_arm)", re.I), "LeftForeArm"),
    # UpperArm: BVH = "LeftUpperArm" or "LeftArm", Unity = "LeftUpperArm"
    (re.compile(r"^leftupperarm$|^leftarm$|left.*(upperarm|upper_arm)", re.I), "LeftUpperArm"),
    (re.compile(r"^lefthand$", re.I), "LeftHand"),

    # ---- RIGHT ARM ----------------------------------------------------------
    (re.compile(r"^rightshoulder$|right.*shoulder", re.I), "RightShoulder"),
    # ForeArm: BVH = "RightForeArm", Unity = "RightLowerArm"
    (re.compile(r"^rightforearm$|^rightlowerarm$|right.*(forearm|lowerarm|lower_arm|fore_arm)", re.I), "RightForeArm"),
    # UpperArm: BVH = "RightUpperArm" or "RightArm", Unity = "RightUpperArm"
    (re.compile(r"^rightupperarm$|^rightarm$|right.*(upperarm|upper_arm)", re.I), "RightUpperArm"),
    (re.compile(r"^righthand$", re.I), "RightHand"),
]


def map_bvh_to_canonical(bvh_name: str) -> str | None:
    """Return the canonical pipeline bone name for a BVH joint name, or None."""
    for pattern, canonical in _BVH_TO_CANONICAL:
        if pattern.search(bvh_name):
            return canonical
    return None


def build_bvh_bone_mapping(
    bvh_data: BVHData,
    bone_map: dict[str, str],
    direct_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {bvh_joint_name: avatar_bone_name} using canonical mapping as bridge.

    The canonical names are the keys of bone_map; the values are avatar bone names.
    Duplicate avatar targets are not permitted -- first match wins.
    """
    canonical_to_avatar = bone_map  # canonical -> avatar
    direct_map = direct_map or {}
    result: dict[str, str] = {}
    used_avatar: set[str] = set()
    unmapped: list[str] = []

    for joint in bvh_data.joints:
        avatar_bone = direct_map.get(joint.name)
        if avatar_bone is None:
            canonical = map_bvh_to_canonical(joint.name)
            if canonical is None:
                unmapped.append(joint.name)
                continue
            avatar_bone = canonical_to_avatar.get(canonical)
        if avatar_bone is None:
            unmapped.append(joint.name)
            continue
        if avatar_bone in used_avatar:
            # Duplicate mapping -- skip extra joints mapping to same target
            continue
        used_avatar.add(avatar_bone)
        result[joint.name] = avatar_bone

    if unmapped:
        import sys
        print(f"[BVH] Unmapped BVH joints (no avatar target): {unmapped}", file=sys.stderr)

    return result


def validate_bvh_skeleton(bvh_data: BVHData, bone_map: dict[str, str]) -> list[str]:
    """Check if the BVH file has the minimum required joints for retargeting.

    Returns a list of warning/error strings. Empty list = OK.
    """
    issues: list[str] = []
    bvh_joint_names = {j.name for j in bvh_data.joints}

    # Required canonical joints for a valid upper-body retarget
    required_canonical = {
        "Hips", "Spine", "Chest",
        "LeftUpperArm", "LeftForeArm", "LeftHand",
        "RightUpperArm", "RightForeArm", "RightHand",
    }

    missing_canonical: list[str] = []
    for canonical in required_canonical:
        found = any(map_bvh_to_canonical(j) == canonical for j in bvh_joint_names)
        if not found:
            missing_canonical.append(canonical)

    if missing_canonical:
        issues.append(
            f"BVH skeleton is missing canonical joints required for retargeting: {missing_canonical}"
        )

    if bvh_data.frame_count == 0:
        issues.append("BVH file contains zero motion frames.")

    if bvh_data.frame_time <= 0:
        issues.append(f"BVH frame_time is non-positive: {bvh_data.frame_time}")

    # Check for duplicate joint names
    names = [j.name for j in bvh_data.joints]
    seen: set[str] = set()
    duplicates = [n for n in names if n in seen or seen.add(n)]  # type: ignore[func-returns-value]
    if duplicates:
        issues.append(f"BVH file contains duplicate joint names: {duplicates}")

    return issues
