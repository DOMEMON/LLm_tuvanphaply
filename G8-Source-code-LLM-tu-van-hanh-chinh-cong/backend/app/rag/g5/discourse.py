"""Bounded discourse constraints; never administrative facts or automatic approvals.

Explicit exclusions and abandoned subjects constrain model proposals. Elliptical
actions may use a known domain, but must still resolve against the catalog.
"""
import re

from app.rag.catalog_policy import is_serving_candidate
from app.rag.g4.intent import detect_fields
from app.rag.g5.topic_gate import TopicDecision, _explicit_match
from app.rag.g5.routing_evidence import field_only_followup
from app.rag.retrieval import normalize


def replacement_query(query):
    """Remove an explicitly deferred topic, not arbitrary conversation history."""
    parts = re.split(r"[,;.!?]+", query)
    for index, part in enumerate(parts[:-1]):
        if re.search(r"\b(?:de sau|gat .{0,40}sang mot ben|bo qua .{0,40}di)\b", normalize(part)):
            remaining = ", ".join(parts[index + 1:]).strip()
            if remaining:
                return remaining, True
    return query, False


def excluded_fields(query):
    removed = set()
    for clause in re.split(r"[,;.!?]+", query):
        text = normalize(clause)
        # Do not mistake 'đừng chỉ mỗi hồ sơ' (expand) for excluding documents.
        if re.search(r"\b(?:khong|dung) (?:chi|rieng)\b", text):
            continue
        if (re.search(r"\b(?:khoi|dung|khong can) (?:noi |nhac |tra loi |trinh bay |de cap |lap )", text)
                or re.search(r"\bbo qua\b", text)):
            removed.update(detect_fields(clause))
    return removed


def inclusive_fields(query):
    """'Not only documents' includes documents; it is not 'no documents'."""
    fields = set()
    for clause in re.split(r"[,;.!?]+", query):
        if re.search(r"\b(?:khong|dung) chi (?:moi |rieng )?", normalize(clause)):
            fields.update(detect_fields(clause))
    return fields


def catalog_hint(data, query):
    text = normalize(query)
    # A newly born child plus a general request for an administrative procedure
    # is a possible birth-registration goal. Ask for confirmation; the event
    # alone is not authority to select a procedure or provide its documents.
    newborn_event = bool(re.search(
        r"\b(?:sinh con|con (?:toi|minh|em) (?:vua |moi )?(?:sinh ra|ra doi)|"
        r"(?:em be|be|tre) (?:vua |moi )?(?:sinh ra|ra doi|chao doi)|"
        r"(?:con|be|tre) (?:vua |moi )?sinh)\b", text,
    ))
    general_procedure_request = bool(re.search(
        r"\b(?:thu tuc|giay to|ho so|lam gi)\b", text,
    ))
    incompatible_newborn_request = bool(re.search(
        r"\b(?:khong|chua|dung) (?:muon |can |co |da )?"
        r"(?:sinh con|sinh ra|ra doi)\b|"
        r"\b(?:khai tu|qua doi|tu vong|da chet|da mat|khong phai khai sinh)\b",
        text,
    ))
    explicit_procedure = _explicit_match(data, query) if newborn_event else None
    if (newborn_event and general_procedure_request and not incompatible_newborn_request
            and explicit_procedure is None):
        candidates = tuple(pid for pid in sorted(data.accepted) if is_serving_candidate(pid)
            and normalize(data.procedures[pid]["title"]) == "thu tuc dang ky khai sinh")
        if len(candidates) == 1:
            return TopicDecision("CLARIFY", reason="semantic_confirmation", candidate_ids=candidates,
                clarification_hint="Bạn muốn hỏi thủ tục đăng ký khai sinh cho bé, đúng không?")
    domestic_marriage_goal = bool(
        re.search(r"\b(?:lay vo|lay chong|cuoi vo|cuoi chong)\b", text)
        and re.search(r"\b(?:hai dua|hai nguoi|hai ben|ca hai|chung toi) (?:deu )?(?:la )?nguoi viet\b", text)
        and not re.search(r"\b(?:xac nhan|tinh trang|doc than|da cuoi|da lay|ly hon)\b", text)
    )
    # A life-event goal merits a targeted question, not a claim that marital
    # status certification itself makes the couple legally married.
    if ((re.search(r"\b(?:thanh|cong nhan la|tro thanh) vo chong\b", text) or domestic_marriage_goal)
            and re.search(r"\bmuon\b", text)
            and not re.search(r"\b(?:khong|chua muon|da thanh|da tro thanh|nuoc ngoai|ngoai quoc|trich luc)\b", text)):
        candidates = tuple(pid for pid in sorted(data.accepted) if is_serving_candidate(pid)
            and normalize(data.procedures[pid]["title"]) == "thu tuc dang ky ket hon")
        if len(candidates) == 1:
            return TopicDecision("CLARIFY", reason="semantic_confirmation", candidate_ids=candidates,
                clarification_hint="Bạn muốn làm thủ tục đăng ký kết hôn để được công nhận là vợ chồng, đúng không?")
    return None


def contextual_action_query(data, query, previous):
    """Resolve a dropped business noun only inside an established business topic."""
    if previous.procedure_id not in data.accepted:
        return None
    title = normalize(data.procedures[previous.procedure_id]["title"])
    # A separate field-only clause does not change the business action. Keep
    # all other clauses so another object (e.g. bank account) cannot disappear.
    clauses = [part for part in re.split(r"[,;.!?]+", query) if part.strip()]
    text = normalize(" ".join(part for part in clauses if not field_only_followup(part)))
    remaining = re.sub(r"\b(?:dong han|nghi han|dong cua vinh vien)\b", "", text)
    elliptical = set(remaining.split()) <= set(
        "gio bay doi y toi minh tui em muon can dinh luon thi sao vay nhe di a roi"
        " cho hoi ho so giay to nop o dau phi le thoi gian giai quyet bao lau cach"
        " hinh thuc va voi the nao".split())
    if ("ho kinh doanh" in title
            and elliptical
            and re.search(r"\b(?:dong han|nghi han|dong cua vinh vien)\b", text)
            and not re.search(r"\b(?:khong|chua|dung|dung co)\b", text)):
        return "Chấm dứt hoạt động hộ kinh doanh"
    return None


def explicit_loss_report(query):
    """A reported cash-loss incident is not a fee follow-up.

    Require an event, an object and a reporting request together; 'mất tiền
    không' and lost civil-registration documents must remain unaffected.
    """
    text = normalize(query)
    return bool(
        re.search(r"\b(?:danh roi|lam roi|bi mat|bi trom|bi lay mat) (?:vi )?tien\b", text)
        and re.search(r"\b(?:trinh bao|bao cong an|bao mat)\b", text)
        and not re.search(r"\b(?:khong|chua|dung)\b", text)
    )
