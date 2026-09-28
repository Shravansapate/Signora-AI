"""Review binding, retained deletion records, and durable import work."""

from alembic import op

revision = "0003_lifecycle_import"
down_revision = "0002_playback"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE avatar_profiles ADD COLUMN revision integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE motion_versions ADD COLUMN reviewed_semantic_revision integer")
    op.execute("ALTER TABLE motion_versions ADD COLUMN deleted_at timestamptz")
    op.execute(
        "ALTER TABLE motion_versions ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute("""
      CREATE TABLE content_reviews (
        id uuid PRIMARY KEY, entity_id uuid NOT NULL, entity_type text NOT NULL,
        actor text NOT NULL, evidence text NOT NULL, reason text NOT NULL,
        decision jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE import_jobs (
        id uuid PRIMARY KEY, request_id uuid UNIQUE NOT NULL,
        source_alias text NOT NULL, source_root_hash text NOT NULL, owner_subject text NOT NULL,
        state text NOT NULL DEFAULT 'QUEUED'
          CHECK(state IN ('QUEUED','RUNNING','PAUSED','COMPLETE')),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE import_items (
        id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES import_jobs(id),
        metadata_name text NOT NULL, metadata_sha256 text, semantic_key text, asset_sha256 text,
        state text NOT NULL
          CHECK(state IN ('DISCOVERED','RUNNING','RETRY','STAGED','UNCHANGED','FAILED')),
        attempts integer NOT NULL DEFAULT 0, attempt_limit integer NOT NULL DEFAULT 3,
        lease_token uuid, lease_until timestamptz, available_at timestamptz NOT NULL DEFAULT now(),
        error_code text, error_detail text, retryable boolean NOT NULL DEFAULT false,
        motion_version_id uuid REFERENCES motion_versions(id),
        updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(job_id, metadata_name),
        CHECK(attempts >= 0 AND attempt_limit >= attempts),
        CHECK((state='RUNNING') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
      );
      CREATE INDEX import_work_queue ON import_items(state, available_at, lease_until);
      CREATE INDEX import_job_progress ON import_items(job_id, state);
      CREATE TABLE asset_cleanup_jobs (
        id uuid PRIMARY KEY, motion_version_id uuid NOT NULL UNIQUE REFERENCES motion_versions(id),
        storage_key text NOT NULL, sha256 text NOT NULL, actor text NOT NULL,
        state text NOT NULL DEFAULT 'QUEUED'
          CHECK(state IN ('QUEUED','COMPLETE','SHARED','FAILED')),
        attempts integer NOT NULL DEFAULT 0, attempt_limit integer NOT NULL DEFAULT 3,
        error_code text, CHECK(attempts >= 0 AND attempt_limit >= attempts),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE FUNCTION protect_review_record() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Review evidence is immutable' USING ERRCODE='23514'; END $$;
      CREATE TRIGGER immutable_content_review BEFORE UPDATE OR DELETE ON content_reviews
        FOR EACH ROW EXECUTE FUNCTION protect_review_record();
      CREATE FUNCTION protect_avatar_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(NEW.rig_fingerprint,NEW.fingerprint_version,NEW.source_sha256)
          IS DISTINCT FROM ROW(OLD.rig_fingerprint,OLD.fingerprint_version,OLD.source_sha256)
        THEN RAISE EXCEPTION 'Avatar identity is immutable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_avatar BEFORE UPDATE ON avatar_profiles
        FOR EACH ROW EXECUTE FUNCTION protect_avatar_identity();
      ALTER TABLE motion_versions ADD CONSTRAINT deleted_motion_inactive
        CHECK(deleted_at IS NULL OR lifecycle_status <> 'ACTIVE');
    """)
    # Do not invent missing historical approval bindings. Unbound versions stay
    # stored, but new selection requires a fresh explicit semantic-version review.
    op.execute("ALTER VIEW eligible_motion_versions RENAME TO eligible_motion_versions_phase2")
    op.execute("""
      CREATE VIEW eligible_motion_versions AS SELECT m.*
      FROM eligible_motion_versions_phase2 m JOIN motion_versions v ON v.id=m.id
      JOIN sign_concepts c ON c.id=m.concept_id
      WHERE v.deleted_at IS NULL AND v.reviewed_semantic_revision=c.semantic_revision
    """)


def downgrade():
    op.execute("DROP VIEW eligible_motion_versions")
    op.execute("ALTER VIEW eligible_motion_versions_phase2 RENAME TO eligible_motion_versions")
    op.execute("DROP TABLE asset_cleanup_jobs, import_items, import_jobs, content_reviews")
    op.execute("DROP FUNCTION protect_review_record()")
    op.execute("DROP TRIGGER immutable_avatar ON avatar_profiles")
    op.execute("DROP FUNCTION protect_avatar_identity()")
    op.execute("ALTER TABLE motion_versions DROP CONSTRAINT deleted_motion_inactive")
    op.execute("ALTER TABLE motion_versions DROP COLUMN reviewed_semantic_revision")
    op.execute("ALTER TABLE motion_versions DROP COLUMN deleted_at")
    op.execute("ALTER TABLE motion_versions DROP COLUMN updated_at")
    op.execute("ALTER TABLE avatar_profiles DROP COLUMN revision")
