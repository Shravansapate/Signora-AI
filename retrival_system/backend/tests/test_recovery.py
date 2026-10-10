"""Actual pg_dump/restore with supplied GLBs; all approvals are isolated fixtures."""
# ruff: noqa: F811 -- reuse isolated database/content fixtures.

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from test_announcements import announcement_setup, approved_template  # noqa: F401
from test_lifecycle import lifecycle_clients, request_ok  # noqa: F401
from test_live_delivery import live_setup, publication  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app import recovery
from app.main import create_app


def target_database(settings):
    uri = recovery.connection_uri(settings.database_url.get_secret_value())
    name = "restore_" + uuid4().hex
    with psycopg.connect(uri, autocommit=True) as connection:
        connection.execute(
            psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name))
        )
    return make_url(uri).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def paired_backup(live_setup, announcement_setup, tmp_path_factory):
    client, headers, did, _, _, settings = live_setup
    assets, _ = announcement_setup
    review = request_ok(
        client,
        headers,
        "/api/v1/review/prepare",
        {
            "avatar_motion_version_id": assets["Train"]["motion_version_id"],
            "motion_version_ids": [
                assets[name]["motion_version_id"]
                for name in ("Train", "Zero", "1_One", "1_One", "Arrive")
            ],
        },
    )["manifest"]
    live = request_ok(client, headers, "/api/v1/announcements", publication(client, headers))
    credential = request_ok(
        client,
        headers,
        f"/api/v1/admin/displays/{did}/token",
        {
            "expected_revision": 1,
            "reason": "Isolated durable credential recovery verification",
        },
    )
    headers = {
        **headers,
        "managed_display": {"Authorization": "Bearer " + credential["access_token"]},
    }
    directory = tmp_path_factory.mktemp("recovery") / "backup"
    pg_bin = Path(os.environ["SIGNORA_TEST_PG_BIN"])
    uri = recovery.connection_uri(settings.database_url.get_secret_value())
    with psycopg.connect(uri) as connection:
        connection.execute(
            "UPDATE display_devices SET session_id=%s,lease_until=now()+interval '1 minute'"
            " WHERE id=%s",
            (uuid4(), did),
        )
    backup = recovery.create_backup(uri, settings.storage_root, directory, pg_bin)
    # This cancellation is intentionally absent from the backup snapshot.
    request_ok(
        client,
        headers,
        f"/api/v1/announcements/{live['message_id']}/revisions",
        {
            "station_id": "TEST",
            "source_event_id": live["source_event_id"]
            if "source_event_id" in live
            else _source_event(uri, live["message_id"]),
            "source_revision": 2,
            "expected_revision": 1,
            "cancel": True,
            "reason": "Cancellation after backup in isolated recovery test",
        },
    )
    return directory, backup, pg_bin, settings, headers, review, live, did


def _source_event(uri, message):
    with psycopg.connect(uri) as connection:
        return connection.execute(
            "SELECT source_event_id FROM announcements WHERE id=%s", (message,)
        ).fetchone()[0]


