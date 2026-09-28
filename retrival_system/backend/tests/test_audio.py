import io
import math
import os
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.asr import LocalASR
from app.audio import inspect_audio
from app.config import Settings
from app.lifecycle import LifecycleError


def stalled_worker(connection, directory):
    # Deliberate fault injection into the process protocol; no model success is simulated.
    connection.recv()


def test_stalled_model_worker_is_terminated_and_busy_lane_is_bounded(tmp_path, monkeypatch):
    import app.asr

    service = LocalASR(
        Settings(
            database_url="postgresql+psycopg://unused/unused",
            storage_root=tmp_path,
            asr_model_path=tmp_path,
        )
    )
    service.timeout = 0.2
    monkeypatch.setattr(app.asr, "serve", stalled_worker)
    stopped = []
    terminate = service._terminate

    def observed_terminate():
        process = service.process
        terminate()
        if process:
            stopped.append(process)

    monkeypatch.setattr(service, "_terminate", observed_terminate)
    try:
        service.lane.acquire()
        with pytest.raises(LifecycleError) as busy:
            service.transcribe(tmp_path / "unused.wav")
        assert busy.value.status == 429 and service.process is None
        service.lane.release()
        with pytest.raises(LifecycleError) as timeout:
            service.transcribe(tmp_path / "unused.wav")
        assert timeout.value.code == "ASR_TIMEOUT"
        assert service.state == "UNAVAILABLE" and service.process is None
        assert len(stopped) == 1 and not stopped[0].is_alive()
        assert service.lane.acquire(blocking=False)
        service.lane.release()
    finally:
        service.close()


def wav_bytes(*, rate=16000, channels=1, frames=8000, silent=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        data = b"".join(
            struct.pack("<h", 0 if silent else int(math.sin(i / 10) * 3000))
            for i in range(frames * channels)
        )
        wav.writeframes(data)
    return output.getvalue()


@pytest.mark.parametrize(
    "options,code",
    [
        ({"rate": 48000}, "AUDIO_FORMAT"),
        ({"channels": 2}, "AUDIO_FORMAT"),
        ({"frames": 3000}, "AUDIO_DURATION"),
        ({"silent": True}, "NO_SPEECH"),
        ({"frames": 960001}, "AUDIO_DURATION"),
    ],
)
def test_audio_bounds_before_model_loading(tmp_path, options, code):
    path = tmp_path / "input.wav"
    path.write_bytes(wav_bytes(**options))
    with pytest.raises(LifecycleError) as exc:
        inspect_audio(path)
    assert exc.value.code == code


def test_audio_integrity_and_complete_recording(tmp_path):
    path = tmp_path / "input.wav"
    data = wav_bytes()
    path.write_bytes(data)
    assert inspect_audio(path)["duration_seconds"] == 0.5
    path.write_bytes(data[:-2])
    with pytest.raises(LifecycleError, match="complete PCM"):
        inspect_audio(path)


def test_unconfigured_asr_is_explicit_and_closes_cleanly(tmp_path):
    service = LocalASR(
        Settings(database_url="postgresql+psycopg://unused/unused", storage_root=tmp_path)
    )
    try:
        with pytest.raises(LifecycleError) as exc:
            service.transcribe(tmp_path / "unused.wav")
        assert exc.value.code == "ASR_UNAVAILABLE"
        assert service.process is None
    finally:
        service.close()


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_ASR") != "1", reason="Opt-in actual local ASR")
def test_real_local_speech_model_finalizes_recording_and_reuses_worker(tmp_path):
    import json
    import time

    root = Path(__file__).resolve().parents[2]
    audio = root / "artifacts/phase-4-synthetic-announcement.wav"
    settings = Settings(
        database_url="postgresql+psycopg://unused/unused",
        storage_root=tmp_path,
        asr_model_path=root / "backend/artifacts/asr-model",
    )
    inspected = inspect_audio(audio)
    service = LocalASR(settings)
    began = time.monotonic()
    try:
        result = service.transcribe(audio)
        cold_seconds = time.monotonic() - began
        assert result["status"] == "FINAL" and result["metadata"]["final"] is True
        assert "train" in result["text"].lower() and "platform" in result["text"].lower()
        assert result["metadata"]["confirmation_required"] is True
        worker = service.process.pid
        began = time.monotonic()
        repeat = service.transcribe(audio)
        assert repeat["text"] == result["text"] and service.process.pid == worker
        report = {
            "status": "PASSED",
            "verified_at": datetime.now(UTC).isoformat(),
            "input": "Synthetic Windows speech fixture; not field-recorded station audio",
            "transcript": result["text"],
            "model_id": result["metadata"]["model_id"],
            "model_revision": result["metadata"]["model_revision"],
            "audio": inspected,
            "cold_seconds": round(cold_seconds, 2),
            "warm_seconds": round(time.monotonic() - began, 2),
            "worker_reused": True,
            "confirmation_required": True,
        }
        (root / "artifacts/phase-4-asr-verification.json").write_text(json.dumps(report, indent=2))
    finally:
        service.close()
    assert service.process is None
