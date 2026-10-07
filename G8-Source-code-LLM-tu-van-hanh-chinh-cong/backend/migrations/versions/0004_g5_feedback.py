"""Store editable user feedback for completed assistant messages."""

import sqlalchemy as sa
from alembic import op

revision = "0004_g5_feedback"
down_revision = "0003_g4_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relevance", sa.String(16), nullable=False),
        sa.Column("satisfaction", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("corpus_version", sa.String(200), nullable=True),
        sa.Column("prompt_version", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_message_feedback_message"),
        sa.CheckConstraint(
            "relevance IN ('relevant', 'not_relevant')",
            name="ck_message_feedback_relevance",
        ),
        sa.CheckConstraint(
            "satisfaction IN ('satisfied', 'not_satisfied')",
            name="ck_message_feedback_satisfaction",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 1000",
            name="ck_message_feedback_reason_length",
        ),
    )
    op.create_index(
        "ix_message_feedback_user_updated",
        "message_feedback",
        ["user_id", "updated_at"],
    )


def downgrade():
    op.drop_table("message_feedback")
