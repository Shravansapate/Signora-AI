"""Authenticated HTTP delivery for phase 2 content playback."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, text

from app.announcement_schema import AnnouncementPlan
from app.config import Principal
from app.live_schema import DevelopmentLivePlan, LivePlan
from app.models import AnnouncementInputRecord, PlaybackRecord
from app.playback import PlaybackUnavailable, prepare_exact, prepare_review, validate_record
from app.retrieval.playback_schema import ExactRequest, PlaybackPlan, PrepareResult, ReviewRequest


def playback_router(sessions, store, principal, *, demo_mode_enabled=False):
    router = APIRouter(prefix="/api/v1")
    Identity = Annotated[Principal, Depends(principal)]

    def check_role(identity, purpose):
        allowed = {"admin", "reviewer"}
        if purpose in {"EXACT_CONTENT", "ANNOUNCEMENT_PREVIEW"}:
            allowed.add("operator")
        if not identity.roles & allowed:
            raise HTTPException(403, "This role cannot access content playback")

    @router.get("/review/motions")
    def motions(response: Response, identity: Identity, limit: int = 100, offset: int = 0):
        check_role(identity, "CONTENT_REVIEW")
        response.headers["Cache-Control"] = "no-store"
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "Invalid pagination")
        with sessions() as session:
            rows = session.execute(
                text("""
              SELECT m.id AS motion_version_id, m.concept_id, c.semantic_key, c.gloss,
                     m.clip_name, m.version_no, m.duration_seconds, m.avatar_profile_id,
                     m.sha256, m.size_bytes, m.technical_qc_status, m.lifecycle_status,
                     m.linguistic_review_status, m.composition_review_status
              FROM motion_versions m JOIN sign_concepts c ON c.id=m.concept_id
              WHERE m.deleted_at IS NULL AND m.revoked_at IS NULL
              ORDER BY c.semantic_key, m.version_no DESC LIMIT :limit OFFSET :offset
            """),
                {"limit": limit, "offset": offset},
            ).mappings()
            return {"items": [dict(row) for row in rows]}

    @router.post("/review/prepare", response_model=PrepareResult)
    def review(request: ReviewRequest, response: Response, identity: Identity):
        check_role(identity, "CONTENT_REVIEW")
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session:
            return prepare_review(session, store, request, identity.subject)

    @router.post("/playback/prepare", response_model=PrepareResult)
    def exact(request: ExactRequest, response: Response, identity: Identity):
        check_role(identity, "EXACT_CONTENT")
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session:
            return prepare_exact(session, store, request, identity.subject)

    def read_plan(session, manifest_id, identity):
        from app.catalog import catalog_lock

        catalog_lock(session)
        record = session.get(PlaybackRecord, manifest_id)
        if record is None:
            raise HTTPException(404, "Playback plan not found")
        if record.purpose == "PUBLISHED":
            from app.display_control import assigned_to
            from app.live import device_access, now, one

            device = one(
                session,
                "SELECT * FROM display_devices WHERE subject=:subject",
                subject=identity.subject,
            )
            if (
                "display" not in identity.roles
                or not device
                or not device["enabled"]
                or device["station_id"] not in identity.station_ids
                or device["station_id"] != record.payload["announcement"]["station_id"]
                or not device["lease_until"]
                or device["lease_until"] <= now()
                or not assigned_to(session, device["id"], manifest_id)
            ):
                raise HTTPException(403, "Fresh station-scoped display session required")
            device_access(session, device["id"], identity)
        elif record.owner_subject != identity.subject:
            raise HTTPException(403, "Playback plan belongs to another principal")
        development = record.purpose == "PUBLISHED" and record.payload.get("schema_version") == 5
        if development and not demo_mode_enabled:
            raise HTTPException(403, "Development live delivery is disabled")
        if record.purpose == "CONTENT_REVIEW":
            receipt = session.scalar(
                select(AnnouncementInputRecord).where(
                    AnnouncementInputRecord.manifest_id == record.id,
                    AnnouncementInputRecord.owner_subject == identity.subject,
                )
            )
            development = bool(receipt and receipt.response.get("demo_mode"))
            if development:
                if not demo_mode_enabled:
                    raise HTTPException(403, "Development previews are disabled on this server")
                from app.announcements import check_station_scope

                check_station_scope(identity, receipt.station_id)
        if record.purpose != "PUBLISHED" and not development:
            check_role(identity, record.purpose)
        if record.purpose == "ANNOUNCEMENT_PREVIEW":
            from app.announcements import check_station_scope

            check_station_scope(identity, record.payload["announcement"]["station_id"])
        try:
            return validate_record(session, store, record, development=development)
        except PlaybackUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get(
        "/playback/{manifest_id}",
        response_model=DevelopmentLivePlan | LivePlan | AnnouncementPlan | PlaybackPlan,
    )
    def manifest(manifest_id: UUID, response: Response, identity: Identity):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session, session.begin():
            plan, _ = read_plan(session, manifest_id, identity)
            return plan

    @router.get("/playback/{manifest_id}/assets/{version_id}/{digest}.glb")
    def asset(manifest_id: UUID, version_id: UUID, digest: str, identity: Identity):
        with sessions() as session, session.begin():
            plan, paths = read_plan(session, manifest_id, identity)
            references = [plan.avatar, *plan.items]
            if not any(
                ref.motion_version_id == version_id and ref.sha256 == digest for ref in references
            ):
                raise HTTPException(404, "Asset is not pinned by this plan")
            return FileResponse(
                paths[version_id],
                media_type="model/gltf-binary",
                headers={
                    "ETag": f'"{digest}"',
                    "Cache-Control": "private, max-age=31536000, immutable",
                    "X-Content-Type-Options": "nosniff",
                    "Vary": "Authorization",
                },
            )

    return router
