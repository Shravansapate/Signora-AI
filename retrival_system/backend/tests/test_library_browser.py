"""Full maintenance UI with real uploads and the durable cleanup worker, isolated from user data."""

# ruff: noqa: F811
import json
import os
from uuid import UUID

import pytest
from sqlalchemy import select
from test_browser_playback import run_browser
from test_lifecycle import LIBRARY, variant
from test_registry import registry_database  # noqa: F401

from app.models import MotionVersion, Station
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in browser")
def test_continuous_library_maintenance(registry_database, tmp_path):
    settings, _, sessions = registry_database
    settings = settings.model_copy(update={"demo_mode_enabled": True})
    store = LocalAssetStore(settings.storage_root)
    with sessions() as session:
        stage_motion(
            LIBRARY / "metadata/Train.metadata.json",
            LIBRARY / "glb/Train.glb",
            "fixture",
            session,
            store,
        )
    with sessions() as session, session.begin():
        session.add(
            Station(
                id="TEST", revision=1, definition={"name": "Test station", "platforms": ["1", "2"]}
            )
        )
    fixture = {}
    for name, label, level in (
        ("word", "browserharbour", "WORD"),
        ("replacement", "browserharbour", "WORD"),
        ("phrase", "browser coast", "PHRASE"),
        ("place", "browserport", "WORD"),
    ):
        key = "BROWSER_WORD" if name in ("word", "replacement") else "BROWSER_" + name.upper()
        metadata, asset = variant(tmp_path, key, marker=name)
        raw = json.loads(metadata.read_text())
        raw["motion_identity"].update(canonical_text=label, gloss=label.upper(), level=level)
        raw["retrieval"]["aliases"] = []
        metadata.write_text(json.dumps(raw))
        fixture[name] = {
            "metadata": str(metadata),
            "motion": str(asset),
            "key": key,
            "label": label,
        }
    run_browser(
        settings,
        tmp_path,
        script="verify-library-maintenance.mjs",
        announcements=True,
        workspaces=fixture,
        worker=True,
    )
    evidence = json.loads((tmp_path / "browser-evidence/verification.json").read_text())
    with sessions() as session:
        for value in evidence["deleted_versions"]:
            motion = session.get(MotionVersion, UUID(value))
            assert motion.deleted_at and motion.source_metadata == {}
            assert not store.resolve(motion.storage_key).exists()
        assert session.get(Station, "TEST").definition["places"] == []
        assert session.get(Station, "TEST").definition["trains"] == []
        assert len(list(session.scalars(select(MotionVersion)))) >= 5
    assert all(
        os.path.isfile(f[field]) for f in fixture.values() for field in ("metadata", "motion")
    )
