from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utcnow


class ChatJob(Base):
    __tablename__ = "chat_jobs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_message_id", name="uq_chat_job_client"),
        CheckConstraint(
            "state IN ('queued','running','completed','failed')", name="ck_chat_job_state"
        ),
        Index("ix_chat_jobs_queue", "state", "created_at"),
        Index("ix_chat_jobs_owner", "user_id", "state"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    client_message_id: Mapped[UUID]
    request_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default="queued")
    worker_slot: Mapped[int | None]
    error: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
