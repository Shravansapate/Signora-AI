"""Actual uploads, PostgreSQL, version selection, metadata and physical cleanup."""

# ruff: noqa: F811
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_lifecycle import LIBRARY, variant
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.lifecycle import LifecycleError, cleanup_one, delete_version
from app.lifecycle_schema import DeleteRequest
from app.main import create_app
from app.models import MotionMetadataRevision, MotionVersion, Station
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def library_environment(registry_database):
    settings, _, sessions = registry_database
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
            Station(id="TEST", revision=1, definition={"name": "Test", "platforms": ["1", "2"]})
        )
    principals, headers = {}, {}
    for role in ("admin", "operator"):
        token = secrets.token_hex(32)
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=role,
            roles={role},
            station_ids={"TEST"},
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        headers[role] = {"Authorization": "Bearer " + token}
    settings = settings.model_copy(
        update={"principals": principals, "demo_mode_enabled": True, "deletion_retention_days": 1}
    )
    with TestClient(create_app(settings)) as client:
        yield client, headers, sessions, store


def upload(client, headers, paths, cid=None):
    metadata, asset = paths
    with metadata.open("rb") as m, asset.open("rb") as a:
        return client.post(
            f"/api/v1/admin/signs/{cid}/motions" if cid else "/api/v1/admin/motions/stage",
            headers=headers["admin"],
            data={} if cid else {"new_concept_only": "true"},
            files={
                "metadata": ("metadata.json", m, "application/json"),
                "motion": ("motion.glb", a, "model/gltf-binary"),
            },
        )


def details(client, headers, cid):
    return client.get(f"/api/v1/review/signs/{cid}", headers=headers["admin"]).json()


