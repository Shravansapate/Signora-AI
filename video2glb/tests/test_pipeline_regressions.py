from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import convert
from src.pipeline import batch_runner
from src.pipeline.batch_runner import BatchJob, BatchProcessResult, ProcessAttempt
from src.qc.animation_channels import evaluate_required_rotation_channels
from src.qc.motion_stability import evaluate_direction_continuity
from src.qc.neutral_shape import evaluate_neutral_finger_shape
from src.motion.neutral_hand import anatomical_palm_normal
from src.tracking.holistic_tracker import _validated_video_frames
from src.tracking.pose_schema import PoseFrame, PoseSequence
from src.tracking.tracking_qc import assess_hand_assignment
from src.video import inspector


def test_batch_failure_summary_reports_worker_qc_cause_not_missing_approval(tmp_path):
    log = tmp_path / "attempt.log"
    log.write_text("diagnostic noise\nFAIL: GLB validation failed: finger jump at frame 18\n", encoding="utf-8")
    assert convert.batch_failure_log_summary(log) == "FAIL: GLB validation failed: finger jump at frame 18"
    assert convert.batch_failure_log_summary(tmp_path / "missing.log") is None
    log.write_text("FAIL: " + "x" * 4000, encoding="utf-8")
    assert len(convert.batch_failure_log_summary(log)) == 2000


def test_paused_batch_does_not_dispatch_remaining_jobs(tmp_path, monkeypatch):
    dispatched, completed, pauses = [], [], []

    def worker(command, *, attempt_number, **kwargs):
        return ProcessAttempt(attempt_number, 0, False, 0.01, tmp_path / "worker.log")

    monkeypatch.setattr(batch_runner, "_run_command_once", worker)
    results = batch_runner.run_isolated_jobs(
        [BatchJob(str(i), ("worker",), str(i)) for i in range(5)],
        cwd=tmp_path, log_dir=tmp_path / "logs", max_workers=1, timeout_seconds=5, retries=0,
        should_pause=lambda: "test checkpoint" if completed else None,
        on_result=lambda result: completed.append(result.key),
        on_pause=pauses.append, on_dispatch=lambda job: dispatched.append(job.key),
    )
    assert dispatched == completed == ["0"]
    assert len(results) == 1
    assert pauses == ["test checkpoint"]


def test_deterministic_quality_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def worker(command, *, attempt_number, **kwargs):
        calls.append(attempt_number)
        return ProcessAttempt(attempt_number, 1, False, 0.01, tmp_path / "qc.log", failure_category="TECHNICAL_QC")

    monkeypatch.setattr(batch_runner, "_run_command_once", worker)
    batch_runner.run_isolated_jobs([BatchJob("video", ("worker",), "video")],
        cwd=tmp_path, log_dir=tmp_path / "logs", max_workers=1, timeout_seconds=5, retries=3)
    assert calls == [1]


def test_source_identity_and_resume_survive_folder_move():
    kwargs = {"source_sha256": "a" * 64, "output_dir_name": "ARRIVE", "context_fingerprint": "b" * 64}
    assert convert.batch_resume_fingerprint(relative_video="batch1/Arrive.mp4", **kwargs) == convert.batch_resume_fingerprint(relative_video="batch2/Arrive.mp4", **kwargs)
    assert convert.source_identity("a" * 64, "Arrive") != convert.source_identity("b" * 64, "Arrive")


def test_duplicate_filename_across_batches_cannot_overwrite_existing_output(tmp_path):
    first = convert.source_identity("a" * 64, "Arrive")
    records = {first: {"output_dir_name": "ARRIVE"}}
    second = convert.source_identity("b" * 64, "Arrive")
    assert convert.choose_output_directory(identity=second, artifact_stem="Arrive", source_sha256="b" * 64,
        uppercase=True, records=records, output_root=tmp_path) == "ARRIVE__" + second[:16]


