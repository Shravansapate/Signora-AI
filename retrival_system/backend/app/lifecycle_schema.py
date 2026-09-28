from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.retrieval.playback_schema import Digest, WireModel

ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class RevisionRequest(WireModel):
    expected_revision: int = Field(ge=1)
    reason: ReviewText


class SwitchRequest(RevisionRequest):
    motion_version_id: UUID
    expected_active_motion_version_id: UUID | None


class MeaningReview(RevisionRequest):
    meaning: ReviewText
    context: ReviewText
    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    decision: Literal["APPROVED", "REJECTED"]
    evidence: ReviewText


class AvatarReview(RevisionRequest):
    sha256: Digest
    rig_fingerprint: Digest
    decision: Literal["APPROVED", "RETIRED"]
    evidence: ReviewText


class MotionReview(RevisionRequest):
    sha256: Digest
    avatar_profile_id: UUID
    semantic_revision: int = Field(ge=1)
    linguistic: Literal["APPROVED", "REJECTED"]
    composition: Literal["APPROVED", "REJECTED", "PENDING"]
    composition_scope: Literal["EXACT_CONTENT"]
    preview_manifest_id: UUID
    rendered_review_confirmed: Literal[True]
    evidence: ReviewText


class DeleteRequest(RevisionRequest):
    confirm_sha256: Digest
    purge_metadata: bool = False
    development_reset: bool = False


class CleanupRetryRequest(WireModel):
    expected_attempts: int = Field(ge=3)
    reason: ReviewText
