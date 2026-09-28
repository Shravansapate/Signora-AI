"""Transactional catalog lifecycle. Source evidence and immutable bytes retain identity."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select

from app.catalog import catalog_lock
from app.models import (
    AuditLog,
    AvatarProfile,
    CleanupJob,
    ContentReview,
    LibrarySelection,
    MotionMetadataRevision,
    MotionVersion,
    PlaybackItemRecord,
    PlaybackRecord,
    RegistryEvent,
    SignConcept,
    TemplateMotionBinding,
)
from app.playback import PlaybackUnavailable, validate_record
from app.storage import StorageError


class LifecycleError(ValueError):
    def __init__(self, code, detail, status=409):
        super().__init__(detail)
        self.code = code
        self.status = status


def require(condition, code, detail, status=409):
    if not condition:
        raise LifecycleError(code, detail, status)


def _concept(session, concept_id, expected):
    catalog_lock(session, write=True)
    concept = session.get(SignConcept, concept_id, with_for_update=True)
    require(concept is not None, "NOT_FOUND", "Concept not found", 404)
    require(
        concept.revision == expected,
        "STALE_REVISION",
        "Catalog changed; refresh the current revision",
    )
    return concept


def _motion(session, concept, motion_id):
    motion = session.get(MotionVersion, motion_id, with_for_update=True)
    require(
        motion is not None and motion.concept_id == concept.id,
        "NOT_FOUND",
        "Motion is not part of this concept",
        404,
    )
    require(motion.deleted_at is None, "VERSION_DELETED", "The motion version was deleted")
    return motion


def _event(session, actor, action, entity_id, details):
    session.add(AuditLog(actor=actor, action=action, entity_id=entity_id, details=details))
    session.add(RegistryEvent(event_type=action, entity_id=entity_id, payload=details))


def _record_review(session, actor, entity, entity_type, request):
    session.add(
        ContentReview(
            entity_id=entity.id,
            entity_type=entity_type,
            actor=actor,
            evidence=request.evidence,
            reason=request.reason,
            decision=request.model_dump(mode="json"),
        )
    )


def _result(concept):
    return {
        "concept_id": str(concept.id),
        "revision": concept.revision,
        "semantic_revision": concept.semantic_revision,
        "enabled": concept.enabled,
        "active_motion_version_id": str(concept.active_motion_version_id)
        if concept.active_motion_version_id
        else None,
    }


def _usable(session, store, concept, motion):
    profile = session.get(AvatarProfile, motion.avatar_profile_id)
    require(
        concept.language_code == "ISL"
        and concept.meaning_status == "APPROVED"
        and all(
            value and value.strip() for value in (concept.meaning, concept.context, concept.domain)
        ),
        "MEANING_UNAPPROVED",
        "Reviewed ISL meaning, context and domain are required",
    )
    require(
        profile is not None and profile.status == "APPROVED",
        "AVATAR_UNAPPROVED",
        "The canonical avatar is not approved",
    )
    require(
        motion.deleted_at is None
        and motion.revoked_at is None
        and motion.lifecycle_status != "REJECTED",
        "VERSION_UNAVAILABLE",
        "Deleted, rejected or revoked versions cannot be activated",
    )
    require(
        motion.technical_qc_status == "PASSED"
        and motion.linguistic_review_status == motion.composition_review_status == "APPROVED"
        and motion.reviewed_sha256 == motion.sha256
        and motion.reviewed_avatar_profile_id == motion.avatar_profile_id
        and motion.reviewed_semantic_revision == concept.semantic_revision
        and motion.reviewer
        and motion.reviewed_at,
        "REVIEW_REQUIRED",
        "This exact motion and semantic revision require approval",
    )
    try:
        store.verify(motion.storage_key, motion.sha256)
        canonical = session.scalar(
            select(MotionVersion)
            .where(
                MotionVersion.avatar_profile_id == profile.id,
                MotionVersion.sha256 == profile.source_sha256,
                MotionVersion.deleted_at.is_(None),
                MotionVersion.revoked_at.is_(None),
                MotionVersion.technical_qc_status == "PASSED",
                MotionVersion.lifecycle_status != "REJECTED",
            )
            .limit(1)
        )
        require(
            canonical is not None, "AVATAR_UNAVAILABLE", "Canonical avatar bytes are unavailable"
        )
        if canonical.id != motion.id:
            store.verify(canonical.storage_key, canonical.sha256)
    except StorageError as exc:
        raise LifecycleError(
            "ASSET_UNAVAILABLE", "Required bytes are unavailable or failed integrity validation"
        ) from exc


def switch_version(session, store, concept_id, request, actor, *, rollback=False):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        require(
            concept.active_motion_version_id == request.expected_active_motion_version_id,
            "STALE_POINTER",
            "The selected active version changed",
        )
        candidate = _motion(session, concept, request.motion_version_id)
        if rollback:
            require(
                candidate.lifecycle_status == "ARCHIVED",
                "NOT_RETAINED",
                "Rollback selects a retained archived version",
            )
        _usable(session, store, concept, candidate)
        previous_id = concept.active_motion_version_id
        if previous_id and previous_id != candidate.id:
            previous = session.get(MotionVersion, previous_id)
            require(
                previous.avatar_profile_id == candidate.avatar_profile_id,
                "AVATAR_MISMATCH",
                "Quality replacement must preserve the avatar profile",
            )
            previous.lifecycle_status = "ARCHIVED"
            session.flush()  # Release the partial ACTIVE uniqueness before promoting the candidate.
        candidate.lifecycle_status = "ACTIVE"
        concept.active_motion_version_id = candidate.id
        concept.enabled = True
        from app.library import select_library

        select_library(session, concept, candidate.id, True)
        concept.revision += 1
        result = _result(concept)
        _event(
            session,
            actor,
            "MOTION_ROLLED_BACK" if rollback else "MOTION_ACTIVATED",
            concept.id,
            {
                **result,
                "previous_motion_version_id": str(previous_id) if previous_id else None,
                "reason": request.reason,
                "sha256": candidate.sha256,
            },
        )
        return result


def set_enabled(session, store, concept_id, request, actor, enabled):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        if enabled:
            require(
                concept.active_motion_version_id is not None,
                "NO_SELECTED_VERSION",
                "Select an approved version before reactivation",
            )
            _usable(
                session, store, concept, _motion(session, concept, concept.active_motion_version_id)
            )
        concept.enabled = enabled
        from app.library import select_library

        select_library(session, concept, concept.active_motion_version_id, enabled)
        concept.revision += 1
        result = _result(concept)
        _event(
            session,
            actor,
            "CONCEPT_REACTIVATED" if enabled else "CONCEPT_DEACTIVATED",
            concept.id,
            {**result, "reason": request.reason},
        )
        return result


def revoke_version(session, concept_id, motion_id, request, actor):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        motion = _motion(session, concept, motion_id)
        require(motion.revoked_at is None, "ALREADY_REVOKED", "The version is already revoked")
        if concept.active_motion_version_id == motion.id:
            concept.active_motion_version_id = None
            concept.enabled = False
            motion.lifecycle_status = "ARCHIVED"
        motion.revoked_at = datetime.now(UTC)
        selection = session.get(LibrarySelection, concept.id)
        if selection and selection.motion_version_id == motion.id:
            selection.enabled = False
            selection.motion_version_id = None
        concept.revision += 1
        result = {**_result(concept), "motion_version_id": str(motion.id)}
        _event(session, actor, "MOTION_REVOKED", motion.id, {**result, "reason": request.reason})
        return result


def review_meaning(session, concept_id, request, actor):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        require(
            not concept.enabled,
            "DEACTIVATE_FIRST",
            "Deactivate this concept before changing its meaning",
        )
        if concept.active_motion_version_id:
            session.get(
                MotionVersion, concept.active_motion_version_id
            ).lifecycle_status = "ARCHIVED"
            concept.active_motion_version_id = None
        concept.meaning, concept.context, concept.domain = (
            request.meaning,
            request.context,
            request.domain,
        )
        concept.meaning_status = request.decision
        concept.semantic_revision += 1
        concept.revision += 1
        _record_review(session, actor, concept, "MEANING", request)
        result = _result(concept)
        _event(
            session,
            actor,
            "MEANING_REVIEWED",
            concept.id,
            {**result, "decision": request.decision, "reason": request.reason},
        )
        return result


def review_avatar(session, store, avatar_id, request, actor):
    with session.begin():
        catalog_lock(session, write=True)
        profile = session.get(AvatarProfile, avatar_id, with_for_update=True)
        require(profile is not None, "NOT_FOUND", "Avatar profile not found", 404)
        require(
            profile.revision == request.expected_revision,
            "STALE_REVISION",
            "The avatar review revision changed",
        )
        require(
            profile.source_sha256 == request.sha256
            and profile.rig_fingerprint == request.rig_fingerprint,
            "IDENTITY_MISMATCH",
            "The review must identify the exact canonical avatar bytes and rig",
        )
        if request.decision == "APPROVED":
            source = session.scalar(
                select(MotionVersion)
                .where(
                    MotionVersion.sha256 == profile.source_sha256,
                    MotionVersion.avatar_profile_id == profile.id,
                    MotionVersion.deleted_at.is_(None),
                    MotionVersion.revoked_at.is_(None),
                    MotionVersion.technical_qc_status == "PASSED",
                )
                .limit(1)
            )
            require(
                source is not None, "AVATAR_UNAVAILABLE", "Canonical avatar source is unavailable"
            )
            store.verify(source.storage_key, source.sha256)
        profile.status = request.decision
        profile.revision += 1
        _record_review(session, actor, profile, "AVATAR", request)
        result = {
            "avatar_profile_id": str(profile.id),
            "revision": profile.revision,
            "status": profile.status,
        }
        _event(session, actor, "AVATAR_REVIEWED", profile.id, {**result, "reason": request.reason})
        return result


def review_motion(session, store, concept_id, motion_id, request, actor):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        motion = _motion(session, concept, motion_id)
        require(
            motion.revoked_at is None, "VERSION_REVOKED", "Revocation cannot be removed by review"
        )
        require(
            request.sha256 == motion.sha256
            and request.avatar_profile_id == motion.avatar_profile_id
            and request.semantic_revision == concept.semantic_revision,
            "IDENTITY_MISMATCH",
            "Review must bind to the current exact bytes, avatar and meaning revision",
        )
        preview = session.get(PlaybackRecord, request.preview_manifest_id)
        require(
            preview is not None
            and preview.owner_subject == actor
            and preview.purpose == "CONTENT_REVIEW",
            "PREVIEW_REQUIRED",
            "Use a content preview owned by the reviewing principal",
        )
        try:
            plan, _ = validate_record(session, store, preview)
        except PlaybackUnavailable as exc:
            raise LifecycleError("PREVIEW_STALE", str(exc)) from exc
        require(
            plan.avatar.profile_id == motion.avatar_profile_id
            and any(item.motion_version_id == motion.id for item in plan.items),
            "PREVIEW_MISMATCH",
            "The preview must include this exact version on its applicable avatar",
        )
        approved = request.linguistic == request.composition == "APPROVED"
        if approved:
            require(
                concept.meaning_status == "APPROVED",
                "MEANING_UNAPPROVED",
                "Review the concept meaning before approving its motion",
            )
        if motion.lifecycle_status == "ACTIVE" and not approved:
            motion.lifecycle_status = "ARCHIVED"
            concept.active_motion_version_id = None
            concept.enabled = False
        motion.linguistic_review_status = request.linguistic
        motion.composition_review_status = request.composition
        motion.reviewed_sha256 = motion.sha256
        motion.reviewed_avatar_profile_id = motion.avatar_profile_id
        motion.reviewed_semantic_revision = concept.semantic_revision
        motion.reviewer = actor
        motion.reviewed_at = datetime.now(UTC)
        concept.revision += 1
        _record_review(session, actor, motion, "MOTION", request)
        result = {**_result(concept), "motion_version_id": str(motion.id)}
        _event(
            session,
            actor,
            "MOTION_REVIEWED",
            motion.id,
            {
                **result,
                "linguistic": request.linguistic,
                "composition": request.composition,
                "composition_scope": request.composition_scope,
                "reason": request.reason,
            },
        )
        return result


def protected_references(session, motion, *, development_reset=False):
    # Development deletion invalidates these manifests through deleted_at;
    # retained production plans and construction evidence still protect bytes.
    disposable = or_(
        PlaybackRecord.purpose == "CONTENT_REVIEW",
        func.coalesce(PlaybackRecord.payload["schema_version"].as_integer() == 5, False),
    )
    protected_plan = ~disposable if development_reset else True
    return {
        "library_selection": session.scalar(
            select(func.count())
            .select_from(LibrarySelection)
            .where(LibrarySelection.motion_version_id == motion.id)
        ),
        "template_reviews": session.scalar(
            select(func.count())
            .select_from(TemplateMotionBinding)
            .where(TemplateMotionBinding.motion_version_id == motion.id)
        ),
        "manifest_items": session.scalar(
            select(func.count())
            .select_from(PlaybackItemRecord)
            .join(PlaybackRecord, PlaybackRecord.id == PlaybackItemRecord.manifest_id)
            .where(PlaybackItemRecord.motion_version_id == motion.id, protected_plan)
        ),
        "manifest_avatars": session.scalar(
            select(func.count())
            .select_from(PlaybackRecord)
            .where(PlaybackRecord.avatar_motion_version_id == motion.id, protected_plan)
        ),
        "canonical_avatars": session.scalar(
            select(func.count())
            .select_from(AvatarProfile)
            .where(AvatarProfile.source_sha256 == motion.sha256)
        ),
        "selected_concepts": session.scalar(
            select(func.count())
            .select_from(SignConcept)
            .where(SignConcept.active_motion_version_id == motion.id)
        ),
    }


def delete_version(
    session, concept_id, motion_id, request, actor, retention_days, *, development_enabled=False
):
    require(
        not request.development_reset or development_enabled,
        "DEVELOPMENT_ONLY",
        "Immediate reset is available only on the development server",
        403,
    )
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        motion = _motion(session, concept, motion_id)
        require(
            request.confirm_sha256 == motion.sha256,
            "IDENTITY_MISMATCH",
            "Deletion requires the exact checksum",
        )
        if request.development_reset:
            require(request.purge_metadata, "METADATA_PURGE_REQUIRED", "Reset removes metadata too")
            from app.library import select_library

            if concept.active_motion_version_id == motion.id:
                concept.active_motion_version_id = None
                concept.enabled = False
            selection = session.get(LibrarySelection, concept.id)
            if selection is None or selection.motion_version_id == motion.id:
                select_library(session, concept, None, False)
            motion.lifecycle_status = "ARCHIVED"
            motion.revoked_at = datetime.now(UTC)
            session.flush()
        require(
            motion.lifecycle_status != "ACTIVE",
            "ACTIVE_VERSION",
            "An active version cannot be deleted",
        )
        require(
            not any(
                protected_references(
                    session, motion, development_reset=request.development_reset
                ).values()
            ),
            "PROTECTED_REFERENCES",
            "This version is retained by a manifest, canonical avatar or selected concept",
        )
        latest_review = session.scalar(
            select(func.max(ContentReview.created_at)).where(ContentReview.entity_id == motion.id)
        )
        touched = max(
            value
            for value in (
                motion.created_at,
                motion.updated_at,
                motion.reviewed_at,
                motion.revoked_at,
                latest_review,
            )
            if value is not None
        )
        require(
            request.development_reset
            or touched + timedelta(days=retention_days) <= datetime.now(UTC),
            "RETENTION_PERIOD",
            f"The {retention_days}-day retention period ends at "
            f"{(touched + timedelta(days=retention_days)).isoformat()}",
        )
        motion.deleted_at = datetime.now(UTC)
        concept.revision += 1
        cleanup = CleanupJob(
            motion_version_id=motion.id,
            storage_key=motion.storage_key,
            sha256=motion.sha256,
            actor=actor,
            development_reset=request.development_reset,
        )
        session.add(cleanup)
        session.flush()
        if request.purge_metadata:
            motion.source_metadata = {}
            motion.technical_report = {}
            session.execute(
                delete(MotionMetadataRevision).where(
                    MotionMetadataRevision.motion_version_id == motion.id
                )
            )
        result = {**_result(concept), "cleanup_job_id": str(cleanup.id), "status": "CLEANUP_QUEUED"}
        result["metadata_purged"] = request.purge_metadata
        result["development_reset"] = request.development_reset
        _event(
            session,
            actor,
            "MOTION_DELETION_REQUESTED",
            motion.id,
            {**result, "sha256": motion.sha256, "reason": request.reason},
        )
        return result


def retry_cleanup(session, job_id, request, actor):
    with session.begin():
        catalog_lock(session, write=True)
        job = session.get(CleanupJob, job_id, with_for_update=True)
        require(job is not None, "NOT_FOUND", "Cleanup job not found", 404)
        require(
            job.state == "FAILED"
            and job.attempts == request.expected_attempts
            and job.attempts == job.attempt_limit,
            "CLEANUP_NOT_RETRYABLE",
            "Refresh the job; only an exhausted failed cleanup can be retried",
        )
        job.state = "QUEUED"
        job.attempt_limit += 3
        job.updated_at = datetime.now(UTC)
        _event(
            session,
            actor,
            "ASSET_CLEANUP_RETRIED",
            job.motion_version_id,
            {
                "cleanup_job_id": str(job.id),
                "attempts": job.attempts,
                "attempt_limit": job.attempt_limit,
                "reason": request.reason,
            },
        )
        return {"id": str(job.id), "state": job.state, "attempt_limit": job.attempt_limit}


def cleanup_one(session, store):
    with session.begin():
        catalog_lock(session, write=True)
        job = session.scalar(
            select(CleanupJob)
            .where(
                CleanupJob.state.in_(["QUEUED", "FAILED"]),
                CleanupJob.attempts < CleanupJob.attempt_limit,
            )
            .order_by(CleanupJob.created_at, CleanupJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.attempts += 1
        job.updated_at = datetime.now(UTC)
        motion = session.get(MotionVersion, job.motion_version_id)
        require(
            motion is not None and motion.deleted_at is not None,
            "CLEANUP_INVALID",
            "Cleanup requires a committed deletion record",
        )
        shared = session.scalar(
            select(func.count())
            .select_from(MotionVersion)
            .where(MotionVersion.storage_key == job.storage_key, MotionVersion.deleted_at.is_(None))
        )
        if shared or any(
            protected_references(session, motion, development_reset=job.development_reset).values()
        ):
            job.state = "SHARED"
        else:
            try:
                store.delete(job.storage_key, job.sha256)
                job.state = "COMPLETE"
                job.error_code = None
            except (StorageError, OSError):
                job.state = "FAILED"
                job.error_code = "STORAGE_DELETE_FAILED"
        _event(
            session,
            job.actor,
            "ASSET_CLEANUP_RESULT",
            motion.id,
            {"cleanup_job_id": str(job.id), "state": job.state, "attempts": job.attempts},
        )
        return {"id": str(job.id), "state": job.state}
