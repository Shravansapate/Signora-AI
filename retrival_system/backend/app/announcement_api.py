"""Thin authorized input and construction API; publication belongs to durable delivery."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select

from app.announcement_schema import (
    AnnouncementInput,
    StationId,
    StationWrite,
    TemplateActivation,
    TemplateCreate,
    TemplatePreviewRequest,
    TemplateReview,
    TranslateResult,
)
from app.announcements import translate, write_station
from app.audio import MAX_AUDIO_BYTES, inspect_audio
from app.config import Principal
from app.lifecycle import _event, require
from app.models import AnnouncementTemplate, Station, VoiceTranscript
from app.templates import (
    activate_template,
    create_template,
    preview_template,
    review_template,
    template_result,
)


def announcement_router(sessions, store, principal, asr, *, demo_mode_enabled=False):
    router = APIRouter(prefix="/api/v1")
    Identity = Annotated[Principal, Depends(principal)]

    def operator(identity: Identity):
        require(
            bool(identity.roles & {"admin", "reviewer", "operator"}),
            "ROLE_REQUIRED",
            "Operator or reviewer role required",
            403,
        )
        return identity

    def admin(identity: Identity):
        require("admin" in identity.roles, "ROLE_REQUIRED", "Administrator role required", 403)
        return identity

    def reviewer(identity: Identity):
        require(
            bool(identity.roles & {"admin", "reviewer"}),
            "ROLE_REQUIRED",
            "Reviewer role required",
            403,
        )
        return identity

    Operator = Annotated[Principal, Depends(operator)]
    Admin = Annotated[Principal, Depends(admin)]
    Reviewer = Annotated[Principal, Depends(reviewer)]

    @router.get("/input/capabilities")
    def capabilities(identity: Operator):
        with sessions() as session:
            stations = session.scalars(select(Station).order_by(Station.id))
            return {
                "source_text_languages": ["en"],
                "roles": sorted(identity.roles),
                "asr_state": asr.state,
                "audio_format": "PCM WAV, 16000 Hz, mono, 16-bit, 0.25-60 seconds",
                "demo_mode_enabled": demo_mode_enabled,
                "stations": [
                    {"id": row.id, "revision": row.revision, "definition": row.definition}
                    for row in stations
                    if identity.roles & {"admin", "reviewer"} or row.id in identity.station_ids
                ],
            }

    @router.post("/admin/stations/{station_id}")
    def station_write(station_id: StationId, request: StationWrite, identity: Admin):
        with sessions() as session:
            return write_station(session, station_id, request, identity.subject)

    @router.post("/admin/templates", status_code=201)
    def template_create(request: TemplateCreate, identity: Admin):
        with sessions() as session:
            return create_template(session, request, identity.subject)

    @router.get("/review/templates")
    def template_list(identity: Reviewer, limit: int = 50, offset: int = 0):
        require(1 <= limit <= 100 and offset >= 0, "PAGINATION", "Invalid pagination", 422)
        with sessions() as session:
            rows = session.scalars(
                select(AnnouncementTemplate)
                .order_by(AnnouncementTemplate.template_key, AnnouncementTemplate.version_no.desc())
                .limit(limit)
                .offset(offset)
            )
            return {"items": [template_result(row) for row in rows]}

    @router.post("/review/templates/{template_id}")
    def template_review(template_id: UUID, request: TemplateReview, identity: Reviewer):
        with sessions() as session:
            return review_template(session, store, template_id, request, identity.subject)

    @router.post("/review/templates/{template_id}/prepare")
    def template_preview(template_id: UUID, request: TemplatePreviewRequest, identity: Reviewer):
        with sessions() as session:
            return preview_template(session, store, template_id, request, identity.subject)

    @router.post("/admin/templates/{template_id}/activation")
    def template_activate(template_id: UUID, request: TemplateActivation, identity: Admin):
        with sessions() as session:
            return activate_template(session, store, template_id, request, identity.subject)

    @router.post("/translate", response_model=TranslateResult)
    def prepare_input(request: AnnouncementInput, identity: Operator):
        require(
            not request.demo_mode or demo_mode_enabled,
            "DEMO_MODE_DISABLED",
            "Development demo mode is disabled on this server",
            403,
        )
        with sessions() as session:
            return translate(session, store, request, identity, demo_mode_enabled=demo_mode_enabled)

    @router.post("/voice/transcribe", status_code=201)
    def transcribe_audio(identity: Operator, audio: Annotated[UploadFile, File()]):
        try:
            with store.stage(audio.file, MAX_AUDIO_BYTES) as path:
                inspected = inspect_audio(path)
                result = asr.transcribe(path)
        except ValueError as exc:
            # Lifecycle errors retain their actionable status/code via the app handler.
            from app.lifecycle import LifecycleError

            if isinstance(exc, LifecycleError):
                raise
            raise HTTPException(422, "Invalid PCM WAV recording") from exc
        with sessions() as session, session.begin():
            row = VoiceTranscript(
                owner_subject=identity.subject,
                text=result["text"],
                audio_sha256=inspected["audio_sha256"],
                asr_metadata={
                    **result["metadata"],
                    "duration_seconds": inspected["duration_seconds"],
                },
                valid_until=datetime.now(UTC) + timedelta(minutes=15),
            )
            session.add(row)
            session.flush()
            _event(
                session,
                identity.subject,
                "VOICE_TRANSCRIBED",
                row.id,
                {
                    "audio_sha256": row.audio_sha256,
                    "model_revision": row.asr_metadata["model_revision"],
                },
            )
            return {
                "transcript_id": row.id,
                "text": row.text,
                "metadata": row.asr_metadata,
                "valid_until": row.valid_until,
                "status": "NEEDS_CONFIRMATION",
            }

    return router
