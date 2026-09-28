"""Scoped candidate retrieval. Approximate candidates never authorize a playback plan."""

import json
import logging
import re
import time
from uuid import uuid4

from sqlalchemy import text

from app.catalog import catalog_lock
from app.lifecycle import LifecycleError
from app.meaning import normalize
from app.retrieval.encoder_model import ENCODING_POLICY, MODEL_ID, MODEL_REVISION
from app.retrieval.search_policy import AMBIGUITY_MARGIN, FUZZY_MIN, POLICY_VERSION, SEMANTIC_MIN


def numeric_identity(value):
    return re.findall(r"[0-9]+[a-z]*", normalize(value)[0])


# The guard only rejects conflicts. Absence of a word never establishes its opposite.
def critical_words(value):
    normalized = re.sub(r"\btrain no\.?\s+", "train number ", normalize(value)[0])
    words = set(re.findall(r"[a-z]+", normalized))
    classes = {
        "event": (
            {"arrive", "arrival", "arriving", "arrived"},
            {"depart", "departure", "departing", "departed"},
            {"cancelled", "canceled", "cancellation"},
        ),
        "access": ({"open", "opened"}, {"closed", "close"}),
    }
    return {
        key: {i for i, family in enumerate(families) if words & family}
        for key, families in classes.items()
    }, words


def compatible_text(query, candidate):
    if numeric_identity(query) != numeric_identity(candidate):
        return False
    role_values = r"\b(train(?: number| no\.?)?|platform)\s+([0-9]+[a-z]*)\b"

    def roles(value):
        return [
            (role.split()[0], literal)
            for role, literal in re.findall(role_values, normalize(value)[0])
        ]

    if sorted(roles(query)) != sorted(roles(candidate)):
        return False
    qclasses, qw = critical_words(query)
    cclasses, cw = critical_words(candidate)
    if any(qclasses[k] and cclasses[k] and qclasses[k] != cclasses[k] for k in qclasses):
        return False
    if bool(qw & {"not", "never", "no"}) != bool(cw & {"not", "never", "no"}):
        return False
    if (qw & {"arrived", "departed"} and cw & {"arriving", "departing"}) or (
        cw & {"arrived", "departed"} and qw & {"arriving", "departing"}
    ):
        return False
    for family in ({"minute", "minutes", "hour", "hours"}, {"am", "pm"}):
        if {w.rstrip("s") for w in qw & family} != {w.rstrip("s") for w in cw & family}:
            return False
    role_pattern = r"\bfrom (.+?) to (.+?)(?= (?:is|has|will)\b|$)"
    qr = re.search(role_pattern, normalize(query)[0])
    cr = re.search(role_pattern, normalize(candidate)[0])
    if bool(qr) != bool(cr) or (qr and qr.groups() != cr.groups()):
        return False
    return True


BASE = """
 FROM sign_concepts c JOIN eligible_motion_versions m ON m.concept_id=c.id
 JOIN retrieval_profiles p ON p.concept_id=c.id
 WHERE p.status='APPROVED' AND p.semantic_revision=c.semantic_revision
 AND p.domain=:domain AND p.context=:context AND c.domain=p.domain AND c.context=p.context
 AND m.avatar_profile_id=:avatar AND c.level=ANY(:levels)
 AND (CAST(:sense AS jsonb) IS NULL OR p.sense=CAST(:sense AS jsonb))
"""
FIELDS = "c.id AS concept_id,c.canonical_text,c.level,m.id AS motion_version_id,m.sha256,p.sense"


def query_rows(session, request, normalized, vector=None):
    params = {
        "domain": request.domain,
        "context": request.context,
        "avatar": request.avatar_profile_id,
        "levels": request.levels,
        "sense": json.dumps(request.sense.model_dump()) if request.sense else None,
        "query": normalized,
    }
    expressions = """SELECT c.id AS concept_id,c.normalized_canonical_text AS expression
      FROM sign_concepts c UNION ALL SELECT a.concept_id,a.normalized_alias
      FROM sign_aliases a JOIN sign_concepts c ON c.id=a.concept_id
      WHERE a.review_status='APPROVED' AND a.review_id IS NOT NULL
      AND a.source_language='en' AND a.domain=:domain AND a.context=:context
      AND a.reviewed_semantic_revision=c.semantic_revision"""
    if vector is None:
        sql = f"""SELECT {FIELDS},
          (c.normalized_canonical_text=:query) AS exact,
          EXISTS(SELECT 1 FROM ({expressions}) e
            WHERE e.concept_id=c.id AND e.expression=:query) AS alias,
          (SELECT max(similarity(e.expression,:query)) FROM ({expressions}) e
            WHERE e.concept_id=c.id) AS score
          {BASE} ORDER BY exact DESC, alias DESC, score DESC,c.id LIMIT 12"""
    else:
        params.update(
            vector=json.dumps(vector),
            model=MODEL_REVISION,
            policy=ENCODING_POLICY,
            model_id=MODEL_ID,
        )
        sql = f"""SELECT {FIELDS}, 1-(e.embedding <=> CAST(:vector AS vector)) AS score
          FROM sign_embeddings e JOIN sign_concepts c ON c.id=e.concept_id
          JOIN eligible_motion_versions m ON m.concept_id=c.id
          JOIN retrieval_profiles p ON p.concept_id=c.id
          WHERE p.status='APPROVED' AND p.semantic_revision=c.semantic_revision
          AND p.domain=:domain AND p.context=:context AND c.domain=p.domain AND c.context=p.context
          AND m.avatar_profile_id=:avatar AND c.level=ANY(:levels)
          AND (CAST(:sense AS jsonb) IS NULL OR p.sense=CAST(:sense AS jsonb))
          AND e.semantic_revision=c.semantic_revision AND e.input_hash=p.input_hash
          AND e.model_id=:model_id AND e.model_revision=:model AND e.encoding_policy=:policy
          ORDER BY e.embedding <=> CAST(:vector AS vector),c.id LIMIT 12"""
    return [dict(row) for row in session.execute(text(sql), params).mappings()]


