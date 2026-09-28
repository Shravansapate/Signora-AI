"""Real database/GLB coverage for automatic operator preview and asset delivery."""
# ruff: noqa: F811 -- shared isolated PostgreSQL fixture.

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.main import create_app
from app.models import Station, VoiceTranscript
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def preview_environment(registry_database):
    settings, engine, sessions = registry_database
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    store = LocalAssetStore(settings.storage_root)
    for name in ("Train", "1_One", "2_Two", "Zero", "Arrive", "Platform", "Accident", "A", "T"):
        with sessions() as session:
            stage_motion(
                library / f"metadata/{name}.metadata.json",
                library / f"glb/{name}.glb",
                "fixture",
                session,
                store,
            )
    with sessions() as session, session.begin():
        session.add(
            Station(id="TEST", revision=1, definition={"name": "Test", "platforms": ["1", "2"]})
        )
    principals, headers = {}, {}
    for subject, role, scope in [
        ("operator", "operator", {"TEST"}),
        ("other", "operator", {"TEST"}),
        ("display", "display", {"TEST"}),
    ]:
        token = secrets.token_hex(32)
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=subject,
            roles={role},
            station_ids=scope,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        headers[subject] = {"Authorization": f"Bearer {token}"}
    configured = settings.model_copy(update={"principals": principals, "demo_mode_enabled": True})
    with TestClient(create_app(configured)) as client:
        yield client, headers, configured, sessions, engine


def payload(**values):
    return {
        "request_id": str(uuid4()),
        "station_id": "TEST",
        "input_type": "TEXT",
        "text": "Train 1201 arrives at platform 2",
        **values,
    }