def test_batch_persistent_records_resume_between_input_folders(tmp_path, monkeypatch):
    input_dir = tmp_path / "batch1"
    input_dir.mkdir()
    video = input_dir / "Arrive.mp4"
    video.write_bytes(b"source video")
    avatar = tmp_path / "avatar.fbx"
    avatar.write_bytes(b"avatar")
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    config = tmp_path / "settings.yaml"
    config.write_text(f'avatar:\n  path: "{avatar.as_posix()}"\npose:\n  model_path: "{model.as_posix()}"\n', encoding="utf-8")
    qc = tmp_path / "qc.yaml"
    qc.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(convert, "evaluate_published_release", lambda *a, **k: (False, ["pending review"]))
    monkeypatch.setattr(convert, "evaluate_published_engineering_candidate", lambda *a, **k: True)
    # This test isolates index/folder resume; artifact verification has separate tests.
    monkeypatch.setattr(convert, "attach_batch_review", lambda item, *a, **k:
                        item.update(review_available=True, review_status="PENDING"))
    monkeypatch.setattr(convert, "verify_completed_batch_item", lambda item, checkpoint, plan: {
        "original_batch_id": checkpoint["batch_id"], "run_id": "a" * 32,
        "preserved_artifact": {"path": "fixture.glb", "sha256": "b" * 64},
    })
    dispatch_counts = []

    def fake_runner(jobs, **kwargs):
        dispatch_counts.append(len(jobs))
        results = []
        for job in jobs:
            assert kwargs["should_pause"]() is None
            kwargs["on_dispatch"](job)
            source = Path(job.command[job.command.index("--video") + 1])
            name = job.command[job.command.index("--output-dir-name") + 1]
            output = tmp_path / "output" / name
            output.mkdir(parents=True, exist_ok=True)
            (output / "Arrive.qc.json").write_text('{"technical_qc":"PASS"}', encoding="utf-8")
            (output / "Arrive.metadata.json").write_text(json.dumps({
                "technical_qc": "PASS", "production": {"engineering_candidate": True},
                "release_evidence": {"source_video": {"sha256": convert.sha256_file(source)},
                                     "avatar": {"sha256": convert.sha256_file(avatar)}}}), encoding="utf-8")
            result = BatchProcessResult(job.key, (ProcessAttempt(1, 0, False, 0.01, tmp_path / "log"),))
            kwargs["on_result"](result)
            results.append(result)
        return results

    monkeypatch.setattr(convert, "run_isolated_jobs", fake_runner)
    args = SimpleNamespace(input_dir=str(input_dir), config=str(config), qc_thresholds=str(qc), motion_catalog=None)
    assert convert.run_batch(args) == 0
    second_dir = tmp_path / "batch2"
    second_dir.mkdir()
    video.rename(second_dir / video.name)
    args.input_dir = str(second_dir)
    assert convert.run_batch(args) == 0
    assert dispatch_counts == [1, 0]
    assert len(list((tmp_path / "output" / "batch_runs").glob("*/summary.json"))) == 2
    latest = json.loads((tmp_path / "output" / "batch_summary.json").read_text())
    assert latest["skipped"] == 1


