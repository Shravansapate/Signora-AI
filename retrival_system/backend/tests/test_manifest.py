from datetime import UTC, datetime

import pytest

from app.retrieval.manifest import ManifestBuildError, MotionSelection, compile_manifest


def selection(concept_id: str, *, eligible: bool = True) -> MotionSelection:
    return MotionSelection(
        concept_id=concept_id,
        motion_version_id=f"{concept_id}-v1",
        asset_key=f"railway/{concept_id}/v1.glb",
        sha256="a" * 64,
        clip_name=concept_id,
        duration_seconds=1.25,
        semantic_group="announcement",
        transition_policy_id="reviewed-default-v1",
        eligible=eligible,
        rejection_reason=None if eligible else "linguistic review pending",
    )


def compile_test_manifest(*selections: MotionSelection):
    issued = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    return compile_manifest(
        manifest_id="manifest-1",
        manifest_revision=1,
        message_id="message-1",
        message_revision=1,
        station_id="NDLS",
        display_group="platforms",
        template_id="train-arrival",
        template_version=1,
        avatar_profile_id="avatar-1",
        avatar_version="2026-01",
        intent="TRAIN_ARRIVAL",
        slots={"train_identifier": "12870", "platform_identifier": "3"},
        caption_text="Train number 12870 is arriving on platform 3.",
        issued_at=issued,
        valid_until=datetime(2026, 9, 12, 10, 5, tzinfo=UTC),
        selections=list(selections),
    )


def test_manifest_preserves_order_and_repeated_occurrences():
    manifest = compile_test_manifest(selection("TRAIN"), selection("DIGIT_1"), selection("DIGIT_1"))

    assert [item.sequence_index for item in manifest.items] == [0, 1, 2]
    assert [item.concept_id for item in manifest.items] == ["TRAIN", "DIGIT_1", "DIGIT_1"]
    assert len(manifest.manifest_hash) == 64


def test_manifest_hash_is_stable_for_equivalent_input():
    first = compile_test_manifest(selection("TRAIN"))
    second = compile_test_manifest(selection("TRAIN"))

    assert first.manifest_hash == second.manifest_hash


def test_ineligible_motion_blocks_complete_manifest():
    with pytest.raises(ManifestBuildError, match="linguistic review pending"):
        compile_test_manifest(selection("TRAIN"), selection("ARRIVE", eligible=False))


def test_manifest_requires_timezone_and_forward_validity():
    issued = datetime(2026, 9, 12, 10, 0)
    with pytest.raises(ManifestBuildError, match="timezone"):
        compile_manifest(
            manifest_id="manifest-1",
            manifest_revision=1,
            message_id="message-1",
            message_revision=1,
            station_id="NDLS",
            display_group="platforms",
            template_id="train-arrival",
            template_version=1,
            avatar_profile_id="avatar-1",
            avatar_version="2026-01",
            intent="TRAIN_ARRIVAL",
            slots={},
            caption_text="Arrival",
            issued_at=issued,
            valid_until=issued,
            selections=[selection("TRAIN")],
        )