def test_upload_select_replace_metadata_archive_rollback_without_restart(
    library_environment, tmp_path
):
    client, headers, sessions, store = library_environment
    key = "LIBRARY_" + uuid4().hex.upper()
    first_paths = variant(tmp_path, key, marker="first")
    # Only this isolated fixture uses an artificial label; production assets remain unchanged.
    label = "workflow" + uuid4().hex[:8]
    raw = json.loads(first_paths[0].read_text())
    raw["motion_identity"].update(canonical_text=label, gloss=label.upper())
    first_paths[0].write_text(json.dumps(raw))
    response = upload(client, headers, first_paths)
    assert response.status_code == 201, response.text
    first = response.json()
    cid, vid1 = first["concept_id"], first["motion_version_id"]
    assert upload(client, headers, first_paths).status_code == 409
    assert details(client, headers, cid)["library_selection"]["enabled"] is False

    def action(name, vid=None):
        current = details(client, headers, cid)["concept"]
        body = {"expected_revision": current["revision"], "reason": "isolated workflow test"}
        if name in {"activate", "rollback"}:
            body.update(
                motion_version_id=vid,
                expected_active_motion_version_id=current["active_motion_version_id"],
            )
        path = f"/api/v1/admin/signs/{cid}/" + (
            f"motions/{vid}/{name}" if name in {"archive", "restore"} else name
        )
        result = client.post(path, headers=headers["admin"], json=body)
        assert result.status_code == 200, result.text
        return result.json()

    def retrieve(word):
        response = client.post(
            "/api/v1/translate",
            headers=headers["operator"],
            json={
                "request_id": str(uuid4()),
                "station_id": "TEST",
                "input_type": "TEXT",
                "text": word,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return [item["motion_version_id"] for item in (body.get("manifest") or {}).get("items", [])]

    action("activate", vid1)
    assert retrieve(label) == [vid1]
    second_paths = variant(tmp_path, key, marker="second")
    raw2 = json.loads(second_paths[0].read_text())
    raw2["motion_identity"].update(canonical_text=label, gloss=label.upper())
    second_paths[0].write_text(json.dumps(raw2))
    second = upload(client, headers, second_paths, cid)
    assert second.status_code == 201, second.text
    vid2 = second.json()["motion_version_id"]
    assert retrieve(label) == [vid1], "Upload must not replace selected content"
    action("activate", vid2)
    assert retrieve(label) == [vid2]
    action("rollback", vid1)
    assert retrieve(label) == [vid1]
    action("archive", vid1)
    assert retrieve(label) == []
    action("restore", vid1)
    assert retrieve(label) == [], "Restore is not activation"
    action("activate", vid1)
    action("deactivate")
    assert retrieve(label) == []
    action("reactivate")
    assert retrieve(label) == [vid1]
    current = details(client, headers, cid)
    metadata_url = f"/api/v1/review/signs/{cid}/motions/{vid1}/metadata"
    downloaded = client.get(metadata_url, headers=headers["admin"]).json()["metadata"]
    alias = "alias" + uuid4().hex[:8]
    downloaded["retrieval"]["aliases"] = [alias]
    update_url = f"/api/v1/admin/signs/{cid}/motions/{vid1}/metadata"
    response = client.post(
        update_url,
        headers=headers["admin"],
        data={
            "expected_revision": str(current["concept"]["revision"]),
            "reason": "add searchable alias",
        },
        files={"metadata": ("metadata.json", json.dumps(downloaded).encode(), "application/json")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["semantic_change"]
    assert retrieve(alias) == []
    action("activate", vid1)
    assert retrieve(alias) == [vid1]
    with sessions() as session:
        first_motion = session.get(MotionVersion, UUID(vid1))
        assert first_motion.source_metadata == raw
        assert store.verify(first_motion.storage_key, first_motion.sha256).is_file()
        assert (
            len(
                list(
                    session.scalars(
                        select(MotionMetadataRevision).where(
                            MotionMetadataRevision.motion_version_id == UUID(vid1)
                        )
                    )
                )
            )
            == 1
        )
    # Strict production view still excludes these unreviewed development selections.
    from sqlalchemy import text

    with sessions() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM eligible_motion_versions WHERE concept_id=:id"),
                {"id": cid},
            )
            == 0
        )


def test_full_version_content_deletion_and_authorization(library_environment, tmp_path):
    client, headers, sessions, store = library_environment
    paths = variant(tmp_path, "DELETE_" + uuid4().hex.upper(), marker="purge")
    result = upload(client, headers, paths).json()
    cid, vid = result["concept_id"], result["motion_version_id"]
    detail = details(client, headers, cid)
    motion = detail["versions"][0]
    body = {
        "expected_revision": detail["concept"]["revision"],
        "confirm_sha256": motion["sha256"],
        "reason": "isolated purge test",
        "purge_metadata": True,
    }
    path = f"/api/v1/admin/signs/{cid}/motions/{vid}"
    assert client.request("DELETE", path, headers=headers["operator"], json=body).status_code == 403
    blocked = client.request("DELETE", path, headers=headers["admin"], json=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "RETENTION_PERIOD"
    # Age only isolated test records; the application enforces the real retention interval.
    from sqlalchemy import text

    with sessions() as session, session.begin():
        session.execute(
            text(
                "UPDATE motion_versions SET created_at=now()-interval '2 days', "
                "updated_at=now()-interval '2 days' WHERE id=:id"
            ),
            {"id": vid},
        )
    response = client.request("DELETE", path, headers=headers["admin"], json=body)
    assert response.status_code == 202, response.text
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "COMPLETE"
    with sessions() as session:
        row = session.get(MotionVersion, UUID(vid))
        assert row.source_metadata == {} and row.technical_report == {}
        assert not store.resolve(row.storage_key).exists()
    assert paths[0].is_file() and paths[1].is_file(), "Original source files must survive"
    key = json.loads(paths[0].read_text())["motion_identity"]["motion_code"]
    replacement = upload(client, headers, variant(tmp_path, key, marker="after-purge"), cid)
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["version_no"] == 2


def test_delete_a_and_reupload_identical_files_without_reviving_old_preview(library_environment):
    client, headers, sessions, store = library_environment
    paths = (LIBRARY / "metadata/A.metadata.json", LIBRARY / "glb/A.glb")
    first = upload(client, headers, paths)
    assert first.status_code == 201, first.text
    cid, vid = first.json()["concept_id"], first.json()["motion_version_id"]
    last_input = {}

    def activate(version):
        c = details(client, headers, cid)["concept"]
        response = client.post(
            f"/api/v1/admin/signs/{cid}/activate",
            headers=headers["admin"],
            json={
                "expected_revision": c["revision"],
                "expected_active_motion_version_id": c["active_motion_version_id"],
                "motion_version_id": version,
                "reason": "isolated A lifecycle test",
            },
        )
        assert response.status_code == 200, response.text

    def translate():
        response = client.post(
            "/api/v1/translate",
            headers=headers["operator"],
            json={
                "request_id": str(uuid4()),
                "station_id": "TEST",
                "input_type": "TEXT",
                "text": "Train xyzabc",
            },
        )
        assert response.status_code == 200, response.text
        last_input.update(response.json())
        return response.json()["manifest"]

    activate(vid)
    plan = translate()
    assert vid in [item["motion_version_id"] for item in plan["items"]]
    published = client.post(
        "/api/v1/development/announcements",
        headers=headers["operator"],
        json={
            "station_id": "TEST",
            "source_event_id": last_input["input_id"],
            "source_revision": 1,
            "expected_revision": 0,
            "reason": "isolated development display fixture",
            "preview_manifest_id": plan["manifest_id"],
            "preview_manifest_hash": plan["manifest_hash"],
        },
    )
    assert published.status_code == 200, published.text
    current = details(client, headers, cid)
    assert current["versions"][0]["references"]["manifest_items"] > 0
    body = {
        "expected_revision": current["concept"]["revision"],
        "confirm_sha256": current["versions"][0]["sha256"],
        "reason": "delete and readd A in isolated database",
        "purge_metadata": True,
        "development_reset": True,
    }
    with sessions() as session, pytest.raises(LifecycleError, match="development server"):
        delete_version(session, UUID(cid), UUID(vid), DeleteRequest(**body), "fixture", 30)
    url = f"/api/v1/admin/signs/{cid}/motions/{vid}"
    assert client.request("DELETE", url, headers=headers["operator"], json=body).status_code == 403
    stale = client.request(
        "DELETE", url, headers=headers["admin"], json={**body, "expected_revision": 99999}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_REVISION"
    deleted = client.request("DELETE", url, headers=headers["admin"], json=body)
    assert deleted.status_code == 202, deleted.text
    from app.models import PlaybackRecord
    from app.playback import PlaybackUnavailable, validate_record

    with sessions() as session, pytest.raises(PlaybackUnavailable):
        record = session.get(PlaybackRecord, UUID(published.json()["manifest_id"]))
        validate_record(session, store, record, development=True)
    assert (
        client.get(
            f"/api/v1/playback/{plan['manifest_id']}", headers=headers["operator"]
        ).status_code
        == 409
    )
    assert vid not in [item["motion_version_id"] for item in translate()["items"]]
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "COMPLETE"
    with sessions() as session:
        row = session.get(MotionVersion, UUID(vid))
        assert row.source_metadata == {} and row.technical_report == {}
        assert not store.resolve(row.storage_key).exists()
    # Even the new-concept upload choice can readd a fully deleted identity.
    again = upload(client, headers, paths)
    assert again.status_code == 201, again.text
    second = again.json()
    assert second["concept_id"] == cid and second["motion_version_id"] != vid
    assert second["version_no"] == 2
    activate(second["motion_version_id"])
    assert second["motion_version_id"] in [
        item["motion_version_id"] for item in translate()["items"]
    ]
    assert (
        client.get(
            f"/api/v1/playback/{plan['manifest_id']}", headers=headers["operator"]
        ).status_code
        == 409
    )
    assert paths[0].is_file() and paths[1].is_file()
    # A fast re-upload before the cleanup worker runs must not delete new bytes.
    current = details(client, headers, cid)
    third_delete = client.request(
        "DELETE",
        f"/api/v1/admin/signs/{cid}/motions/{second['motion_version_id']}",
        headers=headers["admin"],
        json={**body, "expected_revision": current["concept"]["revision"]},
    )
    assert third_delete.status_code == 202, third_delete.text
    third = upload(client, headers, paths, cid)
    assert third.status_code == 201, third.text
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "SHARED"
        motion = session.get(MotionVersion, UUID(third.json()["motion_version_id"]))
        assert store.verify(motion.storage_key, motion.sha256).is_file()
