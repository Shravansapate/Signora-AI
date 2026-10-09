"""Phase 2 contracts against isolated PostgreSQL and the unchanged supplied GLBs."""
# ruff: noqa: F811 -- pytest discovers the imported module-scoped fixture.

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.main import create_app
from app.registry import stage_motion
from app.retrieval.playback_schema import PlaybackPlan, PrepareResult
from app.storage import LocalAssetStore

LIBRARY = Path(__file__).resolve().parents[2] / "metadata_json and glb"


@pytest.fixture(scope="module")
def playback_assets(registry_database):
    settings, engine, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    result = {}
    for name in ("Train", "1_One", "Zero"):
        with sessions() as session:
            staged = stage_motion(
                LIBRARY / "metadata" / f"{name}.metadata.json",
                LIBRARY / "glb" / f"{name}.glb",
                "isolated-phase2-test-admin",
                session,
                store,
            )
        with engine.connect() as connection:
            result[name] = dict(
                connection.execute(
                    text("SELECT * FROM motion_versions WHERE id=:id"),
                    {"id": staged["motion_version_id"]},
                )
                .mappings()
                .one()
            )
    return result


@pytest.fixture
def playback_client(registry_database, playback_assets):
    settings, _, _ = registry_database
    principals, headers = {}, {}
    for role in ("admin", "reviewer", "operator", "display", "other-admin"):
        token = secrets.token_hex(32)
        headers[role] = {"Authorization": f"Bearer {token}"}
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=role,
            roles={"admin" if role == "other-admin" else role},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    configured = settings.model_copy(update={"principals": principals})
    with TestClient(create_app(configured)) as client:
        yield client, headers


def prepare_review(playback_client, playback_assets, names, *, role="reviewer"):
    client, headers = playback_client
    return client.post(
        "/api/v1/review/prepare",
        headers=headers[role],
        json={
            "avatar_motion_version_id": str(playback_assets["Train"]["id"]),
            "motion_version_ids": [str(playback_assets[name]["id"]) for name in names],
        },
    )


def ready_manifest(response):
    assert response.status_code == 200, response.text
    result = PrepareResult.model_validate(response.json())
    assert result.status == "READY", result
    assert result.manifest is not None
    return result.manifest.model_dump(mode="json")


def test_review_roles_are_separate_from_display_and_operator(playback_client, playback_assets):
    client, headers = playback_client
    assert client.get("/api/v1/review/motions").status_code == 401
    for role in ("display", "operator"):
        assert client.get("/api/v1/review/motions", headers=headers[role]).status_code == 403
        assert (
            prepare_review(playback_client, playback_assets, ["Train"], role=role).status_code
            == 403
        )
    for role in ("reviewer", "admin"):
        response = client.get("/api/v1/review/motions", headers=headers[role])
        assert response.status_code == 200
        assert str(playback_assets["Train"]["id"]) in response.text
        assert "source_metadata" not in response.text
        assert "storage_key" not in response.text


