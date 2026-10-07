import asyncio
import io
import json
import time
from datetime import datetime, timezone
from email.message import Message
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from app import current_procedure as cp
from app.realtime_contract import LookupRequest, LookupResult, official_url

TITLE = "Đăng ký Alpha"
REMOTE_ID = "public-alpha"


def request(**changes):
    return LookupRequest(
        **(
            dict(
                procedure_id="internal-alpha",
                procedure_name=TITLE,
                field="processing_times",
                jurisdiction="Phường Mô Phỏng",
                as_of=datetime.now(timezone.utc).date(),
            )
            | changes
        )
    )


def detail(**changes):
    return {
        "name": TITLE,
        "executionMethods": [
            {
                "submissionMethod": "ONLINE",
                "processingTime": 3,
                "processingTimeUnit": "WORKING_DAY",
                "description": "Kể từ khi nhận đủ hồ sơ hợp lệ.",
                "fees": [{"value": 0, "description": "Không thu phí."}],
            }
        ],
        "dossierReceivingAddresses": "UBND Phường Mô Phỏng",
        "executionCases": [
            {"name": "Thành phần hồ sơ", "profileComponents": [{"name": "Đơn đề nghị"}]}
        ],
        "executionSteps": [{"name": "Bước 1", "description": "Nộp hồ sơ"}],
        "legalBasisesDetails": [{"code": "01/2026", "name": "Nghị định thử nghiệm"}],
        "note": "Đối tượng: công dân",
        "subjectTypesDetails": [{"name": "Công dân Việt Nam"}],
    } | changes


def api(data):
    return {"code": "OK", "data": data, "message": "OK"}


def run(search=None, remote_detail=None, **kwargs):
    search = search if search is not None else [{"id": REMOTE_ID, "name": TITLE}]
    remote_detail = remote_detail if remote_detail is not None else detail()
    calls = []

    def fetch(url, payload, timeout):
        calls.append((url, payload))
        assert timeout > 0
        if url == cp.SEARCH_URL:
            return url, api({"items": search, "lastId": None, "total": len(search)})
        if url == cp.DETAIL_URL:
            assert payload == {"id": REMOTE_ID}
            return url, api(remote_detail)
        raise AssertionError(url)

    result = asyncio.run(cp.lookup(request(), enabled=True, fetch=fetch, **kwargs))
    return result, calls


def test_found_via_public_json_api():
    result, calls = run()
    assert result.status == "FOUND"
    assert result.value == (
        "Trực tuyến: 3 ngày làm việc\n"
        "Ghi chú: Kể từ khi nhận đủ hồ sơ hợp lệ."
    )
    assert result.procedure_id == "internal-alpha"
    assert result.source_url == cp.PUBLIC_DETAIL_ROOT + REMOTE_ID
    assert [call[0] for call in calls] == [cp.SEARCH_URL, cp.DETAIL_URL]
    assert result.checked_at.tzinfo


@pytest.mark.parametrize("description", [
    "Theo quy định", "Nghị quyết số 09/2025/NQ-HĐND ngày 27/10/2025.",
    "Miễn phí cho hộ nghèo; các trường hợp khác theo nghị quyết.", "Không thu phí.",
])
def test_zero_with_description_does_not_invent_unconditional_free_fee(description):
    record = detail(executionMethods=[{
        "submissionMethod": "ONLINE", "fees": [{"value": 0, "description": description}],
    }])
    assert cp.extract_value(record, request(field="fees")) == "Trực tuyến: " + description


@pytest.mark.parametrize("amount,expected", [(0, "0 đồng"), (25000, "25000 đồng")])
def test_fee_numeric_value_without_qualifier_is_preserved(amount, expected):
    record = detail(executionMethods=[{
        "submissionMethod": "DIRECT", "fees": [{"value": amount}],
    }])
    assert cp.extract_value(record, request(field="fees")) == "Trực tiếp: " + expected


