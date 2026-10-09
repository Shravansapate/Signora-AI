"""FastAPI registry boundary. Business logic and storage remain separate modules."""

import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.announcement_api import announcement_router
from app.asr import LocalASR
from app.assets.khronos import KhronosValidationError
from app.assets.metadata import MAX_METADATA_BYTES
from app.config import Principal, Settings
from app.database import build_database
from app.display_credentials import authenticate
from app.http_limits import ReceivedBodyLimit, asset_upload, request_limit
from app.lifecycle import LifecycleError
from app.lifecycle_api import lifecycle_router
from app.live_api import live_router
from app.playback_api import playback_router
from app.registry import RegistryConflict, stage_motion
from app.retrieval.api import retrieval_router
from app.retrieval.encoder import LocalEncoder
from app.storage import LocalAssetStore, StorageError
from app.workspace_api import workspace_router

logger = logging.getLogger("signora.api")
bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings()
    engine, sessions = build_database(configured)
    store = LocalAssetStore(configured.storage_root)
    asr = LocalASR(configured)
    encoder = LocalEncoder(configured)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False

    @asynccontextmanager
    async def lifespan(application):
        asr.warm()
        encoder.warm()
        try:
            yield
        finally:
            asr.close()
            encoder.close()
            engine.dispose()

    app = FastAPI(title="Signora retrieval and playback", version="0.7.0", lifespan=lifespan)
    # Keep the byte limiter inside request observation so its 413 survives body parsing.
    app.add_middleware(ReceivedBodyLimit)
    app.state.asr = asr
    app.state.encoder = encoder
    upload_slots = threading.BoundedSemaphore(2)
    audio_slots = threading.BoundedSemaphore(1)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        request_id = uuid.uuid4().hex
        started = time.perf_counter()

        def finish(response):
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.url.path.startswith("/api/v1/"):
                if "/assets/" not in request.url.path or response.status_code >= 400:
                    response.headers["Cache-Control"] = "no-store"
                response.headers["Vary"] = "Authorization"
            logger.info(
                json.dumps(
                    {
                        "event": "request_complete",
                        "request_id": request_id,
                        "method": request.method,
                        "status": response.status_code,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
            )
            return response

        def reject(status, detail, headers=None):
            return finish(
                JSONResponse(status_code=status, content={"detail": detail}, headers=headers)
            )

        if request.url.path.startswith("/api/v1/"):
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            try:
                identity = (
                    await run_in_threadpool(authenticate, configured, sessions, token)
                    if scheme.casefold() == "bearer"
                    else None
                )
            except SQLAlchemyError:
                logger.error("credential_registry_unavailable")
                return reject(503, "Registry unavailable")
            if identity is None:
                return reject(401, "Authentication required", {"WWW-Authenticate": "Bearer"})
            request.state.identity = identity
            if request.url.path.startswith("/api/v1/admin/") and "admin" not in identity.roles:
                return reject(403, "Administrator role required")
            if request.url.path == "/api/v1/voice/transcribe" and not identity.roles & {
                "admin",
                "reviewer",
                "operator",
            }:
                return reject(403, "Operator or reviewer role required")
        is_upload = request.method == "POST" and asset_upload(request.url.path)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            length = request.headers.get("content-length")
            if length is None:
                return reject(411, "Content-Length required")
            limit = request_limit(request.url.path)
            if not length.isdigit() or int(length) > limit:
                return reject(413, "Request exceeds size limit")
        if is_upload and not upload_slots.acquire(blocking=False):
            return reject(429, "Asset validation busy", {"Retry-After": "5"})
        is_audio = request.method == "POST" and request.url.path == "/api/v1/voice/transcribe"
        if is_audio and not audio_slots.acquire(blocking=False):
            return reject(429, "Speech recognition busy", {"Retry-After": "5"})
        try:
            response = await call_next(request)
        finally:
            if is_upload:
                upload_slots.release()
            if is_audio:
                audio_slots.release()
        return finish(response)

    def principal(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None:
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        matched = getattr(request.state, "identity", None)
        if matched is None:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        return matched

    def admin(identity: Annotated[Principal, Depends(principal)]):
        if "admin" not in identity.roles:
            raise HTTPException(403, "Administrator role required")
        return identity

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        logger.error("database_operation_failed", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=503, content={"detail": "Registry unavailable"})

    @app.exception_handler(LifecycleError)
    async def lifecycle_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc), "code": exc.code})

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Required storage object is unavailable",
                "code": "ASSET_UNAVAILABLE",
            },
        )

    @app.get("/health/live")
    def live():
        return {"status": "LIVE"}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as connection:
                version = connection.scalar(text("SELECT version_num FROM alembic_version"))
                connection.execute(text("SELECT 1 FROM eligible_motion_versions LIMIT 1"))
            if version != "0010_display_credentials" or not configured.storage_root.is_dir():
                raise RuntimeError("Required registry contract unavailable")
        except (SQLAlchemyError, RuntimeError):
            return JSONResponse(status_code=503, content={"status": "NOT_READY"})
        return {"status": "READY", "schema_revision": version}

    @app.get("/api/v1/admin/signs")
    def signs(identity: Annotated[Principal, Depends(admin)], limit: int = 50, offset: int = 0):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "Invalid pagination")
        with sessions() as session:
            rows = session.execute(
                text("""
              SELECT id, semantic_key, gloss, language_code, domain, level, enabled,
                     meaning_status, revision, active_motion_version_id
              FROM sign_concepts ORDER BY semantic_key LIMIT :limit OFFSET :offset
            """),
                {"limit": limit, "offset": offset},
            ).mappings()
            return {"items": [dict(row) for row in rows]}

    @app.post("/api/v1/admin/motions/stage", status_code=201)
    def stage(
        identity: Annotated[Principal, Depends(admin)],
        metadata: Annotated[UploadFile, File()],
        motion: Annotated[UploadFile, File()],
        new_concept_only: Annotated[bool, Form()] = False,
    ):
        try:
            with store.stage(metadata.file, MAX_METADATA_BYTES) as metadata_path:
                with store.stage(motion.file) as motion_path:
                    with sessions() as session:
                        return stage_motion(
                            metadata_path,
                            motion_path,
                            identity.subject,
                            session,
                            store,
                            manage_library=True,
                            new_concept_only=new_concept_only,
                            allow_reupload_deleted=configured.demo_mode_enabled,
                        )
        except RegistryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KhronosValidationError as exc:
            raise HTTPException(503, "Structural validator unavailable") from exc
        except (ValueError, StorageError) as exc:
            raise HTTPException(422, "Asset or metadata validation failed") from exc

    app.include_router(
        playback_router(sessions, store, principal, demo_mode_enabled=configured.demo_mode_enabled)
    )
    app.include_router(workspace_router(sessions, principal))
    app.include_router(lifecycle_router(sessions, store, configured, principal))
    app.include_router(
        announcement_router(
            sessions, store, principal, asr, demo_mode_enabled=configured.demo_mode_enabled
        )
    )
    app.include_router(retrieval_router(sessions, principal, encoder))
    from app.display_control import control_router

    app.include_router(control_router(sessions, principal))
    app.include_router(live_router(sessions, store, configured, principal))
    return app
