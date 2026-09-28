"""Versioned reviewed recipes with exact-motion composition bindings."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.announcement_schema import ConceptStep, Meaning, StationDefinition, TemplateDefinition
from app.catalog import catalog_lock
from app.lifecycle import _event, require
from app.meaning import SLOT_TYPES, parse_meaning, structured_caption, validate_signature
from app.models import (
    AnnouncementTemplate,
    AvatarProfile,
    ContentReview,
    MotionVersion,
    PlaybackRecord,
    SignConcept,
    Station,
    TemplateDependency,
    TemplateMotionBinding,
    TemplatePreview,
)
from app.playback import (
    _check_bytes,
    _check_content,
    _eligible_ids,
    _locked_versions,
    _persist,
    validate_record,
)


def definition_hash(definition):
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dependency_ids(definition):
    ids = set()
    for step in definition.recipe:
        if isinstance(step, ConceptStep):
            ids.add(step.concept_id)
        else:
            for units in step.policy.units.values():
                ids.update(units)
    return ids


def template_result(row):
    return {
        field: getattr(row, field)
        for field in (
            "id",
            "template_key",
            "version_no",
            "definition",
            "definition_hash",
            "revision",
            "status",
            "enabled",
            "review_id",
            "bindings",
        )
    }


def create_template(session, request, actor):
    definition = request.definition
    require(
        validate_signature(definition.intent, definition.temporal_state, definition.slot_types),
        "INVALID_SIGNATURE",
        "Template intent and slot roles are inconsistent",
        422,
    )
    ids = dependency_ids(definition)
    require(
        all(SLOT_TYPES[k] == v for k, v in definition.slot_types.items()),
        "SLOT_TYPE_MISMATCH",
        "Template slot types must match their semantic roles",
        422,
    )
    require(
        len(ids) <= 64,
        "DEPENDENCY_LIMIT",
        "A construction supports at most 64 unique concepts",
        422,
    )
    with session.begin():
        catalog_lock(session, write=True)
        current = (
            session.scalar(
                select(func.max(AnnouncementTemplate.version_no)).where(
                    AnnouncementTemplate.template_key == request.template_key
                )
            )
            or 0
        )
        require(
            current == request.expected_version, "STALE_VERSION", "Refresh the template version"
        )
        concepts = session.scalars(select(SignConcept).where(SignConcept.id.in_(ids))).all()
        require(
            len(concepts) == len(ids) and all(c.language_code == "ISL" for c in concepts),
            "MISSING_DEPENDENCY",
            "Every dependency must be a registered ISL concept",
            422,
        )
        fixed = {step.concept_id for step in definition.recipe if isinstance(step, ConceptStep)}
        levels = {
            "EXACT": {"SENTENCE", "PHRASE"},
            "PHRASE": {"SENTENCE", "PHRASE"},
            "WORD": {"WORD"},
        }
        if definition.retrieval_stage in levels:
            require(
                all(
                    c.level in levels[definition.retrieval_stage] for c in concepts if c.id in fixed
                ),
                "LEVEL_MISMATCH",
                "Decomposition must use the declared reviewed content level",
                422,
            )
        data = definition.model_dump(mode="json")
        row = AnnouncementTemplate(
            template_key=request.template_key,
            version_no=current + 1,
            definition=data,
            definition_hash=definition_hash(data),
        )
        session.add(row)
        session.flush()
        session.add_all(
            [TemplateDependency(template_version_id=row.id, concept_id=cid) for cid in ids]
        )
        _event(
            session,
            actor,
            "TEMPLATE_STAGED",
            row.id,
            {"definition_hash": row.definition_hash, "reason": request.reason},
        )
        return template_result(row)


def selected_dependencies(session, store, definition):
    ids = dependency_ids(definition)
    concepts = session.scalars(select(SignConcept).where(SignConcept.id.in_(ids))).all()
    require(
        len(concepts) == len(ids) and all(c.active_motion_version_id for c in concepts),
        "MISSING_DEPENDENCY",
        "Required concepts have no selected approved motion",
    )
    motion_ids = [c.active_motion_version_id for c in concepts]
    rows = _locked_versions(session, motion_ids)
    _check_content(rows, definition.avatar_profile_id)
    require(
        _eligible_ids(session, motion_ids) == set(motion_ids),
        "UNAPPROVED_DEPENDENCY",
        "Every construction dependency must be eligible",
    )
    _check_bytes(rows, store)
    return {
        str(c.id): {
            "motion_version_id": str(m.id),
            "sha256": m.sha256,
            "semantic_revision": c.semantic_revision,
        }
        for m, c, _ in rows.values()
    }


def locked_template(session, template_id, revision):
    catalog_lock(session, write=True)
    row = session.get(AnnouncementTemplate, template_id, with_for_update=True)
    require(row is not None, "NOT_FOUND", "Template version not found", 404)
    require(row.revision == revision, "STALE_REVISION", "Refresh the template revision")
    return row


def preview_template(session, store, template_id, request, actor):
    with session.begin():
        catalog_lock(session)
        row = session.get(AnnouncementTemplate, template_id)
        station = session.get(Station, request.station_id)
        require(
            row is not None and station is not None,
            "NOT_FOUND",
            "Template or station not found",
            404,
        )
        definition = TemplateDefinition.model_validate(row.definition)
        caption = structured_caption(request.example)
        _, meaning, issues = parse_meaning(
            caption,
            station.id,
            StationDefinition.model_validate(station.definition),
            request.service_date,
            request.example,
        )
        require(
            meaning is not None and not issues,
            "INVALID_EXAMPLE",
            "Correct the example's critical fields",
            422,
        )
        require(
            definition.intent == meaning.intent
            and definition.temporal_state == meaning.temporal_state
            and definition.polarity == meaning.polarity
            and definition.slot_types == {k: v.kind for k, v in meaning.slots.items()}
            and all(meaning.slots[k].value == v for k, v in definition.fixed_slots.items()),
            "EXAMPLE_MISMATCH",
            "Example does not match the complete template signature",
            422,
        )
        bindings = selected_dependencies(session, store, definition)
        concepts, groups, boundaries = expand_recipe(definition, meaning)
        ids = [UUID(bindings[str(cid)]["motion_version_id"]) for cid in concepts]
        avatar_id = session.scalar(
            select(MotionVersion.id)
            .join(AvatarProfile)
            .where(
                AvatarProfile.id == definition.avatar_profile_id,
                MotionVersion.sha256 == AvatarProfile.source_sha256,
                MotionVersion.deleted_at.is_(None),
                MotionVersion.revoked_at.is_(None),
            )
            .order_by(MotionVersion.id)
            .limit(1)
        )
        require(avatar_id is not None, "AVATAR_UNAVAILABLE", "Canonical avatar is unavailable")
        rows = _locked_versions(session, [avatar_id, *ids])
        _check_content(rows, definition.avatar_profile_id)
        _check_bytes(rows, store)
        result = _persist(
            session,
            rows,
            ids,
            avatar_id,
            actor,
            "CONTENT_REVIEW",
            caption,
            groups=groups,
            boundaries=boundaries,
        )
        session.add(
            TemplatePreview(
                manifest_id=result.manifest.manifest_id,
                template_version_id=row.id,
                definition_hash=row.definition_hash,
                meaning=meaning.model_dump(mode="json"),
                bindings=bindings,
            )
        )
        return result


def review_template(session, store, template_id, request, actor):
    with session.begin():
        row = locked_template(session, template_id, request.expected_revision)
        require(
            row.definition_hash == request.definition_hash,
            "IDENTITY_MISMATCH",
            "Template definition changed",
        )
        definition = TemplateDefinition.model_validate(row.definition)
        bindings = (
            selected_dependencies(session, store, definition)
            if request.decision == "APPROVED"
            else {}
        )
        seen = set()
        for preview_id in request.preview_manifest_ids:
            preview = session.get(TemplatePreview, preview_id)
            require(
                preview is not None
                and preview.template_version_id == row.id
                and preview.definition_hash == row.definition_hash,
                "CONSTRUCTION_PREVIEW_REQUIRED",
                "Preview this exact template recipe before review",
            )
            require(
                not bindings or preview.bindings == bindings,
                "STALE_PREVIEW",
                "A construction dependency changed after preview",
            )
            record = session.get(PlaybackRecord, preview_id)
            require(
                record is not None
                and record.owner_subject == actor
                and record.purpose == "CONTENT_REVIEW",
                "PREVIEW_REQUIRED",
                "Use current content previews owned by this reviewer",
            )
            plan, _ = validate_record(session, store, record)
            ids, groups, boundaries = expand_recipe(
                definition, Meaning.model_validate(preview.meaning)
            )
            require(
                [str(item.concept_id) for item in plan.items] == [str(cid) for cid in ids]
                and [item.semantic_group for item in plan.items] == groups
                and plan.safe_boundaries == boundaries,
                "PREVIEW_MISMATCH",
                "Preview order and boundaries must match the construction",
            )
            require(
                plan.avatar.profile_id == definition.avatar_profile_id,
                "AVATAR_MISMATCH",
                "Preview avatar differs from the construction",
            )
            seen.update(str(item.motion_version_id) for item in plan.items)
        require(
            {v["motion_version_id"] for v in bindings.values()} <= seen,
            "PREVIEW_INCOMPLETE",
            "Preview every exact dependency in the construction and its realization policies",
        )
        review_id = uuid4()
        session.add(
            ContentReview(
                id=review_id,
                entity_id=row.id,
                entity_type="TEMPLATE",
                actor=actor,
                evidence=request.evidence,
                reason=request.reason,
                decision={**request.model_dump(mode="json"), "bindings": bindings},
            )
        )
        session.flush()
        session.add_all(
            [
                TemplateMotionBinding(
                    review_id=review_id,
                    template_version_id=row.id,
                    motion_version_id=v["motion_version_id"],
                )
                for v in bindings.values()
            ]
        )
        row.status, row.review_id, row.bindings = request.decision, review_id, bindings
        row.enabled = (
            False  # Admin activation is separate, including after a new composition review.
        )
        row.revision += 1
        _event(
            session,
            actor,
            "TEMPLATE_REVIEWED",
            row.id,
            {"review_id": str(review_id), "decision": request.decision, "reason": request.reason},
        )
        return template_result(row)


def activate_template(session, store, template_id, request, actor):
    with session.begin():
        row = locked_template(session, template_id, request.expected_revision)
        if request.enabled:
            require(
                row.status == "APPROVED" and row.review_id is not None,
                "REVIEW_REQUIRED",
                "The construction requires explicit composition approval",
            )
            require(
                selected_dependencies(
                    session, store, TemplateDefinition.model_validate(row.definition)
                )
                == row.bindings,
                "STALE_COMPOSITION",
                "Changed dependencies require a new construction review",
            )
            previous = session.scalars(
                select(AnnouncementTemplate).where(
                    AnnouncementTemplate.template_key == row.template_key,
                    AnnouncementTemplate.enabled.is_(True),
                    AnnouncementTemplate.id != row.id,
                )
            ).all()
            for old in previous:
                old.enabled = False
                old.revision += 1
            session.flush()
        row.enabled = request.enabled
        row.revision += 1
        _event(
            session,
            actor,
            "TEMPLATE_ACTIVATION_CHANGED",
            row.id,
            {"enabled": row.enabled, "revision": row.revision, "reason": request.reason},
        )
        return template_result(row)


def expand_recipe(definition, meaning):
    """Restore order/repetition after policy lookup; complete meaning is mandatory."""
    ids, groups = [], []
    for step in definition.recipe:
        if isinstance(step, ConceptStep):
            resolved = [step.concept_id]
        else:
            slot, policy = meaning.slots[step.slot], step.policy
            value = slot.value
            if policy.mode == "EXACT_VALUES":
                require(
                    value in policy.units,
                    "MISSING_REALIZATION",
                    f"No reviewed realization for {step.slot}",
                )
                resolved = policy.units[value]
            else:
                if policy.mode == "ISL_FINGERSPELL":
                    require(
                        value in policy.spellings,
                        "NAME_POLICY_REQUIRED",
                        "This name has no reviewed spelling",
                    )
                    value = policy.spellings[value]
                require(
                    all(char in policy.units for char in value),
                    "MISSING_REALIZATION",
                    f"Incomplete units for {step.slot}",
                )
                resolved = [cid for char in value for cid in policy.units[char]]
        ids.extend(resolved)
        groups.extend([step.group] * len(resolved))
        require(
            len(ids) <= 64,
            "MESSAGE_LIMIT",
            "The complete construction exceeds 64 motion occurrences",
        )
    boundaries = [
        i
        for i, group in enumerate(groups)
        if (i == len(groups) - 1 or groups[i + 1] != group)
        and (group in definition.safe_after_groups or i == len(groups) - 1)
    ]
    return ids, groups, boundaries
