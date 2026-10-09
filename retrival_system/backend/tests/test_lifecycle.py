"""Lifecycle API proofs with real source bytes and explicitly isolated review fixtures."""
# ruff: noqa: F811 -- pytest fixture reuse.

import hashlib
import json
import secrets
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.lifecycle import LifecycleError, cleanup_one, set_enabled
from app.lifecycle_schema import RevisionRequest
from app.main import create_app
from app.models import SignConcept
from app.registry import stage_motion
from app.storage import LocalAssetStore

LIBRARY = Path(__file__).resolve().parents[2] / "metadata_json and glb"


def variant(tmp_path, identity, *, marker="candidate", source="Train"):
    """Only test copies change: GLB extras simulate a quality revision without changing its rig."""
    raw = (LIBRARY / "glb" / f"{source}.glb").read_bytes()
    length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + length])
    document["extras"] = {"isolated_lifecycle_fixture": marker, "isolated_identity": identity}
    encoded = json.dumps(document, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    changed = struct.pack("<III", 0x46546C67, 2, len(raw) - length + len(encoded))
    changed += struct.pack("<II", len(encoded), 0x4E4F534A) + encoded + raw[20 + length :]
    asset = tmp_path / f"{marker}.glb"
    asset.write_bytes(changed)
    metadata = json.loads(
        (LIBRARY / "metadata" / f"{source}.metadata.json").read_text(encoding="utf-8-sig")
    )
    checksum = hashlib.sha256(changed).hexdigest()
    metadata["motion_identity"]["motion_code"] = identity
    metadata["assets"]["final_glb"].update(
        sha256=checksum, size_bytes=len(changed), relative_path=asset.name
    )
    metadata["file_integrity"].update(glb_sha256=checksum, glb_size_bytes=len(changed))
    if metadata.get("signer_review", {}).get("hash_binding"):
        metadata["signer_review"]["hash_binding"]["glb_sha256"] = checksum
    path = tmp_path / f"{marker}.metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path, asset


@pytest.fixture(scope="module")
def lifecycle_clients(registry_database):
    settings, _, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    # Keep the actual canonical source stable; fixture replacements have different hashes.
    with sessions() as session:
        stage_motion(
            LIBRARY / "metadata/Train.metadata.json",
            LIBRARY / "glb/Train.glb",
            "fixture",
            session,
            store,
        )
    principals, headers = {}, {}
    for role in ("admin", "reviewer", "operator", "display", "other-reviewer"):
        token = secrets.token_hex(32)
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=role,
            roles={"reviewer" if role == "other-reviewer" else role},
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        headers[role] = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(settings.model_copy(update={"principals": principals}))) as client:
        yield client, headers


@pytest.fixture
def staged_concept(registry_database, lifecycle_clients, tmp_path):
    settings, _, sessions = registry_database
    key = "ISOLATED_" + uuid4().hex.upper()
    paths = [variant(tmp_path, key, marker=marker) for marker in ("first", "second")]
    versions = []
    for metadata, asset in paths:
        with sessions() as session:
            versions.append(
                stage_motion(
                    metadata, asset, "fixture", session, LocalAssetStore(settings.storage_root)
                )
            )
    return versions, paths


def request_ok(client, headers, path, body, role="admin", expected=200):
    response = client.post(path, headers=headers[role], json=body)
    assert response.status_code == expected, response.text
    return response.json()


def history(client, headers, concept_id):
    response = client.get(f"/api/v1/review/signs/{concept_id}", headers=headers["reviewer"])
    assert response.status_code == 200, response.text
    return response.json()


