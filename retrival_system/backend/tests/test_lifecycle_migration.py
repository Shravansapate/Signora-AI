"""Phase 3 migration preserves phase 2 content and pinned playback records."""
# ruff: noqa: F811 -- isolated module-scoped PostgreSQL fixture reuse.

from pathlib import Path
from uuid import UUID

from alembic.config import Config
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from alembic import command
from app.models import PlaybackRecord
from app.playback import prepare_review, validate_record
from app.registry import stage_motion
from app.retrieval.playback_schema import ReviewRequest
from app.storage import LocalAssetStore


def test_populated_phase2_upgrade_and_phase3_roundtrip(registry_database):
    settings, engine, sessions = registry_database
    store = LocalAssetStore(settings.storage_root)
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    with sessions() as session:
        staged = stage_motion(
            library / "metadata/Train.metadata.json",
            library / "glb/Train.glb",
            "isolated migration fixture",
            session,
            store,
        )
        motion_id = UUID(staged["motion_version_id"])
        result = prepare_review(
            session,
            store,
            ReviewRequest(avatar_motion_version_id=motion_id, motion_version_ids=[motion_id]),
            "isolated migration fixture",
        )
    assert result.status == "READY"
    plan = result.manifest
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    # Downgrade an isolated installation to seed the actual phase 2 schema.
    command.downgrade(config, "0002_playback")
    with engine.begin() as connection:
        source = connection.scalar(text("SELECT source_metadata FROM motion_versions"))
        connection.execute(text("UPDATE avatar_profiles SET status='APPROVED'"))
        connection.execute(
            text("""
            UPDATE sign_concepts SET enabled=true, domain='test', meaning='fixture meaning',
              context='isolated migration test', meaning_status='APPROVED'
        """)
        )
        connection.execute(
            text("""
            UPDATE motion_versions SET linguistic_review_status='APPROVED',
              composition_review_status='APPROVED', reviewed_sha256=sha256,
              reviewed_avatar_profile_id=avatar_profile_id, reviewer='synthetic migration fixture',
              reviewed_at=now(), lifecycle_status='ACTIVE'
        """)
        )
        connection.execute(
            text("UPDATE sign_concepts SET active_motion_version_id=:id"), {"id": motion_id}
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 1
    for round_index in range(2):
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT source_metadata FROM motion_versions")) == source
            assert connection.scalar(text("SELECT count(*) FROM playback_manifest_items")) == 1
            assert connection.scalar(text("SELECT manifest_hash FROM playback_manifests")) == (
                plan.manifest_hash
            )
            # Upgrading cannot fabricate the missing exact semantic-version approval.
            assert (
                connection.scalar(text("SELECT reviewed_semantic_revision FROM motion_versions"))
                is None
            )
            assert connection.scalar(text("SELECT count(*) FROM eligible_motion_versions")) == 0
        with sessions() as session, session.begin():
            record = session.get(PlaybackRecord, plan.manifest_id)
            retained, _ = validate_record(session, store, record)
            assert retained.manifest_hash == plan.manifest_hash
        if round_index == 0:
            command.downgrade(config, "0002_playback")
