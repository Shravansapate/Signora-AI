"""Five-display routing using real PostgreSQL, retrieval and source GLBs."""

# ruff: noqa: F811
import hashlib
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_development_preview import payload, preview_environment  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.config import Principal
from app.display_credentials import authenticate, issue_token
from app.live import acknowledge, connect_display, reconcile
from app.live_schema import DisplayAck
from app.main import create_app
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def control_environment(preview_environment):
    _, headers, settings, sessions, engine = preview_environment
    principals = dict(settings.principals)
    devices = []
    operator = next(p for p in principals.values() if p.subject == "operator")
    with sessions() as session, session.begin():
        for i in range(int(os.environ.get("SIGNORA_TEST_DISPLAY_COUNT", "5"))):
            did, token = uuid4(), secrets.token_hex(32)
            identity = Principal(
                subject=f"control-display-{i}",
                roles={"display"},
                station_ids={"TEST"},
                expires_at=operator.expires_at,
            )
            principals[hashlib.sha256(token.encode()).hexdigest()] = identity
            session.execute(
                text("""INSERT INTO display_devices(id,station_id,subject,name)
              VALUES(:id,'TEST',:subject,:name)"""),
                {"id": did, "subject": identity.subject, "name": f"Platform screen {i + 1}"},
            )
            # Mixed credentials exercise new display tokens alongside legacy screens
            # through routing, real GLB access, emergencies, and socket delivery.
            if i % 2:
                token = issue_token(session, did, "test-admin", 90)["access_token"]
            devices.append({"id": did, "token": token, "identity": identity})
    configured = settings.model_copy(
        update={
            "principals": principals,
            "websocket_origins": {"http://testserver"},
            "display_poll_seconds": 0.1,
        }
    )
    for device in devices:
        device["identity"] = authenticate(configured, sessions, device["token"])
        assert device["identity"] is not None
    with TestClient(create_app(configured)) as client:
        yield client, headers, configured, sessions, devices


