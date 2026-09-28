"""Registry, immutable versions, exact-version approval and catalog constraints."""

from alembic import op

revision = "0001_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE TABLE avatar_profiles (
      id uuid PRIMARY KEY, rig_fingerprint varchar(64) NOT NULL UNIQUE,
      fingerprint_version integer NOT NULL CHECK (fingerprint_version > 0),
      source_sha256 varchar(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
      status varchar(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','APPROVED','RETIRED'))
    );
    CREATE TABLE sign_concepts (
      id uuid PRIMARY KEY, semantic_key varchar(160) NOT NULL UNIQUE,
      gloss text NOT NULL, canonical_text text NOT NULL, language_code varchar(8) NOT NULL,
      domain text, level varchar(24) NOT NULL, meaning text, context text,
      meaning_status varchar(16) NOT NULL DEFAULT 'PENDING'
        CHECK (meaning_status IN ('PENDING','APPROVED','REJECTED')),
      enabled boolean NOT NULL DEFAULT false,
      semantic_revision integer NOT NULL DEFAULT 1 CHECK (semantic_revision > 0),
      revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
      active_motion_version_id uuid
    );
    CREATE TABLE motion_versions (
      id uuid PRIMARY KEY, concept_id uuid NOT NULL REFERENCES sign_concepts(id),
      version_no integer NOT NULL CHECK(version_no > 0), storage_key text NOT NULL,
      sha256 varchar(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
      size_bytes bigint NOT NULL CHECK(size_bytes > 0 AND size_bytes <= 134217728),
      clip_name text NOT NULL,
      duration_seconds double precision NOT NULL
        CHECK(duration_seconds > 0 AND duration_seconds < 'Infinity'::float8),
      avatar_profile_id uuid NOT NULL REFERENCES avatar_profiles(id),
      technical_qc_status varchar(16) NOT NULL
        CHECK(technical_qc_status IN ('PENDING','PASSED','FAILED')),
      linguistic_review_status varchar(16) NOT NULL
        CHECK(linguistic_review_status IN ('PENDING','APPROVED','REJECTED')),
      composition_review_status varchar(16) NOT NULL
        CHECK(composition_review_status IN ('PENDING','APPROVED','REJECTED')),
      reviewed_sha256 varchar(64), reviewed_avatar_profile_id uuid REFERENCES avatar_profiles(id),
      reviewer text, reviewed_at timestamptz,
      lifecycle_status varchar(16) NOT NULL
        CHECK(lifecycle_status IN ('STAGING','ACTIVE','ARCHIVED','REJECTED')),
      revoked_at timestamptz, metadata_sha256 varchar(64) NOT NULL,
      source_metadata jsonb NOT NULL, technical_report jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(concept_id, version_no), UNIQUE(concept_id, sha256), UNIQUE(concept_id, id),
      CHECK(storage_key ~ '^sha256/[0-9a-f]{2}/[0-9a-f]{64}[.]glb$'),
      CHECK(lifecycle_status <> 'ACTIVE' OR (
        technical_qc_status = 'PASSED' AND linguistic_review_status = 'APPROVED'
        AND composition_review_status = 'APPROVED' AND revoked_at IS NULL
        AND reviewed_sha256 IS NOT NULL AND reviewed_sha256 = sha256
        AND reviewed_avatar_profile_id IS NOT NULL
        AND reviewed_avatar_profile_id = avatar_profile_id
        AND reviewer IS NOT NULL AND length(trim(reviewer)) > 0 AND reviewed_at IS NOT NULL))
    );
    CREATE UNIQUE INDEX one_active_motion ON motion_versions(concept_id)
      WHERE lifecycle_status='ACTIVE';
    ALTER TABLE sign_concepts ADD CONSTRAINT active_motion_same_concept
      FOREIGN KEY(id, active_motion_version_id) REFERENCES motion_versions(concept_id,id)
      DEFERRABLE INITIALLY DEFERRED;
    CREATE TABLE sign_aliases (
      id uuid PRIMARY KEY, concept_id uuid NOT NULL REFERENCES sign_concepts(id),
      source_language varchar(16), alias text NOT NULL, normalized_alias text NOT NULL,
      review_status varchar(16) NOT NULL CHECK(review_status IN ('PENDING','APPROVED','REJECTED')),
      UNIQUE(concept_id,normalized_alias),
      CHECK(review_status <> 'APPROVED' OR source_language IS NOT NULL)
    );
    CREATE INDEX alias_exact ON sign_aliases(normalized_alias, source_language);
    CREATE INDEX alias_trigram ON sign_aliases USING gin(normalized_alias gin_trgm_ops);
    CREATE TABLE admin_audit_logs (
      id uuid PRIMARY KEY, actor text NOT NULL, action varchar(64) NOT NULL,
      entity_id uuid NOT NULL, details jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE registry_events (
      id uuid PRIMARY KEY, event_type varchar(64) NOT NULL,
      entity_id uuid NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE FUNCTION check_active_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE cid uuid;
    BEGIN
      IF TG_TABLE_NAME = 'sign_concepts' THEN cid := COALESCE(NEW.id,OLD.id);
      ELSE cid := COALESCE(NEW.concept_id,OLD.concept_id); END IF;
      IF EXISTS(SELECT 1 FROM sign_concepts c WHERE c.id=cid AND
        ((c.active_motion_version_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM motion_versions m WHERE m.id=c.active_motion_version_id
          AND m.concept_id=c.id AND m.lifecycle_status='ACTIVE'))
        OR EXISTS(SELECT 1 FROM motion_versions m WHERE m.concept_id=c.id
          AND m.lifecycle_status='ACTIVE' AND m.id IS DISTINCT FROM c.active_motion_version_id)))
      THEN RAISE EXCEPTION 'active pointer and motion lifecycle disagree'
        USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER concept_pointer_consistency AFTER INSERT OR UPDATE OR DELETE
      ON sign_concepts DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION check_active_pointer();
    CREATE CONSTRAINT TRIGGER motion_pointer_consistency AFTER INSERT OR UPDATE OR DELETE
      ON motion_versions DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION check_active_pointer();
    CREATE FUNCTION protect_motion_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(OLD.concept_id,OLD.version_no,OLD.storage_key,OLD.sha256,OLD.size_bytes,OLD.clip_name,
             OLD.duration_seconds,OLD.avatar_profile_id,OLD.metadata_sha256,OLD.source_metadata)
      IS DISTINCT FROM ROW(NEW.concept_id,NEW.version_no,NEW.storage_key,NEW.sha256,NEW.size_bytes,
             NEW.clip_name,NEW.duration_seconds,NEW.avatar_profile_id,
             NEW.metadata_sha256,NEW.source_metadata)
      THEN RAISE EXCEPTION 'motion identity is immutable' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER motion_immutable BEFORE UPDATE ON motion_versions
      FOR EACH ROW EXECUTE FUNCTION protect_motion_identity();
    CREATE VIEW eligible_motion_versions AS
      SELECT m.* FROM sign_concepts c JOIN motion_versions m
      ON m.id=c.active_motion_version_id AND m.concept_id=c.id
      JOIN avatar_profiles a ON a.id=m.avatar_profile_id
      WHERE c.enabled AND c.language_code='ISL' AND c.meaning_status='APPROVED'
      AND length(trim(c.meaning))>0 AND length(trim(c.context))>0 AND c.domain IS NOT NULL
      AND a.status='APPROVED' AND m.lifecycle_status='ACTIVE' AND m.technical_qc_status='PASSED'
      AND m.linguistic_review_status='APPROVED' AND m.composition_review_status='APPROVED'
      AND m.reviewed_sha256=m.sha256 AND m.reviewed_avatar_profile_id=m.avatar_profile_id
      AND m.reviewer IS NOT NULL AND m.reviewed_at IS NOT NULL AND m.revoked_at IS NULL;
    """)


def downgrade():
    op.execute("""
    DROP VIEW eligible_motion_versions;
    DROP TRIGGER motion_immutable ON motion_versions;
    DROP TRIGGER motion_pointer_consistency ON motion_versions;
    DROP TRIGGER concept_pointer_consistency ON sign_concepts;
    DROP FUNCTION protect_motion_identity(); DROP FUNCTION check_active_pointer();
    ALTER TABLE sign_concepts DROP CONSTRAINT active_motion_same_concept;
    DROP TABLE registry_events, admin_audit_logs, sign_aliases,
      motion_versions, sign_concepts, avatar_profiles;
    """)
