from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from src.avatar.bone_mapping import load_avatar_profile, load_bone_map
from src.metadata.production_metadata import (
    atomic_write_json,
    build_production_metadata,
    load_catalog_row,
    merge_metadata,
    normalize_motion_identity,
    runtime_versions,
    sha256_file,
    utc_now_iso,
    write_review_delivery,
)
from src.motion.neutral_hand import ensure_neutral_hand_pose
from src.motion.skeleton_solver import MotionBuildResult, solve_motion_from_pose
from src.pipeline.stage_cache import RunRecorder, StageCache
from src.pipeline.batch_runner import (
    MAX_BATCH_RETRIES,
    MAX_BATCH_TIMEOUT_SECONDS,
    MAX_BATCH_WORKERS,
    BatchJob,
    BatchProcessResult,
    assign_output_directory_names,
    discover_mp4_files,
    run_isolated_jobs,
)
from src.qc.gltf_compliance import classify_gltf_validator_report
from src.qc.production_gate import evaluate_production_gate
from src.qc.signer_approval import verify_signer_approval
from src.qc.source_avatar_comparison import (
    _decoded_video_timing, _timeline_error, create_source_avatar_comparison,
)
from src.tracking.holistic_tracker import MediaPipeHolisticBackend
from src.tracking.pose_schema import PoseSequence
from src.tracking.tracking_qc import evaluate_tracking
from src.video.inspector import inspect_video, prepare_video


PROJECT_ROOT = Path(__file__).resolve().parent
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
KHRONOS_VALIDATOR_VERSION = "2.0.0-dev.3.10"


