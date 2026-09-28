"""One deterministic intake/compiler for keyboard, structured forms and confirmed ASR."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text

from app.announcement_schema import (
    AnnouncementPlan,
    Issue,
    Meaning,
    StationDefinition,
    TemplateContext,
    TemplateDefinition,
    TranslateResult,
)
from app.catalog import catalog_lock
from app.demo import prepare_demo
from app.gloss import construct_gloss, recover_lexical, translation_trace
from app.lifecycle import LifecycleError, _event, require
from app.meaning import meaning_hash, parse_meaning, structured_caption, validate_signature
from app.models import (
    AnnouncementInputRecord,
    AnnouncementManifestRef,
    AnnouncementTemplate,
    AvatarProfile,
    MotionVersion,
    PlaybackRecord,
    Station,
    VoiceTranscript,
)
from app.playback import (
    PlaybackUnavailable,
    _check_bytes,
    _check_content,
    _locked_versions,
    _persist,
    validate_record,
)
from app.storage import StorageError
from app.templates import definition_hash, expand_recipe, selected_dependencies


def check_station_scope(identity, station_id):
    require(
        bool(identity.roles & {"admin", "reviewer", "operator"}),
        "ROLE_REQUIRED",
        "An operator or content reviewer role is required",
        403,
    )
    require(
        bool(identity.roles & {"admin", "reviewer"}) or station_id in identity.station_ids,
        "STATION_SCOPE",
        "This principal is not authorized for the station",
        403,
    )


def write_station(session, station_id, request, actor):
    with session.begin():
        catalog_lock(session, write=True)
        catalog_lock(session, write=True)
        row = session.get(Station, station_id, with_for_update=True)
        require(
            (row.revision if row else 0) == request.expected_revision,
            "STALE_REVISION",
            "Refresh the station configuration revision",
        )
        if row is None:
            row = Station(id=station_id, revision=0, definition={})
            session.add(row)
        row.definition = request.definition.model_dump(mode="json")
        row.revision += 1
        _event(
            session,
            actor,
            "STATION_CONFIGURED",
            uuid5(NAMESPACE_URL, "signora/station/" + station_id),
            {"station_id": station_id, "revision": row.revision, "reason": request.reason},
        )
        return {"id": row.id, "revision": row.revision, "definition": row.definition}


def validate_announcement_context(session, plan):
    context = plan.announcement
    template = session.get(AnnouncementTemplate, context.template_version_id)
    station = session.get(Station, context.station_id)
    if (
        template is None
        or not template.enabled
        or template.status != "APPROVED"
        or template.revision != context.template_revision
        or template.review_id != context.review_id
        or template.definition_hash != context.definition_hash
        or station is None
        or station.revision != context.station_revision
        or meaning_hash(context.meaning) != context.meaning_hash
    ):
        raise PlaybackUnavailable(
            "The construction, station configuration or meaning changed; prepare again"
        )
    # Full recipe bindings include policies unused by this particular example.
    for concept_id, binding in template.bindings.items():
        from app.models import SignConcept

        concept = session.get(SignConcept, UUID(concept_id))
        if (
            concept is None
            or not concept.enabled
            or str(concept.active_motion_version_id) != binding["motion_version_id"]
            or concept.semantic_revision != binding["semantic_revision"]
        ):
            raise PlaybackUnavailable(
                "Construction dependencies changed; a fresh review is required"
            )


def _compile(session, store, meaning, station, input_id, caption, owner, valid_until, *, live=None):
    templates = session.scalars(
        select(AnnouncementTemplate).where(
            AnnouncementTemplate.enabled.is_(True),
            AnnouncementTemplate.status == "APPROVED",
            AnnouncementTemplate.definition["intent"].astext == meaning.intent,
        )
    ).all()
    matched = []
    for row in templates:
        definition = TemplateDefinition.model_validate(row.definition)
        if (
            definition.temporal_state == meaning.temporal_state
            and definition.polarity == meaning.polarity
            and definition.slot_types == {k: v.kind for k, v in meaning.slots.items()}
            and all(meaning.slots[k].value == v for k, v in definition.fixed_slots.items())
        ):
            matched.append((row, definition))
    require(
        bool(matched),
        "TEMPLATE_UNAVAILABLE",
        "No unique reviewed construction covers this complete meaning",
    )
    # Each fallback is a separately reviewed complete ISL construction, never English splitting.
    available, failures = [], []
    for stage in ("TEMPLATE", "EXACT", "PHRASE", "WORD"):
        candidates = [
            (row, definition) for row, definition in matched if definition.retrieval_stage == stage
        ]
        for row, definition in candidates:
            try:
                expand_recipe(definition, meaning)
                bindings = selected_dependencies(session, store, definition)
                require(
                    bindings == row.bindings,
                    "STALE_COMPOSITION",
                    "A dependency changed; review the construction again",
                )
                available.append((row, definition, bindings))
            except (LifecycleError, PlaybackUnavailable, StorageError) as exc:
                failures.append(exc)
                continue
        if available:
            break
    if not available and failures:
        raise failures[0]
    require(
        len(available) == 1,
        "AMBIGUOUS_TEMPLATE" if available else "MISSING_REALIZATION",
        "No unique complete reviewed covering realizes every value",
    )
    row, definition, bindings = available[0]
    concepts, groups, boundaries = expand_recipe(definition, meaning)
    motions = [UUID(bindings[str(cid)]["motion_version_id"]) for cid in concepts]
    avatar_id = session.scalar(
        select(MotionVersion.id)
        .join(AvatarProfile, MotionVersion.avatar_profile_id == AvatarProfile.id)
        .where(
            AvatarProfile.id == definition.avatar_profile_id,
            MotionVersion.sha256 == AvatarProfile.source_sha256,
            MotionVersion.deleted_at.is_(None),
            MotionVersion.revoked_at.is_(None),
        )
        .order_by(MotionVersion.id)
        .limit(1)
    )
    require(
        avatar_id is not None, "AVATAR_UNAVAILABLE", "The canonical avatar source is unavailable"
    )
    rows = _locked_versions(session, [avatar_id, *motions])
    _check_content(rows, definition.avatar_profile_id)
    _check_bytes(rows, store)
    context = TemplateContext(
        template_version_id=row.id,
        template_revision=row.revision,
        definition_hash=row.definition_hash,
        review_id=row.review_id,
        input_id=input_id,
        station_id=station.id,
        station_revision=station.revision,
        meaning=meaning,
        meaning_hash=meaning_hash(meaning),
    )
    return _persist(
        session,
        rows,
        motions,
        avatar_id,
        owner,
        "PUBLISHED" if live else "ANNOUNCEMENT_PREVIEW",
        caption,
        context=context,
        groups=groups,
        boundaries=boundaries,
        valid_until=valid_until,
        live=live,
    )


def _replay_receipt(session, store, row):
    response = TranslateResult.model_validate(row.response)
    if row.valid_until <= datetime.now(UTC):
        return response.model_copy(
            update={
                "status": "NEEDS_REVIEW",
                "manifest": None,
                "issues": [Issue(code="INPUT_EXPIRED", detail="Submit a fresh input revision")],
            }
        )
    if response.manifest is not None:
        try:
            validate_record(
                session,
                store,
                session.get(PlaybackRecord, row.manifest_id),
                development=response.demo_mode,
            )
        except PlaybackUnavailable:
            return response.model_copy(
                update={
                    "status": "NEEDS_REVIEW",
                    "manifest": None,
                    "issues": [
                        Issue(
                            code="PREVIEW_STALE",
                            detail="The catalog changed; submit a fresh input revision",
                        )
                    ],
                }
            )
    return response


def translate(session, store, request, identity, *, demo_mode_enabled=False):
    check_station_scope(identity, request.station_id)
    require(
        not request.demo_mode or demo_mode_enabled,
        "DEMO_MODE_DISABLED",
        "Development demo mode is disabled on this server",
        403,
    )
    now = datetime.now(UTC)
    valid_until = request.valid_until or now + timedelta(minutes=15)
    require(
        now < valid_until <= now + timedelta(minutes=15),
        "VALIDITY",
        "Preview validity must be in the next 15 minutes",
        422,
    )
    digest = definition_hash(request.model_dump(mode="json"))
    lock_key = int.from_bytes(
        hashlib.sha256((identity.subject + "\0" + str(request.request_id)).encode()).digest()[:8],
        "big",
        signed=True,
    )
    with session.begin():
        catalog_lock(session)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        prior = session.scalar(
            select(AnnouncementInputRecord).where(
                AnnouncementInputRecord.owner_subject == identity.subject,
                AnnouncementInputRecord.request_id == request.request_id,
            )
        )
        if prior:
            require(
                prior.request_hash == digest,
                "IDEMPOTENCY_CONFLICT",
                "Request ID already identifies a different input",
            )
            return _replay_receipt(session, store, prior)
        station = session.get(Station, request.station_id)
        require(
            station is not None,
            "STATION_UNCONFIGURED",
            "Configure the station before preparing announcements",
            422,
        )
        caption = structured_caption(request.structured) if request.structured else request.text
        voice = None
        if request.input_type == "VOICE":
            voice = session.get(VoiceTranscript, request.transcript_id)
            require(
                voice is not None and voice.owner_subject == identity.subject,
                "TRANSCRIPT_OWNER",
                "Use a transcript issued to this principal",
                403,
            )
            require(
                voice.valid_until > now and voice.asr_metadata.get("final") is True,
                "TRANSCRIPT_EXPIRED",
                "Record a new finalized transcript",
            )
            valid_until = min(valid_until, voice.valid_until)
        definition = StationDefinition.model_validate(station.definition)
        normalized, meaning, issues = parse_meaning(
            caption,
            station.id,
            definition,
            request.service_date,
            request.structured,
            development=demo_mode_enabled,
        )
        status = (
            "UNSUPPORTED" if meaning is None else "NEEDS_CONFIRMATION" if issues else "NEEDS_REVIEW"
        )
        if voice and not request.transcript_confirmed and not demo_mode_enabled:
            status = "NEEDS_CONFIRMATION"
            issues.append(
                Issue(
                    code="CONFIRM_TRANSCRIPT",
                    detail="Confirm the transcript and every critical field before preview",
                )
            )
        input_id, manifest, retrieval = uuid4(), None, None
        if demo_mode_enabled or (meaning is not None and not issues):
            try:
                if demo_mode_enabled:
                    units = construct_gloss(meaning, definition.development_gloss)
                    if meaning is None:
                        units, _ = recover_lexical(normalized)
                    retrieval = translation_trace(
                        caption, normalized, meaning, issues, units, definition.development_gloss
                    )
                    # Use the existing reviewed compiler whenever its complete semantic
                    # contract covers the input. Extended roles/time must not be erased
                    # to force a match with a less expressive reviewed construction.
                    _, strict_meaning, strict_issues = parse_meaning(
                        caption, station.id, definition, request.service_date, request.structured
                    )
                    if (
                        strict_meaning is None
                        and meaning is not None
                        and not issues
                        and not meaning.time_modifier
                        and validate_signature(
                            meaning.intent, meaning.temporal_state, meaning.slots
                        )
                        and not (
                            meaning.intent == "TRAIN_ARRIVAL"
                            and meaning.relations.get("platform_identifier") == "origin"
                        )
                        and not (
                            meaning.intent == "TRAIN_DEPARTURE"
                            and meaning.relations.get("platform_identifier") == "destination"
                        )
                    ):
                        strict_meaning = Meaning.model_validate(
                            meaning.model_dump(
                                exclude={
                                    "parser_version",
                                    "relations",
                                    "event_form",
                                    "time_modifier",
                                }
                            )
                        )
                        strict_issues = []
                    if strict_meaning is not None and not strict_issues:
                        try:
                            manifest = _compile(
                                session,
                                store,
                                strict_meaning,
                                station,
                                input_id,
                                caption,
                                identity.subject,
                                valid_until,
                            )
                        except (LifecycleError, PlaybackUnavailable, StorageError, OSError) as exc:
                            retrieval["reviewed_construction_unavailable"] = str(exc)
                    if manifest is not None:
                        keys = [item.semantic_key for item in manifest.items]
                        recipe = TemplateDefinition.model_validate(
                            session.get(
                                AnnouncementTemplate, manifest.announcement.template_version_id
                            ).definition
                        )
                        retrieval.update(
                            {
                                "translation_status": "REVIEWED_CONSTRUCTION",
                                "construction": {
                                    "source": "REVIEWED_RECIPE",
                                    "template_version_id": str(
                                        manifest.announcement.template_version_id
                                    ),
                                    "review_id": str(manifest.announcement.review_id),
                                    "linguistically_validated": True,
                                },
                                "gloss_tokens": keys,
                                "retrieval_tokens": keys,
                                "tokens": keys,
                                "gloss_units": [
                                    {
                                        "token": item.semantic_key,
                                        "kind": "CONCEPT",
                                        "concept_id": str(item.concept_id),
                                    }
                                    for item in manifest.items
                                ],
                                "matches": [
                                    {
                                        "input": item.semantic_key,
                                        "method": "REVIEWED_RECIPE",
                                        "matched": item.semantic_key,
                                        "motion_version_id": str(item.motion_version_id),
                                    }
                                    for item in manifest.items
                                ],
                                "fingerspelled": [
                                    strict_meaning.slots[step.slot].raw
                                    for step in recipe.recipe
                                    if step.kind == "SLOT" and step.policy.mode == "ISL_FINGERSPELL"
                                ],
                                "missing_gloss": [],
                                "skipped": [],
                                "semantic_coverage": "COMPLETE_REVIEWED",
                                "last_resort": False,
                                "unavailable_motion_ids": [],
                                "clip_count": len(manifest.items),
                                "retrieval_status": "COMPLETE",
                            }
                        )
                    else:
                        manifest, retrieval = prepare_demo(
                            session,
                            store,
                            caption,
                            identity.subject,
                            valid_until,
                            units=units,
                            translation=retrieval,
                        )
                else:
                    manifest = _compile(
                        session,
                        store,
                        meaning,
                        station,
                        input_id,
                        caption,
                        identity.subject,
                        valid_until,
                    )
                status = "READY" if manifest is not None else "ASSET_UNAVAILABLE"
                if manifest is None:
                    issues.append(
                        Issue(
                            code="NO_MEANINGFUL_MOTIONS",
                            detail="No playable motions or letters matching this input were found.",
                        )
                    )
            except LifecycleError as exc:
                issues.append(Issue(code=exc.code, detail=str(exc)))
            except PlaybackUnavailable as exc:
                if demo_mode_enabled:
                    status = "ASSET_UNAVAILABLE"
                issues.append(Issue(code="CONTENT_UNAVAILABLE", detail=str(exc)))
            except (StorageError, OSError):
                status = "ASSET_UNAVAILABLE"
                issues.append(
                    Issue(
                        code="ASSET_UNAVAILABLE",
                        detail="A required immutable object is unavailable",
                    )
                )
        result = TranslateResult(
            input_id=input_id,
            status=status,
            original_text=caption,
            normalized_text=normalized,
            meaning=meaning,
            meaning_hash=meaning_hash(meaning) if meaning else None,
            issues=issues,
            manifest=manifest,
            demo_mode=demo_mode_enabled,
            retrieval=retrieval,
        )
        session.add(
            AnnouncementInputRecord(
                id=input_id,
                request_id=request.request_id,
                owner_subject=identity.subject,
                request_hash=digest,
                station_id=station.id,
                input_type=request.input_type,
                transcript_id=request.transcript_id,
                response=result.model_dump(mode="json"),
                manifest_id=manifest.manifest_id if manifest else None,
                valid_until=valid_until,
            )
        )
        session.flush()
        if isinstance(manifest, AnnouncementPlan):
            context = manifest.announcement
            session.add(
                AnnouncementManifestRef(
                    manifest_id=manifest.manifest_id,
                    template_version_id=context.template_version_id,
                    review_id=context.review_id,
                    station_id=station.id,
                    input_id=input_id,
                )
            )
        _event(
            session,
            identity.subject,
            "ANNOUNCEMENT_PREVIEWED",
            input_id,
            {
                "station_id": station.id,
                "input_type": request.input_type,
                "status": status,
                "demo_mode": demo_mode_enabled,
                "meaning_hash": result.meaning_hash,
                "issue_codes": [i.code for i in issues],
                "transcript_corrected": bool(voice and caption != voice.text),
            },
        )
        return result
