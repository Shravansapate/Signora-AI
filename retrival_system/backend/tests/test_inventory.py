import json
from pathlib import Path

import pytest

from app.assets import inventory


def create_library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    (library / "metadata").mkdir(parents=True)
    (library / "glb").mkdir()
    return library


def test_corrupt_metadata_does_not_abort_other_items(tmp_path, monkeypatch):
    library = create_library(tmp_path)
    for name in ("good", "bad"):
        (library / "metadata" / f"{name}.metadata.json").write_text("{}")
        (library / "glb" / f"{name}.glb").write_bytes(b"test")
    original = inventory.inspect_item

    def inspect(path, glb_root, run_khronos):
        if path.name.startswith("bad"):
            return original(path, glb_root, False)
        return {
            "metadata_file": path.name,
            "status": "INSPECTED",
            "findings": [],
            "identity": {"motion_code": "ISL_GOOD_01", "level": "WORD"},
            "asset_file": "good.glb",
            "technical": {"size_bytes": 4},
        }

    monkeypatch.setattr(inventory, "inspect_item", inspect)
    report = inventory.build_inventory(library, workers=2, run_khronos=False)
    assert report["summary"]["inspected"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["unpaired_glbs"] == ["bad.glb"]
    assert report["summary"]["source_production_eligible"] == 0


def test_duplicate_identity_is_visible_on_every_affected_record(tmp_path, monkeypatch):
    library = create_library(tmp_path)
    for name in ("a", "b"):
        (library / "metadata" / f"{name}.metadata.json").write_text("{}")

    def inspect(path, glb_root, run_khronos):
        return {
            "metadata_file": path.name,
            "status": "INSPECTED",
            "findings": [],
            "identity": {"motion_code": "ISL_SAME_01", "level": "WORD"},
        }

    monkeypatch.setattr(inventory, "inspect_item", inspect)
    report = inventory.build_inventory(library)
    assert report["summary"]["failed"] == 2
    assert report["summary"]["finding_counts"]["DUPLICATE_MOTION_IDENTITY"] == 2


def test_report_writer_never_overwrites_source_metadata(tmp_path):
    library = create_library(tmp_path)
    source = library / "metadata" / "source.metadata.json"
    source.write_text("original")
    with pytest.raises(ValueError, match="outside"):
        inventory.write_report({}, source, library)
    assert source.read_text() == "original"


def test_atomic_report_and_deterministic_content_digest(tmp_path, monkeypatch):
    library = create_library(tmp_path)
    (library / "metadata" / "a.metadata.json").write_text("{}")
    monkeypatch.setattr(
        inventory,
        "inspect_item",
        lambda *args: {
            "metadata_file": "a.metadata.json",
            "status": "FAILED",
            "findings": [],
        },
    )
    report = inventory.build_inventory(library, run_khronos=False)
    second = inventory.build_inventory(library, run_khronos=False)
    assert report["inventory_sha256"] == second["inventory_sha256"]
    target = tmp_path / "report.json"
    inventory.write_report(report, target, library)
    assert json.loads(target.read_text()) == report
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("workers", [0, 5, -1])
def test_concurrency_is_bounded(tmp_path, workers):
    with pytest.raises(ValueError, match="workers"):
        inventory.build_inventory(tmp_path, workers=workers)


def test_empty_or_incorrect_library_fails_explicitly(tmp_path):
    with pytest.raises(ValueError):
        inventory.build_inventory(tmp_path)
    with pytest.raises(ValueError, match="No .metadata.json"):
        inventory.build_inventory(create_library(tmp_path))


@pytest.mark.parametrize(
    ("names", "status", "selected_name"),
    [
        (["ACTUAL"], "INSPECTED", "ACTUAL"),
        (["ACTUAL", "SECOND"], "FAILED", None),
        (["SOURCE", "SECOND"], "INSPECTED", "SOURCE"),
    ],
)
def test_clip_selection_uses_actual_unambiguous_identity(
    tmp_path, monkeypatch, names, status, selected_name
):
    from app.assets import glb

    library = create_library(tmp_path)
    (library / "glb" / "test.glb").write_bytes(b"test")
    source = library / "metadata" / "test.metadata.json"
    source.write_text(
        json.dumps(
            {
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
                        "relative_path": "output/test.glb",
                        "sha256": "a" * 64,
                        "size_bytes": 4,
                    }
                },
                "file_integrity": {"glb_sha256": "a" * 64, "glb_size_bytes": 4},
                "animation": {
                    "animation_name": "SOURCE",
                    "animation_count": 1,
                    "duration_seconds": 1.0,
                },
            }
        )
    )
    before = source.read_bytes()
    monkeypatch.setattr(
        glb,
        "inspect_glb",
        lambda path: {
            "sha256": "a" * 64,
            "size_bytes": 4,
            "clips": [{"name": name, "duration_seconds": 1.0} for name in names],
        },
    )
    result = inventory.inspect_item(source, library / "glb", run_khronos=False)
    assert result["status"] == status
    assert result.get("selected_clip", {}).get("name") == selected_name
    assert source.read_bytes() == before
