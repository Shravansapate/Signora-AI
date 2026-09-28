"""Station-scoped routing, optimistic concurrency and persistent operator history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import Field
from sqlalchemy import text

from app.announcements import check_station_scope
from app.config import Principal
from app.lifecycle import _event, require
from app.lifecycle_schema import ReviewText
from app.live import now, one, stream_lock
from app.live_schema import Label
from app.models import Station
from app.retrieval.playback_schema import WireModel
from app.templates import definition_hash


def operator_scope(identity, station):
    require(bool(identity.roles & {"operator", "admin"}), "ROLE_REQUIRED", "Operator required", 403)
    check_station_scope(identity, station)


def targets(session, station, request):
    rows = (
        session.execute(
            text("""SELECT d.id,coalesce(r.revision,0) AS route_revision
      FROM display_devices d LEFT JOIN display_routes r ON r.display_id=d.id
      WHERE d.station_id=:station AND d.enabled ORDER BY d.id"""),
            {"station": station},
        )
        .mappings()
        .all()
    )
    selected = (
        rows if request.audience == "ALL" else [r for r in rows if r["id"] in request.display_ids]
    )
    require(bool(selected), "NO_DISPLAYS", "No enabled displays selected", 422)
    require(len(selected) <= 256, "DISPLAY_LIMIT", "At most 256 displays per dispatch", 422)
    require(
        request.audience == "ALL" or len(selected) == len(request.display_ids),
        "DISPLAY_SCOPE",
        "One or more displays are disabled or outside this station",
        403,
    )
    for row in selected:
        require(
            request.expected_routes.get(row["id"]) == row["route_revision"],
            "STALE_ASSIGNMENT",
            "Display assignments changed. Refresh and review the targets.",
        )
    return selected


def assign(session, rows, manifest, identity, action, reason):
    for row in rows:
        previous = one(session, "SELECT * FROM display_routes WHERE display_id=:id", id=row["id"])
        rev = (previous["revision"] if previous else 0) + 1
        session.execute(
            text("""INSERT INTO display_routes(display_id,manifest_id,revision)
          VALUES(:id,:manifest,:revision) ON CONFLICT(display_id) DO UPDATE SET
          manifest_id=:manifest,revision=:revision,updated_at=now()"""),
            {"id": row["id"], "manifest": manifest, "revision": rev},
        )
        session.execute(
            text("""INSERT INTO display_route_history
          (display_id,manifest_id,previous_manifest_id,action,actor,reason,route_revision)
          VALUES(:id,:manifest,:previous,:action,:actor,:reason,:revision)"""),
            {
                "id": row["id"],
                "manifest": manifest,
                "previous": previous["manifest_id"] if previous else None,
                "action": action,
                "actor": identity.subject,
                "reason": reason,
                "revision": rev,
            },
        )


def assigned_to(session, display_id, manifest_id):
    """Legacy station queues remain readable only until a display is explicitly controlled."""
    return bool(
        session.scalar(
            text("""SELECT EXISTS(SELECT 1 FROM announcement_revisions a
      WHERE a.manifest_id=:manifest AND (
        EXISTS(SELECT 1 FROM display_routes r WHERE r.display_id=:id AND r.manifest_id=:manifest)
        OR (NOT a.targeted AND NOT EXISTS(SELECT 1 FROM display_routes r
                  WHERE r.display_id=:id))))"""),
            {"id": display_id, "manifest": manifest_id},
        )
    )


class StopDisplays(WireModel):
    request_id: UUID
    station_id: Label
    display_ids: list[UUID] = Field(min_length=1, max_length=256)
    expected_routes: dict[UUID, int] = Field(max_length=256)
    reason: ReviewText
    audience: str = "SELECTED"


class PlatformAssignment(WireModel):
    expected_revision: int = Field(ge=1)
    platform: Label | None = None
    reason: ReviewText


def control_router(sessions, principal):
    router = APIRouter(prefix="/api/v1/control-room")
    Identity = Annotated[Principal, Depends(principal)]

    @router.get("/displays")
    def snapshot(station_id: str, response: Response, identity: Identity):
        operator_scope(identity, station_id)
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session:
            rows = (
                session.execute(
                    text("""SELECT d.id,d.name,d.platform,d.enabled,d.revision,
              d.last_seen,coalesce(d.enabled AND d.lease_until>now(),false) AS online,
              coalesce(r.revision,0) AS route_revision,r.manifest_id,
              CASE WHEN a.state='LIVE' AND ar.valid_until>now()
                   THEN p.payload->>'caption_text' END AS caption,
              CASE WHEN r.display_id IS NULL THEN 'LEGACY'
                   WHEN r.manifest_id IS NULL THEN 'IDLE'
                   WHEN a.state<>'LIVE' OR ar.valid_until<=now() THEN 'EXPIRED'
                   WHEN dd.state='STARTED' THEN 'PLAYING'
                   WHEN dd.state='COMPLETED' THEN 'COMPLETE'
                   ELSE coalesce(dd.state,'QUEUED') END AS playback,
              dd.error_code,ar.priority,ar.valid_until
              FROM display_devices d LEFT JOIN display_routes r ON r.display_id=d.id
              LEFT JOIN playback_manifests p ON p.id=r.manifest_id
              LEFT JOIN announcement_revisions ar ON ar.manifest_id=r.manifest_id
              LEFT JOIN announcements a ON a.id=ar.message_id
              LEFT JOIN display_deliveries dd ON dd.display_id=d.id AND dd.manifest_id=r.manifest_id
              WHERE d.station_id=:station ORDER BY d.platform NULLS LAST,d.name,d.id LIMIT 257"""),
                    {"station": station_id},
                )
                .mappings()
                .all()
            )
            require(len(rows) <= 256, "DISPLAY_LIMIT", "Station exceeds 256 displays", 422)
            history = session.execute(
                text("""SELECT h.*,d.name,p.payload->>'caption_text' AS caption
              FROM display_route_history h JOIN display_devices d ON d.id=h.display_id
              LEFT JOIN playback_manifests p ON p.id=h.manifest_id
              WHERE d.station_id=:station ORDER BY h.id DESC LIMIT 100"""),
                {"station": station_id},
            ).mappings()
            return {
                "items": [dict(r) for r in rows],
                "history": [dict(r) for r in history],
                "server_time": now(),
            }

    @router.post("/stop")
    def stop(request: StopDisplays, identity: Identity):
        operator_scope(identity, request.station_id)
        require(
            request.audience == "SELECTED"
            and len(set(request.display_ids)) == len(request.display_ids),
            "INVALID_TARGETS",
            "Use unique selected display IDs",
            422,
        )
        digest = definition_hash(request.model_dump(mode="json"))
        with sessions() as session, session.begin():
            stream_lock(session, request.station_id)
            prior = one(
                session, "SELECT * FROM display_commands WHERE id=:id", id=request.request_id
            )
            if prior:
                require(
                    prior["actor"] == identity.subject and prior["request_hash"] == digest,
                    "IDEMPOTENCY_CONFLICT",
                    "Command ID was used for another request",
                )
                return {"status": "STOPPED", "request_id": request.request_id}
            rows = targets(session, request.station_id, request)
            assign(session, rows, None, identity, "STOP", request.reason)
            session.execute(
                text("""INSERT INTO display_commands(id,actor,station_id,request_hash)
              VALUES(:id,:actor,:station,:hash)"""),
                {
                    "id": request.request_id,
                    "actor": identity.subject,
                    "station": request.station_id,
                    "hash": digest,
                },
            )
            _event(
                session,
                identity.subject,
                "DISPLAYS_STOPPED",
                request.request_id,
                {"display_ids": [str(r["id"]) for r in rows], "reason": request.reason},
            )
            return {"status": "STOPPED", "request_id": request.request_id}

    @router.post("/displays/{display_id}/platform")
    def platform(display_id: UUID, request: PlatformAssignment, identity: Identity):
        with sessions() as session, session.begin():
            row = one(
                session, "SELECT * FROM display_devices WHERE id=:id FOR UPDATE", id=display_id
            )
            require(row is not None, "NOT_FOUND", "Display not found", 404)
            operator_scope(identity, row["station_id"])
            require(
                row["revision"] == request.expected_revision,
                "STALE_REVISION",
                "Refresh display details",
            )
            station = session.get(Station, row["station_id"])
            require(
                request.platform is None or request.platform in station.definition["platforms"],
                "PLATFORM_UNKNOWN",
                "Choose a configured platform",
                422,
            )
            session.execute(
                text(
                    "UPDATE display_devices SET platform=:platform,revision=revision+1 WHERE id=:id"
                ),
                {"id": display_id, "platform": request.platform},
            )
            _event(
                session,
                identity.subject,
                "DISPLAY_PLATFORM_CHANGED",
                display_id,
                {
                    "platform": request.platform,
                    "previous": row["platform"],
                    "reason": request.reason,
                },
            )
            return {"id": display_id, "platform": request.platform, "revision": row["revision"] + 1}

    return router
