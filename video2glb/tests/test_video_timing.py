from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import numpy as np
import pytest

from src.video import inspector
from src.tracking.holistic_tracker import _validated_video_frames


class Capture:
    def __init__(self, timestamps=(0.0, 0.04, 0.08), *, fps=25.0, reported=None):
        self.timestamps = timestamps
        self.fps = fps
        self.reported = len(timestamps) if reported is None else reported
        self.index = 0
        self.released = False

    def isOpened(self):
        return True

    def get(self, key):
        return {inspector.cv2.CAP_PROP_FPS: self.fps,
                inspector.cv2.CAP_PROP_FRAME_COUNT: self.reported,
                inspector.cv2.CAP_PROP_POS_MSEC: self.timestamps[max(0, self.index - 1)] * 1000,
                inspector.cv2.CAP_PROP_FOURCC: 0}.get(key, 0)

    def read(self):
        if self.index >= len(self.timestamps):
            return False, None
        self.index += 1
        return True, np.zeros((24, 32, 3), dtype=np.uint8)

    def release(self):
        self.released = True


def _inspect(monkeypatch, capture, probe=None):
    monkeypatch.setattr(inspector.cv2, "VideoCapture", lambda _: capture)
    return inspector._inspect_with_opencv(Path("source.mp4"), probe)


def test_actual_decode_count_overrides_opencv_estimate(monkeypatch):
    cap = Capture(reported=600)
    info = _inspect(monkeypatch, cap)
    assert info.frame_count == 3
    assert info.reported_frame_count == 600
    assert info.timestamps_ms == [0, 40, 80]
    assert cap.released


def test_broken_ffprobe_executable_falls_back_to_decoded_opencv(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    video.touch()
    monkeypatch.setattr(inspector.shutil, "which", lambda _: "broken-ffprobe.exe")
    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("bad executable")))
    monkeypatch.setattr(inspector.cv2, "VideoCapture", lambda _: Capture())
    info = inspector.inspect_video(video)
    assert info.inspector == "opencv_decode"
    assert info.frame_count == 3


def test_ffprobe_decode_error_is_not_hidden_by_fallback(monkeypatch):
    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "ffprobe", stderr="invalid packet")))
    with pytest.raises(RuntimeError, match="invalid packet"):
        inspector._inspect_with_ffprobe(Path("corrupt.mp4"), "ffprobe")


def test_malformed_ffprobe_stream_is_rejected(monkeypatch):
    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='{"streams":[42]}', stderr=""))
    with pytest.raises(RuntimeError, match="malformed stream"):
        inspector._inspect_with_ffprobe(Path("corrupt.mp4"), "ffprobe")


def test_ffprobe_and_opencv_decode_disagreement_fails(monkeypatch):
    probe = {"streams": [{"avg_frame_rate": "25/1"}],
             "frames": [{"pts_time": str(index / 25)} for index in range(4)]}
    with pytest.raises(RuntimeError, match="decode frame mismatch"):
        _inspect(monkeypatch, Capture(), probe)


def test_declared_frame_count_detects_truncated_input(monkeypatch):
    probe = {"streams": [{"avg_frame_rate": "25/1", "nb_frames": "4"}],
             "frames": [{"pts_time": str(index / 25)} for index in range(3)]}
    with pytest.raises(RuntimeError, match="Incomplete video decode"):
        _inspect(monkeypatch, Capture(), probe)


def test_declared_frame_count_tolerates_single_frame_container_rounding(monkeypatch):
    probe = {"streams": [{"avg_frame_rate": "25/1", "nb_frames": "100"}],
             "frames": [{"pts_time": str(index / 25)} for index in range(99)]}
    info = _inspect(monkeypatch, Capture([index / 25 for index in range(99)]), probe)
    assert info.frame_count == 99
    assert info.reported_frame_count == 100


@pytest.mark.parametrize("fps", [float("nan"), float("inf"), 0.0, -25.0])
def test_invalid_fps_rejected(monkeypatch, fps):
    with pytest.raises(RuntimeError, match="FPS"):
        _inspect(monkeypatch, Capture(fps=fps))


