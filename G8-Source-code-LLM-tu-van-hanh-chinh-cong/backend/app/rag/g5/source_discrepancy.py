"""Explain the quarantined row 42 without promoting it to approved evidence."""

# ruff: noqa: E501 -- F42 is kept byte-for-byte for hash-verified source display.

from __future__ import annotations

import hashlib
import re

from app.rag.retrieval import normalize

SCHOLARSHIP_ID = "d2_title_ff5c71001a91"
REVIEWED_RELEASE = "company-tthc-9c38dde8-v1-adjudicated-fees-v2"
ROW42_SOURCE_ID = "d2_xlsx_9c38dde8_r0042"
ROW42_CELL_SHA256 = "6c59aeb53dc671cdd61feb83f069a194a36945017b5da63defd30be1492dd92f"

# Exact F42 cell from full_data_company_source.xlsx, sheet "Câu trả lời biểu mẫu 1".
# Deliberately NOT part of the serving evidence bundle or checklist.
ROW42_DOCUMENTS = """- Đơn đề nghị miễn, giảm học phí dành cho học sinh, sinh viên đang học tại các cơ sở giáo dục nghề nghiệp, giáo dục đại học (theo Phụ lục IV của Nghị định 238/2025/NĐ-CP);
- Giấy xác nhận và dự toán kinh phí của các cơ sở giáo dục nghề nghiệp, giáo dục đại học (theo Phụ lục V, VI của Nghị định 238/2025/NĐ-CP);
- Bản sao chứng thực văn bằng tốt nghiệp hoặc xác nhận tốt nghiệp tạm thời hoặc xác nhận học bạ việc hoàn thành chương trình trung học cơ sở (quy định đối tượng tại điểm 1.1 mục 1 Thông báo này)/trung học phổ thông (quy định đối tượng tại điểm 1.3 mục 1 Thông báo này) của hiệu trưởng xác nhận;
- Bản sao chứng thực của cơ quan quản lý trong các trường hợp thuộc nhóm đối tượng tại điểm 1.4 mục 1 Thông báo này;
- Hóa đơn hoặc biên lai đóng tiền học phí;
- Bản sao Căn cước công dân của sinh viên;
- Bản sao Căn cước công dân của cha/mẹ, người có tên trong Đơn đề nghị chi trả tiền miễn, giảm học phí;
- Thông tin chuyển khoản (ATM) của người học hoặc tài khoản của cha hoặc mẹ hoặc người giám hộ;
- Giấy xác nhận nơi cư trú của công an hoặc bản chụp màn hình thể hiện thông tin nơi cư trú trên ứng dụng VNeID (in kèm hồ sơ)."""

CONFLICT_NOTICE = (
    "Theo nguồn dữ liệu từ hệ thống, thủ tục tên ‘xét, cấp học bổng chính sách’ "
    "lại có phần hồ sơ mô tả thủ tục miễn, giảm học phí. Mình chưa thể xác nhận "
    "danh sách đó là hồ sơ xin học bổng chính sách, nên không dùng nó làm checklist "
    "nộp hồ sơ. Nếu muốn đối chiếu, bạn có thể yêu cầu ‘xem nguyên văn từ dữ liệu’."
)
SCHOLARSHIP_OFFER = (
    "Bạn có muốn mình kiểm tra hồ sơ học bổng chính sách đang được công bố "
    "trên Cổng Dịch vụ công Quốc gia không?"
)


def is_quarantined_scholarship_documents(state, data, grounding: dict | None) -> bool:
    """Only the locked row/field under review is eligible for this explanation."""
    if (
        data.version != REVIEWED_RELEASE
        or state.procedure_id != SCHOLARSHIP_ID
        or "required_documents" not in state.field_intents
        or not grounding
        or grounding.get("status") != "INSUFFICIENT_DATA"
        or "required_documents" not in grounding.get("missing_information", [])
    ):
        return False
    procedure = data.procedures.get(SCHOLARSHIP_ID, {})
    return (
        ROW42_SOURCE_ID in procedure.get("source_ids", [])
        and "required_documents" in procedure.get("missing_fields", [])
        and not procedure.get("field_evidence", {}).get("required_documents")
    )


def original_record_requested(text: str) -> bool:
    """A narrow follow-up request; ordinary document questions do not unlock F42."""
    clean = normalize(text)
    if re.fullmatch(
        r"(?:(?:cho )?(?:toi|minh|em|tui) )?(?:muon )?(?:xem|doc) nguyen van"
        r"(?: (?:tu|trong) du lieu)?",
        clean,
    ):
        return True
    source = any(
        phrase in clean
        for phrase in (
            "dong 42", "o f42", "nguon phuong", "ban ghi phuong", "du lieu goc",
            "tu du lieu", "trong du lieu",
        )
    )
    verb = any(phrase in clean for phrase in ("nguyen van", "xem", "cho xem", "trich", "ghi gi"))
    return source and verb


def original_record_reply(state, data, text: str) -> str | None:
    """Expose historical raw text only in the same scholarship conversation."""
    if (
        data.version != REVIEWED_RELEASE
        or state.procedure_id != SCHOLARSHIP_ID
        or not original_record_requested(text)
    ):
        return None
    procedure = data.procedures.get(SCHOLARSHIP_ID, {})
    if ROW42_SOURCE_ID not in procedure.get("source_ids", []):
        return None
    if hashlib.sha256(ROW42_DOCUMENTS.encode("utf-8")).hexdigest() != ROW42_CELL_SHA256:
        return None
    return (
        "Nội dung nguyên văn từ dữ liệu (chỉ để đối chiếu, "
        "không phải danh sách hồ sơ học bổng đã xác minh):\n\n"
        + ROW42_DOCUMENTS
    )
