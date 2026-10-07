"""Owned conversation actions and a verified-answer event stream."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient, get_ai
from app.auth import current_user
from app.chat import conversation_lock, owned_conversation
from app.db import get_db
from app.errors import APIError
from app.jobs import dispatch as send_message
from app.models import Conversation, User, utcnow
from app.schemas import ConversationOutput, InputModel, MessageInput

router = APIRouter(prefix="/api/v1/conversations", tags=["workspace"])


class ConversationUpdate(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_pinned: bool | None = None

    @model_validator(mode="after")
    def not_empty(self):
        if self.title is None and self.is_pinned is None:
            raise ValueError("At least one update is required")
        return self


@router.patch("/{conversation_id}", response_model=ConversationOutput)
async def update_conversation(
    conversation_id: UUID,
    body: ConversationUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_conversation(db, conversation_id, user.id)
    await db.commit()
    async with conversation_lock(conversation_id) as locked:
        item = await owned_conversation(locked, conversation_id, user.id)
        if body.title is not None:
            item.title = body.title
            item.title_is_manual = True
        if body.is_pinned is not None:
            item.is_pinned = body.is_pinned
        item.updated_at = utcnow()
        await locked.commit()
        return ConversationOutput.model_validate(item)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_conversation(db, conversation_id, user.id)
    await db.commit()
    async with conversation_lock(conversation_id) as locked:
        await owned_conversation(locked, conversation_id, user.id)
        await locked.execute(
            delete(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
        await locked.commit()
    return Response(status_code=204)


def event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: UUID,
    body: MessageInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    ai: AIClient = Depends(get_ai),
):
    await owned_conversation(db, conversation_id, user.id)
    await db.commit()
    request_id = request.state.request_id

    async def generate():
        # Generation has its own transaction/lock. Disconnects cannot discard a saved turn.
        task = asyncio.create_task(send_message(conversation_id, user.id, body, request_id, ai))
        try:
            yield event("status", {"phase": "processing", "message": "Đang đối chiếu thông tin…"})
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    yield ": keep-alive\n\n"
            result = await task
            # Only release text after the existing verifier/fallback has completed.
            # This is delivery streaming, not unverified model-token streaming.
            text = result.assistant_message.content
            for start in range(0, len(text), 256):
                yield event("delta", {"text": text[start : start + 256]})
            yield event("complete", result.model_dump(mode="json", exclude_none=True))
        except APIError as exc:
            yield event("error", {"code": exc.code, "message": exc.message, "status": exc.status})
        except Exception:
            yield event(
                "error",
                {
                    "code": "STREAM_ERROR",
                    "message": "Chưa nhận được câu trả lời. Bạn có thể thử lại.",
                    "status": 500,
                },
            )
        finally:
            # Keep the generation alive across an HTTP disconnect, with the task retained.
            if not task.done():
                tasks = getattr(request.app.state, "pending_chat_tasks", set())
                request.app.state.pending_chat_tasks = tasks
                tasks.add(task)

                def finished(completed):
                    tasks.discard(completed)
                    if not completed.cancelled():
                        completed.exception()

                task.add_done_callback(finished)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
