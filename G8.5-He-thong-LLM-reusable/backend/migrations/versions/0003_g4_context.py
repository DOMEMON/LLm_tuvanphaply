"""Persist G4 context within each owned conversation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_g4_context"
down_revision = "0002_g2_rag"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("rag_context", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("conversations", "rag_context")