def approve_fixture(client, headers, concept_id, version_id):
    """Explicit test-only reviewer decisions exercise the real public review workflow."""
    current = history(client, headers, concept_id)
    concept = current["concept"]
    motion = next(v for v in current["versions"] if v["id"] == version_id)
    avatars = client.get("/api/v1/review/avatars", headers=headers["reviewer"]).json()["items"]
    avatar = next(a for a in avatars if a["id"] == motion["avatar_profile_id"])
    if avatar["status"] != "APPROVED":
        request_ok(
            client,
            headers,
            f"/api/v1/review/avatars/{avatar['id']}",
            {
                "expected_revision": avatar["revision"],
                "sha256": avatar["source_sha256"],
                "rig_fingerprint": avatar["rig_fingerprint"],
                "decision": "APPROVED",
                "reason": "isolated test",
                "evidence": "synthetic fixture avatar review",
            },
            role="reviewer",
        )
    if concept["meaning_status"] != "APPROVED":
        request_ok(
            client,
            headers,
            f"/api/v1/review/signs/{concept_id}/meaning",
            {
                "expected_revision": concept["revision"],
                "meaning": "isolated fixture meaning",
                "context": "isolated exact content",
                "domain": "test",
                "decision": "APPROVED",
                "reason": "isolated test",
                "evidence": "synthetic fixture meaning review",
            },
            role="reviewer",
        )
    concept = history(client, headers, concept_id)["concept"]
    preview = request_ok(
        client,
        headers,
        "/api/v1/review/prepare",
        {"avatar_motion_version_id": version_id, "motion_version_ids": [version_id]},
        role="reviewer",
    )
    assert preview["status"] == "READY", preview
    return request_ok(
        client,
        headers,
        f"/api/v1/review/signs/{concept_id}/motions/{version_id}",
        {
            "expected_revision": concept["revision"],
            "sha256": motion["sha256"],
            "avatar_profile_id": motion["avatar_profile_id"],
            "semantic_revision": concept["semantic_revision"],
            "linguistic": "APPROVED",
            "composition": "APPROVED",
            "composition_scope": "EXACT_CONTENT",
            "preview_manifest_id": preview["manifest"]["manifest_id"],
            "rendered_review_confirmed": True,
            "evidence": "synthetic isolated test approval, not source asset certification",
            "reason": "isolated test",
        },
        role="reviewer",
    )


def switch(client, headers, concept_id, version_id, operation="activate"):
    current = history(client, headers, concept_id)["concept"]
    return request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{concept_id}/{operation}",
        {
            "expected_revision": current["revision"],
            "expected_active_motion_version_id": current["active_motion_version_id"],
            "motion_version_id": version_id,
            "reason": "isolated lifecycle test",
        },
    )


def test_replace_rollback_deactivate_reactivate_and_revoke(
    lifecycle_clients, staged_concept, registry_database
):
    client, headers = lifecycle_clients
    versions, paths = staged_concept
    first, second = [row["motion_version_id"] for row in versions]
    cid = versions[0]["concept_id"]
    before = history(client, headers, cid)
    from app.registry import eligible_motions

    settings, engine, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    key = before["concept"]["semantic_key"]
    avatar = UUID(before["versions"][0]["avatar_profile_id"])

    def selected():
        with sessions() as session:
            return eligible_motions(session, [key], avatar, store)

    assert selected() == {}
    response = client.post(
        f"/api/v1/admin/signs/{cid}/activate",
        headers=headers["admin"],
        json={
            "expected_revision": before["concept"]["revision"],
            "expected_active_motion_version_id": None,
            "motion_version_id": first,
            "reason": "must reject pending",
        },
    )
    assert response.status_code == 409
    approve_fixture(client, headers, cid, first)
    switch(client, headers, cid, first)
    approve_fixture(client, headers, cid, second)
    result = switch(client, headers, cid, second)
    assert result["active_motion_version_id"] == second
    assert str(selected()[key]["id"]) == second
    after = history(client, headers, cid)
    assert after["concept"]["id"] == before["concept"]["id"]
    assert after["aliases"] == before["aliases"]
    assert next(v for v in after["versions"] if v["id"] == first)["lifecycle_status"] == "ARCHIVED"
    switch(client, headers, cid, first, "rollback")
    assert str(selected()[key]["id"]) == first
    current = history(client, headers, cid)["concept"]
    semantic_revision = current["semantic_revision"]
    deactivated = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/deactivate",
        {"expected_revision": current["revision"], "reason": "isolated test"},
    )
    assert not deactivated["enabled"] and deactivated["active_motion_version_id"] == first
    assert selected() == {}
    active = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/reactivate",
        {"expected_revision": deactivated["revision"], "reason": "isolated test"},
    )
    assert active["enabled"] and active["semantic_revision"] == semantic_revision
    assert str(selected()[key]["id"]) == first
    revoked = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/motions/{first}/revoke",
        {"expected_revision": active["revision"], "reason": "defective fixture"},
    )
    assert revoked["active_motion_version_id"] is None and not revoked["enabled"]
    assert selected() == {}
    available = client.get("/api/v1/review/motions", headers=headers["reviewer"]).json()["items"]
    assert first not in [row["motion_version_id"] for row in available]
    response = client.post(
        f"/api/v1/admin/signs/{cid}/rollback",
        headers=headers["admin"],
        json={
            "expected_revision": revoked["revision"],
            "expected_active_motion_version_id": None,
            "motion_version_id": first,
            "reason": "revoked rollback must fail",
        },
    )
    assert response.status_code == 409
    with sessions() as session:
        unchanged = stage_motion(
            *paths[0], "fixture", session, LocalAssetStore(settings.storage_root)
        )
    assert unchanged["status"] == "UNCHANGED"  # Semantic review did not modify source evidence.
    with engine.connect() as connection:
        audit = connection.scalar(
            text(
                "SELECT count(*) FROM admin_audit_logs "
                "WHERE action='MOTION_ROLLED_BACK' AND entity_id=:id"
            ),
            {"id": cid},
        )
        events = connection.scalar(
            text(
                "SELECT count(*) FROM registry_events "
                "WHERE event_type='MOTION_ROLLED_BACK' AND entity_id=:id"
            ),
            {"id": cid},
        )
        assert audit == events == 1


