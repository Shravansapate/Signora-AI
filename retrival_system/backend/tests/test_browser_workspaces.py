"""Authorized interface workflows against real PostgreSQL, GLBs and production Next.js."""
# ruff: noqa: F811 -- isolated module fixture reuse.

import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_announcements import announcement_setup, approved_template  # noqa: F401
from test_browser_playback import run_browser
from test_imports import source_fixture
from test_lifecycle import LIBRARY, lifecycle_clients, variant  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.imports import create_import, run_imports


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real browser")
def test_complete_role_workspaces(registry_database, approved_template, tmp_path):
    settings, _, sessions = registry_database
    source = source_fixture(tmp_path, names=("Train",))
    (source / "metadata/broken.metadata.json").write_text("{broken")
    configured = settings.model_copy(update={"import_roots": {"workspace-library": source}})
    with sessions() as session:
        job = create_import(
            session, configured, "workspace-library", uuid4(), "synthetic-workspace-fixture"
        )
    run_imports(sessions, configured, workers=1, job_id=UUID(job["job_id"]))
    original = json.loads(
        (LIBRARY / "metadata/Train.metadata.json").read_text(encoding="utf-8-sig")
    )
    metadata, asset = variant(
        tmp_path, original["motion_identity"]["motion_code"], marker="workspace-quality-candidate"
    )
    did = uuid4()
    identity = Principal(
        subject="workspace-display",
        roles={"display"},
        station_ids={"TEST"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    run_browser(
        configured,
        tmp_path,
        script="verify-browser-workspaces.mjs",
        announcements=True,
        live_display=(did, secrets.token_hex(32), identity),
        workspaces={"metadata": str(metadata), "motion": str(asset), "import_job": job["job_id"]},
    )
