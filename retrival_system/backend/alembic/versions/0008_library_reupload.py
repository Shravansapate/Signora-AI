"""Fresh identities for re-uploaded deleted bytes and scoped development cleanup."""

from alembic import op

revision = "0008_library_reupload"
down_revision = "0007_library_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE asset_cleanup_jobs ADD COLUMN development_reset boolean NOT NULL DEFAULT false;
      ALTER TABLE motion_versions DROP CONSTRAINT motion_versions_concept_id_sha256_key;
      CREATE UNIQUE INDEX motion_versions_retained_checksum
        ON motion_versions(concept_id,sha256) WHERE deleted_at IS NULL;
    """)


def downgrade():
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM motion_versions GROUP BY concept_id,sha256 HAVING count(*)>1)
        THEN RAISE EXCEPTION 'Re-upload history requires a forward repair or pre-upgrade backup';
        END IF;
      END $$;
      DROP INDEX motion_versions_retained_checksum;
      ALTER TABLE motion_versions ADD CONSTRAINT motion_versions_concept_id_sha256_key
        UNIQUE(concept_id,sha256);
      ALTER TABLE asset_cleanup_jobs DROP COLUMN development_reset;
    """)
