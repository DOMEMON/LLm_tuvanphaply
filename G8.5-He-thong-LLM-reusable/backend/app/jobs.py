"""Durable FIFO jobs with database-locked worker slots and per-conversation context."""

import asyncio
import logging
import time
from collections import Counter
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import account_cookie, current_user, token_hash
from app.chat import owned_conversation, send_message
from app.config import get_settings
from app.db import Session, engine, get_db
from app.errors import APIError
from app.job_models import ChatJob
from app.models import Message, SessionRecord, User, utcnow
from app.schemas import MessageInput, MessagePair

router = APIRouter(prefix="/api/v1", tags=["chat jobs"])
logger = logging.getLogger("backend.jobs")
ADMISSION_LOCK = 7314060
WORKER_LOCK = 7314070
ACTIVE = ("queued", "running")
socket_counts: Counter = Counter()


async def enqueue(db, user_id, conversation_id, body, request_id):
    settings = get_settings()
    if not settings.chat_jobs_enabled:
        raise APIError(503, "QUEUE_DISABLED", "Hàng đợi chưa được bật.")
    if len(body.content) > 4000:
        raise APIError(422, "VALIDATION_ERROR", "Câu hỏi không được vượt quá 4.000 ký tự.")
    await owned_conversation(db, conversation_id, user_id)
    # Serialize admission only (milliseconds), never hold this lock during inference.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK})
    existing = await db.scalar(
        select(ChatJob).where(
            ChatJob.conversation_id == conversation_id,
            ChatJob.client_message_id == body.client_message_id,
        )
    )
    payload = body.model_dump(mode="json")
    if existing and existing.payload != payload:
        raise APIError(409, "IDEMPOTENCY_CONFLICT", "Mã yêu cầu đã được dùng với nội dung khác.")
    if existing and existing.state != "failed":
        await db.commit()
        return existing
    if await db.scalar(
        select(ChatJob.id).where(ChatJob.user_id == user_id, ChatJob.state.in_(ACTIVE)).limit(1)
    ):
        raise APIError(
            429, "USER_BUSY", "Tài khoản đang có một câu hỏi chờ xử lý. Vui lòng đợi câu trả lời."
        )
    count = await db.scalar(
        select(func.count()).select_from(ChatJob).where(ChatJob.state.in_(ACTIVE))
    )
    waiting = await db.scalar(
        select(func.count()).select_from(ChatJob).where(ChatJob.state == "queued")
    )
    if (
        count >= settings.chat_workers + settings.chat_queue_limit
        or waiting >= settings.chat_queue_limit
    ):
        raise APIError(
            429, "QUEUE_FULL", "Hệ thống đang phục vụ nhiều người. Vui lòng thử lại sau ít phút."
        )
    if existing:
        job = existing
        job.state, job.error, job.worker_slot = "queued", None, None
        job.request_id, job.created_at, job.updated_at = request_id, utcnow(), utcnow()
    else:
        job = ChatJob(
            user_id=user_id,
            conversation_id=conversation_id,
            client_message_id=body.client_message_id,
            request_id=request_id,
            payload=payload,
        )
        db.add(job)
    await db.commit()
    return job