@pytest.mark.parametrize(
    "names",
    [
        ["Train"],
        ["Train", "1_One", "Zero"],
        ["Train", "Zero", "1_One", "1_One", "Zero", "Train"] * 2,
    ],
    ids=["one", "three", "twelve-with-repeated-digits"],
)
def test_review_manifests_pin_complete_order_and_repetition(
    playback_client, playback_assets, names, registry_database
):
    client, headers = playback_client
    plan = ready_manifest(prepare_review(playback_client, playback_assets, names))
    assert plan["purpose"] == "CONTENT_REVIEW"
    assert plan["operational"] is False
    assert plan["readiness_policy"] == "FULL_MESSAGE"
    assert plan["output_language"] == "ISL"
    assert plan["avatar"]["motion_version_id"] == str(playback_assets["Train"]["id"])
    assert plan["avatar"]["sha256"] == playback_assets["Train"]["sha256"]
    assert [item["motion_version_id"] for item in plan["items"]] == [
        str(playback_assets[name]["id"]) for name in names
    ]
    assert [item["sequence_index"] for item in plan["items"]] == list(range(len(names)))
    for item, name in zip(plan["items"], names, strict=True):
        asset = playback_assets[name]
        assert item["concept_id"] == str(asset["concept_id"])
        assert item["version_no"] == asset["version_no"]
        assert item["sha256"] == asset["sha256"]
        assert item["clip_name"] == asset["clip_name"]
        assert item["duration_seconds"] == asset["duration_seconds"]
        assert item["size_bytes"] == asset["size_bytes"]
        assert item["playback_rate"] == 1
    assert plan["estimated_duration_seconds"] == pytest.approx(
        sum(playback_assets[name]["duration_seconds"] for name in names)
    )
    response = client.get(f"/api/v1/playback/{plan['manifest_id']}", headers=headers["reviewer"])
    assert response.status_code == 200, response.text
    assert PlaybackPlan.model_validate(response.json()).model_dump(mode="json") == plan
    assert "no-store" in response.headers["cache-control"]
    _, engine, _ = registry_database
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM playback_manifests WHERE id=:id"),
                {"id": plan["manifest_id"]},
            )
            == 1
        )


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("train", "NEEDS_REVIEW"),
        ("  TRAIN  ", "NEEDS_REVIEW"),
        ("Train has not departed from platform 0011", "UNSUPPORTED"),
        ("unsupported critical railway instruction", "UNSUPPORTED"),
    ],
)
def test_exact_lookup_does_not_promote_pending_or_partially_match(
    playback_client, playback_assets, phrase, expected
):
    client, headers = playback_client
    response = client.post(
        "/api/v1/playback/prepare",
        headers=headers["operator"],
        json={
            "text": phrase,
            "avatar_profile_id": str(playback_assets["Train"]["avatar_profile_id"]),
            "source_text_language": "en",
        },
    )
    assert response.status_code == 200, response.text
    result = PrepareResult.model_validate(response.json())
    assert result.status == expected
    assert result.manifest is None
    assert result.reasons


def test_request_validation_cannot_expand_review_or_production_scope(
    playback_client, playback_assets
):
    client, headers = playback_client
    review = {
        "avatar_motion_version_id": str(playback_assets["Train"]["id"]),
        "motion_version_ids": [str(playback_assets["Train"]["id"])],
    }
    assert client.post("/api/v1/review/prepare", json=review).status_code == 401
    for items in ([], review["motion_version_ids"] * 65, ["not-a-uuid"]):
        assert (
            client.post(
                "/api/v1/review/prepare",
                json={**review, "motion_version_ids": items},
                headers=headers["reviewer"],
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/api/v1/review/prepare",
            json={**review, "operational": True},
            headers=headers["reviewer"],
        ).status_code
        == 422
    )
    exact = {
        "text": "train",
        "avatar_profile_id": str(playback_assets["Train"]["avatar_profile_id"]),
        "source_text_language": "en",
    }
    assert (
        client.post("/api/v1/playback/prepare", json=exact, headers=headers["display"]).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/playback/prepare",
            json={**exact, "source_text_language": "hi"},
            headers=headers["operator"],
        ).status_code
        == 422
    )


