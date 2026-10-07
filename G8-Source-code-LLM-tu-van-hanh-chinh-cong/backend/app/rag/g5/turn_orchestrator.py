"""Single arbitration point: detectors propose, this module commits.

Strong catalog evidence retains the direct fast path. Weak lexical evidence
cannot win by running before the model. Reducers receive a sealed decision.
"""
from dataclasses import dataclass
import logging
import re

from pydantic import ValidationError
from app.errors import APIError
from app.rag.field_policy import DATASET_FIELDS, serving_fields
from app.rag.g3.retrieval import fields_for_query
from app.rag.g5.intent_context import (
    FieldPatch, PatchEvidence, StringPatch, can_skip_intent_model, model_state,
    procedure_candidates, safe_intent_output, select_pending_procedure,
)
from app.rag.g5.routing_evidence import explicit_catalog_subject, field_only_followup, rank_candidates
from app.rag.g5.dictionary import focus_query
from app.rag.g5.discourse import catalog_hint, contextual_action_query, excluded_fields, inclusive_fields, replacement_query, explicit_loss_report
from app.rag.g5.semantic_intent import suggest_semantic_options
from app.rag.g5.topic_gate import NEW_PROCEDURE_REQUEST, TopicDecision, decide_topic
from app.rag.retrieval import normalize

logger = logging.getLogger("backend.chat")
STRONG_REASONS = {
    "explicit_catalog_name", "current_procedure_explicit", "dictionary_evidence",
    "clarification_selection", "field_only_followup", "document_inventory",
    "evidence_followup", "contextual_followup",
    "orchestrated_catalog_correction",
    "orchestrated_contextual_action",
}
WEAK_REASONS = {
    "needs_model_interpretation", "new_procedure_unresolved", "catalog_lexical_evidence",
    "current_turn_catalog_match", "hybrid_catalog_agreement", "hybrid_semantic_evidence",
    "semantic_current_procedure", "dictionary_weak_anchor", "hybrid_high_confidence",
}
# Capability boundary, not a procedure classifier. Do not let a model's stale
# KEEP proposal answer an unrelated creative-generation command with old facts.
UNSUPPORTED_GENERATION = re.compile(
    r"^(?:(?:ban|hay|giup|minh|toi|em) )*(?:viet|sang tac|ke|lap trinh)\b"
    r".{0,80}\b(?:bai tho|bai hat|truyen cuoi|doan code|chuong trinh)\b"
)
RESET_COMMAND = re.compile(
    r"^(?:(?:ban|hay|giup|cho|toi|minh) )*(?:bat dau lai|lam lai tu dau|"
    r"bo noi dung cu|xoa (?:boi canh|ngu canh)|khoi tao lai|"
    r"reset (?:cuoc hoi thoai|ngu canh|boi canh))(?: (?:nhe|di|voi|a))*$"
)
NEW_SUBJECT_REQUEST = re.compile(r"\b(?:muon|can|dinh)\s+(?:mo|xin|dang ky)\b")


@dataclass(frozen=True)
class ResolvedTurn:
    topic: TopicDecision
    output: object
    fields: tuple[str, ...] | None
    intent_path: str


def model_followup_fields(output, previous):
    """Interpret fields within an established topic, not by lexical paraphrases.

    Called only after strong current-turn switches and boundaries are handled.
    A model cannot name new fields or turn an unconfirmed candidate into state.
    """
    if not previous.procedure_id or output.topic_action != "KEEP":
        return None
    if output.dialogue_act not in {"ASK_INFORMATION", "PROVIDE_CLARIFICATION"}:
        return None
    if output.patch.procedure_id.op != "KEEP" or output.patch.field_intents.op != "SET":
        return None
    fields = set(output.patch.field_intents.value or []) & DATASET_FIELDS
    return tuple(sorted(fields)) if fields else None


def seal_output(data, query, message_id, output, topic, fields):
    sealed = output.model_copy(deep=True)
    evidence = [PatchEvidence(message_id=message_id, quote=query[:300])]
    if topic.action in {"SWITCH", "KEEP"}:
        sealed.dialogue_act = "ASK_INFORMATION"
        sealed.topic_action = topic.action
        sealed.patch.procedure_id = (StringPatch(op="SET", value=topic.procedure_id, evidence=evidence)
                                    if topic.action == "SWITCH" else StringPatch(op="KEEP"))
    elif topic.action == "CLARIFY":
        sealed = safe_intent_output(data, query, message_id)
    if fields:
        sealed.patch.field_intents = FieldPatch(op="SET", value=list(fields), evidence=evidence)
    return sealed