@pytest.mark.parametrize(
    "source,count",
    [
        ("Train ACCIDENT", 2),
        ("train accident", 2),
        ("TRAIN ACCIDENT", 2),
        ("Train Accident", 2),
        ("Train 1201 Arrives at platfrm 2", 8),
        ("Train 1201 arrives at platfrom 2", 8),
        ("trian 1201 arives at platform 2", 8),
        ("Train accident xyzabc", 3),
        ("Train 1201 platform 2", 7),
        ("Train accident at the platform", 3),
    ],
)
def test_recovery_with_real_database_and_glbs(preview_environment, source, count):
    client, headers, _, _, _ = preview_environment
    response = client.post(
        "/api/v1/translate", json=payload(text=source), headers=headers["operator"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "READY", body
    assert len(body["manifest"]["items"]) == count
    assert body["retrieval"]["last_resort"] is False
    assert body["manifest"]["items"][0]["semantic_key"] == "ISL_TRAIN_01"
    if "accident" in source.lower():
        assert body["manifest"]["items"][1]["semantic_key"] == "ISL_ACCIDENT_01"
        assert body["retrieval"]["translation_status"] == "LEXICAL_RECOVERY"
    assert (
        client.get(
            body["manifest"]["items"][0]["asset_url"], headers=headers["operator"]
        ).status_code
        == 200
    )


def test_operator_receives_plan_glb_and_can_revalidate_without_approval(preview_environment):
    client, headers, _, _, engine = preview_environment
    request = payload()
    response = client.post("/api/v1/translate", json=request, headers=headers["operator"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "READY" and result["demo_mode"]
    assert not result["issues"]
    plan = result["manifest"]
    keys = [item["semantic_key"] for item in plan["items"]]
    assert keys == [
        "ISL_TRAIN_01",
        "ISL_1_ONE_01",
        "ISL_2_TWO_01",
        "ISL_ZERO_01",
        "ISL_1_ONE_01",
        "ISL_PLATFORM_01",
        "ISL_2_TWO_01",
        "ISL_ARRIVE_01",
    ]
    assert result["retrieval"]["gloss_tokens"] == ["TRAIN", "1201", "PLATFORM", "2", "ARRIVE"]
    assert result["retrieval"]["retrieval_tokens"] == result["retrieval"]["gloss_tokens"]
    assert not result["retrieval"]["fingerspelled"]
    url = f"/api/v1/playback/{plan['manifest_id']}"
    assert client.get(url, headers=headers["operator"]).status_code == 200
    asset = client.get(plan["items"][0]["asset_url"], headers=headers["operator"])
    assert asset.status_code == 200 and asset.content[:4] == b"glTF"
    for role in ("other", "display"):
        assert client.get(url, headers=headers[role]).status_code == 403
        assert client.get(plan["avatar"]["asset_url"], headers=headers[role]).status_code == 403
    # Retried inputs preserve their pinned identity, without running reviewed validation.
    assert (
        client.post("/api/v1/translate", json=request, headers=headers["operator"]).json() == result
    )
    assert client.get("/api/v1/review/motions", headers=headers["operator"]).status_code == 403
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0


def test_unrecognized_text_and_unconfirmed_voice_use_meaningful_fallback(preview_environment):
    client, headers, _, sessions, _ = preview_environment
    for source in ("🙏", "anything whatever", "Train 1201 arrives at platform 999"):
        result = client.post(
            "/api/v1/translate", json=payload(text=source), headers=headers["operator"]
        ).json()
        if source == "🙏":
            assert result["manifest"] is None
            assert result["retrieval"]["retrieval_status"] == "EMPTY"
        else:
            assert result["status"] == "READY" and result["manifest"]["items"]
        assert not result["retrieval"]["last_resort"]
    with sessions() as session, session.begin():
        transcript = VoiceTranscript(
            owner_subject="operator",
            text=payload()["text"],
            audio_sha256="a" * 64,
            asr_metadata={"final": True},
            valid_until=datetime.now(UTC) + timedelta(minutes=15),
        )
        session.add(transcript)
        session.flush()
        tid = str(transcript.id)
    voice = client.post(
        "/api/v1/translate",
        headers=headers["operator"],
        json=payload(input_type="VOICE", transcript_id=tid),
    ).json()
    assert voice["status"] == "READY" and voice["manifest"]["items"]


def test_strict_server_remains_available_and_cannot_publish_development(preview_environment):
    client, headers, configured, _, _ = preview_environment
    prepared = client.post("/api/v1/translate", json=payload(), headers=headers["operator"]).json()[
        "manifest"
    ]
    publication = {
        "station_id": "TEST",
        "source_event_id": str(uuid4()),
        "source_revision": 1,
        "expected_revision": 0,
        "reason": "isolated test",
        "preview_manifest_id": prepared["manifest_id"],
        "preview_manifest_hash": prepared["manifest_hash"],
    }
    rejected = client.post("/api/v1/announcements", json=publication, headers=headers["operator"])
    assert rejected.status_code == 422 and rejected.json()["code"] == "PREVIEW_REQUIRED"
    with TestClient(
        create_app(configured.model_copy(update={"demo_mode_enabled": False}))
    ) as strict:
        assert (
            strict.get(
                f"/api/v1/playback/{prepared['manifest_id']}", headers=headers["operator"]
            ).status_code
            == 403
        )
        result = strict.post("/api/v1/translate", json=payload(), headers=headers["operator"])
        assert result.json()["status"] == "UNSUPPORTED"


def test_development_live_delivery_station_scope_completion_and_disabled_server(
    preview_environment,
):
    from app.live import acknowledge, connect_display, reconcile, register_device
    from app.live_schema import DeviceRegistration, DisplayAck

    client, headers, configured, sessions, _ = preview_environment
    private = client.post("/api/v1/translate", json=payload(), headers=headers["operator"]).json()
    publication = {
        "station_id": "TEST",
        "source_event_id": private["input_id"],
        "source_revision": 1,
        "expected_revision": 0,
        "reason": "development live test",
        "preview_manifest_id": private["manifest"]["manifest_id"],
        "preview_manifest_hash": private["manifest"]["manifest_hash"],
    }
    assert (
        client.post(
            "/api/v1/development/announcements", json=publication, headers=headers["other"]
        ).status_code
        == 403
    )
    result = client.post(
        "/api/v1/development/announcements", json=publication, headers=headers["operator"]
    )
    assert result.status_code == 200, result.text
    published = result.json()
    assert (
        client.post(
            "/api/v1/development/announcements", json=publication, headers=headers["operator"]
        ).json()
        == published
    )
    principals = {value.subject: value for value in configured.principals.values()}
    display = principals["display"]
    admin = principals["operator"].model_copy(update={"roles": {"admin"}})
    did = uuid4()
    with sessions() as session:
        register_device(
            session,
            did,
            DeviceRegistration(
                station_id="TEST", subject="display", name="Isolated display", expected_revision=0
            ),
            admin,
        )
    sid = connect_display(sessions, did, display)
    sync = reconcile(sessions, None, did, display, sid, 0, 30, True)
    active = next(
        item for item in sync["active"] if str(item["manifest_id"]) == published["manifest_id"]
    )
    assert active["caption_text"] == "Train 1201 arrives at platform 2"
    acknowledge(
        sessions, did, display, sid, DisplayAck(type="HEARTBEAT", cursor=sync["cursor"]), 30, True
    )
    url = f"/api/v1/playback/{published['manifest_id']}"
    response = client.get(url, headers=headers["display"])
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["schema_version"] == 5 and plan["development"]
    assert len(plan["items"]) == len(private["manifest"]["items"])
    assert "review_id" not in plan["announcement"]
    assert client.get(url, headers=headers["operator"]).status_code == 403
    assert client.get(plan["items"][0]["asset_url"], headers=headers["display"]).status_code == 200
    for state in ("RECEIVED", "ASSETS_READY", "STARTED", "COMPLETED"):
        acknowledge(
            sessions,
            did,
            display,
            sid,
            DisplayAck(
                type="ACK",
                cursor=sync["cursor"],
                manifest_id=plan["manifest_id"],
                state=state,
                boundary=len(plan["items"]) - 1 if state == "COMPLETED" else -1,
            ),
            30,
            True,
        )
    fresh = reconcile(sessions, None, did, display, sid, sync["cursor"], 30, True)
    assert fresh["active"][0]["progress"]["state"] == "COMPLETED"
    assert not reconcile(sessions, None, did, display, sid, 0, 30)["active"]
    with TestClient(
        create_app(configured.model_copy(update={"demo_mode_enabled": False}))
    ) as strict:
        assert (
            strict.post(
                "/api/v1/development/announcements", json=publication, headers=headers["operator"]
            ).status_code
            == 403
        )
        assert strict.get(url, headers=headers["display"]).status_code == 403
