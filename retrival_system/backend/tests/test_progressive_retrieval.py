"""Real PostgreSQL, pg_trgm/pgvector and source GLBs; isolated synthetic reviews only."""
# ruff: noqa: F811 -- isolated module fixtures reuse existing staging/review workflows.

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from test_announcements import announcement_setup, payload, review_and_activate  # noqa: F401
from test_lifecycle import (  # noqa: F401
    approve_fixture,
    history,
    lifecycle_clients,
    request_ok,
    switch,
    variant,
)
from test_registry import registry_database  # noqa: F401

from app.config import Settings
from app.registry import stage_motion
from app.retrieval.catalog import index_concepts
from app.retrieval.encoder import LocalEncoder
from app.storage import LocalAssetStore

SENSE = {
    "semantic_class": "RAIL_VEHICLE",
    "polarity": "NEUTRAL",
    "temporal_state": "NONE",
    "literals": {},
}


def profile(client, headers, cid, sense=SENSE):
    concept = history(client, headers, cid)["concept"]
    return request_ok(
        client,
        headers,
        f"/api/v1/review/retrieval/concepts/{cid}",
        {
            "expected_revision": concept["revision"],
            "domain": "test",
            "context": "isolated exact content",
            "sense": sense,
            "description": "A railway train carries passengers on tracks.",
            "decision": "APPROVED",
            "evidence": "Synthetic retrieval fixture only",
            "reason": "Isolated engineering test",
        },
        role="reviewer",
    )


def alias(client, headers, cid, value, decision="APPROVED"):
    concept = history(client, headers, cid)["concept"]
    return request_ok(
        client,
        headers,
        f"/api/v1/review/retrieval/concepts/{cid}/aliases",
        {
            "expected_revision": concept["revision"],
            "alias": value,
            "domain": "test",
            "context": "isolated exact content",
            "decision": decision,
            "evidence": "Synthetic alias decision",
            "reason": "Isolated test",
        },
        role="reviewer",
    )


@pytest.fixture(scope="module")
def search_setup(announcement_setup, lifecycle_clients):
    assets, _ = announcement_setup
    client, headers = lifecycle_clients
    cid = assets["Train"]["concept_id"]
    profile(client, headers, cid)
    alias(client, headers, cid, "railway vehicle")
    avatar = history(client, headers, cid)["versions"][0]["avatar_profile_id"]
    return assets, {
        "text": "train",
        "domain": "test",
        "context": "isolated exact content",
        "avatar_profile_id": avatar,
        "sense": SENSE,
        "semantic": False,
    }


def retrieve(client, headers, request):
    return request_ok(client, headers, "/api/v1/review/retrieval/search", request, role="reviewer")


def test_exact_alias_fuzzy_and_outage_use_one_eligibility_contract(search_setup, lifecycle_clients):
    assets, request = search_setup
    client, headers = lifecycle_clients
    for value, method in [("train", "EXACT"), ("railway vehicle", "ALIAS")]:
        result = retrieve(client, headers, {**request, "text": value, "semantic": True})
        assert result["status"] == "MATCHED" and result["operational"] is False
        assert result["candidates"][0]["method"] == method
        assert result["candidates"][0]["concept_id"] == assets["Train"]["concept_id"]
        assert all(step["method"] != "SEMANTIC" for step in result["trace"])
    fuzzy = retrieve(client, headers, {**request, "text": "trian", "semantic": True})
    assert fuzzy["status"] == "NEEDS_REVIEW"
    assert fuzzy["candidates"][0]["method"] == "FUZZY"
    assert fuzzy["trace"][-1]["code"] == "ENCODER_UNAVAILABLE"
    assert "manifest" not in fuzzy


@pytest.mark.parametrize(
    "changes",
    [
        {"domain": "other"},
        {"context": "other"},
        {"levels": ["SENTENCE"]},
        {"sense": {**SENSE, "polarity": "NEGATIVE"}},
    ],
)
def test_scope_sense_and_level_cannot_be_overridden_by_similarity(
    changes, search_setup, lifecycle_clients
):
    _, request = search_setup
    result = retrieve(*lifecycle_clients, {**request, **changes})
    assert not result["candidates"] and result["status"] == "UNSUPPORTED"


