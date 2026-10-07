"""G4 conservative claim guard, not a semantic/legal correctness classifier.

The plan is built ONLY from server-retrieved evidence. Full source text is an
atomic claim: conditions are not parsed away. Even accepted model output is
rendered by the backend. An unknown paraphrase fails closed, not "false".
"""

import re
from typing import Literal

from pydantic import Field, model_validator

from app.rag.answer_focus import document_excerpt
from app.rag.g3.validator import SECTIONS
from app.rag.retrieval import normalize
from app.rag.field_policy import PAUSED_FIELDS
from app.rag.g4.intent import asks_all_fields
from app.rag.schemas import ID, EvidenceBundle, GroundedAnswer, GroundedText, Strict

PLAN_VERSION = "g4-answer-plan-v1"
LABELS = {
    "jurisdiction": "địa phương áp dụng",
    "required_documents": "Thành phần hồ sơ",
    "fees": "Lệ phí",
    "processing_times": "Thời gian giải quyết",
    "receiving_authority": "Nơi tiếp nhận hồ sơ",
    "submission_methods": "Hình thức nộp",
    "steps": "Trình tự thực hiện",
    "legal_bases": "Căn cứ pháp lý",
    "applicant_scope": "Đối tượng thực hiện",
}
PREFIX = "Mình đã đối chiếu nguồn đã duyệt. Các thông tin bạn cần như sau:"
SINGLE_FIELD_INTROS = {
    "required_documents": "Nếu bạn đang kiểm tra hồ sơ, nguồn đã duyệt yêu cầu các giấy tờ sau:",
    "fees": "Lệ phí theo nguồn đã duyệt là:",
    "processing_times": "Thời gian giải quyết theo nguồn đã duyệt là:",
    "receiving_authority": "Nơi tiếp nhận hồ sơ theo nguồn đã duyệt là:",
    "submission_methods": "Hình thức nộp theo nguồn đã duyệt là:",
    "steps": "Trình tự thực hiện theo nguồn đã duyệt gồm:",
    "legal_bases": "Căn cứ pháp lý theo nguồn đã duyệt gồm:",
    "applicant_scope": "Đối tượng thực hiện theo nguồn đã duyệt là:",
}


def display_claim_value(field: str, value: str) -> str:
    """Expand source shorthand without changing the underlying evidence or exceptions."""
    if field == "receiving_authority":
        value = re.sub(
            r"\bTTPVHCC\b",
            "TTPVHCC (Trung tâm Phục vụ Hành chính công)",
            value,
            flags=re.IGNORECASE,
        )
        return re.sub(r"hành ch1inh", "hành chính", value, flags=re.IGNORECASE)
    if field == "fees":
        simple = " ".join(value.casefold().strip(" .;:\n\t").split())
        if simple in {
            "0",
            "0.0",
            "0,0",
            "0 đồng",
            "0đ",
            "không",
            "không đồng",
            "không có",
            "không thu phí",
        }:
            return "Không thu phí"
    return value


DOCUMENT_MENTIONS = (
    (
        ("cccd", "can cuoc cong dan", "cmnd", "chung minh nhan dan", "ho chieu"),
        "Giấy tờ tùy thân bạn nêu",
    ),
    (("giay chung sinh",), "Giấy chứng sinh bạn nêu"),
    (("giay chung nhan ket hon",), "Giấy chứng nhận kết hôn bạn nêu"),
    (
        ("giay to chung minh thong tin cu tru", "thong bao dang ky tam tru"),
        "Giấy tờ cư trú bạn nêu",
    ),
)


class PlannedClaim(Strict):
    field: Literal[
        "required_documents",
        "fees",
        "processing_times",
        "receiving_authority",
        "submission_methods",
        "steps",
        "legal_bases",
        "applicant_scope",
    ]
    value: GroundedText
    # None means not separately parsed; all conditions remain in value verbatim.
    conditions: None = None
    evidence_ids: list[ID] = Field(min_length=1, max_length=8)


