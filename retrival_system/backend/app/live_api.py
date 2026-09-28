"""Bounded authenticated socket protocol; every connection reconciles committed SQL state."""

import asyncio
import hashlib
import json
import logging
import secrets
import threading
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import Principal
from app.lifecycle import LifecycleError
from app.live import acknowledge, connect_display, now, one, publish, reconcile, register_device
from app.live_schema import DeviceRegistration, DisplayAck, DisplayHello, Publication
from app.playback import PlaybackUnavailable


def live_router(sessions, store, settings, principal):
    router = APIRouter(prefix="/api/v1")
    Identity = Annotated[Principal, Depends(principal)]
    slots = threading.BoundedSemaphore(settings.display_connection_limit)

    @router.post("/development/announcements")
    def development_publish(request: Publication, identity: Identity):
        if not settings.demo_mode_enabled:
            raise HTTPException(403, "Development live delivery is disabled")
        try:
            with sessions() as session:
                return publish(session, store, request, identity, development=True)
        except PlaybackUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/announcements")
    def create(request: Publication, identity: Identity):
        try:
            with sessions() as session:
                return publish(session, store, request, identity)
        except PlaybackUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/announcements/{message_id}/revisions")
    def revise(message_id: UUID, request: Publication, identity: Identity):
        try:
            with sessions() as session:
                return publish(session, store, request, identity, message_id)
        except PlaybackUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/admin/displays/{display_id}")
    def register(display_id: UUID, request: DeviceRegistration, identity: Identity):
        with sessions() as session:
            return register_device(session, display_id, request, identity)

    @router.get("/admin/displays/{display_id}")
    def status(display_id: UUID, identity: Identity):
        if "admin" not in identity.roles:
            raise HTTPException(403, "Administrator required")
        with sessions() as session:
            device = one(
                session,
                """SELECT d.*,s.cursor AS station_cursor,
              s.cursor-d.received_cursor AS receive_lag FROM display_devices d
              LEFT JOIN station_streams s ON s.station_id=d.station_id WHERE d.id=:id""",
                id=display_id,
            )
            if not device:
                raise HTTPException(404, "Display not found")
            deliveries = (
                session.execute(
                    text("""SELECT manifest_id,state,attempt,boundary,
              error_code,updated_at FROM display_deliveries WHERE display_id=:id
              ORDER BY updated_at DESC LIMIT 64"""),
                    {"id": display_id},
                )
                .mappings()
                .all()
            )
            return {**dict(device), "deliveries": [dict(row) for row in deliveries]}

    @router.websocket("/displays/{display_id}/events")
    async def events(socket: WebSocket, display_id: UUID):
        # No credential query parameters or unvalidated cross-origin browser sockets.
        if socket.query_params or socket.headers.get("origin") not in settings.websocket_origins:
            await socket.close(code=1008)
            return
        if not slots.acquire(blocking=False):
            await socket.close(code=1013)
            return
        try:
            await socket.accept()

            async def receive():
                packet = await asyncio.wait_for(
                    socket.receive(), timeout=settings.display_lease_seconds
                )
                if packet["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(packet.get("code", 1000))
                raw = packet.get("text")
                if not isinstance(raw, str) or len(raw.encode()) > 4096:
                    raise ValueError("Socket payload exceeds limit")
                return json.loads(raw)

            hello = DisplayHello.model_validate(await asyncio.wait_for(receive(), timeout=5))
            digest = hashlib.sha256(hello.token.encode()).hexdigest()
            identity = next(
                (v for k, v in settings.principals.items() if secrets.compare_digest(k, digest)),
                None,
            )
            if not identity or identity.expires_at <= now():
                raise ValueError("Expired authentication")
            sid = await run_in_threadpool(connect_display, sessions, display_id, identity)
            cursor = hello.cursor
            while True:
                # No SQL session remains open across a socket wait or network send.
                sync = await run_in_threadpool(
                    reconcile,
                    sessions,
                    store,
                    display_id,
                    identity,
                    sid,
                    cursor,
                    settings.display_lease_seconds,
                    settings.demo_mode_enabled,
                )
                await asyncio.wait_for(socket.send_json(jsonable_encoder(sync)), timeout=5)
                # The client sends one ACK/heartbeat after applying this authoritative snapshot.
                # Unacknowledged work is resent on reconnect; send never means playback complete.
                request = DisplayAck.model_validate(await receive())
                try:
                    reply = await run_in_threadpool(
                        acknowledge,
                        sessions,
                        display_id,
                        identity,
                        sid,
                        request,
                        settings.display_lease_seconds,
                        settings.demo_mode_enabled,
                    )
                except (PlaybackUnavailable, LifecycleError) as exc:
                    # A correction can commit between visible start and its ACK.
                    # Reject that transition without treating fresh credentials as invalid.
                    if not isinstance(exc, PlaybackUnavailable) and exc.code != "LEASE_EXPIRED":
                        raise
                    reply = {
                        "type": "ACKNOWLEDGED",
                        "accepted": False,
                        "manifest_id": request.manifest_id,
                        "state": request.state,
                        "detail": "Playback is no longer current; reconcile the next snapshot.",
                    }
                cursor = request.cursor
                await asyncio.wait_for(socket.send_json(jsonable_encoder(reply)), timeout=5)
                await asyncio.sleep(settings.display_poll_seconds)
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logging.getLogger("signora.api.delivery").warning(
                "display_socket_timeout display=%s", display_id
            )
            await socket.close(code=1013)
        except (
            ValueError,
            ValidationError,
            LifecycleError,
            PlaybackUnavailable,
        ) as exc:
            logging.getLogger("signora.api.delivery").warning(
                "display_socket_rejected display=%s code=%s",
                display_id,
                getattr(exc, "code", type(exc).__name__),
            )
            await socket.close(code=1008)
        except SQLAlchemyError:
            logging.getLogger("signora.api.delivery").error("display_database_unavailable")
            await socket.close(code=1013)
        except asyncio.CancelledError:
            raise
        finally:
            slots.release()

    return router