def test_alias_withdrawal_and_deactivation_revalidate_every_lookup(search_setup, lifecycle_clients):
    assets, request = search_setup
    client, headers = lifecycle_clients
    cid = assets["Train"]["concept_id"]
    alias(client, headers, cid, "railway vehicle", "REJECTED")
    assert all(
        c["method"] != "ALIAS"
        for c in retrieve(client, headers, {**request, "text": "railway vehicle"})["candidates"]
    )
    alias(client, headers, cid, "railway vehicle")
    before = history(client, headers, cid)["concept"]
    changed = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/deactivate",
        {"expected_revision": before["revision"], "reason": "isolated test"},
    )
    try:
        assert not retrieve(client, headers, request)["candidates"]
    finally:
        request_ok(
            client,
            headers,
            f"/api/v1/admin/signs/{cid}/reactivate",
            {"expected_revision": changed["revision"], "reason": "restore fixture"},
        )


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_ENCODER") != "1", reason="Opt-in real E5 model")
def test_pgvector_real_embedding_roundtrip_revision_hash_and_quality_replacement(
    search_setup, lifecycle_clients, registry_database, tmp_path
):
    root = Path(__file__).resolve().parents[2]
    settings, engine, sessions = registry_database
    encoder = LocalEncoder(
        Settings(
            _env_file=None,
            database_url=settings.database_url,
            storage_root=settings.storage_root,
            encoder_model_path=root / "backend/artifacts/e5-model",
        )
    )
    assets, request = search_setup
    cid = UUID(assets["Train"]["concept_id"])
    client, headers = lifecycle_clients
    try:
        assert index_concepts(sessions, encoder, [cid], "test")["items"][0]["status"] == "INDEXED"
        assert index_concepts(sessions, encoder, [cid], "test")["items"][0]["status"] == "UNCHANGED"
        from app.retrieval.search import query_rows
        from app.retrieval.search_schema import SearchRequest

        vector = encoder.encode("railway transport", "query")
        with sessions() as session:
            rows = query_rows(
                session, SearchRequest.model_validate(request), "railway transport", vector
            )
            assert len(rows) == 1 and str(rows[0]["concept_id"]) == str(cid)
            assert 0 < rows[0]["score"] <= 1
        before = history(client, headers, str(cid))["concept"]
        paths = variant(tmp_path, before["semantic_key"], marker="retrieval-quality")
        with sessions() as session:
            staged = stage_motion(
                *paths, "isolated", session, LocalAssetStore(settings.storage_root)
            )
        approve_fixture(client, headers, str(cid), staged["motion_version_id"])
        switch(client, headers, str(cid), staged["motion_version_id"])
        assert index_concepts(sessions, encoder, [cid], "test")["items"][0]["status"] == "UNCHANGED"
        alias(client, headers, str(cid), "rail transport")
        with sessions() as session:
            assert not query_rows(
                session, SearchRequest.model_validate(request), "railway transport", vector
            )
        assert index_concepts(sessions, encoder, [cid], "test")["items"][0]["status"] == "INDEXED"
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM sign_embeddings WHERE concept_id=:id"), {"id": cid}
                )
                == 2
            )
    finally:
        encoder.close()


@pytest.fixture(scope="module")
def approved_word(announcement_setup, lifecycle_clients):
    _, template = announcement_setup
    client, headers = lifecycle_clients
    definition = deepcopy(template["definition"])
    definition["retrieval_stage"] = "WORD"
    word = request_ok(
        client,
        headers,
        "/api/v1/admin/templates",
        {
            "template_key": "synthetic-word-fallback",
            "expected_version": 0,
            "definition": definition,
            "reason": "Synthetic complete word construction",
        },
        expected=201,
    )
    active = review_and_activate(
        client,
        headers,
        word,
        {
            "intent": "TRAIN_ARRIVAL",
            "temporal_state": "ARRIVING_NOW",
            "slots": {"train_identifier": "00110", "platform_identifier": "1"},
        },
    )
    return active


def test_approved_word_decomposition_uses_recipe_order_without_english_splitting(
    approved_word, lifecycle_clients
):
    client, headers = lifecycle_clients
    result = request_ok(client, headers, "/api/v1/translate", payload())
    assert result["status"] == "READY", result
    assert result["manifest"]["announcement"]["template_version_id"] == approved_word["id"]
    assert result["manifest"]["safe_boundaries"] == [5, 6, 7]


