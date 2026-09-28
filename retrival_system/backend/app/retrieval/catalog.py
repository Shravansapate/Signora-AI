"""Explicit retrieval metadata review and resumable semantic indexing."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, text

from app.catalog import catalog_lock
from app.lifecycle import _concept, _event, require
from app.meaning import normalize
from app.models import ContentReview, RetrievalProfile, SignAlias, SignConcept
from app.retrieval.encoder_model import ENCODING_POLICY, MODEL_ID, MODEL_REVISION


def passage(session, concept, profile):
    aliases = session.scalars(
        select(SignAlias)
        .where(
            SignAlias.concept_id == concept.id,
            SignAlias.review_status == "APPROVED",
            SignAlias.review_id.is_not(None),
            SignAlias.reviewed_semantic_revision == concept.semantic_revision,
            SignAlias.source_language == "en",
            SignAlias.domain == profile.domain,
            SignAlias.context == profile.context,
        )
        .order_by(SignAlias.normalized_alias)
    ).all()
    return json.dumps(
        {
            "canonical": concept.canonical_text,
            "semantic_revision": concept.semantic_revision,
            "description": profile.description,
            "domain": profile.domain,
            "context": profile.context,
            "sense": profile.sense,
            "aliases": [row.alias for row in aliases],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def refresh_hash(session, concept, profile):
    session.flush()
    profile.input_hash = hashlib.sha256(passage(session, concept, profile).encode()).hexdigest()


def review_profile(session, concept_id, request, actor):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        require(
            concept.domain == request.domain and concept.context == request.context,
            "SCOPE_MISMATCH",
            "Retrieval scope must match reviewed concept meaning",
            422,
        )
        require(
            concept.meaning_status == "APPROVED", "MEANING_REQUIRED", "Review concept meaning first"
        )
        row = session.get(RetrievalProfile, concept_id)
        signature = request.sense.model_dump(mode="json")
        require(
            row is None
            or row.semantic_revision != concept.semantic_revision
            or row.sense == signature,
            "SEMANTIC_REVIEW_REQUIRED",
            "Change concept meaning before changing its retrieval sense",
        )
        review_id = uuid4()
        session.add(
            ContentReview(
                id=review_id,
                entity_id=concept.id,
                entity_type="RETRIEVAL_PROFILE",
                actor=actor,
                evidence=request.evidence,
                reason=request.reason,
                decision=request.model_dump(mode="json"),
            )
        )
        session.flush()
        if row is None:
            row = RetrievalProfile(concept_id=concept_id)
            session.add(row)
        row.semantic_revision, row.domain, row.context = (
            concept.semantic_revision,
            request.domain,
            request.context,
        )
        row.sense, row.description, row.status, row.review_id = (
            signature,
            request.description,
            request.decision,
            review_id,
        )
        row.input_hash = "0" * 64
        refresh_hash(session, concept, row)
        concept.revision += 1
        _event(
            session,
            actor,
            "RETRIEVAL_PROFILE_REVIEWED",
            concept.id,
            {"review_id": str(review_id), "input_hash": row.input_hash, "status": row.status},
        )
        return {
            "concept_id": concept.id,
            "revision": concept.revision,
            "input_hash": row.input_hash,
            "status": row.status,
        }


def review_alias(session, concept_id, request, actor):
    with session.begin():
        concept = _concept(session, concept_id, request.expected_revision)
        profile = session.get(RetrievalProfile, concept_id)
        require(
            profile is not None
            and profile.status == "APPROVED"
            and profile.semantic_revision == concept.semantic_revision,
            "PROFILE_REQUIRED",
            "Review the current retrieval sense first",
        )
        require(
            (profile.domain, profile.context) == (request.domain, request.context),
            "SCOPE_MISMATCH",
            "Alias scope must match the reviewed sense",
            422,
        )
        normalized = normalize(request.alias)[0]
        require(bool(normalized), "EMPTY_ALIAS", "An alias cannot be empty", 422)
        row = session.scalar(
            select(SignAlias).where(
                SignAlias.concept_id == concept_id, SignAlias.normalized_alias == normalized
            )
        )
        review_id = uuid4()
        session.add(
            ContentReview(
                id=review_id,
                entity_id=concept.id,
                entity_type="ALIAS",
                actor=actor,
                evidence=request.evidence,
                reason=request.reason,
                decision=request.model_dump(mode="json"),
            )
        )
        session.flush()
        if row is None:
            row = SignAlias(concept_id=concept_id, normalized_alias=normalized)
            session.add(row)
        row.alias, row.source_language, row.review_status = (
            request.alias,
            request.source_language,
            request.decision,
        )
        row.domain, row.context = request.domain, request.context
        row.reviewed_semantic_revision, row.review_id = concept.semantic_revision, review_id
        refresh_hash(session, concept, profile)
        concept.revision += 1
        _event(
            session,
            actor,
            "ALIAS_REVIEWED",
            concept.id,
            {"review_id": str(review_id), "status": row.review_status},
        )
        return {"alias_id": row.id, "revision": concept.revision, "input_hash": profile.input_hash}


def index_concepts(sessions, encoder, ids, actor):
    results = []
    for cid in dict.fromkeys(ids):
        try:
            with sessions() as session, session.begin():
                catalog_lock(session)
                concept, profile = session.get(SignConcept, cid), session.get(RetrievalProfile, cid)
                require(
                    concept is not None
                    and profile is not None
                    and profile.status == "APPROVED"
                    and concept.meaning_status == "APPROVED"
                    and profile.semantic_revision == concept.semantic_revision,
                    "PROFILE_REQUIRED",
                    "No current approved semantic input",
                )
                content, digest, semantic_revision = (
                    passage(session, concept, profile),
                    profile.input_hash,
                    concept.semantic_revision,
                )
                exists = session.scalar(
                    text("""SELECT 1 FROM sign_embeddings WHERE concept_id=:id
                  AND model_revision=:model AND encoding_policy=:policy AND input_hash=:hash"""),
                    {"id": cid, "model": MODEL_REVISION, "policy": ENCODING_POLICY, "hash": digest},
                )
                if exists:
                    results.append({"concept_id": str(cid), "status": "UNCHANGED"})
                    continue
            vector = encoder.encode(content, "passage")  # No open transaction while encoding.
            with sessions() as session, session.begin():
                catalog_lock(session)
                current = session.get(RetrievalProfile, cid)
                concept = session.get(SignConcept, cid)
                require(
                    current is not None
                    and current.input_hash == digest
                    and current.status == "APPROVED"
                    and concept.semantic_revision == semantic_revision,
                    "STALE_INPUT",
                    "Semantic input changed during encoding",
                )
                session.execute(
                    text("""INSERT INTO sign_embeddings
                  (concept_id,semantic_revision,model_id,model_revision,encoding_policy,input_hash,embedding)
                  VALUES (:id,:semantic,:model_id,:model,:policy,:hash,CAST(:vector AS vector))
                  ON CONFLICT DO NOTHING"""),
                    {
                        "id": cid,
                        "semantic": semantic_revision,
                        "model_id": MODEL_ID,
                        "model": MODEL_REVISION,
                        "policy": ENCODING_POLICY,
                        "hash": digest,
                        "vector": json.dumps(vector),
                    },
                )
                _event(
                    session,
                    actor,
                    "SEMANTIC_INPUT_INDEXED",
                    cid,
                    {"input_hash": digest, "model_revision": MODEL_REVISION},
                )
            results.append({"concept_id": str(cid), "status": "INDEXED"})
        except ValueError as exc:
            results.append(
                {
                    "concept_id": str(cid),
                    "status": "PENDING",
                    "code": getattr(exc, "code", "ENCODER_UNAVAILABLE"),
                }
            )
    return {"items": results}
