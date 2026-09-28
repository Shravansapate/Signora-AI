"""Atomic publication, station commit order, durable display recovery and invalidation."""

from alembic import op

revision = "0006_live_delivery"
down_revision = "0005_progressive_retrieval"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE playback_manifests DROP CONSTRAINT playback_manifests_purpose_check;
      ALTER TABLE playback_manifests ADD CONSTRAINT playback_manifests_purpose_check
        CHECK(purpose IN ('CONTENT_REVIEW','EXACT_CONTENT','ANNOUNCEMENT_PREVIEW','PUBLISHED'));
      CREATE TABLE station_streams (
        station_id text PRIMARY KEY REFERENCES stations(id), cursor bigint NOT NULL DEFAULT 0,
        replay_floor bigint NOT NULL DEFAULT 0, CHECK(cursor>=replay_floor AND replay_floor>=0)
      );
      CREATE TABLE announcements (
        id uuid PRIMARY KEY, station_id text NOT NULL REFERENCES stations(id),
        source_subject text NOT NULL, source_event_id text NOT NULL,
        current_revision integer NOT NULL CHECK(current_revision>0),
        source_revision bigint NOT NULL CHECK(source_revision>0),
        state text NOT NULL CHECK(state IN ('LIVE','CANCELLED','WITHDRAWN')),
        UNIQUE(station_id,source_subject,source_event_id)
      );
      CREATE TABLE announcement_revisions (
        message_id uuid REFERENCES announcements(id), revision integer NOT NULL,
        source_revision bigint NOT NULL CHECK(source_revision>0), request_hash text NOT NULL,
        manifest_id uuid UNIQUE REFERENCES playback_manifests(id),
        state text NOT NULL CHECK(state IN ('LIVE','CANCELLED')),
        priority integer NOT NULL CHECK(priority BETWEEN 0 AND 3),
        reason text NOT NULL, actor text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), valid_until timestamptz NOT NULL,
        PRIMARY KEY(message_id,revision), UNIQUE(message_id,source_revision),
        CHECK((state='LIVE')=(manifest_id IS NOT NULL))
      );
      ALTER TABLE announcements ADD CONSTRAINT current_announcement_revision
        FOREIGN KEY(id,current_revision) REFERENCES announcement_revisions(message_id,revision)
        DEFERRABLE INITIALLY DEFERRED;
      CREATE TABLE event_outbox (
        id uuid PRIMARY KEY, station_id text NOT NULL REFERENCES station_streams(station_id),
        cursor bigint NOT NULL, message_id uuid NOT NULL REFERENCES announcements(id),
        revision integer NOT NULL, event_type text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(station_id,cursor),
        FOREIGN KEY(message_id,revision) REFERENCES announcement_revisions(message_id,revision)
      );
      CREATE TABLE display_devices (
        id uuid PRIMARY KEY, station_id text NOT NULL REFERENCES stations(id),
        subject text NOT NULL UNIQUE, name text NOT NULL, enabled boolean NOT NULL DEFAULT true,
        revision integer NOT NULL DEFAULT 1, session_id uuid,
        received_cursor bigint NOT NULL DEFAULT 0 CHECK(received_cursor>=0),
        offered_cursor bigint NOT NULL DEFAULT 0, last_seen timestamptz,
        lease_until timestamptz
      );
      CREATE TABLE display_deliveries (
        display_id uuid REFERENCES display_devices(id), manifest_id uuid REFERENCES
              playback_manifests(id),
        state text NOT NULL CHECK(state IN
              ('RECEIVED','ASSETS_READY','STARTED','COMPLETED','FAILED')),
        attempt integer NOT NULL DEFAULT 1 CHECK(attempt>0), boundary integer NOT NULL DEFAULT -1,
        error_code text, updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(display_id,manifest_id)
      );
      CREATE TABLE delivery_attempts (
        display_id uuid REFERENCES display_devices(id), event_id uuid REFERENCES event_outbox(id),
        attempts integer NOT NULL DEFAULT 1, last_sent_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(display_id,event_id)
      );
      CREATE INDEX current_station_announcements ON announcements(station_id,state);
      CREATE TABLE display_acknowledgements (
        display_id uuid REFERENCES display_devices(id),
        manifest_id uuid REFERENCES playback_manifests(id), attempt integer NOT NULL,
        state text NOT NULL, boundary integer NOT NULL, error_code text,
        session_id uuid NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(display_id,manifest_id,attempt,state,boundary)
      );
      CREATE INDEX revision_freshness ON announcement_revisions(valid_until);
      CREATE FUNCTION append_station_event(station text, message uuid, rev integer, kind text)
      RETURNS bigint LANGUAGE plpgsql AS $$
      DECLARE next_cursor bigint;
      BEGIN
        INSERT INTO station_streams(station_id) VALUES(station) ON CONFLICT DO NOTHING;
        UPDATE station_streams SET cursor=cursor+1 WHERE station_id=station RETURNING cursor INTO
              next_cursor;
        INSERT INTO event_outbox(id,station_id,cursor,message_id,revision,event_type)
          VALUES(gen_random_uuid(),station,next_cursor,message,rev,kind);
        RETURN next_cursor;
      END $$;
      CREATE FUNCTION withdraw_affected_announcements() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE item record; affected boolean;
      BEGIN
        FOR item IN SELECT a.id,a.station_id,a.current_revision,p.payload
          FROM announcements a JOIN announcement_revisions r ON r.message_id=a.id AND
              r.revision=a.current_revision
          JOIN playback_manifests p ON p.id=r.manifest_id
          WHERE a.state='LIVE' AND r.valid_until>now() ORDER BY a.station_id,a.id
        LOOP
          affected := false;
          IF TG_TABLE_NAME='sign_concepts' THEN
            affected := EXISTS(SELECT 1 FROM jsonb_array_elements(item.payload->'items') v WHERE
              v->>'concept_id'=NEW.id::text);
          ELSIF TG_TABLE_NAME='motion_versions' THEN
            affected := item.payload->'avatar'->>'motion_version_id'=NEW.id::text OR
              EXISTS(SELECT 1 FROM jsonb_array_elements(item.payload->'items') v WHERE
              v->>'motion_version_id'=NEW.id::text);
          ELSIF TG_TABLE_NAME='avatar_profiles' THEN
            affected := item.payload->'avatar'->>'profile_id'=NEW.id::text;
          ELSIF TG_TABLE_NAME='announcement_templates' THEN
            affected := item.payload->'announcement'->>'template_version_id'=NEW.id::text;
          ELSIF TG_TABLE_NAME='stations' THEN affected := item.station_id=NEW.id;
          END IF;
          IF affected THEN
            PERFORM 1 FROM station_streams WHERE station_id=item.station_id FOR UPDATE;
            UPDATE announcements SET state='WITHDRAWN' WHERE id=item.id AND state='LIVE';
            IF FOUND THEN
              PERFORM
              append_station_event(item.station_id,item.id,item.current_revision,'WITHDRAWN');
            END IF;
          END IF;
        END LOOP;
        RETURN NEW;
      END $$;
      CREATE TRIGGER withdraw_concept AFTER UPDATE ON sign_concepts FOR EACH ROW
        WHEN (OLD.enabled AND NOT NEW.enabled OR OLD.semantic_revision<>NEW.semantic_revision)
        EXECUTE FUNCTION withdraw_affected_announcements();
      CREATE TRIGGER withdraw_motion AFTER UPDATE ON motion_versions FOR EACH ROW
        WHEN (NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL OR
          NEW.linguistic_review_status='REJECTED' OR NEW.composition_review_status='REJECTED')
        EXECUTE FUNCTION withdraw_affected_announcements();
      CREATE TRIGGER withdraw_avatar AFTER UPDATE ON avatar_profiles FOR EACH ROW
        WHEN (OLD.status='APPROVED' AND NEW.status<>'APPROVED')
        EXECUTE FUNCTION withdraw_affected_announcements();
      CREATE TRIGGER withdraw_template AFTER UPDATE ON announcement_templates FOR EACH ROW
        WHEN (OLD.enabled AND NOT NEW.enabled OR NEW.status='REJECTED')
        EXECUTE FUNCTION withdraw_affected_announcements();
      CREATE TRIGGER withdraw_station AFTER UPDATE ON stations FOR EACH ROW
        WHEN (OLD.revision<>NEW.revision) EXECUTE FUNCTION withdraw_affected_announcements();
    """)
    for table in ("announcement_revisions", "event_outbox", "display_acknowledgements"):
        op.execute(f"""CREATE TRIGGER immutable_live_record BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION reject_playback_mutation()""")


def downgrade():
    op.execute("""
      DO $$ BEGIN IF EXISTS(SELECT 1 FROM announcement_revisions)
        THEN RAISE EXCEPTION 'Retained live revisions require paired pre-upgrade backup rollback';
              END IF; END $$;
      DROP TRIGGER withdraw_concept ON sign_concepts;
      DROP TRIGGER withdraw_motion ON motion_versions;
      DROP TRIGGER withdraw_avatar ON avatar_profiles;
      DROP TRIGGER withdraw_template ON announcement_templates;
      DROP TRIGGER withdraw_station ON stations;
      DROP FUNCTION withdraw_affected_announcements();
      DROP FUNCTION append_station_event(text,uuid,integer,text);
      ALTER TABLE announcements DROP CONSTRAINT current_announcement_revision;
      DROP TABLE display_acknowledgements,delivery_attempts,display_deliveries,
        display_devices,event_outbox,
        announcement_revisions,announcements,station_streams;
      ALTER TABLE playback_manifests DROP CONSTRAINT playback_manifests_purpose_check;
      ALTER TABLE playback_manifests ADD CONSTRAINT playback_manifests_purpose_check
        CHECK(purpose IN ('CONTENT_REVIEW','EXACT_CONTENT','ANNOUNCEMENT_PREVIEW'));
    """)