def test_independent_broadcast_stop_and_access(control_environment):
    client, headers, settings, sessions, devices = control_environment
    h = headers["operator"]
    store = LocalAssetStore(settings.storage_root)

    def snapshot():
        r = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    def publish(ids, caption="Train 1201 arrives at platform 2", all=False, emergency=False):
        preview = client.post("/api/v1/translate", json=payload(text=caption), headers=h).json()
        assert preview["manifest"], preview
        state = snapshot()
        body = {
            "station_id": "TEST",
            "source_event_id": str(uuid4()),
            "source_revision": 1,
            "expected_revision": 0,
            "reason": "Isolated control-room verification",
            "preview_manifest_id": preview["manifest"]["manifest_id"],
            "preview_manifest_hash": preview["manifest"]["manifest_hash"],
            "audience": "ALL" if all else "SELECTED",
            "display_ids": [] if all else [str(i) for i in ids],
            "emergency": emergency,
            "expected_routes": {r["id"]: r["route_revision"] for r in state["items"]},
        }
        r = client.post("/api/v1/development/announcements", json=body, headers=h)
        assert r.status_code == 200, r.text
        # Retrying does not change routes or add history.
        history_count = len(snapshot()["history"])
        assert (
            client.post("/api/v1/development/announcements", json=body, headers=h).json()
            == r.json()
        )
        assert len(snapshot()["history"]) == history_count
        return r.json()

    def sync(d):
        sid = connect_display(sessions, d["id"], d["identity"])
        state = reconcile(sessions, store, d["id"], d["identity"], sid, 0, 15, True)
        acknowledge(
            sessions,
            d["id"],
            d["identity"],
            sid,
            DisplayAck(type="HEARTBEAT", cursor=state["cursor"]),
            15,
            True,
        )
        return state

    ids = [d["id"] for d in devices]
    first = publish(ids[:1])
    second = publish(ids[1:3], "Train ACCIDENT")
    assert [r["manifest_id"] for r in sync(devices[0])["active"]] == [UUID(first["manifest_id"])]
    assert [r["manifest_id"] for r in sync(devices[1])["active"]] == [UUID(second["manifest_id"])]
    if len(devices) > 3:
        assert sync(devices[3])["active"] == []
    other = {"Authorization": f"Bearer {devices[1]['token']}"}
    assert client.get(f"/api/v1/playback/{first['manifest_id']}", headers=other).status_code == 403
    assert (
        client.get("/api/v1/control-room/displays?station_id=TEST", headers=other).status_code
        == 403
    )
    assert (
        client.get("/api/v1/control-room/displays?station_id=OTHER", headers=h).status_code == 403
    )
    emergency = publish(ids, "Train ACCIDENT", all=True, emergency=True)
    assert emergency["priority"] == 0
    for d in devices:
        assert [str(r["manifest_id"]) for r in sync(d)["active"]] == [emergency["manifest_id"]]
    state = snapshot()
    stop = {
        "request_id": str(uuid4()),
        "station_id": "TEST",
        "display_ids": [str(ids[0])],
        "expected_routes": {r["id"]: r["route_revision"] for r in state["items"]},
        "reason": "Stop one screen",
    }
    assert client.post("/api/v1/control-room/stop", json=stop, headers=h).status_code == 200
    assert client.post("/api/v1/control-room/stop", json=stop, headers=h).status_code == 200
    assert sync(devices[0])["active"] == []
    assert len(sync(devices[1])["active"]) == 1
    stop["request_id"] = str(uuid4())
    assert client.post("/api/v1/control-room/stop", json=stop, headers=h).status_code == 409
    row = snapshot()["items"][0]
    changed = client.post(
        f"/api/v1/control-room/displays/{row['id']}/platform",
        headers=h,
        json={"expected_revision": row["revision"], "platform": "2", "reason": "Assign platform"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["platform"] == "2"
    assert snapshot()["history"]


@pytest.mark.skipif(
    os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real browser displays"
)
def test_multi_display_browser(control_environment, tmp_path):
    from test_multidisplay_browser import run_control_browser

    _, headers, settings, _, devices = control_environment
    run_control_browser(
        settings, headers["operator"]["Authorization"].split()[1], devices, tmp_path
    )


def test_concurrent_assignments_are_atomic_and_corrections_report_superseded(control_environment):
    client, headers, settings, sessions, devices = control_environment
    h = headers["operator"]

    def snapshot():
        response = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h)
        assert response.status_code == 200
        return response.json()

    preview = client.post("/api/v1/translate", json=payload(), headers=h).json()["manifest"]
    before = snapshot()
    body = {
        "station_id": "TEST",
        "source_event_id": str(uuid4()),
        "source_revision": 1,
        "expected_revision": 0,
        "reason": "Concurrent operators regression",
        "preview_manifest_id": preview["manifest_id"],
        "preview_manifest_hash": preview["manifest_hash"],
        "audience": "SELECTED",
        "display_ids": [str(d["id"]) for d in devices[:2]],
        "expected_routes": {r["id"]: r["route_revision"] for r in before["items"]},
    }
    contenders = [body, {**body, "source_event_id": str(uuid4())}]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda b: client.post("/api/v1/development/announcements", json=b, headers=h),
                contenders,
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    winner_index = next(i for i, r in enumerate(results) if r.status_code == 200)
    winner = results[winner_index].json()
    after = snapshot()
    targeted = [r for r in after["items"] if r["id"] in body["display_ids"]]
    assert len(targeted) == 2
    assert all(r["manifest_id"] == winner["manifest_id"] for r in targeted)
    assert len(after["history"]) == len(before["history"]) + 2
    retry = client.post(
        "/api/v1/development/announcements", json=contenders[winner_index], headers=h
    )
    assert retry.status_code == 200 and retry.json() == winner
    assert len(snapshot()["history"]) == len(after["history"])

    # Correcting the source invalidates an older revision even on a screen that was
    # not included in the correction; its operator status must agree with delivery.
    correction = {
        **contenders[winner_index],
        "source_revision": 2,
        "expected_revision": 1,
        "display_ids": body["display_ids"][:1],
        "expected_routes": {r["id"]: r["route_revision"] for r in after["items"]},
    }
    response = client.post(
        "/api/v1/development/announcements",
        json=correction,
        headers=h,
    )
    assert response.status_code == 200, response.text
    outdated = next(r for r in snapshot()["items"] if r["id"] == str(devices[1]["id"]))
    assert outdated["playback"] == "SUPERSEDED" and outdated["caption"] is None
    d = devices[1]
    sid = connect_display(sessions, d["id"], d["identity"])
    assert (
        reconcile(
            sessions,
            LocalAssetStore(settings.storage_root),
            d["id"],
            d["identity"],
            sid,
            0,
            15,
            True,
        )["active"]
        == []
    )


def test_broadcast_rejects_changed_inventory_without_partial_assignment(control_environment):
    client, headers, _, sessions, devices = control_environment
    h = headers["operator"]
    before = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h).json()
    preview = client.post(
        "/api/v1/translate", json=payload(text="Train ACCIDENT"), headers=h
    ).json()["manifest"]
    body = {
        "station_id": "TEST",
        "source_event_id": str(uuid4()),
        "source_revision": 1,
        "expected_revision": 0,
        "reason": "Inventory concurrency regression",
        "preview_manifest_id": preview["manifest_id"],
        "preview_manifest_hash": preview["manifest_hash"],
        "audience": "ALL",
        "emergency": True,
        "expected_routes": {r["id"]: r["route_revision"] for r in before["items"]},
    }
    extra = uuid4()
    with sessions() as session, session.begin():
        session.execute(
            text(
                "INSERT INTO display_devices(id,station_id,subject,name) "
                "VALUES(:id,'TEST','new-display','New display')"
            ),
            {"id": extra},
        )
    try:
        rejected = client.post("/api/v1/development/announcements", json=body, headers=h)
        assert rejected.status_code == 409, rejected.text
        stop = {
            "request_id": str(uuid4()),
            "station_id": "TEST",
            "audience": "ALL",
            "display_ids": [str(d["id"]) for d in devices],
            "expected_routes": body["expected_routes"],
            "reason": "Stop all must also detect newly registered displays",
        }
        rejected_stop = client.post("/api/v1/control-room/stop", json=stop, headers=h)
        assert rejected_stop.status_code == 409, rejected_stop.text
        assert (
            client.post(
                "/api/v1/control-room/stop", json={**stop, "audience": "TYPO"}, headers=h
            ).status_code
            == 422
        )
        after = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h).json()
        assert after["history"] == before["history"]
        assert {
            r["id"]: r["route_revision"] for r in after["items"] if r["id"] != str(extra)
        } == body["expected_routes"]
        with sessions() as session, session.begin():
            session.execute(
                text("UPDATE display_devices SET enabled=false WHERE id=:id"), {"id": extra}
            )
        accepted = client.post("/api/v1/development/announcements", json=body, headers=h)
        assert accepted.status_code == 200, accepted.text
        current = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h).json()
        assert all(
            r["manifest_id"] == accepted.json()["manifest_id"]
            for r in current["items"]
            if r["enabled"]
        )
        assert next(r for r in current["items"] if r["id"] == str(extra))["manifest_id"] is None
        stop["expected_routes"] = {r["id"]: r["route_revision"] for r in current["items"]}
        stopped = client.post("/api/v1/control-room/stop", json=stop, headers=h)
        assert stopped.status_code == 200, stopped.text
        retry = client.post("/api/v1/control-room/stop", json=stop, headers=h)
        assert retry.status_code == 200 and retry.json() == stopped.json()
        stopped_state = client.get(
            "/api/v1/control-room/displays?station_id=TEST", headers=h
        ).json()
        assert all(r["manifest_id"] is None for r in stopped_state["items"])
    finally:
        with sessions() as session, session.begin():
            session.execute(text("DELETE FROM display_devices WHERE id=:id"), {"id": extra})