@pytest.fixture
def interrupted_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    batch_id = "a" * 32
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    avatar = tmp_path / "avatar.fbx"
    avatar.write_bytes(b"avatar")
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    config = tmp_path / "settings.yaml"
    config.write_text(f'avatar:\n  path: "{avatar.as_posix()}"\npose:\n  model_path: "{model.as_posix()}"\n', encoding="utf-8")
    qc = tmp_path / "qc.yaml"
    qc.write_text("{}", encoding="utf-8")

    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def evidence(path):
        return {"path": str(path), "sha256": convert.sha256_file(path), "size_bytes": path.stat().st_size}

    items, records, artifacts = [], {}, {}
    for index, (stem, disposition, status) in enumerate([
        ("Berth", "PROCESSED", "REVIEW"), ("Coach", "PROCESSED", "FAIL"),
        ("Arrive", "RUNNING", "PENDING"), ("Destination", "PENDING", "PENDING"),
    ], start=1):
        source = input_dir / f"{stem}.mp4"
        source.write_bytes(stem.encode())
        source_hash = convert.sha256_file(source)
        source_id = convert.source_identity(source_hash, stem)
        output = tmp_path / "output" / stem.upper()
        records[source_id] = {"output_dir_name": stem.upper()}
        item = {
            "video": str(source), "relative_video": source.name,
            "source_id": source_id, "source_sha256": source_hash,
            "output_dir": f"output/{stem.upper()}", "disposition": disposition,
            "status": status, "production_eligible": False, "engineering_candidate": False,
            "production_status": "NOT_ELIGIBLE", "resume_fingerprint": "old-fingerprint",
            "returncode": 1 if status == "FAIL" else 2, "timed_out": False,
            "quality_reasons": ["Original quality result"], "attempt_count": 1,
            "attempts": [{"returncode": 1 if status == "FAIL" else 2}],
        }
        items.append(item)
        if disposition != "PROCESSED":
            continue
        item["completed_at"] = "2026-09-07T08:00:00Z"
        run_id = str(index) * 32
        run_root = output / "runs" / run_id
        write_json(run_root / "execution.json", {
            "run_id": run_id, "batch_id": batch_id, "completed_at": item["completed_at"],
            "status": "FAIL" if status == "FAIL" else "NOT_ELIGIBLE",
        })
        candidate_root = tmp_path / "failed" / stem.upper() / run_id if status == "FAIL" else run_root
        candidate_root.mkdir(parents=True, exist_ok=True)
        candidate = candidate_root / f"{stem}.glb"
        candidate.write_bytes(stem.encode() + b" original GLB")
        artifacts[stem] = candidate
        source_copy = candidate_root / "evidence" / "source.mp4"
        source_copy.parent.mkdir()
        source_copy.write_bytes(source.read_bytes())
        if status == "FAIL":
            report = candidate_root / "evidence" / "report.json"
            write_json(report, {})
            bundle_evidence = {name: evidence(report) for name in (
                "avatar", "pose", "motion", "avatar_profile", "bone_map", "neutral_hand_pose",
                "glb_validation", "khronos_validation", "settings", "qc_thresholds",
            )}
            bundle_evidence["source_video"] = evidence(source_copy)
            write_json(candidate_root / "failure_bundle.json", {
                "schema_version": "1.0", "conversion_run_id": run_id, "output_name": stem.upper(),
                "candidate": evidence(candidate), "evidence": bundle_evidence,
            })
        else:
            stable = output / f"{stem}.glb"
            stable.write_bytes(candidate.read_bytes())
            write_json(output / f"{stem}.metadata.json", {
                "run_id": run_id, "technical_qc": status,
                "file_integrity": {"glb_sha256": convert.sha256_file(candidate)},
                "release_evidence": {"source_video": evidence(source_copy), "run_glb": evidence(candidate), "stable_glb": evidence(stable)},
            })
            write_json(output / f"{stem}.release.json", {"run_id": run_id, "artifact": evidence(candidate)})
    original = tmp_path / "output" / "batch_runs" / batch_id / "summary.json"
    write_json(original, {"batch_id": batch_id, "context_fingerprint": "original-context", "items": items})
    write_json(tmp_path / "output" / "batch_index.json", {"schema_version": "1.0", "sources": records})
    args = SimpleNamespace(
        input_dir=str(input_dir), config=str(config), qc_thresholds=str(qc),
        motion_catalog=None, resume_batch=batch_id, batch_failure_threshold=0,
    )
    return args, original, artifacts


