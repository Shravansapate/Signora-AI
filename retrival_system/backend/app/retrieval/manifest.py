"""Build immutable, version-pinned playback manifests.

This module deliberately has no database or transport dependency. Callers provide
already-resolved candidates; this boundary verifies the data needed for a complete
playback plan and gives the plan a stable semantic hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ManifestBuildError(ValueError):
    """The selected content cannot produce a complete playback manifest."""


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MotionSelection(ManifestModel):
    """One eligible, immutable motion selected by retrieval."""

    concept_id: str = Field(min_length=1, max_length=200)
    motion_version_id: str = Field(min_length=1, max_length=200)
    asset_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=SHA256_PATTERN)
    clip_name: str = Field(min_length=1, max_length=200)
    duration_seconds: float = Field(gt=0)
    semantic_group: str = Field(min_length=1, max_length=200)
    transition_policy_id: str = Field(min_length=1, max_length=200)
    eligible: bool = True
    rejection_reason: str | None = None


class ManifestItem(ManifestModel):
    sequence_index: int = Field(ge=0)
    concept_id: str
    motion_version_id: str
    asset_key: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    clip_name: str
    duration_seconds: float = Field(gt=0)
    semantic_group: str
    transition_policy_id: str


class PlaybackManifest(ManifestModel):
    schema_version: int = Field(default=1, ge=1)
    manifest_id: str = Field(min_length=1, max_length=200)
    manifest_revision: int = Field(ge=1)
    manifest_hash: str = Field(pattern=SHA256_PATTERN)
    message_id: str = Field(min_length=1, max_length=200)
    message_revision: int = Field(ge=1)
    station_id: str = Field(min_length=1, max_length=200)
    display_group: str = Field(min_length=1, max_length=200)
    template_id: str = Field(min_length=1, max_length=200)
    template_version: int = Field(ge=1)
    avatar_profile_id: str = Field(min_length=1, max_length=200)
    avatar_version: str = Field(min_length=1, max_length=200)
    intent: str = Field(min_length=1, max_length=200)
    slots: dict[str, Any]
    caption_text: str = Field(min_length=1, max_length=4096)
    issued_at: datetime
    valid_until: datetime
    items: list[ManifestItem] = Field(min_length=1)


def _canonical_payload(manifest: PlaybackManifest) -> bytes:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compile_manifest(
    *,
    manifest_id: str,
    manifest_revision: int,
    message_id: str,
    message_revision: int,
    station_id: str,
    display_group: str,
    template_id: str,
    template_version: int,
    avatar_profile_id: str,
    avatar_version: str,
    intent: str,
    slots: dict[str, Any],
    caption_text: str,
    issued_at: datetime,
    valid_until: datetime,
    selections: list[MotionSelection],
) -> PlaybackManifest:
    """Compile an ordered complete plan from already-resolved motion selections."""
    if not selections:
        raise ManifestBuildError("A playback manifest needs at least one motion.")
    if issued_at.tzinfo is None or valid_until.tzinfo is None:
        raise ManifestBuildError("Manifest timestamps must include a timezone.")
    if valid_until <= issued_at:
        raise ManifestBuildError("Manifest validity must end after it is issued.")

    rejected = [selection for selection in selections if not selection.eligible]
    if rejected:
        reasons = ", ".join(
            f"{selection.concept_id}: {selection.rejection_reason or 'not eligible'}"
            for selection in rejected
        )
        raise ManifestBuildError(f"Manifest contains ineligible motions: {reasons}")

    items = [
        ManifestItem(
            sequence_index=index,
            concept_id=selection.concept_id,
            motion_version_id=selection.motion_version_id,
            asset_key=selection.asset_key,
            sha256=selection.sha256,
            clip_name=selection.clip_name,
            duration_seconds=selection.duration_seconds,
            semantic_group=selection.semantic_group,
            transition_policy_id=selection.transition_policy_id,
        )
        for index, selection in enumerate(selections)
    ]
    manifest = PlaybackManifest(
        manifest_hash="0" * 64,
        manifest_id=manifest_id,
        manifest_revision=manifest_revision,
        message_id=message_id,
        message_revision=message_revision,
        station_id=station_id,
        display_group=display_group,
        template_id=template_id,
        template_version=template_version,
        avatar_profile_id=avatar_profile_id,
        avatar_version=avatar_version,
        intent=intent,
        slots=slots,
        caption_text=caption_text,
        issued_at=issued_at,
        valid_until=valid_until,
        items=items,
    )
    digest = hashlib.sha256(_canonical_payload(manifest)).hexdigest()
    return manifest.model_copy(update={"manifest_hash": digest})
