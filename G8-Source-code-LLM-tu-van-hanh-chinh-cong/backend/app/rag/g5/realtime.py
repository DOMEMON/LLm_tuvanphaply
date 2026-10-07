"""Backend-owned one-shot consent. No model can authorize a current lookup."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.rag.catalog_policy import (
    CURRENT_LOOKUP_NAMES,
    MISSING_RECEIVING_PROCEDURES,
    mcp_review_fields,
    needs_realtime_offer,
)
from app.rag.g4.intent import detect_fields
from app.rag.g5.realtime_contract import LookupField, LookupRequest
from app.rag.g5.source_discrepancy import (
    SCHOLARSHIP_ID,
    SCHOLARSHIP_OFFER,
    is_quarantined_scholarship_documents,
    original_record_requested,
)
from app.rag.retrieval import normalize

OFFER = "Bạn có muốn kiểm tra thông tin hiện tại trên Cổng DVC Quốc gia không?"
CONSENT_HINT = (
    'Bạn chỉ cần trả lời **Có**, mình sẽ kiểm tra ngay; '
    'hoặc **Không**, mình sẽ không kiểm tra nữa.'
)
YES = {
    "có",
    "co",
    "đồng ý",
    "dong y",
    "ok",
    "okay",
    "kiểm tra đi",
    "kiem tra di",
    "có kiểm tra đi",
    "yes",
    "vâng",
    "vang",
    "ừ",
    "ừ kiểm tra đi",
}
NO = {"không", "khong", "không cần", "khong can", "thôi", "thoi", "no"}
FIELD_LABELS = {
    "processing_times": "thời hạn giải quyết",
    "receiving_authority": "cơ quan tiếp nhận",
    "submission_methods": "cách thức nộp",
    "required_documents": "thành phần hồ sơ",
    "fees": "phí/lệ phí",
    "steps": "trình tự thực hiện",
    "legal_bases": "căn cứ pháp lý",
    "applicant_scope": "đối tượng thực hiện",
}


class PendingLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    procedure_id: str = Field(min_length=1, max_length=200)
    procedure_name: str = Field(min_length=1, max_length=500)
    field: LookupField
    jurisdiction: str | None = Field(default=None, max_length=200)
    as_of: date
    offered_at: AwareDatetime
    awaiting_jurisdiction: bool = False
    local_answer: str = Field(max_length=40000)
    local_grounding: dict | None = None
    candidate_titles: list[str] = Field(default_factory=list, max_length=5)
    selected_current_title: str | None = Field(default=None, max_length=500)
    awaiting_variant: bool = False
    remaining_fields: list[LookupField] = Field(default_factory=list, max_length=4)

    def request(self) -> LookupRequest:
        if not self.jurisdiction:
            raise ValueError("REALTIME_JURISDICTION_REQUIRED")
        return LookupRequest(
            procedure_id=self.procedure_id,
            procedure_name=(
                self.selected_current_title
                or CURRENT_LOOKUP_NAMES.get(self.procedure_id, self.procedure_name)
            ),
            field=self.field,
            jurisdiction=self.jurisdiction,
            as_of=self.as_of,
        )


def decision(text: str) -> str | None:
    clean = " ".join(re.sub(r"^[\W_]+|[\W_]+$", "", text.casefold()).split())
    if clean in NO or re.search(r"\bkhông\s+(?:cần|kiểm tra|tra cứu)\b", clean):
        return "no"
    if clean in YES:
        return "yes"
    normalized = normalize(text)
    # Consent is accepted only in the context of an existing, bound offer.
    # These are user requests to act, not questions *about* the portal.
    if re.fullmatch(
        r"(?:(?:vay )?(?:toi|minh|em|tui) muon (?:ban )?|(?:vay )?ban )?"
        r"(?:tra cuu|kiem tra) them"
        r"(?: (?:ho so|thong tin|chinh sach)(?: do| nay)?)?"
        r"(?: (?:tren|o) cong (?:dich vu cong|dvc)(?: quoc gia)?)?"
        r"(?: cho (?:toi|minh|em|tui))?(?: di| nhe| voi| thi sao)?",
        normalized,
    ):
        return "yes"
    if re.fullmatch(
        r"(?:vay )?(?:toi|minh|em|tui) muon (?:ban )?(?:tra cuu|kiem tra)"
        r"(?: (?:ho so|thong tin|chinh sach)(?: do| nay)?)?"
        r"(?: (?:tren|o) cong (?:dich vu cong|dvc)(?: quoc gia)?)?"
        r"(?: cho (?:toi|minh|em|tui))?(?: thi sao)?",
        normalized,
    ):
        return "yes"
    if re.fullmatch(
        r"(?:(?:toi|minh|em) muon (?:tra cuu|kiem tra)|"
        r"(?:ban )?(?:hay )?(?:tra cuu|kiem tra) "
        r"(?:giup (?:toi|minh)|cho (?:toi|minh)|di))",
        normalize(text),
    ):
        return "yes"
    if re.fullmatch(
        r"(?:(?:co|da co) )?(?:(?:nho|vui long) )?(?:ban )?(?:hay )?"
        r"(?:tra cuu|kiem tra|tra) (?:giup|cho|ho) (?:toi|minh|em)"
        r"(?: (?:nhe|di|voi|a))?",
        normalize(text),
    ):
        return "yes"
    # Accept a natural affirmative sentence without treating questions such as
    # "có không?" or an unrelated message containing "có" as authorization.
    if re.match(r"^(?:đồng ý|vâng|ok(?:ay)?|ừ)(?:\b|[,;:])", clean) and re.search(
        r"\b(?:kiểm tra|tra cứu|cổng d(?:ị|i)ch vụ công|cổng dvc)\b", clean
    ):
        return "yes"
    if re.match(r"^có\s*[,;:]\s*(?:hãy\s+)?(?:kiểm tra|tra cứu)\b", clean):
        return "yes"
    if re.match(r"^(?:hãy\s+)?(?:kiểm tra|tra cứu)\s+(?:đi|giúp|cho)\b", clean):
        return "yes"
    if current_lookup_command(text):
        return "yes"
    return None


def current_lookup_command(text):
    """A scoped imperative; a question about the portal is not consent."""
    clean = normalize(text)
    if re.search(r"\b(?:khong|dung|chua) (?:can |muon )?(?:kiem tra|tra cuu|tra)\b", clean):
        return False
    if not re.search(r"\b(?:cong dvc|cong dich vu cong|hien tai|moi nhat|real time)\b", clean):
        return False
    if not re.search(r"\b(?:kiem tra|tra cuu|tra)\b.*\b(?:giup|cho toi|cho minh|di)\b", clean):
        return False
    residual = re.sub(
        r"\b(?:kiem tra|tra cuu|tra|thong tin|hien tai|moi nhat|cong dvc|cong dich vu cong|"
        r"quoc gia|real time|giup|cho|toi|minh|hay|neu|muon|thi|tren|di|nhe|a|ban|vay)\b",
        " ",
        clean,
    )
    return not residual.strip()


def explicit_request(state, data, text, filters, *, answer, grounding, enabled):
    """Honor a user's own current-lookup request without requiring a prior offer.

    Bind to the last local answer. A request naming a different procedure is
    left for ordinary routing, never authorized against the old procedure.
    """
    if not enabled or not state.procedure_id or not grounding or not current_lookup_command(text):
        return None
    fields = list(state.field_intents or state.last_answered_fields)
    if len(fields) != 1 or fields[0] not in FIELD_LABELS:
        return None
    if fields[0] not in mcp_review_fields(state.procedure_id):
        return None
    if fields[0] == "receiving_authority" and state.procedure_id in MISSING_RECEIVING_PROCEDURES:
        if "receiving_authority" not in grounding.get("missing_information", []):
            return None
    if filters.procedure_id and filters.procedure_id != state.procedure_id:
        return None
    today = datetime.now(timezone.utc).date()
    if (filters.as_of and filters.as_of != today) or (state.as_of and state.as_of != today):
        return None
    return PendingLookup(
        procedure_id=state.procedure_id,
        procedure_name=data.procedures[state.procedure_id]["title"],
        field=fields[0],
        jurisdiction=filters.jurisdiction or state.jurisdiction,
        as_of=today,
        offered_at=datetime.now(timezone.utc),
        local_answer=answer,
        local_grounding=grounding,
    )


def consume(state, text, filters, *, enabled):
    """Consume before any I/O; unknown replies/topic/filter changes invalidate the offer."""
    pending = state.pending_realtime
    state.pending_realtime = None
    if not pending or not enabled:
        return None, None
    if not {pending.field, *pending.remaining_fields} <= mcp_review_fields(pending.procedure_id):
        return "out_of_scope", pending
    if pending.field == "receiving_authority" and "receiving_authority" not in (
        pending.local_grounding or {}
    ).get("missing_information", []):
        return "out_of_scope", pending
    age = (datetime.now(timezone.utc) - pending.offered_at).total_seconds()
    if not 0 <= age <= 900 or pending.as_of != datetime.now(timezone.utc).date():
        return None, None
    if (
        filters.procedure_id
        and filters.procedure_id != pending.procedure_id
        or filters.jurisdiction
        and pending.jurisdiction
        and filters.jurisdiction != pending.jurisdiction
        or filters.as_of
        and filters.as_of != pending.as_of
    ):
        return None, None
    if original_record_requested(text):
        # Inspecting the source is not consent, but the earlier lookup offer
        # must survive so the user's next affirmative can still authorize it.
        state.pending_realtime = pending
        return None, None
    action = decision(text)
    if pending.awaiting_variant:
        if action == "no":
            return "no", pending
        clean = normalize(text)
        selected = None
        if clean.isdigit() and 1 <= int(clean) <= len(pending.candidate_titles):
            selected = pending.candidate_titles[int(clean) - 1]
        elif len(clean) >= 6:
            matches = [
                title for title in pending.candidate_titles
                if clean == normalize(title)
                or clean in normalize(title)
                or ("cu tuyen" in clean and "cu tuyen" in normalize(title))
                or ("thuong binh" in clean and "thuong binh" in normalize(title))
            ]
            if len(matches) == 1:
                selected = matches[0]
        if selected is not None:
            pending.selected_current_title = selected
            pending.awaiting_variant = False
            return "yes", pending
        if action == "yes":
            state.pending_realtime = pending
            return "need_variant", pending
        return None, None
    if pending.awaiting_jurisdiction:
        if action == "no":
            return action, pending
        if action == "yes":
            state.pending_realtime = pending
            return "need_jurisdiction", pending
        clean = " ".join(text.strip().split())
        lowered = clean.casefold()
        blocked = ("http://", "https://", "<", ">", "\n", "\r")
        topic_words = ("hồ sơ", "thủ tục", "đăng ký", "lệ phí", "bao lâu", "giấy tờ")
        if not clean or len(clean) > 200 or any(item in lowered for item in blocked + topic_words):
            return None, None
        if detect_fields(clean):
            # A new information request is not an address, even after consent.
            # Let ordinary routing answer it against the current procedure.
            return None, None
        pending.jurisdiction = re.sub(
            r"^(?:ở|tại|địa phương|khu vực)\s+", "", clean, flags=re.IGNORECASE
        ).strip()
        return ("yes", pending) if pending.jurisdiction else (None, None)
    if action == "yes" and not pending.jurisdiction:
        pending.awaiting_jurisdiction = True
        state.pending_realtime = pending
        return "need_jurisdiction", pending
    return (action, pending) if action else (None, None)


def offer(state, data, answer, grounding, *, enabled):
    state.pending_realtime = None
    if not enabled or not state.procedure_id or not grounding:
        return answer
    procedure = data.procedures.get(state.procedure_id)
    if not procedure:
        return answer
    today = datetime.now(timezone.utc).date()
    if state.as_of and state.as_of != today:
        return answer
    requested = list(state.field_intents)
    present = {x.get("field") for x in grounding.get("checklist") or [] if isinstance(x, dict)}
    missing = set(requested) - present
    if not needs_realtime_offer(state.procedure_id, set(requested), local_missing=bool(missing)):
        return answer
    allowed = mcp_review_fields(state.procedure_id)
    candidates = [
        f
        for f in requested
        if f in FIELD_LABELS and f in allowed
        and (f != "receiving_authority" or f in grounding.get("missing_information", []))
        and (f != "fees" or f in grounding.get("missing_information", [])
             or any("theo quy dinh" in normalize(str(x.get("value", "")))
                    for x in grounding.get("checklist", []) if x.get("field") == "fees"))
    ]
    if not candidates:
        return answer
    selected = candidates[0]
    state.pending_realtime = PendingLookup(
        procedure_id=state.procedure_id,
        procedure_name=procedure["title"],
        field=selected,
        jurisdiction=state.jurisdiction,
        as_of=today,
        offered_at=datetime.now(timezone.utc),
        local_answer=answer,
        local_grounding=grounding,
        remaining_fields=candidates[1:],
    )
    # Keep the procedure/field/jurisdiction bound in PendingLookup, not in a
    # user-facing diagnostic line. The answer immediately above provides context.
    invitation = (
        SCHOLARSHIP_OFFER
        if selected == "required_documents"
        and is_quarantined_scholarship_documents(state, data, grounding)
        else (
            "Bạn có muốn mình kiểm tra nơi tiếp nhận hồ sơ của thủ tục này "
            "trên Cổng DVC Quốc gia không?"
            if selected == "receiving_authority" else OFFER
        )
    )
    if len(candidates) > 1:
        invitation = "Bạn có muốn mình kiểm tra " + ", ".join(FIELD_LABELS[f] for f in candidates) + " trên Cổng DVC và nguồn chính thức liên quan không?"
    return answer + "\n\n" + invitation + "\n\n" + CONSENT_HINT


def literal(text):
    # Remote text is displayed as data, never sent into the model or rendered as raw HTML.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


async def _resolve_one(pending, action, client):
    scholarship_documents = (
        pending.procedure_id == SCHOLARSHIP_ID and pending.field == "required_documents"
    )
    if action == "out_of_scope":
        return (
            "Mục này không thuộc nhóm thông tin được hỗ trợ tra Cổng DVC. "
            "Mình giữ nguyên thông tin từ nguồn đã duyệt:\n\n" + pending.local_answer,
            pending.local_grounding,
        )
    if action == "no":
        message = "Đã bỏ qua kiểm tra trực tuyến."
        if not scholarship_documents:
            message += "\n\n" + pending.local_answer
        return message, pending.local_grounding
    if action == "need_jurisdiction":
        return (
            "Bạn cho biết địa phương cần kiểm tra (ví dụ: phường/xã, quận/huyện, "
            "tỉnh/thành phố). Mình chưa gọi Cổng DVC ở bước này.",
            pending.local_grounding,
        )
    if action == "need_variant":
        return (
            "Bạn chọn đúng trường hợp cần tra cứu (trả lời số hoặc tên thủ tục):\n"
            + "\n".join(
                f"{index}. {literal(title)}"
                for index, title in enumerate(pending.candidate_titles, 1)
            ),
            pending.local_grounding,
        )
    request = pending.request()
    result = await client.lookup_current(request) if client is not None else None
    if result is not None and result.status == "AMBIGUOUS" and result.alternatives:
        pending.candidate_titles = result.alternatives
        pending.awaiting_variant = True
        return (
            "Mình đã tra Cổng DVC Quốc gia, nhưng có nhiều trường hợp khác nhau. "
            "Bạn chọn đúng trường hợp cần kiểm tra (trả lời số hoặc tên thủ tục):\n"
            + "\n".join(
                f"{index}. {literal(title)}"
                for index, title in enumerate(pending.candidate_titles, 1)
            ),
            pending.local_grounding,
        )
    if result is None or result.status not in {"FOUND", "PARTIAL"}:
        explanation = "Không kết nối được nguồn tra cứu ở lượt này."
        if result is not None:
            if result.retrieval_note == "DVC_NO_UNIQUE_PUBLIC_API_TITLE_MATCH":
                explanation = (
                    "Đã gọi Cổng DVC nhưng không tìm được một thủ tục trùng khớp duy nhất "
                    "với tên trong dữ liệu nội bộ."
                )
            elif result.retrieval_note == "DVC_FIELD_OR_EXPLICIT_LOCALITY_NOT_VERIFIED":
                explanation = (
                    "Đã gọi Cổng DVC nhưng trang thủ tục không xác nhận được thông tin "
                    "đúng mục hỏi và địa phương bạn nêu."
                )
            elif result.retrieval_note == "DVC_WRONG_OR_UNKNOWN_JURISDICTION":
                explanation = "Đã gọi Cổng DVC nhưng các bản ghi tìm được chưa khớp địa phương. Bạn có thể bổ sung tỉnh/thành phố; mình không lấy thông tin tỉnh khác thay thế."
            elif result.status == "AMBIGUOUS":
                explanation = (
                    "Đã gọi Cổng DVC nhưng có nhiều thủ tục cùng tên; "
                    "chưa thể chọn thay bạn."
                )
            elif result.status == "ERROR":
                explanation = (
                    "Đã gọi Cổng DVC nhưng nguồn tra cứu tạm thời "
                    "không trả được kết quả hợp lệ."
                )
        message = "Chưa xác minh được thông tin hiện tại trên Cổng DVC Quốc gia. " + explanation
        if not scholarship_documents:
            message += (
                " Thông tin từ nguồn nội bộ đã duyệt vẫn được giữ nguyên:\n\n"
                + pending.local_answer
            )
        return message, pending.local_grounding
    if result.scope:
        scope_note = "\nPhạm vi nguồn: " + {"LOCALITY": "đúng địa phương", "PROVINCE": "cấp tỉnh/thành phố", "NATIONAL": "thủ tục cấp quốc gia", "UNVERIFIED": "chưa xác minh"}[result.scope]
        scope_note += "".join("\nLưu ý: " + literal(note) for note in result.limitations)
    elif pending.field == "receiving_authority":
        scope_note = f"\nĐịa phương được xác minh trong nguồn: {literal(result.jurisdiction)}"
    else:
        scope_note = (
            f"\nĐịa phương người dùng cung cấp: {literal(result.jurisdiction)}"
            "\nLưu ý: Cổng DVC trả dữ liệu chung của thủ tục; dòng này không xác nhận "
            "một quy định riêng của địa phương."
        )
    if scholarship_documents:
        introduction = "Hồ sơ được Cổng DVC Quốc gia công bố cho diện bạn đã chọn:\n"
    else:
        local_answer = pending.local_answer
        # Retain every local field already answered, including partial replies.
        introduction = (
            local_answer
            + "\n\nThông tin vừa tra cứu từ Cổng DVC / nguồn chính thức "
            "(chưa qua review nội bộ):\n"
        )
    if result.status == "PARTIAL":
        introduction += "Mới xác minh được một phần, chưa phải kết luận đầy đủ cho trường hợp của bạn.\n"
    answer = (
        introduction
        + literal(result.value)
        + "\n\n"
        + f"Nguồn: [{literal(result.source_title)}]({quote(result.source_url, safe=':/?=&%')})"
        + scope_note
        + f"\nThời điểm kiểm tra: {result.checked_at.isoformat()}"
    )
    if result.cache_hit and result.source_checked_at:
        answer += f"\nDùng bản tra cứu lưu tạm; nguồn được đọc lúc {result.source_checked_at.isoformat()}."
    # Keep the local evidence bundle intact; the web citation is explicit in the text.
    return answer, pending.local_grounding


async def resolve(pending, action, client):
    """One consent can authorize all explicitly offered fields, never new fields."""
    answer, grounding = await _resolve_one(pending, action, client)
    if action != "yes" or pending.awaiting_variant:
        return answer, grounding
    remaining = list(pending.remaining_fields)
    while remaining:
        field = remaining.pop(0)
        # A selected remote variant stays bound across the fields of this offer.
        next_pending = pending.model_copy(update={
            "field": field, "remaining_fields": remaining,
            "local_answer": answer, "local_grounding": grounding,
        }, deep=True)
        answer, grounding = await _resolve_one(next_pending, "yes", client)
        if next_pending.awaiting_variant:
            for key in ("field", "remaining_fields", "local_answer", "candidate_titles", "awaiting_variant"):
                setattr(pending, key, getattr(next_pending, key))
            break
    return answer, grounding
