"""Real PostgreSQL integration tests in a newly initialized isolated cluster."""

import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.config import Principal, Settings
from app.database import build_database
from app.main import create_app
from app.registry import RegistryConflict, eligible_motions, stage_motion
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def registry_database(tmp_path_factory):
    binary = Path(os.environ.get("SIGNORA_TEST_PG_BIN", "C:/Program Files/PostgreSQL/18/bin"))
    if not (binary / "initdb.exe").is_file():
        pytest.skip("PostgreSQL binaries required; set SIGNORA_TEST_PG_BIN")
    root = tmp_path_factory.mktemp("postgres")
    cluster = root / "cluster"
    password = secrets.token_hex(24)
    password_file = root / "init-password"
    password_file.write_text(password)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def run(*arguments):
        command_log = root / "commands.log"
        with command_log.open("ab") as output:
            result = subprocess.run(
                [str(binary / arguments[0]), *arguments[1:]],
                stdout=output,
                stderr=output,
                stdin=subprocess.DEVNULL,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if result.returncode:
            pytest.fail(
                f"Test PostgreSQL command failed: {arguments[0]}: "
                f"{command_log.read_text(errors='replace')[-2000:]}"
            )

    try:
        run(
            "initdb.exe",
            "-D",
            str(cluster),
            "-U",
            "signora_test",
            "--auth=scram-sha-256",
            "--pwfile",
            str(password_file),
            "--encoding=UTF8",
            "--locale=C",
        )
    finally:
        password_file.unlink(missing_ok=True)
    run(
        "pg_ctl.exe",
        "-D",
        str(cluster),
        "-l",
        str(root / "server.log"),
        "-o",
        f"-h 127.0.0.1 -p {port}",
        "-w",
        "start",
    )
    settings = Settings(
        _env_file=None,  # Never inherit a developer's persistent local identities/models.
        database_url=f"postgresql+psycopg://signora_test:{password}@127.0.0.1:{port}/postgres",
        storage_root=root / "objects",
    )
    previous = {
        key: os.environ.get(key) for key in ["SIGNORA_DATABASE_URL", "SIGNORA_STORAGE_ROOT"]
    }
    os.environ["SIGNORA_DATABASE_URL"] = settings.database_url.get_secret_value()
    os.environ["SIGNORA_STORAGE_ROOT"] = str(settings.storage_root)
    engine, sessions = build_database(settings)
    try:
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield settings, engine, sessions
    finally:
        engine.dispose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        run("pg_ctl.exe", "-D", str(cluster), "-m", "fast", "-w", "stop")


def test_migration_roundtrip_and_extensions(registry_database):
    _, engine, _ = registry_database
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0009_display_routing"
        )
        assert (
            connection.scalar(text("SELECT extname FROM pg_extension WHERE extname='pg_trgm'"))
            == "pg_trgm"
        )
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0


def test_stage_real_asset_is_idempotent_and_pending(registry_database):
    settings, engine, sessions = registry_database
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    store = LocalAssetStore(settings.storage_root)
    with sessions() as session:
        first = stage_motion(
            library / "metadata/Train.metadata.json",
            library / "glb/Train.glb",
            "test-admin",
            session,
            store,
        )
        second = stage_motion(
            library / "metadata/Train.metadata.json",
            library / "glb/Train.glb",
            "test-admin",
            session,
            store,
        )
    assert first["status"] == "STAGED" and second["status"] == "UNCHANGED"
    assert first["motion_version_id"] == second["motion_version_id"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM motion_versions")) == 1
        assert connection.scalar(text("SELECT count(*) FROM admin_audit_logs")) == 1
        assert connection.scalar(text("SELECT count(*) FROM registry_events")) == 1
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0
        assert connection.scalar(text("SELECT lifecycle_status FROM motion_versions")) == "STAGING"


def test_database_rejects_immutable_bytes_and_unapproved_activation(registry_database):
    _, engine, _ = registry_database
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE motion_versions SET sha256=:hash"), {"hash": "a" * 64})
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE motion_versions SET lifecycle_status='ACTIVE'"))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sign_concepts SET active_motion_version_id="
                    "(SELECT id FROM motion_versions LIMIT 1)"
                )
            )


def test_changed_metadata_requires_review_without_mutation(registry_database, tmp_path):
    settings, engine, sessions = registry_database
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    metadata = json.loads(
        (library / "metadata/Train.metadata.json").read_text(encoding="utf-8-sig")
    )
    metadata["linguistic"]["meaning"] = "changed"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(metadata))
    with sessions() as session, pytest.raises(RegistryConflict):
        stage_motion(
            path,
            library / "glb/Train.glb",
            "test-admin",
            session,
            LocalAssetStore(settings.storage_root),
        )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM motion_versions")) == 1
        assert connection.scalar(text("SELECT count(*) FROM admin_audit_logs")) == 1