def test_emergency_fanout_over_real_websockets(control_environment):
    client, headers, _, _, devices = control_environment
    h = headers["operator"]
    # Keep every authenticated socket open at once, as separate station displays do.
    with ExitStack() as stack:
        sockets = []
        for device in devices:
            ws = stack.enter_context(
                client.websocket_connect(
                    f"/api/v1/displays/{device['id']}/events",
                    headers={"origin": "http://testserver"},
                )
            )
            ws.send_json({"type": "HELLO", "token": device["token"], "cursor": 0})
            sockets.append((ws, ws.receive_json()))
        before = client.get("/api/v1/control-room/displays?station_id=TEST", headers=h).json()
        preview = client.post(
            "/api/v1/translate", json=payload(text="Train ACCIDENT"), headers=h
        ).json()["manifest"]
        publication = client.post(
            "/api/v1/development/announcements",
            headers=h,
            json={
                "station_id": "TEST",
                "source_event_id": str(uuid4()),
                "source_revision": 1,
                "expected_revision": 0,
                "reason": "Real concurrent WebSocket emergency regression",
                "audience": "ALL",
                "emergency": True,
                "preview_manifest_id": preview["manifest_id"],
                "preview_manifest_hash": preview["manifest_hash"],
                "expected_routes": {r["id"]: r["route_revision"] for r in before["items"]},
            },
        )
        assert publication.status_code == 200, publication.text
        for ws, previous in sockets:
            ws.send_json({"type": "HEARTBEAT", "cursor": previous["cursor"]})
            assert ws.receive_json()["type"] == "ACKNOWLEDGED"
            sync = ws.receive_json()
            assert sync["type"] == "SYNC" and len(sync["active"]) == 1
            assert sync["active"][0]["manifest_id"] == publication.json()["manifest_id"]
            assert sync["active"][0]["priority"] == 0
        # A database-issued token must authorize the actual manifest and GLB,
        # not just the WebSocket connection and announcement envelope.
        managed = next(d for d in devices if d["token"].startswith("sgd_"))
        credential = {"Authorization": f"Bearer {managed['token']}"}
        plan = client.get(
            f"/api/v1/playback/{publication.json()['manifest_id']}", headers=credential
        )
        assert plan.status_code == 200, plan.text
        assert client.get(
            plan.json()["items"][0]["asset_url"], headers=credential
        ).status_code == 200