def test_missing_candidate_and_stale_revision_leave_active_version_intact(
    lifecycle_clients, staged_concept, registry_database
):
    client, headers = lifecycle_clients
    versions, _ = staged_concept
    first, second = [row["motion_version_id"] for row in versions]
    cid = versions[0]["concept_id"]
    approve_fixture(client, headers, cid, first)
    switch(client, headers, cid, first)
    approve_fixture(client, headers, cid, second)
    current = history(client, headers, cid)
    candidate = next(v for v in current["versions"] if v["id"] == second)
    store = LocalAssetStore(registry_database[0].storage_root)
    path = store.resolve(f"sha256/{candidate['sha256'][:2]}/{candidate['sha256']}.glb")
    displaced = path.with_suffix(".unavailable")
    path.rename(displaced)
    try:
        response = client.post(
            f"/api/v1/admin/signs/{cid}/activate",
            headers=headers["admin"],
            json={
                "expected_revision": current["concept"]["revision"],
                "expected_active_motion_version_id": first,
                "motion_version_id": second,
                "reason": "failed candidate",
            },
        )
        assert response.status_code == 409
        assert history(client, headers, cid)["concept"] == current["concept"]
    finally:
        displaced.rename(path)
    request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/deactivate",
        {"expected_revision": current["concept"]["revision"], "reason": "advance revision"},
    )
    response = client.post(
        f"/api/v1/admin/signs/{cid}/activate",
        headers=headers["admin"],
        json={
            "expected_revision": current["concept"]["revision"],
            "expected_active_motion_version_id": first,
            "motion_version_id": second,
            "reason": "stale candidate",
        },
    )
    assert response.status_code == 409 and response.json()["code"] == "STALE_REVISION"


def test_concurrent_catalog_changes_compare_revision(
    lifecycle_clients, staged_concept, registry_database
):
    cid = UUID(staged_concept[0][0]["concept_id"])
    _, _, sessions = registry_database
    with sessions() as session:
        revision = session.get(SignConcept, cid).revision
    request = RevisionRequest(expected_revision=revision, reason="concurrency fixture")

    def mutate():
        with sessions() as session:
            try:
                set_enabled(session, None, cid, request, "fixture", False)
                return "SUCCESS"
            except LifecycleError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: mutate(), range(2))) == ["STALE_REVISION", "SUCCESS"]

    from app.lifecycle import switch_version
    from app.lifecycle_schema import SwitchRequest

    client, headers = lifecycle_clients
    versions = [row["motion_version_id"] for row in staged_concept[0]]
    for version in versions:
        approve_fixture(client, headers, cid, version)
    revision = history(client, headers, cid)["concept"]["revision"]
    store = LocalAssetStore(registry_database[0].storage_root)

    def promote(version):
        with sessions() as session:
            try:
                switch_version(
                    session,
                    store,
                    cid,
                    SwitchRequest(
                        expected_revision=revision,
                        expected_active_motion_version_id=None,
                        motion_version_id=UUID(version),
                        reason="concurrent activation fixture",
                    ),
                    "fixture",
                )
                return "SUCCESS"
            except LifecycleError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(promote, versions)) == ["STALE_REVISION", "SUCCESS"]
    after = history(client, headers, cid)
    active = [v["id"] for v in after["versions"] if v["lifecycle_status"] == "ACTIVE"]
    assert active == [after["concept"]["active_motion_version_id"]]


