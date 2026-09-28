"""Phase 2 HTTP contract; review sequences never claim railway sentence approval."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ReviewRequest(WireModel):
    avatar_motion_version_id: UUID
    motion_version_ids: list[UUID] = Field(min_length=1, max_length=64)


class ExactRequest(WireModel):
    text: str = Field(min_length=1, max_length=2048)
    avatar_profile_id: UUID
    source_text_language: Literal["en"] = "en"


class AvatarReference(WireModel):
    profile_id: UUID
    rig_fingerprint: Digest
    motion_version_id: UUID
    sha256: Digest
    size_bytes: int = Field(gt=0, le=134217728)
    asset_url: str


class PlaybackItem(WireModel):
    sequence_index: int = Field(ge=0)
    semantic_key: str
    concept_id: UUID
    motion_version_id: UUID
    version_no: int = Field(gt=0)
    sha256: Digest
    size_bytes: int = Field(gt=0, le=134217728)
    asset_url: str
    clip_name: str
    duration_seconds: float = Field(gt=0)
    semantic_group: str
    transition: Literal["NONE", "REVIEW_CUT", "APPROVED_CUT"]
    playback_rate: Literal[1] = 1


class PlaybackPlan(WireModel):
    schema_version: Literal[2] = 2
    manifest_id: UUID
    manifest_hash: Digest
    purpose: Literal["CONTENT_REVIEW", "EXACT_CONTENT"]
    output_language: Literal["ISL"] = "ISL"
    operational: Literal[False] = False
    issued_at: AwareDatetime
    valid_until: AwareDatetime
    caption_text: str
    avatar: AvatarReference
    items: list[PlaybackItem] = Field(min_length=1, max_length=64)
    readiness_policy: Literal["FULL_MESSAGE"] = "FULL_MESSAGE"
    safe_boundaries: list[int]
    estimated_duration_seconds: float = Field(gt=0, le=600)


class PrepareResult(WireModel):
    status: Literal["READY", "NEEDS_REVIEW", "UNSUPPORTED", "ASSET_UNAVAILABLE"]
    reasons: list[str] = Field(default_factory=list)
    manifest: PlaybackPlan | None = None
