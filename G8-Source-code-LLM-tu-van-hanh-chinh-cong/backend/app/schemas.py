from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.rag.schemas import Filters, Grounding


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginInput(InputModel):
    display_name: str = Field(min_length=1, max_length=100)


class ConversationInput(InputModel):
    title: str = Field(default="Cuộc trò chuyện mới", min_length=1, max_length=200)


class MessageInput(InputModel):
    retrieval_filters: Filters = Field(default_factory=Filters)
    client_message_id: UUID
    content: str = Field(min_length=1, max_length=10000)


class FeedbackInput(InputModel):
    relevance: Literal["relevant", "not_relevant"]
    satisfaction: Literal["satisfied", "not_satisfied"]
    reason: str | None = Field(default=None, max_length=1000)


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOutput(OutputModel):
    id: UUID
    display_name: str
    username: str | None = None
    email: str | None = None
    is_guest: bool = False


class ConversationOutput(OutputModel):
    id: UUID
    title: str
    is_pinned: bool = False
    topic: str | None = None
    created_at: datetime
    updated_at: datetime


class FeedbackOutput(OutputModel):
    id: UUID
    message_id: UUID
    relevance: Literal["relevant", "not_relevant"]
    satisfaction: Literal["satisfied", "not_satisfied"]
    reason: str | None = None
    provider: str | None = None
    model: str | None = None
    corpus_version: str | None = None
    prompt_version: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageOutput(OutputModel):
    grounding: Grounding | None = None
    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["pending", "completed", "failed"]
    created_at: datetime


class MessagePair(BaseModel):
    user_message: MessageOutput
    assistant_message: MessageOutput


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: UUID


class ErrorOutput(BaseModel):
    error: ErrorDetail


class Usage(BaseModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class AIOutput(BaseModel):
    request_id: UUID
    text: str = Field(min_length=1)
    provider: str = Field(min_length=1, pattern=r"\S")
    model: str = Field(min_length=1)
    usage: Usage
    latency_ms: float = Field(ge=0)


class IntentContextAIOutput(BaseModel):
    request_id: UUID
    output: dict
    provider: str = Field(min_length=1, pattern=r"\S")
    model: str = Field(min_length=1, pattern=r"\S")
    usage: Usage
    latency_ms: float = Field(ge=0)