def main() -> int:
    args = parse_args()
    conversion_started_at = utc_now_iso()
    run_id = uuid.uuid4().hex
    if args.revalidate_failed:
        return revalidate_failed_outputs(args)
    if args.approve_existing:
        return approve_existing_output(args)
    if args.batch:
        return run_batch(args)
    if getattr(args, "resume_batch", None):
        raise SystemExit("--resume-batch requires --batch.")

    if not args.video:
        raise SystemExit("--video is required unless --batch is used.")

    config_path = resolve_project_path(args.config)
    qc_thresholds_path = resolve_project_path(args.qc_thresholds)
    loaded_configuration_hashes = capture_input_hashes(
        {
            "settings": config_path,
            "QC thresholds": qc_thresholds_path,
        }
    )
    config = load_yaml(config_path)
    qc_thresholds = load_yaml(qc_thresholds_path)

    video_path = resolve_project_path(args.video)
    avatar_path = resolve_project_path(args.avatar or config["avatar"]["path"])
    model_path = resolve_project_path(config["pose"]["model_path"])
    gloss = video_path.stem
    requested_output_dir = getattr(args, "output_dir_name", None)
    output_dir_name = (
        validate_output_directory_name(requested_output_dir)
        if requested_output_dir
        else (gloss.upper() if config.get("output", {}).get("uppercase_output_dir", True) else gloss)
    )
    output_dir = PROJECT_ROOT / "output" / output_dir_name
    debug_dir = output_dir / "debug"
    pose_path = output_dir / f"{gloss}.pose.npz"
    body_motion_path = output_dir / f"{gloss}_body_only.motion.npz"
    body_glb_path = output_dir / f"{gloss}_body_only.glb"
    palm_motion_path = output_dir / f"{gloss}_palms.motion.npz"
    palm_glb_path = output_dir / f"{gloss}_palms.glb"
    motion_path = output_dir / f"{gloss}.motion.npz"
    glb_path = output_dir / f"{gloss}.glb"
    run_dir = output_dir / "runs" / run_id
    candidate_glb_path = run_dir / f"{gloss}.glb"
    metadata_path = output_dir / f"{gloss}.metadata.json"
    qc_path = output_dir / f"{gloss}.qc.json"
    glb_validation_path = output_dir / f"{gloss}.glb_validation.json"
    khronos_validation_path = output_dir / f"{gloss}.khronos_validation.json"
    ik_report_path = output_dir / f"{gloss}.ik_calibration.json"
    comparison_path = output_dir / f"{gloss}.comparison.json"
    source_avatar_validation_path = output_dir / f"{gloss}.source_avatar_validation.json"
    match_validation_path = output_dir / f"{gloss}.match_validation.json"
    signer_review_path = output_dir / f"{gloss}.signer_review.json"
    overlay_path = debug_dir / f"{gloss}_pose_overlay.mp4"
    avatar_preview_path = debug_dir / f"{gloss}_avatar_preview.mp4"
    source_avatar_preview_path = debug_dir / f"{gloss}_source_avatar_comparison.mp4"
    candidate_debug_dir = run_dir / "debug"
    candidate_avatar_preview_path = candidate_debug_dir / f"{gloss}_avatar_preview.mp4"
    candidate_source_avatar_preview_path = candidate_debug_dir / f"{gloss}_source_avatar_comparison.mp4"
    release_manifest_path = output_dir / f"{gloss}.release.json"
    blender_path = resolve_project_path(config.get("blender", {}).get("executable") or default_blender_path())
    signer_approval = (
        load_signer_approval(
            resolve_project_path(args.signer_approval),
            resolve_project_path(args.trusted_signers),
        )
        if args.signer_approval
        else None
    )

    print_header(video_path, avatar_path)
    output_lock = acquire_output_lock(output_dir, run_id)
    recorder = RunRecorder(
        run_dir / "execution.json", run_id=run_id,
        batch_id=getattr(args, "batch_id", None),
        attempt=int(os.environ.get("VIDEO2GLB_BATCH_ATTEMPT", "1")),
    )
    cache = StageCache(PROJECT_ROOT / "temp" / "stage_cache" / output_dir_name)
    stage_guard = lambda: None

    def run_stage(index: int, label: str, func):
        print(f"[{index}] {label:<29}", end="", flush=True)
        result = recorder.execute(index, label, func, stage_guard)
        print(" COMPLETE")
        return result

    try:
        invalidate_release_state(
            metadata_path=metadata_path,
            qc_path=qc_path,
            signer_review_path=signer_review_path,
            release_manifest_path=release_manifest_path,
            run_id=run_id,
            gloss=gloss,
        )
        ensure_required_inputs(video_path, avatar_path, model_path)
        critical_inputs = {
            "source video": video_path,
            "avatar": avatar_path,
            "pose model": model_path,
            "settings": config_path,
            "QC thresholds": qc_thresholds_path,
        }
        catalog_path = resolve_project_path(args.motion_catalog) if args.motion_catalog else None
        if catalog_path is not None and catalog_path.is_file():
            critical_inputs["motion catalog"] = catalog_path
        input_hashes = capture_input_hashes(critical_inputs)
        context_options = dict(
            config_path=config_path, qc_thresholds_path=qc_thresholds_path,
            avatar_path=avatar_path, model_path=model_path, motion_catalog_path=catalog_path,
        )
        context_fingerprint = build_batch_context_fingerprint(**context_options)
        expected_context = getattr(args, "expected_context_fingerprint", None)
        if expected_context and context_fingerprint != expected_context:
            raise RuntimeError("Pipeline context changed before the batch worker started; resume with the current version.")
        code_inputs = {
            "pipeline:" + path.relative_to(PROJECT_ROOT).as_posix(): path
            for path in [PROJECT_ROOT / "convert.py", *sorted((PROJECT_ROOT / "src").rglob("*.py"))]
        }
        code_inputs.update({"settings": config_path, "QC thresholds": qc_thresholds_path})
        code_hashes = capture_input_hashes(code_inputs)
        def guard_pipeline_context():
            assert_input_hashes_unchanged(code_inputs, code_hashes)
            assert_input_hashes_unchanged(critical_inputs, input_hashes)
            if build_batch_context_fingerprint(**context_options) != context_fingerprint:
                raise RuntimeError("Pipeline context changed during conversion; resume with the current version.")

        stage_guard = guard_pipeline_context
        if any(
            input_hashes[name] != expected_hash
            for name, expected_hash in loaded_configuration_hashes.items()
        ):
            raise RuntimeError(
                "Settings or QC thresholds changed while they were being loaded; rerun from stable inputs."
            )
        profile_path, bone_map_path, neutral_hand_pose_path = avatar_calibration_paths(
            avatar_path,
            expected_avatar_sha256=input_hashes["avatar"],
        )
        prepared = run_stage(1, "Inspect/prepare video timing", lambda: prepare_video(video_path, run_dir / "working"))
        video_info = prepared.working_info
        working_video_path = prepared.working_path
        preparation_path = run_dir / "preparation.json"
        preparation_payload = prepared.to_json_dict()
        atomic_write_json(preparation_path, preparation_payload)
        critical_inputs["working video"] = working_video_path
        input_hashes["working video"] = sha256_file(working_video_path)
        source_stage_code = lambda directories: {
            "code:" + path.relative_to(PROJECT_ROOT).as_posix(): path
            for directory in directories
            for path in sorted((PROJECT_ROOT / "src" / directory).rglob("*.py"))
        }

        def process_pose_uncached():
            backend = MediaPipeHolisticBackend(model_path=model_path, options=config.get("pose", {}))
            sequence = backend.process_video(
                video_path=working_video_path,
                video_info=video_info,
                overlay_path=overlay_path if should_save_debug(args, config) else None,
                progress=print_tracking_progress,
            )
            sequence.save_npz(pose_path)
            return {"frame_count": sequence.frame_count, "fps": sequence.fps}

        def process_pose():
            outputs = {"pose": pose_path}
            if should_save_debug(args, config):
                outputs["overlay"] = overlay_path
            cache.execute(
                "tracking",
                inputs={"working_video": working_video_path, "model": model_path,
                        **source_stage_code(("video", "tracking"))},
                settings={"pose": config.get("pose", {}), "fps": video_info.fps,
                          "runtime_versions": runtime_versions(),
                          "save_overlay": should_save_debug(args, config)},
                outputs=outputs, action=process_pose_uncached,
            )
            return PoseSequence.load_npz(pose_path)

        pose_sequence = run_stage(2, "Extract holistic landmarks", process_pose)
        run_stage(3, "Verify tracked timeline", lambda: verify_pose_timeline(pose_sequence, video_info))
        qc_result = run_stage(4, "Validate tracking", lambda: evaluate_tracking(pose_sequence, qc_thresholds))
        def calibrate_avatar_and_hands():
            ensure_avatar_calibrated(
                blender_path,
                avatar_path,
                profile_path,
                bone_map_path,
                expected_avatar_sha256=input_hashes["avatar"],
            )
            calibration_lock = acquire_output_lock(profile_path.parent, f"neutral-{run_id}", wait_seconds=30.0)
            try:
                return ensure_neutral_hand_pose(profile_path, bone_map_path, neutral_hand_pose_path)
            finally:
                release_output_lock(calibration_lock)

        run_stage(5, "Calibrate avatar/neutral hands", calibrate_avatar_and_hands)
        if config.get("output", {}).get("save_intermediates", False):
            run_stage(6, "Solve torso/arms", lambda: solve_motion_from_pose(
                pose_path, profile_path, bone_map_path, body_motion_path, gloss,
                body_only=True, include_palms=False, include_fingers=False, smoothing=False,
            ))
            run_stage(7, "Export body GLB", lambda: run_blender_apply(
                blender_path, avatar_path, profile_path, bone_map_path, body_motion_path, body_glb_path,
            ))
            run_stage(8, "Solve palms", lambda: solve_motion_from_pose(
                pose_path, profile_path, bone_map_path, palm_motion_path, gloss,
                include_palms=True, include_fingers=False, smoothing=False,
            ))
            run_stage(9, "Export palm GLB", lambda: run_blender_apply(
                blender_path, avatar_path, profile_path, bone_map_path, palm_motion_path,
                palm_glb_path, use_palm_tracking=True,
            ))

        def solve_final_motion():
            metadata = cache.execute(
                "motion",
                inputs={"pose": pose_path, "profile": profile_path, "bone_map": bone_map_path,
                        "neutral_hand": neutral_hand_pose_path, **source_stage_code(("motion", "avatar"))},
                settings={"gloss": gloss, "smoothing": bool(config.get("processing", {}).get("smoothing", True)),
                          "runtime_versions": runtime_versions()},
                outputs={"motion": motion_path},
                action=lambda: solve_motion_from_pose(
                    pose_path, profile_path, bone_map_path, motion_path, gloss,
                    include_palms=True, include_fingers=True,
                    smoothing=bool(config.get("processing", {}).get("smoothing", True)),
                    neutral_hand_pose_path=neutral_hand_pose_path,
                ).metadata,
            )
            return MotionBuildResult(motion_path, metadata)
        final_motion_result = run_stage(
            10,
            "Solve fingers/QC motion",
            solve_final_motion,
        )
        run_stage(
            11,
            "Launch Blender/export GLB",
            lambda: cache.execute(
                "export",
                inputs={"motion": motion_path, "avatar": avatar_path, "profile": profile_path,
                        "bone_map": bone_map_path, "blender": blender_path,
                        "apply_code": PROJECT_ROOT / "src/blender/blender_apply_motion.py",
                        "utils_code": PROJECT_ROOT / "src/blender/blender_utils.py",
                        "mesh_contact": PROJECT_ROOT / "src/blender/mesh_contact.py",
                        "arm_ik": PROJECT_ROOT / "src/motion/arm_ik.py",
                        "neutral_hand_code": PROJECT_ROOT / "src/motion/neutral_hand.py",
                        "quaternion_utils": PROJECT_ROOT / "src/motion/quaternion_utils.py",
                        "smoothing_code": PROJECT_ROOT / "src/motion/smoothing.py",
                        "mesh_clearance": PROJECT_ROOT / "src/qc/mesh_clearance.py"},
                settings={"ik": True, "fingers": True, "palms": True},
                outputs={"glb": candidate_glb_path, "ik_report": ik_report_path},
                action=lambda: run_blender_apply(
                blender_path,
                avatar_path,
                profile_path,
                bone_map_path,
                motion_path,
                candidate_glb_path,
                ik_report_path,
                use_finger_tracking=True,
                use_palm_tracking=True,
                ),
            ),
        )
        glb_validation = run_stage(
            12,
            "Re-import GLB",
            lambda: run_blender_validate(
                blender_path,
                candidate_glb_path,
                motion_path,
                profile_path,
                bone_map_path,
                glb_validation_path,
                ik_report_path=ik_report_path,
            ),
        )
        khronos_validation = run_stage(
            13,
            "Validate glTF 2.0 compliance",
            lambda: run_khronos_validate(
                candidate_glb_path,
                khronos_validation_path,
                config.get("validation", {}).get("khronos_warning_allowlist", {}),
            ),
        )
        glb_validation = merge_khronos_validation(
            glb_validation,
            khronos_validation,
            glb_validation_path,
        )
        failure_evidence_paths = {
            "source_video": video_path, "avatar": avatar_path, "pose": pose_path,
            "motion": motion_path, "avatar_profile": profile_path, "bone_map": bone_map_path,
            "neutral_hand_pose": neutral_hand_pose_path, "glb_validation": glb_validation_path,
            "khronos_validation": khronos_validation_path,
            "settings": resolve_project_path(args.config),
            "qc_thresholds": resolve_project_path(args.qc_thresholds),
            "video_preparation": preparation_path, "working_video": working_video_path,
            "ik_report": ik_report_path,
        }
        if glb_validation.get("status") == "FAIL":
            failed_debug = {}
            try:
                failed_debug = render_review_debug(
                    candidate_glb_path, video_path, working_video_path, video_info.fps,
                    blender_path, run_dir / "review_debug", run_id,
                )
            except Exception as debug_exc:
                # Keep the original technical failure and its export, even if
                # an invalid candidate cannot be rendered for inspection.
                atomic_write_json(run_dir / "review_debug_error.json", {"error": str(debug_exc)})
                print(f"\nReview preview unavailable: {debug_exc}", flush=True)
            quarantine_failed_candidate(
                candidate_glb_path,
                output_dir_name,
                run_id,
                evidence_paths={**failure_evidence_paths, **failed_debug},
            )
            raise RuntimeError(f"GLB validation failed: {glb_validation.get('reasons')}")
        # The side-by-side comparison is production review evidence, not an
        # optional debug artifact.  ``save_debug`` controls only the pose
        # overlay; release gating always gets a complete comparison video.
        run_stage(
            14,
            "Render avatar preview",
            lambda: cache.execute(
                "preview", inputs={"glb": candidate_glb_path, "blender": blender_path,
                    "render_code": PROJECT_ROOT / "src/blender/blender_render_animation.py",
                    "utils_code": PROJECT_ROOT / "src/blender/blender_utils.py"},
                settings={"fps": video_info.fps}, outputs={"preview": candidate_avatar_preview_path},
                action=lambda: run_blender_render_animation(
                blender_path,
                candidate_glb_path,
                video_info.fps,
                candidate_avatar_preview_path,
                ),
            ),
        )
        source_avatar_validation = run_stage(
            15,
            "Compare source/avatar",
            lambda: create_source_avatar_comparison(
                working_video_path,
                candidate_avatar_preview_path,
                candidate_source_avatar_preview_path,
            ),
        )
        comparison_generated = True
        source_avatar_validation["validation_run_id"] = run_id
        source_avatar_validation["validated_glb_sha256"] = sha256_file(candidate_glb_path)
        source_avatar_validation["source_video_sha256"] = sha256_file(video_path)
        source_avatar_validation["working_video_sha256"] = sha256_file(working_video_path)
        source_avatar_validation["original_source_frame_count"] = prepared.source_info.frame_count
        source_avatar_validation["timing_normalized"] = prepared.normalized
        source_avatar_validation["comparison_video_sha256"] = (
            sha256_file(candidate_source_avatar_preview_path)
            if comparison_generated
            else None
        )
        if qc_result.technical_qc == "FAIL" or source_avatar_validation.get("status") == "FAIL":
            # Preserve this run's comparison report before rejecting publication.
            # Never reuse the mutable report from a previous successful run.
            failed_report = run_dir / "debug" / f"{gloss}.source_avatar_validation.json"
            atomic_write_json(failed_report, source_avatar_validation)
            quarantine_failed_candidate(
                candidate_glb_path, output_dir_name, run_id,
                evidence_paths={**failure_evidence_paths,
                    "source_avatar_validation": failed_report,
                    "comparison_video": candidate_source_avatar_preview_path,
                    "avatar_preview": candidate_avatar_preview_path},
            )
            raise RuntimeError("Required tracking or comparison validation failed; candidate was not published.")
        run_stage(
            16,
            "Publish validated artifacts",
            lambda: (
                assert_input_hashes_unchanged(critical_inputs, input_hashes),
                publish_validated_artifacts(
                    candidate_glb_path=candidate_glb_path,
                    glb_path=glb_path,
                    candidate_avatar_preview_path=candidate_avatar_preview_path,
                    avatar_preview_path=avatar_preview_path,
                    candidate_comparison_path=candidate_source_avatar_preview_path,
                    comparison_path=source_avatar_preview_path,
                ),
            )[-1],
        )
        final_technical_qc = max_status(qc_result.technical_qc, glb_validation.get("status", "FAIL"))
        if source_avatar_validation.get("status") in {"PASS", "REVIEW", "FAIL"}:
            final_technical_qc = max_status(final_technical_qc, source_avatar_validation["status"])
        signer_review = build_signer_review_record(
            gloss=gloss,
            technical_qc=final_technical_qc,
            source_video=video_path,
            glb_path=glb_path,
            comparison_video=source_avatar_preview_path if comparison_generated else None,
            approval=signer_approval,
            run_id=run_id,
        )
        run_stage(
            17,
            "Write comparison/review reports",
            lambda: write_comparison_and_review_reports(
                source_avatar_validation_path,
                match_validation_path,
                signer_review_path,
                source_avatar_validation,
                signer_review,
            ),
        )
        metadata = run_stage(
            18,
            "Write metadata/QC",
            lambda: write_reports(
                metadata_path,
                qc_path,
                video_info,
                qc_result,
                glb_validation,
                final_technical_qc,
                source_avatar_validation,
                signer_review,
                source_video=video_path,
                avatar_file=avatar_path,
                glb_path=glb_path,
                pose_path=pose_path,
                motion_path=motion_path,
                glb_validation_path=glb_validation_path,
                khronos_validation_path=khronos_validation_path,
                source_avatar_validation_path=source_avatar_validation_path,
                signer_review_path=signer_review_path,
                comparison_video_path=source_avatar_preview_path,
                profile_path=profile_path,
                bone_map_path=bone_map_path,
                neutral_hand_pose_path=neutral_hand_pose_path,
                neutral_hand_validation=final_motion_result.metadata.get("neutral_hand_validation", {}),
                started_at=conversion_started_at,
                catalog_path=catalog_path,
                run_id=run_id,
                logical_identity_stem=gloss,
                expected_evidence_hashes={
                    "source_video": input_hashes["source video"],
                    "avatar": input_hashes["avatar"],
                },
                preparation_path=preparation_path,
                working_video_path=working_video_path,
                ik_report_path=ik_report_path,
                execution_context={
                    "run_id": run_id, "batch_id": getattr(args, "batch_id", None),
                    "attempt": recorder.payload["attempt"], "original_filename": video_path.name,
                    "context_fingerprint": context_fingerprint, "input_hashes": input_hashes,
                    "stage_cache": cache.events, "stages": recorder.payload["stages"],
                    "execution_record": relative_display(recorder.path),
                    "calibration_hashes": {"profile": sha256_file(profile_path),
                        "bone_map": sha256_file(bone_map_path), "neutral_hand": sha256_file(neutral_hand_pose_path)},
                },
            ),
        )
        assert_input_hashes_unchanged(critical_inputs, input_hashes)
        run_stage(19, "Record research status", lambda: write_research_status(comparison_path, gloss, glb_path))
        run_stage(
            20,
            "Commit release manifest",
            lambda: write_release_manifest(
                release_manifest_path,
                metadata,
                glb_path,
                run_id,
                immutable_glb_path=candidate_glb_path,
            ),
        )
        execution = recorder.finish(metadata.get("production", {}).get("production_status", "REVIEW"))
        metadata.setdefault("processing", {}).update(
            stages=execution["stages"], elapsed_seconds=execution["elapsed_seconds"],
            conversion_completed_at=execution["completed_at"],
        )
        atomic_write_json(metadata_path, metadata)
        delivery_debug = {"comparison_video": candidate_source_avatar_preview_path,
                          "avatar_preview": candidate_avatar_preview_path,
                          "source_avatar_validation": source_avatar_validation_path}
        try:
            validate_review_debug(delivery_debug, glb=candidate_glb_path, source_video=video_path,
                                  run_id=run_id, working_video=working_video_path)
        except Exception as debug_exc:
            delivery_debug = {}
            print(f"\nReview preview unavailable: {debug_exc}", flush=True)
        try:
            write_review_delivery(
                project_root=PROJECT_ROOT, delivery_dir=PROJECT_ROOT / "output" / "review" / output_dir_name / run_id,
                source_video=video_path, original_glb=candidate_glb_path, run_id=run_id,
                batch_id=getattr(args, "batch_id", None) or run_id, technical_qc=final_technical_qc,
                validation=glb_validation, metadata=metadata, execution=execution,
                context_fingerprint=context_fingerprint,
                debug_paths=delivery_debug,
            )
        except Exception as debug_exc:
            print(f"\nReview delivery unavailable: {debug_exc}", flush=True)
    except Exception as exc:
        execution = recorder.finish("FAIL", error=f"{type(exc).__name__}: {exc}")
        failed_glb = PROJECT_ROOT / "failed" / output_dir_name / run_id / f"{gloss}.glb"
        if failed_glb.is_file():
            try:
                failed_evidence = load_failure_bundle(failed_glb)
                delivery_debug = {role: failed_evidence[role] for role in
                                  ("comparison_video", "avatar_preview", "source_avatar_validation")
                                  if role in failed_evidence}
                try:
                    validate_review_debug(delivery_debug, glb=failed_glb, source_video=video_path,
                                          run_id=run_id, working_video=failed_evidence.get("working_video"))
                except Exception as debug_exc:
                    delivery_debug = {}
                    print(f"\nReview preview unavailable: {debug_exc}", flush=True)
                write_review_delivery(
                    project_root=PROJECT_ROOT, delivery_dir=PROJECT_ROOT / "output" / "review" / output_dir_name / run_id,
                    source_video=video_path, original_glb=failed_glb, run_id=run_id,
                    batch_id=getattr(args, "batch_id", None) or run_id, technical_qc="FAIL",
                    validation=read_json(failed_evidence["glb_validation"], {}), evidence=failed_evidence,
                    execution=execution, context_fingerprint=locals().get("context_fingerprint"),
                    debug_paths=delivery_debug,
                )
            except Exception as debug_exc:
                print(f"\nReview delivery unavailable: {debug_exc}", flush=True)
        release_output_lock(output_lock)
        print(f"\nFAIL: {exc}")
        return 1

    print("\nOUTPUT:")
    print(relative_display(glb_path))
    print(relative_display(pose_path))
    print(relative_display(motion_path))
    print(relative_display(qc_path))
    print(relative_display(source_avatar_validation_path))
    print(relative_display(signer_review_path))
    if ik_report_path.exists():
        print(relative_display(ik_report_path))
    if should_save_debug(args, config):
        print(relative_display(overlay_path))
    print(relative_display(avatar_preview_path))
    print(relative_display(source_avatar_preview_path))
    print("\nTechnical QC:")
    print(final_technical_qc)
    production = metadata.get("production", {})
    print("\nProduction gate:")
    print(production.get("production_status", "NOT_ELIGIBLE"))
    print(f"\nFinal {gloss} integration test attempted.")
    release_output_lock(output_lock)
    if production.get("production_eligible") is True:
        return 0
    if args.require_production:
        return 2
    return 0 if production.get("engineering_candidate") is True else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISL video to GLB converter, built milestone-by-milestone.")
    parser.add_argument("--video", help="Input MP4 path.")
    parser.add_argument("--input-dir", help="Directory of MP4 files for batch mode.")
    parser.add_argument("--batch", action="store_true", help="Process every MP4 in --input-dir without stopping on individual failures.")
    parser.add_argument(
        "--batch-recursive",
        action="store_true",
        help="Include MP4 files below subdirectories of --input-dir.",
    )
    parser.add_argument(
        "--batch-workers",
        type=int,
        default=1,
        help=f"Concurrent isolated conversion workers (1-{MAX_BATCH_WORKERS}; default: 1).",
    )
    parser.add_argument(
        "--batch-timeout-seconds",
        type=float,
        default=3600.0,
        help="Maximum seconds for each conversion attempt (default: 3600).",
    )
    parser.add_argument(
        "--batch-retries",
        type=int,
        default=1,
        help=f"Retries after a crash/timeout (0-{MAX_BATCH_RETRIES}; default: 1).",
    )
    parser.add_argument(
        "--no-batch-resume",
        action="store_false",
        dest="batch_resume",
        help="Reprocess inputs even when the matching batch fingerprint and candidate are intact.",
    )
    parser.set_defaults(batch_resume=True)
    parser.add_argument(
        "--resume-batch",
        help="Continue unfinished inputs from this batch ID, preserving its intact completed results.",
    )
    parser.add_argument(
        "--batch-pause-file", default="./output/.batch_pause",
        help="Stop dispatching and drain active jobs while this file exists; remove it and rerun to resume.",
    )
    parser.add_argument(
        "--batch-failure-threshold", type=int, default=0,
        help="Pause after this many failures in the same category (default: 0, continue all inputs).",
    )
    parser.add_argument("--expected-context-fingerprint", help=argparse.SUPPRESS)
    parser.add_argument("--batch-id", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir-name", help=argparse.SUPPRESS)
    parser.add_argument("--avatar", help="Optional avatar FBX path. Defaults to config/settings.yaml.")
    parser.add_argument("--config", default="./config/settings.yaml", help="Project settings YAML.")
    parser.add_argument("--qc-thresholds", default="./config/qc_thresholds.yaml", help="Tracking QC thresholds YAML.")
    parser.add_argument("--motion-catalog", default="./motion_catalog.csv", help="Optional CSV with linguistic, source, and licensing metadata.")
    parser.add_argument(
        "--signer-approval",
        help="Optional qualified-signer approval JSON. Its artifact hashes must match this run exactly.",
    )
    parser.add_argument(
        "--trusted-signers",
        default="./config/trusted_signers.json",
        help="Registry of active Ed25519 public keys authorized to sign approvals.",
    )
    parser.add_argument(
        "--require-production",
        action="store_true",
        help="Exit successfully only when every strict production-release gate passes.",
    )
    parser.add_argument(
        "--approve-existing",
        action="store_true",
        help="Apply a signer approval to the exact existing artifacts without regenerating them.",
    )
    parser.add_argument("--save-debug", action="store_true", help="Force writing debug overlay video.")
    parser.add_argument(
        "--revalidate-failed",
        action="store_true",
        help="Re-check preserved failed GLBs after a validator fix without re-running video tracking.",
    )
    return parser.parse_args()


class SavedQCResult:
    """Adapter that lets approval-only runs reuse the recorded tracking QC."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = deepcopy(payload)
        self.technical_qc = str(payload.get("technical_qc") or "FAIL").upper()

    def to_json_dict(self) -> dict[str, Any]:
        return deepcopy(self.payload)


def approve_existing_output(args: argparse.Namespace) -> int:
    """Evaluate signer/catalog evidence against existing immutable artifacts."""
    if not args.video:
        raise SystemExit("--video is required with --approve-existing.")
    if not args.signer_approval:
        raise SystemExit("--signer-approval is required with --approve-existing.")

    config = load_yaml(resolve_project_path(args.config))
    video_path = resolve_project_path(args.video)
    avatar_path = resolve_project_path(args.avatar or config["avatar"]["path"])
    gloss = video_path.stem
    output_name = gloss.upper() if config.get("output", {}).get("uppercase_output_dir", True) else gloss
    if getattr(args, "output_dir_name", None):
        output_name = validate_output_directory_name(args.output_dir_name)
    output_dir = PROJECT_ROOT / "output" / output_name
    glb_path = output_dir / f"{gloss}.glb"
    pose_path = output_dir / f"{gloss}.pose.npz"
    motion_path = output_dir / f"{gloss}.motion.npz"
    metadata_path = output_dir / f"{gloss}.metadata.json"
    qc_path = output_dir / f"{gloss}.qc.json"
    glb_validation_path = output_dir / f"{gloss}.glb_validation.json"
    khronos_validation_path = output_dir / f"{gloss}.khronos_validation.json"
    source_avatar_validation_path = output_dir / f"{gloss}.source_avatar_validation.json"
    match_validation_path = output_dir / f"{gloss}.match_validation.json"
    signer_review_path = output_dir / f"{gloss}.signer_review.json"
    comparison_video_path = output_dir / "debug" / f"{gloss}_source_avatar_comparison.mp4"
    release_manifest_path = output_dir / f"{gloss}.release.json"
    blender_path = resolve_project_path(
        config.get("blender", {}).get("executable") or default_blender_path()
    )
    catalog_path = resolve_project_path(args.motion_catalog) if args.motion_catalog else None
    catalog_hash = (
        sha256_file(catalog_path)
        if catalog_path is not None and catalog_path.is_file()
        else None
    )

    lock = acquire_output_lock(output_dir, f"approval-{uuid.uuid4().hex}")
    protected_state: dict[Path, bytes | None] = {}
    try:
        protected_state = capture_file_states(
            (
                metadata_path,
                qc_path,
                glb_validation_path,
                khronos_validation_path,
                source_avatar_validation_path,
                match_validation_path,
                signer_review_path,
                release_manifest_path,
            )
        )
        approval = load_signer_approval(
            resolve_project_path(args.signer_approval),
            resolve_project_path(args.trusted_signers),
        )
        existing_metadata = read_json(metadata_path, {})
        qc_payload = read_json(qc_path, {})
        glb_validation = read_json(glb_validation_path, {})
        source_validation = read_json(source_avatar_validation_path, {})
        if not existing_metadata:
            raise RuntimeError("Existing conversion metadata is incomplete; run a full conversion first.")

        run_id = existing_metadata.get("run_id")
        processing = existing_metadata.get("processing")
        processing = processing if isinstance(processing, dict) else {}
        run_id = str(run_id or processing.get("run_id") or "").strip()
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise RuntimeError("Existing metadata has no immutable conversion run ID; rerun conversion first.")
        immutable_glb_path = output_dir / "runs" / run_id / f"{gloss}.glb"
        required_files = (
            glb_path,
            immutable_glb_path,
        )
        missing = [str(path) for path in required_files if not path.is_file()]
        if missing:
            raise RuntimeError("Approval evidence is missing: " + ", ".join(missing))

        # Approval may replace only the signer-owned review. Every piece of
        # machine evidence must still be byte-for-byte identical to the run
        # that produced the immutable GLB.
        evidence_pass, evidence_reasons = verify_release_evidence(
            existing_metadata,
            ignored_names={"signer_review"},
        )
        if not evidence_pass:
            raise RuntimeError(
                "Existing release evidence changed; rerun the full conversion: "
                + "; ".join(evidence_reasons)
            )
        source_evidence_path = release_evidence_path(existing_metadata, "source_video")
        avatar_evidence_path = release_evidence_path(existing_metadata, "avatar")
        pose_evidence_path = release_evidence_path(existing_metadata, "pose")
        motion_evidence_path = release_evidence_path(existing_metadata, "motion")
        profile_evidence_path = release_evidence_path(existing_metadata, "avatar_profile")
        bone_map_evidence_path = release_evidence_path(existing_metadata, "bone_map")
        neutral_evidence_path = release_evidence_path(existing_metadata, "neutral_hand_pose")
        comparison_evidence_path = release_evidence_path(existing_metadata, "comparison_video")
        # ``write_reports`` refreshes these immutable report-role snapshots.
        # Keep their exact prior bytes until the new approval transaction has
        # passed every late gate and manifest check.
        immutable_report_paths = [
            release_evidence_path(existing_metadata, role)
            for role in (
                "qc",
                "glb_validation",
                "khronos_validation",
                "source_avatar_validation",
                "signer_review",
            )
        ]
        existing_evidence_paths, _ = resolve_release_evidence_paths(existing_metadata)
        preparation_evidence_path = existing_evidence_paths.get("video_preparation")
        working_evidence_path = existing_evidence_paths.get("working_video")
        if "catalog_record" in existing_evidence_paths:
            immutable_report_paths.append(existing_evidence_paths["catalog_record"])
        else:
            immutable_report_paths.append(
                output_dir / "runs" / run_id / "evidence" / "catalog_record.json"
            )
        protected_state.update(
            {
                path: state
                for path, state in capture_file_states(immutable_report_paths).items()
                if path not in protected_state
            }
        )
        qc_payload = read_json(release_evidence_path(existing_metadata, "qc"), {})
        glb_validation = read_json(
            release_evidence_path(existing_metadata, "glb_validation"), {}
        )
        source_validation = read_json(
            release_evidence_path(existing_metadata, "source_avatar_validation"), {}
        )
        if not qc_payload or not glb_validation or not source_validation:
            raise RuntimeError("Immutable conversion reports are incomplete; rerun conversion first.")
        immutable_hash = sha256_file(immutable_glb_path)
        if not immutable_hash or sha256_file(glb_path) != immutable_hash:
            raise RuntimeError("Stable GLB is not the exact immutable run artifact.")
        if glb_validation.get("motion_sha256") != sha256_file(motion_evidence_path):
            raise RuntimeError("Saved motion no longer matches the GLB validation evidence.")

        # Re-run both validators under the output lock so approval cannot rely
        # solely on historical PASS reports.
        glb_validation = run_blender_validate(
            blender_path,
            immutable_glb_path,
            motion_evidence_path,
            profile_evidence_path,
            bone_map_evidence_path,
            glb_validation_path,
            **({"ik_report_path": evidence_paths["ik_report"]} if evidence_paths.get("ik_report") else {}),
        )
        khronos_validation = run_khronos_validate(
            immutable_glb_path,
            khronos_validation_path,
            config.get("validation", {}).get("khronos_warning_allowlist", {}),
        )
        glb_validation = merge_khronos_validation(
            glb_validation,
            khronos_validation,
            glb_validation_path,
        )
        if str(glb_validation.get("status") or "FAIL").upper() == "FAIL":
            raise RuntimeError(
                f"Fresh GLB validation failed; approval was not applied: {glb_validation.get('reasons')}"
            )

        atomic_write_json(
            release_manifest_path,
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "status": "REVIEWING_APPROVAL",
                "releaseable": False,
                "updated_at": utc_now_iso(),
            },
        )
        saved_qc = SavedQCResult(qc_payload)
        final_qc = saved_qc.technical_qc
        signer_review = build_signer_review_record(
            gloss=gloss,
            technical_qc=final_qc,
            source_video=source_evidence_path,
            glb_path=glb_path,
            comparison_video=comparison_evidence_path,
            approval=approval,
            run_id=run_id,
        )
        write_comparison_and_review_reports(
            source_avatar_validation_path,
            match_validation_path,
            signer_review_path,
            source_validation,
            signer_review,
        )
        neutral_validation = (
            existing_metadata.get("technical_validation", {}).get("neutral_hand_validation", {})
            if isinstance(existing_metadata.get("technical_validation"), dict)
            else {}
        )
        metadata = write_reports(
            metadata_path,
            qc_path,
            inspect_video(working_evidence_path or source_evidence_path),
            saved_qc,
            glb_validation,
            final_qc,
            source_validation,
            signer_review,
            source_video=source_evidence_path,
            avatar_file=avatar_evidence_path,
            glb_path=glb_path,
            pose_path=pose_evidence_path,
            motion_path=motion_evidence_path,
            glb_validation_path=glb_validation_path,
            khronos_validation_path=khronos_validation_path,
            source_avatar_validation_path=source_avatar_validation_path,
            signer_review_path=signer_review_path,
            comparison_video_path=comparison_evidence_path,
            profile_path=profile_evidence_path,
            bone_map_path=bone_map_evidence_path,
            neutral_hand_pose_path=neutral_evidence_path,
            neutral_hand_validation=neutral_validation,
            started_at=str(processing.get("conversion_started_at") or utc_now_iso()),
            catalog_path=catalog_path,
            run_id=run_id,
            logical_identity_stem=gloss,
            preparation_path=preparation_evidence_path,
            working_video_path=working_evidence_path,
        )
        if catalog_path is not None and sha256_file(catalog_path) != catalog_hash:
            raise RuntimeError("Motion catalog changed during approval; prior reports were restored.")
        write_release_manifest(
            release_manifest_path,
            metadata,
            glb_path,
            run_id,
            immutable_glb_path=immutable_glb_path,
        )
        if metadata.get("production", {}).get("production_eligible") is True:
            published, published_reasons = evaluate_published_release(
                output_dir,
                gloss,
                metadata,
                trusted_signers_path=resolve_project_path(args.trusted_signers),
            )
            if not published:
                raise RuntimeError(
                    "Published release verification failed: "
                    + "; ".join(published_reasons)
                )
    except Exception as exc:
        try:
            restore_file_states(protected_state)
        except Exception as restore_exc:
            print(f"\nFAIL: approval failed and prior report restoration failed: {restore_exc}")
        print(f"\nFAIL: {exc}")
        return 1
    finally:
        release_output_lock(lock)

    production = metadata.get("production", {})
    print(json.dumps({
        "production_status": production.get("production_status"),
        "production_eligible": production.get("production_eligible") is True,
        "release_manifest": relative_display(release_manifest_path),
    }, indent=2))
    return 0 if production.get("production_eligible") is True else 2


def load_batch_checkpoint(batch_id: str) -> dict[str, Any]:
    """Read an explicit continuation checkpoint without changing its history."""
    if not isinstance(batch_id, str) or RUN_ID_PATTERN.fullmatch(batch_id) is None:
        raise SystemExit("--resume-batch must be a 32-character lowercase hexadecimal batch ID.")
    root = (PROJECT_ROOT / "output" / "batch_runs").resolve()
    path = root / batch_id / "summary.json"
    if not path.resolve().is_relative_to(root / batch_id) or not path.is_file():
        raise RuntimeError(f"Cannot resume batch {batch_id}: its original summary is missing or outside its batch directory.")
    checkpoint = read_json(path, {})
    if checkpoint.get("batch_id") != batch_id or not isinstance(checkpoint.get("items"), list) or not checkpoint["items"]:
        raise RuntimeError(f"Cannot resume batch {batch_id}: invalid original summary.")
    seen: set[str] = set()
    for item in checkpoint["items"]:
        if not isinstance(item, dict):
            raise RuntimeError("Cannot resume batch: invalid original item.")
        source_id = item.get("source_id")
        source_hash = item.get("source_sha256")
        relative_video = item.get("relative_video")
        if (
            not isinstance(relative_video, str) or not relative_video
            or not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
            or source_id != source_identity(source_hash, Path(relative_video).stem)
            or source_id in seen
            or item.get("disposition") not in {"PROCESSED", "SKIPPED_INTACT", "RUNNING", "PENDING"}
        ):
            raise RuntimeError("Cannot resume batch: invalid or duplicate original source identity.")
        seen.add(source_id)
    return checkpoint


def verify_completed_batch_item(item: dict[str, Any], checkpoint: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Verify completed bytes and provenance without reevaluating quality or approval."""
    stem = plan["video"].stem
    output = PROJECT_ROOT / "output" / plan["output_dir_name"]
    original_batch_id = item.get("original_batch_id", checkpoint["batch_id"])
    if (
        item.get("status") not in {"PASS", "REVIEW", "FAIL"}
        or not item.get("completed_at") or item.get("timed_out") is True
        or item.get("source_sha256") != plan["source_sha256"]
        or item.get("output_dir") != relative_display(output)
        or not isinstance(original_batch_id, str) or RUN_ID_PATTERN.fullmatch(original_batch_id) is None
    ):
        raise RuntimeError(f"Cannot preserve completed {stem}: original completion or source binding is invalid.")
    runs_root = (output / "runs").resolve()
    executions = []
    for path in runs_root.glob("*/execution.json"):
        if not path.resolve().is_relative_to(runs_root):
            continue
        execution = read_json(path, {})
        if (
            RUN_ID_PATTERN.fullmatch(path.parent.name) is not None
            and execution.get("run_id") == path.parent.name
            and execution.get("batch_id") == original_batch_id
            and execution.get("completed_at")
        ):
            executions.append(execution)
    if not executions:
        raise RuntimeError(f"Cannot preserve completed {stem}: original completed execution is missing.")
    execution = max(executions, key=lambda value: value["completed_at"])
    run_id = execution["run_id"]
    run_root = runs_root / run_id
    if item["status"] == "FAIL":
        candidate = PROJECT_ROOT / "failed" / plan["output_dir_name"] / run_id / f"{stem}.glb"
        bundle = read_json(candidate.parent / "failure_bundle.json", {})
        if execution.get("status") != "FAIL" or bundle.get("conversion_run_id") != run_id or bundle.get("output_name") != plan["output_dir_name"]:
            raise RuntimeError(f"Cannot preserve completed {stem}: failed artifact is not bound to its original run.")
        evidence_paths = load_failure_bundle(candidate)
        source_path = evidence_paths["source_video"]
        artifact_hash = sha256_file(candidate)
    else:
        metadata = read_json(output / f"{stem}.metadata.json", {})
        manifest = read_json(output / f"{stem}.release.json", {})
        if metadata.get("run_id") != run_id or manifest.get("run_id") != run_id or metadata.get("technical_qc") != item["status"]:
            raise RuntimeError(f"Cannot preserve completed {stem}: published artifact is not bound to its original run.")
        candidate = _verified_bundle_record(manifest.get("artifact"), run_root, "completed artifact")
        if candidate != (run_root / f"{stem}.glb").resolve():
            raise RuntimeError(f"Cannot preserve completed {stem}: unexpected original artifact path.")
        evidence = metadata.get("release_evidence", {})
        source_path = _verified_bundle_record(evidence.get("source_video"), run_root, "completed source")
        run_glb = _verified_bundle_record(evidence.get("run_glb"), run_root, "completed run GLB")
        stable_glb = _verified_bundle_record(evidence.get("stable_glb"), output.resolve(), "completed stable GLB")
        artifact_hash = sha256_file(candidate)
        if (
            run_glb != candidate or stable_glb != (output / f"{stem}.glb").resolve()
            or sha256_file(stable_glb) != artifact_hash
            or metadata.get("file_integrity", {}).get("glb_sha256") != artifact_hash
        ):
            raise RuntimeError(f"Cannot preserve completed {stem}: original GLB hashes or paths differ.")
    if sha256_file(source_path) != plan["source_sha256"]:
        raise RuntimeError(f"Cannot preserve completed {stem}: original artifact belongs to another source.")
    return {
        "original_batch_id": original_batch_id,
        "original_context_fingerprint": item.get("original_context_fingerprint", checkpoint.get("context_fingerprint")),
        "run_id": run_id,
        "preserved_artifact": {"path": relative_display(candidate), "sha256": artifact_hash},
    }


def run_batch(args: argparse.Namespace) -> int:
    """Run a resumable, bounded batch without sharing mutable process state."""

    if not args.input_dir:
        raise SystemExit("--input-dir is required with --batch.")
    batch_id = uuid.uuid4().hex
    batch_lock = acquire_output_lock(PROJECT_ROOT / "output" / ".batch_control", batch_id)
    try:
        return _run_batch_locked(args, batch_id)
    finally:
        release_output_lock(batch_lock)


def render_review_debug(
    glb: Path, source_video: Path, working_video: Path, fps: float,
    blender_path: Path, debug_dir: Path, run_id: str,
) -> dict[str, Path]:
    """Use the existing renderer/comparison path for a hash-bound inspection video."""
    if not math.isfinite(float(fps)) or fps <= 0:
        raise ValueError("Review rendering requires a positive source FPS.")
    stem = source_video.stem
    paths = {"avatar_preview": debug_dir / f"{stem}_avatar_preview.mp4",
             "comparison_video": debug_dir / f"{stem}_source_avatar_comparison.mp4",
             "source_avatar_validation": debug_dir / f"{stem}.source_avatar_validation.json"}

    def generate():
        print(f"Rendering source/avatar review: {source_video.name}", flush=True)
        run_blender_render_animation(blender_path, glb, fps, paths["avatar_preview"])
        report = create_source_avatar_comparison(working_video, paths["avatar_preview"], paths["comparison_video"])
        report.update(validation_run_id=run_id, source_video_sha256=sha256_file(source_video),
                      working_video_sha256=sha256_file(working_video), validated_glb_sha256=sha256_file(glb),
                      comparison_video_sha256=sha256_file(paths["comparison_video"]), inspection_only=True)
        atomic_write_json(paths["source_avatar_validation"], report)
        validate_review_debug(paths, glb=glb, source_video=source_video, run_id=run_id,
                              working_video=working_video)

    StageCache(PROJECT_ROOT / "temp" / "stage_cache" / "review_debug" / run_id).execute(
        "comparison", inputs={"glb": glb, "source_video": source_video, "working_video": working_video,
                              "blender": blender_path,
                              "renderer": PROJECT_ROOT / "src/blender/blender_render_animation.py",
                              "utils": PROJECT_ROOT / "src/blender/blender_utils.py",
                              "comparison": PROJECT_ROOT / "src/qc/source_avatar_comparison.py"},
        settings={"fps": fps, "run_id": run_id, "original_filename": source_video.name,
                  "review_evidence_schema": 1},
        outputs=paths, action=generate,
    )
    # Cached bytes are hash checked by StageCache; still verify their decoded
    # contents and binding before advertising an inspection video.
    validate_review_debug(paths, glb=glb, source_video=source_video, run_id=run_id,
                          working_video=working_video)
    return paths


def validate_review_debug(
    paths: dict[str, Path], *, glb: Path, source_video: Path, run_id: str,
    working_video: Path | None = None,
) -> None:
    """Require complete, decodable comparison evidence for this conversion."""
    if "comparison_video" not in paths or "source_avatar_validation" not in paths:
        raise RuntimeError("Review comparison and its validation report are both required.")
    report = read_json(paths["source_avatar_validation"], {})
    expected = report.get("expected_comparison_frame_count")
    if (report.get("status") not in {"PASS", "REVIEW"}
            or report.get("comparison_complete") is not True
            or isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0
            or any(report.get(name) != expected for name in (
                "source_frame_count", "avatar_frame_count", "comparison_frame_count",
                "comparison_reopened_frame_count"))):
        raise RuntimeError("Review comparison is empty or incomplete; see its validation report.")
    bindings = {"validation_run_id": run_id, "source_video_sha256": sha256_file(source_video),
                "comparison_video_sha256": sha256_file(paths["comparison_video"])}
    # Historical release evidence already binds its report to the run GLB.
    # New reports additionally carry the GLB hash explicitly.
    if "validated_glb_sha256" in report:
        bindings["validated_glb_sha256"] = sha256_file(glb)
    if working_video is not None:
        bindings["working_video_sha256"] = sha256_file(working_video)
    if any(not value or report.get(key) != value for key, value in bindings.items()):
        raise RuntimeError("Review comparison source, GLB, run or video hash binding differs.")
    count, fps, timestamps = _decoded_video_timing(paths["comparison_video"])
    expected_fps = report.get("source_fps")
    if (isinstance(expected_fps, bool) or not isinstance(expected_fps, (int, float))
            or not math.isfinite(expected_fps) or expected_fps <= 0
            or not math.isfinite(fps) or fps <= 0 or abs(fps - expected_fps) > 0.001
            or count != expected):
        raise RuntimeError("Review comparison decoded frame count or FPS differs from its report.")
    timing_error = _timeline_error(timestamps, fps)
    if timing_error is None or timing_error > 0.0011:
        raise RuntimeError("Review comparison decoded timeline is incomplete or inconsistent.")


def review_debug_paths(
    *, original: Path, source_video: Path, run_root: Path, run_id: str,
    metadata: dict | None, evidence: dict[str, Path] | None,
) -> dict[str, Path]:
    """Reuse immutable comparisons; backfill older failed runs without reconversion."""
    paths = {}
    if metadata is not None:
        records = metadata.get("release_evidence", {})
        for role in ("comparison_video", "source_avatar_validation"):
            if role in records:
                paths[role] = _verified_bundle_record(records[role], run_root.resolve(), f"review {role}")
    elif evidence:
        paths = {role: evidence[role] for role in
                 ("comparison_video", "avatar_preview", "source_avatar_validation") if role in evidence}
    if "comparison_video" in paths:
        validate_review_debug(paths, glb=original, source_video=source_video, run_id=run_id,
                              working_video=(evidence or {}).get("working_video"))
        return paths
    if not evidence or "video_preparation" not in evidence or "working_video" not in evidence:
        raise RuntimeError("No bound comparison or prepared working video for review rendering.")
    preparation = read_json(evidence["video_preparation"], {})
    config = load_yaml(PROJECT_ROOT / "config/settings.yaml")
    blender = resolve_project_path(config.get("blender", {}).get("executable") or default_blender_path())
    return render_review_debug(original, source_video, evidence["working_video"],
                               float(preparation.get("working", {}).get("fps", 0)),
                               blender, run_root / "review_debug", run_id)


def attach_batch_review(
    item: dict[str, Any], plan: dict[str, Any], summary: dict[str, Any],
    verified: dict[str, Any] | None = None,
) -> None:
    """Expose verified candidates for inspection without changing technical QC."""
    item.update(technical_qc=item["status"], review_available=False, review_status="UNAVAILABLE",
                debug_available=False)
    item.pop("review_delivery", None)
    item.pop("review_error", None)
    item.pop("debug_error", None)
    try:
        preserved = verified or verify_completed_batch_item(item, summary, plan)
        output = PROJECT_ROOT / "output" / plan["output_dir_name"]
        run_id = preserved["run_id"]
        run_root = output / "runs" / run_id
        original = PROJECT_ROOT / preserved["preserved_artifact"]["path"]
        execution = read_json(run_root / "execution.json", {})
        metadata = None
        evidence = None
        if item["status"] == "FAIL":
            evidence = load_failure_bundle(original)
            validation_path = evidence["glb_validation"]
        else:
            metadata = read_json(output / f"{plan['video'].stem}.metadata.json", {})
            validation_path = _verified_bundle_record(
                metadata.get("release_evidence", {}).get("glb_validation"),
                run_root.resolve(), "review GLB validation",
            )
        try:
            debug_paths = review_debug_paths(original=original, source_video=plan["video"],
                                            run_root=run_root, run_id=run_id, metadata=metadata, evidence=evidence)
            item.pop("debug_error", None)
        except Exception as debug_exc:
            debug_paths = {}
            item["debug_error"] = f"{type(debug_exc).__name__}: {debug_exc}"
        delivery = write_review_delivery(
            project_root=PROJECT_ROOT,
            delivery_dir=PROJECT_ROOT / "output" / "review" / plan["output_dir_name"] / run_id,
            source_video=plan["video"], original_glb=original, run_id=run_id,
            batch_id=summary["batch_id"], technical_qc=item["status"],
            validation=read_json(validation_path, {}), metadata=metadata,
            evidence=evidence, execution=execution,
            context_fingerprint=preserved.get("original_context_fingerprint"),
            debug_paths=debug_paths,
        )
        item.update(preserved)
        item.update(review_available=True, review_status=delivery["review_status"], review_delivery=delivery)
        item["debug_available"] = delivery.get("review_debug", {}).get("comparison_available") is True
    except Exception as exc:
        # A broken/missing export or evidence is not a reviewable success.
        # Preserve the exact blocker and keep converting unrelated inputs.
        item["review_error"] = f"{type(exc).__name__}: {exc}"


def _run_batch_locked(args: argparse.Namespace, batch_id: str) -> int:

    if not args.input_dir:
        raise SystemExit("--input-dir is required with --batch.")
    resume_batch_id = getattr(args, "resume_batch", None)
    if resume_batch_id and not getattr(args, "batch_resume", True):
        raise SystemExit("--resume-batch cannot be combined with --no-batch-resume.")
    checkpoint = load_batch_checkpoint(resume_batch_id) if resume_batch_id else None
    checkpoint_items = {item["source_id"]: item for item in checkpoint["items"]} if checkpoint else {}
    if getattr(args, "signer_approval", None):
        raise SystemExit(
            "--signer-approval is not accepted in batch conversion; apply one hash-bound approval per asset."
        )

    workers = getattr(args, "batch_workers", 1)
    timeout_seconds = getattr(args, "batch_timeout_seconds", 3600.0)
    retries = getattr(args, "batch_retries", 1)
    failure_threshold = getattr(args, "batch_failure_threshold", 0)
    if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int) or failure_threshold < 0:
        raise SystemExit("--batch-failure-threshold must be a nonnegative integer.")
    pause_file = resolve_project_path(getattr(args, "batch_pause_file", "./output/.batch_pause"))
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_BATCH_WORKERS:
        raise SystemExit(f"--batch-workers must be between 1 and {MAX_BATCH_WORKERS}.")
    if not 0 < float(timeout_seconds) <= MAX_BATCH_TIMEOUT_SECONDS:
        raise SystemExit(
            f"--batch-timeout-seconds must be greater than zero and at most "
            f"{MAX_BATCH_TIMEOUT_SECONDS:g}."
        )
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_BATCH_RETRIES:
        raise SystemExit(f"--batch-retries must be between 0 and {MAX_BATCH_RETRIES}.")

    input_dir = resolve_project_path(args.input_dir).resolve()
    config_path = resolve_project_path(args.config)
    qc_thresholds_path = resolve_project_path(args.qc_thresholds)
    config = load_yaml(config_path)
    uppercase_output_dir = bool(config.get("output", {}).get("uppercase_output_dir", True))
    avatar_value = getattr(args, "avatar", None) or config.get("avatar", {}).get("path")
    model_value = config.get("pose", {}).get("model_path")
    if not avatar_value or not model_value:
        raise SystemExit("Batch configuration must define avatar.path and pose.model_path.")
    avatar_path = resolve_project_path(avatar_value)
    model_path = resolve_project_path(model_value)
    shared_inputs = {
        "config": config_path,
        "QC thresholds": qc_thresholds_path,
        "avatar": avatar_path,
        "pose model": model_path,
    }
    missing_shared = [name for name, path in shared_inputs.items() if not path.is_file()]
    if missing_shared:
        raise SystemExit("Missing batch dependency: " + ", ".join(missing_shared))

    videos = discover_mp4_files(
        input_dir,
        recursive=bool(getattr(args, "batch_recursive", False)),
        excluded_roots=(
            PROJECT_ROOT / "output",
            PROJECT_ROOT / "failed",
            PROJECT_ROOT / "temp",
            PROJECT_ROOT / ".git",
        ),
    )
    if not videos:
        raise SystemExit(f"No MP4 files found in {input_dir}")
    trusted_signers_arg = getattr(args, "trusted_signers", "./config/trusted_signers.json")
    trusted_signers_path = resolve_project_path(trusted_signers_arg)
    motion_catalog_value = getattr(args, "motion_catalog", None)
    motion_catalog_path = resolve_project_path(motion_catalog_value) if motion_catalog_value else None
    context_options = dict(
        config_path=config_path,
        qc_thresholds_path=qc_thresholds_path,
        avatar_path=avatar_path,
        model_path=model_path,
        motion_catalog_path=motion_catalog_path,
    )
    context_fingerprint = build_batch_context_fingerprint(**context_options)
    avatar_sha256 = sha256_file(avatar_path)
    summary_path = PROJECT_ROOT / "output" / "batch_summary.json"
    batch_dir = PROJECT_ROOT / "output" / "batch_runs" / batch_id
    index_path = PROJECT_ROOT / "output" / "batch_index.json"
    index = read_json(index_path, {"schema_version": "1.0", "sources": {}})
    if not isinstance(index, dict) or not isinstance(index.get("sources"), dict):
        raise SystemExit("Batch source index is invalid; restore the last intact batch_index.json.")
    source_records = index["sources"]
    plans: list[dict[str, Any]] = []
    seen_sources: dict[str, str] = {}
    duplicate_inputs: list[dict[str, str]] = []
    for index, video in enumerate(videos, start=1):
        relative_video = video.relative_to(input_dir).as_posix()
        source_hash = sha256_file(video)
        identity = source_identity(source_hash, video.stem)
        if identity in seen_sources:
            duplicate_inputs.append({"video": relative_video, "same_as": seen_sources[identity]})
            continue
        seen_sources[identity] = relative_video
        output_dir_name = choose_output_directory(
            identity=identity, artifact_stem=video.stem, source_sha256=source_hash,
            uppercase=uppercase_output_dir, records=source_records,
            output_root=PROJECT_ROOT / "output",
        )
        previous = source_records.get(identity, {})
        previous = previous if isinstance(previous, dict) else {}
        source_records[identity] = {
            **previous, "source_id": identity, "source_sha256": source_hash,
            "artifact_stem": video.stem, "output_dir_name": output_dir_name,
        }
        resume_fingerprint = batch_resume_fingerprint(
            relative_video=relative_video,
            source_sha256=source_hash,
            output_dir_name=output_dir_name,
            context_fingerprint=context_fingerprint,
        )
        plans.append(
            {
                "index": index,
                "video": video,
                "relative_video": relative_video,
                "source_sha256": source_hash,
                "source_id": identity,
                "output_dir_name": output_dir_name,
                "resume_fingerprint": resume_fingerprint,
            }
        )

    if checkpoint and set(checkpoint_items) != {plan["source_id"] for plan in plans}:
        raise RuntimeError("Cannot resume batch: the original input set is missing, changed, or has additional sources.")

    summary: dict[str, Any] = {
        "schema_version": "3.0",
        "batch_id": batch_id,
        "input_root": str(input_dir),
        "context_fingerprint": context_fingerprint,
        "created_at": utc_now_iso(),
        "pause_file": str(pause_file),
        "failure_threshold": failure_threshold,
        "pause_reason": None,
        "input_count": len(videos),
        "duplicate_inputs": duplicate_inputs,
        "recursive": bool(getattr(args, "batch_recursive", False)),
        "resume_enabled": bool(getattr(args, "batch_resume", True)),
        "workers": workers,
        "timeout_seconds": float(timeout_seconds),
        "retries": retries,
        "total": len(plans),
        "pass": 0,
        "review": 0,
        "fail": 0,
        "production_ready": 0,
        "pending_signer_review": 0,
        "processed": 0,
        "skipped": 0,
        "pending": len(plans),
        "timed_out": 0,
        "retried": 0,
        "batch_status": "RUNNING",
        "items": [],
        "review_directory": relative_display(PROJECT_ROOT / "output" / "review"),
    }
    item_by_key: dict[str, dict[str, Any]] = {}
    if checkpoint:
        summary["resumed_from_batch_id"] = resume_batch_id
    jobs: list[BatchJob] = []
    for plan in plans:
        video = plan["video"]
        relative_video = plan["relative_video"]
        item = {
            "video": str(video),
            "relative_video": relative_video,
            "source_id": plan["source_id"],
            "source_sha256": plan["source_sha256"],
            "output_dir": relative_display(PROJECT_ROOT / "output" / plan["output_dir_name"]),
            "resume_fingerprint": plan["resume_fingerprint"],
            "disposition": "PENDING",
            "status": "PENDING",
            "isl_verified": False,
            "production_status": "NOT_ELIGIBLE",
            "production_eligible": False,
            "engineering_candidate": False,
            "release_integrity_reasons": [],
            "returncode": None,
            "timed_out": False,
            "attempt_count": 0,
            "attempts": [],
        }
        summary["items"].append(item)
        item_by_key[relative_video] = item

        original = checkpoint_items.get(plan["source_id"])
        if original and original["disposition"] in {"PROCESSED", "SKIPPED_INTACT"}:
            preserved = verify_completed_batch_item(original, checkpoint, plan)
            item.update(deepcopy(original))
            item.update(preserved)
            item.update(video=str(video), relative_video=relative_video, disposition="SKIPPED_INTACT")
            attach_batch_review(item, plan, summary, preserved)
            if item["review_available"]:
                source_records[plan["source_id"]].update(
                    review_resume_fingerprint=plan["resume_fingerprint"], review_batch_id=batch_id,
                )
            print(f"Preserved {relative_video}: technical={item['status']}, review={item['review_status']}", flush=True)
            continue

        previous = source_records.get(plan["source_id"])
        if (
            checkpoint is None and bool(getattr(args, "batch_resume", True))
            and isinstance(previous, dict)
            and previous.get("review_resume_fingerprint") == plan["resume_fingerprint"]
            and previous.get("review_batch_id")
        ):
            review_checkpoint = load_batch_checkpoint(previous["review_batch_id"])
            prior_item = next((entry for entry in review_checkpoint["items"]
                               if entry["source_id"] == plan["source_id"]), None)
            if prior_item is None:
                raise RuntimeError(f"Missing completed review checkpoint for {relative_video}.")
            preserved = verify_completed_batch_item(prior_item, review_checkpoint, plan)
            item.update(deepcopy(prior_item))
            item.update(preserved)
            item.update(video=str(video), relative_video=relative_video, disposition="SKIPPED_INTACT")
            attach_batch_review(item, plan, summary, preserved)
            if item["review_available"]:
                previous.update(review_batch_id=batch_id)
            print(f"Preserved {relative_video}: technical={item['status']}, review={item['review_status']}", flush=True)
            continue
        can_resume = (
            bool(getattr(args, "batch_resume", True))
            and isinstance(previous, dict)
            and previous.get("resume_fingerprint") == plan["resume_fingerprint"]
            and previous.get("output_dir_name") == plan["output_dir_name"]
        )
        if can_resume and checkpoint is None:
            resumed = classify_batch_artifact(
                video=video,
                output_dir_name=plan["output_dir_name"],
                expected_source_sha256=plan["source_sha256"],
                expected_avatar_sha256=avatar_sha256,
                trusted_signers_path=trusted_signers_path,
                process_result=None,
            )
            if resumed["production_eligible"] or resumed["engineering_candidate"]:
                item.update(resumed)
                item["disposition"] = "SKIPPED_INTACT"
                try:
                    prior_checkpoint = load_batch_checkpoint(previous.get("last_batch_id"))
                    prior_item = next(entry for entry in prior_checkpoint["items"]
                                      if entry["source_id"] == plan["source_id"])
                    preserved = verify_completed_batch_item(prior_item, prior_checkpoint, plan)
                    item.update(completed_at=prior_item["completed_at"], **preserved)
                    attach_batch_review(item, plan, summary, preserved)
                    if item["review_available"]:
                        previous.update(review_resume_fingerprint=plan["resume_fingerprint"], review_batch_id=batch_id)
                except (Exception, SystemExit) as exc:
                    item.update(technical_qc=item["status"], review_available=False,
                                review_status="UNAVAILABLE", review_error=str(exc))
                continue

        cmd = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "convert.py"),
            "--video",
            str(video),
            "--config",
            str(config_path),
            "--qc-thresholds",
            str(qc_thresholds_path),
            "--output-dir-name",
            plan["output_dir_name"],
            "--expected-context-fingerprint",
            context_fingerprint,
            "--batch-id",
            batch_id,
        ]
        if getattr(args, "avatar", None):
            cmd.extend(["--avatar", str(avatar_path)])
        if motion_catalog_path is not None:
            cmd.extend(["--motion-catalog", str(motion_catalog_path)])
        cmd.extend(["--trusted-signers", str(trusted_signers_path)])
        if getattr(args, "save_debug", False):
            cmd.append("--save-debug")
        if getattr(args, "require_production", False):
            cmd.append("--require-production")
        jobs.append(
            BatchJob(
                key=relative_video,
                command=tuple(cmd),
                log_prefix=(
                    f"{plan['index']:04d}_{safe_log_component(video.stem)}_"
                    f"{plan['resume_fingerprint'][:8]}"
                ),
                context_fingerprint=context_fingerprint,
            )
        )

    plan_by_key = {plan["relative_video"]: plan for plan in plans}
    context_changed = False
    checked_context_stats: tuple[Any, ...] | None = None

    def context_is_current() -> bool:
        nonlocal context_changed, checked_context_stats
        if context_changed:
            return False
        # Stat checks are cheap enough for pause polling. Rehash all bytes whenever
        # the file set, size, or write time changes; workers also verify at stages.
        context_paths = batch_context_paths(**context_options)
        stats = tuple(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns) if path.is_file()
            else (str(path), None, None) for path in context_paths
        )
        if stats != checked_context_stats:
            context_changed = build_batch_context_fingerprint(**context_options) != context_fingerprint
            checked_context_stats = stats
        return not context_changed

    def persist() -> None:
        refresh_batch_summary(summary)
        atomic_write_json(batch_dir / "summary.json", summary)
        atomic_write_json(summary_path, summary)
        atomic_write_json(index_path, {"schema_version": "1.0", "sources": source_records})

    def record_result(result: BatchProcessResult) -> None:
        plan = plan_by_key[result.key]
        item = item_by_key[result.key]
        item.update(
            classify_batch_artifact(
                video=plan["video"],
                output_dir_name=plan["output_dir_name"],
                expected_source_sha256=plan["source_sha256"],
                expected_avatar_sha256=avatar_sha256,
                trusted_signers_path=trusted_signers_path,
                process_result=result,
            )
        )
        if not context_is_current():
            item.update(status="FAIL", engineering_candidate=False, production_eligible=False,
                        failure_category="CONTEXT_CHANGED")
            item["release_integrity_reasons"].append("Pipeline context changed during the batch.")
        item["disposition"] = "PROCESSED"
        item["completed_at"] = utc_now_iso()
        record = source_records[plan["source_id"]]
        record.update(last_batch_id=batch_id, last_status=item["status"])
        if item["engineering_candidate"] or item["production_eligible"]:
            record["resume_fingerprint"] = plan["resume_fingerprint"]
        attach_batch_review(item, plan, summary)
        if item["review_available"] and not context_changed:
            record.update(review_resume_fingerprint=plan["resume_fingerprint"], review_batch_id=batch_id)
        persist()
        print(f"[{summary['processed'] + summary['skipped']}/{summary['total']}] "
              f"{result.key}: technical={item['status']}, review={item['review_status']}", flush=True)

    def should_pause() -> str | None:
        if not context_is_current():
            return "CONTEXT_CHANGED: pipeline code or shared inputs changed; drain and rerun after the fix."
        if pause_file.exists():
            return f"REQUESTED: remove {pause_file} and rerun the batch to resume."
        if failure_threshold:
            for group in group_failures(summary["items"]):
                failures = sum(
                    item["status"] == "FAIL" and item.get("failure_category") == group["category"]
                    for item in summary["items"]
                )
                if failures >= failure_threshold:
                    return f"REPEATED_FAILURE: {group['category']} failed {failures} times; inspect before resuming."
        return None

    def record_pause(reason: str) -> None:
        summary["pause_reason"] = reason
        persist()

    def record_dispatch(job: BatchJob) -> None:
        item_by_key[job.key]["disposition"] = "RUNNING"
        item_by_key[job.key]["started_at"] = utc_now_iso()
        persist()
        print(f"Starting {job.key} ({summary['running']} worker(s) active)", flush=True)

    persist()
    try:
        run_isolated_jobs(
            jobs,
            cwd=PROJECT_ROOT,
            log_dir=batch_dir / "logs",
            max_workers=workers,
            timeout_seconds=float(timeout_seconds),
            retries=retries,
            on_result=record_result,
            should_pause=should_pause,
            on_pause=record_pause,
            on_dispatch=record_dispatch,
        )
    except BaseException as exc:
        summary["pause_reason"] = f"CONTROLLER_ERROR: {type(exc).__name__}: {exc}"
        raise
    finally:
        # Unexpected controller interruption must never leave RUNNING as a final state.
        for item in summary["items"]:
            if item["disposition"] == "RUNNING":
                item["disposition"] = "PENDING"
        summary["finished_at"] = utc_now_iso()
        refresh_batch_summary(summary, final=True)
        atomic_write_json(batch_dir / "summary.json", summary)
        atomic_write_json(summary_path, summary)
        atomic_write_json(index_path, {"schema_version": "1.0", "sources": source_records})

    print(
        json.dumps(
            {
                key: summary[key]
                for key in [
                    "total",
                    "pass",
                    "review",
                    "fail",
                    "processed",
                    "skipped",
                    "production_ready",
                    "pending_signer_review",
                    "batch_status",
                    "pending",
                    "pause_reason",
                    "failure_groups",
                    "review_available",
                    "review_unavailable",
                    "review_batch_status",
                    "review_directory",
                ]
            },
            indent=2,
        )
    )
    if summary["batch_status"] == "PAUSED":
        return 3
    if summary["fail"] or summary["review_unavailable"]:
        return 1
    if getattr(args, "require_production", False) and summary["production_ready"] != summary["total"]:
        return 2
    if any(
        item["returncode"] == 2 or not item["engineering_candidate"]
        for item in summary["items"]
    ):
        return 2
    return 0


