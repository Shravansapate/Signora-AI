"""Reviewed sense constraints, never inferred from an embedding score."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.lifecycle_schema import ReviewText
from app.retrieval.playback_schema import WireModel

Scope = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class Sense(WireModel):
    semantic_class: Scope
    polarity: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
    temporal_state: Scope
    # Literal operational fields include names, roles, units and identifiers as exact strings.
    literals: dict[Scope, Annotated[str, StringConstraints(min_length=1, max_length=256)]] = Field(
        default_factory=dict, max_length=16
    )


class ProfileReview(WireModel):
    expected_revision: int = Field(ge=1)
    domain: Scope
    context: Scope
    sense: Sense
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)
    ]
    decision: Literal["APPROVED", "REJECTED"]
    evidence: ReviewText
    reason: ReviewText


class AliasReview(WireModel):
    expected_revision: int = Field(ge=1)
    alias: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    source_language: Literal["en"] = "en"
    domain: Scope
    context: Scope
    decision: Literal["APPROVED", "REJECTED"]
    evidence: ReviewText
    reason: ReviewText


class SearchRequest(WireModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    source_language: Literal["en"] = "en"
    domain: Scope
    context: Scope
    avatar_profile_id: UUID
    levels: list[Literal["WORD", "PHRASE", "SENTENCE", "LETTER", "NUMBER", "FINGERSPELLING"]] = (
        Field(default_factory=lambda: ["SENTENCE", "PHRASE", "WORD"], min_length=1, max_length=6)
    )
    sense: Sense | None = None
    semantic: bool = True


class IndexRequest(WireModel):
    concept_ids: list[UUID] = Field(min_length=1, max_length=32)
