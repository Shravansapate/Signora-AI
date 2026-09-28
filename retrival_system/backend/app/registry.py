"""Stage exact asset versions transactionally; registration never activates content."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.assets.glb import inspect_glb
from app.assets.khronos import validate_glb
from app.assets.metadata import load_metadata, metadata_findings
from app.catalog import catalog_lock
from app.library import current_metadata, select_library
from app.models import (
    AuditLog,
    AvatarProfile,
    LibrarySelection,
    MotionMetadataRevision,
    MotionVersion,
    RegistryEvent,
    SignAlias,
    SignConcept,
)
from app.storage import MAX_OBJECT_BYTES, AssetStore, StorageError


class RegistryConflict(ValueError):
    pass


def eligible_motions(
    session: Session, semantic_keys: list[str], avatar_profile_id: uuid.UUID, store: AssetStore
) -> dict[str, dict]:
    """Central new-plan selection: database eligibility AND current object integrity.

    Returning fewer entries is an unavailable-content result; the future compiler
    must require complete coverage. This function never substitutes archived bytes.
    """
    if not semantic_keys:
        return {}
    if len(semantic_keys) > 128:
        raise ValueError("A registry lookup is limited to 128 concepts")
    query = text("""
      SELECT c.semantic_key, m.id, m.concept_id, m.storage_key, m.sha256, m.clip_name,
             m.duration_seconds, m.avatar_profile_id
      FROM eligible_motion_versions m JOIN sign_concepts c ON c.id=m.concept_id
      WHERE c.semantic_key IN :keys AND m.avatar_profile_id=:avatar
    """).bindparams(bindparam("keys", expanding=True))
    result = {}
    for row in session.execute(
        query, {"keys": semantic_keys, "avatar": avatar_profile_id}
    ).mappings():
        try:
            store.verify(row["storage_key"], row["sha256"])
        except StorageError:
            continue
        result[row["semantic_key"]] = dict(row)
    return result


def stage_motion(
    metadata_path: Path,
    motion_path: Path,
    actor: str,
    session: Session,
    store: AssetStore,
    *,
    expected_concept_id: uuid.UUID | None = None,
    expected_metadata_sha256: str | None = None,
    manage_library: bool = False,
    new_concept_only: bool = False,
    allow_reupload_deleted: bool = False,
) -> dict:
    metadata, metadata_hash, raw = load_metadata(metadata_path)
    if expected_metadata_sha256 is not None and metadata_hash != expected_metadata_sha256:
        raise RegistryConflict("Metadata changed after discovery")
    if motion_path.stat().st_size > MAX_OBJECT_BYTES:
        raise ValueError("Asset exceeds the supported size limit")
    with motion_path.open("rb") as source:
        supplied_hash = hashlib.file_digest(source, "sha256").hexdigest()
    if supplied_hash != metadata.file_integrity.glb_sha256:
        raise ValueError("Source asset checksum changed")
    # Known immutable bytes can reuse their completed technical validation, while
    # both the current input bytes and durable stored object are still checked.
    with session.begin():
        catalog_lock(session)
        if new_concept_only and session.scalar(
            select(SignConcept.id).where(
                SignConcept.semantic_key == metadata.motion_identity.motion_code,
                (
                    select(MotionVersion.id)
                    .where(
                        MotionVersion.concept_id == SignConcept.id,
                        MotionVersion.deleted_at.is_(None),
                    )
                    .exists()
                    if allow_reupload_deleted
                    else True
                ),
            )
        ):
            raise RegistryConflict("This concept already exists; select it and add a GLB version")
        known = session.scalar(
            select(MotionVersion)
            .join(SignConcept)
            .where(
                SignConcept.semantic_key == metadata.motion_identity.motion_code,
                MotionVersion.sha256 == supplied_hash,
                MotionVersion.deleted_at.is_(None) if allow_reupload_deleted else True,
            )
            .order_by(MotionVersion.deleted_at.asc().nullsfirst(), MotionVersion.version_no.desc())
        )
        if known is not None:
            if expected_concept_id is not None and known.concept_id != expected_concept_id:
                raise RegistryConflict("The upload belongs to a different concept")
            if known.deleted_at is not None:
                raise RegistryConflict(
                    "This exact version was deleted; retained history cannot be overwritten"
                )
            if known.metadata_sha256 != metadata_hash and current_metadata(session, known) != raw:
                raise RegistryConflict(
                    "Metadata changed for existing bytes; explicit review required"
                )
            store.verify(known.storage_key, known.sha256)
            return {
                "status": "UNCHANGED",
                "concept_id": str(known.concept_id),
                "motion_version_id": str(known.id),
                "version_no": known.version_no,
            }
    technical = inspect_glb(motion_path)
    official = validate_glb(motion_path)
    findings = metadata_findings(metadata, raw, technical["sha256"], technical["size_bytes"])
    if official["sha256"] != technical["sha256"] or official["errors"] or official["truncated"]:
        raise ValueError("Asset structural validation did not pass")
    if any(f["severity"] == "error" for f in findings):
        raise ValueError("Source metadata does not match the uploaded asset")
    clips = technical["clips"]
    selected = [clip for clip in clips if clip["name"] == metadata.animation.animation_name]
    if not selected and len(clips) == 1:
        selected = clips
    if len(selected) != 1:
        raise ValueError("Selected animation is ambiguous or absent")
    with motion_path.open("rb") as source:
        key = store.put(source, technical["sha256"])
    identity = metadata.motion_identity
    # Inspection and durable storage finish before taking the catalog lock.
    with session.begin():
        catalog_lock(session, write=True)
        if new_concept_only and session.scalar(
            select(SignConcept.id).where(
                SignConcept.semantic_key == identity.motion_code,
                (
                    select(MotionVersion.id)
                    .where(
                        MotionVersion.concept_id == SignConcept.id,
                        MotionVersion.deleted_at.is_(None),
                    )
                    .exists()
                    if allow_reupload_deleted
                    else True
                ),
            )
        ):
            raise RegistryConflict("This concept already exists; select it and add a GLB version")
        # A cleanup worker cannot remove an object between this check and registration.
        store.verify(key, technical["sha256"])
        profile_id = uuid.uuid4()
        session.execute(
            insert(AvatarProfile)
            .values(
                id=profile_id,
                rig_fingerprint=technical["rig_fingerprint"],
                fingerprint_version=technical["rig_fingerprint_version"],
                source_sha256=technical["sha256"],
                status="PENDING",
            )
            .on_conflict_do_nothing(index_elements=["rig_fingerprint"])
        )
        profile = session.scalar(
            select(AvatarProfile).where(
                AvatarProfile.rig_fingerprint == technical["rig_fingerprint"]
            )
        )
        session.execute(
            insert(SignConcept)
            .values(
                id=uuid.uuid4(),
                semantic_key=identity.motion_code,
                gloss=identity.gloss,
                canonical_text=identity.canonical_text,
                normalized_canonical_text=" ".join(identity.canonical_text.casefold().split()),
                language_code=identity.language_code,
                domain=identity.domain,
                level=identity.level,
                meaning=metadata.linguistic.meaning,
                context=metadata.linguistic.context,
                meaning_status="PENDING",
                enabled=False,
                semantic_revision=1,
                revision=1,
            )
            .on_conflict_do_nothing(index_elements=["semantic_key"])
        )
        concept = session.scalar(
            select(SignConcept)
            .where(SignConcept.semantic_key == identity.motion_code)
            .with_for_update()
        )
        if expected_concept_id is not None and concept.id != expected_concept_id:
            raise RegistryConflict("The upload belongs to a different concept")
        previous = session.scalar(
            select(MotionVersion)
            .where(MotionVersion.concept_id == concept.id)
            .order_by(MotionVersion.version_no)
            .limit(1)
        )
        latest_metadata = session.scalar(
            select(MotionMetadataRevision)
            .join(MotionVersion)
            .where(MotionVersion.concept_id == concept.id)
            .order_by(MotionMetadataRevision.created_at.desc(), MotionMetadataRevision.id.desc())
            .limit(1)
        )
        baseline = (
            latest_metadata.payload
            if latest_metadata
            else previous.source_metadata
            if previous
            else raw
        )
        # Source meaning is compared with its registered source record. Runtime
        # meaning annotations never rewrite that record or break idempotent imports.
        for field, supplied in {
            "gloss": identity.gloss,
            "canonical_text": identity.canonical_text,
            "language_code": identity.language_code,
            "level": identity.level,
        }.items():
            if getattr(concept, field) != supplied:
                raise RegistryConflict(
                    "Semantic metadata changed; explicit concept review required"
                )
        source_changed = bool(baseline) and (
            any(
                baseline.get("linguistic", {}).get(field) != raw.get("linguistic", {}).get(field)
                for field in ("meaning", "context")
            )
            or (baseline["motion_identity"].get("domain") != identity.domain)
            or baseline.get("retrieval", {}).get("aliases", [])
            != raw.get("retrieval", {}).get("aliases", [])
        )
        if not baseline:
            # The old payload may have been deliberately purged. The retained
            # concept and aliases still define identity for a fresh GLB version.
            source_changed = any(
                getattr(concept, field) != value
                for field, value in {
                    "domain": identity.domain,
                    "meaning": metadata.linguistic.meaning,
                    "context": metadata.linguistic.context,
                }.items()
            ) or set(
                session.scalars(
                    select(SignAlias.normalized_alias).where(SignAlias.concept_id == concept.id)
                )
            ) != {" ".join(a.casefold().split()) for a in metadata.retrieval.aliases}
        if source_changed:
            raise RegistryConflict(
                "Source semantics changed; use the explicit meaning review workflow"
            )
        existing = session.scalar(
            select(MotionVersion)
            .where(
                MotionVersion.concept_id == concept.id,
                MotionVersion.sha256 == technical["sha256"],
                MotionVersion.deleted_at.is_(None) if allow_reupload_deleted else True,
            )
            .order_by(MotionVersion.deleted_at.asc().nullsfirst(), MotionVersion.version_no.desc())
        )
        if existing:
            if existing.deleted_at is not None:
                raise RegistryConflict("The exact version has been deleted")
            if existing.metadata_sha256 != metadata_hash:
                raise RegistryConflict(
                    "Metadata changed for existing bytes; explicit review required"
                )
            store.verify(existing.storage_key, existing.sha256)
            return {
                "status": "UNCHANGED",
                "concept_id": str(concept.id),
                "motion_version_id": str(existing.id),
                "version_no": existing.version_no,
            }
        version_no = session.scalar(
            text("SELECT coalesce(max(version_no),0)+1 FROM motion_versions WHERE concept_id=:id"),
            {"id": concept.id},
        )
        if manage_library and previous and session.get(LibrarySelection, concept.id) is None:
            # Uploading a replacement must not silently switch development playback.
            retained = concept.active_motion_version_id or session.scalar(
                select(MotionVersion.id)
                .where(
                    MotionVersion.concept_id == concept.id,
                    MotionVersion.deleted_at.is_(None),
                    MotionVersion.revoked_at.is_(None),
                    MotionVersion.lifecycle_status.in_(["STAGING", "ACTIVE"]),
                )
                .order_by(MotionVersion.version_no)
                .limit(1)
            )
            select_library(session, concept, retained, retained is not None)
        version = MotionVersion(
            concept_id=concept.id,
            version_no=version_no,
            storage_key=key,
            sha256=technical["sha256"],
            size_bytes=technical["size_bytes"],
            clip_name=selected[0]["name"],
            duration_seconds=selected[0]["duration_seconds"],
            avatar_profile_id=profile.id,
            metadata_sha256=metadata_hash,
            source_metadata=raw,
            technical_report={"inspection": technical, "khronos": official, "findings": findings},
        )
        session.add(version)
        if manage_library and previous is None:
            select_library(session, concept, None, False)
        if version_no > 1:
            concept.revision += 1
        session.flush()
        aliases = raw.get("retrieval", {}).get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 256:
            raise ValueError("Invalid source alias list")
        for alias in aliases if version_no == 1 else []:
            if not isinstance(alias, str) or not 0 < len(alias) <= 2048:
                raise ValueError("Invalid source alias")
            normalized = " ".join(alias.casefold().split())
            if not normalized:
                raise ValueError("Empty source alias")
            session.execute(
                insert(SignAlias)
                .values(
                    id=uuid.uuid4(),
                    concept_id=concept.id,
                    source_language=None,
                    alias=alias,
                    normalized_alias=normalized,
                    review_status="PENDING",
                )
                .on_conflict_do_nothing(index_elements=["concept_id", "normalized_alias"])
            )
        details = {
            "concept_id": str(concept.id),
            "motion_version_id": str(version.id),
            "sha256": version.sha256,
            "version_no": version.version_no,
        }
        session.add(
            AuditLog(actor=actor, action="MOTION_STAGED", entity_id=version.id, details=details)
        )
        session.add(
            RegistryEvent(event_type="MOTION_STAGED", entity_id=version.id, payload=details)
        )
        return {"status": "STAGED", **details}
