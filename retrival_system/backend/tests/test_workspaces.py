"""Real database read models and role boundaries for Phase 7 screens."""
# ruff: noqa: F811 -- shared isolated fixture definitions.

import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient
from test_announcements import announcement_setup, approved_template  # noqa: F401
from test_lifecycle import lifecycle_clients, request_ok  # noqa: F401
from test_live_delivery import live_setup, publication  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.main import create_app


def test_workspace_library_and_exact_impact(live_setup, approved_template):
    client, headers, _, _, _, _ = live_setup
    for role in ("admin", "reviewer"):
        response = client.get("/api/v1/review/signs?q=TRAIN", headers=headers[role])
        assert response.status_code == 200
        rows = response.json()["items"]
        assert rows and all("train" in row["gloss"].lower() for row in rows)
        cid = rows[0]["id"]
        history = client.get(f"/api/v1/review/signs/{cid}", headers=headers[role]).json()
        assert "technical_report" in history["versions"][0]
        impact = client.get(f"/api/v1/review/signs/{cid}/impact", headers=headers[role]).json()
        assert impact["items"]
        template = client.get(
            f"/api/v1/review/templates/{impact['items'][0]['id']}", headers=headers[role]
        )
        assert template.status_code == 200 and template.json()["dependencies"]
        avatars = client.get("/api/v1/review/avatars", headers=headers[role]).json()
        assert avatars["items"][0]["canonical_motion_version_id"]
    for role in ("operator", "display"):
        for path in (
            "/review/signs",
            f"/review/signs/{cid}/impact",
            f"/review/templates/{impact['items'][0]['id']}",
        ):
            assert client.get("/api/v1" + path, headers=headers[role]).status_code == 403
    assert (
        client.get("/api/v1/review/signs?limit=101", headers=headers["reviewer"]).status_code == 422
    )
    assert (
        client.get("/api/v1/review/signs?q=%27%20OR%201=1--", headers=headers["reviewer"]).json()[
            "items"
        ]
        == []
    )


def test_operator_history_and_monitor_are_station_scoped(live_setup):
    client, headers, did, _, display, configured = live_setup
    body = publication(client, headers)
    published = request_ok(client, headers, "/api/v1/announcements", body)
    listing = client.get("/api/v1/announcements?station_id=TEST", headers=headers["operator"])
    assert listing.status_code == 200 and listing.json()["items"]
    item = next(
        row for row in listing.json()["items"] if row["message_id"] == published["message_id"]
    )
    assert not item["can_revise"] and item["current"]
    history = client.get(
        f"/api/v1/announcements/{published['message_id']}", headers=headers["operator"]
    )
    assert history.status_code == 200 and history.json()["revisions"][0]["revision"] == 1
    monitor = client.get("/api/v1/operations/displays?station_id=TEST", headers=headers["operator"])
    assert monitor.status_code == 200
    assert str(did) in [row["id"] for row in monitor.json()["items"]]
    assert "session_id" not in monitor.text and "token" not in monitor.text
    for role in ("display", "reviewer"):
        assert (
            client.get(
                "/api/v1/operations/displays?station_id=TEST", headers=headers[role]
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/announcements/{published['message_id']}", headers=headers[role]
            ).status_code
            == 403
        )
    token = uuid4().hex
    stranger = display.model_copy(
        update={
            "subject": "other-station-operator",
            "roles": {"operator"},
            "station_ids": {"OTHER"},
        }
    )
    settings = configured.model_copy(
        update={"principals": {hashlib.sha256(token.encode()).hexdigest(): stranger}}
    )
    with TestClient(create_app(settings)) as scoped:
        credentials = {"Authorization": f"Bearer {token}"}
        for path in (
            "/announcements?station_id=TEST",
            "/operations/displays?station_id=TEST",
            f"/announcements/{published['message_id']}",
        ):
            assert scoped.get("/api/v1" + path, headers=credentials).status_code == 403


def test_workspace_identity_and_admin_import_list(live_setup):
    client, headers, _, _, _, _ = live_setup
    for role in ("admin", "reviewer", "operator", "display"):
        result = client.get("/api/v1/session", headers=headers[role])
        assert result.status_code == 200 and role in result.json()["roles"]
        assert set(result.json()) == {"subject", "roles", "expires_at", "station_ids"}
        result = client.get("/api/v1/admin/imports", headers=headers[role])
        assert result.status_code == (200 if role == "admin" else 403)
    assert client.get("/api/v1/session").status_code == 401
