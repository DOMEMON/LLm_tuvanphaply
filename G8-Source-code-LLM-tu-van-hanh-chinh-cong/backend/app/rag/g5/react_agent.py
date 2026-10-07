"""Read-only local ReAct experiment; untrusted plans can only refine clarification.

Capabilities are closures over the approved catalog, NOT a general dispatcher.
No network/DB/MCP tool exists. A failed run returns the original policy untouched.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import APIError
from app.rag.catalog_policy import is_serving_candidate
from app.rag.g5.routing_evidence import rank_candidates
from app.rag.g5.topic_gate import TopicDecision

# Inherit the runtime's INFO-enabled chat logger; root is WARNING-only.
logger = logging.getLogger("backend.chat.react")


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["search_catalog", "inspect_procedure", "finish"]
    query: str = Field(max_length=200)
    procedure_ids: list[str] = Field(max_length=4)


@dataclass(frozen=True)
class AgentResult:
    topic: TopicDecision
    outcome: str
    actions: tuple[str, ...]
    latency_ms: int


async def run_react(
    data, query, previous_id, topic, planner, *, max_steps=4, timeout=15, dictionary=None
):
    """At most search -> inspect -> inspect -> finish; no legal answer generation."""
    if topic.action not in {"CLARIFY", "DEFER"}:
        return AgentResult(topic, "skipped_resolved", (), 0)
    if topic.reason.startswith("dictionary_") and topic.reason != "dictionary_weak_anchor":
        return AgentResult(topic, "skipped_policy_boundary", (), 0)
    started = perf_counter()
    actions, observations, visited = [], [], set()
    seen, inspected = set(), set()
    catalog = {
        pid: p
        for pid, p in data.procedures.items()
        if pid in data.accepted and is_serving_candidate(pid)
    }

    def result(outcome, selected=topic):
        return AgentResult(
            selected, outcome, tuple(actions), round((perf_counter() - started) * 1000)
        )

    try:
        async with asyncio.timeout(timeout):
            for step in range(min(max_steps, 4)):
                context = json.dumps(
                    {
                        "user_message": query[:4000],
                        "previous_topic": catalog.get(previous_id, {}).get("title"),
                        "remaining_steps": min(max_steps, 4) - step,
                        "observations": observations,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                action = Action.model_validate(await planner(context))
                signature = action.model_dump_json()
                if signature in visited:
                    return result("repeated_action")
                visited.add(signature)
                actions.append(action.action)
                if action.action == "search_catalog":
                    if not action.query.strip() or action.procedure_ids:
                        return result("invalid_arguments")
                    ids = [
                        c.procedure_id
                        for c in rank_candidates(data, action.query)
                        if c.procedure_id in catalog
                    ][:4]
                    if dictionary is not None:
                        found = dictionary.match(action.query, previous_id)
                        if found.action != "NONE":
                            ids = [pid for pid in found.ids if pid in catalog]
                    seen.update(ids)
                    observation = [{"id": pid, "title": catalog[pid]["title"]} for pid in ids]
                elif action.action == "inspect_procedure":
                    if action.query or len(action.procedure_ids) != 1:
                        return result("invalid_arguments")
                    pid = action.procedure_ids[0]
                    if pid not in seen or pid not in catalog:
                        return result("unobserved_id")
                    inspected.add(pid)
                    p = catalog[pid]
                    # Schema/qualifications, not whole private documents or user messages.
                    observation = {
                        "id": pid,
                        "title": p["title"],
                        "available_fields": sorted(p.get("field_evidence", {})),
                        "aliases": p.get("aliases", [])[:4],
                    }
                else:
                    ids = tuple(dict.fromkeys(action.procedure_ids))
                    if action.query or any(pid not in inspected for pid in ids):
                        return result("unobserved_id")
                    if not ids:
                        return result("abstained")
                    # A rewrite is a hypothesis, not user evidence. Never silently switch.
                    return result(
                        "clarification_proposed",
                        TopicDecision(
                            "CLARIFY", reason="react_local_suggestion", candidate_ids=ids
                        ),
                    )
                observations.append({"action": action.model_dump(), "result": observation})
            return result("step_limit")
    except TimeoutError:
        return result("timeout")
    except (APIError, ValueError, ValidationError, KeyError, TypeError):
        return result("invalid_or_unavailable")


async def maybe_refine_topic(
    ai, data, query, previous_id, topic, request_id, settings, *, dictionary=None
):
    if settings.g6_react_mode == "off":
        return topic

    async def planner(context):
        return await ai.plan_react_action(request_id=request_id, context=context)

    result = await run_react(
        data,
        query,
        previous_id,
        topic,
        planner,
        max_steps=settings.g6_react_max_steps,
        timeout=settings.g6_react_timeout_seconds,
        dictionary=dictionary,
    )
    logger.info(
        "react_ablation request_id=%s mode=%s outcome=%s actions=%s latency_ms=%s",
        request_id,
        settings.g6_react_mode,
        result.outcome,
        ",".join(result.actions),
        result.latency_ms,
    )
    return result.topic if settings.g6_react_mode == "assist" else topic