async def resolve_turn(*, data, query, previous, ai, dictionary, settings,
                       request_id, message_id, previous_user="", previous_assistant=""):
    if explicit_loss_report(query):
        # This catalog has no cash-loss reporting procedure. Never answer this
        # explicit incident using a stale civil-registration/fee procedure.
        output = safe_intent_output(data, query, str(message_id))
        return ResolvedTurn(TopicDecision("CLARIFY", reason="unsupported_loss_report"),
                            output, None, "orchestrator_fast_path")
    if RESET_COMMAND.fullmatch(normalize(query)):
        output = safe_intent_output(data, query, str(message_id))
        output.dialogue_act = "RESET_CONTEXT"
        output.topic_action = "RESET"
        output.patch.procedure_id = StringPatch(op="KEEP")
        output.patch.field_intents = FieldPatch(op="KEEP")
        return ResolvedTurn(TopicDecision("DEFER", reason="orchestrated_explicit_reset"),
                            output, None, "orchestrator_fast_path")
    if UNSUPPORTED_GENERATION.search(normalize(query)):
        output = safe_intent_output(data, query, str(message_id))
        output.dialogue_act = "OUT_OF_SCOPE"
        output.topic_action = "KEEP"
        output.patch.procedure_id = StringPatch(op="KEEP")
        output.patch.field_intents = FieldPatch(op="KEEP")
        return ResolvedTurn(TopicDecision("DEFER", reason="orchestrated_capability_boundary"),
                            output, None, "orchestrator_fast_path")
    # No hybrid here: lexical proposals and independent dense evidence must not
    # vote twice, or consume GPU for an already resolved turn.
    routing_query, abandoned = replacement_query(query)
    proposal = await decide_topic(data, routing_query, None if abandoned else previous.procedure_id, dictionary=dictionary)
    hint = catalog_hint(data, routing_query)
    subject = explicit_catalog_subject(data, routing_query)
    if subject:
        hint = TopicDecision("SWITCH", subject, "explicit_catalog_name")
    contextual_query = contextual_action_query(data, query, previous) if not abandoned else None
    if hint and (hint.reason != "semantic_confirmation" or proposal.action == "DEFER"
                 or proposal.reason in WEAK_REASONS):
        proposal = hint
    elif contextual_query:
        action = await decide_topic(data, contextual_query, None, dictionary=dictionary)
        if action.action == "SWITCH":
            proposal = TopicDecision("SWITCH", action.procedure_id, "orchestrated_contextual_action")
    if re.search(r"\b(?:tat ca|toan bo|nhieu) (?:cac )?thu tuc\b(?! nay)", normalize(query)):
        proposal = TopicDecision("CLARIFY", reason="multiple_procedures",
            clarification_hint="Bạn muốn xem thủ tục cụ thể nào? Mình cần phân biệt từng thủ tục để trả đúng nguồn.")
    selected = select_pending_procedure(previous, query, dictionary=dictionary, topic=proposal)
    if selected:
        proposal = TopicDecision("SWITCH", selected, "clarification_selection")
    elif previous.procedure_id and normalize(query) in {"co", "ok", "oke", "vang", "cam on"}:
        # Consent and pending-procedure confirmation are handled before this.
        # A bare acknowledgement is not permission to repeat old factual fields.
        output = safe_intent_output(data, query, str(message_id))
        return ResolvedTurn(TopicDecision("KEEP", reason="contextual_followup"),
                            seal_output(data, query, str(message_id), output,
                                TopicDecision("KEEP", reason="contextual_followup"), ()),
                            (), "orchestrator_fast_path")
    ranked = rank_candidates(data, query) if proposal.reason == "catalog_lexical_evidence" else []
    distinctive_catalog = bool(ranked and len(ranked[0].matched) >= 2 and ranked[0].coverage >= .85
        and (len(ranked) == 1 or ranked[0].score - ranked[1].score >= .12))
    _, has_explicit_correction = focus_query(query)
    if distinctive_catalog and has_explicit_correction and proposal.action == "SWITCH":
        # The rejected clause has been excluded from current-turn evidence.
        # A named replacement is a SWITCH, not a model-inferred global RESET.
        proposal = TopicDecision("SWITCH", proposal.procedure_id, "orchestrated_catalog_correction")
    explicit_fields = tuple(sorted(serving_fields(fields_for_query(data, query), data.version)))
    strong = proposal.reason in STRONG_REASONS
    protected = proposal.action == "CLARIFY" and proposal.reason not in WEAK_REASONS
    excludes_information = bool(re.search(r"\b(?:khong|ko|dung) (?:can|muon|hoi|xem|lay)\b", normalize(query)))
    fast = protected or bool(subject) or proposal.reason in {"orchestrated_catalog_correction", "orchestrated_contextual_action"} or (strong and (
        can_skip_intent_model(query, proposal)
        or (proposal.reason == "field_only_followup" and field_only_followup(query) and not excludes_information)
    ))
    path = "orchestrator_fast_path"
    output = safe_intent_output(data, query, str(message_id))
    if not fast:
        try:
            output = await ai.analyze_intent_context(
                request_id=request_id, current_message_id=message_id, current_user_message=query,
                state_before=model_state(previous), procedure_candidates=procedure_candidates(data),
                data_classification="D2_COMPANY_REAL", proposal_only=True,
                previous_user_message=previous_user, previous_assistant_message=previous_assistant,
            )
            path = "orchestrator_model_proposal"
        except (APIError, ValueError, ValidationError):
            if not settings.g5_intent_context_fallback_enabled:
                raise
            path = "orchestrator_safe_fallback"
    final = proposal
    resolved_fields = explicit_fields or None
    router = ai.hybrid_router if settings.g5_hybrid_enabled else None
    # Model classification + distinctive catalog evidence can go direct without
    # dense search. This preserves clear existing requests (e.g. business pause)
    # while incidental token hits and disagreements still require clarification.
    catalog_agreement = bool(
        distinctive_catalog and explicit_fields and path == "orchestrator_model_proposal"
        and output.topic_action == "SWITCH" and output.patch.procedure_id.op == "SET"
        and output.patch.procedure_id.value == proposal.procedure_id == ranked[0].procedure_id
    )
    if catalog_agreement:
        final = TopicDecision("SWITCH", proposal.procedure_id, "orchestrated_model_catalog_agreement")
    if strong and proposal.action == "KEEP" and path == "orchestrator_model_proposal":
        contextual_fields = model_followup_fields(output, previous)
        if contextual_fields:
            resolved_fields = contextual_fields
    if not strong and not protected and not catalog_agreement:
        followup = model_followup_fields(output, previous) if path == "orchestrator_model_proposal" else None
        if followup and (abandoned or proposal.reason == "new_procedure_unresolved"
                         or NEW_PROCEDURE_REQUEST.search(normalize(query))
                         or (NEW_SUBJECT_REQUEST.search(normalize(query)) and not field_only_followup(query))):
            # Explicitly requesting a new procedure is not evidence that the
            # previous procedure still applies, even if the model says KEEP.
            followup = None
        if followup and proposal.action == "SWITCH" and proposal.procedure_id != previous.procedure_id:
            # Model KEEP versus lexical new-topic: ask if independent semantic
            # evidence supports the new topic; never silently choose either.
            candidates = await router.semantic_candidates(data, query) if router else []
            if candidates and candidates[0].procedure_id == proposal.procedure_id and candidates[0].dense_score >= .55:
                followup = None
        if followup:
            final = TopicDecision("KEEP", reason="orchestrated_semantic_followup")
            resolved_fields = followup
        elif output.dialogue_act in {"RESET_CONTEXT", "GREET", "OUT_OF_SCOPE"}:
            final = TopicDecision("DEFER", reason="orchestrated_dialogue")
        else:
            final = await suggest_semantic_options(data, routing_query, output, router)
    if final.reason == "orchestrated_contextual_action":
        resolved_fields = explicit_fields or tuple(previous.last_answered_fields or previous.field_intents)
    if final.action in {"SWITCH", "KEEP"} and resolved_fields is None:
        if final.reason == "clarification_selection" and previous.field_intents:
            resolved_fields = tuple(previous.field_intents)
        elif final.reason in {"explicit_catalog_name", "dictionary_evidence"} and final.action == "SWITCH":
            resolved_fields = ()
    # Explicitly excluded fields cannot be reintroduced by a model proposal or
    # carried over from the previous answer. Apply only on the resolved topic.
    removed = excluded_fields(query)
    included = inclusive_fields(query)
    if included and resolved_fields and final.action in {"KEEP", "SWITCH"}:
        resolved_fields = tuple(sorted(set(resolved_fields) | (included & DATASET_FIELDS)))
    if removed and final.action in {"KEEP", "SWITCH"}:
        requested = set(explicit_fields) - removed
        if not requested:
            requested = (set(previous.last_answered_fields or previous.field_intents)
                         if final.action == "KEEP" else set(resolved_fields or ())) - removed
        resolved_fields = tuple(sorted(requested))
    sealed = seal_output(data, query, str(message_id), output, final, resolved_fields)
    logger.info("turn_arbitration request_id=%s proposal=%s final=%s reason=%s path=%s",
                request_id, proposal.reason, final.action, final.reason, path)
    return ResolvedTurn(final, sealed, resolved_fields, path)