async def snapshot(db, job_id, user_id):
    job = await db.scalar(select(ChatJob).where(ChatJob.id == job_id, ChatJob.user_id == user_id))
    if job is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy yêu cầu.")
    result = {
        "job_id": str(job.id),
        "conversation_id": str(job.conversation_id),
        "state": job.state,
        "client_message_id": str(job.client_message_id),
        "content": job.payload["content"],
    }
    if job.state == "queued":
        ahead = await db.scalar(
            select(func.count())
            .select_from(ChatJob)
            .where(
                ChatJob.state == "queued",
                ChatJob.created_at < job.created_at,
            )
        )
        result.update(position=ahead + 1, message=f"Đang chờ đến lượt · vị trí {ahead + 1}",
                      poll_after_ms=2000)
    elif job.state == "running":
        result["message"] = "Đang đối chiếu thông tin và chuẩn bị câu trả lời…"
        result["poll_after_ms"] = 750
    elif job.state == "failed":
        result["error"] = job.error
    elif job.state == "completed":
        user_message = await db.scalar(
            select(Message).where(
                Message.conversation_id == job.conversation_id,
                Message.client_message_id == job.client_message_id,
            )
        )
        assistant = await db.scalar(
            select(Message).where(
                Message.conversation_id == job.conversation_id,
                Message.request_id == user_message.request_id,
                Message.role == "assistant",
            )
        )
        result["result"] = MessagePair(
            user_message=user_message, assistant_message=assistant
        ).model_dump(mode="json")
    return result


async def finish(job_id, state, error=None):
    async with Session() as db:
        if state == "failed":
            job = await db.get(ChatJob, job_id)
            if job:
                await db.execute(
                    update(Message)
                    .where(
                        Message.conversation_id == job.conversation_id,
                        Message.client_message_id == job.client_message_id,
                        Message.status == "pending",
                    )
                    .values(status="failed")
                )
        await db.execute(
            update(ChatJob)
            .where(ChatJob.id == job_id)
            .values(state=state, error=error, updated_at=utcnow())
        )
        await db.commit()


async def process(job, ai):
    started = time.perf_counter()
    logger.info("job_started job_id=%s queue_wait_ms=%d", job.id,
                max(0, int((utcnow() - job.created_at).total_seconds() * 1000)))
    try:
        async with asyncio.timeout(get_settings().chat_job_timeout_seconds):
            await send_message(
                job.conversation_id,
                job.user_id,
                MessageInput.model_validate(job.payload),
                job.request_id,
                ai,
            )
        await finish(job.id, "completed")
    except APIError as exc:
        await finish(
            job.id, "failed", {"code": exc.code, "message": exc.message, "status": exc.status}
        )
    except TimeoutError:
        await finish(
            job.id,
            "failed",
            {
                "code": "JOB_TIMEOUT",
                "message": "Xử lý quá thời gian cho phép. Bạn có thể thử lại.",
                "status": 504,
            },
        )
    except Exception as exc:
        logger.error("job_failed job_id=%s error_type=%s", job.id, type(exc).__name__)
        await finish(
            job.id,
            "failed",
            {
                "code": "JOB_FAILED",
                "message": "Chưa hoàn tất câu trả lời. Bạn có thể thử lại.",
                "status": 500,
            },
        )
    finally:
        logger.info("job_finished job_id=%s processing_ms=%d", job.id,
                    int((time.perf_counter() - started) * 1000))


async def claim(db, slot):
    cutoff = utcnow() - timedelta(seconds=get_settings().chat_queue_timeout_seconds)
    await db.execute(
        update(ChatJob)
        .where(ChatJob.state == "queued", ChatJob.created_at < cutoff)
        .values(
            state="failed",
            updated_at=utcnow(),
            error={
                "code": "QUEUE_TIMEOUT",
                "message": "Yêu cầu chờ quá lâu. Vui lòng thử lại.",
                "status": 504,
            },
        )
    )
    job = await db.scalar(
        select(ChatJob)
        .where(ChatJob.state == "queued")
        .order_by(
            ChatJob.created_at,
            ChatJob.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job:
        job.state, job.worker_slot, job.updated_at = "running", slot, utcnow()
    await db.commit()
    return job


async def worker(slot, ai):
    while True:
        try:
            # One session lock per numbered slot: adding API processes cannot multiply GPU jobs.
            async with engine.connect() as connection:
                locked = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK + slot}
                )
                await connection.commit()
                if not locked:
                    await asyncio.sleep(1)
                    continue
                try:
                    async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                        # Acquiring this slot proves its old owner is gone. Replay is idempotent.
                        await db.execute(
                            update(ChatJob)
                            .where(ChatJob.state == "running", ChatJob.worker_slot == slot)
                            .values(state="queued", worker_slot=None)
                        )
                        await db.commit()
                        while True:
                            job = await claim(db, slot)
                            if job:
                                await process(job, ai)
                                db.expunge_all()
                            else:
                                await asyncio.sleep(0.4)
                finally:
                    try:
                        await connection.rollback()
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK + slot}
                        )
                        await connection.commit()
                    except BaseException:
                        await connection.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("worker_reconnect slot=%s error_type=%s", slot, type(exc).__name__)
            await asyncio.sleep(1)


