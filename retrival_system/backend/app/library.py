"""Library maintenance without rewriting immutable GLBs or inventing reviews."""

from sqlalchemy import delete, select

from app.assets.metadata import load_metadata, metadata_findings
from app.lifecycle import _concept, _event, _motion, _result, require
from app.models import (
    AvatarProfile,
    LibrarySelection,
    MotionMetadataRevision,
    MotionVersion,
    RetrievalProfile,
    SignAlias,
)
from app.playback import PlaybackUnavailable, _check_bytes, _check_content


def select_library(session, concept, motion_id, enabled):
    row = session.get(LibrarySelection, concept.id)
    if row is None:
        row = LibrarySelection(concept_id=concept.id)
        session.add(row)
    row.motion_version_id, row.enabled = motion_id, enabled
    return row


def validate_development(rows, profile_id, store):
    try:
        _check_content(rows, profile_id, development=True)
        _check_bytes(rows, store)
    except PlaybackUnavailable as exc:
        from app.lifecycle import LifecycleError

        raise LifecycleError("VERSION_UNAVAILABLE", str(exc)) from exc


def development_switch(session, store, concept_id, request, actor, *, rollback=False):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        require(
            concept.active_motion_version_id == request.expected_active_motion_version_id,
            "STALE_POINTER",
            "The production selection changed; refresh the library",
        )
        candidate = _motion(session, concept, request.motion_version_id)
        if rollback:
            require(
                candidate.lifecycle_status == "ARCHIVED",
                "NOT_RETAINED",
                "Select an archived version",
            )
        profile = session.get(AvatarProfile, candidate.avatar_profile_id)
        rows = {candidate.id: (candidate, concept, profile)}
        validate_development(rows, profile.id, store)
        old = session.get(LibrarySelection, concept.id)
        old_id = old.motion_version_id if old else concept.active_motion_version_id
        if old_id and old_id != candidate.id:
            previous = session.get(MotionVersion, old_id)
            require(
                previous.avatar_profile_id == candidate.avatar_profile_id,
                "AVATAR_MISMATCH",
                "Replacement must retain the avatar profile",
            )
            if previous.lifecycle_status != "ACTIVE":
                previous.lifecycle_status = "ARCHIVED"
        # Production ACTIVE states and approval evidence remain untouched.
        if candidate.lifecycle_status == "ARCHIVED":
            candidate.lifecycle_status = "STAGING"
        select_library(session, concept, candidate.id, True)
        concept.revision += 1
        result = {**_result(concept), "development_motion_version_id": str(candidate.id)}
        _event(
            session,
            actor,
            "LIBRARY_ROLLED_BACK" if rollback else "LIBRARY_ACTIVATED",
            concept.id,
            {
                **result,
                "reason": request.reason,
                "previous_motion_version_id": str(old_id) if old_id else None,
            },
        )
        return result


def archive_version(session, concept_id, motion_id, request, actor, *, restore=False):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        motion = _motion(session, concept, motion_id)
        require(
            motion.revoked_at is None,
            "VERSION_REVOKED",
            "A revoked version cannot be restored or archived",
        )
        if restore:
            require(
                motion.lifecycle_status == "ARCHIVED", "NOT_ARCHIVED", "Select an archived version"
            )
            motion.lifecycle_status = "STAGING"
            if session.get(LibrarySelection, concept.id) is None:
                select_library(session, concept, None, False)
            # Restore retains bytes for preview; activation remains explicit.
        else:
            if concept.active_motion_version_id == motion.id:
                concept.active_motion_version_id = None
                concept.enabled = False
            selection = session.get(LibrarySelection, concept.id)
            if selection and selection.motion_version_id == motion.id:
                select_library(session, concept, None, False)
            motion.lifecycle_status = "ARCHIVED"
        concept.revision += 1
        _event(
            session,
            actor,
            "MOTION_RESTORED" if restore else "MOTION_ARCHIVED",
            motion.id,
            {"concept_id": str(concept.id), "reason": request.reason},
        )
        return _result(concept)