def test_empty_fee_does_not_turn_method_label_into_fee_evidence():
    record = detail(executionMethods=[{"submissionMethod": "ONLINE", "fees": [{}]}])
    assert cp.extract_value(record, request(field="fees")) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/dvc",
        "https://dichvucong.gov.vn.evil.example/a",
        "https://dichvucong.gov.vn@evil.example/a",
        "http://dichvucong.gov.vn/a",
        "https://dichvucong.gov.vn:8443/a",
        "https://user@dichvucong.gov.vn/a",
        "https://dichvucong.gov.vn/a#fragment",
        "file:///etc/passwd",
        "https://127.0.0.1/a",
        "https://dichvucong.gov.vn/\na",
        "https://dichvucong.gov.vn\\@evil.example/",
    ],
)
def test_url_allowlist(url):
    with pytest.raises(ValueError):
        official_url(url)


def test_exact_title_required_and_ambiguous_rejected():
    result, calls = run(search=[{"id": "wrong", "name": "Đăng ký Beta"}])
    assert result.status == "NOT_FOUND"
    assert len(calls) == 1
    result, calls = run(
        search=[{"id": REMOTE_ID, "name": TITLE}, {"id": "other", "name": TITLE}]
    )
    assert result.status == "AMBIGUOUS"
    assert len(calls) == 1


def test_unique_high_similarity_identity_allows_reviewed_title_drift():
    local = (
        "Tạm ngừng kinh doanh/tiếp tục kinh doanh trước thời hạn đã thông báo "
        "của hộ kinh doanh"
    )
    current = (
        "Tạm ngừng kinh doanh, tiếp tục kinh doanh trước thời hạn đã đăng ký "
        "của hộ kinh doanh"
    )
    items = [
        {"id": REMOTE_ID, "name": current},
        {"id": "other", "name": "Đăng ký tạm ngừng của tổ chức khoa học"},
    ]
    assert cp._select_identity(items, local) == (
        REMOTE_ID,
        current,
        "UNIQUE_HIGH_SIMILARITY_TITLE",
    )
    assert cp._fallback_query(local) == "Tạm ngừng kinh doanh"


def test_similarity_close_tie_fails_closed():
    title = "Đăng ký Alpha thay đổi"
    items = [
        {"id": "a", "name": "Đăng ký Alpha thay đổi A"},
        {"id": "b", "name": "Đăng ký Alpha thay đổi B"},
    ]
    assert cp._select_identity(items, title) is None


def test_generic_scholarship_heading_returns_verifiable_choices():
    title = "Xét, cấp học bổng chính sách"
    variants = [
        title + " đối với sinh viên học theo chế độ cử tuyển",
        title + " đối với học viên cơ sở giáo dục nghề nghiệp tư thục dành cho thương binh",
    ]
    calls = []

    def fetch(url, payload, timeout):
        calls.append((url, payload))
        return url, api({"items": [
            {"id": "a", "name": variants[0]},
            {"id": "b", "name": variants[1]},
        ]})

    result = asyncio.run(cp.lookup(
        request(procedure_name=title, field="required_documents"),
        enabled=True,
        fetch=fetch,
    ))
    assert result.status == "AMBIGUOUS"
    assert result.alternatives == variants
    assert len(calls) == 1 and calls[0][0] == cp.SEARCH_URL


def test_user_selected_scholarship_variant_fetches_its_own_detail():
    title = "Xét, cấp học bổng chính sách đối với sinh viên học theo chế độ cử tuyển"

    def fetch(url, payload, timeout):
        if url == cp.SEARCH_URL:
            return url, api({"items": [{"id": REMOTE_ID, "name": title}]})
        assert payload == {"id": REMOTE_ID}
        return url, api(detail(name=title))

    result = asyncio.run(cp.lookup(
        request(procedure_name=title, field="required_documents"),
        enabled=True,
        fetch=fetch,
    ))
    assert result.status == "FOUND" and "Đơn đề nghị" in result.value


def test_detail_identity_mismatch_fails_closed():
    result, _ = run(remote_detail=detail(name="Đăng ký Beta"))
    assert result.status == "ERROR"
    assert result.value is None