@pytest.mark.parametrize("timestamps", [(0.0, 0.0), (0.0, float("nan")), (0.04, 0.02)])
def test_invalid_presentation_timestamps_rejected(monkeypatch, timestamps):
    with pytest.raises(RuntimeError, match="timestamps"):
        _inspect(monkeypatch, Capture(timestamps))


def test_fractional_cfr_preserves_rate_and_actual_timestamps(monkeypatch):
    fps = 30000 / 1001
    timestamps = [index / fps for index in range(100)]
    probe = {"streams": [{"avg_frame_rate": "30000/1001"}],
             "frames": [{"pts_time": f"{value:.6f}"} for value in timestamps]}
    info = _inspect(monkeypatch, Capture(timestamps, fps=fps), probe)
    assert info.fps == pytest.approx(fps)
    assert info.fps_rational == "30000/1001"
    assert not info.variable_frame_rate
    assert info.timestamps_ms[-1] == round(timestamps[-1] * 1000)


def test_vfr_is_detected_from_decoded_pts_not_container_fps(monkeypatch):
    info = _inspect(monkeypatch, Capture((0.0, 0.04, 0.12, 0.16)))
    assert info.variable_frame_rate
    assert info.fps == pytest.approx(25)


def test_cfr_preparation_does_not_transcode_or_write(monkeypatch, tmp_path):
    info = _inspect(monkeypatch, Capture())
    monkeypatch.setattr(inspector, "inspect_video", lambda _: info)
    directory = tmp_path / "working"
    prepared = inspector.prepare_video("source.mp4", directory)
    assert prepared.working_path == Path("source.mp4")
    assert not directory.exists()
    assert not prepared.normalized
    assert [row["source_frame"] for row in prepared.timing_mapping] == [0, 1, 2]


def test_vfr_requires_ffmpeg(monkeypatch, tmp_path):
    info = _inspect(monkeypatch, Capture((0.0, 0.04, 0.12, 0.16)))
    monkeypatch.setattr(inspector, "inspect_video", lambda _: info)
    monkeypatch.setattr(inspector.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="requires a working FFmpeg"):
        inspector.prepare_video("source.mp4", tmp_path)


def test_vfr_preparation_records_duplicated_source_frame_mapping(monkeypatch, tmp_path):
    source = _inspect(monkeypatch, Capture((0.0, 0.04, 0.12, 0.16)))
    working = _inspect(monkeypatch, Capture((0.0, 0.04, 0.08, 0.12, 0.16)))
    infos = iter([source, working])
    monkeypatch.setattr(inspector, "inspect_video", lambda _: next(infos))
    monkeypatch.setattr(inspector.shutil, "which", lambda _: "ffmpeg")
    commands = []
    monkeypatch.setattr(inspector.subprocess, "run", lambda command, **_: commands.append(command))
    prepared = inspector.prepare_video("source.mp4", tmp_path)
    assert prepared.normalized
    assert [row["source_frame"] for row in prepared.timing_mapping] == [0, 1, 1, 2, 3]
    assert "ffv1" in commands[0]
    assert "fps=fps=25:start_time=0:round=near" in commands[0][commands[0].index("-vf") + 1]
    json.dumps(prepared.to_json_dict(), allow_nan=False)


@pytest.mark.parametrize("count", [2, 4])
def test_tracking_cannot_silently_accept_one_missing_or_extra_frame(monkeypatch, count):
    info = _inspect(monkeypatch, Capture())
    with pytest.raises(RuntimeError, match="tracking decode|more than"):
        list(_validated_video_frames(Capture([index / 25 for index in range(count)]), info))


def test_tracking_uses_inspected_timestamps(monkeypatch):
    info = _inspect(monkeypatch, Capture((0.0, 0.0404, 0.0806)))
    frames = list(_validated_video_frames(Capture(), info))
    assert [timestamp for _, timestamp, _ in frames] == [0, 40, 81]


@pytest.mark.parametrize("value", ["nan", "inf", "0/0", "a/b", "1/0", "-25/1", None])
def test_malformed_fps_metadata_never_raises_or_returns_nonfinite(value):
    assert inspector._parse_fps(value) is None
