import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.assets.metadata import SourceMetadata, load_metadata, metadata_findings, resolve_asset


@pytest.fixture
def metadata():
    # Synthetic schema fixture; never imported into the supplied asset library.
    return {
        "metadata_schema_version": "3.1",
        "motion_identity": {
            "motion_code": "ISL_TEST_01",
            "gloss": "TEST",
            "canonical_text": "test",
            "language_code": "ISL",
            "level": "WORD",
            "variant_no": 1,
        },
        "assets": {
            "final_glb": {
                "relative_path": "output/TEST/test.glb",
                "sha256": "a" * 64,
                "size_bytes": 4,
            }
        },
        "file_integrity": {"glb_sha256": "a" * 64, "glb_size_bytes": 4},
        "animation": {
            "animation_name": "TEST",
            "animation_count": 1,
            "duration_seconds": 1.2,
        },
    }


def test_identity_comes_from_metadata_not_filename(metadata, tmp_path):
    path = tmp_path / "unrelated.metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    parsed, digest, raw = load_metadata(path)
    assert parsed.motion_identity.motion_code == "ISL_TEST_01"
    assert len(digest) == 64
    assert raw == metadata


def test_false_pending_fields_stay_false_pending(metadata):
    parsed = SourceMetadata.model_validate(metadata)
    findings = metadata_findings(parsed, metadata, "a" * 64, 4)
    assert not parsed.production_eligible
    assert not parsed.isl_validation.isl_verified
    assert "SOURCE_REVIEW_PENDING" in {f["code"] for f in findings}
    assert not any(f["severity"] == "error" for f in findings)


def test_wrong_language_and_string_boolean_rejected(metadata):
    wrong_language = copy.deepcopy(metadata)
    wrong_language["motion_identity"]["language_code"] = "ASL"
    with pytest.raises(ValidationError):
        SourceMetadata.model_validate(wrong_language)
    metadata["production_eligible"] = "false"
    with pytest.raises(ValidationError):
        SourceMetadata.model_validate(metadata)


@pytest.mark.parametrize(
    "retrieval", [None, [], "aliases", {"aliases": "train"}, {"aliases": [12]}]
)
def test_malformed_retrieval_metadata_rejected(metadata, retrieval):
    metadata["retrieval"] = retrieval
    with pytest.raises(ValidationError):
        SourceMetadata.model_validate(metadata)


def test_conflicting_exact_hashes_and_sizes_reported(metadata):
    parsed = SourceMetadata.model_validate(metadata)
    findings = metadata_findings(parsed, metadata, "b" * 64, 12)
    assert sum(f["code"] == "ASSET_HASH_MISMATCH" for f in findings) == 2
    assert sum(f["code"] == "ASSET_SIZE_MISMATCH" for f in findings) == 2


@pytest.mark.parametrize(
    "path",
    ["../test.glb", "x/../test.glb", "C:/test.glb", "/test.glb", "test.glb:stream", "x//test.glb"],
)
def test_metadata_cannot_escape_asset_root(metadata, tmp_path, path):
    metadata["assets"]["final_glb"]["relative_path"] = path
    with pytest.raises(ValueError):
        resolve_asset(SourceMetadata.model_validate(metadata), tmp_path)


def test_old_converter_directories_not_followed(metadata, tmp_path):
    target = tmp_path / "test.glb"
    target.write_bytes(b"test")
    resolved = resolve_asset(SourceMetadata.model_validate(metadata), tmp_path)
    assert resolved == target.resolve()


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a": NaN}'])
def test_ambiguous_json_rejected(tmp_path, data):
    path = tmp_path / "invalid.json"
    path.write_text(data)
    with pytest.raises(ValueError):
        load_metadata(path)


def test_supplied_schema_projects_without_mutation():
    root = Path(__file__).resolve().parents[2] / "metadata_json and glb" / "metadata"
    if not root.is_dir():
        pytest.skip("Supplied library not available in this checkout")
    for path in root.glob("*.metadata.json"):
        parsed, digest, raw = load_metadata(path)
        assert parsed.motion_identity.motion_code == raw["motion_identity"]["motion_code"]
        assert parsed.isl_validation.isl_verified == raw["isl_validation"]["isl_verified"]
        assert len(digest) == 64
