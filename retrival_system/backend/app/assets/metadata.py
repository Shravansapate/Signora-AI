"""Read the supplied converter metadata as evidence, never as executable instructions.

Only fields needed by retrieval ingestion are projected. Other converter fields remain
in the original file, identified by its SHA-256; they are neither discarded from the
source nor promoted to runtime approval. In particular, source ``exists`` flags and
absolute converter paths are not trusted as local filesystem authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=2048)]
MAX_METADATA_BYTES = 32 * 1024 * 1024


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class MotionIdentity(SourceModel):
    motion_code: Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Z0-9_]+$")]
    gloss: Text
    canonical_text: Text
    language_code: Literal["ISL"]
    domain: Text | None = None
    level: Literal["WORD", "PHRASE", "SENTENCE", "NUMBER", "FINGERSPELLING", "LETTER"]
    variant_no: Annotated[int, Field(ge=1)]


class FinalAsset(SourceModel):
    relative_path: Text
    sha256: SHA256
    size_bytes: Annotated[int, Field(gt=0)]


class Assets(SourceModel):
    final_glb: FinalAsset


class Integrity(SourceModel):
    glb_sha256: SHA256
    glb_size_bytes: Annotated[int, Field(gt=0)]


class AnimationMetadata(SourceModel):
    animation_name: Text
    animation_count: Annotated[int, Field(ge=1)]
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Review(SourceModel):
    isl_verified: bool = False
    signer_verdict: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None


class Linguistic(SourceModel):
    meaning: str | None = None
    context: str | None = None
    non_manual_features_available: bool | None = None


class RetrievalMetadata(SourceModel):
    aliases: Annotated[list[Text], Field(max_length=256)] = Field(default_factory=list)


class SourceMetadata(SourceModel):
    metadata_schema_version: Literal["3.1"]
    motion_identity: MotionIdentity
    assets: Assets
    file_integrity: Integrity
    animation: AnimationMetadata
    linguistic: Linguistic = Field(default_factory=Linguistic)
    retrieval: RetrievalMetadata = Field(default_factory=RetrievalMetadata)
    isl_validation: Review = Field(default_factory=Review)
    technical_qc: str | None = None
    production_eligible: bool = False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_number(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def load_metadata(path: Path) -> tuple[SourceMetadata, str, dict]:
    with path.open("rb") as source:
        data = source.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("Metadata exceeds the 32 MiB limit")
    raw = json.loads(
        data.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_invalid_number
    )
    return SourceMetadata.model_validate(raw), hashlib.sha256(data).hexdigest(), raw


def resolve_asset(metadata: SourceMetadata, glb_root: Path) -> Path:
    """Use a source basename only; never follow converter paths outside this library."""
    portable = metadata.assets.final_glb.relative_path.replace("\\", "/")
    components = portable.split("/")
    if any(part in ("", ".", "..") for part in components) or ":" in portable:
        raise ValueError("Invalid source relative asset path")
    name = PurePosixPath(portable).name
    if not re.fullmatch(r"[^<>:\"/\\|?*\x00-\x1f]+\.glb", name, flags=re.IGNORECASE):
        raise ValueError("Expected a safe GLB basename")
    root = glb_root.resolve(strict=True)
    target = (root / name).resolve(strict=True)
    if target.parent != root or not target.is_file():
        raise ValueError("Asset is not a regular file inside the configured GLB directory")
    return target


def metadata_findings(metadata: SourceMetadata, raw: dict, sha256: str, size: int) -> list[dict]:
    """Report evidence conflicts separately from byte validation; do not rewrite them."""
    findings = []

    def add(code: str, field: str, severity: str = "error") -> None:
        findings.append({"code": code, "field": field, "severity": severity})

    for field, claimed in (
        ("file_integrity.glb_sha256", metadata.file_integrity.glb_sha256),
        ("assets.final_glb.sha256", metadata.assets.final_glb.sha256),
        (
            "signer_review.hash_binding.glb_sha256",
            raw.get("signer_review", {}).get("hash_binding", {}).get("glb_sha256"),
        ),
    ):
        if claimed is not None and claimed != sha256:
            add("ASSET_HASH_MISMATCH", field)
    for field, claimed_size in (
        ("file_integrity.glb_size_bytes", metadata.file_integrity.glb_size_bytes),
        ("assets.final_glb.size_bytes", metadata.assets.final_glb.size_bytes),
    ):
        if claimed_size != size:
            add("ASSET_SIZE_MISMATCH", field)
    review = metadata.isl_validation
    if not (
        review.isl_verified
        and review.signer_verdict == "PASS"
        and review.reviewer
        and review.reviewed_at
    ):
        add("SOURCE_REVIEW_PENDING", "isl_validation", "pending")
    if not metadata.linguistic.meaning or not metadata.linguistic.context:
        add("SOURCE_MEANING_CONTEXT_MISSING", "linguistic", "pending")
    if metadata.motion_identity.domain is None:
        add("SOURCE_DOMAIN_MISSING", "motion_identity.domain", "pending")
    if metadata.technical_qc != "PASS":
        add("SOURCE_TECHNICAL_QC_NOT_PASS", "technical_qc", "pending")
    return findings