def test_explicit_batch_continuation_preserves_review_and_quarantined_failure(interrupted_batch, monkeypatch):
    args, original, artifacts = interrupted_batch
    original_bytes = original.read_bytes()
    hashes = {stem: convert.sha256_file(path) for stem, path in artifacts.items()}
    dispatched = []
    monkeypatch.setattr(convert, "run_isolated_jobs", lambda jobs, **kwargs: dispatched.extend(job.key for job in jobs))
    monkeypatch.setattr(convert, "classify_batch_artifact", lambda **kwargs: pytest.fail("Inherited quality must not be reclassified"))

    assert convert.run_batch(args) == 1
    assert dispatched == ["Arrive.mp4", "Destination.mp4"]
    summary = json.loads((convert.PROJECT_ROOT / "output" / "batch_summary.json").read_text())
    assert summary["resumed_from_batch_id"] == args.resume_batch
    assert summary["batch_id"] != args.resume_batch
    assert summary["skipped"] == 2
    completed = {item["relative_video"]: item for item in summary["items"] if item["disposition"] == "SKIPPED_INTACT"}
    assert completed["Berth.mp4"]["status"] == "REVIEW"
    assert completed["Coach.mp4"]["status"] == "FAIL"
    for item in completed.values():
        assert item["production_eligible"] is False and item["engineering_candidate"] is False
        assert item["resume_fingerprint"] == "old-fingerprint"
        assert item["original_context_fingerprint"] == "original-context"
        assert item["quality_reasons"] == ["Original quality result"]
        assert item["attempt_count"] == 1
    assert original.read_bytes() == original_bytes
    assert {stem: convert.sha256_file(path) for stem, path in artifacts.items()} == hashes
    args.resume_batch = summary["batch_id"]
    dispatched.clear()
    assert convert.run_batch(args) == 1
    assert dispatched == ["Arrive.mp4", "Destination.mp4"]


@pytest.mark.parametrize("damage", ["review_glb", "failed_glb", "missing_glb", "source", "run_binding"])
def test_explicit_batch_continuation_refuses_changed_completed_originals(interrupted_batch, monkeypatch, damage):
    args, original, artifacts = interrupted_batch
    original_bytes = original.read_bytes()
    if damage == "review_glb":
        artifacts["Berth"].write_bytes(b"changed")
    elif damage == "failed_glb":
        artifacts["Coach"].write_bytes(b"changed")
    elif damage == "missing_glb":
        artifacts["Coach"].unlink()
    elif damage == "source":
        (Path(args.input_dir) / "Berth.mp4").write_bytes(b"changed source")
    else:
        execution_path = artifacts["Berth"].parent / "execution.json"
        execution = json.loads(execution_path.read_text())
        execution["batch_id"] = "b" * 32
        execution_path.write_text(json.dumps(execution), encoding="utf-8")
    monkeypatch.setattr(convert, "run_isolated_jobs", lambda *args, **kwargs: pytest.fail("Must refuse before worker dispatch"))
    with pytest.raises(RuntimeError):
        convert.run_batch(args)
    assert original.read_bytes() == original_bytes
    assert len(list(original.parent.parent.glob("*/summary.json"))) == 1


@pytest.mark.parametrize("batch_id", ["../outside", "a" * 31, "A" * 32, "a" * 32 + "/../other"])
def test_explicit_batch_continuation_rejects_unsafe_id(batch_id):
    with pytest.raises(SystemExit, match="hexadecimal batch ID"):
        convert.load_batch_checkpoint(batch_id)


def test_review_summary_keeps_failures_separate_from_review_availability():
    summary = {"total": 2, "items": [
        {"status": "FAIL", "disposition": "PROCESSED", "review_available": True},
        {"status": "REVIEW", "disposition": "SKIPPED_INTACT", "review_available": True},
    ]}
    convert.refresh_batch_summary(summary, final=True)
    assert summary["fail"] == 1 and summary["review"] == 1
    assert summary["batch_status"] == "FAIL"
    assert summary["review_batch_status"] == "READY_FOR_REVIEW"
    assert summary["review_available"] == 2 and summary["production_ready"] == 0
    summary["items"][0]["review_available"] = False
    convert.refresh_batch_summary(summary, final=True)
    assert summary["review_batch_status"] == "INCOMPLETE"
    assert summary["review_unavailable"] == 1


