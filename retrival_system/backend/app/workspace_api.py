"""Authorized read models for operator and content-management interfaces."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text

from app.announcement_schema import StationId
from app.announcements import check_station_scope
from app.config import Principal
from app.lifecycle import require
from app.live import now, one
from app.models import AnnouncementTemplate, ImportJob, TemplateDependency
from app.templates import template_result


def workspace_router(sessions, principal):
    router = APIRouter(prefix="/api/v1")
    Identity = Annotated[Principal, Depends(principal)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0, le=100000)]

    def role(identity, roles):
        require(bool(identity.roles & roles), "ROLE_REQUIRED", "Workspace access denied", 403)

    def station_access(identity, station):
        role(identity, {"admin", "operator"})
        check_station_scope(identity, station)

    @router.get("/session")
    def session_identity(identity: Identity):
        return identity.model_dump(mode="json")

    @router.get("/review/signs")
    def library(
        identity: Identity,
        q: str = Query(default="", max_length=160),
        limit: Limit = 50,
        offset: Offset = 0,
    ):
        role(identity, {"admin", "reviewer"})
        with sessions() as session:
            rows = session.execute(
                text("""SELECT c.id,c.gloss,c.canonical_text,c.semantic_key,
              c.meaning_status,c.enabled,c.revision,c.active_motion_version_id,c.level,
              (SELECT enabled FROM library_selections l WHERE l.concept_id=c.id) AS library_enabled,
              EXISTS(SELECT 1 FROM eligible_motion_versions e WHERE e.concept_id=c.id) AS eligible
              FROM sign_concepts c WHERE :q='' OR strpos(lower(c.gloss || ' ' || c.canonical_text
              || ' ' || c.semantic_key),lower(:q))>0
              ORDER BY c.semantic_key,c.id LIMIT :limit OFFSET :offset"""),
                {"q": q.strip(), "limit": limit, "offset": offset},
            ).mappings()
            return {"items": [dict(row) for row in rows]}

    @router.get("/review/signs/{concept_id}/impact")
    def impact(concept_id: UUID, identity: Identity):
        role(identity, {"admin", "reviewer"})
        with sessions() as session:
            rows = session.scalars(
                select(AnnouncementTemplate)
                .join(TemplateDependency)
                .where(TemplateDependency.concept_id == concept_id)
                .order_by(AnnouncementTemplate.template_key, AnnouncementTemplate.version_no.desc())
            )
            return {"items": [template_result(row) for row in rows]}

    @router.get("/admin/imports")
    def imports(identity: Identity, limit: Limit = 50, offset: Offset = 0):
        role(identity, {"admin"})
        with sessions() as session:
            rows = session.scalars(
                select(ImportJob)
                .order_by(ImportJob.created_at.desc(), ImportJob.id)
                .limit(limit)
                .offset(offset)
            )
            return {
                "items": [
                    {
                        field: getattr(row, field)
                        for field in (
                            "id",
                            "source_alias",
                            "owner_subject",
                            "state",
                            "created_at",
                            "updated_at",
                        )
                    }
                    for row in rows
                ]
            }

    @router.get("/review/templates/{template_id}")
    def template_details(template_id: UUID, identity: Identity):
        role(identity, {"admin", "reviewer"})
        with sessions() as session:
            row = session.get(AnnouncementTemplate, template_id)
            require(row is not None, "NOT_FOUND", "Template not found", 404)
            dependencies = session.execute(
                text("""SELECT c.id,c.gloss,c.semantic_revision,
              c.active_motion_version_id,EXISTS(SELECT 1 FROM eligible_motion_versions e
              WHERE e.concept_id=c.id) AS eligible FROM template_dependencies d
              JOIN sign_concepts c ON c.id=d.concept_id WHERE d.template_version_id=:id
              ORDER BY c.semantic_key,c.id"""),
                {"id": template_id},
            ).mappings()
            return {**template_result(row), "dependencies": [dict(item) for item in dependencies]}

    @router.get("/announcements")
    def announcements(
        station_id: StationId, identity: Identity, limit: Limit = 50, offset: Offset = 0
    ):
        station_access(identity, station_id)
        with sessions() as session:
            rows = session.execute(
                text("""SELECT a.id AS message_id,a.source_subject,a.source_event_id,
              a.current_revision AS revision,a.source_revision,a.state,r.priority,r.valid_until,
              r.created_at,p.payload->>'caption_text' AS caption_text,
              (a.state='LIVE' AND r.valid_until>now()) AS current,
              (a.source_subject=:subject OR :admin) AS can_revise
              FROM announcements a JOIN announcement_revisions r ON r.message_id=a.id
              AND r.revision=a.current_revision LEFT JOIN playback_manifests p ON p.id=r.manifest_id
              WHERE a.station_id=:station ORDER BY r.created_at DESC,a.id
              LIMIT :limit OFFSET :offset"""),
                {
                    "station": station_id,
                    "subject": identity.subject,
                    "admin": "admin" in identity.roles,
                    "limit": limit,
                    "offset": offset,
                },
            ).mappings()
            return {"items": [dict(row) for row in rows], "server_time": now()}

    @router.get("/announcements/{message_id}")
    def announcement(message_id: UUID, identity: Identity, limit: Limit = 50, offset: Offset = 0):
        role(identity, {"admin", "operator"})
        with sessions() as session:
            message = one(session, "SELECT * FROM announcements WHERE id=:id", id=message_id)
            require(message is not None, "NOT_FOUND", "Announcement not found", 404)
            check_station_scope(identity, message["station_id"])
            rows = session.execute(
                text("""SELECT r.revision,r.source_revision,r.state,r.priority,
              r.reason,r.actor,r.created_at,r.valid_until,r.manifest_id,
              p.payload->>'caption_text' AS caption_text FROM announcement_revisions r
              LEFT JOIN playback_manifests p ON p.id=r.manifest_id WHERE r.message_id=:id
              ORDER BY r.revision DESC LIMIT :limit OFFSET :offset"""),
                {"id": message_id, "limit": limit, "offset": offset},
            ).mappings()
            return {**dict(message), "revisions": [dict(row) for row in rows]}

    @router.get("/operations/displays")
    def displays(station_id: StationId, identity: Identity, limit: Limit = 50, offset: Offset = 0):
        station_access(identity, station_id)
        with sessions() as session:
            rows = session.execute(
                text("""SELECT d.id,d.name,d.subject,d.station_id,d.enabled,d.revision,
              d.last_seen,d.lease_until,d.received_cursor,coalesce(s.cursor,0) AS station_cursor,
              greatest(coalesce(s.cursor,0)-d.received_cursor,0) AS receive_lag,
              (d.enabled AND d.lease_until>now()) AS lease_current,
              (SELECT count(*) FROM announcements a JOIN announcement_revisions r
                ON r.message_id=a.id AND r.revision=a.current_revision
                LEFT JOIN display_deliveries dd
                  ON dd.display_id=d.id AND dd.manifest_id=r.manifest_id
                WHERE a.station_id=d.station_id AND a.state='LIVE' AND r.valid_until>now()
                AND (EXISTS(SELECT 1 FROM display_routes dr WHERE dr.display_id=d.id
                   AND dr.manifest_id=r.manifest_id)
                  OR (NOT r.targeted AND NOT EXISTS(SELECT 1 FROM display_routes dr
                   WHERE dr.display_id=d.id)))
                AND coalesce(dd.state,'RECEIVED')<>'COMPLETED') AS pending_messages,
              (SELECT jsonb_build_object('state',dd.state,'error_code',dd.error_code,
                 'manifest_id',dd.manifest_id,'updated_at',dd.updated_at)
                 FROM display_deliveries dd WHERE dd.display_id=d.id
                 ORDER BY dd.updated_at DESC,dd.manifest_id LIMIT 1) AS latest_delivery
              FROM display_devices d LEFT JOIN station_streams s ON s.station_id=d.station_id
              WHERE d.station_id=:station ORDER BY d.name,d.id LIMIT :limit OFFSET :offset"""),
                {"station": station_id, "limit": limit, "offset": offset},
            ).mappings()
            return {"items": [dict(row) for row in rows], "server_time": now()}

    return router