def test_retrieval_roles_bounds_and_missing_sense(search_setup, lifecycle_clients):
    _, request = search_setup
    client, headers = lifecycle_clients
    path = "/api/v1/review/retrieval/search"
    assert client.post(path, json=request).status_code == 401
    for role in ("operator", "display"):
        assert client.post(path, headers=headers[role], json=request).status_code == 403
    for change in ({"source_language": "hi"}, {"levels": []}, {"text": " "}):
        assert (
            client.post(path, headers=headers["reviewer"], json={**request, **change}).status_code
            == 422
        )
    assert retrieve(client, headers, {**request, "sense": None})["status"] == "NEEDS_REVIEW"
    too_many = {"concept_ids": [search_setup[0]["Train"]["concept_id"]] * 33}
    assert (
        client.post(
            "/api/v1/review/retrieval/index", headers=headers["reviewer"], json=too_many
        ).status_code
        == 422
    )


def test_pending_ambiguous_and_revoked_candidates(
    search_setup, lifecycle_clients, registry_database, tmp_path
):
    assets, request = search_setup
    client, headers = lifecycle_clients
    settings, _, sessions = registry_database
    paths = variant(tmp_path, "ISOLATED_SEARCH_DUPLICATE", marker="ambiguous")
    with sessions() as session:
        staged = stage_motion(*paths, "isolated", session, LocalAssetStore(settings.storage_root))
    cid, version = staged["concept_id"], staged["motion_version_id"]
    assert all(c["concept_id"] != cid for c in retrieve(client, headers, request)["candidates"])
    approve_fixture(client, headers, cid, version)
    switch(client, headers, cid, version)
    profile(client, headers, cid)
    duplicate = retrieve(client, headers, request)
    assert duplicate["status"] == "NEEDS_REVIEW" and duplicate["ambiguous_stages"] == ["EXACT"]
    request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/motions/{version}/revoke",
        {
            "expected_revision": history(client, headers, cid)["concept"]["revision"],
            "reason": "Synthetic defective candidate",
        },
    )
    result = retrieve(client, headers, request)
    assert [c["concept_id"] for c in result["candidates"]] == [assets["Train"]["concept_id"]]


