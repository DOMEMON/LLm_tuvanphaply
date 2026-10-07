import asyncio
import json
from uuid import UUID

import httpx
from fastapi import Request
from pydantic import ValidationError

from app.config import get_settings
from app.errors import APIError
from app.rag.g5.intent_context import IntentContextOutput, validate_model_output
from app.schemas import AIOutput, IntentContextAIOutput


class AIClient:
    def __init__(self, client: httpx.AsyncClient, *, hybrid_router=None, source_update_client=None):
        self.client = client
        self.hybrid_router = hybrid_router
        self.source_update_client = source_update_client

    async def plan_react_action(self, *, request_id: UUID, context: str) -> dict:
        settings = get_settings()
        try:
            response = await self.client.post(
                settings.ai_service_url.rstrip("/") + "/internal/v3/react-step",
                json={
                    "request_id": str(request_id),
                    "data_classification": "D2_COMPANY_REAL",
                    "context": context,
                },
                timeout=settings.g6_react_timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
            if result["request_id"] != str(request_id):
                raise ValueError("react request mismatch")
            return result["action"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise APIError(502, "REACT_UNAVAILABLE", "Nhánh thử nghiệm chưa sẵn sàng.") from exc

    async def generate(self, request_id: UUID, messages: list[dict]) -> str:
        settings = get_settings()
        try:
            # A wall-clock deadline also bounds a response that keeps streaming bytes.
            async with asyncio.timeout(settings.ai_request_timeout_seconds):
                response = await self.client.post(
                    settings.ai_service_url.rstrip("/") + "/internal/v1/generate",
                    json={
                        "request_id": str(request_id),
                        "messages": messages,
                        "max_output_tokens": settings.llm_max_output_tokens,
                    },
                    timeout=settings.ai_request_timeout_seconds,
                )
                if response.status_code == 504:
                    raise APIError(
                        504,
                        "AI_SERVICE_TIMEOUT",
                        "AI đang phản hồi chậm. Vui lòng thử lại.",
                    )
                response.raise_for_status()
                result = AIOutput.model_validate(response.json())
                if result.request_id != request_id or not result.text.strip():
                    raise ValueError("Invalid AI response")
                return result.text
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise APIError(
                504, "AI_SERVICE_TIMEOUT", "AI đang phản hồi chậm. Vui lòng thử lại."
            ) from exc
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise APIError(
                502, "AI_SERVICE_ERROR", "Không thể nhận phản hồi từ AI. Vui lòng thử lại."
            ) from exc

    async def analyze_intent_context(
        self,
        *,
        request_id: UUID,
        current_message_id: UUID,
        current_user_message: str,
        state_before: dict,
        procedure_candidates: list[dict[str, str]],
        data_classification: str,
        proposal_only: bool = False,
        previous_user_message: str = "",
        previous_assistant_message: str = "",
    ) -> IntentContextOutput:
        settings = get_settings()
        try:
            async with asyncio.timeout(settings.ai_request_timeout_seconds):
                response = await self.client.post(
                    settings.ai_service_url.rstrip("/") + "/internal/v3/intent-context",
                    json={
                        "request_id": str(request_id),
                        "current_message_id": str(current_message_id),
                        "current_user_message": current_user_message,
                        "state_before": state_before,
                        "procedure_candidates": procedure_candidates,
                        "data_classification": data_classification,
                        "max_output_tokens": 384,
                        **({"proposal_only": True,
                            "previous_user_message": previous_user_message[:1000],
                            "previous_assistant_message": previous_assistant_message[:1600]}
                           if proposal_only else {}),
                    },
                    timeout=settings.ai_request_timeout_seconds,
                )
                response.raise_for_status()
            result = IntentContextAIOutput.model_validate(response.json())
            if result.request_id != request_id:
                raise ValueError("intent response request mismatch")
            return validate_model_output(
                json.dumps(result.output, ensure_ascii=False),
                current_message_id=str(current_message_id),
                current_user_message=current_user_message,
                candidate_ids={candidate["id"] for candidate in procedure_candidates},
                state_before=state_before,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise APIError(
                504, "INTENT_CONTEXT_TIMEOUT", "Bộ hiểu hội thoại đang phản hồi chậm."
            ) from exc
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
            raise APIError(
                502, "INTENT_CONTEXT_ERROR", "Không thể kiểm chứng kết quả hiểu hội thoại."
            ) from exc


def get_ai(request: Request) -> AIClient:
    return request.app.state.ai
