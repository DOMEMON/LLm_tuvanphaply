import hashlib
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIClient
from app.rag.g7.workflow import answer_turn as g7_answer_turn
from app.config import get_settings
from app.db import engine
from app.errors import APIError
from app.models import Conversation, Message, utcnow
from app.rag.corpus import CorpusError
from app.rag.g3.runtime import load_active_dataset
from app.rag.g4.context import prepare_turn
from app.rag.g5 import realtime, source_discrepancy
from app.rag.g5.dictionary import configured_dictionary
from app.rag.g5.intent_context import (
    IntentContextState,
    can_skip_intent_model,
    load_context,
    merge_legacy_context,
    model_state,
    prepare_model_turn,
    procedure_candidates,
    record_grounded_turn,
    safe_intent_output,
    select_pending_procedure,
    to_legacy_context,
)
from app.rag.g5.react_agent import maybe_refine_topic
from app.rag.g5.source_mcp import MCPSourceUpdateClient
from app.rag.g5.topic_gate import TopicDecision, decide_topic
from app.rag.g5.semantic_intent import confirm_semantic_topic
from app.rag.g5.turn_orchestrator import resolve_turn
from app.rag.grounding import answer_with_rag
from app.rag.schemas import Filters, Grounding, RetrievalInput
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
            grounding = None
            next_context = None
            policy_answer = None
            realtime_handled = False
            if get_settings().rag_enabled and get_settings().g3_d2_enabled:
                answer, grounding, next_context = await g7_answer_turn(
                    query=body.content, raw_state=conversation.rag_context,
                    history=[m for m in history if m.id != user_message.id],
                    filters=body.retrieval_filters, ai=ai, request_id=request_id,
                    db=db, conversation=conversation,
                )
            elif get_settings().rag_enabled:
                retrieval_query = RetrievalInput(
                    query=body.content,
                    filters=body.retrieval_filters,
                    top_k=get_settings().rag_top_k,
                )
                if get_settings().g3_d2_enabled:
                    settings = get_settings()
                    try:
                        data = load_active_dataset(
                            str(settings.g3_private_dataset_path),
                            settings.rag_corpus_version,
                            settings.g3_expected_rows,
                            settings.g3_corpus_sha256,
                        )
                        previous_model = load_context(
                            conversation.rag_context,
                            corpus_version=data.version,
                        )
                        had_offer = previous_model.pending_realtime is not None
                        direct_lookup = None
                        if not had_offer:
                            latest_answer = next(
                                (item for item in reversed(history) if item.role == "assistant"),
                                None,
                            )
                            if latest_answer is not None:
                                direct_lookup = realtime.explicit_request(
                                    previous_model,
                                    data,
                                    body.content,
                                    body.retrieval_filters,
                                    answer=latest_answer.content,
                                    grounding=latest_answer.grounding,
                                    enabled=settings.g5_realtime_mcp_enabled,
                                )
                                if direct_lookup is not None:
                                    previous_model.pending_realtime = direct_lookup
                                    had_offer = True
                        consent, pending = realtime.consume(
                            previous_model,
                            "đồng ý" if direct_lookup is not None else body.content,
                            body.retrieval_filters,
                            enabled=settings.g5_realtime_mcp_enabled,
                        )
                        if had_offer:
                            # Persist one-shot consumption before network I/O. A retry
                            # cannot issue the same external lookup a second time.
                            conversation.rag_context = previous_model.model_dump(mode="json")
                            await db.commit()
                        if consent:
                            realtime_handled = True
                            next_context = previous_model
                            current_client = None
                            if consent == "yes":
                                try:
                                    current_client = MCPSourceUpdateClient(
                                        url=settings.g5_source_mcp_url,
                                        timeout_seconds=settings.g5_realtime_mcp_timeout_seconds,
                                    )
                                except ValueError:
                                    pass
                            policy_answer, grounding = await realtime.resolve(
                                pending,
                                consent,
                                current_client,
                            )
                            if pending.awaiting_variant:
                                previous_model.pending_realtime = pending
                        if not realtime_handled:
                            original = source_discrepancy.original_record_reply(
                                previous_model, data, body.content
                            )
                            if original is not None:
                                # Raw F42 is inspectable, not approved as a
                                # document checklist or a model evidence claim.
                                realtime_handled = True
                                next_context = previous_model
                                policy_answer = original
                                grounding = Grounding(
                                    request_id=request_id,
                                    evidence_bundle_id=uuid5(request_id, "g6-raw-source-warning"),
                                    status="INSUFFICIENT_DATA",
                                    corpus_version=data.version,
                                    data_classification="D2_COMPANY_REAL",
                                    provider="backend-policy",
                                    model="quarantined-source-view",
                                    sources=[],
                                    missing_information=["required_documents"],
                                ).model_dump(mode="json")
                        use_legacy = not realtime_handled and not settings.g5_intent_context_enabled
                        if (settings.g5_intent_context_enabled and not realtime_handled
                                and settings.g6_turn_orchestrator_enabled):
                            prior = [item for item in history if item.id != user_message.id]
                            resolved_turn = await resolve_turn(
                                data=data, query=body.content, previous=previous_model, ai=ai,
                                dictionary=configured_dictionary(settings, data), settings=settings,
                                request_id=request_id, message_id=user_message.id,
                                previous_user=next((m.content for m in reversed(prior) if m.role == "user"), ""),
                                previous_assistant=next((m.content for m in reversed(prior) if m.role == "assistant"), ""),
                            )
                            retrieval_query, next_context, policy_answer = prepare_model_turn(
                                data, retrieval_query, previous_model, resolved_turn.output,
                                message_id=str(user_message.id), topic_decision=resolved_turn.topic,
                                topic_finalized=True, resolved_fields=resolved_turn.fields,
                                evidence_mode=settings.g8_evidence_mode,
                            )
                        if (settings.g5_intent_context_enabled and not realtime_handled
                                and not settings.g6_turn_orchestrator_enabled):
                            dictionary = configured_dictionary(settings, data)
                            candidates = procedure_candidates(data)
                            topic_decision = await decide_topic(
                                data,
                                body.content,
                                previous_model.procedure_id,
                                ai.hybrid_router if settings.g5_hybrid_enabled else None,
                                dictionary=dictionary,
                            )
                            selected = select_pending_procedure(
                                previous_model,
                                body.content,
                                dictionary=dictionary,
                                topic=topic_decision,
                            )
                            if selected:
                                topic_decision = TopicDecision(
                                    "SWITCH",
                                    selected,
                                    "clarification_selection",
                                )
                            topic_decision = await maybe_refine_topic(
                                ai,
                                data,
                                body.content,
                                previous_model.procedure_id,
                                topic_decision,
                                request_id,
                                settings,
                                dictionary=dictionary,
                            )
                            try:
                                if can_skip_intent_model(body.content, topic_decision):
                                    intent_output = safe_intent_output(
                                        data,
                                        body.content,
                                        str(user_message.id),
                                    )
                                    intent_path = "catalog_fast_path"
                                else:
                                    intent_output = await ai.analyze_intent_context(
                                        request_id=request_id,
                                        current_message_id=user_message.id,
                                        current_user_message=body.content,
                                        state_before=model_state(previous_model),
                                        procedure_candidates=candidates,
                                        data_classification="D2_COMPANY_REAL",
                                    )
                                    intent_path = "ai_service"
                            except (APIError, ValueError, ValidationError) as exc:
                                if not settings.g5_intent_context_fallback_enabled:
                                    raise
                                logger.warning(
                                    "intent_context_fallback request_id=%s error=%s",
                                    request_id,
                                    type(exc).__name__,
                                )
                                # Failure must obey the same topic boundary; the
                                # legacy reducer could silently resurrect old evidence.
                                intent_output = safe_intent_output(
                                    data,
                                    body.content,
                                    str(user_message.id),
                                )
                                intent_path = "safe_fallback"
                            topic_decision = await confirm_semantic_topic(
                                data, body.content, intent_output, topic_decision,
                                ai.hybrid_router if settings.g5_hybrid_enabled else None,
                            )
                            retrieval_query, next_context, policy_answer = prepare_model_turn(
                                data,
                                retrieval_query,
                                previous_model,
                                intent_output,
                                message_id=str(user_message.id),
                                topic_decision=topic_decision,
                                evidence_mode=settings.g8_evidence_mode,
                            )
                            logger.info(
                                "topic_gate request_id=%s action=%s reason=%s intent_path=%s "
                                "previous=%s resolved=%s fields=%s pending=%s",
                                request_id,
                                topic_decision.action,
                                topic_decision.reason,
                                intent_path,
                                previous_model.procedure_id,
                                next_context.procedure_id,
                                next_context.field_intents,
                                next_context.pending_clarification,
                            )
                        if use_legacy:
                            previous = to_legacy_context(previous_model)
                            retrieval_query, legacy_context, query_context = prepare_turn(
                                data,
                                retrieval_query,
                                previous,
                                user_message.id,
                            )
                            # Dense routing is a bounded fallback for the legacy path.
                            if (
                                query_context.procedure_id is None
                                and settings.g5_hybrid_enabled
                                and ai.hybrid_router is not None
                            ):
                                candidate = await ai.hybrid_router.route(data, body.content)
                                if candidate is not None:
                                    retrieval_query, legacy_context, _ = prepare_turn(
                                        data,
                                        RetrievalInput(
                                            query=body.content,
                                            filters=body.retrieval_filters,
                                            top_k=settings.rag_top_k,
                                        ),
                                        previous,
                                        user_message.id,
                                        procedure_hint=candidate.procedure_id,
                                    )
                            if settings.g5_intent_context_enabled:
                                next_context = merge_legacy_context(
                                    previous_model,
                                    legacy_context,
                                    message_id=str(user_message.id),
                                    explicit_as_of=body.retrieval_filters.as_of,
                                )
                            else:
                                next_context = legacy_context
                    except (CorpusError, ValueError, ValidationError) as exc:
                        raise APIError(
                            503, "RAG_CORPUS_UNAVAILABLE", "Corpus RAG chưa sẵn sàng."
                        ) from exc
                if policy_answer is not None:
                    answer = policy_answer
                    # A backend clarification is not a mock AI answer, and must
                    # never appear as source-verified administrative information.
                    if grounding is None:
                        grounding = Grounding(
                            request_id=request_id,
                            evidence_bundle_id=uuid5(request_id, "g6-policy-reply"),
                            status="NEED_CLARIFICATION",
                            corpus_version=data.version,
                            data_classification="D2_COMPANY_REAL",
                            provider="backend-policy",
                            model="catalog-context-policy",
                            sources=[],
                            missing_information=[
                                next_context.pending_clarification or "procedure_id"
                            ],
                        ).model_dump(mode="json")
                else:
                    answer, grounding = await answer_with_rag(
                        db,
                        retrieval_query,
                        request_id,
                        ai,
                    )
                    if isinstance(next_context, IntentContextState):
                        if source_discrepancy.is_quarantined_scholarship_documents(
                            next_context, data, grounding
                        ):
                            answer = (
                                answer + "\n\n" + source_discrepancy.CONFLICT_NOTICE
                                if grounding.get("checklist")
                                else source_discrepancy.CONFLICT_NOTICE
                            )
                        answer = realtime.offer(
                            next_context,
                            data,
                            answer,
                            grounding,
                            enabled=settings.g5_realtime_mcp_enabled,
                        )
            else:
                answer = await ai.generate(
                    request_id,
                    [{"role": message.role, "content": message.content} for message in history],
                )
        except APIError:
            user_message.status = "failed"
            conversation.updated_at = utcnow()
            await db.commit()
            raise
        if grounding and isinstance(next_context, IntentContextState):
            active_id = getattr(next_context, "procedure_id", None)
            if active_id and active_id in data.procedures:
                grounding["procedure_title"] = data.procedures[active_id]["title"]
        if isinstance(next_context, IntentContextState) and not realtime_handled:
            next_context = record_grounded_turn(
                next_context,
                user_query=body.content,
                message_id=str(user_message.id),
                grounding=grounding,
            )
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
                if title.lower().startswith("thủ tục "):
                    title = title[7:].strip()
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