def test_asset_transport_checks_identity_membership_and_owner(playback_client, playback_assets):
    client, headers = playback_client
    plan = ready_manifest(prepare_review(playback_client, playback_assets, ["1_One"]))
    plan_url = f"/api/v1/playback/{plan['manifest_id']}"
    item = plan["items"][0]
    asset_url = item["asset_url"]
    assert client.get(plan_url, headers=headers["reviewer"]).status_code == 200
    response = client.get(asset_url, headers=headers["reviewer"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.headers["etag"] == f'"{item["sha256"]}"'
    assert "private" in response.headers["cache-control"]
    assert "immutable" in response.headers["cache-control"]
    assert len(response.content) == item["size_bytes"]
    assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
    assert response.content[:4] == b"glTF"
    assert client.get(asset_url).status_code == 401
    assert client.get(plan_url).status_code == 401
    for role in ("admin", "other-admin", "operator", "display"):
        assert client.get(plan_url, headers=headers[role]).status_code == 403
        assert client.get(asset_url, headers=headers[role]).status_code == 403
    wrong_hash = asset_url.replace(item["sha256"], "a" * 64)
    assert client.get(wrong_hash, headers=headers["reviewer"]).status_code == 404
    absent = playback_assets["Zero"]
    nonmember = f"{plan_url}/assets/{absent['id']}/{absent['sha256']}.glb"
    assert client.get(nonmember, headers=headers["reviewer"]).status_code == 404
    assert client.get(f"/api/v1/playback/{uuid4()}", headers=headers["reviewer"]).status_code == 404


def test_missing_required_asset_fails_whole_plan_and_existing_delivery(
    playback_client, playback_assets, registry_database
):
    client, headers = playback_client
    plan = ready_manifest(prepare_review(playback_client, playback_assets, ["Train", "Zero"]))
    settings, _, _ = registry_database
    missing = playback_assets["Zero"]
    path = LocalAssetStore(settings.storage_root).resolve(missing["storage_key"])
    displaced = path.with_suffix(".test-unavailable")
    path.rename(displaced)
    try:
        response = prepare_review(playback_client, playback_assets, ["Train", "Zero"])
        assert response.status_code == 200, response.text
        result = PrepareResult.model_validate(response.json())
        assert result.status == "ASSET_UNAVAILABLE"
        assert result.manifest is None
        assert result.reasons
        assert (
            client.get(
                f"/api/v1/playback/{plan['manifest_id']}", headers=headers["reviewer"]
            ).status_code
            == 409
        )
        # Even a present first item cannot be delivered from an incomplete plan.
        assert (
            client.get(plan["items"][0]["asset_url"], headers=headers["reviewer"]).status_code
            == 409
        )
    finally:
        displaced.rename(path)


def test_revoked_review_version_invalidates_manifest_and_all_assets(
    playback_client, playback_assets, registry_database
):
    client, headers = playback_client
    plan = ready_manifest(prepare_review(playback_client, playback_assets, ["Train", "Zero"]))
    _, engine, _ = registry_database
    version = {"id": playback_assets["Zero"]["id"]}
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE motion_versions SET revoked_at=now() WHERE id=:id"), version
        )
    try:
        assert (
            client.get(
                f"/api/v1/playback/{plan['manifest_id']}", headers=headers["reviewer"]
            ).status_code
            == 409
        )
        assert (
            client.get(plan["avatar"]["asset_url"], headers=headers["reviewer"]).status_code == 409
        )
        response = prepare_review(playback_client, playback_assets, ["Train", "Zero"])
        assert response.status_code == 200, response.text
        assert response.json()["status"] != "READY"
        assert response.json()["manifest"] is None
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE motion_versions SET revoked_at=NULL WHERE id=:id"), version
            )


def test_preparation_never_mutates_supplied_metadata_or_review_state(
    playback_client, playback_assets, registry_database
):
    ready_manifest(prepare_review(playback_client, playback_assets, ["Train", "1_One", "Zero"]))
    _, engine, _ = registry_database
    with engine.connect() as connection:
        for name, asset in playback_assets.items():
            row = (
                connection.execute(
                    text("SELECT * FROM motion_versions WHERE id=:id"), {"id": asset["id"]}
                )
                .mappings()
                .one()
            )
            source = json.loads(
                (LIBRARY / "metadata" / f"{name}.metadata.json").read_text(encoding="utf-8-sig")
            )
            assert row["source_metadata"] == source
            assert row["linguistic_review_status"] == "PENDING"
            assert row["composition_review_status"] == "PENDING"
            assert row["reviewer"] is None
            assert row["lifecycle_status"] == "STAGING"
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0


def test_expiry_blocks_pinned_plan_even_when_bytes_are_cached(
    playback_client, playback_assets, monkeypatch
):
    from app import playback

    client, headers = playback_client
    plan = ready_manifest(prepare_review(playback_client, playback_assets, ["Train"]))
    monkeypatch.setattr(playback, "utc_now", lambda: datetime.fromisoformat(plan["valid_until"]))
    assert (
        client.get(
            f"/api/v1/playback/{plan['manifest_id']}", headers=headers["reviewer"]
        ).status_code
        == 409
    )
    assert client.get(plan["avatar"]["asset_url"], headers=headers["reviewer"]).status_code == 409