def test_missing_candidate_is_not_advertised_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    item = {"status": "FAIL", "review_delivery": {"glb_path": "stale.glb"},
            "debug_available": True, "debug_error": "stale preview error"}
    convert.attach_batch_review(item, {"video": tmp_path / "bad.mp4", "output_dir_name": "BAD"}, {"batch_id": "a" * 32})
    assert item["technical_qc"] == "FAIL"
    assert item["review_available"] is False and item["review_status"] == "UNAVAILABLE"
    assert item["review_error"]
    assert "review_delivery" not in item
    assert item["debug_available"] is False
    assert "debug_error" not in item


@pytest.fixture
def review_preview(tmp_path, monkeypatch):
    """Real tiny MP4s exercise comparison decoding; Blender itself is mocked."""
    monkeypatch.setattr(convert, "PROJECT_ROOT", tmp_path)
    source = tmp_path / "Clip.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 25, (64, 48))
    assert writer.isOpened()
    for index in range(4):
        writer.write(np.full((48, 64, 3), 30 + index * 30, np.uint8))
    writer.release()
    run_id = "a" * 32
    run_root = tmp_path / "output" / "CLIP" / "runs" / run_id
    run_root.mkdir(parents=True)
    glb = run_root / "Clip.glb"
    glb.write_bytes(b"conversion artifact; renderer is mocked")
    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"mock executable")
    for name in ("src/blender/blender_render_animation.py", "src/blender/blender_utils.py",
                 "src/qc/source_avatar_comparison.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture dependency", encoding="utf-8")
    rendered = []

    def render(executable, candidate, fps, output):
        rendered.append((candidate, fps))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(source.read_bytes())

    monkeypatch.setattr(convert, "run_blender_render_animation", render)
    monkeypatch.setattr(convert, "load_yaml", lambda path: {"blender": {"executable": str(blender)}})
    return SimpleNamespace(source=source, glb=glb, blender=blender, run_id=run_id,
                           run_root=run_root, rendered=rendered)


def _render_review_fixture(fixture):
    return convert.render_review_debug(fixture.glb, fixture.source, fixture.source, 25,
                                       fixture.blender, fixture.run_root / "review_debug", fixture.run_id)


@pytest.mark.parametrize("failed", [False, True])
def test_review_debug_reuses_verified_normal_and_failed_comparison(review_preview, failed):
    fixture = review_preview
    paths = _render_review_fixture(fixture)
    metadata = None if failed else {"release_evidence": {
        role: convert.release_evidence_record(path) for role, path in paths.items()
        if role != "avatar_preview"}}
    evidence = {**paths, "working_video": fixture.source} if failed else None
    reused = convert.review_debug_paths(original=fixture.glb, source_video=fixture.source,
        run_root=fixture.run_root, run_id=fixture.run_id, metadata=metadata, evidence=evidence)
    assert reused["comparison_video"] == paths["comparison_video"]
    assert len(fixture.rendered) == 1


def test_review_debug_backfills_failed_export_and_reuses_exact_render_cache(review_preview):
    fixture = review_preview
    preparation = fixture.run_root / "video_preparation.json"
    preparation.write_text(json.dumps({"working": {"fps": 25}}), encoding="utf-8")
    args = dict(original=fixture.glb, source_video=fixture.source, run_root=fixture.run_root,
        run_id=fixture.run_id, metadata=None,
        evidence={"video_preparation": preparation, "working_video": fixture.source})
    first = convert.review_debug_paths(**args)
    before = {role: path.read_bytes() for role, path in first.items()}
    assert convert.review_debug_paths(**args) == first
    assert len(fixture.rendered) == 1
    assert {role: path.read_bytes() for role, path in first.items()} == before
    fixture.glb.write_bytes(b"different export")
    convert.review_debug_paths(**args)
    assert len(fixture.rendered) == 2
    report = json.loads(first["source_avatar_validation"].read_text())
    assert report["validated_glb_sha256"] == convert.sha256_file(fixture.glb)


