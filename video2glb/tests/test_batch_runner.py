from __future__ import annotations

from pathlib import Path
from threading import Barrier, Event, Lock
import time

import pytest

import convert
from src.pipeline import batch_runner
from src.pipeline.batch_runner import BatchJob, BatchProcessResult, ProcessAttempt


def _attempt(tmp_path: Path, *, number: int, returncode: int, timed_out: bool = False, retryable: bool = False):
    return ProcessAttempt(
        attempt=number,
        returncode=returncode,
        timed_out=timed_out,
        duration_seconds=0.01,
        log_path=tmp_path / f"attempt-{number}.log",
        retryable=retryable,
    )


def test_recursive_discovery_is_case_insensitive_and_excludes_output(tmp_path: Path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "B.MP4").write_bytes(b"b")
    (tmp_path / "nested" / "a.mp4").write_bytes(b"a")
    (tmp_path / "nested" / "ignore.txt").write_text("x", encoding="utf-8")
    (tmp_path / "output" / "debug.mp4").write_bytes(b"debug")

    videos = batch_runner.discover_mp4_files(
        tmp_path,
        recursive=True,
        excluded_roots=(tmp_path / "output",),
    )

    assert [path.name for path in videos] == ["B.MP4", "a.mp4"]


def test_duplicate_stems_get_stable_distinct_output_directories(tmp_path: Path):
    first = tmp_path / "one" / "Change.mp4"
    second = tmp_path / "two" / "Change.mp4"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    names = batch_runner.assign_output_directory_names(
        [first, second],
        input_dir=tmp_path,
        uppercase=True,
    )
    repeated = batch_runner.assign_output_directory_names(
        [first, second],
        input_dir=tmp_path,
        uppercase=True,
    )

    assert names == repeated
    assert names[first].startswith("CHANGE__")
    assert names[second].startswith("CHANGE__")
    assert names[first] != names[second]
    assert "/" not in names[first] and "\\" not in names[first]


def test_unique_flat_stem_preserves_existing_output_name(tmp_path: Path):
    video = tmp_path / "Change.mp4"
    video.write_bytes(b"video")

    names = batch_runner.assign_output_directory_names(
        [video],
        input_dir=tmp_path,
        uppercase=True,
    )

    assert names[video] == "CHANGE"


def test_transient_failed_attempt_is_retried_and_exit_two_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    returncodes = iter([1, 0, 2])

    def fake_run(_command, *, attempt_number, **_kwargs):
        return _attempt(
            tmp_path,
            number=attempt_number,
            returncode=next(returncodes),
            retryable=True,
        )

    monkeypatch.setattr(batch_runner, "_run_command_once", fake_run)
    monkeypatch.setattr(batch_runner.time, "sleep", lambda _seconds: None)
    jobs = [
        BatchJob("a", ("python", "a"), "a"),
        BatchJob("b", ("python", "b"), "b"),
    ]

    results = batch_runner.run_isolated_jobs(
        jobs,
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        max_workers=1,
        timeout_seconds=10,
        retries=2,
    )

    assert [attempt.returncode for attempt in results[0].attempts] == [1, 0]
    assert [attempt.returncode for attempt in results[1].attempts] == [2]


def test_parallel_completion_still_returns_deterministic_job_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(command, *, attempt_number, **_kwargs):
        if command[-1] == "slow":
            time.sleep(0.03)
        return _attempt(tmp_path, number=attempt_number, returncode=0)

    monkeypatch.setattr(batch_runner, "_run_command_once", fake_run)
    jobs = [
        BatchJob("slow", ("worker", "slow"), "slow"),
        BatchJob("fast", ("worker", "fast"), "fast"),
    ]
    completed: list[str] = []

    results = batch_runner.run_isolated_jobs(
        jobs,
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        max_workers=2,
        timeout_seconds=10,
        retries=0,
        on_result=lambda result: completed.append(result.key),
    )

    assert completed == ["fast", "slow"]
    assert [result.key for result in results] == ["slow", "fast"]


