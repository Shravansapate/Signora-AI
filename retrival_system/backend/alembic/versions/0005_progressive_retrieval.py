"""Scoped reviewed senses/aliases and revision-pinned exact pgvector search."""

from alembic import op

revision = "0005_progressive_retrieval"
down_revision = "0004_announcement_input"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE EXTENSION IF NOT EXISTS vector;
      ALTER TABLE sign_aliases ADD COLUMN domain text, ADD COLUMN context text,
        ADD COLUMN reviewed_semantic_revision integer,
        ADD COLUMN review_id uuid REFERENCES content_reviews(id);
      CREATE TABLE retrieval_profiles (
        concept_id uuid PRIMARY KEY REFERENCES sign_concepts(id),
        semantic_revision integer NOT NULL CHECK(semantic_revision>0),
        domain text NOT NULL, context text NOT NULL, sense jsonb NOT NULL,
        description text NOT NULL, review_id uuid NOT NULL REFERENCES content_reviews(id),
        status text NOT NULL CHECK(status IN ('APPROVED','REJECTED')),
        input_hash text NOT NULL CHECK(length(input_hash)=64)
      );
      CREATE TABLE sign_embeddings (
        concept_id uuid NOT NULL REFERENCES sign_concepts(id),
        semantic_revision integer NOT NULL, model_id text NOT NULL,
        model_revision text NOT NULL, encoding_policy text NOT NULL,
        input_hash text NOT NULL CHECK(length(input_hash)=64),
        embedding vector(384) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(concept_id, model_revision, encoding_policy, input_hash),
        CHECK(vector_norm(embedding) BETWEEN 0.999 AND 1.001)
      );
      CREATE TRIGGER immutable_embedding BEFORE UPDATE OR DELETE ON sign_embeddings
        FOR EACH ROW EXECUTE FUNCTION reject_playback_mutation();
      CREATE INDEX retrieval_profile_scope ON retrieval_profiles(domain,context);
    """)


def downgrade():
    op.execute("""
      DROP TABLE sign_embeddings, retrieval_profiles;
      ALTER TABLE sign_aliases DROP COLUMN domain, DROP COLUMN context,
        DROP COLUMN reviewed_semantic_revision, DROP COLUMN review_id;
    """)
    # The shared extension may be used by other schemas. Never drop it implicitly.
