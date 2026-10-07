from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ID = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
QueryText = Annotated[str, StringConstraints(min_length=1, max_length=4_000, pattern=r"\S")]
GroundedText = Annotated[str, StringConstraints(min_length=1, max_length=8_000, pattern=r"\S")]
Title = Annotated[str, StringConstraints(min_length=1, max_length=500, pattern=r"\S")]
Scope = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500, strip_whitespace=True, pattern=r"\S"),
]
Status = Literal["ANSWER", "NEED_CLARIFICATION", "INSUFFICIENT_DATA"]
DataClassification = Literal["D0_SYSTEM_SMOKE", "D1_PUBLIC_DEMO", "D2_COMPANY_REAL"]
ValidityStatus = Literal["CURRENT", "EXPIRED", "FUTURE", "UNKNOWN"]
BUSINESS_FIELDS = (
    "applicant_scope",
    "receiving_authority",
    "submission_methods",
    "required_documents",
    "steps",
    "fees",
    "processing_time",
    "legal_bases",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceManifest(Strict):
    source_id: ID
    source_url: Text
    source_domain: Text
    title: Text
    issuing_authority: str | None
    jurisdiction: str | None
    procedure_code: str | None
    retrieved_at: AwareDatetime
    effective_from: date | None
    effective_to: date | None
    status: Literal["UNKNOWN", "CONFIRMED_FROM_SOURCE"]
    content_sha256: Hash
    raw_path: Text
    parser_version: Text
    review_status: Literal["PENDING", "APPROVED"]
    usage: Literal["D1_PUBLIC_DEMO", "D0_SYSTEM_SMOKE"]

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("Invalid effective interval")
        return self


class ProcedureRecord(Strict):
    procedure_id: ID
    title: Text
    aliases: list[Text]
    jurisdiction: str | None
    applicant_scope: list[Text]
    receiving_authority: list[Text]
    submission_methods: list[Text]
    required_documents: list[Text]
    steps: list[Text]
    fees: list[Text]
    processing_time: list[Text]
    legal_bases: list[Text]
    source_ids: list[ID] = Field(min_length=1)
    version: Text
    field_evidence: dict[str, list[list[ID]]]


class SourceLocator(Strict):
    archive_member: Text | None = None
    page_start: int | None = Field(default=None, ge=1, strict=True)
    page_end: int | None = Field(default=None, ge=1, strict=True)
    inner_content_sha256: Hash | None = None
    extraction_tool: Text | None = None

    @model_validator(mode="after")
    def valid_page_range(self):
        if self.page_start and self.page_end and self.page_end < self.page_start:
            raise ValueError("Invalid source page interval")
        return self


class ProcedureFragment(Strict):
    fragment_id: ID
    procedure_id: ID
    section_type: Literal[
        "OVERVIEW",
        "APPLICANT_SCOPE",
        "RECEIVING_AUTHORITY",
        "SUBMISSION_METHODS",
        "REQUIRED_DOCUMENTS",
        "STEPS",
        "FEES",
        "PROCESSING_TIME",
        "LEGAL_BASES",
        "PROCEDURE_SCOPE",
        "EFFECTIVE_DATE",
        "OTHER",
    ]
    text: GroundedText
    source_id: ID
    ordinal: int = Field(ge=0, strict=True)
    content_sha256: Hash
    source_locator: SourceLocator | None = None


class Filters(Strict):
    procedure_id: ID | None = None
    jurisdiction: Scope | None = None
    as_of: date | None = None


class RetrievalInput(Strict):
    # Internal override for a pending clarification; not exposed by MessageInput.
    field_intents: list[ID] | None = None
    # Server-owned evidence acquisition policy. Clients cannot set this value.
    acquisition_mode: Literal["hybrid_retrieval", "direct_catalog"] = "hybrid_retrieval"
    query: QueryText
    top_k: int = Field(default=5, ge=1, le=8, strict=True)
    filters: Filters = Field(default_factory=Filters)


class Evidence(Strict):
    fragment_id: ID
    source_id: ID
    procedure_id: ID
    section_type: ID
    title: Title
    url: Text | None
    text: GroundedText
    score: float = Field(ge=0, allow_inf_nan=False)
    jurisdiction: Scope | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    validity_status: ValidityStatus = "UNKNOWN"
    metadata: dict

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Invalid effective interval")
        return self


class EvidenceBundle(Strict):
    request_id: UUID
    evidence_bundle_id: UUID
    corpus_version: ID
    data_classification: DataClassification
    query: QueryText
    evidence: list[Evidence] = Field(max_length=8)
    as_of: date | None = None

    @model_validator(mode="after")
    def valid_evidence_budget(self):
        fragment_ids = [item.fragment_id for item in self.evidence]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("Duplicate evidence fragment IDs")
        if sum(len(item.text) for item in self.evidence) > 32_000:
            raise ValueError("Evidence text budget exceeded")
        return self


class Usage(Strict):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class GroundedAnswer(Strict):
    request_id: UUID
    evidence_bundle_id: UUID
    corpus_version: ID
    data_classification: DataClassification
    prompt_version: ID
    answer: GroundedText
    status: Status
    cited_fragment_ids: list[ID] = Field(max_length=8)
    missing_information: list[Scope] = Field(max_length=10)
    provider: ID
    model: ID
    usage: Usage
    latency_ms: int = Field(ge=0, strict=True)

    @field_validator("cited_fragment_ids", "missing_information")
    @classmethod
    def unique_lists(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate list value")
        return value

    @model_validator(mode="after")
    def valid_status(self):
        if self.status == "ANSWER" and (not self.cited_fragment_ids or self.missing_information):
            raise ValueError("Invalid ANSWER invariant")
        if self.status == "NEED_CLARIFICATION" and not self.missing_information:
            raise ValueError("Invalid NEED_CLARIFICATION invariant")
        if self.status == "INSUFFICIENT_DATA" and self.cited_fragment_ids:
            raise ValueError("Invalid INSUFFICIENT_DATA invariant")
        return self


class Citation(Strict):
    fragment_id: ID
    source_id: ID
    title: Text
    url: Text | None
    metadata: dict


class VerificationResult(Strict):
    plan_version: Literal["g4-answer-plan-v1"] = "g4-answer-plan-v1"
    policy: Literal["full-claim-exact-v1"] = "full-claim-exact-v1"
    candidate_checked: bool
    candidate_passed: bool
    fallback_used: bool
    reasons: list[str]


class ChecklistItem(Strict):
    field: ID
    label: Title
    value: GroundedText
    evidence_ids: list[ID] = Field(min_length=1, max_length=8)


class Grounding(Strict):
    partial: bool = False
    parts: list[dict] | None = None
    request_id: UUID
    evidence_bundle_id: UUID
    status: Status
    corpus_version: ID
    data_classification: DataClassification
    prompt_version: ID | None = None
    provider: ID | None = None
    model: ID | None = None
    sources: list[Citation]
    procedure_title: Title | None = None
    missing_information: list[str]
    verification: VerificationResult | None = None
    checklist: list[ChecklistItem] | None = None
    knowledge_version: ID | None = None
    source_checked_at: AwareDatetime | None = None


class GoldQuestion(Strict):
    question_id: ID
    question: Text
    procedure_id: ID | None
    facts: dict
    expected_status: Status
    expected_points: list[Text]
    forbidden_points: list[Text]
    gold_source_ids: list[ID]
    gold_fragment_ids: list[ID]
    contains_pii: Literal[False]
    split: Literal["TRAIN", "DEV", "TEST"]
    author: Text
    reviewer: Text
    filters: Filters = Field(default_factory=Filters)


class GroundingEvidence(Strict):
    fragment_id: ID
    source_id: ID
    procedure_id: ID
    section_type: ID
    title: Title
    text: GroundedText
    jurisdiction: Scope | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    validity_status: ValidityStatus = "UNKNOWN"


class GenerateGroundedRequest(Strict):
    request_id: UUID
    evidence_bundle_id: UUID
    corpus_version: ID
    data_classification: DataClassification
    query: QueryText
    evidence: list[GroundingEvidence] = Field(max_length=8)
    as_of: date | None = None
    max_output_tokens: int = Field(ge=32, le=2048, strict=True)

    @model_validator(mode="after")
    def valid_evidence_budget(self):
        fragment_ids = [item.fragment_id for item in self.evidence]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("Duplicate evidence fragment IDs")
        if sum(len(item.text) for item in self.evidence) > 32_000:
            raise ValueError("Evidence text budget exceeded")
        return self
