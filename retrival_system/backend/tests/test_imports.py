"""Durable import recovery uses actual metadata and GLBs, never mocked registration."""
# ruff: noqa: F811 -- module-scoped PostgreSQL fixture reuse.

import hashlib
import json
import os
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.imports import (
    _claim,
    _finish,
    control_import,
    create_import,
    import_status,
    process_one,
    run_imports,
)
from app.main import create_app
from app.storage import StorageError

LIBRARY = Path(__file__).resolve().parents[2] / "metadata_json and glb"


def source_fixture(tmp_path, names=("Train", "1_One", "Zero")):
    root = tmp_path / "source"
    (root / "metadata").mkdir(parents=True)
    (root / "glb").mkdir()
    for name in names:
        (root / "metadata" / f"{name}.metadata.json").write_bytes(
            (LIBRARY / "metadata" / f"{name}.metadata.json").read_bytes()
        )
        # Links are read-only test inputs: no test writes or unlinks original source files.
        os.link(LIBRARY / "glb" / f"{name}.glb", root / "glb" / f"{name}.glb")
    return root


def new_job(sessions, settings, request_id=None):
    with sessions() as session:
        return create_import(
            session, settings, "library", request_id or uuid4(), "import-test-admin"
        )


def report(sessions, job_id):
    with sessions() as session:
        return import_status(session, UUID(str(job_id)))


def test_import_pause_resume_idempotency_and_bad_item_isolation(registry_database, tmp_path):
    settings, engine, sessions = registry_database
    root = source_fixture(tmp_path)
    (root / "metadata/broken.metadata.json").write_text('{"not-valid":')
    configured = settings.model_copy(update={"import_roots": {"library": root}})
    request_id = uuid4()
    job = new_job(sessions, configured, request_id)
    job_id = UUID(job["job_id"])
    assert new_job(sessions, configured, request_id)["job_id"] == str(job_id)
    run_imports(sessions, configured, workers=1, max_items=1, job_id=job_id)
    first = report(sessions, job_id)
    assert first["counts"]["staged"] == 1
    assert first["counts"]["failed"] == 1
    with sessions() as session:
        control_import(session, job_id, "test", pause=True)
    assert not process_one(sessions, configured, job_id=job_id)
    with sessions() as session:
        control_import(session, job_id, "test")
    run_imports(sessions, configured, workers=2, job_id=job_id)
    complete = report(sessions, job_id)
    assert complete["state"] == "COMPLETE"
    assert complete["counts"]["staged"] == complete["counts"]["pending_review"] == 3
    assert complete["counts"]["eligible"] == complete["counts"]["active"] == 0
    assert complete["counts"]["failed"] == 1
    assert run_imports(sessions, configured, workers=2, job_id=job_id) == 0
    repeat = UUID(new_job(sessions, configured)["job_id"])
    run_imports(sessions, configured, workers=2, job_id=repeat)
    rerun = report(sessions, repeat)
    assert rerun["counts"]["unchanged"] == 3 and rerun["counts"]["staged"] == 0
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM motion_versions")) == 3
        assert (
            connection.scalar(
                text("SELECT count(*) FROM admin_audit_logs WHERE action='MOTION_STAGED'")
            )
            == 3
        )


def test_crashed_worker_lease_is_recovered_and_stale_completion_is_fenced(
    registry_database, tmp_path
):
    settings, engine, sessions = registry_database
    configured = settings.model_copy(
        update={"import_roots": {"library": source_fixture(tmp_path, ("Train",))}}
    )
    job_id = UUID(new_job(sessions, configured)["job_id"])
    abandoned = _claim(sessions, job_id)
    assert abandoned
    assert _claim(sessions, job_id) is None
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE import_items SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": abandoned["item_id"]},
        )
    assert process_one(sessions, configured, job_id=job_id)
    finished = report(sessions, job_id)
    assert finished["state"] == "COMPLETE" and finished["items"][0]["attempts"] == 2
    assert finished["items"][0]["state"] == "UNCHANGED"
    assert not _finish(sessions, abandoned, failure=("OBSOLETE", "stale completion", False))
    assert report(sessions, job_id)["items"][0]["state"] == "UNCHANGED"


def test_worker_crash_after_catalog_commit_resumes_without_duplicate(
    registry_database, tmp_path, monkeypatch
):
    from app import imports

    settings, engine, sessions = registry_database
    configured = settings.model_copy(
        update={"import_roots": {"library": source_fixture(tmp_path, ("Train",))}}
    )
    job_id = UUID(new_job(sessions, configured)["job_id"])
    original = imports._finish

    def crash(*args, **kwargs):
        raise RuntimeError("isolated crash after registration commit")

    monkeypatch.setattr(imports, "_finish", crash)
    with pytest.raises(RuntimeError):
        process_one(sessions, configured, job_id=job_id)
    monkeypatch.setattr(imports, "_finish", original)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE import_items SET lease_until=now()-interval '1 second' WHERE job_id=:id"),
            {"id": job_id},
        )
        versions_before = connection.scalar(text("SELECT count(*) FROM motion_versions"))
    process_one(sessions, configured, job_id=job_id)
    assert report(sessions, job_id)["items"][0]["state"] == "UNCHANGED"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM motion_versions")) == versions_before


