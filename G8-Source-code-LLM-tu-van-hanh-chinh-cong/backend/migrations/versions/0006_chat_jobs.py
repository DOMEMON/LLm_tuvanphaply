"""Persistent bounded chat queue and conversation topic classification."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_chat_jobs"
down_revision = "0005_accounts_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("topic", sa.String(200), nullable=True))
    op.create_table(
        "chat_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_message_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("worker_slot", sa.Integer(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "client_message_id", name="uq_chat_job_client"),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','failed')", name="ck_chat_job_state"
        ),
    )
    op.create_index("ix_chat_jobs_queue", "chat_jobs", ["state", "created_at"])
    op.create_index("ix_chat_jobs_owner", "chat_jobs", ["user_id", "state"])


def downgrade():
    op.drop_table("chat_jobs")
    op.drop_column("conversations", "topic")