def test_delete_checks_retention_references_and_preserves_history(
    lifecycle_clients, staged_concept, registry_database
):
    client, headers = lifecycle_clients
    versions, _ = staged_concept
    cid, vid = versions[0]["concept_id"], versions[0]["motion_version_id"]
    current = history(client, headers, cid)
    motion = next(v for v in current["versions"] if v["id"] == vid)
    body = {
        "expected_revision": current["concept"]["revision"],
        "confirm_sha256": motion["sha256"],
        "reason": "isolated permanent deletion test",
    }
    path = f"/api/v1/admin/signs/{cid}/motions/{vid}"
    response = client.request("DELETE", path, headers=headers["admin"], json=body)
    assert response.status_code == 409 and response.json()["code"] == "RETENTION_PERIOD"
    settings, engine, sessions = registry_database
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE motion_versions SET created_at=now()-interval '31 days', "
                "updated_at=now()-interval '31 days' WHERE id=:id"
            ),
            {"id": vid},
        )
    response = client.request("DELETE", path, headers=headers["admin"], json=body)
    assert response.status_code == 202, response.text
    store = LocalAssetStore(settings.storage_root)
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "COMPLETE"
    assert not store.resolve(f"sha256/{motion['sha256'][:2]}/{motion['sha256']}.glb").exists()
    retained = history(client, headers, cid)
    assert next(v for v in retained["versions"] if v["id"] == vid)["deleted_at"]
    available = client.get("/api/v1/review/motions", headers=headers["reviewer"]).json()["items"]
    assert vid not in [row["motion_version_id"] for row in available]
    # A referenced second version is protected even after retention elapses.
    second = versions[1]["motion_version_id"]
    preview = request_ok(
        client,
        headers,
        "/api/v1/review/prepare",
        {"avatar_motion_version_id": second, "motion_version_ids": [second]},
        role="reviewer",
    )
    assert preview["status"] == "READY"
    body.update(
        expected_revision=retained["concept"]["revision"],
        confirm_sha256=retained["versions"][0]["sha256"],
    )
    response = client.request(
        "DELETE", f"/api/v1/admin/signs/{cid}/motions/{second}", headers=headers["admin"], json=body
    )
    assert response.status_code == 409 and response.json()["code"] == "PROTECTED_REFERENCES"


def test_lifecycle_roles_and_bound_review_identity(lifecycle_clients, staged_concept):
    client, headers = lifecycle_clients
    cid = staged_concept[0][0]["concept_id"]
    current = history(client, headers, cid)["concept"]
    for role in ("reviewer", "operator", "display"):
        assert (
            client.post(
                f"/api/v1/admin/signs/{cid}/deactivate",
                headers=headers[role],
                json={"expected_revision": current["revision"], "reason": "role rejection"},
            ).status_code
            == 403
        )
    for role in ("operator", "display"):
        assert client.get(f"/api/v1/review/signs/{cid}", headers=headers[role]).status_code == 403
    version = staged_concept[0][0]["motion_version_id"]
    request = {
        "expected_revision": current["revision"],
        "sha256": "0" * 64,
        "avatar_profile_id": str(uuid4()),
        "semantic_revision": current["semantic_revision"],
        "linguistic": "APPROVED",
        "composition": "APPROVED",
        "composition_scope": "EXACT_CONTENT",
        "preview_manifest_id": str(uuid4()),
        "rendered_review_confirmed": True,
        "evidence": "isolated invalid review",
        "reason": "fixture",
    }
    response = client.post(
        f"/api/v1/review/signs/{cid}/motions/{version}", headers=headers["reviewer"], json=request
    )
    assert response.status_code == 409 and response.json()["code"] == "IDENTITY_MISMATCH"

    motion = next(v for v in history(client, headers, cid)["versions"] if v["id"] == version)
    request.update(sha256=motion["sha256"], avatar_profile_id=motion["avatar_profile_id"])
    for role, preview_version, code in (
        ("other-reviewer", version, "PREVIEW_REQUIRED"),
        ("reviewer", staged_concept[0][1]["motion_version_id"], "PREVIEW_MISMATCH"),
    ):
        preview = request_ok(
            client,
            headers,
            "/api/v1/review/prepare",
            {"avatar_motion_version_id": version, "motion_version_ids": [preview_version]},
            role=role,
        )
        assert preview["status"] == "READY"
        request["preview_manifest_id"] = preview["manifest"]["manifest_id"]
        response = client.post(
            f"/api/v1/review/signs/{cid}/motions/{version}",
            headers=headers["reviewer"],
            json=request,
        )
        assert response.status_code == 409 and response.json()["code"] == code


