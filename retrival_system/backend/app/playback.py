"""Exact selection and private content-review snapshots; no sentence composition."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import defer

from app.catalog import catalog_lock
from app.models import (
    AuditLog,
    AvatarProfile,
    MotionVersion,
    PlaybackItemRecord,
    PlaybackRecord,
    SignConcept,
)
from app.retrieval.playback_schema import (
    AvatarReference,
    PlaybackItem,
    PlaybackPlan,
    PrepareResult,
)
from app.storage import StorageError

PLAN_TTL_SECONDS = 900


def utc_now():
    return datetime.now(UTC)


class PlaybackUnavailable(ValueError):
    pass


def semantic_hash(plan):
    """Transport URLs carry no semantic identity and can later be renewed."""

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v) for k, v in value.items() if k not in {"asset_url", "manifest_hash"}
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            clean(plan.model_dump(mode="json")), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _locked_versions(session, ids):
    catalog_lock(session)
    rows = session.execute(
        select(MotionVersion, SignConcept, AvatarProfile)
        .options(defer(MotionVersion.source_metadata), defer(MotionVersion.technical_report))
        .join(SignConcept, MotionVersion.concept_id == SignConcept.id)
        .join(AvatarProfile, MotionVersion.avatar_profile_id == AvatarProfile.id)
        .where(MotionVersion.id.in_(set(ids)))
        .order_by(MotionVersion.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(set(ids)):
        raise PlaybackUnavailable("A required motion version is absent")
    return {motion.id: (motion, concept, profile) for motion, concept, profile in rows}


def _check_content(rows, profile_id, *, development=False):
    for motion, concept, profile in rows.values():
        if (
            concept.language_code != "ISL"
            or motion.avatar_profile_id != profile_id
            or (not development and profile.status not in {"PENDING", "APPROVED"})
            or motion.technical_qc_status != "PASSED"
            or motion.lifecycle_status not in {"STAGING", "ACTIVE", "ARCHIVED"}
            or motion.revoked_at is not None
            or motion.deleted_at is not None
            or (not development and motion.linguistic_review_status == "REJECTED")
            or (not development and motion.composition_review_status == "REJECTED")
        ):
            raise PlaybackUnavailable("A required motion is incompatible or unavailable for review")


def _check_bytes(rows, store):
    paths = {}
    for motion, _, _ in rows.values():
        path = store.verify(motion.storage_key, motion.sha256)
        if path.stat().st_size != motion.size_bytes:
            raise StorageError("Asset size changed")
        paths[motion.id] = path
    return paths


def _eligible_ids(session, ids):
    query = text("SELECT id FROM eligible_motion_versions WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    return set(session.scalars(query, {"ids": list(set(ids))}))


def _persist(
    session,
    rows,
    ids,
    avatar_id,
    owner,
    purpose,
    caption,
    *,
    context=None,
    groups=None,
    boundaries=None,
    valid_until=None,
    live=None,
):
    avatar, _, profile = rows[avatar_id]
    issued = utc_now()
    plan_id = uuid4()

    def asset_url(motion):
        return f"/api/v1/playback/{plan_id}/assets/{motion.id}/{motion.sha256}.glb"

    duration = sum(rows[item][0].duration_seconds for item in ids)
    if duration > 600:
        raise PlaybackUnavailable("The complete sequence exceeds the ten-minute review limit")
    plan_type, extra = PlaybackPlan, {}
    if context is not None:
        from app.announcement_schema import AnnouncementPlan

        plan_type, extra = AnnouncementPlan, {"announcement": context}
    if live is not None:
        from app.live_schema import LivePlan

        plan_type, extra = LivePlan, {"announcement": context, "delivery": live}
    plan = plan_type(
        **extra,
        manifest_id=plan_id,
        manifest_hash="0" * 64,
        purpose=purpose,
        issued_at=issued,
        valid_until=valid_until or issued + timedelta(seconds=PLAN_TTL_SECONDS),
        caption_text=caption,
        avatar=AvatarReference(
            profile_id=profile.id,
            rig_fingerprint=profile.rig_fingerprint,
            motion_version_id=avatar.id,
            sha256=avatar.sha256,
            size_bytes=avatar.size_bytes,
            asset_url=asset_url(avatar),
        ),
        items=[
            PlaybackItem(
                sequence_index=index,
                semantic_key=rows[item][1].semantic_key,
                concept_id=rows[item][1].id,
                motion_version_id=item,
                version_no=rows[item][0].version_no,
                sha256=rows[item][0].sha256,
                size_bytes=rows[item][0].size_bytes,
                asset_url=asset_url(rows[item][0]),
                clip_name=rows[item][0].clip_name,
                duration_seconds=rows[item][0].duration_seconds,
                semantic_group=groups[index]
                if groups
                else (f"review-item-{index}" if purpose == "CONTENT_REVIEW" else "content"),
                transition="APPROVED_CUT"
                if context
                else ("REVIEW_CUT" if purpose == "CONTENT_REVIEW" else "NONE"),
            )
            for index, item in enumerate(ids)
        ],
        safe_boundaries=boundaries if boundaries is not None else list(range(len(ids))),
        estimated_duration_seconds=duration,
    )
    plan = plan.model_copy(update={"manifest_hash": semantic_hash(plan)})
    session.add(
        PlaybackRecord(
            id=plan_id,
            owner_subject=owner,
            purpose=purpose,
            avatar_motion_version_id=avatar_id,
            manifest_hash=plan.manifest_hash,
            issued_at=issued,
            valid_until=plan.valid_until,
            payload=plan.model_dump(mode="json"),
            selection_revisions={
                str(concept.id): concept.semantic_revision for _, concept, _ in rows.values()
            },
        )
    )
    session.flush()
    session.add_all(
        [
            PlaybackItemRecord(manifest_id=plan_id, sequence_index=i, motion_version_id=item)
            for i, item in enumerate(ids)
        ]
    )
    session.add(
        AuditLog(
            actor=owner,
            action="PLAYBACK_PREPARED",
            entity_id=plan_id,
            details={"purpose": purpose, "item_count": len(ids), "hash": plan.manifest_hash},
        )
    )
    return plan if context is not None else PrepareResult(status="READY", manifest=plan)


def prepare_review(session, store, request, owner):
    try:
        with session.begin():
            rows = _locked_versions(
                session, [request.avatar_motion_version_id, *request.motion_version_ids]
            )
            _check_content(rows, rows[request.avatar_motion_version_id][0].avatar_profile_id)
            _check_bytes(rows, store)
            return _persist(
                session,
                rows,
                request.motion_version_ids,
                request.avatar_motion_version_id,
                owner,
                "CONTENT_REVIEW",
                " / ".join(rows[item][1].gloss for item in request.motion_version_ids),
            )
    except PlaybackUnavailable as exc:
        return PrepareResult(status="NEEDS_REVIEW", reasons=[str(exc)])
    except (StorageError, OSError):
        return PrepareResult(
            status="ASSET_UNAVAILABLE",
            reasons=["A required asset is unavailable or failed integrity validation"],
        )


def prepare_exact(session, store, request, owner):
    normalized = " ".join(request.text.casefold().split())
    if not normalized:
        return PrepareResult(status="UNSUPPORTED", reasons=["An exact expression is required"])
    try:
        with session.begin():
            catalog_lock(session)
            concepts = session.scalars(
                select(SignConcept)
                .where(SignConcept.normalized_canonical_text == normalized)
                .order_by(SignConcept.id)
                .with_for_update(read=True)
            ).all()
            if not concepts:
                return PrepareResult(
                    status="UNSUPPORTED", reasons=["No exact registered expression"]
                )
            if len(concepts) != 1:
                return PrepareResult(
                    status="NEEDS_REVIEW",
                    reasons=["The expression has multiple registered meanings"],
                )
            concept = concepts[0]
            if not concept.active_motion_version_id:
                raise PlaybackUnavailable("The exact expression has no eligible active version")
            avatar_id = session.scalar(
                select(MotionVersion.id)
                .join(AvatarProfile, MotionVersion.avatar_profile_id == AvatarProfile.id)
                .where(
                    AvatarProfile.id == request.avatar_profile_id,
                    MotionVersion.sha256 == AvatarProfile.source_sha256,
                )
                .order_by(MotionVersion.id)
                .limit(1)
            )
            if avatar_id is None:
                raise PlaybackUnavailable("The canonical avatar source is unavailable")
            selected = concept.active_motion_version_id
            rows = _locked_versions(session, [selected, avatar_id])
            _check_content(rows, request.avatar_profile_id)
            if _eligible_ids(session, [selected]) != {selected}:
                raise PlaybackUnavailable(
                    "The exact expression is not eligible for the selected avatar"
                )
            _check_bytes(rows, store)
            return _persist(
                session, rows, [selected], avatar_id, owner, "EXACT_CONTENT", concept.canonical_text
            )
    except PlaybackUnavailable as exc:
        return PrepareResult(status="NEEDS_REVIEW", reasons=[str(exc)])
    except (StorageError, OSError):
        return PrepareResult(
            status="ASSET_UNAVAILABLE",
            reasons=["A required asset is unavailable or failed integrity validation"],
        )


def validate_record(session, store, record, *, verify_bytes=True, development=False):
    """Recheck unpublished selection and every required object, including the avatar."""
    if record.purpose == "PUBLISHED":
        from app.live_schema import DevelopmentLivePlan, LivePlan

        if record.payload.get("schema_version") == 5:
            if not development:
                raise PlaybackUnavailable("Development live delivery is disabled")
            plan = DevelopmentLivePlan.model_validate(record.payload)
        else:
            plan = LivePlan.model_validate(record.payload)
    elif record.purpose == "ANNOUNCEMENT_PREVIEW":
        from app.announcement_schema import AnnouncementPlan

        plan = AnnouncementPlan.model_validate(record.payload)
    else:
        plan = PlaybackPlan.model_validate(record.payload)
    if plan.valid_until <= utc_now() or semantic_hash(plan) != record.manifest_hash:
        raise PlaybackUnavailable("The plan expired or failed integrity validation")
    ids = [item.motion_version_id for item in plan.items]
    rows = _locked_versions(session, [record.avatar_motion_version_id, *ids])
    _check_content(
        rows,
        plan.avatar.profile_id,
        development=development
        and (record.purpose == "CONTENT_REVIEW" or plan.schema_version == 5),
    )
    avatar, _, profile = rows[record.avatar_motion_version_id]
    if (
        profile.rig_fingerprint != plan.avatar.rig_fingerprint
        or avatar.sha256 != plan.avatar.sha256
    ):
        raise PlaybackUnavailable("The avatar contract changed")
    if record.purpose in {"EXACT_CONTENT", "ANNOUNCEMENT_PREVIEW"}:
        if profile.source_sha256 != avatar.sha256 or _eligible_ids(session, ids) != set(ids):
            raise PlaybackUnavailable("The exact selection changed; prepare a fresh plan")
        if any(
            record.selection_revisions.get(str(concept.id)) != concept.semantic_revision
            for _, concept, _ in rows.values()
        ):
            raise PlaybackUnavailable("The selected meaning changed; prepare a fresh plan")
    if record.purpose == "ANNOUNCEMENT_PREVIEW":
        from app.announcements import validate_announcement_context

        validate_announcement_context(session, plan)
    if record.purpose == "PUBLISHED":
        from app.live import validate_published

        validate_published(session, record, plan, rows)
    for item in plan.items:
        motion = rows[item.motion_version_id][0]
        if (item.sha256, item.clip_name, item.duration_seconds) != (
            motion.sha256,
            motion.clip_name,
            motion.duration_seconds,
        ):
            raise PlaybackUnavailable("A pinned motion contract changed")
    try:
        return plan, _check_bytes(rows, store) if verify_bytes else {}
    except (StorageError, OSError) as exc:
        raise PlaybackUnavailable("The complete plan has an unavailable asset") from exc
