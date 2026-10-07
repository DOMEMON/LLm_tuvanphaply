"""Versioned G2 corpus and persisted message grounding; retains all G1 rows."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_g2_rag"
down_revision = "0001_g1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rag_ingestion_runs",
        sa.Column("corpus_version", sa.String(160), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("fixture", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table, key in (("rag_sources", "source_id"), ("rag_procedures", "procedure_id")):
        op.create_table(
            table,
            sa.Column(
                "corpus_version",
                sa.String(160),
                sa.ForeignKey("rag_ingestion_runs.corpus_version"),
                primary_key=True,
            ),
            sa.Column(key, sa.String(160), primary_key=True),
            sa.Column("payload", JSONB, nullable=False),
        )
    op.create_table(
        "rag_fragments",
        sa.Column("corpus_version", sa.String(160), primary_key=True),
        sa.Column("fragment_id", sa.String(160), primary_key=True),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("procedure_id", sa.String(160), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["corpus_version", "source_id"], ["rag_sources.corpus_version", "rag_sources.source_id"]
        ),
        sa.ForeignKeyConstraint(
            ["corpus_version", "procedure_id"],
            ["rag_procedures.corpus_version", "rag_procedures.procedure_id"],
        ),
    )
    op.add_column("messages", sa.Column("grounding", JSONB, nullable=True))
    op.add_column("messages", sa.Column("retrieval_filters", JSONB, nullable=True))


def downgrade():
    op.drop_column("messages", "retrieval_filters")
    op.drop_column("messages", "grounding")
    op.drop_table("rag_fragments")
    op.drop_table("rag_procedures")
    op.drop_table("rag_sources")
    op.drop_table("rag_ingestion_runs")