def test_review_records_and_avatar_identity_are_immutable(
    lifecycle_clients, staged_concept, registry_database
):
    client, headers = lifecycle_clients
    cid, vid = staged_concept[0][0]["concept_id"], staged_concept[0][0]["motion_version_id"]
    approve_fixture(client, headers, cid, vid)
    engine = registry_database[1]
    for query in (
        "UPDATE content_reviews SET evidence='tampered'",
        "UPDATE avatar_profiles SET source_sha256=repeat('0',64)",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(query))


def test_activation_and_audit_rollback_together_on_commit_failure(
    lifecycle_clients, staged_concept, registry_database, monkeypatch
):
    from app import lifecycle
    from app.lifecycle_schema import SwitchRequest

    client, headers = lifecycle_clients
    versions, _ = staged_concept
    cid = versions[0]["concept_id"]
    first, second = [row["motion_version_id"] for row in versions]
    approve_fixture(client, headers, cid, first)
    switch(client, headers, cid, first)
    approve_fixture(client, headers, cid, second)
    before = history(client, headers, cid)
    settings, engine, sessions = registry_database
    with engine.connect() as connection:
        events_before = connection.scalar(text("SELECT count(*) FROM registry_events"))
        audit_before = connection.scalar(text("SELECT count(*) FROM admin_audit_logs"))
    original = lifecycle._event

    def fail_after_audit(*args, **kwargs):
        original(*args, **kwargs)
        args[0].flush()
        raise RuntimeError("isolated commit-path failure")

    monkeypatch.setattr(lifecycle, "_event", fail_after_audit)
    with pytest.raises(RuntimeError), sessions() as session:
        lifecycle.switch_version(
            session,
            LocalAssetStore(settings.storage_root),
            UUID(cid),
            SwitchRequest(
                expected_revision=before["concept"]["revision"],
                expected_active_motion_version_id=UUID(first),
                motion_version_id=UUID(second),
                reason="atomicity fixture",
            ),
            "fixture",
        )
    after = history(client, headers, cid)
    assert after["concept"] == before["concept"]
    assert [v["lifecycle_status"] for v in after["versions"]] == [
        v["lifecycle_status"] for v in before["versions"]
    ]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM registry_events")) == events_before
        assert connection.scalar(text("SELECT count(*) FROM admin_audit_logs")) == audit_before


def test_shared_object_cleanup_and_post_unlink_crash_recovery(
    lifecycle_clients, staged_concept, registry_database, tmp_path, monkeypatch
):
    from app import lifecycle

    client, headers = lifecycle_clients
    versions, paths = staged_concept
    cid, vid = versions[0]["concept_id"], versions[0]["motion_version_id"]
    settings, engine, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    metadata = json.loads(paths[0][0].read_text())
    metadata["motion_identity"]["motion_code"] = "SHARED_" + uuid4().hex.upper()
    shared_path = tmp_path / "shared.metadata.json"
    shared_path.write_text(json.dumps(metadata))
    with sessions() as session:
        shared = stage_motion(shared_path, paths[0][1], "fixture", session, store)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE motion_versions SET created_at=now()-interval '31 days', "
                "updated_at=now()-interval '31 days' WHERE id IN (:first,:second)"
            ),
            {"first": vid, "second": shared["motion_version_id"]},
        )

    def schedule(concept, version):
        current = history(client, headers, concept)
        row = next(v for v in current["versions"] if v["id"] == version)
        response = client.request(
            "DELETE",
            f"/api/v1/admin/signs/{concept}/motions/{version}",
            headers=headers["admin"],
            json={
                "expected_revision": current["concept"]["revision"],
                "confirm_sha256": row["sha256"],
                "reason": "shared fixture deletion",
            },
        )
        assert response.status_code == 202, response.text
        return row

    row = schedule(cid, vid)
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "SHARED"
    key = f"sha256/{row['sha256'][:2]}/{row['sha256']}.glb"
    assert store.verify(key, row["sha256"]).is_file()
    schedule(shared["concept_id"], shared["motion_version_id"])
    original = lifecycle._event

    def crash(*args, **kwargs):
        raise RuntimeError("isolated crash after object unlink")

    monkeypatch.setattr(lifecycle, "_event", crash)
    with pytest.raises(RuntimeError), sessions() as session:
        cleanup_one(session, store)
    assert not store.resolve(key).exists()
    monkeypatch.setattr(lifecycle, "_event", original)
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "COMPLETE"