def test_timeout_is_recorded_as_failure_code_124(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class TimedOutProcess:
        def wait(self, timeout=None):
            raise batch_runner.subprocess.TimeoutExpired(["worker"], timeout)

    monkeypatch.setattr(batch_runner.subprocess, "Popen", lambda *_args, **_kwargs: TimedOutProcess())
    monkeypatch.setattr(batch_runner, "_terminate_process_tree", lambda _process: None)

    attempt = batch_runner._run_command_once(
        ("worker",),
        cwd=tmp_path,
        log_path=tmp_path / "timeout.log",
        timeout_seconds=0.01,
        attempt_number=1,
    )

    assert attempt.returncode == 124
    assert attempt.timed_out is True
    assert attempt.error == "Conversion exceeded 0.01 seconds."


def test_process_success_cannot_pass_without_intact_release_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    video = tmp_path / "input" / "Change.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")
    output = tmp_path / "output" / "CHANGE"
    output.mkdir(parents=True)
    source_hash = convert.sha256_file(video)
    (output / "Change.qc.json").write_text(
        '{"technical_qc":"PASS"}', encoding="utf-8"
    )
    (output / "Change.metadata.json").write_text(
        (
            '{"technical_qc":"PASS","production":{"production_eligible":true,'
            '"engineering_candidate":true},"release_evidence":{'
            f'"source_video":{{"sha256":"{source_hash}"}},'
            '"avatar":{"sha256":"avatar-hash"}}}'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(convert, "evaluate_published_release", lambda *_args, **_kwargs: (False, ["invalid release"]))
    monkeypatch.setattr(convert, "evaluate_published_engineering_candidate", lambda *_args, **_kwargs: False)
    result = BatchProcessResult(
        key="Change.mp4",
        attempts=(_attempt(tmp_path, number=1, returncode=0),),
    )

    classified = convert.classify_batch_artifact(
        video=video,
        output_dir_name="CHANGE",
        expected_source_sha256=source_hash,
        expected_avatar_sha256="avatar-hash",
        trusted_signers_path=tmp_path / "trusted.json",
        process_result=result,
    )

    assert classified["status"] == "FAIL"
    assert classified["production_eligible"] is False
    assert classified["engineering_candidate"] is False


@pytest.mark.parametrize(
    ("workers", "timeout", "retries"),
    [(0, 10.0, 0), (9, 10.0, 0), (1, 0.0, 0), (1, 86_401.0, 0), (1, 10.0, 4)],
)
def test_batch_resource_limits_are_enforced(
    tmp_path: Path,
    workers: int,
    timeout: float,
    retries: int,
):
    with pytest.raises(ValueError):
        batch_runner.run_isolated_jobs(
            [],
            cwd=tmp_path,
            log_dir=tmp_path / "logs",
            max_workers=workers,
            timeout_seconds=timeout,
            retries=retries,
        )


def test_twenty_video_batch_bounds_workers_reports_progress_and_continues_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A 20-file batch uses two slots, not 20 simultaneous conversion processes."""
    lock = Lock()
    first_workers = Barrier(2)
    active = peak_active = 0
    calls: list[tuple[str, int]] = []
    dispatched: list[str] = []
    completed: list[str] = []

    def worker(command, *, attempt_number, log_path, **_kwargs):
        nonlocal active, peak_active
        key = command[-1]
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            calls.append((key, attempt_number))
        try:
            if key in {"video-00", "video-01"}:
                first_workers.wait(timeout=5)
            time.sleep(0.005)
            if key == "video-03":
                raise RuntimeError("isolated worker defect")
            failed_qc = key in {"video-00", "video-07", "video-19"}
            return ProcessAttempt(
                attempt=attempt_number,
                returncode=1 if failed_qc else 0,
                timed_out=False,
                duration_seconds=0.005,
                log_path=log_path,
                failure_category="TECHNICAL_QC" if failed_qc else None,
                retryable=False,
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(batch_runner, "_run_command_once", worker)
    jobs = [
        BatchJob(f"video-{i:02d}", ("worker", f"video-{i:02d}"), f"video-{i:02d}", "context-hash")
        for i in range(20)
    ]
    results = batch_runner.run_isolated_jobs(
        jobs, cwd=tmp_path, log_dir=tmp_path / "logs", max_workers=2,
        timeout_seconds=10, retries=3,
        on_dispatch=lambda job: dispatched.append(job.key),
        on_result=lambda result: completed.append(result.key),
    )

    assert peak_active == 2
    assert active == 0
    assert dispatched == [job.key for job in jobs]
    assert sorted(completed) == sorted(dispatched)
    assert len(completed) == 20
    assert [result.key for result in results] == dispatched
    assert len(calls) == 20  # Neither deterministic QC failures nor worker defects are retried.
    assert all(number == 1 for _key, number in calls)
    assert [result.key for result in results if result.returncode] == [
        "video-00", "video-03", "video-07", "video-19",
    ]
    assert results[3].attempts[0].failure_category == "WORKER_ERROR"
    assert "isolated worker defect" in results[3].attempts[0].log_path.read_text(encoding="utf-8")
    assert all(result.attempts[0].context_fingerprint == "context-hash" for result in results)
    assert len({result.attempts[0].log_path for result in results}) == 20


def test_transient_retries_stop_at_configured_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    attempts: list[int] = []
    delays: list[float] = []

    def worker(_command, *, attempt_number, **_kwargs):
        attempts.append(attempt_number)
        return _attempt(tmp_path, number=attempt_number, returncode=124, timed_out=True, retryable=True)

    monkeypatch.setattr(batch_runner, "_run_command_once", worker)
    monkeypatch.setattr(batch_runner.time, "sleep", delays.append)
    result = batch_runner._run_job(
        BatchJob("video", ("worker",), "video"), cwd=tmp_path,
        log_dir=tmp_path, timeout_seconds=10, retries=3,
    )

    assert attempts == [1, 2, 3, 4]
    assert delays == [1, 2, 4]
    assert result.returncode == 124
    assert result.timed_out is True


@pytest.mark.parametrize(
    ("message", "returncode", "category", "retryable"),
    [
        ("FAIL: GLB validation failed: finger jump", 1, "TECHNICAL_QC", False),
        ('{"technical_qc": "FAIL"}', 1, "TECHNICAL_QC", False),
        ("QC failed: wrist/body collision", 1, "TECHNICAL_QC", False),
        ("No decodable frames", 1, "INVALID_INPUT", False),
        ("ModuleNotFoundError: no module named bpy", 1, "DEPENDENCY_MISSING", False),
        ("Pipeline context changed", 1, "CONTEXT_CHANGED", False),
        ("MemoryError: out of memory", 1, "RESOURCE_EXHAUSTED", True),
        ("Resource temporarily unavailable", 1, "RESOURCE_EXHAUSTED", True),
        ("Unknown conversion error", 1, "CONVERSION_ERROR", False),
        ("Review required", 2, None, False),
        ("Success", 0, None, False),
        ("Interrupted", 130, "INTERRUPTED", False),
    ],
)
def test_failure_classification_retries_only_identified_transient_errors(
    tmp_path: Path, message: str, returncode: int, category: str | None, retryable: bool,
):
    log_path = tmp_path / "worker.log"
    log_path.write_text(message, encoding="utf-8")
    assert batch_runner.classify_process_failure(
        returncode, timed_out=False, log_path=log_path,
    ) == (category, retryable)


def test_pausing_two_worker_batch_drains_active_jobs_without_dispatching_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    initial_workers = Barrier(2)
    first_completed = Event()
    dispatched: list[str] = []
    completed: list[str] = []
    pauses: list[str] = []

    def worker(command, *, attempt_number, **_kwargs):
        initial_workers.wait(timeout=5)
        if command[-1] == "1":
            assert first_completed.wait(timeout=5)
        return _attempt(tmp_path, number=attempt_number, returncode=0)

    def record_result(result):
        completed.append(result.key)
        first_completed.set()

    monkeypatch.setattr(batch_runner, "_run_command_once", worker)
    results = batch_runner.run_isolated_jobs(
        [BatchJob(str(i), ("worker", str(i)), str(i)) for i in range(20)],
        cwd=tmp_path, log_dir=tmp_path / "logs", max_workers=2,
        timeout_seconds=10, retries=0,
        on_dispatch=lambda job: dispatched.append(job.key), on_result=record_result,
        should_pause=lambda: "checkpoint requested" if completed else None,
        on_pause=pauses.append,
    )

    assert dispatched == ["0", "1"]
    assert completed == ["0", "1"]
    assert [result.key for result in results] == ["0", "1"]
    assert pauses == ["checkpoint requested"]


def test_flat_twenty_video_batch_does_not_reprocess_nested_test_copies(tmp_path: Path):
    nested = tmp_path / "previous-test-copies"
    nested.mkdir()
    for index in range(20):
        name = f"Video_{index:02d}.mp4"
        (tmp_path / name).write_bytes(b"video")
        (nested / name).write_bytes(b"previous test copy")

    videos = batch_runner.discover_mp4_files(tmp_path, recursive=False)

    assert len(videos) == 20
    assert all(video.parent == tmp_path for video in videos)
    assert [video.name for video in videos] == [f"Video_{index:02d}.mp4" for index in range(20)]
