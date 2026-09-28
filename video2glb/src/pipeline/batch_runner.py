from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
import errno
import hashlib
import os
from pathlib import Path
import signal
import subprocess
from threading import Event
import time
from typing import Callable, Iterable


MAX_BATCH_WORKERS = 8
MAX_BATCH_RETRIES = 3
MAX_BATCH_TIMEOUT_SECONDS = 86_400.0


@dataclass(frozen=True)
class BatchJob:
    """One conversion command with a stable identity and log prefix."""

    key: str
    command: tuple[str, ...]
    log_prefix: str
    context_fingerprint: str | None = None


@dataclass(frozen=True)
class ProcessAttempt:
    attempt: int
    returncode: int
    timed_out: bool
    duration_seconds: float
    log_path: Path
    error: str | None = None
    failure_category: str | None = None
    retryable: bool = False
    context_fingerprint: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "log_path": str(self.log_path),
            "error": self.error,
            "failure_category": self.failure_category,
            "retryable": self.retryable,
            "context_fingerprint": self.context_fingerprint,
        }


@dataclass(frozen=True)
class BatchProcessResult:
    key: str
    attempts: tuple[ProcessAttempt, ...]

    @property
    def returncode(self) -> int:
        return self.attempts[-1].returncode if self.attempts else 1

    @property
    def timed_out(self) -> bool:
        return bool(self.attempts and self.attempts[-1].timed_out)

    @property
    def error(self) -> str | None:
        return self.attempts[-1].error if self.attempts else "Worker produced no result."


def discover_mp4_files(
    input_dir: Path,
    *,
    recursive: bool,
    excluded_roots: Iterable[Path] = (),
) -> list[Path]:
    """Return regular MP4 inputs in a repeatable, case-insensitive order."""

    if not input_dir.is_dir():
        raise ValueError(f"Batch input directory does not exist: {input_dir}")
    excluded = tuple(path.resolve() for path in excluded_roots)
    candidates = input_dir.rglob("*") if recursive else input_dir.iterdir()
    videos: list[Path] = []
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.casefold() != ".mp4":
            continue
        resolved = candidate.resolve()
        if any(_is_relative_to(resolved, root) for root in excluded):
            continue
        videos.append(resolved)
    return sorted(set(videos), key=lambda path: (str(path).casefold(), str(path)))


def assign_output_directory_names(
    videos: Iterable[Path],
    *,
    input_dir: Path,
    uppercase: bool,
) -> dict[Path, str]:
    """Preserve normal stem folders and disambiguate recursive stem collisions."""

    ordered = list(videos)
    counts: dict[str, int] = {}
    for video in ordered:
        counts[video.stem.casefold()] = counts.get(video.stem.casefold(), 0) + 1

    assigned: dict[Path, str] = {}
    used: set[str] = set()
    for video in ordered:
        base = video.stem.upper() if uppercase else video.stem
        if counts[video.stem.casefold()] > 1:
            relative = video.relative_to(input_dir).as_posix().casefold()
            suffix = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
            # Keep room for the suffix and avoid long Windows output paths.
            base = f"{base[:80]}__{suffix}"
        collision_key = base.casefold()
        if collision_key in used:
            raise ValueError(f"Batch output identity collision could not be resolved: {base}")
        used.add(collision_key)
        assigned[video] = base
    return assigned


def run_isolated_jobs(
    jobs: Iterable[BatchJob],
    *,
    cwd: Path,
    log_dir: Path,
    max_workers: int,
    timeout_seconds: float,
    retries: int,
    on_result: Callable[[BatchProcessResult], None] | None = None,
    should_pause: Callable[[], str | None] | None = None,
    on_pause: Callable[[str], None] | None = None,
    on_dispatch: Callable[[BatchJob], None] | None = None,
) -> list[BatchProcessResult]:
    """Dispatch only available slots; a pause drains active jobs and preserves pending jobs."""

    ordered = list(jobs)
    _validate_options(max_workers, timeout_seconds, retries)
    keys = [job.key for job in ordered]
    if len(set(keys)) != len(keys):
        raise ValueError("Batch job keys must be unique.")
    log_dir.mkdir(parents=True, exist_ok=True)
    if not ordered:
        return []

    by_key: dict[str, BatchProcessResult] = {}
    worker_count = min(max_workers, len(ordered))
    cancellation = Event()
    paused = False
    next_job = 0

    def pause(reason: str) -> None:
        nonlocal paused
        if not paused:
            paused = True
            if on_pause is not None:
                on_pause(reason)

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="video2glb") as executor:
        futures: dict[Future[BatchProcessResult], BatchJob] = {}
        while futures or (next_job < len(ordered) and not paused):
            try:
                reason = should_pause() if should_pause is not None and not paused else None
                if reason:
                    pause(reason)
                while not paused and next_job < len(ordered) and len(futures) < worker_count:
                    job = ordered[next_job]
                    if on_dispatch is not None:
                        on_dispatch(job)
                    futures[executor.submit(
                        _run_job, job, cwd=cwd, log_dir=log_dir,
                        timeout_seconds=timeout_seconds, retries=retries,
                        cancellation=cancellation,
                    )] = job
                    next_job += 1
                if not futures:
                    break
                completed, _ = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in completed:
                    job = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # Fail closed for an unexpected worker defect.
                        log_path = log_dir / f"{job.log_prefix}.worker-error.log"
                        log_path.write_text(f"Unexpected batch worker error: {exc}\n", encoding="utf-8")
                        result = BatchProcessResult(job.key, (ProcessAttempt(
                            attempt=1, returncode=1, timed_out=False, duration_seconds=0.0,
                            log_path=log_path, error=f"Unexpected batch worker error: {exc}",
                            failure_category="WORKER_ERROR", context_fingerprint=job.context_fingerprint,
                        ),))
                    by_key[result.key] = result
                    if on_result is not None:
                        on_result(result)
            except KeyboardInterrupt:
                cancellation.set()
                pause("INTERRUPTED: active workers stopped; pending jobs are resumable.")
    return [by_key[job.key] for job in ordered if job.key in by_key]


