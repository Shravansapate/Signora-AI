"""Bounded GLB inspection for the persistent-avatar contract.

This complements the Khronos validator; it is not a replacement for it or an
ISL review. Unsupported sparse/compressed payloads fail closed. No URI is fetched.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
from collections import Counter
from pathlib import Path

MAX_GLB_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 100_000
MAX_ANIMATION_VALUES = 8_000_000
_COMPONENTS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_DIMENSIONS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


class GlbValidationError(ValueError):
    """The file cannot satisfy the supported immutable animation contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GlbValidationError(message)


def _integer(value: object, label: str, minimum: int = 0) -> int:
    _require(type(value) is int and value >= minimum, f"Invalid {label}")
    return value


def _index(value: object, collection: list, label: str) -> int:
    result = _integer(value, label)
    _require(result < len(collection), f"Out-of-range {label}")
    return result


def _objects(value: object, label: str) -> list[dict]:
    _require(isinstance(value, list) and len(value) <= MAX_OBJECTS, f"Invalid {label}")
    _require(all(isinstance(item, dict) for item in value), f"Invalid {label} object")
    return value


def _vector(value: object, size: int, label: str) -> list[float]:
    _require(isinstance(value, list) and len(value) == size, f"Invalid {label}")
    _require(all(type(v) in (int, float) and math.isfinite(v) for v in value), f"Nonfinite {label}")
    return [float(v) for v in value]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise GlbValidationError(f"Nonfinite JSON number: {value}")


