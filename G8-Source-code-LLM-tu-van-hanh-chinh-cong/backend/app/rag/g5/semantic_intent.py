"""Use Qwen + independent embeddings for bounded procedure confirmation.

No lexical overlap is required here. This cannot bypass dictionary boundaries,
select an unknown procedure, or answer administrative facts before confirmation.
"""
import logging

from app.rag.catalog_policy import is_serving_candidate
from app.rag.g5.dictionary import focus_query
from app.rag.g5.topic_gate import TopicDecision, _accent_conflict

logger = logging.getLogger("backend.semantic_intent")
UNRESOLVED_REASONS = {"needs_model_interpretation", "new_procedure_unresolved"}


async def confirm_semantic_topic(data, query, output, topic, router):
    if (router is None or topic.action not in {"DEFER", "CLARIFY"}
            or topic.reason not in UNRESOLVED_REASONS
            or output.topic_action != "SWITCH" or output.patch.procedure_id.op != "SET"
            or output.dialogue_act in {"GREET", "OUT_OF_SCOPE", "RESET_CONTEXT"}):
        return topic
    proposed = output.patch.procedure_id.value
    if proposed not in data.accepted or not is_serving_candidate(proposed):
        return topic
    if _accent_conflict(query, data.procedures[proposed]["title"]):
        return topic
    focused, changed = focus_query(query)
    # Model evidence may reference a rejected clause. Leave that to explicit
    # correction rules until semantic negation is evaluated independently.
    if changed or not focused.strip():
        return topic
    candidates = await router.semantic_candidates(data, focused)
    if not candidates:
        return topic
    first = candidates[0]
    margin = first.dense_score - candidates[1].dense_score if len(candidates) > 1 else 1.0
    # Engineering thresholds for a suggestion, NOT calibrated probabilities.
    agreed = first.procedure_id == proposed and first.dense_score >= 0.55 and margin >= 0.01
    logger.info("semantic_confirmation model=%s dense=%s score=%.3f margin=%.3f agreed=%s",
                proposed, first.procedure_id, first.dense_score, margin, agreed)
    if not agreed:
        return topic
    title = data.procedures[proposed]["title"]
    return TopicDecision(
        "CLARIFY", reason="semantic_confirmation", candidate_ids=(proposed,),
        clarification_hint=f"Bạn đang hỏi về thủ tục: {title}. Mình hiểu như vậy đúng không?",
    )


async def suggest_semantic_options(data, query, output, router):
    """Resolve agreement/disagreement as a question, never as an implicit SET.

    Unlike the legacy helper, disagreement is made visible to the citizen rather
    than discarding all useful candidates. Thresholds are not probabilities.
    """
    unresolved = TopicDecision("CLARIFY", reason="orchestrated_unresolved")
    focused, changed = focus_query(query)
    if router is None or changed or not focused.strip():
        return unresolved
    candidates = [candidate for candidate in await router.semantic_candidates(data, focused)
                  if candidate.procedure_id in data.accepted
                  and is_serving_candidate(candidate.procedure_id)
                  and not _accent_conflict(query, data.procedures[candidate.procedure_id]["title"])]
    if not candidates or candidates[0].dense_score < .55:
        return unresolved
    first = candidates[0]
    margin = first.dense_score - candidates[1].dense_score if len(candidates) > 1 else 1.0
    proposed = output.patch.procedure_id.value if output.patch.procedure_id.op == "SET" else None
    if (proposed == first.procedure_id and margin >= .01) or (first.dense_score >= .60 and margin >= .03):
        title = data.procedures[first.procedure_id]["title"]
        return TopicDecision("CLARIFY", reason="semantic_confirmation", candidate_ids=(first.procedure_id,),
            clarification_hint=f"Bạn đang hỏi về thủ tục: {title}. Mình hiểu như vậy đúng không?")
    options = tuple(c.procedure_id for c in candidates[:3]
                    if c.dense_score >= .55 and first.dense_score - c.dense_score <= .06)
    if len(options) < 2:
        return unresolved
    return TopicDecision("CLARIFY", reason="orchestrated_semantic_disagreement", candidate_ids=options,
        clarification_hint="Có vài thủ tục gần với nhu cầu này; mình cần bạn xác nhận để không lấy nhầm nguồn.")