def test_receiving_authority_requires_explicit_locality():
    def fetch(url, payload, timeout):
        if url == cp.SEARCH_URL:
            return url, api({"items": [{"id": REMOTE_ID, "name": TITLE}]})
        return url, api(detail(dossierReceivingAddresses="UBND cấp xã"))

    result = asyncio.run(
        cp.lookup(request(field="receiving_authority"), enabled=True, fetch=fetch)
    )
    assert result.status == "NOT_FOUND"
    assert "LOCALITY" in result.retrieval_note


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("submission_methods", "Trực tuyến"),
        ("required_documents", "Đơn đề nghị"),
        ("fees", "Không thu phí"),
        ("steps", "Nộp hồ sơ"),
        ("legal_bases", "01/2026"),
        ("applicant_scope", "Công dân Việt Nam"),
        ("receiving_authority", "Phường Mô Phỏng"),
    ],
)
def test_structured_field_extractors(field, expected):
    assert expected in cp.extract_value(detail(), request(field=field))


def test_disabled_historical_timeout_and_sanitized_failure():
    def never(*args):
        pytest.fail("must not fetch")

    assert asyncio.run(cp.lookup(request(), fetch=never)).status == "ERROR"
    historical = asyncio.run(
        cp.lookup(request(as_of="2000-01-01"), enabled=True, fetch=never)
    )
    assert historical.status == "NOT_FOUND"

    def slow(*args):
        time.sleep(0.04)
        return cp.SEARCH_URL, api({"items": []})

    result = asyncio.run(cp.lookup(request(), enabled=True, timeout=0.005, fetch=slow))
    assert result.retrieval_note == "DVC_TIMEOUT"

    def broken(*args):
        raise RuntimeError("secret remote response")

    result = asyncio.run(cp.lookup(request(), enabled=True, fetch=broken))
    assert result.status == "ERROR"
    assert "secret" not in result.model_dump_json()


def test_schema():
    result, _ = run()
    for patch in [
        {"source_url": None},
        {"source_url": "https://evil.example"},
        {"unexpected": True},
        {"checked_at": "2026-01-01T00:00:00"},
        {"status": "ERROR"},
    ]:
        with pytest.raises(ValidationError):
            LookupResult.model_validate(result.model_dump() | patch)


def test_redirect_external_blocked_before_request(monkeypatch):
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(req.full_url)
            headers = Message()
            headers["Location"] = "https://evil.example/private"
            raise HTTPError(req.full_url, 302, "redirect", headers, io.BytesIO())

    monkeypatch.setattr(cp, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="DVC_URL_NOT_ALLOWED"):
        cp.fetch_json(cp.SEARCH_URL, {"q": TITLE})
    assert calls == [cp.SEARCH_URL]


def test_tool_default_off(monkeypatch):
    from app.server import lookup_current_procedure

    monkeypatch.delenv("G5_REALTIME_MCP_ENABLED", raising=False)
    result = asyncio.run(lookup_current_procedure(**request().model_dump(mode="json")))
    assert result.retrieval_note == "DVC_REALTIME_DISABLED"


def test_wire_contract_copies_match():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "source-service/app/realtime_contract.py").read_bytes() == (
        root / "backend/app/rag/g5/realtime_contract.py"
    ).read_bytes()


def test_allowed_redirect_and_body_limit(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers = Message()
            self.headers["Content-Type"] = "application/json; charset=utf-8"

        def geturl(self):
            return cp.DETAIL_URL

    class Opener:
        def open(self, req, timeout):
            calls.append(req.full_url)
            if len(calls) == 1:
                headers = Message()
                headers["Location"] = cp.DETAIL_URL
                raise HTTPError(req.full_url, 307, "redirect", headers, io.BytesIO())
            return Response(b"x" * (cp.MAX_BYTES + 1))

    monkeypatch.setattr(cp, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="TOO_LARGE"):
        cp.fetch_json(cp.SEARCH_URL, {"q": TITLE})
    assert calls == [cp.SEARCH_URL, cp.DETAIL_URL]


def test_fetch_json_rejects_non_json(monkeypatch):
    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps({"code": "OK"}).encode())
            self.headers = Message()
            self.headers["Content-Type"] = "text/html"

        def geturl(self):
            return cp.SEARCH_URL

    class Opener:
        def open(self, req, timeout):
            return Response()

    monkeypatch.setattr(cp, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="UNSUPPORTED_CONTENT"):
        cp.fetch_json(cp.SEARCH_URL, {"q": TITLE})