def classify_batch_artifact(
    *,
    video: Path,
    output_dir_name: str,
    expected_source_sha256: str | None,
    expected_avatar_sha256: str | None,
    trusted_signers_path: Path,
    process_result: BatchProcessResult | None,
) -> dict[str, Any]:
    """Classify only evidence-bound output; process success alone never passes."""

    output_dir = PROJECT_ROOT / "output" / output_dir_name
    metadata_path = output_dir / f"{video.stem}.metadata.json"
    qc_path = output_dir / f"{video.stem}.qc.json"
    metadata = read_json(metadata_path, {})
    qc = read_json(qc_path, {})
    reasons: list[str] = []
    qc_status = str(qc.get("technical_qc") or "FAIL").upper()
    metadata_qc = str(metadata.get("technical_qc") or "FAIL").upper()
    if qc_status not in {"PASS", "REVIEW", "FAIL"}:
        reasons.append("QC report has no valid technical_qc status.")
        qc_status = "FAIL"
    if metadata_qc != qc_status:
        reasons.append("Mutable QC report does not match conversion metadata.")

    production = metadata.get("production")
    production = production if isinstance(production, dict) else {}
    production_status = str(production.get("production_status") or "NOT_ELIGIBLE")
    try:
        production_eligible, release_reasons = evaluate_published_release(
            output_dir,
            video.stem,
            metadata,
            trusted_signers_path=trusted_signers_path,
        )
    except Exception as exc:
        production_eligible, release_reasons = False, [
            f"Published release evaluation failed: {exc}"
        ]
    reasons.extend(release_reasons)
    try:
        engineering_candidate = evaluate_published_engineering_candidate(
            output_dir,
            video.stem,
            metadata,
        )
    except Exception as exc:
        engineering_candidate = False
        reasons.append(f"Engineering candidate evaluation failed: {exc}")

    evidence = metadata.get("release_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    source_record = evidence.get("source_video")
    avatar_record = evidence.get("avatar")
    source_record = source_record if isinstance(source_record, dict) else {}
    avatar_record = avatar_record if isinstance(avatar_record, dict) else {}
    current_source_sha256 = sha256_file(video)
    if not expected_source_sha256 or current_source_sha256 != expected_source_sha256:
        reasons.append("Source video changed during batch processing.")
    if source_record.get("sha256") != expected_source_sha256:
        reasons.append("Published evidence is not bound to this source video.")
    if avatar_record.get("sha256") != expected_avatar_sha256:
        reasons.append("Published evidence is not bound to the configured avatar.")

    returncode = process_result.returncode if process_result is not None else 0
    timed_out = process_result.timed_out if process_result is not None else False
    attempts = (
        [attempt.to_json_dict() for attempt in process_result.attempts]
        if process_result is not None
        else []
    )
    if returncode not in {0, 2}:
        reasons.append(f"Conversion process exited with code {returncode}.")
    if timed_out:
        reasons.append("The final conversion attempt timed out.")
    if process_result is not None and process_result.error:
        reasons.append(process_result.error)

    input_binding_pass = not any(
        reason.startswith(("Source video", "Published evidence")) for reason in reasons
    )
    verified_candidate = bool(
        returncode in {0, 2}
        and input_binding_pass
        and not any("QC report" in reason for reason in reasons)
        and (production_eligible or engineering_candidate)
    )
    if verified_candidate and qc_status == "PASS":
        status = "PASS"
    elif returncode in {0, 2} and input_binding_pass and qc_status == "REVIEW":
        status = "REVIEW"
        production_eligible = False
        engineering_candidate = False
    else:
        status = "FAIL"
        production_eligible = False
        engineering_candidate = False

    isl_validation = metadata.get("isl_validation")
    isl_validation = isl_validation if isinstance(isl_validation, dict) else {}
    quality_reasons: list[str] = []
    if process_result is not None and returncode not in {0, 2} and process_result.attempts:
        diagnostic = batch_failure_log_summary(process_result.attempts[-1].log_path)
        if diagnostic:
            quality_reasons.append(diagnostic)
    for report in (qc, metadata.get("glb_validation"), metadata.get("source_avatar_validation")):
        if not isinstance(report, dict):
            continue
        for key in ("reasons", "fail_reasons", "review_reasons"):
            values = report.get(key)
            if isinstance(values, list):
                quality_reasons.extend(value for value in values if isinstance(value, str))
    if process_result is not None and process_result.error:
        quality_reasons.append(process_result.error)
    if not quality_reasons and status in {"FAIL", "REVIEW"}:
        quality_reasons.extend(reasons)
    return {
        "status": status,
        "failure_category": (
            process_result.attempts[-1].failure_category
            if process_result is not None and process_result.attempts
            and process_result.attempts[-1].failure_category
            else ("VALIDATION_FAILED" if status == "FAIL" else (
                "QUALITY_REVIEW" if status == "REVIEW" else None
            ))
        ),
        "isl_verified": isl_validation.get("isl_verified") is True,
        "production_status": production_status,
        "production_eligible": production_eligible is True,
        "engineering_candidate": engineering_candidate is True,
        "release_integrity_reasons": list(dict.fromkeys(reasons)),
        "quality_reasons": list(dict.fromkeys(quality_reasons)),
        "returncode": returncode,
        "timed_out": timed_out,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def build_batch_context_fingerprint(
    *,
    config_path: Path,
    qc_thresholds_path: Path,
    avatar_path: Path,
    model_path: Path,
    motion_catalog_path: Path | None,
) -> str:
    """Bind resume decisions to code, settings, model, avatar, and catalog bytes."""

    paths = batch_context_paths(
        config_path=config_path, qc_thresholds_path=qc_thresholds_path,
        avatar_path=avatar_path, model_path=model_path, motion_catalog_path=motion_catalog_path,
    )
    records = [
        {
            "path": relative_display(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size if path.is_file() else None,
        }
        for path in paths
    ]
    payload = json.dumps({"files": records, "runtime_versions": runtime_versions()}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def batch_context_paths(
    *, config_path: Path, qc_thresholds_path: Path, avatar_path: Path,
    model_path: Path, motion_catalog_path: Path | None,
) -> list[Path]:
    paths = [
        PROJECT_ROOT / "convert.py",
        PROJECT_ROOT / "requirements.txt",
        PROJECT_ROOT / "package-lock.json",
        config_path,
        qc_thresholds_path,
        avatar_path,
        model_path,
    ]
    paths.extend(sorted((PROJECT_ROOT / "src").rglob("*.py"), key=lambda path: str(path).casefold()))
    if motion_catalog_path is not None:
        paths.append(motion_catalog_path)
    return paths


def batch_resume_fingerprint(
    *,
    relative_video: str,
    source_sha256: str | None,
    output_dir_name: str,
    context_fingerprint: str,
) -> str:
    payload = json.dumps(
        {
            "source_id": source_identity(source_sha256, Path(relative_video).stem),
            "source_sha256": source_sha256,
            "output_dir_name": output_dir_name,
            "context_fingerprint": context_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refresh_batch_summary(summary: dict[str, Any], *, final: bool = False) -> None:
    items = summary.get("items", [])
    summary["pass"] = sum(item.get("status") == "PASS" for item in items)
    summary["review"] = sum(item.get("status") == "REVIEW" for item in items)
    summary["fail"] = sum(item.get("status") == "FAIL" for item in items)
    summary["processed"] = sum(item.get("disposition") == "PROCESSED" for item in items)
    summary["skipped"] = sum(item.get("disposition") == "SKIPPED_INTACT" for item in items)
    summary["pending"] = sum(item.get("disposition") == "PENDING" for item in items)
    summary["running"] = sum(item.get("disposition") == "RUNNING" for item in items)
    summary["timed_out"] = sum(item.get("timed_out") is True for item in items)
    summary["retried"] = sum(int(item.get("attempt_count") or 0) > 1 for item in items)
    summary["production_ready"] = sum(item.get("production_eligible") is True for item in items)
    summary["pending_signer_review"] = sum(
        item.get("engineering_candidate") is True and item.get("production_eligible") is not True
        for item in items
    )
    summary["failure_groups"] = group_failures(items)
    summary["review_available"] = sum(item.get("review_available") is True for item in items)
    summary["review_unavailable"] = sum(
        item.get("disposition") in {"PROCESSED", "SKIPPED_INTACT"}
        and item.get("review_available") is not True for item in items
    )
    summary["review_batch_status"] = (
        "READY_FOR_REVIEW" if summary["review_available"] == summary["total"]
        else ("IN_PROGRESS" if summary["pending"] or summary["running"] else "INCOMPLETE")
    )
    if summary.get("pause_reason"):
        summary["batch_status"] = "DRAINING" if summary["running"] and not final else "PAUSED"
    elif not final and (summary["pending"] or summary["running"]):
        summary["batch_status"] = "RUNNING"
    elif summary["fail"]:
        summary["batch_status"] = "FAIL"
    elif summary["production_ready"] == summary["total"]:
        summary["batch_status"] = "APPROVED"
    elif summary["pass"] == summary["total"] and summary["pending_signer_review"]:
        summary["batch_status"] = "ENGINEERING_CANDIDATE"
    else:
        summary["batch_status"] = "REVIEW"


def safe_log_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (safe or "video")[:60]


def source_identity(source_sha256: str | None, artifact_stem: str) -> str:
    if not source_sha256 or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("A valid source SHA256 is required for batch identity.")
    return hashlib.sha256((source_sha256 + "\n" + artifact_stem).encode("utf-8")).hexdigest()


def choose_output_directory(
    *, identity: str, artifact_stem: str, source_sha256: str | None,
    uppercase: bool, records: dict[str, Any], output_root: Path,
) -> str:
    previous = records.get(identity)
    if isinstance(previous, dict) and previous.get("output_dir_name"):
        return validate_output_directory_name(previous["output_dir_name"])
    base = artifact_stem.upper() if uppercase else artifact_stem
    try:
        validate_output_directory_name(base)
    except SystemExit:
        base = safe_log_component(base)[:80] + "__" + identity[:16]
    reserved = {
        str(record.get("output_dir_name", "")).casefold()
        for key, record in records.items() if key != identity and isinstance(record, dict)
    }
    for candidate in (base, base[:80] + "__" + identity[:16], "VIDEO__" + identity):
        if candidate.casefold() in reserved:
            continue
        folder = output_root / candidate
        if folder.exists() and any(folder.iterdir()):
            metadata = read_json(folder / f"{artifact_stem}.metadata.json", {})
            if metadata.get("file_integrity", {}).get("source_video_sha256") != source_sha256:
                continue
        return validate_output_directory_name(candidate)
    raise RuntimeError(f"Cannot reserve an independent output directory for {artifact_stem}.")


def batch_failure_log_summary(log_path: Path) -> str | None:
    """Bounded diagnostic only; log text never grants release eligibility."""
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            lines = stream.read(65536).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("FAIL:"):
            return line[:2000]
    return None


def group_failures(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("status") not in {"FAIL", "REVIEW"}:
            continue
        category = str(item.get("failure_category") or item["status"])
        group = grouped.setdefault(category, {"category": category, "count": 0, "videos": [], "sample_reasons": []})
        group["count"] += 1
        group["videos"].append(item.get("relative_video") or item.get("video"))
        for reason in item.get("quality_reasons", []):
            if reason not in group["sample_reasons"] and len(group["sample_reasons"]) < 5:
                group["sample_reasons"].append(reason)
    return [grouped[key] for key in sorted(grouped)]


def validate_output_directory_name(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SystemExit("--output-dir-name must be a non-empty single path component.")
    if value in {".", ".."} or len(value) > 120:
        raise SystemExit("--output-dir-name is not a safe output component.")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise SystemExit("--output-dir-name contains a path separator or invalid character.")
    if value.endswith((".", " ")) or value.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        raise SystemExit("--output-dir-name is a reserved Windows output component.")
    return value


def revalidate_failed_outputs(args: argparse.Namespace) -> int:
    """Recover valid/reviewable GLBs after a validation-only defect is fixed."""
    config = load_yaml(resolve_project_path(args.config))
    blender_path = resolve_project_path(config.get("blender", {}).get("executable") or default_blender_path())
    failed_root = PROJECT_ROOT / "failed"
    input_dir = resolve_project_path(args.input_dir or "./input")
    input_videos = {video.stem.lower(): video for video in input_dir.glob("*.mp4")}
    signer_approval = (
        load_signer_approval(
            resolve_project_path(args.signer_approval),
            resolve_project_path(args.trusted_signers),
        )
        if args.signer_approval
        else None
    )
    summary = {
        "total": 0,
        "pass": 0,
        "review": 0,
        "fail": 0,
        "recovered": 0,
        "skipped_current_release": 0,
        "skipped_current_candidate": 0,
        "skipped_claimed_candidate": 0,
        "skipped_superseded_candidate": 0,
        "items": [],
    }
    failed_candidates = sorted(
        (
            path
            for path in failed_root.glob("**/*.glb")
            if "evidence" not in path.relative_to(failed_root).parts
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    seen_outputs: set[tuple[str, str]] = set()

    for failed_glb in failed_candidates:
        relative_parts = failed_glb.relative_to(failed_root).parts
        output_name = relative_parts[0]
        output_dir = PROJECT_ROOT / "output" / output_name
        gloss = failed_glb.stem
        output_key = (output_name.casefold(), gloss.casefold())
        if output_key in seen_outputs:
            summary["skipped_superseded_candidate"] += 1
            continue
        seen_outputs.add(output_key)
        summary["total"] += 1
        run_id = uuid.uuid4().hex
        run_dir = output_dir / "runs" / run_id
        stable_motion_path = output_dir / f"{gloss}.motion.npz"
        stable_pose_path = output_dir / f"{gloss}.pose.npz"
        validation_path = output_dir / f"{gloss}.glb_validation.json"
        khronos_validation_path = output_dir / f"{gloss}.khronos_validation.json"
        qc_path = output_dir / f"{gloss}.qc.json"
        metadata_path = output_dir / f"{gloss}.metadata.json"
        source_avatar_path = output_dir / f"{gloss}.source_avatar_validation.json"
        match_path = output_dir / f"{gloss}.match_validation.json"
        signer_review_path = output_dir / f"{gloss}.signer_review.json"
        release_manifest_path = output_dir / f"{gloss}.release.json"
        comparison_video_path = output_dir / "debug" / f"{gloss}_source_avatar_comparison.mp4"
        item: dict[str, Any] = {"video": relative_display(failed_glb), "status": "FAIL", "recovered": False}
        output_lock = acquire_output_lock(output_dir, run_id)
        status = "FAIL"
        try:
            if not failed_glb.is_file():
                item["status"] = "SKIPPED_ALREADY_CLAIMED"
                item["reason"] = "Another revalidator already claimed this failed candidate."
                summary["skipped_claimed_candidate"] += 1
                summary["items"].append(item)
                continue
            existing_metadata = read_json(metadata_path, {})
            current_release, _ = evaluate_published_release(
                output_dir,
                gloss,
                existing_metadata,
                trusted_signers_path=resolve_project_path(
                    getattr(args, "trusted_signers", "./config/trusted_signers.json")
                ),
            )
            if current_release:
                item["status"] = "SKIPPED_CURRENT_APPROVED"
                item["reason"] = "A verified current release already exists; historical failures cannot revoke it."
                summary["skipped_current_release"] += 1
                summary["items"].append(item)
                continue
            if evaluate_published_engineering_candidate(output_dir, gloss, existing_metadata):
                item["status"] = "SKIPPED_CURRENT_CANDIDATE"
                item["reason"] = "A verified newer engineering candidate exists; historical failures cannot revoke it."
                summary["skipped_current_candidate"] += 1
                summary["items"].append(item)
                continue

            bundle = load_failure_bundle(failed_glb)
            video_path = bundle["source_video"]
            avatar_path = bundle["avatar"]
            pose_path = bundle["pose"]
            motion_path = bundle["motion"]
            profile_path = bundle["avatar_profile"]
            bone_map_path = bundle["bone_map"]
            neutral_hand_pose_path = bundle["neutral_hand_pose"]
            preparation_path = bundle.get("video_preparation")
            working_video_path = bundle.get("working_video", video_path)
            bundle_config = load_yaml(bundle["settings"])
            thresholds = load_yaml(bundle["qc_thresholds"])
            invalidate_release_state(
                metadata_path=metadata_path,
                qc_path=qc_path,
                signer_review_path=signer_review_path,
                release_manifest_path=release_manifest_path,
                run_id=run_id,
                gloss=gloss,
            )
            validation = validate_existing_glb(
                blender_path,
                failed_glb,
                motion_path,
                profile_path,
                bone_map_path,
                validation_path,
                **({"ik_report_path": bundle["ik_report"]} if bundle.get("ik_report") else {}),
            )
            khronos_validation = run_khronos_validate(
                failed_glb,
                khronos_validation_path,
                bundle_config.get("validation", {}).get("khronos_warning_allowlist", {}),
            )
            validation = merge_khronos_validation(
                validation,
                khronos_validation,
                validation_path,
            )
            status = str(validation.get("status", "FAIL"))
            item["status"] = status
            output_glb = failed_glb
            immutable_glb = failed_glb
            if status not in {"PASS", "REVIEW"}:
                raise RuntimeError(f"GLB remains invalid: {validation.get('reasons')}")
            recovered_glb = output_dir / failed_glb.name
            immutable_glb = run_dir / failed_glb.name
            atomic_publish_file(failed_glb, immutable_glb)

            sequence = PoseSequence.load_npz(pose_path)
            tracking_qc = evaluate_tracking(sequence, thresholds)
            if tracking_qc.technical_qc not in {"PASS", "REVIEW"}:
                raise RuntimeError("Required tracking validation failed; recovered candidate was not published.")
            source_info, video_info = validate_recovery_video_timing(
                source_video=video_path, working_video=working_video_path,
                preparation_path=preparation_path, sequence=sequence,
            )
            candidate_preview = run_dir / "debug" / f"{gloss}_avatar_preview.mp4"
            candidate_comparison = run_dir / "debug" / f"{gloss}_source_avatar_comparison.mp4"
            run_blender_render_animation(
                blender_path,
                immutable_glb,
                video_info.fps,
                candidate_preview,
            )
            source_validation = create_source_avatar_comparison(
                working_video_path,
                candidate_preview,
                candidate_comparison,
            )
            comparison_generated = True
            source_validation["validation_run_id"] = run_id
            source_validation["source_video_sha256"] = sha256_file(video_path)
            source_validation["working_video_sha256"] = sha256_file(working_video_path)
            source_validation["original_source_frame_count"] = source_info.frame_count
            source_validation["timing_normalized"] = (
                read_json(preparation_path, {}).get("normalized") if preparation_path is not None else False
            )
            source_validation["comparison_video_sha256"] = (
                sha256_file(candidate_comparison) if comparison_generated else None
            )
            if source_validation.get("status") not in {"PASS", "REVIEW"}:
                raise RuntimeError("Required comparison validation failed; recovered candidate was not published.")
            final_qc = max_status(tracking_qc.technical_qc, status)
            if source_validation.get("status") in {"PASS", "REVIEW", "FAIL"}:
                final_qc = max_status(final_qc, source_validation["status"])
            if final_qc == "FAIL":
                raise RuntimeError("Required validation failed; recovered candidate was not published.")
            status = final_qc
            item["status"] = final_qc
            atomic_publish_file(
                candidate_preview,
                output_dir / "debug" / f"{gloss}_avatar_preview.mp4",
            )
            atomic_publish_file(candidate_comparison, comparison_video_path)
            atomic_publish_file(immutable_glb, recovered_glb)
            atomic_publish_file(pose_path, stable_pose_path)
            atomic_publish_file(motion_path, stable_motion_path)
            output_glb = recovered_glb
            signer_review = build_signer_review_record(
                gloss=gloss,
                technical_qc=final_qc,
                source_video=video_path or Path(f"input/{gloss}.mp4"),
                glb_path=output_glb,
                comparison_video=comparison_video_path if comparison_generated else None,
                approval=signer_approval,
                run_id=run_id,
            )
            write_comparison_and_review_reports(
                source_avatar_path,
                match_path,
                signer_review_path,
                source_validation,
                signer_review,
            )
            neutral_validation = read_json(neutral_hand_pose_path, {}).get("validation", {})
            metadata = write_reports(
                metadata_path,
                qc_path,
                video_info,
                tracking_qc,
                validation,
                final_qc,
                source_validation,
                signer_review,
                source_video=video_path,
                avatar_file=avatar_path,
                glb_path=output_glb,
                pose_path=pose_path,
                motion_path=motion_path,
                glb_validation_path=validation_path,
                khronos_validation_path=khronos_validation_path,
                source_avatar_validation_path=source_avatar_path,
                signer_review_path=signer_review_path,
                comparison_video_path=comparison_video_path if comparison_generated else None,
                profile_path=profile_path,
                bone_map_path=bone_map_path,
                neutral_hand_pose_path=neutral_hand_pose_path,
                neutral_hand_validation=neutral_validation,
                started_at=utc_now_iso(),
                catalog_path=resolve_project_path(args.motion_catalog) if args.motion_catalog else None,
                run_id=run_id,
                logical_identity_stem=gloss,
                preparation_path=preparation_path,
                working_video_path=working_video_path if preparation_path is not None else None,
                ik_report_path=bundle.get("ik_report"),
                execution_context={"run_id": run_id, "original_filename": f"{gloss}.mp4"},
            )
            write_release_manifest(
                release_manifest_path,
                metadata,
                output_glb,
                run_id,
                immutable_glb_path=immutable_glb,
            )
            recovered_archive = failed_glb.with_name(
                f"{failed_glb.name}.{run_id}.recovered"
            )
            os.replace(failed_glb, recovered_archive)
            item["recovered"] = True
            summary["recovered"] += 1
        except Exception as exc:
            item["error"] = str(exc)
            item["status"] = "FAIL"
            status = "FAIL"
            if output_lock is not None:
                atomic_write_json(
                    release_manifest_path,
                    {
                        "schema_version": "1.0",
                        "run_id": run_id,
                        "status": "FAILED",
                        "releaseable": False,
                        "reason": str(exc),
                        "updated_at": utc_now_iso(),
                    },
                )
        finally:
            release_output_lock(output_lock)

        status_key = status.lower() if status in {"PASS", "REVIEW", "FAIL"} else "fail"
        summary[status_key] += 1
        summary["items"].append(item)

    summary_path = PROJECT_ROOT / "output" / "revalidation_summary.json"
    refreshed_batch = summarize_existing_batch(
        sorted(input_videos.values()),
        uppercase_output_dir=bool(config.get("output", {}).get("uppercase_output_dir", True)),
        trusted_signers_path=resolve_project_path(
            getattr(args, "trusted_signers", "./config/trusted_signers.json")
        ),
    )
    write_json(PROJECT_ROOT / "output" / "batch_summary.json", refreshed_batch)
    summary["batch_after_revalidation"] = {
        key: refreshed_batch[key]
        for key in ("total", "pass", "review", "fail", "production_ready", "pending_signer_review")
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2))
    if summary["fail"]:
        return 1
    if getattr(args, "require_production", False) and (
        refreshed_batch["production_ready"] != refreshed_batch["total"]
    ):
        return 2
    return 0


def validate_recovery_video_timing(
    *, source_video: Path, working_video: Path, preparation_path: Path | None,
    sequence: PoseSequence,
):
    """A validation-only retry must use the exact timeline used by its saved pose."""
    source_info = inspect_video(source_video)
    working_info = source_info if working_video == source_video else inspect_video(working_video)
    if preparation_path is None:
        if source_info.variable_frame_rate:
            raise RuntimeError(
                "Failed VFR candidate has no preserved preparation/working-video evidence; rerun full conversion."
            )
    else:
        preparation = read_json(preparation_path, {})
        if preparation.get("normalized") not in (True, False):
            raise RuntimeError("Preserved video preparation has no valid normalization decision.")
        for role, info in (("source", source_info), ("working", working_info)):
            recorded = preparation.get(role)
            if not isinstance(recorded, dict) or recorded.get("frame_count") != info.frame_count:
                raise RuntimeError(f"Preserved {role} frame count does not match recovery video.")
            recorded_fps = recorded.get("fps")
            if not isinstance(recorded_fps, (int, float)) or isinstance(recorded_fps, bool) or not (
                abs(float(recorded_fps) - info.fps) <= max(1e-6, info.fps * 1e-5)
            ):
                raise RuntimeError(f"Preserved {role} FPS does not match recovery video.")
        mapping = preparation.get("timing_mapping")
        if not isinstance(mapping, list) or len(mapping) != working_info.frame_count:
            raise RuntimeError("Preserved timing map does not cover every working frame.")
    if sequence.frame_count != working_info.frame_count or not (
        abs(sequence.fps - working_info.fps) <= max(1e-6, working_info.fps * 1e-5)
    ):
        raise RuntimeError("Saved pose timing does not match recovery video; rerun full conversion.")
    return source_info, working_info


def summarize_existing_batch(
    videos: list[Path],
    *,
    uppercase_output_dir: bool = True,
    trusted_signers_path: Path | None = None,
) -> dict[str, Any]:
    """Build a current batch summary from already processed output folders."""
    summary = {
        "total": len(videos),
        "pass": 0,
        "review": 0,
        "fail": 0,
        "production_ready": 0,
        "pending_signer_review": 0,
        "items": [],
    }
    for video in videos:
        output_name = video.stem.upper() if uppercase_output_dir else video.stem
        output_dir = PROJECT_ROOT / "output" / output_name
        qc_payload = read_json(output_dir / f"{video.stem}.qc.json", {})
        metadata_payload = read_json(output_dir / f"{video.stem}.metadata.json", {})
        status = str(qc_payload.get("technical_qc", "FAIL"))
        production = metadata_payload.get("production")
        production = production if isinstance(production, dict) else {}
        isl_validation = metadata_payload.get("isl_validation")
        isl_validation = isl_validation if isinstance(isl_validation, dict) else {}
        isl_verified = isl_validation.get("isl_verified") is True
        engineering_candidate = production.get("engineering_candidate") is True
        production_status = str(production.get("production_status") or "NOT_ELIGIBLE")
        production_eligible, release_integrity_reasons = evaluate_published_release(
            output_dir,
            video.stem,
            metadata_payload,
            trusted_signers_path=trusted_signers_path,
        )
        if status not in {"PASS", "REVIEW", "FAIL"}:
            status = "FAIL"
        summary[status.lower()] += 1
        if production_eligible:
            summary["production_ready"] += 1
        elif engineering_candidate:
            summary["pending_signer_review"] += 1
        summary["items"].append(
            {
                "video": str(video),
                "status": status,
                "isl_verified": isl_verified,
                "production_status": production_status,
                "production_eligible": production_eligible,
                "engineering_candidate": engineering_candidate,
                "release_integrity_reasons": release_integrity_reasons,
            }
        )
    return summary


def evaluate_published_release(
    output_dir: Path,
    stem: str,
    metadata: dict[str, Any],
    *,
    trusted_signers_path: Path | None = None,
) -> tuple[bool, list[str]]:
    """Rebuild the release decision from immutable, role-specific evidence.

    Stored eligibility booleans and a historical signature-verification flag
    are only projections.  A current release must still have an intact run
    bundle, correctly typed report roles, a reproducible gate result, and a
    signer approval that verifies against today's trusted-key registry.
    """
    reasons: list[str] = []
    production = metadata.get("production")
    production = production if isinstance(production, dict) else {}
    if production.get("production_eligible") is not True:
        reasons.append("metadata production_eligible is not the JSON boolean true")

    manifest = read_json(output_dir / f"{stem}.release.json", {})
    if manifest.get("releaseable") is not True:
        reasons.append("release manifest is not releaseable")
    if str(manifest.get("status") or "").upper() != "APPROVED":
        reasons.append("release manifest status is not APPROVED")

    metadata_run_id = metadata.get("run_id")
    processing = metadata.get("processing")
    if not metadata_run_id and isinstance(processing, dict):
        metadata_run_id = processing.get("run_id")
    manifest_run_id = manifest.get("run_id")
    if not metadata_run_id or metadata_run_id != manifest_run_id:
        reasons.append("metadata and release-manifest run IDs do not match")
    if not isinstance(manifest_run_id, str) or RUN_ID_PATTERN.fullmatch(manifest_run_id) is None:
        reasons.append("release-manifest run ID is invalid")

    evidence_pass, evidence_reasons = verify_release_evidence(metadata)
    if not evidence_pass:
        reasons.extend(evidence_reasons)
    if manifest.get("evidence") != metadata.get("release_evidence"):
        reasons.append("manifest evidence does not match metadata evidence")

    evidence_paths, path_reasons = resolve_release_evidence_paths(metadata)
    reasons.extend(path_reasons)
    if isinstance(manifest_run_id, str) and RUN_ID_PATTERN.fullmatch(manifest_run_id):
        evidence_root = (output_dir / "runs" / manifest_run_id / "evidence").resolve()
        immutable_roles = {
            "source_video",
            "avatar",
            "pose",
            "motion",
            "qc",
            "glb_validation",
            "khronos_validation",
            "source_avatar_validation",
            "signer_review",
            "comparison_video",
            "avatar_profile",
            "bone_map",
            "neutral_hand_pose",
            "catalog_record",
            "video_preparation",
            "ik_report",
            "working_video",
        }
        for role in immutable_roles:
            role_path = evidence_paths.get(role)
            if role_path is None:
                continue
            try:
                role_path.resolve().relative_to(evidence_root)
            except ValueError:
                reasons.append(f"{role} evidence is outside the immutable run evidence directory")

        role_paths = {
            role: str(path.resolve()).casefold()
            for role, path in evidence_paths.items()
            if role in immutable_roles
        }
        reverse_roles: dict[str, list[str]] = {}
        for role, path_key in role_paths.items():
            reverse_roles.setdefault(path_key, []).append(role)
        for aliased_roles in reverse_roles.values():
            if len(aliased_roles) > 1:
                reasons.append(
                    "release evidence roles alias one file: "
                    + ", ".join(sorted(aliased_roles))
                )

    reports: dict[str, dict[str, Any]] = {}
    for role in (
        "qc",
        "glb_validation",
        "khronos_validation",
        "source_avatar_validation",
        "signer_review",
        "neutral_hand_pose",
        "catalog_record",
    ):
        path = evidence_paths.get(role)
        if path is None:
            continue
        report = read_json(path, {})
        if not report:
            reasons.append(f"{role} evidence is not a readable non-empty JSON object")
            continue
        reports[role] = report

    qc_report = reports.get("qc", {})
    glb_report = reports.get("glb_validation", {})
    khronos_report = reports.get("khronos_validation", {})
    source_report = reports.get("source_avatar_validation", {})
    signer_report = reports.get("signer_review", {})
    neutral_hand_report = reports.get("neutral_hand_pose", {})
    catalog_evidence = reports.get("catalog_record", {})
    technical_validation = metadata.get("technical_validation")
    technical_validation = technical_validation if isinstance(technical_validation, dict) else {}
    if glb_report != technical_validation.get("glb_validation"):
        reasons.append("immutable GLB-validation report does not match metadata")
    if source_report != technical_validation.get("source_avatar_validation"):
        reasons.append("immutable source/avatar report does not match metadata")
    if signer_report != metadata.get("signer_review"):
        reasons.append("immutable signer-review report does not match metadata")
    if khronos_report != glb_report.get("khronos_validation"):
        reasons.append("standalone Khronos report does not match the GLB-validation report role")
    if str(qc_report.get("technical_qc") or "").upper() != str(
        metadata.get("technical_qc") or ""
    ).upper():
        reasons.append("immutable QC report does not match metadata technical_qc")
    neutral_hand_validation = neutral_hand_report.get("validation")
    neutral_hand_validation = (
        neutral_hand_validation if isinstance(neutral_hand_validation, dict) else {}
    )
    run_neutral_validation = technical_validation.get("neutral_hand_validation")
    if not isinstance(run_neutral_validation, dict) or not neutral_hand_validation or any(
        run_neutral_validation.get(key) != value for key, value in neutral_hand_validation.items()
    ):
        reasons.append("neutral-hand calibration evidence does not match metadata")
    if source_report.get("validation_run_id") != manifest_run_id:
        reasons.append("source/avatar report is not bound to the conversion run ID")
    if signer_report.get("run_id") != manifest_run_id:
        reasons.append("signer-review report is not bound to the conversion run ID")
    motion_identity = metadata.get("motion_identity")
    motion_identity = motion_identity if isinstance(motion_identity, dict) else {}
    logical_gloss = str(motion_identity.get("gloss") or "").strip().upper()
    if not logical_gloss or str(signer_report.get("gloss") or "").strip().upper() != logical_gloss:
        reasons.append("signer-review gloss does not match the logical motion identity")
    catalog_record = catalog_evidence.get("catalog")
    catalog_record = catalog_record if isinstance(catalog_record, dict) else {}
    logical_identity_stem = catalog_evidence.get("logical_identity_stem")
    if not isinstance(logical_identity_stem, str) or not logical_identity_stem.strip():
        reasons.append("catalog evidence has no logical identity stem")
    else:
        evidence_identity = normalize_motion_identity(
            logical_identity_stem,
            catalog_record.get("domain"),
        )
        if evidence_identity.get("gloss") != logical_gloss:
            reasons.append("catalog evidence identity does not match metadata motion identity")

    integrity = metadata.get("file_integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    expected_hash = integrity.get("glb_sha256")
    artifact = manifest.get("artifact")
    artifact = artifact if isinstance(artifact, dict) else {}
    artifact_path_value = artifact.get("path")
    artifact_path: Path | None = None
    if isinstance(artifact_path_value, str) and artifact_path_value.strip():
        try:
            artifact_path = resolve_project_path(artifact_path_value)
        except (OSError, TypeError, ValueError) as exc:
            reasons.append(f"release artifact path could not be resolved: {exc}")
    if artifact_path is None:
        reasons.append("release manifest has no immutable artifact path")
    elif isinstance(manifest_run_id, str) and RUN_ID_PATTERN.fullmatch(manifest_run_id):
        expected_run_root = (output_dir / "runs" / str(manifest_run_id)).resolve()
        runs_root = (output_dir / "runs").resolve()
        try:
            expected_run_root.relative_to(runs_root)
            artifact_path.resolve().relative_to(expected_run_root)
        except ValueError:
            reasons.append("release artifact is not inside its immutable run directory")
        actual_hash = sha256_file(artifact_path)
        if not actual_hash or actual_hash != expected_hash or actual_hash != artifact.get("sha256"):
            reasons.append("immutable release artifact hash does not match metadata/manifest")
        run_glb_path = evidence_paths.get("run_glb")
        if run_glb_path is None or run_glb_path.resolve() != artifact_path.resolve():
            reasons.append("run_glb evidence does not identify the release artifact")
    else:
        reasons.append("immutable artifact cannot be resolved for an invalid run ID")

    stable_glb = output_dir / f"{stem}.glb"
    if sha256_file(stable_glb) != expected_hash:
        reasons.append("stable GLB alias does not match the approved immutable artifact")

    stable_evidence_path = evidence_paths.get("stable_glb")
    if stable_evidence_path is None or stable_evidence_path.resolve() != stable_glb.resolve():
        reasons.append("stable_glb evidence does not identify the published alias")

    manifest_checks = manifest.get("integrity_checks")
    if not isinstance(manifest_checks, dict) or not manifest_checks:
        reasons.append("release manifest has no integrity_checks map")
    elif any(value is not True for value in manifest_checks.values()):
        reasons.append("release manifest integrity_checks are not all exact JSON true values")

    # Reverify the exact signed payload.  The flattened signer record and its
    # historical verification result are not trusted independently.
    gate_signer_review = deepcopy(signer_report)
    signed_approval = signer_report.get("signed_approval")
    if not isinstance(signed_approval, dict):
        reasons.append("signer-review evidence has no exact signed_approval payload")
    else:
        registry_path = trusted_signers_path or (
            PROJECT_ROOT / "config" / "trusted_signers.json"
        )
        try:
            verified_approval = verify_signer_approval(signed_approval, registry_path)
        except (OSError, TypeError, ValueError) as exc:
            reasons.append(f"stored signer approval is not currently trusted: {exc}")
        else:
            for field, value in verified_approval.items():
                if field in {"schema_version", "signature_verification"}:
                    continue
                if signer_report.get(field) != value:
                    reasons.append(f"signer-review field differs from signed approval: {field}")
            if signer_report.get("signature_verification") != verified_approval.get(
                "signature_verification"
            ):
                reasons.append("stored signature-verification projection is not reproducible")
            gate_signer_review["signature_verification"] = deepcopy(
                verified_approval["signature_verification"]
            )

    source_path = evidence_paths.get("source_video")
    comparison_path = evidence_paths.get("comparison_video")
    source_hash = sha256_file(source_path) if source_path is not None else None
    comparison_hash = sha256_file(comparison_path) if comparison_path is not None else None
    recomputed_gate = evaluate_production_gate(
        technical_qc=qc_report.get("technical_qc"),
        glb_validation=glb_report,
        source_avatar_validation=source_report,
        signer_review=gate_signer_review,
        catalog=catalog_record,
        source_video_sha256=source_hash,
        glb_sha256=expected_hash,
        comparison_video_sha256=comparison_hash,
        neutral_hand_validation=neutral_hand_validation,
    )
    if recomputed_gate.get("production_eligible") is not True:
        reasons.append("production gate does not recompute to an approved release")
    if production.get("release_gate") != recomputed_gate:
        reasons.append("stored production gate differs from recomputed evidence")
    if manifest.get("release_gate") != recomputed_gate:
        reasons.append("release-manifest gate differs from recomputed evidence")
    if qc_report.get("production_gate") != recomputed_gate:
        reasons.append("immutable QC release gate differs from recomputed evidence")
    if str(production.get("production_status") or "").upper() != "APPROVED":
        reasons.append("metadata production status is not APPROVED")
    return not reasons, reasons


def resolve_release_evidence_paths(
    metadata: dict[str, Any],
) -> tuple[dict[str, Path], list[str]]:
    """Resolve evidence records without allowing malformed path values to throw."""

    records = metadata.get("release_evidence")
    if not isinstance(records, dict):
        return {}, ["metadata release_evidence is missing"]
    paths: dict[str, Path] = {}
    reasons: list[str] = []
    for role, record in records.items():
        value = record.get("path") if isinstance(record, dict) else None
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            paths[str(role)] = resolve_project_path(value)
        except (OSError, TypeError, ValueError) as exc:
            reasons.append(f"{role} evidence path could not be resolved: {exc}")
    return paths, reasons


def evaluate_published_engineering_candidate(
    output_dir: Path,
    stem: str,
    metadata: dict[str, Any],
) -> bool:
    """Return true only for a fully intact current engineering candidate."""

    production = metadata.get("production")
    production = production if isinstance(production, dict) else {}
    if production.get("engineering_candidate") is not True:
        return False
    manifest = read_json(output_dir / f"{stem}.release.json", {})
    if manifest.get("engineering_candidate") is not True:
        return False
    if str(manifest.get("status") or "").upper() not in {
        "ENGINEERING_CANDIDATE",
        "APPROVED",
    }:
        return False
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        return False
    if manifest.get("run_id") != run_id:
        return False
    integrity_checks = manifest.get("integrity_checks")
    if not isinstance(integrity_checks, dict) or not integrity_checks:
        return False
    if not all(value is True for value in integrity_checks.values()):
        return False
    evidence_pass, _ = verify_release_evidence(metadata)
    return evidence_pass and manifest.get("evidence") == metadata.get("release_evidence")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return data


def ensure_required_inputs(video_path: Path, avatar_path: Path, model_path: Path) -> None:
    missing = [path for path in (video_path, avatar_path, model_path) if not path.exists()]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Required input missing: {names}")


def capture_input_hashes(paths: dict[str, Path]) -> dict[str, str]:
    """Bind a conversion attempt to the exact bytes read by long-running stages."""

    hashes: dict[str, str] = {}
    for name, path in paths.items():
        digest = sha256_file(path)
        if digest is None:
            raise FileNotFoundError(f"Critical {name} input is missing or unreadable: {path}")
        hashes[name] = digest
    return hashes


def assert_input_hashes_unchanged(
    paths: dict[str, Path],
    expected_hashes: dict[str, str],
) -> None:
    """Refuse publication if any input changed while conversion was running."""

    changed = [
        name
        for name, path in paths.items()
        if sha256_file(path) != expected_hashes.get(name)
    ]
    if changed:
        raise RuntimeError(
            "Critical conversion input changed before publication: "
            + ", ".join(sorted(changed))
            + ". Rerun from stable inputs."
        )


def run_stage(index: int, label: str, func):
    print(f"[{index}] {label:<29}", end="", flush=True)
    result = func()
    print(" PASS")
    return result


def print_tracking_progress(done: int, total: int) -> None:
    if total:
        print(f"\r[2] Extract holistic landmarks ... {done}/{total}", end="", flush=True)
    else:
        print(f"\r[2] Extract holistic landmarks ... {done}", end="", flush=True)


def verify_pose_timeline(sequence, video_info) -> None:
    """Check cached tracking against the currently decoded working timeline."""
    if sequence.frame_count != video_info.frame_count:
        raise RuntimeError("Tracked frame count differs from the prepared video timeline.")
    if not math.isclose(sequence.fps, video_info.fps, rel_tol=1e-6, abs_tol=1e-6):
        raise RuntimeError("Tracked FPS differs from the prepared video timeline.")
    if sequence.width != video_info.width or sequence.height != video_info.height:
        raise RuntimeError("Tracked dimensions differ from the prepared video.")
    if len(video_info.timestamps_ms) != sequence.frame_count:
        raise RuntimeError("Prepared video timestamp count differs from tracked frames.")
    for index, (frame, expected_time) in enumerate(zip(sequence.frames, video_info.timestamps_ms)):
        if frame.frame_index != index or frame.timestamp_ms != expected_time:
            raise RuntimeError(f"Tracked frame/timestamp mismatch at frame {index}.")


def avatar_calibration_paths(
    avatar_path: Path,
    *,
    expected_avatar_sha256: str | None = None,
) -> tuple[Path, Path, Path]:
    """Return an immutable, content-addressed calibration location."""

    avatar_hash = sha256_file(avatar_path)
    if avatar_hash is None:
        raise FileNotFoundError(f"Avatar file is missing or unreadable: {avatar_path}")
    if expected_avatar_sha256 is not None and avatar_hash != expected_avatar_sha256:
        raise RuntimeError("Avatar changed before its calibration path was selected.")
    root = PROJECT_ROOT / "config" / "avatar_calibrations" / avatar_hash
    return (
        root / "avatar_profile.json",
        root / "avatar_bone_map.json",
        root / "neutral_hand_pose.json",
    )


def ensure_avatar_calibrated(
    blender_path: Path,
    avatar_path: Path,
    profile_path: Path,
    bone_map_path: Path,
    *,
    expected_avatar_sha256: str | None = None,
) -> None:
    """Create or verify a calibration bound to the exact avatar bytes."""

    avatar_hash = sha256_file(avatar_path)
    if avatar_hash is None:
        raise FileNotFoundError(f"Avatar file is missing or unreadable: {avatar_path}")
    if expected_avatar_sha256 is not None and avatar_hash != expected_avatar_sha256:
        raise RuntimeError("Avatar changed before calibration began.")
    lock = acquire_output_lock(profile_path.parent, f"calibration-{avatar_hash}", wait_seconds=30.0)
    try:
        needs_calibration = True
        if profile_path.is_file() and bone_map_path.is_file():
            try:
                profile = load_avatar_profile(profile_path)
                bone_payload = read_json(bone_map_path, {})
                load_bone_map(bone_map_path)
                profile_calibration = profile.get("calibration")
                profile_calibration = (
                    profile_calibration if isinstance(profile_calibration, dict) else {}
                )
                bone_calibration = bone_payload.get("calibration")
                bone_calibration = (
                    bone_calibration if isinstance(bone_calibration, dict) else {}
                )
                needs_calibration = not (
                    profile.get("status") == "PASS"
                    and bone_payload.get("status") == "PASS"
                    and profile_calibration.get("avatar_sha256") == avatar_hash
                    and bone_calibration.get("avatar_sha256") == avatar_hash
                    and bone_calibration.get("avatar_profile_sha256")
                    == sha256_file(profile_path)
                )
            except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
                needs_calibration = True
        if not needs_calibration:
            return

        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "calibrate_avatar.py"),
            "--blender",
            str(blender_path),
            "--avatar",
            str(avatar_path),
            "--profile",
            str(profile_path),
            "--bone-map",
            str(bone_map_path),
        ]
        subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
        profile = load_avatar_profile(profile_path)
        bone_payload = read_json(bone_map_path, {})
        profile["calibration"] = {
            "schema_version": "1.0",
            "avatar_path": relative_display(avatar_path),
            "avatar_sha256": avatar_hash,
            "generated_at": utc_now_iso(),
        }
        atomic_write_json(profile_path, profile)
        bone_payload["calibration"] = {
            "schema_version": "1.0",
            "avatar_sha256": avatar_hash,
            "avatar_profile_sha256": sha256_file(profile_path),
            "generated_at": utc_now_iso(),
        }
        atomic_write_json(bone_map_path, bone_payload)
        if sha256_file(avatar_path) != avatar_hash:
            raise RuntimeError("Avatar changed while calibration was running.")
        load_bone_map(bone_map_path)
    finally:
        release_output_lock(lock)


def run_blender_apply(
    blender_path: Path,
    avatar_path: Path,
    profile_path: Path,
    bone_map_path: Path,
    motion_path: Path,
    output_path: Path,
    ik_report_path: Path | None = None,
    *,
    use_ik: bool = True,
    use_finger_tracking: bool = False,
    use_palm_tracking: bool = False,
) -> None:
    ensure_blender(blender_path)
    cmd = [
        str(blender_path),
        "--factory-startup",
        "--background",
        "--python",
        str(PROJECT_ROOT / "src" / "blender" / "blender_apply_motion.py"),
        "--",
        "--avatar",
        str(avatar_path),
        "--profile",
        str(profile_path),
        "--bone-map",
        str(bone_map_path),
        "--motion",
        str(motion_path),
        "--output",
        str(output_path),
    ]
    # The direct solver stores body-relative wrist targets. Blender IK uses
    # the avatar's fixed bone lengths to reach those targets without stretching.
    if use_ik:
        cmd.append("--use-ik")
    # Hand-world landmarks are stored only as local finger directions. Blender
    # attaches those directions to the solved avatar palm before baking.
    if use_finger_tracking:
        cmd.append("--use-finger-tracking")
    if use_palm_tracking:
        cmd.append("--use-palm-tracking")
    if ik_report_path is not None:
        cmd.extend(["--ik-report", str(ik_report_path)])
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"Blender did not create a non-empty GLB: {output_path}")


def run_blender_validate(
    blender_path: Path,
    glb_path: Path,
    motion_path: Path,
    profile_path: Path,
    bone_map_path: Path,
    report_path: Path,
    ik_report_path: Path | None = None,
) -> dict[str, Any]:
    """Validate one exact GLB and atomically publish only its fresh report.

    Blender writes to a unique per-invocation report.  A crashed validator can
    therefore never make this function return an older PASS report left at the
    stable report path.
    """
    ensure_blender(blender_path)
    invocation_id = uuid.uuid4().hex
    temporary_report_path = report_path.with_name(
        f".{report_path.stem}.{invocation_id}.json"
    )
    glb_hash_before = sha256_file(glb_path)
    motion_hash_before = sha256_file(motion_path)
    ik_hash_before = sha256_file(ik_report_path) if ik_report_path is not None else None
    atomic_write_json(
        report_path,
        {
            "status": "RUNNING",
            "validation_run_id": invocation_id,
            "validated_glb_sha256": glb_hash_before,
            "motion_sha256": motion_hash_before,
            "started_at": utc_now_iso(),
        },
    )
    cmd = [
        str(blender_path),
        "--factory-startup",
        "--background",
        "--python",
        str(PROJECT_ROOT / "src" / "blender" / "blender_validate_glb.py"),
        "--",
        "--glb",
        str(glb_path),
        "--motion",
        str(motion_path),
        "--profile",
        str(profile_path),
        "--bone-map",
        str(bone_map_path),
        "--report",
        str(temporary_report_path),
    ]
    if ik_report_path is not None:
        if ik_hash_before is None:
            raise RuntimeError("The export correction report is missing.")
        cmd.extend(["--ik-report", str(ik_report_path)])
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False)
    try:
        if not temporary_report_path.is_file():
            failure = {
                "status": "FAIL",
                "reasons": ["Blender validation did not create a fresh report."],
                "validator_returncode": result.returncode,
            }
            atomic_write_json(report_path, failure)
            raise RuntimeError(failure["reasons"][0])
        try:
            validation = json.loads(temporary_report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            failure = {
                "status": "FAIL",
                "reasons": [f"Fresh Blender validation report is unreadable: {exc}"],
                "validator_returncode": result.returncode,
            }
            atomic_write_json(report_path, failure)
            raise RuntimeError(failure["reasons"][0]) from exc
        if not isinstance(validation, dict):
            raise RuntimeError("Fresh Blender validation report must contain a JSON object.")

        glb_hash_after = sha256_file(glb_path)
        motion_hash_after = sha256_file(motion_path)
        reasons = list(validation.get("reasons") or [])
        if ik_report_path is not None and sha256_file(ik_report_path) != ik_hash_before:
            reasons.append("The export correction report changed during validation.")
            validation["status"] = "FAIL"
        if glb_hash_before is None or motion_hash_before is None:
            reasons.append("Validation input hash could not be computed.")
        if glb_hash_after != glb_hash_before or motion_hash_after != motion_hash_before:
            reasons.append("A validation input changed while Blender was running.")
        if result.returncode != 0 and str(validation.get("status", "")).upper() != "FAIL":
            reasons.append(f"Blender validator exited with code {result.returncode}.")
        if reasons and (
            result.returncode != 0
            or glb_hash_before is None
            or motion_hash_before is None
            or glb_hash_after != glb_hash_before
            or motion_hash_after != motion_hash_before
        ):
            validation["status"] = "FAIL"
        validation["reasons"] = reasons
        validation["validation_run_id"] = invocation_id
        validation["validated_glb_sha256"] = glb_hash_before
        validation["motion_sha256"] = motion_hash_before
        validation["ik_report_sha256"] = ik_hash_before
        validation["validator_returncode"] = result.returncode
        validation["validated_at"] = utc_now_iso()
        atomic_write_json(report_path, validation)
        return validation
    finally:
        if temporary_report_path.exists():
            temporary_report_path.unlink()


def validate_existing_glb(
    blender_path: Path,
    glb_path: Path,
    motion_path: Path,
    profile_path: Path,
    bone_map_path: Path,
    report_path: Path,
    ik_report_path: Path | None = None,
) -> dict[str, Any]:
    """Revalidate an existing GLB without ever accepting a stale report."""
    attempt_id = uuid.uuid4().hex
    atomic_write_json(report_path, {"status": "RUNNING", "validation_run_id": attempt_id})
    try:
        return run_blender_validate(
            blender_path,
            glb_path,
            motion_path,
            profile_path,
            bone_map_path,
            report_path,
            **({"ik_report_path": ik_report_path} if ik_report_path else {}),
        )
    except subprocess.CalledProcessError:
        current = read_json(report_path, {})
        if str(current.get("status", "")).upper() == "FAIL":
            return current
        raise


def run_khronos_validate(
    glb_path: Path,
    report_path: Path,
    warning_allowlist: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the pinned official glTF 2.0 validator and classify its report."""
    node = shutil.which("node")
    validator_script = PROJECT_ROOT / "tools" / "validate_glb.mjs"
    validator_package = PROJECT_ROOT / "node_modules" / "gltf-validator" / "package.json"
    if not node:
        classified = classify_gltf_validator_report(None)
        classified["execution_error"] = "Node.js is not available on PATH."
        atomic_write_json(report_path, classified)
        return classified
    if not validator_script.is_file() or not validator_package.is_file():
        classified = classify_gltf_validator_report(None)
        classified["execution_error"] = (
            "Pinned glTF validator tooling is missing; run npm ci before conversion."
        )
        atomic_write_json(report_path, classified)
        return classified

    try:
        installed_version = str(
            json.loads(validator_package.read_text(encoding="utf-8")).get("version") or ""
        )
    except (OSError, json.JSONDecodeError):
        installed_version = ""
    if installed_version != KHRONOS_VALIDATOR_VERSION:
        classified = classify_gltf_validator_report(None)
        classified["execution_error"] = (
            f"Expected gltf-validator {KHRONOS_VALIDATOR_VERSION}, found {installed_version or 'unknown'}. "
            "Run npm ci to restore the pinned validator."
        )
        atomic_write_json(report_path, classified)
        return classified

    glb_hash_before = sha256_file(glb_path)
    result = subprocess.run(
        [node, str(validator_script), str(glb_path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        classified = classify_gltf_validator_report(None)
        classified["execution_error"] = (
            result.stderr.strip() or f"glTF Validator exited with code {result.returncode}."
        )
        classified["validator_returncode"] = result.returncode
        atomic_write_json(report_path, classified)
        return classified

    classified = classify_gltf_validator_report(
        result.stdout,
        allowlisted_warnings=warning_allowlist or {},
    )
    try:
        official = json.loads(result.stdout)
    except json.JSONDecodeError:
        official = {}
    glb_hash_after = sha256_file(glb_path)
    if glb_hash_before is None or glb_hash_after != glb_hash_before:
        classified["status"] = "FAIL"
        classified.setdefault("reasons", []).append(
            {
                "code": "VALIDATION_INPUT_CHANGED",
                "message": "The GLB changed while Khronos validation was running.",
            }
        )
    if classified.get("validator_version") != KHRONOS_VALIDATOR_VERSION:
        classified["status"] = "FAIL"
        classified.setdefault("reasons", []).append(
            {
                "code": "VALIDATOR_VERSION_MISMATCH",
                "message": (
                    f"Expected validator report version {KHRONOS_VALIDATOR_VERSION}, "
                    f"found {classified.get('validator_version')!r}."
                ),
            }
        )
    classified["validator_returncode"] = result.returncode
    classified["validated_glb_sha256"] = glb_hash_before
    classified["validated_at"] = utc_now_iso()
    classified["asset_info"] = official.get("info") if isinstance(official, dict) else None
    atomic_write_json(report_path, classified)
    return classified


def merge_khronos_validation(
    glb_validation: dict[str, Any],
    khronos_validation: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    """Make standards compliance part of the same fail-closed GLB decision."""
    merged = deepcopy(glb_validation)
    merged["khronos_validation"] = deepcopy(khronos_validation)
    status = str(khronos_validation.get("status") or "FAIL").upper()
    reasons = [
        str(reason.get("message") or reason.get("code"))
        for reason in khronos_validation.get("reasons", [])
        if isinstance(reason, dict)
    ]
    if status not in {"PASS", "REVIEW"}:
        merged["status"] = "FAIL"
        if not reasons:
            reasons = [f"Unexpected validator status {status!r}."]
        merged["reasons"] = list(merged.get("reasons") or []) + [
            f"Khronos glTF validation: {reason}" for reason in reasons
        ]
    elif status == "REVIEW" and str(merged.get("status") or "PASS").upper() == "PASS":
        merged["status"] = "REVIEW"
        merged["review_reasons"] = list(merged.get("review_reasons") or []) + [
            f"Khronos glTF validation: {reason}" for reason in reasons
        ]
    atomic_write_json(report_path, merged)
    return merged


def run_blender_render_animation(blender_path: Path, glb_path: Path, fps: float, output_path: Path) -> None:
    ensure_blender(blender_path)
    cmd = [
        str(blender_path),
        "--factory-startup",
        "--background",
        "--python-exit-code",
        "17",
        "--python",
        str(PROJECT_ROOT / "src" / "blender" / "blender_render_animation.py"),
        "--",
        "--glb",
        str(glb_path),
        "--output",
        str(output_path),
        "--fps",
        str(fps),
    ]
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"Blender did not create a non-empty avatar preview: {output_path}")


def ensure_blender(blender_path: Path) -> None:
    if not blender_path.exists():
        raise FileNotFoundError(f"Blender executable not found: {blender_path}")


def move_failed_output(glb_path: Path) -> None:
    if not glb_path.exists():
        return
    failed_dir = PROJECT_ROOT / "failed" / glb_path.parent.name
    failed_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(glb_path), str(failed_dir / glb_path.name))


def write_reports(
    metadata_path: Path,
    qc_path: Path,
    video_info,
    qc_result,
    glb_validation: dict[str, Any] | None = None,
    final_technical_qc: str | None = None,
    source_avatar_validation: dict[str, Any] | None = None,
    signer_review: dict[str, Any] | None = None,
    *,
    source_video: Path | None = None,
    avatar_file: Path | None = None,
    glb_path: Path | None = None,
    pose_path: Path | None = None,
    motion_path: Path | None = None,
    glb_validation_path: Path | None = None,
    khronos_validation_path: Path | None = None,
    source_avatar_validation_path: Path | None = None,
    signer_review_path: Path | None = None,
    comparison_video_path: Path | None = None,
    profile_path: Path | None = None,
    bone_map_path: Path | None = None,
    neutral_hand_pose_path: Path | None = None,
    neutral_hand_validation: dict[str, Any] | None = None,
    started_at: str | None = None,
    catalog_path: Path | None = None,
    run_id: str | None = None,
    logical_identity_stem: str | None = None,
    expected_evidence_hashes: dict[str, str] | None = None,
    execution_context: dict[str, Any] | None = None,
    preparation_path: Path | None = None,
    working_video_path: Path | None = None,
    ik_report_path: Path | None = None,
) -> dict[str, Any]:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    existing_metadata = read_json(metadata_path, {})
    if ik_report_path is None and existing_metadata.get("run_id") == run_id:
        saved_ik = existing_metadata.get("release_evidence", {}).get("ik_report", {})
        if saved_ik.get("path"):
            ik_report_path = resolve_project_path(saved_ik["path"])
    expected_ik_hash = (glb_validation or {}).get("ik_report_sha256")
    if expected_ik_hash and (ik_report_path is None or sha256_file(ik_report_path) != expected_ik_hash):
        raise RuntimeError("Export correction report does not match fresh GLB validation evidence.")
    preserved_execution = execution_context if execution_context is not None else {"run_id": run_id}
    if execution_context is None and existing_metadata.get("run_id") == run_id:
        processing = existing_metadata.get("processing")
        processing = processing if isinstance(processing, dict) else {}
        identity = existing_metadata.get("asset_identity")
        identity = identity if isinstance(identity, dict) else {}
        avatar = existing_metadata.get("avatar")
        avatar = avatar if isinstance(avatar, dict) else {}
        preserved_execution = {
            **deepcopy(processing),
            "run_id": run_id,
            "batch_id": processing.get("batch_id") or identity.get("batch_id"),
            "original_filename": identity.get("original_filename"),
            "calibration_hashes": deepcopy(avatar.get("calibration_hashes", {})),
        }
    final_technical_qc = final_technical_qc or qc_result.technical_qc
    video_summary = video_info.to_json_dict()
    timestamps = video_summary.pop("timestamps_ms", []) or []
    fps = float(video_summary.get("fps") or 0.0)
    frame_count = int(video_summary.get("frame_count") or 0)
    video_summary["timestamp_start_ms"] = int(timestamps[0]) if timestamps else 0
    video_summary["timestamp_end_ms"] = (
        int(timestamps[-1])
        if timestamps
        else (int(round((frame_count - 1) / fps * 1000.0)) if fps and frame_count else 0)
    )
    video_summary["timestamp_interval_ms"] = 1000.0 / fps if fps else None
    video_summary["timestamps_explicit"] = False
    metadata = {
        "video": video_summary,
        "milestone": "Direct MP4 to GLB production integration",
        "pipeline_stage": "video_to_pose_to_motion_to_glb",
        "technical_qc": final_technical_qc,
        "isl_verified": (signer_review or {}).get("isl_verified") is True,
        "production_status": "NOT_ELIGIBLE",
        "production_eligible": False,
        "engineering_candidate": False,
        "run_id": run_id,
        "glb_validation": glb_validation or {},
        "source_avatar_validation": source_avatar_validation or {},
        "signer_review": signer_review or {},
    }
    qc_payload = qc_result.to_json_dict()
    qc_payload["technical_qc"] = final_technical_qc
    qc_payload["glb_validation"] = glb_validation or {}
    qc_payload["source_avatar_validation"] = source_avatar_validation or {}
    qc_payload["isl_verified"] = (signer_review or {}).get("isl_verified") is True
    qc_payload["production_status"] = "NOT_ELIGIBLE"
    qc_payload["production_eligible"] = False
    qc_payload["engineering_candidate"] = False
    qc_payload["run_id"] = run_id
    if glb_validation and glb_validation.get("status") == "REVIEW":
        qc_payload["review_reasons"] = list(qc_payload.get("review_reasons", [])) + list(glb_validation.get("review_reasons", []))
    # Create the QC dependency before asset records are built.  Metadata is the
    # final commit record and is written only after every projection agrees.
    atomic_write_json(qc_path, qc_payload)

    # Refresh the legacy top-level summary as well as schema-v3 metadata while
    # preserving unrelated/human-maintained fields from an earlier file.
    metadata = merge_metadata(existing_metadata, metadata)
    catalog_record_path: Path | None = None
    if all((source_video, avatar_file, glb_path, pose_path, motion_path, glb_validation_path,
            source_avatar_validation_path, signer_review_path, profile_path, bone_map_path)):
        identity_stem = logical_identity_stem or source_video.stem
        catalog_row = load_catalog_row(catalog_path, identity_stem)
        if isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id):
            catalog_record_path = (
                glb_path.parent / "runs" / run_id / "evidence" / "catalog_record.json"
            )
            atomic_write_json(
                catalog_record_path,
                {
                    "schema_version": "1.0",
                    "logical_identity_stem": identity_stem,
                    "catalog": deepcopy(catalog_row),
                },
            )
        production_metadata = build_production_metadata(
            project_root=PROJECT_ROOT,
            source_video=source_video,
            avatar_file=avatar_file,
            glb_path=glb_path,
            pose_path=pose_path,
            motion_path=motion_path,
            qc_path=qc_path,
            glb_validation_path=glb_validation_path,
            source_avatar_validation_path=source_avatar_validation_path,
            signer_review_path=signer_review_path,
            comparison_video_path=comparison_video_path,
            video_info=video_info,
            tracking_qc=qc_payload,
            glb_validation=glb_validation or {},
            source_avatar_validation=source_avatar_validation or {},
            signer_review=signer_review or {},
            avatar_profile=load_avatar_profile(profile_path),
            bone_map=load_bone_map(bone_map_path),
            neutral_hand_validation=neutral_hand_validation or {},
            neutral_hand_pose_path=neutral_hand_pose_path,
            started_at=started_at or utc_now_iso(),
            completed_at=utc_now_iso(),
            catalog_row=catalog_row,
            logical_identity_stem=identity_stem,
            execution_context=preserved_execution,
            preparation=preparation_summary(preparation_path, working_video_path),
        )
        metadata = merge_metadata(metadata, production_metadata)
        # These sections are generated release evidence.  Never allow a prior
        # human value or approval to survive a rerun through nullable merging.
        for controlled_key in (
            "file_integrity",
            "technical_validation",
            "isl_validation",
            "production",
            "asset_identity",
        ):
            metadata[controlled_key] = deepcopy(production_metadata[controlled_key])
        if execution_context is not None:
            for controlled_key in ("processing", "preparation", "video", "animation"):
                metadata[controlled_key] = deepcopy(production_metadata[controlled_key])
        if isinstance(metadata.get("processing"), dict):
            metadata["processing"]["run_id"] = run_id

        production = metadata["production"]
        metadata["production_status"] = production["production_status"]
        metadata["production_eligible"] = production["production_eligible"]
        metadata["engineering_candidate"] = production["engineering_candidate"]
        metadata["isl_verified"] = metadata["isl_validation"]["isl_verified"]
        metadata["signer_review"] = deepcopy(signer_review or {})

        qc_payload["production_status"] = production["production_status"]
        qc_payload["production_eligible"] = production["production_eligible"]
        qc_payload["engineering_candidate"] = production["engineering_candidate"]
        qc_payload["production_gate"] = deepcopy(production["release_gate"])
        qc_payload["isl_verified"] = metadata["isl_validation"]["isl_verified"]
    # Schema 3.0 intentionally summarizes constant-rate timestamps instead of
    # retaining the legacy per-frame array.
    if isinstance(metadata.get("video"), dict):
        metadata["video"].pop("timestamps_ms", None)
    atomic_write_json(qc_path, qc_payload)
    if all((source_video, avatar_file, glb_path, pose_path, motion_path)):
        mutable_evidence_paths = {
            "source_video": source_video,
            "avatar": avatar_file,
            "pose": pose_path,
            "motion": motion_path,
            "qc": qc_path,
            "glb_validation": glb_validation_path,
            "khronos_validation": khronos_validation_path,
            "source_avatar_validation": source_avatar_validation_path,
            "signer_review": signer_review_path,
            "comparison_video": comparison_video_path,
            "avatar_profile": profile_path,
            "bone_map": bone_map_path,
            "neutral_hand_pose": neutral_hand_pose_path,
            "catalog_record": catalog_record_path,
            "video_preparation": preparation_path,
            "working_video": working_video_path if preparation_path is not None else None,
            "ik_report": ik_report_path,
        }
        run_dir = (
            glb_path.parent / "runs" / str(run_id)
            if isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id)
            else None
        )
        snapshot_paths = (
            snapshot_release_dependencies(
                run_dir,
                mutable_evidence_paths,
                expected_hashes=expected_evidence_hashes,
            )
            if run_dir is not None
            else mutable_evidence_paths
        )
        immutable_glb_path = run_dir / glb_path.name if run_dir is not None else glb_path
        evidence_paths = {
            **snapshot_paths,
            "stable_glb": glb_path,
            "run_glb": immutable_glb_path,
        }
        metadata["release_evidence"] = {
            name: release_evidence_record(path)
            for name, path in evidence_paths.items()
            if path is not None
        }
        if snapshot_paths.get("video_preparation") is not None:
            metadata["preparation"] = preparation_summary(
                snapshot_paths["video_preparation"], snapshot_paths.get("working_video"),
            )
        metadata.setdefault("file_integrity", {}).update(
            {
                f"{name}_sha256": record["sha256"]
                for name, record in metadata["release_evidence"].items()
            }
        )
    atomic_write_json(metadata_path, metadata)
    return metadata


def preparation_summary(preparation_path: Path | None, working_video_path: Path | None) -> dict[str, Any]:
    if preparation_path is None:
        return {}
    payload = read_json(preparation_path, {})
    summary = {key: deepcopy(value) for key, value in payload.items() if key != "timing_mapping"}
    for role in ("source", "working"):
        if isinstance(summary.get(role), dict):
            summary[role].pop("timestamps_ms", None)
            summary[role].pop("timestamps_seconds", None)
    summary["timing_mapping_frame_count"] = len(payload.get("timing_mapping", []))
    summary["timing_evidence"] = release_evidence_record(preparation_path)
    if working_video_path is not None:
        summary["working_video"] = release_evidence_record(working_video_path)
    return summary


def release_evidence_record(path: Path) -> dict[str, Any]:
    target = path.resolve()
    return {
        "path": relative_display(target),
        "exists": target.is_file(),
        "size_bytes": target.stat().st_size if target.is_file() else None,
        "sha256": sha256_file(target),
    }


def snapshot_release_dependencies(
    run_dir: Path,
    paths: dict[str, Path | None],
    *,
    expected_hashes: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    """Copy release dependencies into the run bundle before manifest commit."""

    evidence_dir = run_dir / "evidence"
    expected_hashes = expected_hashes or {}
    snapshots: dict[str, Path | None] = {}
    for name, source in paths.items():
        if source is None:
            snapshots[name] = None
            continue
        source = source.resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError(f"Release evidence is missing or empty: {source}")
        expected_hash = expected_hashes.get(name)
        if expected_hash is not None and sha256_file(source) != expected_hash:
            raise RuntimeError(f"Release evidence changed before snapshot: {name}")
        suffix = "".join(source.suffixes) or ".bin"
        destination = evidence_dir / f"{name}{suffix}"
        if source != destination.resolve():
            atomic_publish_file(source, destination)
        if expected_hash is not None and sha256_file(destination) != expected_hash:
            raise RuntimeError(f"Release evidence snapshot hash mismatch: {name}")
        snapshots[name] = destination
    return snapshots


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    return value if isinstance(value, dict) else default


def default_source_avatar_validation() -> dict[str, Any]:
    return {
        "status": "NOT_RENDERED",
        "reason": "No source/avatar comparison report was available during validation recovery.",
        "isl_verified": False,
        "isl_verification_status": "PENDING_SIGNER_REVIEW",
    }


def build_signer_review_record(
    gloss: str,
    technical_qc: str,
    source_video: Path,
    glb_path: Path,
    comparison_video: Path | None,
    approval: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    current_hashes = {
        "source_video_sha256": sha256_file(source_video),
        "glb_sha256": sha256_file(glb_path),
        "comparison_video_sha256": sha256_file(comparison_video) if comparison_video else None,
    }
    record: dict[str, Any] = {
        "schema_version": "2.0",
        "run_id": run_id,
        "gloss": gloss.upper(),
        "technical_qc": technical_qc,
        "isl_verified": False,
        "production_status": "PENDING_GATE_EVALUATION" if technical_qc != "FAIL" else "NOT_ELIGIBLE",
        "source_video": relative_display(source_video),
        "avatar_glb": relative_display(glb_path),
        "comparison_video": relative_display(comparison_video) if comparison_video else None,
        "hash_binding": deepcopy(current_hashes),
        "current_artifact_hashes": deepcopy(current_hashes),
        "signer_verdict": "PENDING",
        "reviewer": None,
        "reviewed_at": None,
        "notes": None,
        "collision_review": {
            "status": "PENDING",
            "method": None,
            "notes": None,
        },
        "approval_rule": "A qualified ISL signer must mark this sign PASS before it is released for passenger-facing use.",
    }
    if approval:
        if str(approval.get("gloss") or "").strip().upper() != gloss.strip().upper():
            raise ValueError("Signer approval gloss does not match this conversion.")
        # Hash binding is signer-owned evidence.  Never fill missing approval
        # hashes from the artifacts produced by this run, because that would
        # let an unbound approval silently approve new bytes.
        supplied_binding = approval.get("hash_binding")
        record["hash_binding"] = (
            deepcopy(supplied_binding) if isinstance(supplied_binding, dict) else {}
        )
        for key in (
            "signer_verdict",
            "isl_verified",
            "reviewer",
            "reviewed_at",
            "notes",
            "collision_review",
            "review_method",
            "signature",
            "signature_verification",
        ):
            if key in approval:
                record[key] = deepcopy(approval[key])
        # Preserve the exact cryptographic payload separately from the
        # flattened review projection.  Published-release verification can
        # then validate the original signed fields against the *current*
        # trusted-key registry (including key revocation) instead of trusting
        # a stored ``signature_verification=true`` flag.
        signed_approval = deepcopy(approval)
        signed_approval.pop("signature_verification", None)
        signed_approval.pop("approval_source", None)
        record["signed_approval"] = signed_approval
        record["approval_source"] = approval.get("approval_source")
    return record


def load_signer_approval(path: Path, trusted_signers_path: Path) -> dict[str, Any]:
    """Load and cryptographically verify signer-owned approval fields."""
    if not path.is_file():
        raise FileNotFoundError(f"Signer approval JSON not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Signer approval JSON is invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Signer approval JSON must contain an object.")
    if "isl_verified" in payload and not isinstance(payload["isl_verified"], bool):
        raise ValueError("signer approval isl_verified must be a JSON boolean.")
    verdict = str(payload.get("signer_verdict") or "PENDING").upper()
    if verdict not in {"PASS", "REVIEW", "FAIL", "PENDING"}:
        raise ValueError("signer approval signer_verdict must be PASS, REVIEW, FAIL, or PENDING.")
    verified = verify_signer_approval(payload, trusted_signers_path)
    verified["approval_source"] = relative_display(path)
    return verified


def write_comparison_and_review_reports(
    source_avatar_validation_path: Path,
    match_validation_path: Path,
    signer_review_path: Path,
    source_avatar_validation: dict[str, Any],
    signer_review: dict[str, Any],
) -> None:
    """Write current technical timing and human-review records together."""
    current_match = {
        **source_avatar_validation,
        "report_kind": "CURRENT_DIRECT_PIPELINE_TIMING_COMPATIBILITY",
        "summary": "This supersedes stale render-comparison reports. It verifies timing and frame alignment, not ISL meaning.",
    }
    write_json(source_avatar_validation_path, source_avatar_validation)
    write_json(match_validation_path, current_match)
    write_json(signer_review_path, signer_review)


def write_research_status(path: Path, gloss: str, new_glb_path: Path) -> None:
    legacy_script = PROJECT_ROOT / "legacy" / "bvh_to_glb_rokoko.py"
    payload = {
        "gloss": gloss.upper(),
        "new_pipeline": {
            "status": "PASS" if new_glb_path.exists() else "FAIL",
            "glb": relative_display(new_glb_path),
        },
        "old_pipeline": {
            "status": "UNAVAILABLE",
            "reason": "legacy/bvh_to_glb_rokoko.py was not present; TDPT/BVH/Rokoko comparison was not run.",
        },
        "comparison_metrics": {
            "arm_trajectory": "PENDING_LEGACY_BASELINE",
            "palm_orientation": "PENDING_LEGACY_BASELINE",
            "finger_accuracy": "PENDING_LEGACY_BASELINE",
            "jitter": "PENDING_LEGACY_BASELINE",
            "timing": "PENDING_LEGACY_BASELINE",
            "processing_effort": "PENDING_LEGACY_BASELINE",
        },
    }
    if legacy_script.exists():
        payload["old_pipeline"]["status"] = "PRESENT_NOT_RUN"
        payload["old_pipeline"]["reason"] = "Legacy script exists, but automated TDPT/BVH source generation is not configured."
    atomic_write_json(path, payload)


def invalidate_release_state(
    *,
    metadata_path: Path,
    qc_path: Path,
    signer_review_path: Path,
    release_manifest_path: Path,
    run_id: str,
    gloss: str,
) -> None:
    """Fail closed before any artifact from a new run can replace old output."""
    for state_path in (metadata_path, qc_path):
        payload = read_json(state_path, {})
        if not payload:
            continue
        payload["run_id"] = run_id
        payload["production_status"] = "PROCESSING"
        payload["production_eligible"] = False
        payload["engineering_candidate"] = False
        production = payload.get("production")
        if isinstance(production, dict):
            production["production_status"] = "PROCESSING"
            production["production_eligible"] = False
            production["engineering_candidate"] = False
        atomic_write_json(state_path, payload)

    previous_review = read_json(signer_review_path, {})
    if previous_review and (
        str(previous_review.get("signer_verdict") or "").upper() == "PASS"
        or str(previous_review.get("production_status") or "").upper() == "APPROVED"
    ):
        history_dir = signer_review_path.parent / "review_history"
        archive_path = history_dir / f"{signer_review_path.stem}.{uuid.uuid4().hex}.json"
        archived = deepcopy(previous_review)
        archived["review_state"] = "SUPERSEDED"
        archived["superseded_by_run_id"] = run_id
        archived["superseded_at"] = utc_now_iso()
        atomic_write_json(archive_path, archived)

    atomic_write_json(
        signer_review_path,
        {
            "schema_version": "2.0",
            "run_id": run_id,
            "gloss": gloss.upper(),
            "signer_verdict": "PENDING",
            "isl_verified": False,
            "production_status": "PROCESSING",
            "reason": "A new conversion run invalidated every earlier artifact approval.",
        },
    )
    atomic_write_json(
        release_manifest_path,
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "gloss": gloss.upper(),
            "status": "PROCESSING",
            "releaseable": False,
            "updated_at": utc_now_iso(),
        },
    )


def acquire_output_lock(output_dir: Path, run_id: str, *, wait_seconds: float = 0.0):
    """Hold a process-scoped exclusive lock for one output directory."""
    if not math.isfinite(wait_seconds) or not 0.0 <= wait_seconds <= 55.0:
        raise ValueError("Lock wait must be finite and between 0 and 55 seconds.")
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".conversion.lock"
    stream = lock_path.open("a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (OSError, BlockingIOError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stream.close()
                raise RuntimeError(
                    f"Another conversion is already writing {output_dir}."
                ) from exc
            time.sleep(min(0.1, remaining))
        except BaseException:
            stream.close()
            raise
    stream.seek(0)
    stream.truncate()
    stream.write(run_id.encode("ascii"))
    stream.flush()
    os.fsync(stream.fileno())
    stream.seek(0)
    return stream


def release_output_lock(stream) -> None:
    if stream is None or stream.closed:
        return
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def publish_validated_artifacts(
    *,
    candidate_glb_path: Path,
    glb_path: Path,
    candidate_avatar_preview_path: Path | None,
    avatar_preview_path: Path | None,
    candidate_comparison_path: Path | None,
    comparison_path: Path | None,
) -> None:
    pairs = [(candidate_glb_path, glb_path)]
    if candidate_avatar_preview_path is not None and avatar_preview_path is not None:
        pairs.append((candidate_avatar_preview_path, avatar_preview_path))
    if candidate_comparison_path is not None and comparison_path is not None:
        pairs.append((candidate_comparison_path, comparison_path))
    for source, destination in pairs:
        atomic_publish_file(source, destination)


def atomic_publish_file(source: Path, destination: Path) -> None:
    """Copy a validated binary through a same-directory temporary file."""
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"Validated candidate is missing or empty: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if sha256_file(source) != sha256_file(temporary):
            raise RuntimeError(f"Published artifact hash mismatch: {destination}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def capture_file_states(paths) -> dict[Path, bytes | None]:
    """Capture small mutable reports so approval can roll back late failure."""

    states: dict[Path, bytes | None] = {}
    for value in paths:
        path = Path(value).resolve()
        states[path] = path.read_bytes() if path.is_file() else None
    return states


def restore_file_states(states: dict[Path, bytes | None]) -> None:
    """Atomically restore the exact pre-approval report set."""

    for path, data in states.items():
        if data is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def quarantine_failed_candidate(
    candidate: Path,
    output_name: str,
    run_id: str,
    *,
    evidence_paths: dict[str, Path | None] | None = None,
) -> None:
    """Preserve a failed GLB with the exact inputs needed to revalidate it."""

    if not candidate.exists():
        return
    destination_dir = PROJECT_ROOT / "failed" / output_name / run_id
    destination = destination_dir / candidate.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"Refusing to overwrite quarantined GLB: {destination}")
    shutil.move(str(candidate), str(destination))
    snapshots = snapshot_release_dependencies(destination_dir, evidence_paths or {})
    atomic_write_json(
        destination_dir / "failure_bundle.json",
        {
            "schema_version": "1.0",
            "status": "QUARANTINED",
            "output_name": output_name,
            "gloss": candidate.stem,
            "conversion_run_id": run_id,
            "quarantined_at": utc_now_iso(),
            "candidate": release_evidence_record(destination),
            "evidence": {
                name: release_evidence_record(path)
                for name, path in snapshots.items()
                if path is not None
            },
        },
    )


def load_failure_bundle(failed_glb: Path) -> dict[str, Path]:
    """Load a complete hash-bound failed-run bundle or refuse revalidation."""

    manifest_path = failed_glb.parent / "failure_bundle.json"
    manifest = read_json(manifest_path, {})
    if manifest.get("schema_version") != "1.0":
        raise RuntimeError(
            "Failed candidate has no supported hash-bound failure bundle; rerun the source conversion."
        )
    root = failed_glb.parent.resolve()
    candidate = manifest.get("candidate")
    if not isinstance(candidate, dict):
        raise RuntimeError("Failure bundle candidate record is missing.")
    candidate_path = _verified_bundle_record(candidate, root, "candidate")
    if candidate_path != failed_glb.resolve():
        raise RuntimeError("Failure bundle candidate path does not match the selected GLB.")

    records = manifest.get("evidence")
    if not isinstance(records, dict):
        raise RuntimeError("Failure bundle evidence is missing.")
    required = {
        "source_video",
        "avatar",
        "pose",
        "motion",
        "avatar_profile",
        "bone_map",
        "neutral_hand_pose",
        "glb_validation",
        "khronos_validation",
        "settings",
        "qc_thresholds",
    }
    if {"video_preparation", "working_video"}.intersection(records):
        required.update({"video_preparation", "working_video"})
    missing = sorted(required.difference(records))
    if missing:
        raise RuntimeError("Failure bundle is incomplete: " + ", ".join(missing))
    paths = {
        name: _verified_bundle_record(record, root, name)
        for name, record in records.items()
    }
    validation = read_json(paths["glb_validation"], {})
    expected_ik_hash = validation.get("ik_report_sha256")
    if expected_ik_hash is not None:
        if not isinstance(expected_ik_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_ik_hash) is None:
            raise RuntimeError("Failure bundle validation has an invalid IK correction report hash.")
        ik_report = paths.get("ik_report")
        if ik_report is None:
            raise RuntimeError("Failure bundle is missing the IK correction report required by GLB validation.")
        if sha256_file(ik_report) != expected_ik_hash:
            raise RuntimeError("Failure bundle IK correction report does not match GLB validation.")
    return paths


def _verified_bundle_record(record: Any, root: Path, name: str) -> Path:
    if not isinstance(record, dict):
        raise RuntimeError(f"Failure bundle {name} record is invalid.")
    value = record.get("path")
    expected_hash = record.get("sha256")
    expected_size = record.get("size_bytes")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Failure bundle {name} path is invalid.")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise RuntimeError(f"Failure bundle {name} hash is invalid.")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise RuntimeError(f"Failure bundle {name} size is invalid.")
    path = resolve_project_path(value)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Failure bundle {name} escapes its run directory.") from exc
    if not path.is_file() or path.stat().st_size != expected_size:
        raise RuntimeError(f"Failure bundle {name} is missing or has the wrong size.")
    if sha256_file(path) != expected_hash:
        raise RuntimeError(f"Failure bundle {name} hash does not match.")
    return path


def verify_release_evidence(
    metadata: dict[str, Any],
    *,
    ignored_names: set[str] | None = None,
) -> tuple[bool, list[str]]:
    ignored_names = ignored_names or set()
    records = metadata.get("release_evidence")
    if not isinstance(records, dict) or not records:
        return False, ["metadata release_evidence is missing"]
    reasons: list[str] = []
    required = {
        "source_video",
        "avatar",
        "stable_glb",
        "run_glb",
        "pose",
        "motion",
        "qc",
        "glb_validation",
        "khronos_validation",
        "source_avatar_validation",
        "signer_review",
        "comparison_video",
        "avatar_profile",
        "bone_map",
        "neutral_hand_pose",
    }
    preparation_required = bool(metadata.get("preparation")) or bool(
        {"video_preparation", "working_video"}.intersection(records)
    )
    if preparation_required:
        required.update({"video_preparation", "working_video"})
    validation_evidence = metadata.get("technical_validation", {}).get("glb_validation", metadata.get("glb_validation", {}))
    if validation_evidence.get("ik_report_sha256"):
        required.add("ik_report")
        if records.get("ik_report", {}).get("sha256") != validation_evidence["ik_report_sha256"]:
            reasons.append("export correction report is not bound to the validated GLB")
    missing = sorted(required.difference(records).difference(ignored_names))
    if missing:
        reasons.append("missing release evidence: " + ", ".join(missing))
    for name, record in records.items():
        if name in ignored_names:
            continue
        if not isinstance(record, dict):
            reasons.append(f"{name} evidence is not an object")
            continue
        value = record.get("path")
        expected_hash = record.get("sha256")
        expected_size = record.get("size_bytes")
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"{name} evidence path is invalid")
            continue
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            reasons.append(f"{name} evidence hash is invalid")
            continue
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
            reasons.append(f"{name} evidence size is invalid")
            continue
        try:
            path = resolve_project_path(value)
            current_hash = sha256_file(path)
            current_size = path.stat().st_size if path.is_file() else None
        except (OSError, TypeError, ValueError) as exc:
            reasons.append(f"{name} evidence could not be read: {exc}")
            continue
        if not current_hash or current_hash != expected_hash:
            reasons.append(f"{name} hash does not match")
        if current_size is None or current_size != expected_size:
            reasons.append(f"{name} size does not match")
    if preparation_required and not {"video_preparation", "working_video"}.intersection(ignored_names):
        preparation_paths, _ = resolve_release_evidence_paths(metadata)
        preparation_path = preparation_paths.get("video_preparation")
        working_path = preparation_paths.get("working_video")
        if preparation_path is not None and working_path is not None:
            try:
                expected_preparation = preparation_summary(preparation_path, working_path)
                if expected_preparation != metadata.get("preparation"):
                    reasons.append("video preparation evidence does not match metadata")
                preparation = read_json(preparation_path, {})
                working = preparation.get("working", {})
                source = preparation.get("source", {})
                mapping = preparation.get("timing_mapping")
                if not isinstance(mapping, list) or len(mapping) != working.get("frame_count"):
                    reasons.append("video preparation timing map does not cover every working frame")
                validation = metadata.get("technical_validation", {}).get("source_avatar_validation", {})
                if validation.get("working_video_sha256") != sha256_file(working_path):
                    reasons.append("source/avatar validation is not bound to the working video")
                if validation.get("source_frame_count") != working.get("frame_count"):
                    reasons.append("source/avatar frame count does not match prepared video")
                if validation.get("original_source_frame_count") != source.get("frame_count"):
                    reasons.append("original source frame count does not match preparation evidence")
                if validation.get("timing_normalized") is not preparation.get("normalized"):
                    reasons.append("source/avatar timing normalization does not match preparation evidence")
            except (OSError, TypeError, ValueError, AttributeError) as exc:
                reasons.append(f"video preparation evidence is invalid: {exc}")
    return not reasons, reasons


def release_evidence_path(metadata: dict[str, Any], name: str) -> Path:
    """Resolve one already-verified evidence record without silent fallback."""

    records = metadata.get("release_evidence")
    record = records.get(name) if isinstance(records, dict) else None
    value = record.get("path") if isinstance(record, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Release evidence path is missing: {name}")
    path = resolve_project_path(value)
    if not path.is_file():
        raise RuntimeError(f"Release evidence file is missing: {name}: {path}")
    return path


def write_release_manifest(
    path: Path,
    metadata: dict[str, Any],
    glb_path: Path,
    run_id: str,
    *,
    immutable_glb_path: Path | None = None,
) -> None:
    production = metadata.get("production")
    production = production if isinstance(production, dict) else {}
    integrity = metadata.get("file_integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    immutable_glb_path = immutable_glb_path or glb_path
    expected_hash = integrity.get("glb_sha256")
    expected_size = integrity.get("glb_size_bytes")
    metadata_run_id = metadata.get("run_id")
    processing = metadata.get("processing")
    if not metadata_run_id and isinstance(processing, dict):
        metadata_run_id = processing.get("run_id")
    validation = metadata.get("technical_validation")
    validation = validation if isinstance(validation, dict) else {}
    glb_validation = validation.get("glb_validation")
    glb_validation = glb_validation if isinstance(glb_validation, dict) else {}
    validated_hash = glb_validation.get("validated_glb_sha256")
    khronos_validation = glb_validation.get("khronos_validation")
    khronos_validation = khronos_validation if isinstance(khronos_validation, dict) else {}
    khronos_counts = khronos_validation.get("counts")
    khronos_counts = khronos_counts if isinstance(khronos_counts, dict) else {}

    run_id_valid = isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) is not None
    metadata_run_id_valid = (
        isinstance(metadata_run_id, str)
        and RUN_ID_PATTERN.fullmatch(metadata_run_id) is not None
    )
    immutable_in_run_dir = False
    if run_id_valid:
        expected_run_root = (glb_path.parent / "runs" / run_id).resolve()
        runs_root = (glb_path.parent / "runs").resolve()
        try:
            expected_run_root.relative_to(runs_root)
            immutable_glb_path.resolve().relative_to(expected_run_root)
            immutable_in_run_dir = True
        except ValueError:
            immutable_in_run_dir = False

    evidence_pass, evidence_reasons = verify_release_evidence(metadata)

    actual_hash = sha256_file(immutable_glb_path)
    stable_hash = sha256_file(glb_path)
    actual_size = immutable_glb_path.stat().st_size if immutable_glb_path.is_file() else None
    integrity_checks = {
        "run_id_valid": run_id_valid,
        "metadata_run_id_valid": metadata_run_id_valid,
        "metadata_run_id_matches": metadata_run_id == run_id,
        "immutable_artifact_in_run_directory": immutable_in_run_dir,
        "immutable_artifact_exists": immutable_glb_path.is_file() and bool(actual_size),
        "stable_alias_exists": glb_path.is_file() and glb_path.stat().st_size > 0,
        "immutable_hash_matches_metadata": bool(
            actual_hash and expected_hash and actual_hash == expected_hash
        ),
        "stable_hash_matches_metadata": bool(
            stable_hash and expected_hash and stable_hash == expected_hash
        ),
        "size_matches_metadata": actual_size == expected_size and actual_size is not None,
        "validator_hash_matches_metadata": bool(
            validated_hash and expected_hash and validated_hash == expected_hash
        ),
        "khronos_validation_pass": str(khronos_validation.get("status") or "").upper() == "PASS",
        "khronos_hash_matches_metadata": bool(
            khronos_validation.get("validated_glb_sha256")
            and khronos_validation.get("validated_glb_sha256") == expected_hash
        ),
        "khronos_error_count_zero": khronos_counts.get("numErrors") == 0,
        "release_evidence_matches": evidence_pass,
    }
    integrity_pass = all(integrity_checks.values())
    releaseable = (
        production.get("production_eligible") is True
        and str(production.get("production_status") or "").upper() == "APPROVED"
        and integrity_pass
    )
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "status": (
            str(production.get("production_status") or "NOT_ELIGIBLE")
            if integrity_pass
            else "INTEGRITY_FAILURE"
        ),
        "releaseable": releaseable,
        "engineering_candidate": production.get("engineering_candidate") is True,
        "artifact": {
            "path": relative_display(immutable_glb_path),
            "sha256": actual_hash,
            "size_bytes": actual_size,
        },
        "stable_alias": {
            "path": relative_display(glb_path),
            "sha256": stable_hash,
        },
        "integrity_checks": integrity_checks,
        "integrity_reasons": evidence_reasons,
        "evidence": deepcopy(metadata.get("release_evidence") or {}),
        "release_gate": deepcopy(production.get("release_gate") or {}),
        "updated_at": utc_now_iso(),
    }
    if releaseable:
        payload["released_at"] = utc_now_iso()
    atomic_write_json(path, payload)
    if not integrity_pass:
        raise RuntimeError(
            "Release manifest integrity commit failed: "
            + ", ".join(name for name, passed in integrity_checks.items() if not passed)
        )


def should_save_debug(args: argparse.Namespace, config: dict[str, Any]) -> bool:
    return bool(args.save_debug or config.get("output", {}).get("save_debug", True))


def print_header(video_path: Path, avatar_path: Path) -> None:
    print("=" * 51)
    print("ISL VIDEO -> GLB CONVERTER")
    print("=" * 51)
    print("\nInput:")
    print(video_path.name)
    print("\nAvatar:")
    print(avatar_path.name)
    print()


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def default_blender_path() -> str:
    return "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"


def relative_display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def max_status(current: str, candidate: str) -> str:
    order = {"PASS": 0, "REVIEW": 1, "FAIL": 2}
    return candidate if order.get(candidate, 2) > order.get(current, 2) else current


if __name__ == "__main__":
    raise SystemExit(main())
