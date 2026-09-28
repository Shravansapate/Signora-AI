"""Retrieval review tools reuse existing identities and audit records."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.config import Principal
from app.lifecycle import require
from app.retrieval.catalog import index_concepts, review_alias, review_profile
from app.retrieval.search import search
from app.retrieval.search_schema import AliasReview, IndexRequest, ProfileReview, SearchRequest


def retrieval_router(sessions, principal, encoder):
    router = APIRouter(prefix="/api/v1/review/retrieval")
    Identity = Annotated[Principal, Depends(principal)]

    def reviewer(identity: Identity):
        require(
            bool(identity.roles & {"reviewer", "admin"}),
            "ROLE_REQUIRED",
            "Reviewer role required",
            403,
        )
        return identity

    Reviewer = Annotated[Principal, Depends(reviewer)]

    @router.get("/status")
    def status(identity: Reviewer):
        return {"encoder_state": encoder.state, "approximate_selection": "REVIEW_ONLY"}

    @router.post("/concepts/{concept_id}")
    def profile(concept_id: UUID, request: ProfileReview, identity: Reviewer):
        with sessions() as session:
            return review_profile(session, concept_id, request, identity.subject)

    @router.post("/concepts/{concept_id}/aliases")
    def alias(concept_id: UUID, request: AliasReview, identity: Reviewer):
        with sessions() as session:
            return review_alias(session, concept_id, request, identity.subject)

    @router.post("/index")
    def index(request: IndexRequest, identity: Reviewer):
        return index_concepts(sessions, encoder, request.concept_ids, identity.subject)

    @router.post("/search")
    def retrieve(request: SearchRequest, identity: Reviewer):
        return search(sessions, encoder, request)

    return router