def _read_container(path: Path) -> tuple[dict, bytes, str, int]:
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        _require(stat.S_ISREG(before.st_mode), "Asset must be a regular file")
        _require(28 <= before.st_size <= MAX_GLB_BYTES, "GLB file size outside supported bounds")
        data = source.read(MAX_GLB_BYTES + 1)
        after = os.fstat(source.fileno())
    _require(
        len(data) == before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns,
        "Asset changed during inspection",
    )
    magic, version, length = struct.unpack_from("<4sII", data)
    _require(magic == b"glTF" and version == 2, "Expected a GLB 2 container")
    _require(length == len(data), "GLB declared length differs from file length")
    chunks = []
    offset = 12
    while offset < length:
        _require(offset + 8 <= length, "Truncated chunk header")
        size, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        _require(size % 4 == 0 and offset + size <= length, "Invalid chunk bounds/alignment")
        chunks.append((kind, offset, size))
        _require(len(chunks) <= 2, "Only JSON and BIN chunks are supported")
        offset += size
    _require(
        len(chunks) == 2 and [c[0] for c in chunks] == [0x4E4F534A, 0x004E4942],
        "Expected exactly one JSON chunk followed by one BIN chunk",
    )
    _, start, count = chunks[0]
    _require(0 < count <= MAX_JSON_BYTES, "JSON chunk outside supported bounds")
    try:
        document = json.loads(
            data[start : start + count].decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GlbValidationError("Invalid GLB JSON") from exc
    _require(isinstance(document, dict), "GLB JSON must be an object")
    _require(
        isinstance(document.get("asset"), dict) and document["asset"].get("version") == "2.0",
        "Expected glTF asset version 2.0",
    )
    _require(
        document["asset"].get("minVersion", "2.0") == "2.0", "Unsupported minimum glTF version"
    )
    _require(not document.get("extensionsRequired"), "Required extensions are not supported")
    _, start, count = chunks[1]
    return document, data[start : start + count], hashlib.sha256(data).hexdigest(), len(data)


class _Accessors:
    def __init__(self, document: dict, binary: bytes):
        self.binary = binary
        self.accessors = _objects(document.get("accessors", []), "accessors")
        self.views = _objects(document.get("bufferViews", []), "bufferViews")
        buffers = _objects(document.get("buffers", []), "buffers")
        _require(
            len(buffers) == 1 and "uri" not in buffers[0],
            "Exactly one embedded buffer is required; external/data URIs are rejected",
        )
        size = _integer(buffers[0].get("byteLength"), "buffer byteLength", 1)
        _require(size <= len(binary) <= size + 3, "BIN length differs from embedded buffer")
        for view in self.views:
            _require(view.get("buffer") == 0, "Buffer view must use the embedded buffer")
            start = _integer(view.get("byteOffset", 0), "bufferView byteOffset")
            length = _integer(view.get("byteLength"), "bufferView byteLength", 1)
            _require(start + length <= size, "Buffer view exceeds embedded buffer")
            if "byteStride" in view:
                stride = _integer(view["byteStride"], "bufferView byteStride", 4)
                _require(stride <= 252 and stride % 4 == 0, "Invalid bufferView byteStride")
            _require(not view.get("extensions"), "Compressed/extended buffer views unsupported")
        for image in _objects(document.get("images", []), "images"):
            _require("uri" not in image, "Images must be embedded; external/data URIs rejected")
            _index(image.get("bufferView"), self.views, "image bufferView")
            _require(
                image.get("mimeType") in ("image/png", "image/jpeg"),
                "Unsupported embedded image format",
            )
        self.layouts = [self._layout(a) for a in self.accessors]
        self.decoded: dict[int, list[tuple]] = {}
        self.values_read = 0

    def _layout(self, accessor: dict) -> tuple[int, int, int, str, int]:
        _require(
            "sparse" not in accessor and not accessor.get("extensions"),
            "Sparse/extended accessors are not supported by this inspector",
        )
        view = self.views[_index(accessor.get("bufferView"), self.views, "accessor bufferView")]
        component_type = accessor.get("componentType")
        _require(
            type(component_type) is int and component_type in _COMPONENTS,
            "Invalid accessor component type",
        )
        component, component_size = _COMPONENTS[component_type]
        kind = accessor.get("type")
        _require(isinstance(kind, str) and kind in _DIMENSIONS, "Invalid accessor type")
        dimensions = _DIMENSIONS[kind]
        # Small integer matrices have per-column padding; reject until explicitly supported.
        _require(
            not kind.startswith("MAT") or component_size == 4,
            "Only 32-bit matrix accessors are supported",
        )
        count = _integer(accessor.get("count"), "accessor count", 1)
        element_size = component_size * dimensions
        stride = view.get("byteStride", element_size)
        offset = _integer(accessor.get("byteOffset", 0), "accessor byteOffset")
        absolute = view.get("byteOffset", 0) + offset
        _require(
            stride >= element_size
            and offset % component_size == 0
            and absolute % component_size == 0,
            "Invalid accessor alignment/stride",
        )
        _require(
            offset + (count - 1) * stride + element_size <= view["byteLength"],
            "Accessor exceeds buffer view",
        )
        for bound in ("min", "max"):
            if bound in accessor:
                _vector(accessor[bound], dimensions, f"accessor {bound}")
        return absolute, stride, count, "<" + component * dimensions, dimensions

    def read(self, value: object, label: str) -> tuple[dict, list[tuple]]:
        index = _index(value, self.accessors, label)
        if index not in self.decoded:
            offset, stride, count, format_string, dimensions = self.layouts[index]
            self.values_read += count * dimensions
            _require(self.values_read <= MAX_ANIMATION_VALUES, "Animation decode budget exceeded")
            unpacker = struct.Struct(format_string)
            values = [unpacker.unpack_from(self.binary, offset + i * stride) for i in range(count)]
            _require(
                all(math.isfinite(v) for row in values for v in row), f"Nonfinite values in {label}"
            )
            self.decoded[index] = values
        return self.accessors[index], self.decoded[index]


def _nodes(document: dict) -> tuple[list[dict], list[dict], list[int | None]]:
    nodes = _objects(document.get("nodes", []), "nodes")
    _require(0 < len(nodes) <= 4096, "Node count outside supported bounds")
    names = [node.get("name") for node in nodes]
    _require(
        all(isinstance(name, str) and name for name in names),
        "Every node needs a stable nonempty binding name",
    )
    _require(len(set(names)) == len(names), "Duplicate node names make binding ambiguous")
    binding_names = [re.sub(r"[\[\].:/]", "", re.sub(r"\s", "_", name)) for name in names]
    _require(
        all(binding_names) and len(set(binding_names)) == len(binding_names),
        "Node names collide after Three.js binding sanitization",
    )
    parents: list[int | None] = [None] * len(nodes)
    for index, node in enumerate(nodes):
        children = node.get("children", [])
        _require(isinstance(children, list) and len(children) <= len(nodes), "Invalid children")
        for child in children:
            child = _index(child, nodes, "child node")
            _require(parents[child] is None, "Node has duplicate or multiple parents")
            parents[child] = index
    for index in range(len(nodes)):
        visited = set()
        cursor = index
        while cursor is not None:
            _require(cursor not in visited, "Cycle in node hierarchy")
            visited.add(cursor)
            cursor = parents[cursor]
    contract = []
    for index, node in enumerate(nodes):
        _require(
            "matrix" not in node
            or not any(k in node for k in ("translation", "rotation", "scale")),
            "Node combines matrix and TRS",
        )
        item = {
            "name": node["name"],
            "parent": nodes[parents[index]]["name"] if parents[index] is not None else None,
        }
        if "matrix" in node:
            item["matrix"] = _vector(node["matrix"], 16, "node matrix")
        else:
            item["translation"] = _vector(node.get("translation", [0, 0, 0]), 3, "translation")
            item["rotation"] = _vector(node.get("rotation", [0, 0, 0, 1]), 4, "rotation")
            item["scale"] = _vector(node.get("scale", [1, 1, 1]), 3, "scale")
            _require(
                abs(sum(v * v for v in item["rotation"]) - 1) < 0.002,
                "Node rotation quaternion is not normalized",
            )
        contract.append(item)
    return nodes, contract, parents


def _morphs(document: dict, nodes: list[dict]) -> dict[str, dict]:
    meshes = _objects(document.get("meshes", []), "meshes")
    result = {}
    for node in nodes:
        if "mesh" not in node:
            continue
        mesh = meshes[_index(node["mesh"], meshes, "node mesh")]
        primitives = _objects(mesh.get("primitives", []), "mesh primitives")
        _require(bool(primitives), "Mesh must contain primitives")
        counts = [len(_objects(p.get("targets", []), "morph targets")) for p in primitives]
        _require(len(set(counts)) == 1, "Inconsistent primitive morph target counts")
        count = counts[0]
        if count:
            extras = mesh.get("extras", {})
            names = extras.get("targetNames", []) if isinstance(extras, dict) else []
            _require(
                isinstance(names, list)
                and len(names) == count
                and all(isinstance(n, str) and n for n in names)
                and len(set(names)) == count,
                "Morph targets need unique mapping names",
            )
            weights = node.get("weights", mesh.get("weights", [0] * count))
            result[node["name"]] = {"names": names, "weights": _vector(weights, count, "weights")}
    return result


def _skins(document: dict, nodes: list[dict], data: _Accessors) -> list[dict]:
    skins = _objects(document.get("skins", []), "skins")
    result = []
    for skin in skins:
        joints = skin.get("joints")
        _require(isinstance(joints, list) and 0 < len(joints) <= len(nodes), "Invalid skin joints")
        indices = [_index(joint, nodes, "skin joint") for joint in joints]
        _require(len(set(indices)) == len(indices), "Duplicate skin joints")
        item = {"joints": [nodes[index]["name"] for index in indices]}
        if "skeleton" in skin:
            item["skeleton"] = nodes[_index(skin["skeleton"], nodes, "skin skeleton")]["name"]
        if "inverseBindMatrices" in skin:
            accessor, values = data.read(skin["inverseBindMatrices"], "inverse bind matrices")
            _require(
                accessor["type"] == "MAT4"
                and accessor["componentType"] == 5126
                and len(values) == len(indices),
                "Invalid inverse bind matrix accessor",
            )
            item["inverse_bind_matrices"] = values
        else:
            item["inverse_bind_matrices"] = [[1 if i % 5 == 0 else 0 for i in range(16)]] * len(
                joints
            )
        result.append(item)
    for node in nodes:
        if "skin" in node:
            _index(node["skin"], skins, "node skin")
            _require("mesh" in node, "Skinned node requires a mesh")
    return result


def _animations(
    document: dict, nodes: list[dict], parents: list[int | None], morphs: dict, data: _Accessors
) -> list[dict]:
    animations = _objects(document.get("animations", []), "animations")
    _require(0 < len(animations) <= 256, "Animation count outside supported bounds")
    names = [animation.get("name") for animation in animations]
    _require(
        all(isinstance(name, str) and name for name in names), "Animations require stable names"
    )
    _require(len(set(names)) == len(names), "Duplicate animation names")
    result = []
    for animation in animations:
        samplers = _objects(animation.get("samplers", []), "animation samplers")
        channels = _objects(animation.get("channels", []), "animation channels")
        _require(bool(channels), "Animation has no channels")
        targets, paths, root_motion = set(), Counter(), []
        end, start = 0.0, float("inf")
        for channel in channels:
            target = channel.get("target")
            _require(isinstance(target, dict), "Invalid animation target")
            node_index = _index(target.get("node"), nodes, "animation target node")
            node = nodes[node_index]
            _require("matrix" not in node, "Animated nodes require TRS transforms")
            path = target.get("path")
            _require(path in ("translation", "rotation", "scale", "weights"), "Invalid target path")
            _require((node_index, path) not in targets, "Duplicate animation target channel")
            targets.add((node_index, path))
            paths[path] += 1
            sampler = samplers[_index(channel.get("sampler"), samplers, "animation sampler")]
            interpolation = sampler.get("interpolation", "LINEAR")
            _require(
                interpolation in ("LINEAR", "STEP", "CUBICSPLINE"), "Unsupported interpolation"
            )
            input_accessor, times = data.read(sampler.get("input"), "animation input")
            _require(
                input_accessor["componentType"] == 5126
                and input_accessor["type"] == "SCALAR"
                and not input_accessor.get("normalized", False),
                "Invalid animation timeline type",
            )
            _require(
                times[0][0] >= 0
                and all(a[0] < b[0] for a, b in zip(times, times[1:], strict=False)),
                "Animation timeline must be nonnegative and strictly increasing",
            )
            output_accessor, values = data.read(sampler.get("output"), "animation output")
            expected_type = (
                "SCALAR" if path == "weights" else ("VEC4" if path == "rotation" else "VEC3")
            )
            _require(
                output_accessor["componentType"] == 5126
                and output_accessor["type"] == expected_type
                and not output_accessor.get("normalized", False),
                "Invalid animation output type",
            )
            multiplier = 3 if interpolation == "CUBICSPLINE" else 1
            width = len(morphs.get(node["name"], {}).get("names", [])) if path == "weights" else 1
            _require(
                width > 0 and len(values) == len(times) * multiplier * width,
                "Animation output count differs from timeline/target",
            )
            if path == "rotation":
                rotations = values[1::3] if interpolation == "CUBICSPLINE" else values
                _require(
                    all(abs(sum(v * v for v in row) - 1) < 0.002 for row in rotations),
                    "Animation quaternion is not normalized",
                )
            start, end = min(start, times[0][0]), max(end, times[-1][0])
            if path == "translation" and (parents[node_index] is None or "Hips" in node["name"]):
                samples = values[1::3] if interpolation == "CUBICSPLINE" else values
                extent = [
                    max(row[i] for row in samples) - min(row[i] for row in samples)
                    for i in range(3)
                ]
                root_motion.append({"node": node["name"], "translation_extent": extent})
        _require(end > 0, "Animation has no positive duration")
        result.append(
            {
                "name": animation["name"],
                "duration_seconds": end,
                "start_seconds": start,
                "channel_count": len(channels),
                "target_names": sorted({nodes[index]["name"] for index, _ in targets}),
                "paths": dict(paths),
                "root_motion": root_motion,
            }
        )
    return result


def inspect_glb(path: Path) -> dict:
    """Inspect exact bytes without editing them or interpreting metadata approval."""
    document, binary, digest, size = _read_container(path)
    data = _Accessors(document, binary)
    nodes, node_contract, parents = _nodes(document)
    morphs = _morphs(document, nodes)
    skins = _skins(document, nodes, data)
    for node, contract_node in zip(nodes, node_contract, strict=True):
        if "skin" in node:
            # File-local mesh/skin indices can change when equivalent GLBs are
            # serialized. Bind identity is the actual skin contract, not that index.
            contract_node["skin"] = skins[node["skin"]]
    clips = _animations(document, nodes, parents, morphs, data)
    contract = {
        "nodes": sorted(node_contract, key=lambda n: n["name"]),
        "skins": skins,
        "morphs": morphs,
    }
    fingerprint = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        "sha256": digest,
        "size_bytes": size,
        "clips": clips,
        "rig_fingerprint": fingerprint,
        "rig_fingerprint_version": 3,
        "node_count": len(nodes),
        "skin_joint_counts": [len(s["joints"]) for s in skins],
        "node_names": [n["name"] for n in nodes],
        "morph_targets": morphs,
        "embedded_image_count": len(document.get("images", [])),
        "embedded_image_bytes": sum(
            data.views[i["bufferView"]]["byteLength"] for i in document.get("images", [])
        ),
        "self_contained": True,
        "generator": document["asset"].get("generator"),
        "validation_scope": "SUPPORTED_CONTAINER_BINDING_AND_ANIMATION_STRUCTURE",
        "linguistic_approval": "NOT_EVALUATED",
    }
