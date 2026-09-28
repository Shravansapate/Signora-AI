"""Registry records. Eligibility is a constrained database view, not an API flag."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AvatarProfile(Base):
    __tablename__ = "avatar_profiles"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rig_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    fingerprint_version: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class SignConcept(Base):
    __tablename__ = "sign_concepts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    semantic_key: Mapped[str] = mapped_column(String(160), unique=True)
    gloss: Mapped[str] = mapped_column(Text)
    canonical_text: Mapped[str] = mapped_column(Text)
    normalized_canonical_text: Mapped[str] = mapped_column(Text)
    language_code: Mapped[str] = mapped_column(String(8), default="ISL")
    domain: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(24))
    meaning: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    meaning_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    enabled: Mapped[bool] = mapped_column(default=False)
    semantic_revision: Mapped[int] = mapped_column(default=1)
    revision: Mapped[int] = mapped_column(default=1)
    active_motion_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class MotionVersion(Base):
    __tablename__ = "motion_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    concept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sign_concepts.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    clip_name: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[float]
    avatar_profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("avatar_profiles.id"))
    technical_qc_status: Mapped[str] = mapped_column(String(16), default="PASSED")
    linguistic_review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    composition_review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reviewed_sha256: Mapped[str | None] = mapped_column(String(64))
    reviewed_avatar_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_semantic_revision: Mapped[int | None] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), default="STAGING")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_sha256: Mapped[str] = mapped_column(String(64))
    source_metadata: Mapped[dict] = mapped_column(JSONB)
    technical_report: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SignAlias(Base):
    __tablename__ = "sign_aliases"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    concept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sign_concepts.id"))
    source_language: Mapped[str | None] = mapped_column(String(16))
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    domain: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    reviewed_semantic_revision: Mapped[int | None] = mapped_column(Integer)
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("content_reviews.id"))


class RetrievalProfile(Base):
    __tablename__ = "retrieval_profiles"
    concept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sign_concepts.id"), primary_key=True)
    semantic_revision: Mapped[int] = mapped_column(Integer)
    domain: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text)
    sense: Mapped[dict] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_reviews.id"))
    status: Mapped[str] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegistryEvent(Base):
    __tablename__ = "registry_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlaybackRecord(Base):
    __tablename__ = "playback_manifests"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_subject: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    avatar_motion_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motion_versions.id"))
    manifest_hash: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    selection_revisions: Mapped[dict] = mapped_column(JSONB)


class PlaybackItemRecord(Base):
    __tablename__ = "playback_manifest_items"
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playback_manifests.id"), primary_key=True
    )
    sequence_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    motion_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motion_versions.id"))


class ContentReview(Base):
    __tablename__ = "content_reviews"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    decision: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    source_alias: Mapped[str] = mapped_column(Text)
    source_root_hash: Mapped[str] = mapped_column(Text)
    owner_subject: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportItem(Base):
    __tablename__ = "import_items"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_jobs.id"))
    metadata_name: Mapped[str] = mapped_column(Text)
    metadata_sha256: Mapped[str | None] = mapped_column(Text)
    semantic_key: Mapped[str | None] = mapped_column(Text)
    asset_sha256: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="DISCOVERED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    attempt_limit: Mapped[int] = mapped_column(Integer, default=3)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(default=False)
    motion_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("motion_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CleanupJob(Base):
    __tablename__ = "asset_cleanup_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    motion_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("motion_versions.id"), unique=True
    )
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    development_reset: Mapped[bool] = mapped_column(default=False)
    state: Mapped[str] = mapped_column(Text, default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    attempt_limit: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LibrarySelection(Base):
    """Explicit development selection; never grants production approval."""

    __tablename__ = "library_selections"
    concept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sign_concepts.id"), primary_key=True)
    motion_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    enabled: Mapped[bool] = mapped_column(default=False)


class MotionMetadataRevision(Base):
    __tablename__ = "motion_metadata_revisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    motion_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motion_versions.id"))
    metadata_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    definition: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer)


class AnnouncementTemplate(Base):
    __tablename__ = "announcement_templates"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    template_key: Mapped[str] = mapped_column(Text)
    version_no: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    definition_hash: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("content_reviews.id"))
    bindings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TemplateDependency(Base):
    __tablename__ = "template_dependencies"
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("announcement_templates.id"), primary_key=True
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sign_concepts.id"), primary_key=True)


class TemplateMotionBinding(Base):
    __tablename__ = "template_motion_bindings"
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_reviews.id"), primary_key=True)
    template_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("announcement_templates.id"))
    motion_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("motion_versions.id"), primary_key=True
    )


class VoiceTranscript(Base):
    __tablename__ = "voice_transcripts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_subject: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    audio_sha256: Mapped[str] = mapped_column(Text)
    asr_metadata: Mapped[dict] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnnouncementInputRecord(Base):
    __tablename__ = "announcement_inputs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    owner_subject: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    input_type: Mapped[str] = mapped_column(Text)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_transcripts.id"))
    response: Mapped[dict] = mapped_column(JSONB)
    manifest_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("playback_manifests.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnnouncementManifestRef(Base):
    __tablename__ = "announcement_manifest_refs"
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playback_manifests.id"), primary_key=True
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("announcement_templates.id"))
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_reviews.id"))
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    input_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("announcement_inputs.id"))


class TemplatePreview(Base):
    __tablename__ = "template_previews"
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playback_manifests.id"), primary_key=True
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("announcement_templates.id"))
    definition_hash: Mapped[str] = mapped_column(Text)
    meaning: Mapped[dict] = mapped_column(JSONB)
    bindings: Mapped[dict] = mapped_column(JSONB)
