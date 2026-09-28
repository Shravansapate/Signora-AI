"""Opt-in end-to-end operational protocol on disposable, explicitly synthetic content."""
# ruff: noqa: F811 -- module-isolated fixture reuse.

import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_announcements import announcement_setup, approved_template  # noqa: F401
from test_browser_playback import run_browser
from test_lifecycle import lifecycle_clients, request_ok  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.config import Principal


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real browser")
def test_real_live_delivery_browser(
    approved_template, lifecycle_clients, registry_database, tmp_path
):
    settings, _, _ = registry_database
    client, headers = lifecycle_clients
    did = uuid4()
    request_ok(
        client,
        headers,
        f"/api/v1/admin/displays/{did}",
        {
            "station_id": "TEST",
            "subject": "live-browser-device",
            "name": "Synthetic browser",
            "expected_revision": 0,
        },
    )
    identity = Principal(
        subject="live-browser-device",
        roles={"display"},
        station_ids={"TEST"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    run_browser(
        settings,
        tmp_path,
        script="verify-browser-live.mjs",
        announcements=True,
        live_display=(did, secrets.token_hex(32), identity),
    )