@pytest.mark.parametrize("field", ["validation_run_id", "source_video_sha256",
                                  "validated_glb_sha256", "comparison_video_sha256"])
def test_review_comparison_refuses_wrong_run_or_artifact_binding(review_preview, field):
    fixture = review_preview
    paths = _render_review_fixture(fixture)
    report = json.loads(paths["source_avatar_validation"].read_text())
    report[field] = "wrong"
    paths["source_avatar_validation"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RuntimeError, match="binding differs"):
        convert.validate_review_debug(paths, glb=fixture.glb, source_video=fixture.source,
                                      run_id=fixture.run_id)


def test_review_comparison_accepts_legacy_report_bound_by_release_evidence(review_preview):
    fixture = review_preview
    paths = _render_review_fixture(fixture)
    report = json.loads(paths["source_avatar_validation"].read_text())
    del report["validated_glb_sha256"]
    paths["source_avatar_validation"].write_text(json.dumps(report), encoding="utf-8")
    convert.validate_review_debug(paths, glb=fixture.glb, source_video=fixture.source,
                                  run_id=fixture.run_id)


@pytest.mark.parametrize("failure", ["render", "empty", "incomplete"])
def test_failed_review_preview_never_commits_cache_manifest(review_preview, monkeypatch, failure):
    fixture = review_preview
    if failure == "render":
        monkeypatch.setattr(convert, "run_blender_render_animation",
            lambda *args: (_ for _ in ()).throw(RuntimeError("render failed")))
    else:
        def bad_comparison(source, avatar, output):
            output.write_bytes(b"undecodable preview")
            return {"status": "FAIL" if failure == "empty" else "REVIEW",
                    "comparison_complete": False, "expected_comparison_frame_count": 4,
                    "comparison_frame_count": 0 if failure == "empty" else 2}
        monkeypatch.setattr(convert, "create_source_avatar_comparison", bad_comparison)
    with pytest.raises(RuntimeError, match="render failed|empty or incomplete"):
        _render_review_fixture(fixture)
    assert not list((convert.PROJECT_ROOT / "temp" / "stage_cache").rglob("manifest.json"))


def test_review_comparison_reopens_video_instead_of_trusting_report(review_preview):
    fixture = review_preview
    paths = _render_review_fixture(fixture)
    paths["comparison_video"].write_bytes(b"not decodable")
    report = json.loads(paths["source_avatar_validation"].read_text())
    report["comparison_video_sha256"] = convert.sha256_file(paths["comparison_video"])
    paths["source_avatar_validation"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RuntimeError, match="decoded frame count or FPS"):
        convert.validate_review_debug(paths, glb=fixture.glb, source_video=fixture.source,
                                      run_id=fixture.run_id)


def test_batch_review_keeps_failed_glb_available_when_preview_fails(review_preview, monkeypatch):
    fixture = review_preview
    validation = fixture.run_root / "validation.json"
    validation.write_text(json.dumps({"status": "FAIL"}), encoding="utf-8")
    monkeypatch.setattr(convert, "load_failure_bundle", lambda path: {"glb_validation": validation})
    monkeypatch.setattr(convert, "review_debug_paths",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("preview cannot decode")))
    deliveries = []

    def delivery(**kwargs):
        deliveries.append(kwargs)
        return {"review_status": "PENDING", "review_debug": {"comparison_available": False}}

    monkeypatch.setattr(convert, "write_review_delivery", delivery)
    item = {"status": "FAIL", "debug_available": True}
    verified = {"run_id": fixture.run_id,
                "preserved_artifact": {"path": str(fixture.glb), "sha256": convert.sha256_file(fixture.glb)}}
    convert.attach_batch_review(item, {"video": fixture.source, "output_dir_name": "CLIP"},
                                {"batch_id": "b" * 32}, verified)
    assert item["technical_qc"] == "FAIL" and item["review_available"] is True
    assert item["debug_available"] is False and "preview cannot decode" in item["debug_error"]
    assert deliveries[0]["debug_paths"] == {}
    assert deliveries[0]["technical_qc"] == "FAIL"


