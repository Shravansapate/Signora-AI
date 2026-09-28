import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Principal, Settings
from app.main import create_app


def test_expired_credentials_and_unauthorized_uploads_fail_before_parsing(tmp_path):
    token = secrets.token_hex(32)
    settings = Settings(
        database_url="postgresql+psycopg://unavailable:unused@127.0.0.1:1/unused",
        storage_root=tmp_path,
        principals={
            hashlib.sha256(token.encode()).hexdigest(): Principal(
                subject="expired",
                roles={"admin"},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        },
    )
    with TestClient(create_app(settings)) as client:
        expired = client.get("/api/v1/admin/signs", headers={"Authorization": f"Bearer {token}"})
        assert expired.status_code == 401
        upload = client.post("/api/v1/admin/motions/stage", content=b"invalid multipart")
        assert upload.status_code == 401
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        review = client.post("/api/v1/review/prepare", content=b"{invalid-json")
        assert review.status_code == 401
        assert review.headers["cache-control"] == "no-store"
        assert review.headers["x-request-id"]


def test_json_requests_are_bounded_before_body_parsing(tmp_path):
    token = secrets.token_hex(32)
    settings = Settings(
        database_url="postgresql+psycopg://unavailable:unused@127.0.0.1:1/unused",
        storage_root=tmp_path,
        principals={
            hashlib.sha256(token.encode()).hexdigest(): Principal(
                subject="reviewer",
                roles={"reviewer"},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        },
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/review/prepare",
            content=b" " * 32769,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
