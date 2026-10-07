"""Catalog-derived current-turn candidates; never a source of administrative facts.

Aliases come from the reviewed catalog. Phrase normalization is routing-only;
the original utterance remains the evidence. A dictionary can add reviewed aliases
without changing the reducer or teaching facts to the language model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.rag.catalog_policy import is_serving_candidate, preferred_procedure_id
from app.rag.g3.retrieval import procedure_names
from app.rag.g4.intent import detect_fields
from app.rag.retrieval import normalize
from app.rag.g5.text_views import compatible_tokens, surface

# Function words and information requests are not procedure identity. Do not
# remove action words (ngung, dut, lai, doi) or discriminating population words.
GENERIC = frozenset(
    "thu tuc hanh chinh ho so giay to phep cap dang ky co yeu to doi voi cua va "
    "nguoi cac nhung mot theo ve de duoc tai den trong nay toi minh em anh chi "
    "can muon xin hoi lam chuan bi gom gi nao bao lau ngay mat nop o dau "
    "phi le tien hinh thuc truc tuyen qua mang khong hay thi con la cho biet "
    "thong tin trinh tu buoc quy dinh phap ly ban sao ban chinh".split()
) - {"so", "o"}

ROUTING_PHRASES = {
    "tam nghi": "tam ngung",
    "nghi tam": "tam ngung",
    "ngung tam thoi": "tam ngung",
    "dong cua vinh vien": "cham dut",
    "dong han": "cham dut",
    "ngoai quoc": "nuoc ngoai",
    "xay nha": "xay dung",
    "cat nha": "xay dung",
    "dang ki": "dang ky",
    "dang ksy": "dang ky",
}
ACTION_PHRASES = (
    "tam ngung",
    "cham dut",
    "cap lai",
    "cap doi",
    "thanh lap",
    "thay doi",
    "tiep tuc",
)


def routing_text(query: str) -> str:
    text = normalize(query)
    # "Về việc ..." introduces a subject; it is not evidence for "việc làm".
    text = re.sub(r"\bve (?:cai )?viec\b", "ve", text)
    text = re.sub(r"\bnha(?: (?:toi|minh|em|sap|se|dinh|chuan bi)){0,4} xay\b", "xay dung", text)
    for phrase, replacement in ROUTING_PHRASES.items():
        text = re.sub(rf"\b{re.escape(phrase)}\b", replacement, text)
    return text


def positive_text(query: str) -> str:
    text = routing_text(query)
    # In "A, không phải B", A is the requested procedure and B is rejected.
    # Only use the left clause when it contains real routing information; this
    # must not break a statement such as "tôi không phải người nước ngoài".
    contrast = re.search(r"\bkhong phai\b", text)
    if contrast:
        left = text[: contrast.start()].strip(" ,;:-")
        right = text[contrast.end() :]
        replacement = re.search(r"\b(?:ma|chu|thay vao do)\b\s+", right)
        if replacement:
            text = right[replacement.end() :]
        elif len(set(left.split()) - GENERIC) >= 2:
            text = left
    # Retain the target after an explicit correction, not the rejected procedure.
    for pattern in (
        r"\b(?:thuc su can(?: hoi| lam)?(?: la)?|chuyen (?:han |chu de )sang|doi sang)\s+",
        r"\bkhong (?:phai|hoi|lam|can|muon)\b.*?\b(?:ma|nua)\s+",
    ):
        matches = list(re.finditer(pattern, text))
        if matches:
            text = text[matches[-1].end() :]
    # A negated qualifier must not strengthen a more specific catalog variant.
    text = re.sub(r"\bkhong (?:co |phai |voi )?(?:yeu to |nguoi )?nuoc ngoai\b", "", text)
    return text


def phrase_in(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


@dataclass(frozen=True, slots=True)
class LexicalCandidate:
    procedure_id: str
    score: float
    coverage: float
    matched: frozenset[str]


def explicit_catalog_subject(data, query: str) -> str | None:
    """Match a whole, accented catalog subject after conversational wrappers.

    Do not treat 'sổ nhà' or ambiguous unaccented 'so nha' as 'số nhà'.
    This is a title comparison, not a new dictionary of procedure aliases.
    """
    text = surface(query)
    subject = re.sub(
        r"^(?:(?:tôi|mình|em|tui) )?(?:(?:cần|muốn) )?(?:làm |hỏi )?thủ tục (?:về )?",
        "", text,
    )
    if subject == text:
        return None
    matches = []
    for pid in sorted(data.accepted):
        if not is_serving_candidate(pid):
            continue
        title = re.sub(r"^(?:hồ sơ cấp|hồ sơ|thủ tục) ", "", surface(data.procedures[pid]['title']))
        if len(title.split()) >= 2 and subject == title:
            matches.append(pid)
    return matches[0] if len(matches) == 1 else None


def rank_candidates(data, query: str) -> list[LexicalCandidate]:
    """Rank *all* canonical procedures, independent of conversation state.

    Weighted current-query coverage prevents short but incompatible titles from
    beating a long official title. Names/aliases, not answer fragments, are used.
    """
    # 'số' and 'ở' can identify a subject; 'hồ sơ' is a generic phrase.
    # Keep the original accented query for compatible_tokens below.
    text = re.sub(r"\bho so\b", " ", positive_text(query))
    names: dict[str, list[str]] = {}
    for pid, procedure in data.procedures.items():
        canonical = preferred_procedure_id(pid)
        if canonical not in data.procedures or not is_serving_candidate(canonical):
            continue
        names.setdefault(canonical, []).extend(procedure_names(data, procedure))
    tokens = {pid: set(" ".join(values).split()) - GENERIC for pid, values in names.items()}
    frequency = Counter(token for values in tokens.values() for token in values)
    query_tokens = set(text.split()) - GENERIC
    actions = {action for action in ACTION_PHRASES if phrase_in(text, action)}
    eligible = {
        pid: values
        for pid, values in tokens.items()
        if not actions
        or any(phrase_in(routing_text(data.procedures[pid]["title"]), action) for action in actions)
    }
    # Unknown words carry no lexical evidence; they can still be interpreted by Qwen.
    domain_tokens = query_tokens & set().union(*eligible.values()) if eligible else set()
    weight = {token: math.log(1 + len(tokens) / count) for token, count in frequency.items()}
    denominator = sum(weight[t] for t in domain_tokens) or 1
    ranked = []
    for pid, values in eligible.items():
        matched = values & query_tokens
        original_names = [data.procedures[pid]["title"], *data.procedures[pid].get("aliases", [])]
        # Preserve evidence from explicit routing rewrites, but do not let a
        # different accented syllable count as an identity match.
        raw_words = set(normalize(query).split())
        compatible = compatible_tokens(query, " ".join(original_names))
        matched -= (raw_words - compatible)
        if not matched:
            continue
        # A qualifier by itself ("nước ngoài") needs an established family;
        # it cannot turn every unrelated conversation into foreign marriage.
        bases = [
            other
            for key, other in tokens.items()
            if key != pid and 2 <= len(other) <= 3 and other < values
        ]
        if bases and not any(len(base & matched) >= 2 for base in bases):
            continue
        if any(matched <= base for base in bases):
            continue
        title = routing_text(data.procedures[pid]["title"])
        if actions and not any(phrase_in(title, action) for action in actions):
            continue
        matched_weight = sum(weight[t] for t in matched)
        coverage = matched_weight / denominator
        title_coverage = matched_weight / (sum(weight[t] for t in values) or 1)
        # Query coverage dominates; compact official titles break otherwise equal
        # matches. Shared generic nouns alone cannot resolve close alternatives.
        score = coverage + 0.15 * title_coverage
        ranked.append(LexicalCandidate(pid, score, coverage, frozenset(matched)))
    return sorted(ranked, key=lambda item: (-item.score, item.procedure_id))


def lexical_winner(candidates: list[LexicalCandidate]) -> str | None:
    if not candidates or candidates[0].coverage < 0.85 or len(candidates[0].matched) < 2:
        return None
    if len(candidates) > 1 and candidates[0].score - candidates[1].score < 0.12:
        return None
    return candidates[0].procedure_id


def distinctive_candidates(data, query: str) -> set[str]:
    """Require a contiguous, catalog-unique phrase, not scattered common words.

    Background words (company, family, situation) otherwise dilute a specific
    request. Two independent unique phrases still require disambiguation.
    """
    text = positive_text(query)
    owners: dict[str, set[str]] = {}
    for pid, procedure in data.procedures.items():
        canonical = preferred_procedure_id(pid)
        if canonical not in data.accepted or not is_serving_candidate(canonical):
            continue
        for name in procedure_names(data, procedure):
            words = routing_text(name).split()
            for size in (2, 3):
                for index in range(len(words) - size + 1):
                    part = words[index : index + size]
                    if len(set(part) - (GENERIC - {"quy"})) < 2:
                        continue
                    owners.setdefault(" ".join(part), set()).add(canonical)
        # A mixed-case official title may contain a distinctive acronym (THCS).
        # Require a second title word; the acronym alone is not a full request.
        if not procedure["title"].isupper():
            for acronym in re.findall(r"\b[A-Z]{3,6}\b", procedure["title"]):
                title_words = set(routing_text(procedure["title"]).split()) - GENERIC
                if (set(text.split()) & title_words) - {acronym.lower()}:
                    owners.setdefault(acronym.lower(), set()).add(canonical)
    found = set()
    title_tokens = {
        pid: set(routing_text(p["title"]).split()) - GENERIC for pid, p in data.procedures.items()
    }
    for phrase, pids in owners.items():
        if len(pids) != 1 or not phrase_in(text, phrase):
            continue
        pid = next(iter(pids))
        bases = [
            tokens
            for key, tokens in title_tokens.items()
            if key != pid and 2 <= len(tokens) <= 3 and tokens < title_tokens[pid]
        ]
        if bases and not any(len(tokens & set(text.split())) >= 2 for tokens in bases):
            continue
        found.add(pid)
    return found


def evidence_followup(data, query: str, active_id: str) -> bool:
    """A question about documents/branches already in this procedure's evidence.

    This is not a global document-to-procedure router. A new explicit procedure
    or a new action must still pass the independent topic gate first.
    """
    text = routing_text(query)
    if not detect_fields(query):
        return False
    if re.search(r"\b(?:muon|xin|dang ky|bat dau|chuyen sang|doi sang|mo tai khoan)\b", text):
        return False
    procedure = data.procedures.get(active_id, {})
    values = list(procedure.get("required_documents", []))
    for fid in procedure.get("field_evidence", {}).get("required_documents", []):
        fragment = getattr(data, "fragments", {}).get(fid, {})
        values.append(fragment.get("text", ""))
    source_words = set(routing_text(" ".join(values)).split())
    branch = re.search(r"\bchuyen(?: truong)? (di|den)\b", text)
    if branch and f"ho so chuyen truong {branch.group(1)}" in routing_text(" ".join(values)):
        return True
    field_words = set(
        "the neu la ho so chuyen den di can mang nop co khong chua cu moi giay "
        "tai lieu thong tin tai khoan ngan hang va hay ban chinh photo sao "
        "cho co quan nao nhung gi gom o dau phi le bao nhieu toi minh em "
        "xin hoi duoc chu a nhe phai dung mau don so ay nhi thi sao dem goc vay".split()
    )
    content = set(text.split()) - field_words
    supported = (set(text.split()) - GENERIC) & source_words
    return len(supported) >= 2 and content <= source_words


def field_only_followup(query: str) -> bool:
    """Recognize bounded elliptical questions, not any sentence containing 'hồ sơ'."""
    # Colloquial trailing "nha" is a particle; accented "nhà" is a topic noun.
    text = routing_text(re.sub(r"\bnha\s*[.!?]*$", "", query, flags=re.IGNORECASE))
    # Strip only discourse operators, not bare negation or arbitrary content.
    # The remaining sentence must still consist entirely of field vocabulary.
    text = re.sub(r"\b(?:dung|khong) chi (?:moi |rieng )?", "", text)
    text = re.sub(r"^(?:gio |bay gio )?(?:chi )?giu\b", "", text)
    text = re.sub(r"^(?:van )?cho phan\b", "cho", text)
    # Source-selection instructions do not introduce another procedure. Only
    # discard their prefix when the suffix itself is a strictly field-only ask.
    local = re.search(r"\b(?:hay |chi )?tra (?:loi )?theo nguon (?:local|noi bo) ve (.+)$", text)
    if local and field_only_followup(local.group(1)):
        return True
    text = re.sub(r"\b(?:thu tuc nay|viec nay|cai nay|vay|the|con|a|nhe|voi|toi|minh)\b", " ", text)
    # Recognize the old field label even while it is paused. Do not add loose
    # "doi"/"tuong" tokens, which could hide a real change-of-procedure request.
    text = re.sub(r"\bdoi tuong thuc hien\b", " ", text)
    text = " ".join(text.split())
    field_words = set(
        "ho so gom can nhung gi giay to tai lieu thanh phan chuan bi phai mang theo "
        "le phi bao nhieu tien ton mat dong mien thoi gian bao lau may ngay lam viec "
        "giai quyet xu ly tra ket qua khi nao duoc nhan nop gui cach bang theo "
        "phuong thuc hinh truc tuyen tiep online buu dien chinh qua mang o tai dau "
        "noi co quan bo phan don vi tham quyen tiep nhan quy trinh trinh tu cac buoc "
        "can cu co so phap ly van ban luat dieu khoan la khong ko co hay va hoac "
        "cho biet muon hoi xem nhu the nao du muon biet xong het chua bao gio "
        "xin vui long nho dia diem duong chu a nhe nhi ay do ne nua the thi sao "
        "luon ca hai them cung dum giup tui hong tren thu tuc dem ra "
        "tat toan bo moi thu thong tin day du coi giai han hen lay xong "
        "chung bua thuc hien huong dan tung ve cua muc chi rieng thoi".split()
    )
    if detect_fields(query) and not (set(text.split()) - field_words):
        return True
    return bool(
        re.fullmatch(
            r"(?:ho so(?: gom| can)?(?: nhung)?(?: gi)?|giay to(?: can)?(?: nhung)?(?: gi)?|"
            r"(?:mat |cho |trong )?bao lau|may ngay|"
            r"(?:le )?phi(?: bao nhieu)?|(?:ton|mat) bao nhieu tien|"
            r"(?:nop|gui)(?: ho so)? (?:o |tai )?dau|"
            r"(?:co )?(?:gui|nop|lam)(?: ho so)? (?:qua mang|online|truc tuyen|truc tiep|buu dien)"
            r"(?: duoc)?(?: khong| ko)?|"
            r"(?:ho so va )?le phi|(?:cac buoc|quy trinh|trinh tu)|"
            r"(?:can cu|co so) phap ly)",
            text,
        )
    )