def development_enabled(session, store, concept_id, request, actor, enabled):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        selection = session.get(LibrarySelection, concept.id)
        motion_id = selection.motion_version_id if selection else concept.active_motion_version_id
        if enabled:
            require(motion_id is not None, "NO_SELECTED_VERSION", "Activate a version first")
            motion = _motion(session, concept, motion_id)
            profile = session.get(AvatarProfile, motion.avatar_profile_id)
            rows = {motion.id: (motion, concept, profile)}
            validate_development(rows, profile.id, store)
        select_library(session, concept, motion_id, enabled)
        # Deactivation also prevents strict production selection.
        if not enabled:
            concept.enabled = False
        concept.revision += 1
        _event(
            session,
            actor,
            "LIBRARY_REACTIVATED" if enabled else "LIBRARY_DEACTIVATED",
            concept.id,
            {"reason": request.reason},
        )
        return _result(concept)


def current_metadata(session, motion):
    revision = session.scalar(
        select(MotionMetadataRevision)
        .where(MotionMetadataRevision.motion_version_id == motion.id)
        .order_by(MotionMetadataRevision.created_at.desc(), MotionMetadataRevision.id.desc())
        .limit(1)
    )
    return revision.payload if revision else motion.source_metadata


def update_metadata(session, store, concept_id, motion_id, path, request, actor):
    metadata, digest, raw = load_metadata(path)
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        motion = _motion(session, concept, motion_id)
        identity = metadata.motion_identity
        require(
            identity.motion_code == concept.semantic_key
            and identity.language_code == concept.language_code,
            "IDENTITY_MISMATCH",
            "Metadata must retain the concept motion_code and language",
            422,
        )
        require(
            not any(
                f["severity"] == "error"
                for f in metadata_findings(metadata, raw, motion.sha256, motion.size_bytes)
            ),
            "ASSET_MISMATCH",
            "Metadata hashes and sizes must match the selected GLB",
            422,
        )
        require(
            raw.get("animation") == motion.source_metadata.get("animation"),
            "ANIMATION_CHANGED",
            "Animation metadata changes require a new validated GLB upload",
            422,
        )
        store.verify(motion.storage_key, motion.sha256)
        aliases = raw.get("retrieval", {}).get("aliases", [])
        require(
            isinstance(aliases, list)
            and len(aliases) <= 256
            and all(isinstance(a, str) and 0 < len(a.strip()) <= 2048 for a in aliases),
            "INVALID_ALIASES",
            "Aliases must be a list of nonempty strings",
            422,
        )
        values = dict(
            gloss=identity.gloss,
            canonical_text=identity.canonical_text,
            level=identity.level,
            domain=identity.domain,
            meaning=metadata.linguistic.meaning,
            context=metadata.linguistic.context,
        )
        before = current_metadata(session, motion)
        semantic_change = any(getattr(concept, key) != value for key, value in values.items()) or (
            before.get("retrieval", {}).get("aliases", []) != aliases
        )
        if semantic_change:
            for key, value in values.items():
                setattr(concept, key, value)
            concept.normalized_canonical_text = " ".join(identity.canonical_text.casefold().split())
            concept.semantic_revision += 1
            concept.meaning_status = "PENDING"
            concept.enabled = False
            selected = session.get(LibrarySelection, concept.id)
            select_library(
                session, concept, selected.motion_version_id if selected else motion.id, False
            )
            session.execute(delete(SignAlias).where(SignAlias.concept_id == concept.id))
            for value in dict.fromkeys(" ".join(a.casefold().split()) for a in aliases):
                session.add(
                    SignAlias(
                        concept_id=concept.id,
                        alias=value,
                        normalized_alias=value,
                        review_status="PENDING",
                    )
                )
            profile = session.get(RetrievalProfile, concept.id)
            if profile:
                profile.status = "REJECTED"
        session.add(
            MotionMetadataRevision(
                motion_version_id=motion.id,
                metadata_sha256=digest,
                payload=raw,
                actor=actor,
                reason=request.reason,
            )
        )
        concept.revision += 1
        _event(
            session,
            actor,
            "MOTION_METADATA_UPDATED",
            motion.id,
            {
                "metadata_sha256": digest,
                "semantic_change": semantic_change,
                "reason": request.reason,
            },
        )
        return {
            **_result(concept),
            "semantic_change": semantic_change,
            "status": "METADATA_UPDATED",
            "motion_version_id": str(motion.id),
        }
