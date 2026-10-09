"""Upgrade a populated phase-1 catalog and roll back without losing its concepts."""
# ruff: noqa: F811 -- pytest discovers this imported fixture.

from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import text
from test_registry import registry_database  # noqa: F401

from alembic import command


def test_existing_catalog_backfill_and_phase2_only_rollback(registry_database):
    _, engine, _ = registry_database
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.downgrade(config, "0001_registry")
    concept_id = uuid4()
    original = "  StraßE \t Bahn  "
    with engine.begin() as connection:
        connection.execute(
            text("""
          INSERT INTO sign_concepts(id,semantic_key,gloss,canonical_text,language_code,level)
          VALUES (:id,'ISOLATED_MIGRATION_FIXTURE','FIXTURE',:value,'ISL','WORD')
        """),
            {"id": concept_id, "value": original},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(
            text("""
          SELECT canonical_text, normalized_canonical_text, enabled
          FROM sign_concepts WHERE id=:id
        """),
            {"id": concept_id},
        ).one()
        assert row.canonical_text == original
        assert row.normalized_canonical_text == "strasse bahn"
        assert row.enabled is False
    command.downgrade(config, "0001_registry")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT canonical_text FROM sign_concepts WHERE id=:id"), {"id": concept_id}
            )
            == original
        )
        assert connection.scalar(text("SELECT to_regclass('playback_manifests')")) is None
    command.upgrade(config, "head")