def test_discovery_snapshot_rejects_changed_metadata_and_asset_bytes(registry_database, tmp_path):
    settings, _, sessions = registry_database
    root = source_fixture(tmp_path, ("Train", "Zero"))
    configured = settings.model_copy(update={"import_roots": {"library": root}})
    job_id = UUID(new_job(sessions, configured)["job_id"])
    metadata = root / "metadata/Train.metadata.json"
    metadata.write_bytes(metadata.read_bytes() + b" ")
    # Replace the test link, preserving the source inode and bytes.
    (root / "glb/Zero.glb").unlink()
    (root / "glb/Zero.glb").write_bytes(b"invalid changed input")
    run_imports(sessions, configured, workers=2, job_id=job_id)
    outcome = report(sessions, job_id)
    assert outcome["state"] == "COMPLETE" and outcome["counts"]["failed"] == 2
    assert {item["error_code"] for item in outcome["items"]} == {
        "SOURCE_CHANGED",
        "ASSET_OR_METADATA_INVALID",
    }


def test_retries_are_bounded_and_require_deliberate_resume(
    registry_database, tmp_path, monkeypatch
):
    from app import imports

    settings, engine, sessions = registry_database
    configured = settings.model_copy(
        update={"import_roots": {"library": source_fixture(tmp_path, ("Train",))}}
    )
    job_id = UUID(new_job(sessions, configured)["job_id"])
    original = imports.stage_motion

    def storage_failure(*args, **kwargs):
        raise StorageError("injected transient storage outage")

    monkeypatch.setattr(imports, "stage_motion", storage_failure)
    for _attempt in range(3):
        assert process_one(sessions, configured, job_id=job_id)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE import_items SET available_at=now() WHERE job_id=:id"), {"id": job_id}
            )
    failed = report(sessions, job_id)
    assert failed["state"] == "COMPLETE" and failed["items"][0]["attempts"] == 3
    assert failed["items"][0]["state"] == "FAILED"
    assert not process_one(sessions, configured, job_id=job_id)
    monkeypatch.setattr(imports, "stage_motion", original)
    with sessions() as session:
        control_import(session, job_id, "test", retry_failed=True)
    assert process_one(sessions, configured, job_id=job_id)
    assert report(sessions, job_id)["items"][0]["state"] == "UNCHANGED"


def test_import_creation_race_and_admin_source_boundary(registry_database, tmp_path):
    settings, _, sessions = registry_database
    root = source_fixture(tmp_path, ("Train",))
    configured = settings.model_copy(update={"import_roots": {"library": root}})
    request_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: new_job(sessions, configured, request_id), range(2)))
    assert jobs[0]["job_id"] == jobs[1]["job_id"]
    tokens, principals = {}, {}
    for role in ("admin", "reviewer", "operator", "display"):
        token = secrets.token_hex(32)
        tokens[role] = {"Authorization": f"Bearer {token}"}
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=role, roles={role}, expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    with TestClient(create_app(configured.model_copy(update={"principals": principals}))) as client:
        body = {"source_alias": "library", "request_id": str(uuid4())}
        for role in ("reviewer", "operator", "display"):
            assert (
                client.post("/api/v1/admin/imports", headers=tokens[role], json=body).status_code
                == 403
            )
        assert client.post("/api/v1/admin/imports", json=body).status_code == 401
        assert (
            client.post(
                "/api/v1/admin/imports",
                headers=tokens["admin"],
                json={**body, "source_alias": "../../private"},
            ).status_code
            == 422
        )
        response = client.post("/api/v1/admin/imports", headers=tokens["admin"], json=body)
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert (
            str(root)
            not in client.get(f"/api/v1/admin/imports/{job_id}", headers=tokens["admin"]).text
        )
        assert (
            client.post(f"/api/v1/admin/imports/{job_id}/pause", headers=tokens["admin"]).json()[
                "state"
            ]
            == "PAUSED"
        )
        assert (
            client.post(
                f"/api/v1/admin/imports/{job_id}/resume", headers=tokens["admin"], json={}
            ).status_code
            == 200
        )


def test_worker_cli_resumes_persisted_work_in_a_fresh_process(registry_database, tmp_path):
    settings, _, sessions = registry_database
    root = source_fixture(tmp_path, ("Train",))
    configured = settings.model_copy(update={"import_roots": {"library": root}})
    job_id = UUID(new_job(sessions, configured)["job_id"])
    environment = {
        **os.environ,
        "SIGNORA_DATABASE_URL": settings.database_url.get_secret_value(),
        "SIGNORA_STORAGE_ROOT": str(settings.storage_root),
        "SIGNORA_IMPORT_ROOTS": json.dumps({"library": str(root)}),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.worker", "--once", "--workers", "1"],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, "Worker process failed; no credentials are emitted"
    assert report(sessions, job_id)["state"] == "COMPLETE"
    assert report(sessions, job_id)["items"][0]["state"] == "UNCHANGED"