class AnswerPlan(Strict):
    version: Literal["g4-answer-plan-v1"] = PLAN_VERSION
    requested_fields: list[str] = Field(max_length=8)
    claims: list[PlannedClaim] = Field(max_length=8)
    status: Literal["ANSWER", "NEED_CLARIFICATION", "INSUFFICIENT_DATA"]
    missing_fields: list[str] = Field(max_length=8)
    reason: str | None = None

    @model_validator(mode="after")
    def valid_plan(self):
        if len(self.requested_fields) != len(set(self.requested_fields)):
            raise ValueError("DUPLICATE_PLAN_FIELD")
        if any(field not in SECTIONS for field in self.requested_fields):
            raise ValueError("UNKNOWN_PLAN_FIELD")
        ids = [fid for claim in self.claims for fid in claim.evidence_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("DUPLICATE_PLAN_EVIDENCE")
        if self.status == "ANSWER":
            if (
                not self.claims
                or self.missing_fields
                or self.reason
                or {c.field for c in self.claims} != set(self.requested_fields)
            ):
                raise ValueError("INCOMPLETE_ANSWER_PLAN")
        elif not self.missing_fields or not self.reason:
            raise ValueError("INVALID_ABSTENTION_PLAN")
        elif self.claims and (
            self.status != "INSUFFICIENT_DATA"
            or {c.field for c in self.claims} & set(self.missing_fields)
            or {c.field for c in self.claims} | set(self.missing_fields)
            != set(self.requested_fields)
        ):
            raise ValueError("INVALID_PARTIAL_ANSWER_PLAN")
        return self


def requested_fields(query: str, *, procedure_titles=()) -> list[str]:
    """Small guard adapter until Minh supplies explicit multi-intent QueryContext.

    This does NOT change retrieval or infer a procedure from conversation history.
    Prefer an abstention to accepting document evidence for a location question.
    """
    q = " " + normalize(query) + " "
    # "chi phí" / "thời hạn" can be part of a procedure name, not an asked field.
    # Only strip exact, whole, server-owned title spans; never strip loose keywords.
    titles = set()
    for title in procedure_titles:
        normalized = normalize(title)
        titles.add(normalized)
        for prefix in ("thu tuc hanh chinh ", "thu tuc "):
            if normalized.startswith(prefix):
                titles.add(normalized.removeprefix(prefix))
    for title in sorted(titles, key=len, reverse=True):
        if len(title.split()) >= 2:
            q = q.replace(" " + title + " ", " ")

    def has(*phrases):
        return any(" " + phrase + " " in q for phrase in phrases)

    place = has("o dau", "noi nop", "noi tiep nhan", "dia diem", "co quan tiep nhan")
    docs = has("giay to", "thanh phan ho so", "chuan bi", "ho so gom", "ho so can")
    docs = docs or (has("ho so") and not place)
    flags = {
        "required_documents": docs,
        "fees": has("le phi", "phi", "bao nhieu tien", "fees"),
        "processing_times": has("thoi gian", "bao lau", "may ngay", "thoi han"),
        "receiving_authority": place,
        "submission_methods": has("hinh thuc", "truc tuyen", "online", "nop cach nao"),
        "steps": has("trinh tu", "cac buoc", "thuc hien the nao", "thuc hien nhu the nao"),
        "legal_bases": has("can cu phap ly"),
        "applicant_scope": has("doi tuong", "ai duoc"),
    }
    return [field for field, matched in flags.items() if matched]


def render_plan(plan: AnswerPlan, *, query: str = "", submission_methods=()) -> str:
    if plan.status == "NEED_CLARIFICATION":
        return "Bạn muốn biết hồ sơ, lệ phí, thời gian hay nơi nộp của thủ tục này?"
    if plan.status != "ANSWER":
        labels = ", ".join(
            "Thời hạn phải thông báo/nộp trước khi thực hiện"
            if field == "steps"
            and re.search(r"\b(?:khi nao phai|han (?:bao|nop))\b", normalize(query))
            else LABELS.get(field, field)
            for field in plan.missing_fields
        )
        if plan.claims:
            complete_part = AnswerPlan(
                requested_fields=list(dict.fromkeys(c.field for c in plan.claims)),
                claims=plan.claims,
                status="ANSWER",
                missing_fields=[],
            )
            answer = render_plan(complete_part, query=query, submission_methods=submission_methods)
            # Overview requests show available data without a missing-field footer.
            # Explicit questions still get a short response under the requested
            # supported field so a missing fee/location is not silently skipped.
            if not asks_all_fields(query):
                for field in plan.missing_fields:
                    if field not in PAUSED_FIELDS:
                        answer += f"\n\n{LABELS.get(field, field)}:\nChưa có thông tin trong nguồn hiện có."
            return answer
        suffix = f" ({labels})" if labels else ""
        return f"Chưa đủ căn cứ từ nguồn phù hợp để trả lời đầy đủ{suffix}."
    if len(plan.claims) == 1:
        claim = plan.claims[0]
        normalized_query = normalize(query)
        normalized_value = normalize(claim.value)
        if claim.field == "required_documents":
            excerpt = document_excerpt(claim.value, query)
            if excerpt:
                return "Phần hồ sơ liên quan đến câu hỏi của bạn theo nguồn đã duyệt:\n" + excerpt
            for phrases, label in DOCUMENT_MENTIONS:
                if any(phrase in normalized_query for phrase in phrases) and any(
                    phrase in normalized_value for phrase in phrases
                ):
                    return (
                        f"{label} có trong thành phần hồ sơ theo nguồn đã duyệt. "
                        "Bạn vẫn cần đối chiếu các điều kiện và giấy tờ còn lại "
                        "trong danh sách đầy đủ dưới đây:\n" + claim.value
                    )
        value = display_claim_value(claim.field, claim.value)
        if (
            claim.field == "submission_methods"
            and normalize(value) == "ca hai"
            and set(submission_methods) == {"DIRECT", "ONLINE"}
        ):
            value = "Trực tiếp và trực tuyến (nguồn ghi: “Cả hai”)."
        return f"{SINGLE_FIELD_INTROS[claim.field]}\n{value}"
    normalized_query = normalize(query)
    claim_by_field = {claim.field: claim for claim in plan.claims}
    online_original_question = (
        set(claim_by_field) == {"required_documents", "submission_methods"}
        and any(term in normalized_query for term in ("onl", "online", "truc tuyen", "qua mang"))
        and any(term in normalized_query for term in ("ban chinh", "doi chieu", "xuat trinh"))
    )
    if online_original_question:
        submission = claim_by_field["submission_methods"]
        documents = claim_by_field["required_documents"]
        return (
            "Theo nguồn đã duyệt:\n\n"
            f"{LABELS[submission.field]}:\n"
            f"{display_claim_value(submission.field, submission.value)}\n\n"
            f"{LABELS[documents.field]}:\n{documents.value}\n\n"
            "Nguồn hiện chỉ xác nhận hai nội dung trên, chưa nêu rõ thời điểm hoặc cách "
            "đối chiếu bản chính đối với hồ sơ nộp trực tuyến. Vì vậy chưa đủ căn cứ để "
            "khẳng định bạn phải mang bản chính ngay khi nộp trực tuyến."
        )
    return (
        PREFIX
        + "\n\n"
        + "\n\n".join(
            f"{LABELS[claim.field]}:\n"
            + (
                (document_excerpt(claim.value, query) or claim.value)
                if claim.field == "required_documents"
                else display_claim_value(claim.field, claim.value)
            )
            for claim in plan.claims
        )
    )


def build_answer_plan(
    bundle: EvidenceBundle,
    *,
    fields: list[str] | None = None,
    field_evidence: dict[str, list[str]] | None = None,
) -> AnswerPlan:
    fields = list(
        dict.fromkeys(
            requested_fields(bundle.query, procedure_titles=[e.title for e in bundle.evidence])
            if fields is None
            else fields
        )
    )
    if any(field not in SECTIONS for field in fields):
        raise ValueError("UNKNOWN_PLAN_FIELD")
    if not fields:
        return AnswerPlan(
            requested_fields=[],
            claims=[],
            status="NEED_CLARIFICATION",
            missing_fields=["field_intents"],
            reason="INTENT_UNRESOLVED",
        )
    claims, missing = [], []
    procedure_ids = {item.procedure_id for item in bundle.evidence}
    for field in fields:
        items = [item for item in bundle.evidence if item.section_type == SECTIONS[field]]
        available = {item.fragment_id for item in items}
        # Without the authoritative field manifest, completeness cannot be established.
        required = set(field_evidence.get(field, [])) if field_evidence is not None else set()
        invalid_date = any(
            item.validity_status in {"EXPIRED", "FUTURE"}
            or (
                bundle.as_of
                and (
                    item.validity_status != "CURRENT"
                    or not item.effective_from
                    or bundle.as_of < item.effective_from
                    or (item.effective_to and bundle.as_of > item.effective_to)
                )
            )
            for item in items
        )
        if len(procedure_ids) != 1 or not required or available != required or invalid_date:
            missing.append(field)
            continue
        # Preserve the reviewed field manifest order, not BM25/lexicographic order.
        by_id = {item.fragment_id: item for item in items}
        for fid in field_evidence[field]:
            item = by_id[fid]
            claims.append(
                PlannedClaim(field=field, value=item.text, evidence_ids=[item.fragment_id])
            )
    plan = AnswerPlan(
        requested_fields=fields,
        claims=claims,
        status="INSUFFICIENT_DATA" if missing else "ANSWER",
        missing_fields=missing,
        reason="FIELD_EVIDENCE_INCOMPLETE" if missing else None,
    )
    if len(render_plan(plan)) > 8000:
        # Never truncate a fee exception, time qualification, or tail fragment.
        return AnswerPlan(
            requested_fields=fields,
            claims=[],
            status="INSUFFICIENT_DATA",
            missing_fields=fields,
            reason="ANSWER_BUDGET_EXCEEDED",
        )
    return plan


def plan_citations(plan: AnswerPlan) -> list[str]:
    return list(dict.fromkeys(fid for claim in plan.claims for fid in claim.evidence_ids))


def verify_candidate(plan: AnswerPlan, answer: GroundedAnswer) -> list[str]:
    """Return fixed reason codes only. Binding/contract checks must run first.

    Exact full-claim matching is intentionally stricter than semantic entailment.
    Whitespace can differ; words, numbers, punctuation and conditions cannot.
    """
    if plan.status != "ANSWER":
        return ["PLAN_NOT_ANSWERABLE"]
    errors = []
    if answer.status != "ANSWER":
        errors.append("CANDIDATE_STATUS_MISMATCH")
    if set(answer.cited_fragment_ids) != set(plan_citations(plan)):
        errors.append("CLAIM_CITATIONS_INCOMPLETE")
    values = [claim.value for claim in plan.claims]
    alternatives = [render_plan(plan), "\n".join(values), "\n".join("- " + v for v in values)]
    # Exact answer form used by the reviewed full-corpus SFT bundle. Claim text
    # remains verbatim; status and citation binding are checked above.
    trained_prefix = "Theo ngu\u1ed3n \u0111\u00e3 duy\u1ec7t:"
    alternatives.append(trained_prefix + "\n" + "\n".join("- " + v for v in values))
    alternatives.append(
        "Theo nguồn đã duyệt (chế độ trích xuất an toàn):\n" + "\n".join("- " + v for v in values)
    )

    def compact(text):
        return " ".join(text.split())

    if compact(answer.answer) not in {compact(text) for text in alternatives}:
        errors.append("FULL_CLAIM_TEXT_NOT_VERIFIED")
    return errors
