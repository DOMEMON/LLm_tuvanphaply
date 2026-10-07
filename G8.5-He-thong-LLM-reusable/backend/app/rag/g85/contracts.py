from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Predicate(Strict):
    attribute: str
    operator: Literal['contains_any', 'equals_set', 'excludes']
    values: list[str] = Field(min_length=1, max_length=12)


class Task(Strict):
    kind: Literal["answer", "catalog", "clarify", "outside", "chat", "action"]
    quote: str = Field(min_length=1, max_length=4000)
    entity: str = ""
    attributes: list[str] = Field(default_factory=list, max_length=12)
    domains: list[str] = Field(default_factory=list, max_length=20)
    candidates: list[str] = Field(default_factory=list, max_length=8)
    mode: Literal["direct", "search"] = "direct"
    search_query: str = Field(default="", max_length=1000)
    service_place: str = Field(default="", max_length=150,
        description="Literal requested service place copied from current user, empty if absent. Do not copy locality from PROFILE.")
    place_quote: str = Field(default="", max_length=500,
        description="Literal current user clause about service location, empty if absent. Residence is not service location.")
    question: str = Field(default="", max_length=250)
    predicates: list[Predicate] = Field(default_factory=list, max_length=4)


class Plan(Strict):
    relation: Literal["replace", "continue", "extend", "reset"]
    tasks: list[Task] = Field(min_length=1, max_length=8)


class State(Strict):
    version: Literal["g85-v1"] = "g85-v1"
    dataset_version: str
    profile_id: str
    profile_version: str
    active: list[Task] = Field(default_factory=list, max_length=36)
    focused: list[str] = Field(default_factory=list, max_length=36)
    displayed_options: list[str] = Field(default_factory=list, max_length=64)
    pending: list[Task] = Field(default_factory=list, max_length=8)
    awaiting_rephrase: bool = False
    catalog_context: dict = Field(default_factory=dict)


def load_state(raw, profile, dataset_version):
    keys = {"dataset_version": dataset_version, "profile_id": profile["id"],
            "profile_version": profile["version"]}
    if not raw or raw.get("version") != "g85-v1" or any(raw.get(k) != v for k, v in keys.items()):
        return State(**keys)
    return State.model_validate(raw)


def current_package_history(history, raw, profile, dataset_version):
    """Never reintroduce pre-switch chat text after the first reset turn."""
    keys = {'dataset_version': dataset_version, 'profile_id': profile['id'],
            'profile_version': profile['version']}
    if not raw or any(raw.get(k) != value for k, value in keys.items()):
        return []
    boundary = 0
    for i, message in enumerate(history):
        if message.role == 'assistant':
            grounding = getattr(message, 'grounding', None) or {}
            if grounding.get('corpus_version') != dataset_version:
                boundary = i + 1
    return history[boundary:]


def transition(state, plan, options):
    result = state.model_copy(deep=True)
    selected = [t for t in plan.tasks if t.kind == "answer"]
    harmless_side_turn = all(t.kind in {"outside", "chat"} for t in plan.tasks)
    if plan.relation == "reset" or plan.relation == "replace" and not harmless_side_turn:
        result.active = []
    keys = {t.entity for t in selected if t.entity}
    result.active = [t for t in result.active if t.entity not in keys] + selected
    result.active = result.active[-36:]
    result.pending = [t for t in plan.tasks if t.kind == "clarify"]
    # Preserve previous focus through an unrelated outside/chat turn. Unclear
    # references remain explicit in the plan; the engine never picks a subject.
    if selected:
        result.focused = list(dict.fromkeys(t.entity for t in selected if t.entity))
    elif plan.relation in {"replace", "reset"} and not all(t.kind in {"outside", "chat"} for t in plan.tasks):
        result.focused = []
    if options:
        # A single drill-down retains its parent menu. A newly displayed
        # multi-entity answer establishes its own order, even on continue.
        is_menu_followup = (plan.relation in {"continue", "extend"} and selected
                            and len(set(options)) == 1
                            and set(options).issubset(state.displayed_options))
        if not is_menu_followup:
            result.displayed_options = list(dict.fromkeys(options))
    elif plan.relation in {"replace", "reset"} and not all(t.kind in {"outside", "chat"} for t in plan.tasks):
        result.displayed_options = []
    result.awaiting_rephrase = False
    return result
