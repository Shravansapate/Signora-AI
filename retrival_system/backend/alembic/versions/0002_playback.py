"""Persist immutable content playback plans and ordered version references."""

from sqlalchemy import text

from alembic import op

revision = "0002_playback"
down_revision = "0001_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE sign_concepts ADD COLUMN normalized_canonical_text text")
    connection = op.get_bind()
    for row in connection.execute(text("SELECT id, canonical_text FROM sign_concepts")):
        connection.execute(
            text("UPDATE sign_concepts SET normalized_canonical_text=:value WHERE id=:id"),
            {"id": row.id, "value": " ".join(row.canonical_text.casefold().split())},
        )
    # Backfill updates queue the phase-1 deferred pointer triggers. PostgreSQL must
    # execute them before another ALTER TABLE on this populated catalog.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE sign_concepts ALTER COLUMN normalized_canonical_text SET NOT NULL")
    op.execute("SET CONSTRAINTS ALL DEFERRED")
    op.execute("CREATE INDEX concept_exact_text ON sign_concepts(normalized_canonical_text)")
    op.execute("""
      CREATE TABLE playback_manifests (
        id uuid PRIMARY KEY,
        owner_subject text NOT NULL,
        purpose text NOT NULL CHECK (purpose IN ('CONTENT_REVIEW','EXACT_CONTENT')),
        avatar_motion_version_id uuid NOT NULL REFERENCES motion_versions(id),
        manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
        issued_at timestamptz NOT NULL,
        valid_until timestamptz NOT NULL CHECK (valid_until > issued_at),
        payload jsonb NOT NULL,
        selection_revisions jsonb NOT NULL,
        CHECK (payload->>'manifest_id' = id::text),
        CHECK (payload->>'manifest_hash' = manifest_hash),
        CHECK (payload->>'purpose' = purpose)
      )
    """)
    op.execute("""
      CREATE TABLE playback_manifest_items (
        manifest_id uuid NOT NULL REFERENCES playback_manifests(id),
        sequence_index integer NOT NULL CHECK (sequence_index BETWEEN 0 AND 63),
        motion_version_id uuid NOT NULL REFERENCES motion_versions(id),
        PRIMARY KEY (manifest_id, sequence_index)
      )
    """)
    op.execute("CREATE INDEX playback_expiration ON playback_manifests(valid_until)")
    op.execute("CREATE INDEX playback_version_refs ON playback_manifest_items(motion_version_id)")
    op.execute("""
      CREATE FUNCTION reject_playback_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Playback snapshots are immutable' USING ERRCODE='23514'; END $$
    """)
    for table in ("playback_manifests", "playback_manifest_items"):
        op.execute(f"""
          CREATE TRIGGER immutable_playback BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION reject_playback_mutation()
        """)
    op.execute("""
      CREATE FUNCTION check_playback_references() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE plan_id uuid; snapshot jsonb; expected_count integer;
      BEGIN
        IF TG_TABLE_NAME='playback_manifests' THEN plan_id := NEW.id;
        ELSE plan_id := NEW.manifest_id; END IF;
        SELECT payload INTO snapshot FROM playback_manifests WHERE id=plan_id;
        expected_count := jsonb_array_length(snapshot->'items');
        IF expected_count NOT BETWEEN 1 AND 64 OR
          (SELECT count(*) FROM playback_manifest_items WHERE manifest_id=plan_id)
            <> expected_count OR
          EXISTS (SELECT 1 FROM playback_manifest_items i WHERE i.manifest_id=plan_id AND
            (snapshot->'items'->i.sequence_index->>'motion_version_id')::uuid
              IS DISTINCT FROM i.motion_version_id)
        THEN RAISE EXCEPTION 'Playback references must match the complete ordered snapshot'
          USING ERRCODE='23514'; END IF;
        RETURN NULL;
      END $$
    """)
    for table in ("playback_manifests", "playback_manifest_items"):
        op.execute(f"""
          CREATE CONSTRAINT TRIGGER complete_playback_references AFTER INSERT ON {table}
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_playback_references()
        """)


def downgrade():
    op.execute("DROP TABLE playback_manifest_items")
    op.execute("DROP TABLE playback_manifests")
    op.execute("DROP FUNCTION reject_playback_mutation()")
    op.execute("DROP FUNCTION check_playback_references()")
    op.execute("ALTER TABLE sign_concepts DROP COLUMN normalized_canonical_text")
