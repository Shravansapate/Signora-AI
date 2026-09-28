"""Explicit development selection and retained metadata revisions."""

from alembic import op

revision = "0007_library_workflow"
down_revision = "0006_live_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE library_selections (
        concept_id uuid PRIMARY KEY REFERENCES sign_concepts(id),
        motion_version_id uuid, enabled boolean NOT NULL DEFAULT false,
        FOREIGN KEY(concept_id,motion_version_id) REFERENCES motion_versions(concept_id,id),
        CHECK(NOT enabled OR motion_version_id IS NOT NULL)
      );
      CREATE TABLE motion_metadata_revisions (
        id uuid PRIMARY KEY, motion_version_id uuid NOT NULL REFERENCES motion_versions(id),
        metadata_sha256 varchar(64) NOT NULL, payload jsonb NOT NULL,
        actor text NOT NULL, reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX metadata_version_history
        ON motion_metadata_revisions(motion_version_id,created_at);
      CREATE OR REPLACE FUNCTION protect_motion_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(OLD.concept_id,OLD.version_no,OLD.storage_key,OLD.sha256,OLD.size_bytes,
          OLD.clip_name,OLD.duration_seconds,OLD.avatar_profile_id,OLD.metadata_sha256)
        IS DISTINCT FROM ROW(NEW.concept_id,NEW.version_no,NEW.storage_key,
          NEW.sha256,NEW.size_bytes,
          NEW.clip_name,NEW.duration_seconds,NEW.avatar_profile_id,NEW.metadata_sha256)
        OR (OLD.source_metadata IS DISTINCT FROM NEW.source_metadata AND NOT
          (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NOT NULL
          AND NEW.source_metadata='{}'::jsonb))
        THEN RAISE EXCEPTION 'motion identity is immutable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
    """)


def downgrade():
    op.execute("""
      DROP TABLE motion_metadata_revisions;
      DROP TABLE library_selections;
      CREATE OR REPLACE FUNCTION protect_motion_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(OLD.concept_id,OLD.version_no,OLD.storage_key,OLD.sha256,
          OLD.size_bytes,OLD.clip_name,
          OLD.duration_seconds,OLD.avatar_profile_id,OLD.metadata_sha256,OLD.source_metadata)
        IS DISTINCT FROM ROW(NEW.concept_id,NEW.version_no,NEW.storage_key,
          NEW.sha256,NEW.size_bytes,
          NEW.clip_name,NEW.duration_seconds,NEW.avatar_profile_id,NEW.metadata_sha256,NEW.source_metadata)
        THEN RAISE EXCEPTION 'motion identity is immutable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
    """)
