import hashlib
import logging
from contextlib import asynccontextmanager
from uuid import UUID
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.ai import AIClient
from app.rag.g85.backend_bridge import answer_turn as g7_answer_turn
from app.config import get_settings
from app.db import engine
from app.errors import APIError
from app.models import Conversation, Message, utcnow
from app.rag.schemas import Filters
from app.schemas import MessageInput, MessagePair

AI_CONTEXT_MAX_MESSAGES = 100
logger = logging.getLogger("backend.chat")


async def owned_conversation(db: AsyncSession, conversation_id: UUID, user_id: UUID):
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy cuộc trò chuyện.")
    return conversation


@asynccontextmanager
async def conversation_lock(conversation_id: UUID):
    # Session-level PostgreSQL lock spans commits and works across backend workers.
    # Try-lock returns immediately; one conversation can have only one generation in flight.
    key = int.from_bytes(
        hashlib.blake2b(conversation_id.bytes, digest_size=8).digest(), "big", signed=True
    )
    async with engine.connect() as connection:
        locked = False
        try:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            await connection.commit()
            if not locked:
                raise APIError(409, "MESSAGE_IN_PROGRESS", "Đang xử lý tin nhắn. Thử lại sau.")
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                yield db
        finally:
            if locked:
                try:
                    await connection.rollback()
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    await connection.commit()
                except BaseException:
                    # Never return a session-locked connection to the pool.
                    await connection.invalidate()
                    raise


async def send_message(
    conversation_id: UUID, user_id: UUID, body: MessageInput, request_id: UUID, ai: AIClient
) -> MessagePair:
    if get_settings().rag_enabled and len(body.content) > 4_000:
        raise APIError(422, "VALIDATION_ERROR", "Tin nhắn RAG không được vượt quá 4.000 ký tự.")
    filters = body.retrieval_filters.model_dump(mode="json")
    async with conversation_lock(conversation_id) as db:
        conversation = await owned_conversation(db, conversation_id, user_id)
        user_message = await db.scalar(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.client_message_id == body.client_message_id,
                Message.role == "user",
            )
        )
        if user_message:
            if (
                user_message.content != body.content
                or (user_message.retrieval_filters or Filters().model_dump(mode="json")) != filters
            ):
                raise APIError(
                    409, "IDEMPOTENCY_CONFLICT", "client_message_id đã được dùng với nội dung khác."
                )
            assistant = await db.scalar(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.request_id == user_message.request_id,
                    Message.role == "assistant",
                )
            )
            if assistant:
                return MessagePair(user_message=user_message, assistant_message=assistant)
            # Reject retry of an old failed turn after a newer turn, preserving ordered history.
            latest = await db.scalar(
                select(Message.id)
                .where(Message.conversation_id == conversation.id, Message.role == "user")
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
            if latest != user_message.id:
                raise APIError(
                    409, "RETRY_OUT_OF_ORDER", "Hãy gửi lại nội dung bằng client_message_id mới."
                )
            user_message.status = "pending"
            user_message.request_id = request_id
        else:
            user_message = Message(
                conversation_id=conversation.id,
                client_message_id=body.client_message_id,
                role="user",
                content=body.content,
                retrieval_filters=filters,
                status="pending",
                request_id=request_id,
            )
            db.add(user_message)
        conversation.updated_at = utcnow()
        if not conversation.title_is_manual and conversation.title == "Cuộc trò chuyện mới":
            conversation.title = " ".join(body.content.split())[:100]
        await db.commit()  # Durable even if AI fails or this worker exits.
        # The internal AI contract accepts at most 100 messages. Select newest first
        # so long conversations keep the most relevant context, then restore chronology.
        newest_history = (
            await db.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    ((Message.status == "completed") | (Message.id == user_message.id)),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(AI_CONTEXT_MAX_MESSAGES)
            )
        ).all()
        history = list(reversed(newest_history))
        await db.commit()  # No idle database transaction during network I/O.
        try:
            answer, grounding, next_context = await g7_answer_turn(
                query=body.content, raw_state=conversation.rag_context,
                history=[m for m in history if m.id != user_message.id],
                filters=body.retrieval_filters, ai=ai, request_id=request_id,
                db=db, conversation=conversation,
            )
        except APIError:
            user_message.status = "failed"
            conversation.updated_at = utcnow()
            await db.commit()
            raise
        if next_context is not None:
            conversation.rag_context = next_context.model_dump(mode="json")
        if grounding:
            titles = list(
                dict.fromkeys(
                    source.get("title", "")
                    for source in grounding.get("sources", [])
                    if source.get("title")
                )
            )
            if len(titles) == 1:
                title = titles[0].strip()
                if title:
                    title = title[0].upper() + title[1:]
                if title.isupper():
                    title = title.capitalize()
                conversation.topic = title[:200]
                if not conversation.title_is_manual and len(history) == 1:
                    conversation.title = title[:100]
        user_message.status = "completed"
        assistant = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            grounding=grounding,
            status="completed",
            request_id=request_id,
        )
        db.add(assistant)
        conversation.updated_at = utcnow()
        await db.commit()
        return MessagePair(user_message=user_message, assistant_message=assistant)