def test_auth_roles_readiness_and_staging_validation(registry_database):
    settings, _, _ = registry_database
    admin, display = secrets.token_hex(32), secrets.token_hex(32)
    settings = settings.model_copy(
        update={
            "principals": {
                hashlib.sha256(admin.encode()).hexdigest(): Principal(
                    subject="admin",
                    roles={"admin"},
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                hashlib.sha256(display.encode()).hexdigest(): Principal(
                    subject="display",
                    roles={"display"},
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            }
        }
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/admin/signs").status_code == 401
        assert (
            client.get(
                "/api/v1/admin/signs", headers={"Authorization": f"Bearer {display}"}
            ).status_code
            == 403
        )
        response = client.get("/api/v1/admin/signs", headers={"Authorization": f"Bearer {admin}"})
        assert response.status_code == 200
        assert response.json()["items"][0]["semantic_key"] == "ISL_TRAIN_01"
        assert "X-Request-ID" in response.headers
        invalid = client.post(
            "/api/v1/admin/motions/stage",
            headers={"Authorization": f"Bearer {admin}"},
            files={
                "metadata": ("bad.json", io.BytesIO(b"{}")),
                "motion": ("bad.glb", io.BytesIO(b"bad")),
            },
        )
        assert invalid.status_code == 422
        library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
        with (library / "metadata/Train.metadata.json").open("rb") as metadata:
            with (library / "glb/Train.glb").open("rb") as motion:
                valid = client.post(
                    "/api/v1/admin/motions/stage",
                    headers={"Authorization": f"Bearer {admin}"},
                    files={
                        "metadata": ("metadata.json", metadata),
                        "motion": ("Train.glb", motion),
                    },
                )
        assert valid.status_code == 201
        assert valid.json()["status"] == "UNCHANGED"


def test_eligibility_excludes_wrong_language_rig_and_disabled(registry_database):
    settings, engine, _ = registry_database
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("UPDATE avatar_profiles SET status='APPROVED'"))
            connection.execute(
                text("""
                UPDATE sign_concepts SET enabled=true, domain='railway', meaning='test meaning',
                  context='test context', meaning_status='APPROVED'
            """)
            )
            connection.execute(
                text("""
                UPDATE motion_versions SET linguistic_review_status='APPROVED',
                  composition_review_status='APPROVED', reviewed_sha256=sha256,
                  reviewed_avatar_profile_id=avatar_profile_id, reviewer='synthetic-test-review',
                  reviewed_at=now(), lifecycle_status='ACTIVE', reviewed_semantic_revision=1
            """)
            )
            connection.execute(
                text(
                    "UPDATE sign_concepts SET active_motion_version_id="
                    "(SELECT id FROM motion_versions LIMIT 1)"
                )
            )
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 1
            store = LocalAssetStore(settings.storage_root)
            avatar = connection.scalar(text("SELECT id FROM avatar_profiles LIMIT 1"))
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                selected = eligible_motions(session, ["ISL_TRAIN_01"], avatar, store)
                assert set(selected) == {"ISL_TRAIN_01"}
                key = selected["ISL_TRAIN_01"]["storage_key"]
                checksum = selected["ISL_TRAIN_01"]["sha256"]
                generated_object = store.resolve(key)
                assert generated_object.is_relative_to(settings.storage_root.resolve())
                generated_object.unlink()
                try:
                    assert eligible_motions(session, ["ISL_TRAIN_01"], avatar, store) == {}
                finally:
                    source = (
                        Path(__file__).resolve().parents[2] / "metadata_json and glb/glb/Train.glb"
                    )
                    with source.open("rb") as original:
                        store.put(original, checksum)
            for change, restore in [
                (
                    "UPDATE sign_concepts SET language_code='ASL'",
                    "UPDATE sign_concepts SET language_code='ISL'",
                ),
                ("UPDATE sign_concepts SET enabled=false", "UPDATE sign_concepts SET enabled=true"),
                (
                    "UPDATE avatar_profiles SET status='RETIRED'",
                    "UPDATE avatar_profiles SET status='APPROVED'",
                ),
            ]:
                connection.execute(text(change))
                assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0
                connection.execute(text(restore))
        finally:
            transaction.rollback()


def test_concurrent_registration_commits_one_version_and_audit(registry_database, tmp_path):
    settings, engine, sessions = registry_database
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    metadata = json.loads(
        (library / "metadata/Train.metadata.json").read_text(encoding="utf-8-sig")
    )
    # Deliberately synthetic identity in the isolated test database only.
    metadata["motion_identity"]["motion_code"] = "ISL_CONCURRENCY_TEST_01"
    path = tmp_path / "test-identity.json"
    path.write_text(json.dumps(metadata))

    def register():
        with sessions() as session:
            return stage_motion(
                path,
                library / "glb/Train.glb",
                "test-admin",
                session,
                LocalAssetStore(settings.storage_root),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))
    assert sorted(result["status"] for result in results) == ["STAGED", "UNCHANGED"]
    assert len({result["motion_version_id"] for result in results}) == 1
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM motion_versions WHERE concept_id=:id"),
                {"id": results[0]["concept_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM admin_audit_logs WHERE entity_id=:id"),
                {"id": results[0]["motion_version_id"]},
            )
            == 1
        )
