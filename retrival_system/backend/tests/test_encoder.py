"""Real pinned encoder verification is opt-in; no fake vector success path."""

import json
import math
import os
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.lifecycle import LifecycleError
from app.retrieval.encoder import LocalEncoder
from app.retrieval.encoder_model import ENCODING_POLICY, MODEL_REVISION


def stalled_encoder(connection, directory):
    time.sleep(30)


def test_stalled_encoder_is_killed_and_corrupt_snapshot_fails_closed(tmp_path, monkeypatch):
    import app.retrieval.encoder

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/db",
        storage_root=tmp_path,
        encoder_model_path=tmp_path,
    )
    service = LocalEncoder(settings)
    service.timeout = 0.2
    with monkeypatch.context() as patch:
        patch.setattr(app.retrieval.encoder, "worker", stalled_encoder)
        processes = []
        terminate = service._terminate

        def observed_terminate():
            if service.process:
                processes.append(service.process)
            terminate()

        patch.setattr(service, "_terminate", observed_terminate)
        with pytest.raises(LifecycleError) as failure:
            service.encode("train", "query")
        assert failure.value.code == "ENCODER_TIMEOUT"
        assert processes and all(not p.is_alive() for p in processes)
    service.close()
    service = LocalEncoder(settings)
    try:
        with pytest.raises(LifecycleError) as failure:
            service.encode("train", "query")
        assert failure.value.code == "ENCODER_UNAVAILABLE"
        assert service.process is None and not service.cache
    finally:
        service.close()


def test_unconfigured_encoder_and_busy_lane_fail_explicitly(tmp_path):
    encoder = LocalEncoder(
        Settings(
            _env_file=None, database_url="postgresql+psycopg://unused/db", storage_root=tmp_path
        )
    )
    try:
        with pytest.raises(LifecycleError, match="not configured"):
            encoder.encode("train", "query")
        encoder.lane.acquire()
        try:
            with pytest.raises(LifecycleError) as busy:
                encoder.encode("train", "query")
            assert busy.value.status == 429
        finally:
            encoder.lane.release()
    finally:
        encoder.close()


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_ENCODER") != "1", reason="Opt-in real E5 encoder")
def test_real_e5_dimensions_prefix_pooling_cache_and_process_reuse(tmp_path):
    root = Path(__file__).resolve().parents[2]
    encoder = LocalEncoder(
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://unused/db",
            storage_root=tmp_path,
            encoder_model_path=root / "backend/artifacts/e5-model",
        )
    )
    start = time.monotonic()
    try:
        query = encoder.encode("The train is arriving", "query")
        cold = time.monotonic() - start
        assert len(query) == 384 and math.isclose(sum(v * v for v in query), 1, abs_tol=1e-5)
        pid = encoder.process.pid
        start = time.monotonic()
        passage = encoder.encode("The train is arriving", "passage")
        warm = time.monotonic() - start
        assert query != passage and encoder.process.pid == pid
        original = list(query)
        query[0] = 999
        assert encoder.encode("The train is arriving", "query") == original
        assert all(math.isfinite(v) for v in passage)
        (tmp_path / "encoder-verification.json").write_text(
            json.dumps(
                {
                    "status": "PASSED",
                    "model_revision": MODEL_REVISION,
                    "policy": ENCODING_POLICY,
                    "dimensions": 384,
                    "cold_seconds": cold,
                    "warm_seconds": warm,
                    "same_process": True,
                    "query_passage_cosine": sum(
                        a * b for a, b in zip(original, passage, strict=True)
                    ),
                },
                indent=2,
            )
        )
    finally:
        encoder.close()
    assert encoder.process is None
