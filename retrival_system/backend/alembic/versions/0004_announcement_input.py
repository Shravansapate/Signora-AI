"""Reviewed constructions, scoped station input, and private voice/meaning receipts."""

from alembic import op

revision = "0004_announcement_input"
down_revision = "0003_lifecycle_import"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE stations (
        id text PRIMARY KEY, definition jsonb NOT NULL, revision integer NOT NULL CHECK(revision>0)
      );
      CREATE TABLE announcement_templates (
        id uuid PRIMARY KEY, template_key text NOT NULL, version_no integer NOT NULL,
        definition jsonb NOT NULL, definition_hash text NOT NULL,
        revision integer NOT NULL DEFAULT 1, enabled boolean NOT NULL DEFAULT false,
        status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
        review_id uuid REFERENCES content_reviews(id), bindings jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(template_key,version_no),
        CHECK(NOT enabled OR (status='APPROVED' AND review_id IS NOT NULL))
      );
      CREATE UNIQUE INDEX one_enabled_template_version ON announcement_templates(template_key)
        WHERE enabled;
      CREATE TABLE template_dependencies (
        template_version_id uuid REFERENCES announcement_templates(id),
        concept_id uuid REFERENCES sign_concepts(id), PRIMARY KEY(template_version_id,concept_id)
      );
      CREATE TABLE template_motion_bindings (
        review_id uuid REFERENCES content_reviews(id),
        template_version_id uuid NOT NULL REFERENCES announcement_templates(id),
        motion_version_id uuid REFERENCES motion_versions(id),
        PRIMARY KEY(review_id,motion_version_id)
      );
      CREATE INDEX template_motion_retention ON template_motion_bindings(motion_version_id);
      CREATE TABLE template_previews (
        manifest_id uuid PRIMARY KEY REFERENCES playback_manifests(id),
        template_version_id uuid NOT NULL REFERENCES announcement_templates(id),
        definition_hash text NOT NULL, meaning jsonb NOT NULL, bindings jsonb NOT NULL
      );
      CREATE TABLE voice_transcripts (
        id uuid PRIMARY KEY, owner_subject text NOT NULL, text text NOT NULL,
        audio_sha256 text NOT NULL, metadata jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), valid_until timestamptz NOT NULL
      );
      CREATE TABLE announcement_inputs (
        id uuid PRIMARY KEY, request_id uuid NOT NULL, owner_subject text NOT NULL,
        request_hash text NOT NULL, station_id text NOT NULL REFERENCES stations(id),
        input_type text NOT NULL CHECK(input_type IN ('TEXT','VOICE','STRUCTURED')),
        transcript_id uuid REFERENCES voice_transcripts(id), response jsonb NOT NULL,
        manifest_id uuid REFERENCES playback_manifests(id),
        created_at timestamptz NOT NULL DEFAULT now(), valid_until timestamptz NOT NULL,
        UNIQUE(owner_subject,request_id)
      );
      CREATE TABLE announcement_manifest_refs (
        manifest_id uuid PRIMARY KEY REFERENCES playback_manifests(id),
        template_version_id uuid NOT NULL REFERENCES announcement_templates(id),
        review_id uuid NOT NULL REFERENCES content_reviews(id),
        station_id text NOT NULL REFERENCES stations(id),
        input_id uuid NOT NULL REFERENCES announcement_inputs(id)
      );
      ALTER TABLE playback_manifests DROP CONSTRAINT playback_manifests_purpose_check;
      ALTER TABLE playback_manifests ADD CONSTRAINT playback_manifests_purpose_check
        CHECK(purpose IN ('CONTENT_REVIEW','EXACT_CONTENT','ANNOUNCEMENT_PREVIEW'));
      CREATE FUNCTION protect_template_definition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(OLD.id,OLD.template_key,OLD.version_no,OLD.definition,OLD.definition_hash)
          IS DISTINCT FROM ROW(NEW.id,NEW.template_key,NEW.version_no,
                               NEW.definition,NEW.definition_hash)
        THEN RAISE EXCEPTION 'Template definitions are immutable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_template_definition BEFORE UPDATE ON announcement_templates
        FOR EACH ROW EXECUTE FUNCTION protect_template_definition();
    """)
    for table in (
        "template_previews",
        "template_dependencies",
        "template_motion_bindings",
        "voice_transcripts",
        "announcement_inputs",
        "announcement_manifest_refs",
    ):
        op.execute(f"""
          CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_playback_mutation()
        """)


def downgrade():
    # Do not erase retained phase-4 snapshots merely to make an older schema accept them.
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM playback_manifests WHERE purpose='ANNOUNCEMENT_PREVIEW')
        THEN RAISE EXCEPTION 'Retained phase 4 plans require a paired pre-upgrade backup rollback';
        END IF;
      END $$;
      DROP TABLE announcement_manifest_refs, announcement_inputs, voice_transcripts,
        template_previews, template_motion_bindings, template_dependencies,
        announcement_templates, stations;
      DROP FUNCTION protect_template_definition();
      ALTER TABLE playback_manifests DROP CONSTRAINT playback_manifests_purpose_check;
      ALTER TABLE playback_manifests ADD CONSTRAINT playback_manifests_purpose_check
        CHECK(purpose IN ('CONTENT_REVIEW','EXACT_CONTENT'));
    """)
