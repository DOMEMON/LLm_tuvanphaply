"""Typed, bounded planner contract. No generated administrative facts accepted."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

FIELDS = ['required_documents', 'fees', 'receiving_authority', 'processing_times', 'submission_methods']
FieldName = Literal['required_documents', 'fees', 'receiving_authority', 'processing_times',
                    'submission_methods', 'steps', 'legal_bases', 'applicant_scope']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Scope(Strict):
    # Requested place of service, NEVER nationality/residence mentioned incidentally.
    places: list[str] = Field(default_factory=list, max_length=5)
    # Legacy internal state compatibility; the model no longer fills hierarchy.
    country: str = Field(default='', max_length=80)
    province: str = Field(default='', max_length=100)
    ward: str = Field(default='', max_length=100)
    quote: str = Field(default='', max_length=300)


class CatalogPredicate(Strict):
    field: Literal['fees', 'submission_methods', 'processing_times', 'required_documents', 'receiving_authority']
    operator: Literal['eq', 'contains', 'not_contains', 'lte', 'gte']
    value: str = Field(min_length=1, max_length=160)


class Task(Strict):
    kind: Literal['procedure', 'catalog', 'clarify', 'outside', 'chat']
    quote: str = Field(min_length=1, max_length=4000)
    # Wire planner uses semantic labels; validation binds them to internal codes.
    code: str = Field(default='', max_length=500)
    fields: list[FieldName] = Field(default_factory=list, max_length=8)
    domains: list[str] = Field(default_factory=list, max_length=13)
    candidates: list[str] = Field(default_factory=list, max_length=5)
    scope: Scope = Field(default_factory=Scope)
    # Short question only. Never rendered unchecked as facts.
    question: str = Field(default='', max_length=250)
    predicates: list[CatalogPredicate] = Field(default_factory=list, max_length=4)


class Plan(Strict):
    relation: Literal['replace', 'continue', 'extend', 'reset']
    tasks: list[Task] = Field(min_length=1, max_length=8)
    overflow: bool = False


class State(Strict):
    version: Literal['g7-v3'] = 'g7-v3'
    corpus_version: str
    active: list[Task] = Field(default_factory=list, max_length=36)
    focused: list[str] = Field(default_factory=list, max_length=36)
    pending: list[Task] = Field(default_factory=list, max_length=36)
    displayed_options: list[str] = Field(default_factory=list, max_length=36)
    # Only backend can bind/consume this consent. Model is never its authority.
    pending_realtime: dict | None = None
    # Do not fall back to an older topic after the latest user turn failed.
    awaiting_rephrase: bool = False
    catalog_context: dict = Field(default_factory=dict)
    reference_limited: bool = False


def load_state(raw, version):
    if not raw or raw.get('version') != 'g7-v3' or raw.get('corpus_version') != version:
        return State(corpus_version=version)
    from app.rag.g8.mentor_context import bound_context
    return bound_context(State.model_validate(raw))