async def dispatch(conversation_id, user_id, body, request_id, ai):
    """Legacy REST/SSE also honor admission limits on G6 (no bypass path)."""
    if not get_settings().chat_jobs_enabled:
        return await send_message(conversation_id, user_id, body, request_id, ai)
    async with Session() as db:
        job = await enqueue(db, user_id, conversation_id, body, request_id)
        job_id = job.id
    while True:
        async with Session() as db:
            state = await snapshot(db, job_id, user_id)
        if state["state"] == "completed":
            # Keep required nullable fields (e.g. an internal citation's url)
            # in snapshot(), so REST and WebSocket use the same valid contract.
            return MessagePair.model_validate(state["result"])
        if state["state"] == "failed":
            error = state["error"]
            raise APIError(error["status"], error["code"], error["message"])
        await asyncio.sleep(state.get("poll_after_ms", 750) / 1000)


@router.post("/conversations/{conversation_id}/jobs", status_code=202)
async def submit_job(
    conversation_id: UUID,
    body: MessageInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await enqueue(db, user.id, conversation_id, body, request.state.request_id)
    return await snapshot(db, job.id, user.id)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    return await snapshot(db, job_id, user.id)


@router.get("/conversations/{conversation_id}/active-job")
async def active_job(
    conversation_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_conversation(db, conversation_id, user.id)
    job = await db.scalar(
        select(ChatJob)
        .where(ChatJob.conversation_id == conversation_id, ChatJob.state.in_(ACTIVE))
        .order_by(ChatJob.created_at.desc())
        .limit(1)
    )
    return await snapshot(db, job.id, user.id) if job else None


@router.websocket("/jobs/{job_id}/socket")
async def job_socket(websocket: WebSocket, job_id: UUID, account_id: UUID):
    # Cookies carry the secret. URL contains public IDs only. Explicit Origin check stops CSWSH.
    if websocket.headers.get("origin", "").rstrip("/") not in get_settings().allowed_web_origins:
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(account_cookie(account_id))
    if not token or socket_counts[account_id] >= 4 or sum(socket_counts.values()) >= 32:
        await websocket.close(code=1008)
        return

    async def authenticated_snapshot():
        async with Session() as db:
            valid = await db.scalar(
                select(SessionRecord.id).where(
                    SessionRecord.user_id == account_id,
                    SessionRecord.token_hash == token_hash(token),
                    SessionRecord.expires_at > utcnow(),
                )
            )
            if not valid:
                raise APIError(401, "UNAUTHORIZED", "Vui lòng đăng nhập.")
            return await snapshot(db, job_id, account_id)

    # Reserve before the first await so concurrent handshakes cannot all pass
    # the connection cap. The finally block also releases failed authentication.
    socket_counts[account_id] += 1
    try:
        await authenticated_snapshot()
        await websocket.accept()
        while True:
            state = await authenticated_snapshot()
            await websocket.send_json(state)
            if state["state"] in ("completed", "failed"):
                await websocket.close(code=1000)
                break
            await asyncio.sleep(state.get("poll_after_ms", 750) / 1000)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except APIError:
        await websocket.close(code=1008)
    finally:
        socket_counts[account_id] -= 1
        if socket_counts[account_id] == 0:
            del socket_counts[account_id]
