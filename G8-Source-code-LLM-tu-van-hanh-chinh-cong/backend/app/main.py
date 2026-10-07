import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from app.accounts import router as accounts_router
from app.ai import AIClient, get_ai
from app.auth import account_cookie, current_user, request_token, token_hash
from app.chat import owned_conversation
from app.checklists import router as checklists_router
from app.config import get_settings
from app.db import engine, get_db
from app.errors import APIError
from app.forms import router as forms_router
from app.jobs import dispatch as send_message
from app.jobs import router as jobs_router
from app.jobs import worker
from app.models import Conversation, Message, MessageFeedback, SessionRecord, User, utcnow
from app.rag.corpus import CorpusError
from app.rag.g3.runtime import load_active_dataset
from app.rag.g5.dictionary import configured_dictionary
from app.rag.g5.hybrid import HybridProcedureRouter
from app.rag.g5.source_mcp import MCPSourceUpdateClient
from app.rag.store import load_corpus
from app.schemas import (
    ConversationInput,
    ConversationOutput,
    ErrorOutput,
    FeedbackInput,
    FeedbackOutput,
    LoginInput,
    MessageInput,
    MessageOutput,
    MessagePair,
    UserOutput,
)
from app.workspace import router as workspace_router

logger = logging.getLogger("g1.requests")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.g6_dictionary_enabled:
        configured_dictionary(
            settings,
            load_active_dataset(
                str(settings.g3_private_dataset_path),
                settings.rag_corpus_version,
                settings.g3_expected_rows,
                settings.g3_corpus_sha256,
            ),
        )
    async with httpx.AsyncClient() as client:
        hybrid_router = None
        if settings.g5_hybrid_enabled:
            hybrid_router = HybridProcedureRouter(
                base_url=settings.g5_embedding_service_url,
                model_name=settings.g5_embedding_model,
                timeout_seconds=settings.g5_embedding_timeout_seconds,
                client=client,
            )
        source_update_client = None
        if settings.g5_source_mcp_enabled:
            source_update_client = MCPSourceUpdateClient(
                url=settings.g5_source_mcp_url,
                timeout_seconds=settings.g5_source_mcp_timeout_seconds,
            )
        app.state.ai = AIClient(
            client,
            hybrid_router=hybrid_router,
            source_update_client=source_update_client,
        )
        workers = (
            [
                asyncio.create_task(worker(slot, app.state.ai))
                for slot in range(settings.chat_workers)
            ]
            if settings.chat_jobs_enabled
            else []
        )
        try:
            yield
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    await engine.dispose()


app = FastAPI(title="G1 Core Backend", version="0.1.0", lifespan=lifespan)
app.include_router(jobs_router)


def error_response(request: Request, status: int, code: str, message: str):
    request_id = str(getattr(request.state, "request_id", uuid4()))
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


