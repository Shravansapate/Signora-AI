import json
from pathlib import Path

import pytest

from src.pipeline.stage_cache import RunRecorder, StageCache


def test_cache_reuses_exact_artifacts_and_invalidates_changed_dependency(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"first")
    output = tmp_path / "pose"
    cache = StageCache(tmp_path / "cache")
    calls = []

    def action():
        calls.append(1)
        output.write_bytes(source.read_bytes() + b" processed")
        return {"frames": 5}

    kwargs = dict(inputs={"source": source}, settings={"fps": 25}, outputs={"pose": output}, action=action)
    assert cache.execute("tracking", **kwargs) == {"frames": 5}
    output.unlink()
    assert cache.execute("tracking", **kwargs) == {"frames": 5}
    assert output.read_bytes() == b"first processed"
    assert len(calls) == 1
    source.write_bytes(b"changed")
    cache.execute("tracking", **kwargs)
    assert len(calls) == 2


def test_cache_rejects_corruption_and_never_commits_failed_stage(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"source")
    output = tmp_path / "output"
    cache = StageCache(tmp_path / "cache")
    calls = []

    def action():
        calls.append(1)
        output.write_bytes(b"valid")

    kwargs = dict(inputs={"source": source}, settings={}, outputs={"pose": output}, action=action)
    cache.execute("tracking", **kwargs)
    cached = next((tmp_path / "cache").rglob("pose"))
    cached.write_bytes(b"corrupt")
    cache.execute("tracking", **kwargs)
    assert len(calls) == 2

    def failed():
        output.write_bytes(b"partial")
        raise RuntimeError("decoder failed")

    with pytest.raises(RuntimeError, match="decoder failed"):
        cache.execute("broken", **{**kwargs, "action": failed})
    assert not list((tmp_path / "cache" / "broken").rglob("manifest.json"))


def test_cache_does_not_commit_mutating_inputs(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    output = tmp_path / "output"
    cache = StageCache(tmp_path / "cache")

    def action():
        source.write_bytes(b"changed")
        output.write_bytes(b"bad")

    with pytest.raises(RuntimeError, match="Dependencies changed"):
        cache.execute("tracking", inputs={"source": source}, settings={}, outputs={"pose": output}, action=action)
    assert not list((tmp_path / "cache").rglob("manifest.json"))


def test_failed_run_retains_stage_reason_and_attempt(tmp_path):
    record = RunRecorder(tmp_path / "execution.json", run_id="a" * 32, batch_id="batch", attempt=2)

    def fail():
        raise RuntimeError("bad timing")

    with pytest.raises(RuntimeError):
        record.execute(1, "Inspect video", fail, lambda: None)
    record.finish("FAIL", error="bad timing")
    payload = json.loads(record.path.read_text())
    assert payload["failure_stage"] == "Inspect video"
    assert payload["attempt"] == 2
    assert payload["stages"][0]["status"] == "FAILED"
    assert payload["completed_at"]
