"""Runtime display credentials; only SHA-256 digests are retained."""

from alembic import op

revision = "0010_display_credentials"
down_revision = "0009_display_routing"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE display_credentials (
        display_id uuid PRIMARY KEY REFERENCES display_devices(id),
        token_hash text NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
        expires_at timestamptz NOT NULL,
        issued_at timestamptz NOT NULL DEFAULT now(),
        issued_by text NOT NULL
      );
    """)


def downgrade():
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM display_credentials) THEN
          RAISE EXCEPTION 'Issued credentials require a pre-upgrade backup or forward repair';
        END IF;
      END $$;
      DROP TABLE display_credentials;
    """)