def test_persisted_snapshot_and_order_are_immutable(
    playback_client, playback_assets, registry_database
):
    plan = ready_manifest(prepare_review(playback_client, playback_assets, ["Train"] * 3))
    _, engine, _ = registry_database
    with engine.connect() as connection:
        assert (
            list(
                connection.scalars(
                    text(
                        "SELECT motion_version_id FROM playback_manifest_items "
                        "WHERE manifest_id=:id ORDER BY sequence_index"
                    ),
                    {"id": plan["manifest_id"]},
                )
            )
            == [playback_assets["Train"]["id"]] * 3
        )
    for query in (
        "UPDATE playback_manifests SET payload='{}' WHERE id=:id",
        "DELETE FROM playback_manifests WHERE id=:id",
        "UPDATE playback_manifest_items SET sequence_index=4 WHERE manifest_id=:id",
        "INSERT INTO playback_manifest_items SELECT manifest_id, 4, motion_version_id "
        "FROM playback_manifest_items WHERE manifest_id=:id LIMIT 1",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(query), {"id": plan["manifest_id"]})


def test_exact_positive_selection_and_deactivation_in_isolated_approved_fixture(
    playback_client, playback_assets, registry_database
):
    """Synthetic approval applies only to disposable test DB records, never source metadata."""
    client, headers = playback_client
    _, engine, _ = registry_database
    asset = playback_assets["Train"]
    params = {
        "version": asset["id"],
        "concept": asset["concept_id"],
        "avatar": asset["avatar_profile_id"],
    }
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE avatar_profiles SET status='APPROVED' WHERE id=:avatar"), params
        )
        connection.execute(
            text("""
          UPDATE sign_concepts SET enabled=true, domain='railway', meaning='isolated test meaning',
          context='isolated test context', meaning_status='APPROVED',
          active_motion_version_id=:version
          WHERE id=:concept
        """),
            params,
        )
        connection.execute(
            text("""
          UPDATE motion_versions SET linguistic_review_status='APPROVED',
          composition_review_status='APPROVED',
          reviewed_sha256=sha256, reviewed_avatar_profile_id=avatar_profile_id,
          reviewer='synthetic-test-review', reviewed_at=now(), lifecycle_status='ACTIVE',
          reviewed_semantic_revision=1
          WHERE id=:version
        """),
            params,
        )
    try:
        response = client.post(
            "/api/v1/playback/prepare",
            headers=headers["operator"],
            json={"text": "  TRAIN  ", "avatar_profile_id": str(asset["avatar_profile_id"])},
        )
        plan = ready_manifest(response)
        assert plan["purpose"] == "EXACT_CONTENT"
        assert len(plan["items"]) == 1
        assert plan["items"][0]["motion_version_id"] == str(asset["id"])
        assert plan["items"][0]["transition"] == "NONE"
        assert (
            client.get(plan["items"][0]["asset_url"], headers=headers["operator"]).status_code
            == 200
        )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE sign_concepts SET enabled=false WHERE id=:concept"), params
            )
        assert (
            client.get(
                f"/api/v1/playback/{plan['manifest_id']}", headers=headers["operator"]
            ).status_code
            == 409
        )
        response = client.post(
            "/api/v1/playback/prepare",
            headers=headers["operator"],
            json={"text": "train", "avatar_profile_id": str(asset["avatar_profile_id"])},
        )
        assert response.json()["status"] == "NEEDS_REVIEW"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE avatar_profiles SET status='PENDING' WHERE id=:avatar"), params
            )
            connection.execute(
                text("""
              UPDATE motion_versions SET linguistic_review_status='PENDING',
              composition_review_status='PENDING', reviewed_sha256=NULL,
              reviewed_avatar_profile_id=NULL, reviewer=NULL, reviewed_at=NULL,
              lifecycle_status='STAGING' WHERE id=:version
            """),
                params,
            )
            connection.execute(
                text("""
              UPDATE sign_concepts SET enabled=false, domain=NULL, meaning=NULL, context=NULL,
              meaning_status='PENDING', active_motion_version_id=NULL WHERE id=:concept
            """),
                params,
            )


def test_semantic_hash_does_not_include_transport_urls(playback_client, playback_assets):
    from app.playback import semantic_hash

    plan = PlaybackPlan.model_validate(
        ready_manifest(prepare_review(playback_client, playback_assets, ["Train"]))
    )
    changed = plan.model_copy(
        update={
            "avatar": plan.avatar.model_copy(update={"asset_url": "/renewed"}),
            "items": [item.model_copy(update={"asset_url": "/renewed"}) for item in plan.items],
        }
    )
    assert semantic_hash(plan) == semantic_hash(changed) == plan.manifest_hash
