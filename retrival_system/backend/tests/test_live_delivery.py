"""Real PostgreSQL transactions/sockets and source GLBs; synthetic reviews stay isolated."""
# ruff: noqa: F811 -- reuse the existing isolated registry and review fixtures.

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect
from test_announcements import announcement_setup, approved_template, payload  # noqa: F401
from test_lifecycle import (  # noqa: F401
    approve_fixture,
    history,
    lifecycle_clients,
    request_ok,
    switch,
    variant,
)
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.lifecycle import LifecycleError
from app.live import acknowledge, connect_display, publish, reconcile
from app.live_schema import DisplayAck, Publication
from app.main import create_app
from app.models import PlaybackRecord
from app.playback import PlaybackUnavailable, validate_record
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def live_setup(approved_template, lifecycle_clients, registry_database):
    settings, _, sessions = registry_database
    _, headers = lifecycle_clients
    principals = {}
    for role, header in headers.items():
        token = header["Authorization"].split()[1]
        principals[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            subject=role,
            roles={"reviewer" if role == "other-reviewer" else role},
            station_ids={"TEST"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    token = secrets.token_hex(32)
    display = Principal(
        subject="test-device",
        roles={"display"},
        station_ids={"TEST"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    principals[hashlib.sha256(token.encode()).hexdigest()] = display
    configured = settings.model_copy(
        update={
            "principals": principals,
            "websocket_origins": {"http://testserver"},
            "display_poll_seconds": 0.1,
        }
    )
    did = uuid4()
    with TestClient(create_app(configured)) as client:
        request_ok(
            client,
            headers,
            f"/api/v1/admin/displays/{did}",
            {
                "station_id": "TEST",
                "subject": display.subject,
                "name": "Synthetic display",
                "expected_revision": 0,
            },
        )
        yield client, headers, did, token, display, configured


def publication(client, headers, *, source=None, revision=1, expected=0, **changes):
    preview = request_ok(client, headers, "/api/v1/translate", payload())
    assert preview["status"] == "READY", preview
    return {
        "station_id": "TEST",
        "source_event_id": source or uuid4().hex,
        "source_revision": revision,
        "expected_revision": expected,
        "preview_manifest_id": preview["manifest"]["manifest_id"],
        "preview_manifest_hash": preview["manifest"]["manifest_hash"],
        "reason": "Isolated publication test",
        **changes,
    }


def test_atomic_publication_idempotency_and_commit_order(live_setup, registry_database):
    client, headers, _, _, _, configured = live_setup
    _, engine, sessions = registry_database
    request = publication(client, headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: request_ok(client, headers, "/api/v1/announcements", request), range(2)
            )
        )
    assert results[0] == results[1] and results[0]["revision"] == 1
    mid = UUID(results[0]["message_id"])
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM event_outbox WHERE message_id=:id"), {"id": mid}
            )
            == 1
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE announcement_revisions SET priority=0 WHERE message_id=:id"),
                {"id": mid},
            )
    conflict = client.post(
        "/api/v1/announcements", headers=headers["admin"], json={**request, "reason": "different"}
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    # Inject failure before commit, after all publication writes have run.
    actor = next(p for p in configured.principals.values() if p.subject == "admin")
    failed = Publication.model_validate(publication(client, headers))
    from unittest.mock import patch

    with patch("app.live.append_event", side_effect=RuntimeError("simulated pre-commit crash")):
        with sessions() as session, pytest.raises(RuntimeError):
            publish(session, LocalAssetStore(configured.storage_root), failed, actor)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM announcements WHERE source_event_id=:source"),
                {"source": failed.source_event_id},
            )
            == 0
        )
    # Concurrent distinct events allocate contiguous station order, never raw sequence order.
    requests = [publication(client, headers) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda r: request_ok(client, headers, "/api/v1/announcements", r), requests))
    with engine.connect() as connection:
        cursors = list(
            connection.scalars(
                text("SELECT cursor FROM event_outbox WHERE station_id='TEST' ORDER BY cursor")
            )
        )
        assert cursors == list(range(1, len(cursors) + 1))


