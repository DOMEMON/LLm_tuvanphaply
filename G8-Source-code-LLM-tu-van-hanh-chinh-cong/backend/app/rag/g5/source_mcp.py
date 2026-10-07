"""Read published source overrides through MCP and bind them as evidence."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import urlsplit

from mcp import Client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.rag.g3.validator import SECTIONS
from app.rag.g5.realtime_contract import LookupRequest, LookupResult
from app.rag.schemas import Evidence, EvidenceBundle

DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "source-service"})


def _title_norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\s*([,;/()])\s*", r"\1", text)
    return " ".join(text.split()).strip(" :")


def _compatible_current_title(request: LookupRequest, answer: LookupResult) -> bool:
    if answer.status not in {"FOUND", "PARTIAL"} or not answer.source_title:
        return True
    expected = _title_norm(request.procedure_name)
    if answer.matched_procedure_name:
        # v2 binds a document citation to the requested procedure separately
        # from the document's own title; never accept another procedure binding.
        return _title_norm(answer.matched_procedure_name) == expected
    actual = _title_norm(answer.source_title)
    if actual == expected:
        return True
    return (
        "DVC_PUBLIC_JSON_API_UNIQUE_HIGH_SIMILARITY_TITLE" in answer.retrieval_note
        and SequenceMatcher(None, expected, actual).ratio() >= 0.88
    )


class PublishedSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^g5-snapshot-[0-9a-f]{32}$")
    version: str = Field(pattern=r"^g5-source-v[0-9]{6}$")
    procedure_id: str
    field: str
    section_type: str
    text: str = Field(min_length=1, max_length=8_000)
    title: str = Field(min_length=1, max_length=500)
    source_url: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["PUBLISHED"]
    created_at: AwareDatetime
    reviewed_at: AwareDatetime
    reviewer: str = Field(min_length=1, max_length=100)
    published_at: AwareDatetime

    @model_validator(mode="after")
    def verify_snapshot(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("G5_SOURCE_CONTENT_HASH_MISMATCH")
        if not self.created_at <= self.reviewed_at <= self.published_at:
            raise ValueError("G5_SOURCE_REVIEW_TIMELINE_INVALID")
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("G5_SOURCE_EFFECTIVE_RANGE_INVALID")
        return self


class MCPSourceUpdateClient:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
    ) -> None:
        parsed = urlsplit(url)
        hostname = parsed.hostname.casefold() if parsed.hostname else None
        if (
            parsed.scheme != "http"
            or hostname not in {item.casefold() for item in allowed_hosts}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("G5_SOURCE_MCP_URL_NOT_ALLOWED")
        if not url.rstrip("/").endswith("/mcp"):
            raise ValueError("G5_SOURCE_MCP_ENDPOINT_REQUIRED")
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def get_update(self, *, procedure_id: str, field: str) -> PublishedSourceUpdate | None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with Client(self.url, read_timeout_seconds=self.timeout_seconds) as client:
                    result = await client.call_tool(
                        "get_procedure_update",
                        {"procedure_id": procedure_id, "field": field},
                        read_timeout_seconds=self.timeout_seconds,
                    )
            payload = result.structured_content
            if result.is_error or not isinstance(payload, dict):
                return None
            # MCP wraps a plain Python dict in {"result": ...} on some SDK paths.
            if set(payload) == {"result"} and isinstance(payload["result"], dict):
                payload = payload["result"]
            if payload.get("status") != "PUBLISHED":
                return None
            update = PublishedSourceUpdate.model_validate(payload.get("snapshot"))
            if (
                update.status != "PUBLISHED"
                or update.procedure_id != procedure_id
                or update.field != field
                or update.section_type != SECTIONS[field]
            ):
                return None
            return update
        except Exception:
            # Optional update transport fails open to the immutable reviewed corpus.
            return None

    async def lookup_current(self, request: LookupRequest) -> LookupResult | None:
        """Call the separate unreviewed lookup path after consent was consumed."""

        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with Client(self.url, read_timeout_seconds=self.timeout_seconds) as client:
                    result = await client.call_tool(
                        "lookup_current_procedure",
                        request.model_dump(mode="json"),
                        read_timeout_seconds=self.timeout_seconds,
                    )
            payload = result.structured_content
            if result.is_error or not isinstance(payload, dict):
                return None
            if set(payload) == {"result"} and isinstance(payload["result"], dict):
                payload = payload["result"]
            answer = LookupResult.model_validate(payload)
            now = datetime.now(timezone.utc)
            if (
                answer.procedure_id != request.procedure_id
                or answer.field != request.field
                or answer.jurisdiction != request.jurisdiction
                or request.as_of != now.date()
                or not -30 <= (now - answer.checked_at).total_seconds() <= 120
                or not _compatible_current_title(request, answer)
            ):
                return None
            return answer
        except Exception:
            # Realtime is optional and never replaces the immutable local source.
            return None


def _valid_on(update: PublishedSourceUpdate, as_of: date) -> bool:
    return (update.effective_from is None or update.effective_from <= as_of) and (
        update.effective_to is None or as_of <= update.effective_to
    )


async def apply_source_updates(
    bundle: EvidenceBundle,
    *,
    fields: list[str],
    field_evidence: dict[str, list[str]],
    client: MCPSourceUpdateClient,
) -> tuple[EvidenceBundle, dict[str, list[str]]]:
    """Replace a field atomically only with a current PUBLISHED MCP snapshot."""
    procedure_ids = {item.procedure_id for item in bundle.evidence}
    if len(procedure_ids) != 1:
        return bundle, field_evidence
    procedure_id = next(iter(procedure_ids))
    effective_date = bundle.as_of or datetime.now(timezone.utc).date()
    evidence = list(bundle.evidence)
    updated_manifest = {key: list(value) for key, value in field_evidence.items()}
    for field in fields:
        if field not in SECTIONS:
            continue
        update = await client.get_update(procedure_id=procedure_id, field=field)
        if update is None or not _valid_on(update, effective_date):
            continue
        section = SECTIONS[field]
        existing = [item for item in evidence if item.section_type == section]
        if not existing:
            # G5 does not let MCP invent a field absent from the reviewed base corpus.
            continue
        evidence = [item for item in evidence if item.section_type != section]
        source_id = f"g5-source-{update.content_sha256[:16]}"
        evidence.append(
            Evidence(
                fragment_id=update.snapshot_id,
                source_id=source_id,
                procedure_id=procedure_id,
                section_type=section,
                title=update.title,
                url=update.source_url,
                text=update.text,
                score=1.0,
                jurisdiction=existing[0].jurisdiction,
                effective_from=update.effective_from,
                effective_to=update.effective_to,
                validity_status="CURRENT",
                metadata={
                    "source_type": "MCP_REVIEWED_UPDATE",
                    "source_version": update.version,
                    "source_checked_at": update.published_at.isoformat(),
                    "reviewer": update.reviewer,
                },
            )
        )
        updated_manifest[field] = [update.snapshot_id]
    return bundle.model_copy(update={"evidence": evidence}), updated_manifest
