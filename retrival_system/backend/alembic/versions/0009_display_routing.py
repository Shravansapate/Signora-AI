"""Durable individual display assignments and control-room history."""

from alembic import op

revision = "0009_display_routing"
down_revision = "0008_library_reupload"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE display_devices ADD COLUMN platform text;
      ALTER TABLE announcement_revisions ADD COLUMN targeted boolean NOT NULL DEFAULT false;
      CREATE TABLE display_routes (
        display_id uuid PRIMARY KEY REFERENCES display_devices(id),
        manifest_id uuid REFERENCES playback_manifests(id),
        revision bigint NOT NULL CHECK(revision>0),
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE display_commands (
        id uuid PRIMARY KEY, actor text NOT NULL, station_id text NOT NULL REFERENCES stations(id),
        request_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE display_route_history (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        display_id uuid NOT NULL REFERENCES display_devices(id),
        manifest_id uuid REFERENCES playback_manifests(id),
        previous_manifest_id uuid REFERENCES playback_manifests(id),
        action text NOT NULL, actor text NOT NULL, reason text NOT NULL,
        route_revision bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX display_route_history_recent ON display_route_history(display_id,id DESC);
      CREATE INDEX display_routes_manifest ON display_routes(manifest_id);
    """)


def downgrade():
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM display_routes) THEN
          RAISE EXCEPTION 'Routing history requires a paired pre-upgrade backup or forward repair';
        END IF;
      END $$;
      DROP TABLE display_route_history;
      DROP TABLE display_commands;
      DROP TABLE display_routes;
      ALTER TABLE announcement_revisions DROP COLUMN targeted;
      ALTER TABLE display_devices DROP COLUMN platform;
    """)