def test_preview_renderer_propagates_blender_python_errors(tmp_path, monkeypatch):
    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"executable")
    output = tmp_path / "preview.mp4"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        output.write_bytes(b"preview")

    monkeypatch.setattr(convert.subprocess, "run", run)
    convert.run_blender_render_animation(blender, tmp_path / "candidate.glb", 25, output)
    index = commands[0].index("--python-exit-code")
    assert commands[0][index + 1] == "17"


def test_review_resume_skips_unchanged_failed_and_reviewed_exports(interrupted_batch, monkeypatch):
    args, original, artifacts = interrupted_batch
    dispatches = []

    def fake_review(item, plan, summary, verified=None):
        item.update(technical_qc=item["status"], review_status="PENDING", review_available=True)

    monkeypatch.setattr(convert, "attach_batch_review", fake_review)
    monkeypatch.setattr(convert, "run_isolated_jobs", lambda jobs, **kwargs: dispatches.append([job.key for job in jobs]))
    assert convert.run_batch(args) == 1
    args.resume_batch = None
    assert convert.run_batch(args) == 1
    assert dispatches == [["Arrive.mp4", "Destination.mp4"]] * 2
    summary = json.loads((convert.PROJECT_ROOT / "output" / "batch_summary.json").read_text())
    assert summary["skipped"] == 2 and summary["review_available"] == 2
    assert summary["fail"] == 1 and summary["review"] == 1
    # Content tampering must not silently trigger conversion or appear intact.
    artifacts["Coach"].write_bytes(b"tampered")
    with pytest.raises(RuntimeError):
        convert.run_batch(args)
    assert len(dispatches) == 2


def test_batch_default_continues_after_quality_failures(monkeypatch):
    monkeypatch.setattr("sys.argv", ["convert.py", "--batch", "--input-dir", "input"])
    args = convert.parse_args()
    assert args.batch_failure_threshold == 0
    assert args.batch_workers == 1


def test_ffprobe_launch_failure_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("broken executable")))
    assert inspector._inspect_with_ffprobe(tmp_path / "video.mp4", "ffprobe") is None


@pytest.mark.parametrize("fps", [25.0, 30000 / 1001])
def test_decode_preserves_complete_cfr_timeline(tmp_path, monkeypatch, fps):
    video = tmp_path / "test.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 48))
    assert writer.isOpened()
    for index in range(6):
        writer.write(np.full((48, 64, 3), index * 20, dtype=np.uint8))
    writer.release()
    monkeypatch.setattr(inspector.shutil, "which", lambda name: None)
    prepared = inspector.prepare_video(video, tmp_path / "working")
    assert prepared.normalized is False
    assert prepared.working_info.frame_count == 6
    assert prepared.working_info.fps == pytest.approx(fps, rel=1e-4)
    assert len(prepared.timing_mapping) == 6