def _run_job(
    job: BatchJob,
    *,
    cwd: Path,
    log_dir: Path,
    timeout_seconds: float,
    retries: int,
    cancellation: Event | None = None,
) -> BatchProcessResult:
    attempts: list[ProcessAttempt] = []
    for attempt_number in range(1, retries + 2):
        log_path = log_dir / f"{job.log_prefix}.attempt-{attempt_number}.log"
        attempt = _run_command_once(
            job.command,
            cwd=cwd,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
            attempt_number=attempt_number,
            cancellation=cancellation,
        )
        attempt = replace(attempt, context_fingerprint=job.context_fingerprint)
        attempts.append(attempt)
        if attempt.returncode in {0, 2} or not attempt.retryable:
            break
        if attempt_number <= retries:
            delay = min(2 ** (attempt_number - 1), 10)
            if cancellation is not None:
                if cancellation.wait(delay):
                    break
            else:
                time.sleep(delay)
    return BatchProcessResult(key=job.key, attempts=tuple(attempts))


def _run_command_once(
    command: tuple[str, ...],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    attempt_number: int,
    cancellation: Event | None = None,
) -> ProcessAttempt:
    started = time.monotonic()
    timed_out = False
    error: str | None = None
    returncode = 1
    process: subprocess.Popen[bytes] | None = None
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["VIDEO2GLB_BATCH_ATTEMPT"] = str(attempt_number)
    launch_retryable = False
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    with log_path.open("wb") as log_stream:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=environment,
                **popen_options,
            )
            while True:
                if cancellation is not None and cancellation.is_set():
                    _terminate_process_tree(process)
                    error, returncode = "Conversion interrupted by batch controller.", 130
                    break
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    error = f"Conversion exceeded {timeout_seconds:g} seconds."
                    _terminate_process_tree(process)
                    returncode = 124
                    break
                try:
                    returncode = process.wait(timeout=min(remaining, 0.5))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (OSError, ValueError) as exc:
            error = f"Could not launch conversion process: {exc}"
            launch_retryable = isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
            }
            log_stream.write((error + "\n").encode("utf-8", errors="replace"))
        finally:
            log_stream.flush()
    category, retryable = classify_process_failure(
        int(returncode), timed_out=timed_out, log_path=log_path,
        launch_error=error is not None and error.startswith("Could not launch"),
        launch_retryable=launch_retryable,
    )
    return ProcessAttempt(
        attempt=attempt_number,
        returncode=int(returncode),
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        log_path=log_path,
        error=error,
        failure_category=category,
        retryable=retryable,
    )


def classify_process_failure(
    returncode: int, *, timed_out: bool, log_path: Path,
    launch_error: bool = False, launch_retryable: bool = False,
) -> tuple[str | None, bool]:
    """Only positively identified transient failures are worth an identical retry."""
    if timed_out:
        return "TIMEOUT", True
    if returncode == 130:
        return "INTERRUPTED", False
    if returncode in {0, 2}:
        return None, False
    if launch_error:
        return ("RESOURCE_EXHAUSTED", True) if launch_retryable else ("LAUNCH_ERROR", False)
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 65_536))
            tail = stream.read().decode("utf-8", errors="replace").casefold()
    except OSError:
        tail = ""
    if any(value in tail for value in ("context fingerprint", "pipeline context changed", "input changed")):
        return "CONTEXT_CHANGED", False
    if any(value in tail for value in ("out of memory", "memoryerror", "resource temporarily unavailable")):
        return "RESOURCE_EXHAUSTED", True
    if any(value in tail for value in ("modulenotfounderror", "no module named", "filenotfounderror")):
        return "DEPENDENCY_MISSING", False
    if any(value in tail for value in ("invalid video", "cannot open video", "no decodable frames")):
        return "INVALID_INPUT", False
    if any(value in tail for value in ("validation failed", "qc failed", "technical_qc")):
        return "TECHNICAL_QC", False
    return "CONVERSION_ERROR", False


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop the conversion and Blender descendants after an attempt timeout."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _validate_options(max_workers: int, timeout_seconds: float, retries: int) -> None:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise ValueError("Batch worker count must be an integer.")
    if not 1 <= max_workers <= MAX_BATCH_WORKERS:
        raise ValueError(f"Batch worker count must be between 1 and {MAX_BATCH_WORKERS}.")
    if not 0 < timeout_seconds <= MAX_BATCH_TIMEOUT_SECONDS:
        raise ValueError(
            f"Batch timeout must be greater than zero and at most "
            f"{MAX_BATCH_TIMEOUT_SECONDS:g} seconds."
        )
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise ValueError("Batch retry count must be an integer.")
    if not 0 <= retries <= MAX_BATCH_RETRIES:
        raise ValueError(f"Batch retries must be between 0 and {MAX_BATCH_RETRIES}.")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