def test_socket_recovery_acknowledgements_and_display_asset_scope(live_setup, registry_database):
    client, headers, did, token, display, configured = live_setup
    _, engine, _ = registry_database
    published = request_ok(client, headers, "/api/v1/announcements", publication(client, headers))
    path = f"/api/v1/displays/{did}/events"
    asset_headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect(path, headers={"origin": "http://testserver"}) as socket:
        socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
        sync = socket.receive_json()
        assert sync["type"] == "SYNC" and any(
            r["manifest_id"] == published["manifest_id"] for r in sync["active"]
        )
        socket.send_json({"type": "HEARTBEAT", "cursor": sync["cursor"]})
        assert socket.receive_json()["type"] == "ACKNOWLEDGED"
        response = client.get(f"/api/v1/playback/{published['manifest_id']}", headers=asset_headers)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["schema_version"] == 4 and plan["operational"] is True
        assert client.get(plan["items"][0]["asset_url"], headers=asset_headers).status_code == 200
        for state in ("RECEIVED", "ASSETS_READY", "STARTED", "COMPLETED"):
            sync = socket.receive_json()
            socket.send_json(
                {
                    "type": "ACK",
                    "cursor": sync["cursor"],
                    "manifest_id": published["manifest_id"],
                    "state": state,
                    "boundary": 7 if state == "COMPLETED" else -1,
                }
            )
            assert socket.receive_json()["state"] == state
    # New application instance: progress is SQL state, independent of socket process memory.
    with TestClient(create_app(configured)) as restarted:
        with restarted.websocket_connect(path, headers={"origin": "http://testserver"}) as socket:
            socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
            replay = socket.receive_json()
            item = next(r for r in replay["active"] if r["manifest_id"] == published["manifest_id"])
            assert item["progress"]["state"] == "COMPLETED"
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT received_cursor FROM display_devices WHERE id=:id"), {"id": did}
            )
            == sync["cursor"]
        )
    for role in ("operator", "reviewer", "display"):
        assert client.post("/api/v1/announcements", headers=headers[role], json={}).status_code in (
            403,
            422,
        )
    assert (
        client.get(
            f"/api/v1/playback/{published['manifest_id']}", headers=headers["display"]
        ).status_code
        == 403
    )


def test_cancellation_between_visible_start_and_ack_keeps_socket(live_setup):
    client, headers, did, token, _, _ = live_setup
    body = publication(client, headers)
    published = request_ok(client, headers, "/api/v1/announcements", body)
    with client.websocket_connect(
        f"/api/v1/displays/{did}/events", headers={"origin": "http://testserver"}
    ) as socket:
        socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
        for state in ("RECEIVED", "ASSETS_READY"):
            sync = socket.receive_json()
            socket.send_json(
                {
                    "type": "ACK",
                    "cursor": sync["cursor"],
                    "manifest_id": published["manifest_id"],
                    "state": state,
                }
            )
            assert socket.receive_json()["state"] == state
        sync = socket.receive_json()
        request_ok(
            client,
            headers,
            f"/api/v1/announcements/{published['message_id']}/revisions",
            {
                "station_id": "TEST",
                "source_event_id": body["source_event_id"],
                "source_revision": 2,
                "expected_revision": 1,
                "cancel": True,
                "reason": "Synthetic correction race",
            },
        )
        socket.send_json(
            {
                "type": "ACK",
                "cursor": sync["cursor"],
                "manifest_id": published["manifest_id"],
                "state": "STARTED",
            }
        )
        assert socket.receive_json()["accepted"] is False
        sync = socket.receive_json()
        assert published["manifest_id"] not in [i["manifest_id"] for i in sync["active"]]
        socket.send_json(
            {
                "type": "ACK",
                "cursor": sync["cursor"],
                "manifest_id": published["manifest_id"],
                "state": "FAILED",
                "error_code": "SUPERSEDED",
            }
        )
        assert socket.receive_json()["state"] == "FAILED"


