"""Lexical field intent, independent of procedure identification."""

import re

from app.rag.retrieval import normalize
from app.rag.field_policy import DATASET_FIELDS

PATTERNS = {
    "receiving_authority": (
        r"\b(?:nop|gui|lam|nhan|xin)(?: ho so)? (?:o |tai )?(?:cho|noi) nao\b",
        r"\b(?:nop|gui|lam|xin|tiep nhan)(?: ho so)? (?:o |tai )?dau\b",
        r"\b(?:den|toi) (?:dau|co quan nao)\b",
        r"\b(?:o dau|tai dau|co quan nao)\b",
        r"\b(?:noi nop|cho nop|noi nhan|noi tiep nhan|dia diem|dia chi tiep nhan|"
        r"co quan tiep nhan|don vi tiep nhan)\b",
        r"\bnop cho (?:ben|phia) (?:di|den)\b",
        r"\b(?:co quan|don vi|bo phan) (?:nao )?(?:tiep nhan|giai quyet|xu ly)\b",
        r"\b(?:ai|noi nao) (?:tiep nhan|giai quyet|xu ly)\b",
        r"\b(?:co quan|don vi|bo phan) nao\b",
        r"\bco quan nhan\b",
        r"\b(?:mang|dua|gui).{0,80}\b(?:toi|den) (?:dung )?(?:co quan|don vi|bo phan|noi) nao\b",
    ),
    "required_documents": (
        r"\b(?:dem|mang|can|chuan bi)(?: theo)? (?:bo )?"
        r"(?:giay|giay to|tai lieu|bang chung|don)(?: nao| gi)\b",
        r"\b(?:mau|bo giay|bang chung) (?:gi|nao)\b",
        r"\b(?:can |phai )?dem(?: theo)? (?:gi|nhung gi)\b",
        r"\b(?:thanh phan ho so|ho so gom|ho so can (?:co |nhung )?gi)\b",
        r"\b(?:giay to|tai lieu|bo ho so).{0,60}\b(?:gom|can co|nhung gi|gi nao|gi|nao)\b",
        r"\b(?:can|chuan bi) (?:nhung )?gi\b",
        r"\b(?:can|phai) (?:nop|gui|chuan bi)(?: nhung)? gi\b",
        r"\b(?:co )?(?:can|phai) mang .+ (?:khong|ko)\b",
        r"\b(?:mau don|mau to khai|bieu mau|mau so)\b",
        r"\b(?:ban chinh|ban photo|ban sao|hoc ba|benh an|giay cu tru|"
        r"giay xac nhan|tai khoan ngan hang)\b",
        r"\b(?:can |phai )?mang(?: theo)? (?:giay to |tai lieu )?(?:gi|nhung gi|gi nao)\b",
    ),
    "fees": (
        r"\b(?:phi|le phi|bao nhieu tien|ton bao nhieu|ton tien|chi phi|mien phi|"
        r"dong bao nhieu|fees)\b",
        # Payment questions, not bare "tiền" (which also describes benefits/loans).
        r"\b(?:co |(?:co )?phai )?(?:mat|ton|dong|tra|nop) tien (?:khong|ko|hong|khong a)\b",
        r"\b(?:tien phai (?:dong|nop|tra)|(?:dong|nop|tra) (?:het |mat )?bao nhieu)\b",
    ),
    "processing_times": (
        r"\b(?:thoi gian|bao lau|may ngay|bao nhieu ngay|thoi han|hen tra|khi nao co ket qua)\b",
        r"\b(?:giai quyet|xu ly) (?:trong )?(?:bao lau|may ngay)\b",
        r"\bmay bua\b",
        r"\b(?:khi nao|bao gio|chung nao) (?:moi )?(?:xong|tra|nhan|co)\b",
        r"\b(?:thoi luong xu ly|han tra ket qua|hen lay ket qua)\b",
        r"\bcho.{0,60}\bket qua.{0,30}\b(?:bao lau|may ngay)\b",
    ),
    "submission_methods": (
        r"\b(?:hinh thuc|phuong thuc|truc tuyen|online|buu dien|buu chinh|nop cach nao)\b",
        r"\b(?:nop|lam) (?:qua mang|truc tiep)\b",
        r"\b(?:nop|gui|bao)(?: ho so| tam nghi| tam ngung)? (?:tren mang|bang cach nao)\b",
        r"\b(?:nop|gui|bao)\b.{0,60}\bbang cach nao\b",
        r"\b(?:gui|chuyen|nop)(?: ho so)? (?:qua|bang) (?:mang|online|buu dien|buu chinh)\b",
        r"\b(?:cach nop|nop (?:ho so )?bang (?:nhung )?hinh thuc|nop .* nhu the nao)\b",
        r"\b(?:nop|gui)(?: ho so)? (?:bang |theo )?cach nao\b",
        r"\bho so.{0,60}\b(?:gui|chuyen).{0,40}\b(?:cach nao|the nao)\b",
    ),
    "steps": (
        r"\b(?:trinh tu|cac buoc|quy trinh|buoc nao|lam the nao|can lam sao|"
        r"cach thuc hien|cach lam thu tuc|huong dan tung buoc|"
        r"khi nao phai (?:bao|nop)|han (?:bao|nop)|thuc hien (?:nhu )?the nao|lan luot)\b",
    ),
    "legal_bases": (
        r"\b(?:can cu phap ly|co so phap ly|van ban nao.{0,50}can cu|quy dinh.{0,50}van ban nao)\b",
    ),
    "applicant_scope": (
        r"\b(?:doi tuong|ai duoc|ai co the|nhung ai|truong hop nao|ai dung ho so)\b",
    ),
}


