"""Persist user preparation work separately from chat history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_preparation_checklists"
down_revision = "0006_chat_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preparation_checklists",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("origin_key", sa.Uuid(), nullable=False),
        sa.Column(
            "conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("procedure_id", sa.String(200), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("corpus_version", sa.String(200), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "origin_key", name="uq_checklist_origin"),
    )
    op.create_index("ix_preparation_checklists_user_id", "preparation_checklists", ["user_id"])
    op.create_table(
        "preparation_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "checklist_id",
            sa.Uuid(),
            sa.ForeignKey("preparation_checklists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("evidence_ids", JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_source", sa.String(24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('unknown','have','missing','not_applicable')",
            name="ck_preparation_item_status",
        ),
        sa.CheckConstraint(
            "status_source IN ('NOT_PROVIDED','USER_REPORTED')", name="ck_preparation_item_source"
        ),
    )
    op.create_index("ix_preparation_items_checklist_id", "preparation_items", ["checklist_id"])
    op.create_table(
        "preparation_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "checklist_id",
            sa.Uuid(),
            sa.ForeignKey("preparation_checklists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Uuid(), sa.ForeignKey("preparation_items.id", ondelete="CASCADE")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("before", sa.String(20)),
        sa.Column("after", sa.String(20)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_preparation_events_checklist_id", "preparation_events", ["checklist_id"])


def downgrade():
    op.drop_table("preparation_events")
    op.drop_table("preparation_items")
    op.drop_table("preparation_checklists")
