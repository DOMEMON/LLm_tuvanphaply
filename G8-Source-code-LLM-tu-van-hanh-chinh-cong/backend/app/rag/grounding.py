import asyncio
import logging
import time

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.errors import APIError
from app.rag.corpus import CorpusError
from app.rag.g3.runtime import (
    load_active_dataset,
    retrieve_active_dataset,
    retrieve_direct_active_dataset,
)
from app.rag.retrieval import retrieve
from app.rag.schemas import (
    Citation,
    EvidenceBundle,
    GenerateGroundedRequest,
    GroundedAnswer,
    Grounding,
    GroundingEvidence,
    RetrievalInput,
)
from app.rag.store import load_corpus

logger = logging.getLogger("g1.requests")
EXPECTED_GROUNDED_PROMPT_VERSION = "g2-grounded-v2"


def validate_answer(answer: GroundedAnswer, bundle: EvidenceBundle) -> Grounding:
    available = {item.fragment_id: item for item in bundle.evidence}
    cited = answer.cited_fragment_ids
    if answer.request_id != bundle.request_id or (
        answer.evidence_bundle_id != bundle.evidence_bundle_id
        or answer.corpus_version != bundle.corpus_version
        or answer.data_classification != bundle.data_classification
        or answer.prompt_version != EXPECTED_GROUNDED_PROMPT_VERSION
    ):
        raise APIError(
            502,
            "AI_RESPONSE_BINDING_INVALID",
            "Phản hồi AI không khớp với lượt truy hồi hiện tại.",
        )
    if len(cited) != len(set(cited)) or not set(cited) <= available.keys():
        raise APIError(502, "AI_CITATION_INVALID", "Phản hồi AI có nguồn không hợp lệ.")
    if (
        (answer.status == "ANSWER" and (not available or not cited or answer.missing_information))
        or (answer.status == "NEED_CLARIFICATION" and not answer.missing_information)
        or (answer.status == "INSUFFICIENT_DATA" and cited)
    ):
        raise APIError(502, "AI_RESPONSE_INVALID", "Phản hồi AI không đúng quy tắc an toàn.")
    return Grounding(
        request_id=bundle.request_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        status=answer.status,
        corpus_version=bundle.corpus_version,
        data_classification=bundle.data_classification,
        prompt_version=answer.prompt_version,
        provider=answer.provider,
        model=answer.model,
        sources=[
            Citation(
                fragment_id=i,
                source_id=available[i].source_id,
                title=available[i].title,
                url=available[i].url,
                metadata=available[i].metadata,
            )
            for i in cited
        ],
        missing_information=answer.missing_information,
    )


async def generate_grounded(client: httpx.AsyncClient, bundle: EvidenceBundle) -> GroundedAnswer:
    settings = get_settings()
    payload = GenerateGroundedRequest(
        request_id=bundle.request_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        corpus_version=bundle.corpus_version,
        data_classification=bundle.data_classification,
        query=bundle.query,
        evidence=[
            GroundingEvidence(
                fragment_id=item.fragment_id,
                source_id=item.source_id,
                procedure_id=item.procedure_id,
                section_type=item.section_type,
                title=item.title,
                text=item.text,
                jurisdiction=item.jurisdiction,
                effective_from=item.effective_from,
                effective_to=item.effective_to,
                validity_status=item.validity_status,
            )
            for item in bundle.evidence
        ],
        as_of=bundle.as_of,
        max_output_tokens=settings.grounded_max_output_tokens,
    )
    try:
        async with asyncio.timeout(settings.ai_request_timeout_seconds):
            response = await client.post(
                settings.ai_service_url.rstrip("/") + "/internal/v2/generate",
                json=payload.model_dump(mode="json"),
                timeout=settings.ai_request_timeout_seconds,
            )
            if response.status_code == 504:
                raise TimeoutError()
            if response.status_code == 503:
                raise APIError(
                    503,
                    "AI_GROUNDED_UNAVAILABLE",
                    "AI grounded chưa sẵn sàng cho loại dữ liệu này.",
                )
            response.raise_for_status()
            return GroundedAnswer.model_validate(response.json())
    except APIError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise APIError(
            504, "AI_SERVICE_TIMEOUT", "AI đang phản hồi chậm. Vui lòng thử lại."
        ) from exc
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        raise APIError(502, "AI_SERVICE_ERROR", "Phản hồi grounded AI không hợp lệ.") from exc


