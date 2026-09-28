"""Five-display routing using real PostgreSQL, retrieval and source GLBs."""

# ruff: noqa: F811
import hashlib
import os
import secrets
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_development_preview import payload, preview_environment  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.config import Principal
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
            devices.append({"id": did, "token": token, "identity": identity})
    configured = settings.model_copy(
        update={
            "principals": principals,
            "websocket_origins": {"http://testserver"},
            "display_poll_seconds": 0.1,
        }
    )
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
