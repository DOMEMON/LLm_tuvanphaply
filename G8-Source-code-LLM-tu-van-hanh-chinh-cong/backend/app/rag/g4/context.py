"""Conversation-local state; only user messages establish facts, each with provenance."""

import re
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.rag.catalog_policy import preferred_procedure_id
from app.rag.g3.retrieval import fields_for_query, procedure_names, retrieve, routing_query
from app.rag.retrieval import normalize
from app.rag.schemas import ID, Filters, RetrievalInput, Scope, Strict


class QueryContext(Strict):
    procedure_id: ID | None = None
    field_intents: list[ID] = Field(default_factory=list)
    jurisdiction: Scope | None = None
    source_message_ids: dict[str, UUID] = Field(default_factory=dict)


class ConversationContext(Strict):
    schema_version: Literal["g4-context-v1"] = "g4-context-v1"
    corpus_version: ID
    active_procedure_id: ID | None = None
    confirmed_jurisdiction: Scope | None = None
    field_intents: list[ID] = Field(default_factory=list)
    pending_clarification: Literal["procedure_id", "field_intents", "jurisdiction"] | None = None
    source_message_ids: dict[str, UUID] = Field(default_factory=dict)


FOLLOWUP_WORDS = frozenset(
    "con nua vay thi a ah nhe nhe nho the du kien giai quyet ket qua mat ton "
    "nhan du hoan tat xong dau den can toi co quan don vi xin hoi biet giup minh em anh "
    "va hoac chuan bi giay to nop gui cach lieu khong duoc la nhu the nao "
    "quy phuong thuc mang buu dien chinh buoc cac ban cho muon ro cu chi tiet xac them "
    "thong tin ve vui long noi phai muc se dung nen neu cau chieu cuoi de dieu do giu loi "
    "nguyen amount_vnd bo dan hien hoa ma nguoi diem tinh".split()
)


def _explicit_scope(data, text):
    """Match recorded place names only; explicit unknown province/ward stays unverified."""
    q = " " + normalize(text) + " "
    names = {
        value
        for p in data.procedures.values()
        for key, value in p["jurisdiction"].items()
        if key != "country_code" and value and " " + normalize(value) + " " in q
    }
    if names:
        return max(names, key=len)
    match = re.search(
        r"(?:^|\bở\s+|\btại\s+)((?:tỉnh|thành phố|quận|huyện|phường|xã)\s+[^?!.;,]{1,100})",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def prepare_turn(data, query, previous, message_id: UUID, *, procedure_hint: str | None = None):
    """Resolve an isolated turn without reading assistant text or evaluation labels."""
    if previous is None or previous.corpus_version != data.version:
        state = ConversationContext(corpus_version=data.version)
    else:
        state = previous.model_copy(deep=True)
        state.active_procedure_id = preferred_procedure_id(state.active_procedure_id)
    detected = fields_for_query(data, query.query)
    q = " " + normalize(query.query) + " "
    explicit = any(
        " " + name + " " in q for p in data.procedures.values() for name in procedure_names(data, p)
    )
    requested_scope = query.filters.jurisdiction or _explicit_scope(data, query.query)
    identity_query = normalize(query.query)
    if requested_scope:
        identity_query = identity_query.replace(normalize(requested_scope), " ")
    scoped_tokens = set(routing_query(identity_query).split()) - FOLLOWUP_WORDS
    short_followup = bool(state.active_procedure_id) and not scoped_tokens and not explicit
    fields = sorted(detected)
    if (
        not fields
        and state.field_intents
        and (short_followup or state.pending_clarification in {"procedure_id", "jurisdiction"})
    ):
        fields = state.field_intents
    pid = preferred_procedure_id(query.filters.procedure_id)
    inherited = False
    if (
        pid is None
        and state.active_procedure_id
        and (
            short_followup
            or (
                requested_scope
                and state.pending_clarification == "jurisdiction"
                and not explicit
                and not scoped_tokens
            )
        )
    ):
        pid = state.active_procedure_id
        inherited = True
    elif pid is None and procedure_hint is not None and not explicit:
        if procedure_hint not in data.procedures:
            raise ValueError("UNKNOWN_PROCEDURE_HINT")
        pid = preferred_procedure_id(procedure_hint)
    # Persist an explicitly supplied place, including an unsupported place; retrieval abstains.
    scope = requested_scope or state.confirmed_jurisdiction
    prepared = RetrievalInput(
        query=query.query,
        top_k=query.top_k,
        field_intents=fields or None,
        filters=Filters(procedure_id=pid, jurisdiction=scope, as_of=query.filters.as_of),
        acquisition_mode=query.acquisition_mode,
    )
    found = retrieve(
        data,
        prepared.query,
        prepared.top_k,
        procedure_id=pid,
        jurisdiction=scope,
        field_intents=fields or None,
        as_of=query.filters.as_of.isoformat() if query.filters.as_of else None,
    )
    raw_resolved = found["procedure_id"]
    resolved = preferred_procedure_id(raw_resolved)
    if resolved is not None and resolved != raw_resolved:
        found = retrieve(
            data,
            prepared.query,
            prepared.top_k,
            procedure_id=resolved,
            jurisdiction=scope,
            field_intents=fields or None,
            as_of=query.filters.as_of.isoformat() if query.filters.as_of else None,
        )
        resolved = found["procedure_id"]
    if resolved:
        if not inherited:
            state.source_message_ids["active_procedure_id"] = message_id
        state.active_procedure_id = resolved
        prepared.filters.procedure_id = resolved
    else:
        # Explicit unknown/ambiguous requests must not silently reuse the previous procedure.
        state.active_procedure_id = None
        state.source_message_ids.pop("active_procedure_id", None)
    if requested_scope:
        state.confirmed_jurisdiction = requested_scope
        state.source_message_ids["confirmed_jurisdiction"] = message_id
    if detected:
        state.field_intents = sorted(detected)
        state.source_message_ids["field_intents"] = message_id
    elif not fields:
        state.field_intents = []
        state.source_message_ids.pop("field_intents", None)
    if resolved is None:
        state.pending_clarification = "procedure_id"
    elif not fields:
        state.pending_clarification = "field_intents"
    elif found["reason"] == "JURISDICTION_UNVERIFIED":
        state.pending_clarification = "jurisdiction"
    else:
        state.pending_clarification = None
    if state.pending_clarification:
        state.source_message_ids["pending_clarification"] = message_id
    else:
        state.source_message_ids.pop("pending_clarification", None)
    # If the field is unresolved, send an explicit empty override; runtime asks to clarify.
    prepared.field_intents = fields
    context = QueryContext(
        procedure_id=resolved,
        field_intents=fields,
        jurisdiction=scope,
        source_message_ids=dict(state.source_message_ids),
    )
    return prepared, state, context