def asks_all_fields(query: str) -> bool:
    """All procedure information, not merely all documents or all procedures."""
    text = normalize(query)
    if re.search(r"\b(?:khong|chua|dung) (?:can |muon )?(?:xem |coi |biet )?(?:tat ca|toan bo|moi thu)\b", text):
        return False
    if re.search(r"\b(?:tat ca|toan bo) (?:cac )?thu tuc\b(?! nay)", text):
        return False
    if re.search(
        r"\b(?:tat ca moi thu|tat ca cac muc|moi thong tin|(?:tat ca|toan bo|day du) (?:cac )?thong tin)\b",
        text,
    ):
        return True
    if re.search(r"\b(?:xem|coi|cho|biet) (?:tat ca|toan bo|moi thu) (?:ve|cua)\b", text):
        return True
    return bool(re.fullmatch(
        r"(?:(?:toi|minh|em|tui|ban|cho|xin|muon|can|hay|hoi|xem|coi|biet|gui|ve) )*"
        r"(?:tat ca|toan bo|moi thu|het)"
        r"(?: (?:ve|cua))?(?: thu tuc nay)?(?: (?:luon|di|nhe|voi|a))?",
        text,
    ))


def detect_fields(query: str, names=()) -> set[str]:
    if asks_all_fields(query):
        return set(DATASET_FIELDS)
    text = " " + normalize(query) + " "
    text = re.sub(
        r"\bkhong (?:can )?(?:hoi|xem|biet|quan tam)(?: ve)? "
        r"(?:le phi|ho so|giay to|thoi gian|noi nop|cach nop|can cu phap ly|quy trinh)"
        r"(?: nua)?\b",
        " ",
        text,
    )
    # A procedure title can contain words such as fees/time without asking that field.
    for name in sorted(set(names), key=len, reverse=True):
        normalized_name = normalize(name)
        remainder = " ho so " if normalized_name.startswith("ho so ") else " "
        text = text.replace(" " + normalized_name + " ", remainder)
    # The benefit being applied for is not the fee charged for submitting it.
    text = re.sub(r"\b(?:ho tro|tro cap)(?: [a-z]+){0,3} (?:chi phi|kinh phi)\b", " ", text)
    text = re.sub(r"\b(?:ho tro|mien giam|mien|giam) hoc phi\b", " ", text)
    text = re.sub(r"\bde (?:lam|nop|bo sung|hoan thien) ho so\b", " ", text)
    # Document mentions in a case description are not themselves document questions.
    document_question = bool(
        re.search(r"\b(?:can|phai|mang|nop|gom|chuan bi|dung|hay|co .+ khong)\b", text)
    )
    fields = {f for f, patterns in PATTERNS.items() if any(re.search(p, text) for p in patterns)}
    if not document_question and fields == {"steps", "required_documents"}:
        fields.discard("required_documents")
    # "hồ sơ" is the object of a submission/location question, not a document intent.
    if re.search(r"\b(?:ho so|giay to|tai lieu)\b", text) and (
        not fields
        or re.search(r"\bho so va\b|\bva ho so\b", text)
        or re.search(r"\bho so\b.*\bva\b", text)
    ):
        fields.add("required_documents")
    if re.search(r"\bdocuments\b", text):
        fields.add("required_documents")
    # Enumerated fields are independent requests even without a conjunction:
    # "hồ sơ, phí, nơi nộp". Keep ordinary "nộp hồ sơ ở đâu" as location-only.
    for clause in re.split(r"[,;.!?\n]+", query):
        part = normalize(clause)
        if re.fullmatch(
            r"(?:(?:cho|xin|hoi|xem|can|ca|va|voi|luon|nua|nhung|toi|minh|em|tui) )*"
            r"(?:ho so|giay to|tai lieu)(?: (?:luon|nua|gi|nao))?",
            part,
        ):
            fields.add("required_documents")
    if re.search(
        r"\b(?:ho so|giay to) (?:voi|ca) (?:le phi|phi|noi nop|cho nop|thoi gian|thoi han)\b", text
    ):
        fields.add("required_documents")
    return fields