def test_actual_restore_preserves_manifest_assets_and_fences_live_state(paired_backup, tmp_path):
    directory, backup, pg_bin, settings, headers, review, live, did = paired_backup
    target = target_database(settings)
    storage = tmp_path / "restored-assets"
    receipt = recovery.restore_backup(directory, target, storage, pg_bin)
    assert receipt["assets_verified"] == 4
    restored = settings.model_copy(
        update={
            "database_url": settings.database_url.__class__(
                make_url(target)
                .set(drivername="postgresql+psycopg")
                .render_as_string(hide_password=False)
            ),
            "storage_root": storage,
        }
    )
    with TestClient(create_app(restored)) as client:
        assert client.get("/health/ready").status_code == 200
        # Restore deliberately disables displays until an administrator re-enables them.
        assert client.get("/api/v1/session", headers=headers["managed_display"]).status_code == 401
        response = client.get(f"/api/v1/playback/{review['manifest_id']}", headers=headers["admin"])
        assert response.status_code == 200, response.text
        assert response.json() == review
        for asset in [review["avatar"], *review["items"]]:
            response = client.get(asset["asset_url"], headers=headers["admin"])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == asset["sha256"]
        response = client.get(f"/api/v1/playback/{live['manifest_id']}", headers=headers["admin"])
        assert response.status_code == 403, response.text
    with psycopg.connect(target) as connection:
        assert connection.execute(
            "SELECT state FROM announcements WHERE id=%s", (live["message_id"],)
        ).fetchone() == ("WITHDRAWN",)
        assert connection.execute(
            "SELECT state FROM announcement_revisions WHERE message_id=%s", (live["message_id"],)
        ).fetchone() == ("LIVE",)
        assert connection.execute(
            "SELECT enabled,session_id,lease_until FROM display_devices WHERE id=%s", (did,)
        ).fetchone() == (False, None, None)
        assert (
            connection.execute(
                "SELECT count(*) FROM event_outbox WHERE event_type='WITHDRAWN'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT details->>'backup_id' FROM admin_audit_logs WHERE action='BACKUP_RESTORED'"
        ).fetchone()[0] == str(backup.backup_id)
    assert recovery.verify_backup(directory) == backup
    with TestClient(create_app(restored)) as client:
        device = client.get(f"/api/v1/admin/displays/{did}", headers=headers["admin"]).json()
        enabled = client.post(
            f"/api/v1/admin/displays/{did}",
            headers=headers["admin"],
            json={
                "expected_revision": device["revision"],
                "station_id": device["station_id"],
                "subject": device["subject"],
                "name": device["name"],
                "enabled": True,
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert client.get("/api/v1/session", headers=headers["managed_display"]).status_code == 200


def test_refuses_populated_database_and_existing_storage(paired_backup, tmp_path):
    directory, _, pg_bin, settings, *_ = paired_backup
    with pytest.raises(recovery.RecoveryError, match="empty database"):
        recovery.restore_backup(
            directory, settings.database_url.get_secret_value(), tmp_path / "assets", pg_bin
        )
    assert not (tmp_path / "assets").exists()
    with pytest.raises(recovery.RecoveryError, match="must not exist"):
        recovery.restore_backup(directory, target_database(settings), tmp_path, pg_bin)


def test_corrupt_asset_rejected_before_database_mutation(paired_backup, tmp_path):
    directory, backup, pg_bin, settings, *_ = paired_backup
    # Change and restore one byte of the isolated backup, never a source library GLB.
    asset = directory / "assets" / backup.assets[0].storage_key
    with asset.open("r+b") as stream:
        original = stream.read(1)
        stream.seek(0)
        stream.write(bytes([original[0] ^ 255]))
    try:
        target = target_database(settings)
        with pytest.raises(recovery.RecoveryError, match="checksum"):
            recovery.restore_backup(directory, target, tmp_path / "assets", pg_bin)
        with psycopg.connect(target) as connection:
            recovery._empty_database(connection)
        assert not (tmp_path / "assets").exists()
    finally:
        with asset.open("r+b") as stream:
            stream.write(original)


@pytest.mark.parametrize("change", ["traversal", "duplicate", "schema", "dump"])
def test_backup_validation_rejects_incompatible_or_corrupt_input(paired_backup, tmp_path, change):
    directory, backup, *_ = paired_backup
    manifest = backup.model_dump(mode="json")
    if change == "traversal":
        manifest["assets"][0]["storage_key"] = "../../source.glb"
    elif change == "duplicate":
        manifest["assets"].append(manifest["assets"][0])
    elif change == "schema":
        manifest["schema_revision"] = "future"
    else:
        manifest["database"]["sha256"] = "0" * 64
        # Immutable dump can be hard-linked without duplicating large assets.
        os.link(directory / "database.dump", tmp_path / "database.dump")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((recovery.RecoveryError, ValidationError)):
        recovery.verify_backup(tmp_path)


def test_failed_operational_fence_rolls_back_entire_database(paired_backup, tmp_path, monkeypatch):
    directory, _, pg_bin, settings, *_ = paired_backup
    target = target_database(settings)
    monkeypatch.setattr(recovery, "_fence_sql", lambda _: "SELECT nonexistent_recovery_function();")
    with pytest.raises(recovery.RecoveryError, match="psql failed"):
        recovery.restore_backup(directory, target, tmp_path / "assets", pg_bin)
    with psycopg.connect(target) as connection:
        recovery._empty_database(connection)
    assert not (tmp_path / "assets/restore-receipt.json").exists()


def test_backup_blocks_cleanup_and_rejects_missing_objects(paired_backup, tmp_path, monkeypatch):
    _, backup, pg_bin, settings, *_ = paired_backup
    uri = recovery.connection_uri(settings.database_url.get_secret_value())
    original_check = recovery._check
    observed = []

    def locked_check(path, expected, destination=None):
        if destination is not None and not observed:
            with psycopg.connect(uri, autocommit=True) as connection:
                acquired = connection.execute(
                    "SELECT pg_try_advisory_lock(1397311310,1)"
                ).fetchone()[0]
                observed.append(acquired)
        original_check(path, expected, destination)

    monkeypatch.setattr(recovery, "_check", locked_check)
    recovery.create_backup(uri, settings.storage_root, tmp_path / "locked-backup", pg_bin)
    assert observed == [False]
    asset = settings.storage_root / backup.assets[0].storage_key
    hidden = asset.with_suffix(".temporarily-unavailable")
    asset.rename(hidden)
    try:
        with pytest.raises(FileNotFoundError):
            recovery.create_backup(uri, settings.storage_root, tmp_path / "incomplete", pg_bin)
        assert not (tmp_path / "incomplete").exists()
    finally:
        hidden.rename(asset)
