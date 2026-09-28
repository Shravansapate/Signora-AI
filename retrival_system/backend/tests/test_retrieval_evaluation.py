"""English engineering calibration, separate held-out phrasing, and real SQL latency.

These labels evaluate candidate assistance, not ISL correctness or pilot comprehension.
No fixture is inserted into the approved catalog. The fixed split is intent-family based;
the held-out critical contrast suite also uses distinct values and phrasing.
"""

import hashlib
import json
import math
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from app.config import Settings
from app.retrieval.encoder import LocalEncoder
from app.retrieval.encoder_model import ENCODING_POLICY, MODEL_REVISION
from app.retrieval.search_policy import AMBIGUITY_MARGIN, FUZZY_MIN, SEMANTIC_MIN

# Development families and held-out families are disjoint, not numeric substitutions.
CORPUS = [
    ("train", "A railway vehicle that carries passengers on tracks."),
    ("platform", "The area where passengers board a train at a railway station."),
    ("ticket", "A travel document permitting a passenger to take a journey."),
    ("delay", "A service running later than its scheduled time."),
    ("exit", "The way out of a building or railway station."),
    ("help", "Assistance for a passenger who needs support."),
    ("arrival", "A train reaching the station."),
    ("departure", "A train leaving the station."),
]
DEVELOPMENT = {
    "fuzzy": [
        ("traiin", "train"),
        ("trian", "train"),
        ("platfrm", "platform"),
        ("tickt", "ticket"),
        ("delai", "delay"),
    ],
    "semantic": [
        ("railway vehicle", "train"),
        ("boarding area", "platform"),
        ("travel pass", "ticket"),
        ("service running late", "delay"),
    ],
}
HELD_OUT = {
    "fuzzy": [
        ("exiit", "exit"),
        ("hellp", "help"),
        ("arival", "arrival"),
        ("departur", "departure"),
    ],
    "semantic": [
        ("way out", "exit"),
        ("passenger assistance", "help"),
        ("train reaching the station", "arrival"),
        ("train leaving the station", "departure"),
    ],
}
UNSUPPORTED = ["chocolate cake recipe", "volcanic rock formation", "galaxy telescope"]


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_ENCODER") != "1", reason="Opt-in real E5 model")
def test_real_encoder_calibration_and_held_out_candidates(registry_database):  # noqa: F811
    settings, engine, _ = registry_database
    root = Path(__file__).resolve().parents[1]
    encoder = LocalEncoder(
        Settings(
            database_url=settings.database_url,
            storage_root=settings.storage_root,
            encoder_model_path=root / "artifacts/e5-model",
        )
    )
    timings = []
    try:
        vectors = []
        for canonical, description in CORPUS:
            content = json.dumps(
                {
                    "canonical": canonical,
                    "semantic_revision": 1,
                    "description": description,
                    "domain": "railway",
                    "context": "passenger information",
                    "aliases": [],
                    "sense": {
                        "semantic_class": "RAILWAY_INFORMATION",
                        "polarity": "NEUTRAL",
                        "temporal_state": "NONE",
                        "literals": {},
                    },
                },
                sort_keys=True,
            )
            vectors.append(encoder.encode(content, "passage"))
        values = ",".join(f"(:name{i},CAST(:v{i} AS vector))" for i in range(len(CORPUS)))
        params = {f"name{i}": c[0] for i, c in enumerate(CORPUS)}
        params.update({f"v{i}": json.dumps(v) for i, v in enumerate(vectors)})

        def rank(query, stage):
            parameters = {**params, "query": query}
            if stage == "semantic":
                parameters["vector"] = json.dumps(encoder.encode(query, "query"))
                expression = "1-(embedding <=> CAST(:vector AS vector))"
            else:
                expression = "similarity(canonical,:query)"
            started = time.perf_counter_ns()
            with engine.connect() as connection:
                rows = (
                    connection.execute(
                        text(
                            f"SELECT canonical,{expression} AS score FROM (VALUES {values}) "
                            "AS c(canonical,embedding) ORDER BY score DESC,canonical"
                        ),
                        parameters,
                    )
                    .mappings()
                    .all()
                )
            timings.append((time.perf_counter_ns() - started) / 1e6)
            return [dict(row) for row in rows]

        development, cutoffs = {}, {}
        for stage, cases in DEVELOPMENT.items():
            development[stage] = [
                {"query": q, "target": target, "ranked": rank(q, stage)} for q, target in cases
            ]
            positive_scores = [
                next(r["score"] for r in case["ranked"] if r["canonical"] == case["target"])
                for case in development[stage]
            ]
            # Largest hundredth retaining every development target; no tuning on held-out data.
            cutoffs[stage] = math.floor(min(positive_scores) * 100) / 100
        held_out = {}
        for stage, cases in HELD_OUT.items():
            scored = [
                {"query": q, "target": target, "ranked": rank(q, stage)} for q, target in cases
            ]
            recall = sum(
                any(
                    r["canonical"] == case["target"] and r["score"] >= cutoffs[stage]
                    for r in case["ranked"][:3]
                )
                for case in scored
            ) / len(scored)
            held_out[stage] = {"candidate_recall_at_3": recall, "cases": scored}
        unsupported = {
            stage: [
                {
                    "query": q,
                    "suggested": [r for r in rank(q, stage) if r["score"] >= cutoffs[stage]],
                }
                for q in UNSUPPORTED
            ]
            for stage in ("fuzzy", "semantic")
        }
        ambiguous = [rank(query, "semantic") for query in ("railway station", "travel information")]
        calibrated_margin = (
            math.ceil(max(rows[0]["score"] - rows[1]["score"] for rows in ambiguous) * 100) / 100
        )
        # Scan-size benchmark is explicitly synthetic, separate from linguistic evidence.
        with engine.connect() as connection, connection.begin():
            connection.execute(
                text(
                    "CREATE TEMP TABLE benchmark_vectors (id int, embedding vector(384)) "
                    "ON COMMIT DROP"
                )
            )
            connection.execute(
                text("INSERT INTO benchmark_vectors VALUES (:id,CAST(:v AS vector))"),
                [{"id": i, "v": json.dumps(vectors[i % len(vectors)])} for i in range(149)],
            )
            plan = connection.execute(
                text(
                    "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM benchmark_vectors "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 12"
                ),
                {"q": json.dumps(vectors[0])},
            ).scalar_one()
        report = {
            "scope": "Synthetic English candidate assistance; no operational approval",
            "model_revision": MODEL_REVISION,
            "encoding_policy": ENCODING_POLICY,
            "fixture_sha256": hashlib.sha256(
                json.dumps([CORPUS, DEVELOPMENT, HELD_OUT], sort_keys=True).encode()
            ).hexdigest(),
            "development": development,
            "calibrated_cutoffs": cutoffs,
            "ambiguity_policy": {
                "margin": AMBIGUITY_MARGIN,
                "development_margin": calibrated_margin,
                "development_rankings": ambiguous,
                "action": "review all approximate candidates",
            },
            "held_out": held_out,
            "unsupported": unsupported,
            "automatic_approximate_acceptance": False,
            "scan_benchmark": {
                "rows": 149,
                "scope": "Synthetic repeated vectors, not catalog approvals",
                "plan": plan,
            },
            "sql_latency_ms": {
                "count": len(timings),
                "p50": sorted(timings)[len(timings) // 2],
                "p95": sorted(timings)[int(len(timings) * 0.95)],
            },
        }
        (root / "artifacts/phase-5-retrieval-evaluation.json").write_text(
            json.dumps(report, indent=2)
        )
        assert FUZZY_MIN == cutoffs["fuzzy"] and SEMANTIC_MIN == cutoffs["semantic"], cutoffs
        assert AMBIGUITY_MARGIN == calibrated_margin, calibrated_margin
        assert all(stage["candidate_recall_at_3"] == 1 for stage in held_out.values()), held_out
    finally:
        encoder.close()
