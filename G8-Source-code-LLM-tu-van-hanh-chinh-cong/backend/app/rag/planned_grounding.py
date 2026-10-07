"""Opt-in G4 orchestration. Existing G1/G2/G3 generation stays unchanged."""

import logging

from app.config import get_settings
from app.errors import APIError
from app.rag.answer_plan import (
    LABELS,
    build_answer_plan,
    display_claim_value,
    plan_citations,
    render_plan,
    verify_candidate,
)
from app.rag.schemas import ChecklistItem, Citation, Grounding, VerificationResult

logger = logging.getLogger("g1.requests")


async def answer_with_plan(client, bundle, data, *, fields=None, field_evidence=None):
    # Local import avoids the orchestration import cycle.
    from app.rag.grounding import generate_grounded, validate_answer

    pids = {item.procedure_id for item in bundle.evidence}
    procedure = data.procedures.get(next(iter(pids)), {}) if len(pids) == 1 else {}
    plan = build_answer_plan(
        bundle,
        fields=fields,
        field_evidence=field_evidence or procedure.get("field_evidence", {}),
    )
    checked, reasons, provider, model = False, [], "answer-plan", "deterministic-v1"
    audit_enabled = get_settings().g6_candidate_audit_enabled
    if plan.status == "ANSWER" and audit_enabled:
        # The model receives only the fields authorized by the server-owned plan.
        selected = set(plan_citations(plan))
        scoped_bundle = bundle.model_copy(
            update={"evidence": [item for item in bundle.evidence if item.fragment_id in selected]}
        )
        try:
            answer = await generate_grounded(client, scoped_bundle)
            validate_answer(answer, scoped_bundle)
            checked, provider, model = True, answer.provider, answer.model
            reasons = verify_candidate(plan, answer)
        except APIError as exc:
            # No ungrounded G1 fallback. The complete plan can still be rendered.
            reasons = [exc.code]
    elif plan.status == "ANSWER":
        reasons = ["DETERMINISTIC_PLAN_RENDER"]
    else:
        reasons = [plan.reason]
    verification = VerificationResult(
        candidate_checked=checked,
        candidate_passed=checked and not reasons,
        fallback_used=plan.status == "ANSWER" and audit_enabled and bool(reasons),
        reasons=reasons,
    )
    available = {item.fragment_id: item for item in bundle.evidence}
    source_versions = {
        item.metadata.get("source_version")
        for item in bundle.evidence
        if item.metadata.get("source_version")
    }
    checked_times = [
        item.metadata.get("source_checked_at")
        for item in bundle.evidence
        if item.metadata.get("source_checked_at")
    ]
    grounding = Grounding(
        request_id=bundle.request_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        status=plan.status,
        corpus_version=bundle.corpus_version,
        data_classification=bundle.data_classification,
        provider=provider,
        model=model,
        prompt_version="g2-grounded-v2" if checked else None,
        sources=[
            Citation(
                fragment_id=fid,
                source_id=available[fid].source_id,
                title=available[fid].title,
                url=available[fid].url,
                metadata=available[fid].metadata,
            )
            for fid in plan_citations(plan)
        ],
        missing_information=plan.missing_fields,
        verification=verification,
        checklist=[
            ChecklistItem(
                field=claim.field,
                label=LABELS[claim.field],
                value=display_claim_value(claim.field, claim.value),
                evidence_ids=claim.evidence_ids,
            )
            for claim in plan.claims
        ],
        knowledge_version=(
            next(iter(source_versions)) if len(source_versions) == 1 else bundle.corpus_version
        ),
        source_checked_at=(max(checked_times) if checked_times else None),
    )
    logger.info(
        "request_id=%s stage=claim_verification status=%s checked=%s passed=%s "
        "fallback=%s reasons=%s",
        bundle.request_id,
        plan.status,
        checked,
        verification.candidate_passed,
        verification.fallback_used,
        ",".join(reasons),
    )
    return render_plan(
        plan, query=bundle.query, submission_methods=procedure.get("submission_methods", [])
    ), grounding.model_dump(mode="json")