def test_correction_cancel_lease_and_snapshot_recovery(live_setup, registry_database):
    client, headers, did, _, display, configured = live_setup
    _, engine, sessions = registry_database
    first_request = publication(client, headers)
    first = request_ok(client, headers, "/api/v1/announcements", first_request)
    second_request = publication(
        client, headers, source=first_request["source_event_id"], revision=2, expected=1
    )
    second = request_ok(
        client, headers, f"/api/v1/announcements/{first['message_id']}/revisions", second_request
    )
    store = LocalAssetStore(configured.storage_root)
    sid = connect_display(sessions, did, display)
    sync = reconcile(sessions, store, did, display, sid, 0, 15)
    ids = [str(item["manifest_id"]) for item in sync["active"]]
    assert first["manifest_id"] not in ids and second["manifest_id"] in ids
    with sessions() as session:
        with pytest.raises(PlaybackUnavailable):
            validate_record(session, store, session.get(PlaybackRecord, UUID(first["manifest_id"])))
    stale = client.post(
        f"/api/v1/announcements/{first['message_id']}/revisions",
        headers=headers["admin"],
        json={**second_request, "source_revision": 1, "expected_revision": 2},
    )
    assert stale.status_code == 409
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE station_streams SET replay_floor=cursor WHERE station_id='TEST'")
        )
    assert reconcile(sessions, store, did, display, sid, 0, 15)["mode"] == "SNAPSHOT"
    for state in ("RECEIVED", "ASSETS_READY"):
        acknowledge(
            sessions,
            did,
            display,
            sid,
            DisplayAck(
                type="ACK", cursor=sync["cursor"], manifest_id=second["manifest_id"], state=state
            ),
            15,
        )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE display_devices SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": did},
        )
    with pytest.raises(LifecycleError, match="Fresh heartbeat"):
        acknowledge(
            sessions,
            did,
            display,
            sid,
            DisplayAck(
                type="ACK",
                cursor=sync["cursor"],
                manifest_id=second["manifest_id"],
                state="STARTED",
            ),
            15,
        )
    cancelled = request_ok(
        client,
        headers,
        f"/api/v1/announcements/{first['message_id']}/revisions",
        {
            "station_id": "TEST",
            "source_event_id": first_request["source_event_id"],
            "source_revision": 3,
            "expected_revision": 2,
            "cancel": True,
            "reason": "Synthetic cancellation",
        },
    )
    assert cancelled["state"] == "CANCELLED" and cancelled["manifest_id"] is None
    assert second["manifest_id"] not in [
        str(i["manifest_id"])
        for i in reconcile(sessions, store, did, display, sid, 0, 15)["active"]
    ]


@pytest.mark.parametrize(
    "origin,token", [("https://untrusted.example", None), ("http://testserver", "invalid")]
)
def test_socket_origin_and_credentials_are_enforced(live_setup, origin, token):
    client, _, did, real_token, _, _ = live_setup
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/v1/displays/{did}/events", headers={"origin": origin}
        ) as socket:
            socket.send_json({"type": "HELLO", "token": token or real_token, "cursor": 0})
            socket.receive_json()


def test_idle_socket_timeout_can_reconnect(live_setup):
    _, _, did, token, _, configured = live_setup
    short_lease = configured.model_copy(update={"display_lease_seconds": 5})
    with TestClient(create_app(short_lease)) as client:
        with client.websocket_connect(
            f"/api/v1/displays/{did}/events", headers={"origin": "http://testserver"}
        ) as socket:
            socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
            assert socket.receive_json()["type"] == "SYNC"
            with pytest.raises(WebSocketDisconnect) as failure:
                socket.receive_json()
            assert failure.value.code == 1013


def test_display_subject_cannot_be_registered_twice(live_setup):
    client, headers, _, _, display, _ = live_setup
    response = client.post(
        f"/api/v1/admin/displays/{uuid4()}",
        headers=headers["admin"],
        json={
            "station_id": "TEST",
            "subject": display.subject,
            "name": "Duplicate test device",
            "expected_revision": 0,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DEVICE_IDENTITY"


def test_deactivation_expiry_and_ack_guards(live_setup, registry_database, approved_template):
    from unittest.mock import patch

    client, headers, did, token, display, configured = live_setup
    _, engine, sessions = registry_database
    published = request_ok(client, headers, "/api/v1/announcements", publication(client, headers))
    for role in ("reviewer", "display"):
        valid_request = publication(client, headers)
        assert (
            client.post(
                "/api/v1/announcements", headers=headers[role], json=valid_request
            ).status_code
            == 403
        )
    sid = connect_display(sessions, did, display)
    store = LocalAssetStore(configured.storage_root)
    sync = reconcile(sessions, store, did, display, sid, 0, 15)
    with pytest.raises(LifecycleError, match="Receipt precedes"):
        acknowledge(
            sessions,
            did,
            display,
            sid,
            DisplayAck(
                type="ACK",
                cursor=sync["cursor"],
                manifest_id=published["manifest_id"],
                state="COMPLETED",
                boundary=7,
            ),
            15,
        )
    acknowledge(
        sessions,
        did,
        display,
        sid,
        DisplayAck(
            type="ACK",
            cursor=sync["cursor"],
            manifest_id=published["manifest_id"],
            state="RECEIVED",
        ),
        15,
    )
    with pytest.raises(LifecycleError, match="partial semantic groups"):
        acknowledge(
            sessions,
            did,
            display,
            sid,
            DisplayAck(
                type="ACK",
                cursor=sync["cursor"],
                manifest_id=published["manifest_id"],
                state="ASSETS_READY",
                boundary=1,
            ),
            15,
        )
    with patch("app.playback.utc_now", return_value=datetime.now(UTC) + timedelta(hours=1)):
        assert not reconcile(sessions, store, did, display, sid, 0, 15)["active"]
    cid = approved_template[0]["Arrive"]["concept_id"]
    changed = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/deactivate",
        {
            "expected_revision": history(client, headers, cid)["concept"]["revision"],
            "reason": "Synthetic withdrawal",
        },
    )
    try:
        assert published["manifest_id"] not in [
            str(i["manifest_id"])
            for i in reconcile(sessions, store, did, display, sid, 0, 15)["active"]
        ]
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT state FROM announcements WHERE id=:id"),
                    {"id": UUID(published["message_id"])},
                )
                == "WITHDRAWN"
            )
    finally:
        request_ok(
            client,
            headers,
            f"/api/v1/admin/signs/{cid}/reactivate",
            {"expected_revision": changed["revision"], "reason": "Restore isolated fixture"},
        )


