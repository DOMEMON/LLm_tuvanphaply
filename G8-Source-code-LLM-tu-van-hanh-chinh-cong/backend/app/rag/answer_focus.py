"""Display exact evidence spans for a narrow question; retain full cited evidence.

No model summary and no inferred obligations. Section headings travel with a
quoted document so conditions for another branch are not silently discarded.
"""

import re

from app.rag.retrieval import normalize


def unsupported_personal_conclusion(query: str) -> str | None:
    """Make the assistant's evidentiary limit explicit; never decide a case."""
    q = normalize(query)
    if re.search(r"\b(?:tu ket luan|xac nhan luon|khang dinh)\b", q):
        return (
            "Mình không có đủ căn cứ để xác nhận tình trạng hoặc kết luận kết quả "
            "cho trường hợp cá nhân này; mình chỉ có thể hướng dẫn theo nguồn thủ tục."
        )
    return None


def document_excerpt(value: str, query: str) -> str | None:
    q = normalize(query)
    lines = value.splitlines()
    # A narrow projection must not silently choose the first of two branches,
    # or turn an exclusion into the very document the user did not ask for.
    branches = set(re.findall(r"\bchuyen(?: truong)? (di|den)\b", q))
    if len(branches) > 1 or re.search(r"\b(?:ngoai|khong phai|khong can)\b", q):
        return None
    if branches:
        starts = [
            (i, re.search(r"\bho so chuyen truong (di|den)\b", normalize(line)))
            for i, line in enumerate(lines)
            if re.search(r"\bho so chuyen truong (?:di|den)\b", normalize(line))
        ]
        for position, (start, match) in enumerate(starts):
            if match.group(1) in branches:
                end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
                return "\n".join(lines[start:end]).strip()

    terms = [
        term
        for term in (
            "hoc ba",
            "tai khoan",
            "cu tru",
            "giay xac nhan khuyet tat cu",
            "giay chung sinh",
            "giay chung nhan ket hon",
        )
        if term in q
    ]
    form_question = bool(re.search(r"\b(?:mau don|mau to khai|bieu mau|mau so)\b", q))
    if not terms and not form_question:
        return None
    if terms and (
        any(term not in normalize(value) for term in terms)
        or (" va " in f" {q} " and len(terms) < 2)
    ):
        # Preserve the complete source when only part of a compound request is
        # understood or supported; a partial excerpt would hide that omission.
        return None
    headings = []
    selected = []
    for line in lines:
        text = normalize(line)
        is_heading = bool(re.match(r"^(?:\d+\.|[IVX]+\.)?\s*(?:Thành phần|Đối với)", line))
        is_heading = is_heading or bool(re.match(r"^[IVX]+\.", line))
        if is_heading:
            headings = [line]
        matched = any(term in text for term in terms)
        # Preserve the whole form line, including its applicability and reference.
        matched = matched or (form_question and "mau" in text and "don" in text)
        if matched:
            for part in [*headings, line]:
                if part not in selected:
                    selected.append(part)
    # No reliable line boundary: keep the entire field rather than cut conditions.
    if not selected or "\n".join(selected).strip() == value.strip():
        return None
    return "\n".join(selected)
