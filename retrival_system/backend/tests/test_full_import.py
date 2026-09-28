"""Opt-in full supplied-library import, restart and rerun proof in disposable PostgreSQL."""
# ruff: noqa: F811 -- pytest fixture reuse.

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from app.imports import create_import, import_status, run_imports
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.mark.skipif(
    os.environ.get("SIGNORA_TEST_FULL_IMPORT") != "1", reason="Opt-in all 149 real GLBs"
)
def test_full_library_resume_and_rerun(registry_database):
    settings, engine, sessions = registry_database
    repository = Path(__file__).resolve().parents[2]
    library = repository / "metadata_json and glb"
    configured = settings.model_copy(update={"import_roots": {"supplied": library}})
    with sessions() as session:
        stage_motion(
            library / "metadata/Train.metadata.json",
            library / "glb/Train.glb",
            "isolated-full-import",
            session,
            LocalAssetStore(settings.storage_root),
        )
    with sessions() as session:
        job_id = UUID(
            create_import(session, configured, "supplied", uuid4(), "isolated-full-import")[
                "job_id"
            ]
        )
    began = time.monotonic()
    assert run_imports(sessions, configured, workers=2, max_items=5, job_id=job_id) == 5
    with sessions() as session:
        partial = import_status(session, job_id)
    assert partial["counts"]["staged"] == 5 and partial["state"] == "RUNNING"
    # A fresh pool/session lifetime resumes durable item state.
    assert run_imports(sessions, configured, workers=2, job_id=job_id) == 144
    with sessions() as session:
        first = import_status(session, job_id)
    assert first["state"] == "COMPLETE", first["counts"]
    assert first["counts"]["staged"] == 148 and first["counts"]["unchanged"] == 1
    assert first["counts"]["pending_review"] == 149 and first["counts"]["eligible"] == 0
    assert first["counts"]["failed"] == 0
    cold_seconds = time.monotonic() - began
    with sessions() as session:
        repeated_id = UUID(
            create_import(session, configured, "supplied", uuid4(), "isolated-full-import")[
                "job_id"
            ]
        )
    began = time.monotonic()
    run_imports(sessions, configured, workers=2, job_id=repeated_id)
    with sessions() as session:
        repeated = import_status(session, repeated_id)
    assert repeated["counts"]["unchanged"] == 149 and repeated["counts"]["failed"] == 0
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM motion_versions")) == 149
        assert connection.scalar(text("SELECT count(*) FROM sign_concepts")) == 149
        assert (
            connection.scalar(
                text("SELECT count(*) FROM admin_audit_logs WHERE action='MOTION_STAGED'")
            )
            == 149
        )
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0
    result = {
        "status": "PASSED",
        "verified_at": datetime.now(UTC).isoformat(),
        "source": "149 unchanged supplied GLBs and metadata",
        "database": "disposable PostgreSQL",
        "concurrency": 2,
        "resume_after_items": 5,
        "first_import": first["counts"],
        "rerun": repeated["counts"],
        "cold_import_seconds": round(cold_seconds, 2),
        "rerun_seconds": round(time.monotonic() - began, 2),
    }
    (repository / "artifacts/phase-3-full-import.json").write_text(json.dumps(result, indent=2))
