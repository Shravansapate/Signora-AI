"""Opt-in production-browser integration, with real GLBs and a disposable database."""
# ruff: noqa: F811 -- reuse module-scoped pytest fixtures.

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_playback import playback_assets  # noqa: F401
from test_registry import registry_database  # noqa: F401

from app.config import Principal


@pytest.mark.skipif(
    os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real Chromium test"
)
def test_real_browser_playback(registry_database, playback_assets, tmp_path):
    settings, _, _ = registry_database
    run_browser(settings, tmp_path)


def run_browser(
    settings,
    tmp_path,
    *,
    script="verify-browser-playback.mjs",
    announcements=False,
    live_display=None,
    workspaces=None,
    worker=False,
):
    repository = Path(__file__).resolve().parents[2]
    frontend = repository / "frontend"
    # The production build pins the same-origin rewrite to this private loopback listener.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8000))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        frontend_port = probe.getsockname()[1]
    token = secrets.token_hex(32)
    identity = Principal(
        subject="isolated-browser-reviewer",
        roles={"operator"} if announcements else {"reviewer"},
        station_ids={"TEST"} if announcements else set(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    principals = {hashlib.sha256(token.encode()).hexdigest(): identity.model_dump(mode="json")}
    review_token = secrets.token_hex(32)
    if announcements:
        principals[hashlib.sha256(review_token.encode()).hexdigest()] = Principal(
            subject="reviewer", roles={"reviewer"}, expires_at=identity.expires_at
        ).model_dump(mode="json")
    if live_display:
        did, display_token, display_identity = live_display
        principals[hashlib.sha256(display_token.encode()).hexdigest()] = (
            display_identity.model_dump(mode="json")
        )
    admin_token = secrets.token_hex(32)
    if workspaces:
        principals[hashlib.sha256(admin_token.encode()).hexdigest()] = Principal(
            subject="workspace-browser-admin",
            roles={"admin"},
            station_ids={"TEST"},
            expires_at=identity.expires_at,
        ).model_dump(mode="json")
    environment = {
        **os.environ,
        "SIGNORA_DATABASE_URL": settings.database_url.get_secret_value(),
        "SIGNORA_STORAGE_ROOT": str(settings.storage_root),
        "SIGNORA_PRINCIPALS": json.dumps(principals),
        "SIGNORA_BACKEND_URL": "http://127.0.0.1:8000",
        "SIGNORA_BROWSER_ORIGIN": f"http://127.0.0.1:{frontend_port}",
        "SIGNORA_BROWSER_TOKEN": token,
        "SIGNORA_BROWSER_OUTPUT": str(tmp_path / "browser-evidence"),
        "NEXT_TELEMETRY_DISABLED": "1",
        "SIGNORA_NEXT_DIST_DIR": ".next-test",
        "SIGNORA_DEMO_MODE_ENABLED": str(settings.demo_mode_enabled).lower(),
        "SIGNORA_DELETION_RETENTION_DAYS": str(settings.deletion_retention_days),
    }
    if announcements:
        environment["SIGNORA_BROWSER_REVIEW_TOKEN"] = review_token
        if not live_display and not workspaces:
            environment["SIGNORA_ASR_MODEL_PATH"] = str(
                repository / "backend/artifacts/asr-small.en"
            )
        environment["SIGNORA_BROWSER_AUDIO"] = str(
            repository / "artifacts/phase-4-synthetic-announcement.wav"
        )
    if live_display:
        environment.update(
            SIGNORA_BROWSER_DISPLAY_ID=str(did),
            SIGNORA_BROWSER_DISPLAY_TOKEN=display_token,
            SIGNORA_WEBSOCKET_ORIGINS=json.dumps([environment["SIGNORA_BROWSER_ORIGIN"]]),
            SIGNORA_BROWSER_OUTPUT=str(tmp_path / "browser-evidence"),
        )
    if workspaces:
        environment.update(
            SIGNORA_BROWSER_ADMIN_TOKEN=admin_token,
            SIGNORA_WEBSOCKET_ORIGINS=json.dumps([environment["SIGNORA_BROWSER_ORIGIN"]]),
            SIGNORA_BROWSER_WORKSPACE_DATA=json.dumps(workspaces),
            SIGNORA_IMPORT_ROOTS=json.dumps(
                {key: str(value) for key, value in settings.import_roots.items()}
            ),
            SIGNORA_BROWSER_OUTPUT=str(tmp_path / "browser-evidence"),
        )
    processes = []
    with (tmp_path / "services.log").open("wb") as log:
        try:
            build = subprocess.run(
                ["node", "node_modules/next/dist/bin/next", "build"],
                cwd=frontend,
                env=environment,
                stdout=log,
                stderr=log,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert build.returncode == 0, "Production browser build failed; see services.log"
            for args, cwd in [
                (
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8000",
                        "--ws-max-size",
                        "4096",
                    ],
                    repository,
                ),
                *(
                    [([sys.executable, "-m", "app.worker", "--workers", "1"], repository)]
                    if worker
                    else []
                ),
                (
                    [
                        "node",
                        "node_modules/next/dist/bin/next",
                        "start",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        str(frontend_port),
                    ],
                    frontend,
                ),
            ]:
                processes.append(
                    subprocess.Popen(
                        args,
                        cwd=cwd,
                        env=environment,
                        stdout=log,
                        stderr=log,
                        stdin=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                )
            for url in [
                "http://127.0.0.1:8000/health/ready",
                environment["SIGNORA_BROWSER_ORIGIN"],
            ]:
                deadline = time.monotonic() + 40
                while True:
                    try:
                        if httpx.get(url, timeout=2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                        pytest.fail(
                            "Isolated browser services did not become ready; see services.log"
                        )
                    time.sleep(0.2)
            result = subprocess.run(
                ["node", f"scripts/{script}"],
                cwd=frontend,
                env=environment,
                timeout=900 if workspaces else 600,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            (tmp_path / "browser.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            for process in reversed(processes):
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
