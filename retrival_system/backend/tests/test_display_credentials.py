"""Real database credentials, live connections, replacement, and legacy compatibility."""
# ruff: noqa: F811

import hashlib
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.websockets import WebSocketDisconnect
from test_registry import registry_database  # noqa: F401

from alembic import command
from app.config import Principal
from app.main import create_app


@pytest.fixture(scope="module")
def credentials_environment(registry_database):
    settings, _, sessions = registry_database
    tokens = {role: secrets.token_hex(32) for role in ("admin", "operator", "display")}
    principals = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(
            subject="credential-test-" + role,
            roles={role},
            station_ids={"TEST"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        for role, token in tokens.items()
    }
    settings = settings.model_copy(
        update={
            "principals": principals,
            "websocket_origins": {"http://testserver"},
            "display_poll_seconds": 0.1,
        }
    )
    headers = {role: {"Authorization": "Bearer " + token} for role, token in tokens.items()}
    with TestClient(create_app(settings)) as client:
        for station in ("TEST", "OTHER"):
            response = client.post(
                f"/api/v1/admin/stations/{station}",
                headers=headers["admin"],
                json={
                    "expected_revision": 0,
                    "reason": "Isolated credential verification",
                    "definition": {"name": station, "platforms": ["1", "2"]},
                },
            )
            assert response.status_code == 200, response.text
        yield client, headers, tokens, settings, sessions


def register(client, headers, **changes):
    did = uuid4()
    body = {
        "station_id": "TEST",
        "subject": "device-" + did.hex,
        "name": "Test display",
        "enabled": True,
        "expected_revision": 0,
        "issue_access_token": True,
        **changes,
    }
    response = client.post(f"/api/v1/admin/displays/{did}", headers=headers["admin"], json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return did, response.json(), body


def authorization(token):
    return {"Authorization": "Bearer " + token}


def hello(client, did):
    socket = client.websocket_connect(
        f"/api/v1/displays/{did}/events", headers={"origin": "http://testserver"}
    )
    return socket


def test_registration_is_immediately_usable_without_restart(credentials_environment):
    client, headers, _, settings, sessions = credentials_environment
    did, result, body = register(client, headers)
    token = result["access_token"]
    assert token.startswith("sgd_") and len(token) >= 40
    assert hashlib.sha256(token.encode()).hexdigest() not in settings.principals
    response = client.get("/api/v1/session", headers=authorization(token))
    assert response.status_code == 200 and response.json()["roles"] == ["display"]
    for path in (
        "/api/v1/admin/displays/" + str(did),
        "/api/v1/control-room/displays?station_id=TEST",
    ):
        assert client.get(path, headers=authorization(token)).status_code == 403
    assert (
        client.post(
            "/api/v1/announcements",
            json={
                "station_id": "TEST",
                "source_event_id": str(uuid4()),
                "source_revision": 1,
                "expected_revision": 0,
                "cancel": True,
                "reason": "Display must not publish",
            },
            headers=authorization(token),
        ).status_code
        == 403
    )
    with hello(client, did) as socket:
        socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
        assert socket.receive_json()["station_id"] == "TEST"
    for station in ("TEST", "OTHER"):
        other, _, _ = register(client, headers, station_id=station)
        with hello(client, other) as socket:
            socket.send_json({"type": "HELLO", "token": token, "cursor": 0})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
    assert (
        client.post(
            f"/api/v1/admin/displays/{did}", json=body, headers=headers["admin"]
        ).status_code
        == 409
    )
    read = client.get(f"/api/v1/admin/displays/{did}", headers=headers["admin"])
    assert "access_token" not in read.json() and token not in read.text
    with sessions() as session:
        row = (
            session.execute(
                text("SELECT * FROM display_credentials WHERE display_id=:id"), {"id": did}
            )
            .mappings()
            .one()
        )
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(dict(row))
    audit = client.get("/api/v1/admin/audit", headers=headers["admin"])
    assert token not in audit.text
    # A second app process uses the same durable registry without new configuration.
    with TestClient(create_app(settings)) as second:
        assert second.get("/api/v1/session", headers=authorization(token)).status_code == 200


def test_replacement_revokes_old_http_and_open_socket(credentials_environment):
    client, headers, _, _, _ = credentials_environment
    did, initial, _ = register(client, headers)
    old = initial["access_token"]
    body = {"expected_revision": initial["revision"], "reason": "Lost token recovery"}
    with hello(client, did) as socket:
        socket.send_json({"type": "HELLO", "token": old, "cursor": 0})
        sync = socket.receive_json()
        replaced = client.post(
            f"/api/v1/admin/displays/{did}/token", json=body, headers=headers["admin"]
        )
        assert replaced.status_code == 200, replaced.text
        new = replaced.json()["access_token"]
        assert new != old and replaced.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/session", headers=authorization(old)).status_code == 401
        assert client.get("/api/v1/session", headers=authorization(new)).status_code == 200
        socket.send_json({"type": "HEARTBEAT", "cursor": sync["cursor"]})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert (
        client.post(
            f"/api/v1/admin/displays/{did}/token", json=body, headers=headers["admin"]
        ).status_code
        == 409
    )
    for role in ("operator", "display"):
        assert (
            client.post(
                f"/api/v1/admin/displays/{did}/token", json=body, headers=headers[role]
            ).status_code
            == 403
        )


def test_disabled_and_expired_tokens_are_rejected(credentials_environment):
    client, headers, _, _, sessions = credentials_environment
    did, initial, body = register(client, headers)
    token = initial["access_token"]
    updated = client.post(
        f"/api/v1/admin/displays/{did}",
        headers=headers["admin"],
        json={**body, "issue_access_token": False, "expected_revision": 1, "enabled": False},
    )
    assert updated.status_code == 200
    assert client.get("/api/v1/session", headers=authorization(token)).status_code == 401
    with sessions() as session, session.begin():
        session.execute(text("UPDATE display_devices SET enabled=true WHERE id=:id"), {"id": did})
        session.execute(
            text(
                "UPDATE display_credentials SET expires_at=now()-interval '1 second' "
                "WHERE display_id=:id"
            ),
            {"id": did},
        )
    assert client.get("/api/v1/session", headers=authorization(token)).status_code == 401


def test_legacy_credentials_work_until_explicit_replacement(credentials_environment):
    client, headers, tokens, _, _ = credentials_environment
    did, result, _ = register(
        client, headers, subject="credential-test-display", issue_access_token=False
    )
    assert "access_token" not in result
    with hello(client, did) as socket:
        socket.send_json({"type": "HELLO", "token": tokens["display"], "cursor": 0})
        assert socket.receive_json()["type"] == "SYNC"
    result = client.post(
        f"/api/v1/admin/displays/{did}/token",
        headers=headers["admin"],
        json={"expected_revision": 1, "reason": "Migrate static device token"},
    )
    assert result.status_code == 200
    assert client.get("/api/v1/session", headers=headers["display"]).status_code == 401
    assert (
        client.get(
            "/api/v1/session", headers=authorization(result.json()["access_token"])
        ).status_code
        == 200
    )


def test_concurrent_replacements_cannot_silently_overwrite(credentials_environment):
    client, headers, _, _, _ = credentials_environment
    did, _, _ = register(client, headers)

    def replace(_):
        return client.post(
            f"/api/v1/admin/displays/{did}/token",
            headers=headers["admin"],
            json={"expected_revision": 1, "reason": "Concurrent token replacement"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    token = next(r.json()["access_token"] for r in results if r.status_code == 200)
    assert client.get("/api/v1/session", headers=authorization(token)).status_code == 200


def test_downgrade_cannot_silently_discard_issued_tokens(credentials_environment):
    client, headers, _, _, sessions = credentials_environment
    _, issued, _ = register(client, headers)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="Issued credentials require"):
        command.downgrade(config, "0009_display_routing")
    with sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0010_display_credentials"
        )
    assert (
        client.get("/api/v1/session", headers=authorization(issued["access_token"])).status_code
        == 200
    )


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real browser")
def test_display_registration_browser(credentials_environment, tmp_path):
    from test_browser_playback import run_browser

    _, _, _, settings, _ = credentials_environment
    run_browser(
        settings,
        tmp_path,
        script="verify-display-registration.mjs",
        announcements=True,
        workspaces={"display_registration": True},
    )
