"""Role-protected review, version maintenance, import control, and history APIs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import Field
from sqlalchemy import select

from app.assets.khronos import KhronosValidationError
from app.assets.metadata import MAX_METADATA_BYTES
from app.config import Principal
from app.imports import control_import, create_import, import_status
from app.library import (
    archive_version,
    current_metadata,
    development_enabled,
    development_switch,
    update_metadata,
)
from app.lifecycle import (
    delete_version,
    protected_references,
    retry_cleanup,
    review_avatar,
    review_meaning,
    review_motion,
    revoke_version,
    set_enabled,
    switch_version,
)
from app.lifecycle_schema import (
    AvatarReview,
    CleanupRetryRequest,
    DeleteRequest,
    MeaningReview,
    MotionReview,
    RevisionRequest,
    SwitchRequest,
)
from app.models import (
    AuditLog,
    AvatarProfile,
    CleanupJob,
    ContentReview,
    LibrarySelection,
    MotionMetadataRevision,
    MotionVersion,
    RetrievalProfile,
    SignAlias,
    SignConcept,
)
from app.registry import RegistryConflict, stage_motion
from app.retrieval.playback_schema import WireModel
from app.storage import StorageError


class ImportRequest(WireModel):
    source_alias: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request_id: UUID


class ResumeRequest(WireModel):
    retry_failed: bool = False


def lifecycle_router(sessions, store, settings, principal):
    router = APIRouter(prefix="/api/v1")

    def admin(identity: Annotated[Principal, Depends(principal)]):
        if "admin" not in identity.roles:
            raise HTTPException(403, "Administrator role required")
        return identity

    def reviewer(identity: Annotated[Principal, Depends(principal)]):
        if not identity.roles & {"admin", "reviewer"}:
            raise HTTPException(403, "Content reviewer role required")
        return identity

    Admin = Annotated[Principal, Depends(admin)]
    Reviewer = Annotated[Principal, Depends(reviewer)]

    @router.get("/admin/import-sources")
    def sources(identity: Admin):
        return {"items": sorted(settings.import_roots)}

    @router.post("/admin/imports", status_code=202)
    def start_import(request: ImportRequest, identity: Admin):
        try:
            with sessions() as session:
                return create_import(
                    session, settings, request.source_alias, request.request_id, identity.subject
                )
        except OSError as exc:
            raise HTTPException(422, "Configured import source is unavailable") from exc

    @router.get("/admin/imports/{job_id}")
    def progress(job_id: UUID, identity: Admin, limit: int = 100, offset: int = 0):
        with sessions() as session:
            return import_status(session, job_id, limit, offset)

    @router.post("/admin/imports/{job_id}/pause")
    def pause(job_id: UUID, identity: Admin):
        with sessions() as session:
            return control_import(session, job_id, identity.subject, pause=True)

    @router.post("/admin/imports/{job_id}/resume")
    def resume(job_id: UUID, request: ResumeRequest, identity: Admin):
        with sessions() as session:
            return control_import(
                session, job_id, identity.subject, retry_failed=request.retry_failed
            )

    @router.get("/review/signs/{concept_id}")
    def history(concept_id: UUID, identity: Reviewer):
        with sessions() as session:
            concept = session.get(SignConcept, concept_id)
            if concept is None:
                raise HTTPException(404, "Concept not found")
            versions = session.scalars(
                select(MotionVersion)
                .where(MotionVersion.concept_id == concept_id)
                .order_by(MotionVersion.version_no.desc())
            ).all()
            fields = (
                "id",
                "version_no",
                "sha256",
                "size_bytes",
                "clip_name",
                "duration_seconds",
                "avatar_profile_id",
                "technical_qc_status",
                "technical_report",
                "linguistic_review_status",
                "composition_review_status",
                "lifecycle_status",
                "reviewed_semantic_revision",
                "reviewer",
                "reviewed_at",
                "created_at",
                "updated_at",
                "revoked_at",
                "deleted_at",
            )
            reviews = session.scalars(
                select(ContentReview)
                .where(ContentReview.entity_id.in_([concept_id, *[m.id for m in versions]]))
                .order_by(ContentReview.created_at.desc())
                .limit(200)
            )
            aliases = session.scalars(select(SignAlias).where(SignAlias.concept_id == concept_id))
            profile = session.get(RetrievalProfile, concept_id)
            return {
                "development_mode": settings.demo_mode_enabled,
                "deletion_retention_days": settings.deletion_retention_days,
                "library_selection": {
                    "motion_version_id": selection.motion_version_id,
                    "enabled": selection.enabled,
                }
                if (selection := session.get(LibrarySelection, concept_id))
                else None,
                "retrieval_profile": {
                    field: getattr(profile, field)
                    for field in (
                        "description",
                        "sense",
                        "domain",
                        "context",
                        "status",
                        "semantic_revision",
                    )
                }
                if profile
                else None,
                "concept": {
                    field: getattr(concept, field)
                    for field in (
                        "id",
                        "semantic_key",
                        "gloss",
                        "canonical_text",
                        "meaning",
                        "context",
                        "domain",
                        "level",
                        "language_code",
                        "meaning_status",
                        "enabled",
                        "revision",
                        "semantic_revision",
                        "active_motion_version_id",
                    )
                },
                "versions": [
                    {
                        **{field: getattr(m, field) for field in fields},
                        "references": protected_references(session, m),
                    }
                    for m in versions
                ],
                "aliases": [
                    {
                        "id": alias.id,
                        "alias": alias.alias,
                        "source_language": alias.source_language,
                        "review_status": alias.review_status,
                    }
                    for alias in aliases
                ],
                "reviews": [
                    {
                        "id": row.id,
                        "entity_id": row.entity_id,
                        "entity_type": row.entity_type,
                        "actor": row.actor,
                        "evidence": row.evidence,
                        "reason": row.reason,
                        "decision": row.decision,
                        "created_at": row.created_at,
                    }
                    for row in reviews
                ],
            }

    @router.get("/review/avatars")
    def avatars(identity: Reviewer):
        with sessions() as session:
            return {
                "items": [
                    {
                        "canonical_motion_version_id": session.scalar(
                            select(MotionVersion.id)
                            .where(
                                MotionVersion.avatar_profile_id == row.id,
                                MotionVersion.sha256 == row.source_sha256,
                                MotionVersion.revoked_at.is_(None),
                                MotionVersion.deleted_at.is_(None),
                            )
                            .order_by(MotionVersion.id)
                            .limit(1)
                        ),
                        **{
                            field: getattr(row, field)
                            for field in (
                                "id",
                                "rig_fingerprint",
                                "fingerprint_version",
                                "source_sha256",
                                "status",
                                "revision",
                            )
                        },
                    }
                    for row in session.scalars(select(AvatarProfile).order_by(AvatarProfile.id))
                ]
            }

    @router.post("/review/avatars/{avatar_id}")
    def avatar_decision(avatar_id: UUID, request: AvatarReview, identity: Reviewer):
        with sessions() as session:
            return review_avatar(session, store, avatar_id, request, identity.subject)

    @router.post("/review/signs/{concept_id}/meaning")
    def meaning_decision(concept_id: UUID, request: MeaningReview, identity: Reviewer):
        with sessions() as session:
            return review_meaning(session, concept_id, request, identity.subject)

    @router.post("/review/signs/{concept_id}/motions/{version_id}")
    def motion_decision(
        concept_id: UUID, version_id: UUID, request: MotionReview, identity: Reviewer
    ):
        with sessions() as session:
            return review_motion(session, store, concept_id, version_id, request, identity.subject)

    @router.post("/admin/signs/{concept_id}/motions", status_code=201)
    def upload_version(
        concept_id: UUID,
        identity: Admin,
        metadata: Annotated[UploadFile, File()],
        motion: Annotated[UploadFile, File()],
    ):
        with sessions() as session:
            if session.get(SignConcept, concept_id) is None:
                raise HTTPException(404, "Concept not found")
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
                            expected_concept_id=concept_id,
                            manage_library=True,
                            allow_reupload_deleted=settings.demo_mode_enabled,
                        )
        except RegistryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KhronosValidationError as exc:
            raise HTTPException(503, "Structural validator unavailable") from exc
        except (ValueError, StorageError) as exc:
            raise HTTPException(422, "Asset or metadata validation failed") from exc

    @router.post("/admin/signs/{concept_id}/activate")
    def activate(concept_id: UUID, request: SwitchRequest, identity: Admin):
        with sessions() as session:
            action = development_switch if settings.demo_mode_enabled else switch_version
            return action(session, store, concept_id, request, identity.subject)

    @router.post("/admin/signs/{concept_id}/rollback")
    def rollback(concept_id: UUID, request: SwitchRequest, identity: Admin):
        with sessions() as session:
            action = development_switch if settings.demo_mode_enabled else switch_version
            return action(session, store, concept_id, request, identity.subject, rollback=True)

    @router.post("/admin/signs/{concept_id}/deactivate")
    def deactivate(concept_id: UUID, request: RevisionRequest, identity: Admin):
        with sessions() as session:
            action = development_enabled if settings.demo_mode_enabled else set_enabled
            return action(session, store, concept_id, request, identity.subject, False)

    @router.post("/admin/signs/{concept_id}/reactivate")
    def reactivate(concept_id: UUID, request: RevisionRequest, identity: Admin):
        with sessions() as session:
            action = development_enabled if settings.demo_mode_enabled else set_enabled
            return action(session, store, concept_id, request, identity.subject, True)

    @router.post("/admin/signs/{concept_id}/motions/{version_id}/archive")
    def archive(concept_id: UUID, version_id: UUID, request: RevisionRequest, identity: Admin):
        with sessions() as session:
            return archive_version(session, concept_id, version_id, request, identity.subject)

    @router.post("/admin/signs/{concept_id}/motions/{version_id}/restore")
    def restore(concept_id: UUID, version_id: UUID, request: RevisionRequest, identity: Admin):
        with sessions() as session:
            return archive_version(
                session, concept_id, version_id, request, identity.subject, restore=True
            )

    @router.get("/review/signs/{concept_id}/motions/{version_id}/metadata")
    def metadata_content(concept_id: UUID, version_id: UUID, identity: Reviewer):
        with sessions() as session:
            motion = session.get(MotionVersion, version_id)
            if motion is None or motion.concept_id != concept_id or motion.deleted_at:
                raise HTTPException(404, "Motion metadata unavailable")
            return {
                "metadata": current_metadata(session, motion),
                "history": [
                    {
                        "id": row.id,
                        "sha256": row.metadata_sha256,
                        "actor": row.actor,
                        "reason": row.reason,
                        "created_at": row.created_at,
                    }
                    for row in session.scalars(
                        select(MotionMetadataRevision)
                        .where(MotionMetadataRevision.motion_version_id == version_id)
                        .order_by(MotionMetadataRevision.created_at.desc())
                    )
                ],
            }

    @router.post("/admin/signs/{concept_id}/motions/{version_id}/metadata")
    def metadata_update(
        concept_id: UUID,
        version_id: UUID,
        identity: Admin,
        metadata: Annotated[UploadFile, File()],
        expected_revision: Annotated[int, Form(ge=1)],
        reason: Annotated[str, Form(min_length=1, max_length=4000)],
    ):
        try:
            request = RevisionRequest(expected_revision=expected_revision, reason=reason)
            with store.stage(metadata.file, MAX_METADATA_BYTES) as path:
                with sessions() as session:
                    return update_metadata(
                        session, store, concept_id, version_id, path, request, identity.subject
                    )
        except (RegistryConflict, ValueError, StorageError) as exc:
            from app.lifecycle import LifecycleError

            if isinstance(exc, LifecycleError):
                raise
            raise HTTPException(
                422, "Metadata is invalid or does not match the selected GLB"
            ) from exc

    @router.post("/admin/signs/{concept_id}/motions/{version_id}/revoke")
    def revoke(concept_id: UUID, version_id: UUID, request: RevisionRequest, identity: Admin):
        with sessions() as session:
            return revoke_version(session, concept_id, version_id, request, identity.subject)

    @router.delete("/admin/signs/{concept_id}/motions/{version_id}", status_code=202)
    def delete(concept_id: UUID, version_id: UUID, request: DeleteRequest, identity: Admin):
        with sessions() as session:
            return delete_version(
                session,
                concept_id,
                version_id,
                request,
                identity.subject,
                settings.deletion_retention_days,
                development_enabled=settings.demo_mode_enabled,
            )

    @router.get("/admin/cleanup/{job_id}")
    def cleanup_status(job_id: UUID, identity: Admin):
        with sessions() as session:
            job = session.get(CleanupJob, job_id)
            if job is None:
                raise HTTPException(404, "Cleanup job not found")
            return {
                field: getattr(job, field)
                for field in (
                    "id",
                    "motion_version_id",
                    "state",
                    "attempts",
                    "attempt_limit",
                    "error_code",
                    "updated_at",
                )
            }

    @router.post("/admin/cleanup/{job_id}/retry", status_code=202)
    def cleanup_retry(job_id: UUID, request: CleanupRetryRequest, identity: Admin):
        with sessions() as session:
            return retry_cleanup(session, job_id, request, identity.subject)

    @router.get("/admin/audit")
    def audit(identity: Admin, limit: int = 100, offset: int = 0):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "Invalid pagination")
        with sessions() as session:
            rows = session.scalars(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id)
                .limit(limit)
                .offset(offset)
            )
            return {
                "items": [
                    {
                        field: getattr(row, field)
                        for field in ("id", "actor", "action", "entity_id", "details", "created_at")
                    }
                    for row in rows
                ]
            }

    return router
