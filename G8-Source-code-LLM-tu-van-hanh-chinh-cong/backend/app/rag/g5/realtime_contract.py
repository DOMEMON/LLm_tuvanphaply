"""Wire contract v1. Keep identical to backend/app/rag/g5/realtime_contract.py."""

from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

ALLOWED_HOSTS = frozenset(
    {
        "dichvucong.gov.vn",
        "www.dichvucong.gov.vn",
        "thutuc.dichvucong.gov.vn",
        "vpcp.dichvucong.gov.vn",
        "congbao.hochiminhcity.gov.vn",
        "congbaocdn.chinhphu.vn",
        "vbpl.vn",
    }
)
LookupField = Literal[
    "processing_times",
    "receiving_authority",
    "submission_methods",
    "required_documents",
    "fees",
    "steps",
    "legal_bases",
    "applicant_scope",
]


def official_url(url: str) -> str:
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in ALLOWED_HOSTS
        or p.username is not None
        or p.password is not None
        or p.port not in (None, 443)
        or p.fragment
        or any(c.isspace() or ord(c) < 32 for c in url)
        or "\\" in url
    ):
        raise ValueError("DVC_URL_NOT_ALLOWED")
    return url


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    procedure_id: str = Field(min_length=1, max_length=200)
    procedure_name: str = Field(min_length=1, max_length=500)
    field: LookupField
    jurisdiction: str = Field(min_length=1, max_length=200)
    as_of: date


class LookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["FOUND", "PARTIAL", "NOT_FOUND", "AMBIGUOUS", "ERROR"]
    procedure_id: str = Field(min_length=1, max_length=200)
    field: LookupField
    value: str | None = Field(default=None, max_length=8000)
    source_title: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=2000)
    jurisdiction: str = Field(min_length=1, max_length=200)
    checked_at: AwareDatetime
    retrieval_note: str = Field(min_length=1, max_length=1000)
    alternatives: list[str] = Field(default_factory=list, max_length=5)
    scope: Literal["LOCALITY", "PROVINCE", "NATIONAL", "UNVERIFIED"] | None = None
    limitations: list[str] = Field(default_factory=list, max_length=5)
    source_checked_at: AwareDatetime | None = None
    cache_hit: bool = False
    matched_procedure_name: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_found(self):
        if any(not 1 <= len(note) <= 1000 for note in self.limitations):
            raise ValueError("INVALID_LIMITATION")
        if self.source_url:
            official_url(self.source_url)
        if any(not 1 <= len(title) <= 500 for title in self.alternatives):
            raise ValueError("INVALID_ALTERNATIVE_TITLE")
        if self.alternatives and self.status != "AMBIGUOUS":
            raise ValueError("ALTERNATIVES_REQUIRE_AMBIGUOUS_STATUS")
        if self.status in {"FOUND", "PARTIAL"}:
            if not all((self.value, self.source_title, self.source_url)):
                raise ValueError("FOUND_REQUIRES_VALUE_AND_CITATION")
            if self.status == "PARTIAL" and (not self.limitations or not self.scope):
                raise ValueError("PARTIAL_REQUIRES_SCOPE_AND_LIMITATIONS")
        elif any(item is not None for item in (self.value, self.source_title, self.source_url)):
            raise ValueError("NON_FOUND_MUST_NOT_HAVE_VALUE_OR_CITATION")
        return self