def test_template_exact_phrase_word_hierarchy_and_complete_fallback(
    announcement_setup, approved_word, lifecycle_clients, registry_database, tmp_path
):
    _, template = announcement_setup
    client, headers = lifecycle_clients
    settings, _, sessions = registry_database
    # Test-copy identity only; this exercises covering mechanics, not actual ISL grammar.
    metadata, asset = variant(tmp_path, "ISOLATED_PHRASE_MECHANICS", marker="phrase")
    document = json.loads(metadata.read_text())
    document["motion_identity"]["level"] = "PHRASE"
    metadata.write_text(json.dumps(document))
    with sessions() as session:
        staged = stage_motion(
            metadata, asset, "isolated", session, LocalAssetStore(settings.storage_root)
        )
    cid, version = staged["concept_id"], staged["motion_version_id"]
    approve_fixture(client, headers, cid, version)
    switch(client, headers, cid, version)
    example = {
        "intent": "TRAIN_ARRIVAL",
        "temporal_state": "ARRIVING_NOW",
        "slots": {"train_identifier": "00110", "platform_identifier": "1"},
    }
    phrase = deepcopy(template["definition"])
    phrase["retrieval_stage"] = "PHRASE"
    phrase["recipe"] = [
        {
            "kind": "CONCEPT",
            "concept_id": cid,
            "covers": ["intent", "temporal_state", "polarity"],
            "group": "event",
        },
        phrase["recipe"][1],
        phrase["recipe"][3],
    ]
    phrase["safe_after_groups"] = ["event", "train", "platform"]
    exact = deepcopy(phrase)
    exact["retrieval_stage"] = "EXACT"
    exact["fixed_slots"] = example["slots"]
    exact["recipe"] = [
        {
            "kind": "CONCEPT",
            "concept_id": cid,
            "covers": ["intent", "temporal_state", "polarity", *example["slots"]],
            "group": "whole",
        }
    ]
    exact["safe_after_groups"] = ["whole"]
    constructions = {}
    for stage, definition in [("PHRASE", phrase), ("EXACT", exact)]:
        row = request_ok(
            client,
            headers,
            "/api/v1/admin/templates",
            {
                "template_key": "synthetic-" + stage.lower(),
                "expected_version": 0,
                "definition": definition,
                "reason": "Synthetic covering mechanics",
            },
            expected=201,
        )
        constructions[stage] = review_and_activate(client, headers, row, example)
    constructions["TEMPLATE"] = review_and_activate(client, headers, template, example)
    for stage in ("TEMPLATE", "EXACT", "PHRASE"):
        row = constructions[stage]
        result = request_ok(client, headers, "/api/v1/translate", payload())
        assert result["status"] == "READY", result
        assert result["manifest"]["announcement"]["template_version_id"] == row["id"]
        if stage == "EXACT":
            assert len(result["manifest"]["items"]) == 1
            mismatch = request_ok(
                client,
                headers,
                "/api/v1/translate",
                payload(text="Train 00110 is arriving on platform 3."),
            )
            assert mismatch["status"] != "READY" and mismatch["manifest"] is None
        request_ok(
            client,
            headers,
            f"/api/v1/admin/templates/{row['id']}/activation",
            {
                "expected_revision": row["revision"],
                "enabled": False,
                "reason": "Exercise next approved stage",
            },
        )
    word = request_ok(client, headers, "/api/v1/translate", payload())
    assert word["status"] == "READY" and len(word["manifest"]["items"]) == 8


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_ENCODER") != "1", reason="Opt-in real E5 model")
def test_encoder_runs_without_catalog_locks_and_revalidates_changes(
    search_setup, lifecycle_clients, registry_database, tmp_path
):
    from app.retrieval.search import search
    from app.retrieval.search_schema import SearchRequest

    _, request = search_setup
    client, headers = lifecycle_clients
    settings, engine, sessions = registry_database
    paths = variant(tmp_path, "ISOLATED_ENCODER_RACE", marker="encoder-race")
    with sessions() as session:
        staged = stage_motion(*paths, "isolated", session, LocalAssetStore(settings.storage_root))
    cid, version = staged["concept_id"], staged["motion_version_id"]
    approve_fixture(client, headers, cid, version)
    switch(client, headers, cid, version)
    profile(client, headers, cid)
    encoder = LocalEncoder(
        settings.model_copy(
            update={
                "encoder_model_path": Path(__file__).resolve().parents[1] / "artifacts/e5-model"
            }
        )
    )

    class ChangingEncoder:
        state = "READY"

        def encode(self, content, kind):
            vector = encoder.encode(content, kind)
            with engine.connect() as connection, connection.begin():
                assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(1397311310,1)"))
            if kind == "passage":
                alias(client, headers, cid, "test rail vehicle")
            else:
                request_ok(
                    client,
                    headers,
                    f"/api/v1/admin/signs/{cid}/deactivate",
                    {
                        "expected_revision": history(client, headers, cid)["concept"]["revision"],
                        "reason": "Concurrent synthetic withdrawal",
                    },
                )
            return vector

    try:
        raced = index_concepts(sessions, ChangingEncoder(), [UUID(cid)], "test")["items"][0]
        assert raced["status"] == "PENDING" and raced["code"] == "STALE_INPUT"
        assert (
            index_concepts(sessions, encoder, [UUID(cid)], "test")["items"][0]["status"]
            == "INDEXED"
        )
        result = search(
            sessions,
            ChangingEncoder(),
            SearchRequest.model_validate({**request, "text": "trian", "semantic": True}),
        )
        assert all(str(c["concept_id"]) != cid for c in result["candidates"])
        before = history(client, headers, cid)["concept"]
        request_ok(
            client,
            headers,
            f"/api/v1/review/signs/{cid}/meaning",
            {
                "expected_revision": before["revision"],
                "meaning": "Revised synthetic meaning",
                "domain": "test",
                "context": "isolated exact content",
                "decision": "APPROVED",
                "evidence": "Synthetic semantic revision",
                "reason": "Revision invalidation test",
            },
            role="reviewer",
        )
        assert (
            index_concepts(sessions, encoder, [UUID(cid)], "test")["items"][0]["code"]
            == "PROFILE_REQUIRED"
        )
        profile(client, headers, cid)
        assert (
            index_concepts(sessions, encoder, [UUID(cid)], "test")["items"][0]["status"]
            == "INDEXED"
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(DISTINCT semantic_revision) FROM sign_embeddings "
                        "WHERE concept_id=:id"
                    ),
                    {"id": UUID(cid)},
                )
                == 2
            )
    finally:
        encoder.close()