def test_cleanup_retry_is_bounded_authorized_and_preserves_attempt_history(
    lifecycle_clients, staged_concept, registry_database, monkeypatch
):
    from app.storage import StorageError

    client, headers = lifecycle_clients
    cid = staged_concept[0][0]["concept_id"]
    vid = staged_concept[0][0]["motion_version_id"]
    settings, engine, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    current = history(client, headers, cid)
    version = next(v for v in current["versions"] if v["id"] == vid)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE motion_versions SET created_at=now()-interval '31 days', "
                "updated_at=now()-interval '31 days' WHERE id=:id"
            ),
            {"id": vid},
        )
    response = client.request(
        "DELETE",
        f"/api/v1/admin/signs/{cid}/motions/{vid}",
        headers=headers["admin"],
        json={
            "expected_revision": current["concept"]["revision"],
            "confirm_sha256": version["sha256"],
            "reason": "isolated cleanup recovery",
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()["cleanup_job_id"]
    original = store.delete

    def unavailable(*args):
        raise StorageError("isolated storage outage")

    monkeypatch.setattr(store, "delete", unavailable)
    for _ in range(3):
        with sessions() as session:
            assert cleanup_one(session, store)["state"] == "FAILED"
    with sessions() as session:
        assert cleanup_one(session, store) is None
    path = f"/api/v1/admin/cleanup/{job}"
    failed = client.get(path, headers=headers["admin"]).json()
    assert failed["attempts"] == failed["attempt_limit"] == 3
    body = {"expected_attempts": 3, "reason": "storage restored in isolated test"}
    assert client.post(path + "/retry", headers=headers["reviewer"], json=body).status_code == 403
    assert client.post(path + "/retry", headers=headers["admin"], json=body).status_code == 202
    assert client.post(path + "/retry", headers=headers["admin"], json=body).status_code == 409
    monkeypatch.setattr(store, "delete", original)
    with sessions() as session:
        assert cleanup_one(session, store)["state"] == "COMPLETE"
    complete = client.get(path, headers=headers["admin"]).json()
    assert complete["attempts"] == 4 and complete["attempt_limit"] == 6
    assert complete["error_code"] is None


def test_version_upload_preserves_active_identity_and_rejects_wrong_concept(
    lifecycle_clients, staged_concept, registry_database, tmp_path
):
    client, headers = lifecycle_clients
    cid = staged_concept[0][0]["concept_id"]
    vid = staged_concept[0][0]["motion_version_id"]
    approve_fixture(client, headers, cid, vid)
    switch(client, headers, cid, vid)
    before = history(client, headers, cid)
    paths = variant(tmp_path, before["concept"]["semantic_key"], marker="third")

    def upload(target):
        with paths[0].open("rb") as metadata, paths[1].open("rb") as motion:
            return client.post(
                f"/api/v1/admin/signs/{target}/motions",
                headers=headers["admin"],
                files={"metadata": ("metadata.json", metadata), "motion": ("motion.glb", motion)},
            )

    with registry_database[1].connect() as connection:
        different = connection.scalar(
            text("SELECT id FROM sign_concepts WHERE semantic_key='ISL_TRAIN_01'")
        )
    assert upload(different).status_code == 409
    assert history(client, headers, cid) == before
    added = upload(cid)
    assert added.status_code == 201 and added.json()["status"] == "STAGED", added.text
    assert upload(cid).json()["status"] == "UNCHANGED"
    after = history(client, headers, cid)
    assert after["concept"]["active_motion_version_id"] == vid
    assert after["concept"]["semantic_revision"] == before["concept"]["semantic_revision"]
    assert after["aliases"] == before["aliases"]
    assert len(after["versions"]) == 3
    assert after["versions"][0]["linguistic_review_status"] == "PENDING"
