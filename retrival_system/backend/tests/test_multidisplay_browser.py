"""Browser service harness; credentials stay in child environment, not output."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def run_control_browser(settings, operator_token, devices, tmp_path):
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8000))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "SIGNORA_DATABASE_URL": settings.database_url.get_secret_value(),
        "SIGNORA_STORAGE_ROOT": str(settings.storage_root),
        "SIGNORA_PRINCIPALS": json.dumps(
            {k: v.model_dump(mode="json") for k, v in settings.principals.items()}
        ),
        "SIGNORA_DEMO_MODE_ENABLED": "true",
        "SIGNORA_WEBSOCKET_ORIGINS": json.dumps([origin]),
        "SIGNORA_DISPLAY_POLL_SECONDS": "0.2",
        "SIGNORA_DISPLAY_LEASE_SECONDS": "30",
        "SIGNORA_BROWSER_ORIGIN": origin,
        "SIGNORA_BROWSER_TOKEN": operator_token,
        "SIGNORA_BROWSER_DISPLAYS": json.dumps(
            [{"id": str(d["id"]), "token": d["token"]} for d in devices]
        ),
        "SIGNORA_BROWSER_OUTPUT": str(tmp_path / "browser-evidence"),
        "SIGNORA_NEXT_DIST_DIR": ".next-test",
        "SIGNORA_BACKEND_URL": "http://127.0.0.1:8000",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    processes = []
    with (tmp_path / "services.log").open("wb") as log:
        try:
            build = subprocess.run(
                ["node", "node_modules/next/dist/bin/next", "build"],
                cwd=root / "frontend",
                env=env,
                stdout=log,
                stderr=log,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert build.returncode == 0, f"Frontend build failed; see {tmp_path}/services.log"
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
                    ],
                    root,
                ),
                (
                    [
                        "node",
                        "node_modules/next/dist/bin/next",
                        "start",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    root / "frontend",
                ),
            ]:
                processes.append(
                    subprocess.Popen(
                        args,
                        cwd=cwd,
                        env=env,
                        stdout=log,
                        stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                )
            for address in ["http://127.0.0.1:8000/health/ready", origin]:
                for _ in range(100):
                    try:
                        if httpx.get(address, timeout=2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise AssertionError(
                        f"Service unavailable: {address}; see {tmp_path}/services.log"
                    )
            result = subprocess.run(
                ["node", "scripts/verify-multi-display.mjs"],
                cwd=root / "frontend",
                env=env,
                timeout=900,
                capture_output=True,
                text=True,
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
                process.wait(timeout=15)
