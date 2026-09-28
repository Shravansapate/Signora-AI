from __future__ import annotations

from pathlib import Path

import pytest

from src.qc import source_avatar_comparison as comparison


class _FakeCapture:
    def __init__(self, *, reported_frames: int, decodable_frames: int, fps: float = 25.0):
        self.reported_frames = reported_frames
        self.remaining = decodable_frames
        self.decoded = 0
        self.fps = fps
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, property_id: int) -> float:
        values = {
            comparison.cv2.CAP_PROP_FPS: self.fps,
            comparison.cv2.CAP_PROP_FRAME_COUNT: self.reported_frames,
            comparison.cv2.CAP_PROP_FRAME_WIDTH: 64,
            comparison.cv2.CAP_PROP_FRAME_HEIGHT: 64,
            comparison.cv2.CAP_PROP_POS_MSEC: max(0, self.decoded - 1) / self.fps * 1000.0,
        }
        return float(values.get(property_id, 0.0))

    def read(self):
        if self.remaining <= 0:
            return False, None
        self.remaining -= 1
        self.decoded += 1
        return True, object()

    def release(self) -> None:
        self.released = True


class _FakeWriter:
    def __init__(self):
        self.write_calls = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def write(self, _frame) -> None:
        self.write_calls += 1

    def release(self) -> None:
        self.released = True


def _install_video_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_reported: int,
    source_decodable: int,
    avatar_reported: int,
    avatar_decodable: int,
    reopened_decodable: int,
) -> _FakeWriter:
    captures = iter(
        [
            _FakeCapture(
                reported_frames=source_reported,
                decodable_frames=source_decodable,
            ),
            _FakeCapture(
                reported_frames=avatar_reported,
                decodable_frames=avatar_decodable,
            ),
            _FakeCapture(
                reported_frames=reopened_decodable,
                decodable_frames=reopened_decodable,
            ),
        ]
    )
    writer = _FakeWriter()
    monkeypatch.setattr(comparison.cv2, "VideoCapture", lambda _path: next(captures))
    monkeypatch.setattr(comparison.cv2, "VideoWriter", lambda *_args: writer)
    monkeypatch.setattr(comparison.cv2, "VideoWriter_fourcc", lambda *_args: 0)
    monkeypatch.setattr(comparison.cv2, "resize", lambda frame, *_args, **_kwargs: frame)
    monkeypatch.setattr(comparison.cv2, "hconcat", lambda frames: frames)
    return writer


def _run(tmp_path: Path) -> dict:
    return comparison.create_source_avatar_comparison(
        tmp_path / "source.mp4",
        tmp_path / "avatar.mp4",
        tmp_path / "comparison.mp4",
    )


def test_complete_written_and_reopened_comparison_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _install_video_fakes(
        monkeypatch,
        source_reported=3,
        source_decodable=3,
        avatar_reported=3,
        avatar_decodable=3,
        reopened_decodable=3,
    )

    report = _run(tmp_path)

    assert report["status"] == "PASS"
    assert report["expected_comparison_frame_count"] == 3
    assert report["comparison_frame_count"] == 3
    assert report["comparison_reopened_frame_count"] == 3
    assert report["comparison_complete"] is True
    assert report["reasons"] == []


def test_early_input_read_cannot_report_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _install_video_fakes(
        monkeypatch,
        source_reported=3,
        source_decodable=2,
        avatar_reported=3,
        avatar_decodable=3,
        reopened_decodable=2,
    )

    report = _run(tmp_path)

    assert report["status"] == "REVIEW"
    assert report["comparison_frame_count"] == 2
    assert report["comparison_complete"] is False
    assert "INCOMPLETE_COMPARISON_WRITE" in report["reasons"]
    assert "INCOMPLETE_COMPARISON_REOPEN" in report["reasons"]


def test_truncated_saved_video_cannot_report_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _install_video_fakes(
        monkeypatch,
        source_reported=3,
        source_decodable=3,
        avatar_reported=3,
        avatar_decodable=3,
        reopened_decodable=2,
    )

    report = _run(tmp_path)

    assert report["status"] == "REVIEW"
    assert report["comparison_frame_count"] == 3
    assert report["comparison_reopened_frame_count"] == 2
    assert report["comparison_complete"] is False
    assert report["reasons"] == ["INCOMPLETE_COMPARISON_REOPEN"]


def test_zero_frame_comparison_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _install_video_fakes(
        monkeypatch,
        source_reported=0,
        source_decodable=0,
        avatar_reported=0,
        avatar_decodable=0,
        reopened_decodable=0,
    )

    report = _run(tmp_path)

    assert report["status"] == "FAIL"
    assert report["comparison_complete"] is False
    assert "NO_EXPECTED_COMPARISON_FRAMES" in report["reasons"]


def test_single_extra_avatar_frame_is_not_a_pass(tmp_path, monkeypatch):
    _install_video_fakes(monkeypatch, source_reported=3, source_decodable=3,
                         avatar_reported=4, avatar_decodable=4, reopened_decodable=3)
    report = _run(tmp_path)
    assert report["status"] != "PASS"
    assert "SOURCE_AVATAR_FRAME_COUNT_MISMATCH" in report["reasons"]
    assert report["comparison_complete"] is False


def test_matching_counts_with_wrong_presentation_timestamps_do_not_pass(tmp_path, monkeypatch):
    _install_video_fakes(monkeypatch, source_reported=3, source_decodable=3,
                         avatar_reported=3, avatar_decodable=3, reopened_decodable=3)
    original = _FakeCapture.get
    monkeypatch.setattr(_FakeCapture, "get", lambda self, prop: 0.0 if prop == comparison.cv2.CAP_PROP_POS_MSEC else original(self, prop))
    report = _run(tmp_path)
    assert report["status"] != "PASS"
    assert "SOURCE_WORKING_TIMELINE_MISMATCH" in report["reasons"]