@app.middleware("http")
async def trace_request(request: Request, call_next):
    request.state.request_id = uuid4()
    started = time.perf_counter()
    try:
        # SameSite cookies plus explicit Origin validation for browser mutations.
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        forbidden_origin = (
            origin.rstrip("/") not in settings.allowed_web_origins
            if origin is not None
            else fetch_site == "cross-site"
        )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (forbidden_origin):
            response = error_response(request, 403, "FORBIDDEN_ORIGIN", "Origin không hợp lệ.")
        else:
            response = await call_next(request)
    except Exception as exc:
        # Exception messages can contain SQL, URLs or credentials. Log only safe metadata.
        route = request.scope.get("route")
        trace = exc.__traceback__
        while trace and trace.tb_next:
            trace = trace.tb_next
        location = (
            f"{trace.tb_frame.f_code.co_filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:"
            f"{trace.tb_frame.f_code.co_name}:{trace.tb_lineno}"
            if trace is not None
            else "unknown"
        )
        logger.error(
            "unhandled_request_error request_id=%s route=%s method=%s "
            "error_type=%s error_location=%s",
            request.state.request_id,
            getattr(route, "path", "unmatched"),
            request.method,
            type(exc).__name__,
            location,
        )
        response = error_response(request, 500, "INTERNAL_ERROR", "Lỗi hệ thống.")
    response.headers["X-Request-ID"] = str(request.state.request_id)
    response.headers["Cache-Control"] = "no-store"
    route = request.scope.get("route")
    logger.info(
        "request_id=%s route=%s method=%s status=%s latency_ms=%.2f",
        request.state.request_id,
        getattr(route, "path", "unmatched"),
        request.method,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    return response


# Outer CORS wrapper also decorates normalized error responses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_web_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Account-ID"],
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(APIError)
async def handle_api_error(request: Request, exc: APIError):
    return error_response(request, exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError):
    return error_response(request, 422, "VALIDATION_ERROR", "Dữ liệu gửi lên không hợp lệ.")


@app.exception_handler(HTTPException)
async def handle_http(request: Request, exc: HTTPException):
    return error_response(request, exc.status_code, "HTTP_ERROR", "Yêu cầu không hợp lệ.")


router = APIRouter(
    prefix="/api/v1",
    responses={
        status: {"model": ErrorOutput} for status in (401, 403, 404, 409, 422, 500, 502, 503, 504)
    },
)


def set_cookie(response: Response, token: str):
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/login", response_model=UserOutput)
async def login(
    body: LoginInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    if not settings.auth_demo_login_enabled:
        raise APIError(403, "DEMO_LOGIN_DISABLED", "Hãy đăng nhập bằng tài khoản và mật khẩu.")
    user = await db.scalar(
        select(User)
        .where(
            User.display_name == body.display_name,
            User.username.is_(None),
            User.google_subject.is_(None),
            User.is_guest.is_(False),
        )
        .order_by(User.created_at)
        .limit(1)
    )
    if user is None:
        user = User(display_name=body.display_name)
        db.add(user)
        await db.flush()
    old_token = request.cookies.get(settings.session_cookie_name)
    if old_token:
        await db.execute(
            delete(SessionRecord).where(SessionRecord.token_hash == token_hash(old_token))
        )
    token = secrets.token_urlsafe(32)
    db.add(
        SessionRecord(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    await db.commit()
    set_cookie(response, token)
    return user


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    token = request_token(request)
    if token:
        await db.execute(delete(SessionRecord).where(SessionRecord.token_hash == token_hash(token)))
        await db.commit()
    response = Response(status_code=204)
    selected = request.headers.get("X-Account-ID")
    try:
        cookie_name = account_cookie(UUID(selected)) if selected else settings.session_cookie_name
    except ValueError:
        cookie_name = settings.session_cookie_name
    response.delete_cookie(
        cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/auth/me", response_model=UserOutput)
async def me(user: User = Depends(current_user)):
    return user


@router.get("/conversations", response_model=list[ConversationOutput])
async def conversations(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(
                Conversation.is_pinned.desc(),
                Conversation.updated_at.desc(),
                Conversation.id.desc(),
            )
        )
    ).all()


@router.post("/conversations", response_model=ConversationOutput, status_code=201)
async def create_conversation(
    body: ConversationInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    conversation = Conversation(
        user_id=user.id, title=body.title, title_is_manual=body.title != "Cuộc trò chuyện mới"
    )
    db.add(conversation)
    await db.commit()
    return conversation


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOutput],
    response_model_exclude_none=True,
)
async def messages(
    conversation_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_conversation(db, conversation_id, user.id)
    return (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
    ).all()


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePair,
    response_model_exclude_none=True,
)
async def post_message(
    conversation_id: UUID,
    body: MessageInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    ai: AIClient = Depends(get_ai),
):
    # Ownership is checked before locking, so other users cannot observe in-flight state.
    await owned_conversation(db, conversation_id, user.id)
    await db.commit()
    return await send_message(conversation_id, user.id, body, request.state.request_id, ai)


async def owned_assistant_message(db: AsyncSession, message_id: UUID, user_id: UUID) -> Message:
    message = await db.scalar(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.id == message_id, Conversation.user_id == user_id)
    )
    if message is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy câu trả lời.")
    if message.role != "assistant" or message.status != "completed":
        raise APIError(422, "ASSISTANT_MESSAGE_REQUIRED", "Chỉ đánh giá câu trả lời đã hoàn tất.")
    return message


@router.get(
    "/conversations/{conversation_id}/feedback",
    response_model=list[FeedbackOutput],
    response_model_exclude_none=True,
)
async def conversation_feedback(
    conversation_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_conversation(db, conversation_id, user.id)
    return (
        await db.scalars(
            select(MessageFeedback)
            .join(Message, Message.id == MessageFeedback.message_id)
            .where(
                Message.conversation_id == conversation_id,
                MessageFeedback.user_id == user.id,
            )
            .order_by(MessageFeedback.updated_at, MessageFeedback.id)
        )
    ).all()


@router.put(
    "/messages/{message_id}/feedback",
    response_model=FeedbackOutput,
    response_model_exclude_none=True,
)
async def put_feedback(
    message_id: UUID,
    body: FeedbackInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await owned_assistant_message(db, message_id, user.id)
    grounding = message.grounding if isinstance(message.grounding, dict) else {}
    now = utcnow()
    values = {
        "user_id": user.id,
        "relevance": body.relevance,
        "satisfaction": body.satisfaction,
        "reason": body.reason.strip() if body.reason and body.reason.strip() else None,
        "provider": grounding.get("provider"),
        "model": grounding.get("model"),
        "corpus_version": grounding.get("corpus_version"),
        "prompt_version": grounding.get("prompt_version"),
        "updated_at": now,
    }
    await db.execute(
        insert(MessageFeedback)
        .values(message_id=message.id, created_at=now, **values)
        .on_conflict_do_update(
            index_elements=[MessageFeedback.message_id],
            set_=values,
        )
    )
    await db.commit()
    return await db.scalar(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message.id,
            MessageFeedback.user_id == user.id,
        )
    )


@router.delete("/messages/{message_id}/feedback", status_code=204)
async def delete_feedback(
    message_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await owned_assistant_message(db, message_id, user.id)
    await db.execute(
        delete(MessageFeedback).where(
            MessageFeedback.message_id == message.id,
            MessageFeedback.user_id == user.id,
        )
    )
    await db.commit()
    return Response(status_code=204)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise APIError(503, "DATABASE_UNAVAILABLE", "Database chưa sẵn sàng.") from exc
    return {"status": "ok"}


@app.get("/health/rag")
async def rag_health(
    db: AsyncSession = Depends(get_db),
    ai: AIClient = Depends(get_ai),
):
    current = get_settings()
    if not current.rag_enabled:
        return {"status": "disabled"}
    routing_dictionary = None
    try:
        if current.g3_d2_enabled:
            data = load_active_dataset(
                str(current.g3_private_dataset_path),
                current.rag_corpus_version,
                current.g3_expected_rows,
                current.g3_corpus_sha256,
            )
            corpus_version = data.version
            data_classification = "D2_COMPANY_REAL"
            source_count = len(data.sources)
            procedure_count = len(data.accepted)
            fragment_count = len(data.fragments)
            routing_dictionary = configured_dictionary(current, data)
        else:
            corpus = await load_corpus(
                db,
                current.rag_corpus_version,
                current.rag_allow_fixtures,
            )
            corpus_version = corpus.version
            data_classification = "D0_SYSTEM_SMOKE" if corpus.fixture else "D1_PUBLIC_DEMO"
            source_count = len(corpus.sources)
            procedure_count = len(corpus.procedures)
            fragment_count = len(corpus.fragments)
        response = await ai.client.get(
            current.ai_service_url.rstrip("/") + "/internal/v2/health",
            timeout=current.ai_request_timeout_seconds,
        )
        response.raise_for_status()
        grounded = response.json()
        if (
            not isinstance(grounded, dict)
            or grounded.get("status") != "ok"
            or grounded.get("endpoint") != "grounded-v2"
            or (
                current.g3_d2_enabled
                and (
                    grounded.get("external_calls_enabled") is not False
                    or "D2_COMPANY_REAL" not in grounded.get("allowed_data_classifications", [])
                )
            )
        ):
            raise ValueError("invalid grounded readiness")
        intent = None
        if current.g5_intent_context_enabled:
            response = await ai.client.get(
                current.ai_service_url.rstrip("/") + "/internal/v3/intent-context/health",
                timeout=current.ai_request_timeout_seconds,
            )
            response.raise_for_status()
            intent = response.json()
            if (
                not isinstance(intent, dict)
                or intent.get("status") != "ok"
                or intent.get("endpoint") != "intent-context-v2"
                or intent.get("external_calls_enabled") is not False
                or "D2_COMPANY_REAL" not in intent.get("allowed_data_classifications", [])
            ):
                raise ValueError("invalid intent/context readiness")
    except (CorpusError, httpx.HTTPError, ValueError) as exc:
        raise APIError(503, "RAG_UNAVAILABLE", "RAG hoặc grounded AI chưa sẵn sàng.") from exc
    readiness = {
        "status": "ok",
        "corpus_version": corpus_version,
        "data_classification": data_classification,
        "sources": source_count,
        "procedures": procedure_count,
        "fragments": fragment_count,
        "grounded_provider": grounded.get("provider"),
        "prompt_version": grounded.get("prompt_version"),
        "external_calls_enabled": grounded.get("external_calls_enabled"),
    }
    if routing_dictionary is not None:
        readiness["routing_dictionary"] = {
            "version": routing_dictionary.vocabulary.version,
            "sha256": routing_dictionary.sha256,
        }
    if intent is not None:
        readiness.update(
            intent_context_enabled=True,
            intent_context_model=intent.get("model"),
        )
    return readiness


app.include_router(router)
app.include_router(accounts_router)
from app.browser_sessions import router as browser_sessions_router
app.include_router(browser_sessions_router)
app.include_router(workspace_router)
app.include_router(checklists_router)
app.include_router(forms_router)