def lexical_candidates(rows):
    trace = []
    for stage, predicate in [("EXACT", lambda r: r["exact"]), ("ALIAS", lambda r: r["alias"])]:
        candidates = [{**r, "method": stage} for r in rows if predicate(r)]
        trace.append({"method": stage, "candidates": len(candidates)})
        if candidates:
            return candidates, trace, stage
    candidates = [{**r, "method": "FUZZY"} for r in rows if r["score"] >= FUZZY_MIN]
    trace.append({"method": "FUZZY", "candidates": len(candidates), "minimum": FUZZY_MIN})
    return candidates, trace, "FUZZY" if candidates else "NONE"


def search(sessions, encoder, request):
    began, query_id = time.perf_counter(), str(uuid4())
    normalized = normalize(request.text)[0]

    def lexical(session):
        rows = query_rows(session, request, normalized)
        return lexical_candidates(
            [r for r in rows if compatible_text(normalized, r["canonical_text"])]
        )

    with sessions() as session, session.begin():
        catalog_lock(session)
        candidates, trace, method = lexical(session)
    vector, failure = None, None
    if method not in {"EXACT", "ALIAS"} and request.semantic:
        try:
            vector = encoder.encode(normalized, "query")
        except LifecycleError as exc:
            failure = exc.code
        # Re-read both stages after inference/outage: replacements and withdrawals may have
        # committed while no catalog lock was held. Never merge stale lexical selections.
        with sessions() as session, session.begin():
            catalog_lock(session)
            candidates, trace, method = lexical(session)
            if method not in {"EXACT", "ALIAS"}:
                semantic = (
                    []
                    if vector is None
                    else [
                        {**r, "method": "SEMANTIC"}
                        for r in query_rows(session, request, normalized, vector)
                        if r["score"] >= SEMANTIC_MIN
                        and compatible_text(normalized, r["canonical_text"])
                    ]
                )
                trace.append(
                    {
                        "method": "SEMANTIC",
                        "candidates": len(semantic),
                        "minimum": SEMANTIC_MIN,
                        "model_revision": MODEL_REVISION,
                        **({"code": failure} if failure else {}),
                    }
                )
                # A concept may appear in both lists. Preserve each stage's rank and score;
                # fuzzy results cannot crowd semantic suggestions out of the response.
                candidates += semantic
    exact = method in {"EXACT", "ALIAS"}
    ambiguity = []
    for stage in ("EXACT", "ALIAS", "FUZZY", "SEMANTIC"):
        group = [r for r in candidates if r["method"] == stage]
        margin = group[0]["score"] - group[1]["score"] if len(group) > 1 else None
        if len(group) > 1 and (stage in {"EXACT", "ALIAS"} or margin < AMBIGUITY_MARGIN):
            ambiguity.append(stage)
    status = (
        "MATCHED"
        if exact and len(candidates) == 1 and request.sense
        else "NEEDS_REVIEW"
        if candidates
        else "UNSUPPORTED"
    )
    result = {
        "query_id": query_id,
        "status": status,
        "operational": False,
        "candidates": [
            {k: v for k, v in r.items() if k not in {"exact", "alias"}} for r in candidates
        ],
        "trace": trace,
        "ambiguous_stages": ambiguity,
        "policy_version": POLICY_VERSION,
        "encoder_state": encoder.state,
        "reason": "Approximate matches are review suggestions. Announcement playback requires "
        "a complete reviewed construction.",
    }
    logging.getLogger("signora.api.retrieval").info(
        json.dumps(
            {
                "event": "retrieval_complete",
                "query_id": query_id,
                "status": status,
                "methods": [r["method"] for r in trace],
                "candidates": len(candidates),
                "encoder_code": failure,
                "ambiguous_stages": ambiguity,
                "elapsed_ms": round((time.perf_counter() - began) * 1000, 2),
            }
        )
    )
    return result