async def _answer_with_rag(db, query: RetrievalInput, request_id, ai):
    settings = get_settings()
    started = time.perf_counter()
    try:
        if not settings.rag_corpus_version:
            raise CorpusError("CORPUS_VERSION_REQUIRED")
        if settings.g3_d2_enabled:
            data = load_active_dataset(
                str(settings.g3_private_dataset_path),
                settings.rag_corpus_version,
                settings.g3_expected_rows,
                settings.g3_corpus_sha256,
            )
            from app.rag.field_policy import serving_fields

            if query.field_intents is not None:
                query = query.model_copy(update={
                    "field_intents": serving_fields(query.field_intents, data.version)
                })
            if query.acquisition_mode == "direct_catalog":
                bundle, retrieval_status, missing_fields = retrieve_direct_active_dataset(
                    data, query, request_id
                )
            else:
                bundle, retrieval_status, missing_fields = retrieve_active_dataset(
                    data, query, request_id
                )
            corpus_version = data.version
        else:
            corpus = await load_corpus(db, settings.rag_corpus_version, settings.rag_allow_fixtures)
            legacy_result = retrieve(corpus, query, request_id)
            bundle = legacy_result.bundle
            retrieval_status = (
                "NEED_CLARIFICATION" if legacy_result.needs_clarification else "INSUFFICIENT_DATA"
            )
            missing_fields = (
                ["procedure_id hoặc jurisdiction"] if legacy_result.needs_clarification else []
            )
            corpus_version = corpus.version
    except (CorpusError, ValidationError, ValueError) as exc:
        raise APIError(503, "RAG_CORPUS_UNAVAILABLE", "Corpus RAG chưa sẵn sàng.") from exc
    await db.commit()  # Release transaction before external generation.
    logger.info(
        "request_id=%s stage=retrieval mode=%s latency_ms=%.3f hits=%s",
        request_id,
        query.acquisition_mode,
        (time.perf_counter() - started) * 1000,
        len(bundle.evidence),
    )
    if not bundle.evidence:
        status = retrieval_status
        if status == "NEED_CLARIFICATION" and "field_intents" in missing_fields:
            from app.rag.field_policy import FIELD_CHOICES

            text = FIELD_CHOICES
            if settings.g3_d2_enabled and query.filters.procedure_id in data.procedures:
                title = data.procedures[query.filters.procedure_id]["title"]
                text = f"Mình đang hiểu bạn hỏi thủ tục: {title.rstrip('.')}.\n\n" + text
            from app.rag.answer_focus import unsupported_personal_conclusion

            caution = unsupported_personal_conclusion(query.query)
            if caution:
                text = caution + "\n\n" + text
        elif status == "NEED_CLARIFICATION":
            text = "Vui lòng cho biết thủ tục hoặc phạm vi địa phương cần tra cứu."
        else:
            from app.rag.answer_plan import LABELS

            labels = ", ".join(LABELS.get(field, field) for field in missing_fields)
            text = (
                f"Nguồn đã duyệt chưa có đủ thông tin về: {labels}."
                if labels
                else "Chưa có đủ dữ liệu đã duyệt để trả lời câu hỏi này."
            )
        grounding = Grounding(
            request_id=request_id,
            evidence_bundle_id=bundle.evidence_bundle_id,
            status=status,
            corpus_version=corpus_version,
            data_classification=bundle.data_classification,
            sources=[],
            missing_information=missing_fields,
        )
        return text, grounding.model_dump(mode="json")
    # Never call legacy G1 generation as a fallback for a missing grounded endpoint.
    if settings.g4_answer_plan_enabled:
        from app.rag.planned_grounding import answer_with_plan

        return await answer_with_plan(
            ai.client,
            bundle,
            data,
            fields=query.field_intents,
        )
    answer = await generate_grounded(ai.client, bundle)
    grounding = validate_answer(answer, bundle)
    return answer.answer, grounding.model_dump(mode="json")


async def answer_with_rag(db, query: RetrievalInput, request_id, ai):
    started = time.perf_counter()
    try:
        return await _answer_with_rag(db, query, request_id, ai)
    finally:
        logger.info(
            "request_id=%s stage=rag_orchestration latency_ms=%.3f",
            request_id,
            (time.perf_counter() - started) * 1000,
        )