def test_quality_replacement_retains_snapshot_and_revocation_withdraws_atomically(
    live_setup, registry_database, approved_template, tmp_path
):
    client, headers, did, _, display, configured = live_setup
    _, engine, sessions = registry_database
    assets, _ = approved_template
    request = publication(client, headers)
    published = request_ok(client, headers, "/api/v1/announcements", request)
    cid = assets["Train"]["concept_id"]
    retained = history(client, headers, cid)["concept"]["active_motion_version_id"]
    paths = variant(
        tmp_path, history(client, headers, cid)["concept"]["semantic_key"], marker="live-quality"
    )
    store = LocalAssetStore(configured.storage_root)
    with sessions() as session:
        staged = stage_motion(*paths, "isolated-live-test", session, store)
    approve_fixture(client, headers, cid, staged["motion_version_id"])
    switch(client, headers, cid, staged["motion_version_id"])
    with sessions() as session, session.begin():
        plan, _ = validate_record(
            session, store, session.get(PlaybackRecord, UUID(published["manifest_id"]))
        )
        assert str(plan.items[0].motion_version_id) == retained
    # Unpublished previews cannot bypass the construction's exact-version review.
    failed = client.post(
        "/api/v1/announcements",
        headers=headers["admin"],
        json={**request, "source_event_id": uuid4().hex},
    )
    assert failed.status_code == 409
    before = history(client, headers, cid)["concept"]
    request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/motions/{retained}/revoke",
        {"expected_revision": before["revision"], "reason": "Synthetic defect in retained version"},
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT state FROM announcements WHERE id=:id"),
                {"id": UUID(published["message_id"])},
            )
            == "WITHDRAWN"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM event_outbox WHERE message_id=:id "
                    "AND event_type='WITHDRAWN'"
                ),
                {"id": UUID(published["message_id"])},
            )
            == 1
        )
    sid = connect_display(sessions, did, display)
    assert published["manifest_id"] not in [
        str(i["manifest_id"])
        for i in reconcile(sessions, store, did, display, sid, 0, 15)["active"]
    ]


def test_session_fencing_and_future_cursor_are_rejected(live_setup, registry_database):
    _, _, did, _, display, configured = live_setup
    _, _, sessions = registry_database
    old = connect_display(sessions, did, display)
    current = connect_display(sessions, did, display)
    with pytest.raises(LifecycleError, match="newer display session"):
        reconcile(sessions, LocalAssetStore(configured.storage_root), did, display, old, 0, 15)
    with pytest.raises(LifecycleError, match="exceeds committed"):
        reconcile(
            sessions, LocalAssetStore(configured.storage_root), did, display, current, 999999, 15
        )


def test_live_downgrade_protects_retained_publication(live_setup, registry_database):
    from alembic.config import Config
    from sqlalchemy.exc import DBAPIError

    from alembic import command

    _, engine, _ = registry_database
    with pytest.raises(DBAPIError, match="paired pre-upgrade backup"):
        command.downgrade(Config("alembic.ini"), "0005_progressive_retrieval")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0010_display_credentials"
        )
