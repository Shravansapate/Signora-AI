import copy
import hashlib
import json
import struct

import pytest

from app.assets.glb import GlbValidationError, inspect_glb


@pytest.fixture
def animation():
    document = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "scene": 0,
        "nodes": [{"name": "Hand"}],
        "buffers": [{"byteLength": 32}],
        "bufferViews": [
            {"buffer": 0, "byteLength": 8},
            {"buffer": 0, "byteOffset": 8, "byteLength": 24},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "SCALAR", "count": 2},
            {"bufferView": 1, "componentType": 5126, "type": "VEC3", "count": 2},
        ],
        "animations": [
            {
                "name": "HELD_HAND",
                "samplers": [{"input": 0, "output": 1}],
                "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}],
            }
        ],
    }
    binary = struct.pack("<8f", 0.04, 1.04, 1, 2, 3, 1, 2, 3)
    return document, binary


def write_glb(path, document, binary):
    data = json.dumps(document).encode()
    data += b" " * (-len(data) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(data), 0x4E4F534A) + data
    chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks)
    return path


def test_held_hand_valid_and_duration_uses_playback_time_domain(tmp_path, animation):
    path = write_glb(tmp_path / "held.glb", *animation)
    before = path.read_bytes()
    result = inspect_glb(path)
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    assert result["size_bytes"] == len(before)
    assert result["clips"][0]["duration_seconds"] == pytest.approx(1.04)
    assert result["clips"][0]["start_seconds"] == pytest.approx(0.04)
    assert result["clips"][0]["channel_count"] == 1
    assert result["linguistic_approval"] == "NOT_EVALUATED"
    assert path.read_bytes() == before


@pytest.mark.parametrize("values", [(0.1, 0.1), (-1.0, 1.0), (0.0, float("nan"))])
def test_invalid_timestamps_fail(tmp_path, animation, values):
    document, binary = animation
    binary = struct.pack("<2f", *values) + binary[8:]
    with pytest.raises(GlbValidationError):
        inspect_glb(write_glb(tmp_path / "invalid.glb", document, binary))


@pytest.mark.parametrize(
    "names",
    [["Hand", "Hand"], ["mix:Hand", "mixHand"], ["Hand.Finger", "HandFinger"], ["a b", "a_b"]],
)
def test_ambiguous_binding_names_fail(tmp_path, animation, names):
    document, binary = animation
    document["nodes"] = [{"name": name} for name in names]
    with pytest.raises(GlbValidationError, match="ambiguous|sanitization"):
        inspect_glb(write_glb(tmp_path / "duplicate.glb", document, binary))


@pytest.mark.parametrize("resource", ["buffers", "images"])
def test_external_resources_never_loaded(tmp_path, animation, resource):
    document, binary = animation
    if resource == "buffers":
        document["buffers"][0]["uri"] = "https://127.0.0.1/private"
    else:
        document["images"] = [{"uri": "secret.png"}]
    with pytest.raises(GlbValidationError, match="embedded"):
        inspect_glb(write_glb(tmp_path / "external.glb", document, binary))


def test_corrupt_container_rejected(tmp_path, animation):
    path = write_glb(tmp_path / "short.glb", *animation)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(GlbValidationError, match="length"):
        inspect_glb(path)


def test_buffer_accessor_overrun_rejected(tmp_path, animation):
    document, binary = animation
    document["accessors"][1]["count"] = 3
    with pytest.raises(GlbValidationError, match="exceeds"):
        inspect_glb(write_glb(tmp_path / "overrun.glb", document, binary))


def test_nonfinite_motion_rejected(tmp_path, animation):
    document, binary = animation
    binary = binary[:8] + struct.pack("<6f", float("inf"), 0, 0, 0, 0, 0)
    with pytest.raises(GlbValidationError, match="Nonfinite"):
        inspect_glb(write_glb(tmp_path / "nonfinite.glb", document, binary))


def test_rest_pose_changes_fingerprint(tmp_path, animation):
    document, binary = animation
    changed = copy.deepcopy(document)
    changed["nodes"][0]["translation"] = [1, 0, 0]
    original = inspect_glb(write_glb(tmp_path / "original.glb", document, binary))
    other = inspect_glb(write_glb(tmp_path / "other.glb", changed, binary))
    assert original["rig_fingerprint"] != other["rig_fingerprint"]


def test_hierarchy_cycle_rejected(tmp_path, animation):
    document, binary = animation
    document["nodes"][0]["children"] = [0]
    with pytest.raises(GlbValidationError, match="Cycle"):
        inspect_glb(write_glb(tmp_path / "cycle.glb", document, binary))


def test_duplicate_animation_target_rejected(tmp_path, animation):
    document, binary = animation
    document["animations"][0]["channels"] *= 2
    with pytest.raises(GlbValidationError, match="Duplicate animation target"):
        inspect_glb(write_glb(tmp_path / "duplicate.glb", document, binary))