def test_vfr_normalization_keeps_original_and_records_mapping(tmp_path, monkeypatch):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"original")
    source = inspector.VideoInfo(str(original), "h264", 64, 48, 25, 0.2, 4, None,
        [0, 40, 120, 160], "test", [0, .04, .12, .16], True, "test", 4, "25")
    working = replace(source, frame_count=5, timestamps_ms=[0, 40, 80, 120, 160],
                      timestamps_seconds=[0, .04, .08, .12, .16], variable_frame_rate=False)
    monkeypatch.setattr(inspector, "inspect_video", lambda p: source if Path(p) == original else working)
    monkeypatch.setattr(inspector.shutil, "which", lambda name: name)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"working")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(inspector.subprocess, "run", run)
    prepared = inspector.prepare_video(original, tmp_path / "working")
    assert original.read_bytes() == b"original"
    assert prepared.normalized is True
    assert prepared.timing_mapping[2]["source_frame"] == 1
    assert "ffv1" in commands[0]


@pytest.mark.parametrize("actual_count", [2, 4])
def test_tracking_rejects_short_or_extra_decode(actual_count):
    frames = iter([np.zeros((48, 64, 3), np.uint8)] * actual_count)
    capture = SimpleNamespace(read=lambda: (True, frame) if (frame := next(frames, None)) is not None else (False, None))
    info = inspector.VideoInfo("input.mp4", "h264", 64, 48, 25, .12, 3, None, [0, 40, 80], "test")
    with pytest.raises(RuntimeError, match="decode|decoded"):
        list(_validated_video_frames(capture, info))


def test_single_visible_hand_assigned_to_opposite_wrist_is_flagged():
    pose = np.zeros((33, 4))
    pose[:, 3] = 1
    pose[15, :2], pose[16, :2] = [.2, .5], [.8, .5]
    left = np.zeros((21, 3))
    left[0, :2] = [.8, .5]
    frame = PoseFrame(0, 0, pose, pose.copy(), left, None, None, None, {})
    assert assess_hand_assignment(frame)["left"] == "POSSIBLE_WRONG_SIDE"
    pose[15, :2], pose[16, :2] = [.8, .5], [.2, .5]
    assert assess_hand_assignment(frame)["left"] == "CONSISTENT"


def test_required_channel_validation_accepts_constants_but_rejects_missing_motion():
    rotations = np.zeros((3, 1, 4))
    rotations[..., 0] = 1
    assert evaluate_required_rotation_channels(rotations, ["LeftHand"], set())["status"] == "PASS"
    rotations[1, 0] = [np.cos(.2), 0, np.sin(.2), 0]
    assert evaluate_required_rotation_channels(rotations, ["LeftHand"], set())["status"] == "FAIL"


def test_neutral_geometry_detects_curled_export():
    directions = np.zeros((5, 3, 3))
    directions[..., 1] = 1
    assert evaluate_neutral_finger_shape(directions)["status"] == "PASS"
    directions[1, 2] = [1, 0, 0]
    assert evaluate_neutral_finger_shape(directions)["status"] == "FAIL"


def test_palmar_normals_keep_anatomical_sign():
    left = anatomical_palm_normal(np.array([1, 0, 0]), np.array([0, 1, 0]), "Left")
    right = anatomical_palm_normal(np.array([1, 0, 0]), np.array([0, 1, 0]), "Right")
    assert left[2] == -1 and right[2] == 1


def test_all_frame_finger_continuity_detects_untracked_transition_spike():
    directions = np.zeros((4, 1, 3))
    directions[..., 1] = 1
    directions[2, 0] = [1, 0, 0]
    report = evaluate_direction_continuity(directions, ["LeftIndex1"], 25)
    assert report["status"] == "FAIL"
    assert report["evaluated_every_frame_transition"] is True
    assert 3 in report["fail_frame_indices"]


def test_supported_fast_finger_motion_is_reviewed_not_rejected():
    directions = np.zeros((3, 1, 3))
    directions[:, 0] = [[0, 1, 0], [1, 0, 0], [0, -1, 0]]
    report = evaluate_direction_continuity(directions, ["LeftIndex1"], 25,
        source_directions=directions.copy(), source_valid=np.ones((3, 1), bool))
    assert report["status"] == "REVIEW"
    assert report["maximum_unexplained_step_degrees"] == 0
