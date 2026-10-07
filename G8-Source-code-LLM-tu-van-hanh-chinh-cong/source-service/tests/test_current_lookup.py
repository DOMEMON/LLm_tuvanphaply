import asyncio
from datetime import datetime, timezone

import pytest

from app import current_lookup as v2
from app import current_procedure as cp
from app import official_documents as docs
from app.realtime_contract import LookupRequest, LookupResult


def request(**changes):
    return LookupRequest(
        **(
            dict(
                procedure_id="fixture",
                procedure_name="Đăng ký Alpha",
                field="receiving_authority",
                jurisdiction="Phường Thử, TP.HCM",
                as_of=datetime.now(timezone.utc).date(),
            )
            | changes
        )
    )


def item(id="a", **changes):
    return (
        dict(
            id=id,
            name="Đăng ký Alpha",
            state="ACTIVE",
            type="STANDARD",
            departmentPromulgate="Bộ Thử",
        )
        | changes
    )


def run(items=None, detail=None, req=None, **kwargs):
    calls = []

    def fetch(url, payload, timeout):
        calls.append((url, payload))
        data = (
            {"items": [item()] if items is None else items}
            if url == cp.SEARCH_URL
            else {"name": "Đăng ký Alpha", "unitGroupsExecuting": [{"name": "UBND cấp xã"}]}
            if detail is None
            else detail
        )
        return url, {"code": "OK", "data": data}

    return asyncio.run(v2.lookup(req or request(), enabled=True, fetch=fetch, **kwargs)), calls


def test_national_authority_is_partial_not_verified_address():
    result, _ = run()
    assert result.status == "PARTIAL" and result.scope == "NATIONAL"
    assert result.value == "UBND cấp xã" and result.limitations


def test_explicit_local_address_found_without_losing_conditions():
    result, _ = run(
        detail={"name": "Đăng ký Alpha", "dossierReceivingAddresses": "UBND Phường Thử, TP.HCM"}
    )
    assert result.status == "FOUND" and result.scope == "LOCALITY"


def test_another_province_never_used_even_for_exact_title():
    result, calls = run([item(type="SPECIFIC", departmentPromulgate="UBND tỉnh Gia Lai")])
    assert result.status == "NOT_FOUND" and not result.value
    assert all(url == cp.SEARCH_URL for url, _ in calls)


def test_correct_province_preferred_over_national_and_wrong_province():
    result, calls = run(
        [
            item("national"),
            item("wrong", type="SPECIFIC", departmentPromulgate="UBND tỉnh Gia Lai"),
            item("right", type="SPECIFIC", departmentPromulgate="UBND Thành phố Hồ Chí Minh"),
        ]
    )
    assert result.scope == "PROVINCE"
    assert calls[-1][1] == {"id": "right"}


def test_duplicate_same_province_does_not_pick_first():
    result, _ = run([item("a"), item("b")])
    assert result.status == "AMBIGUOUS"


def test_detail_id_must_match_selected_record():
    result, _ = run(detail={"id": "other", "name": "Đăng ký Alpha"})
    assert result.status == "ERROR" and not result.value


def test_capped_field_not_reported_as_complete(monkeypatch):
    monkeypatch.setattr(cp, "extract_value", lambda *_: "a" * 8000)
    result, _ = run()
    assert result.status == "NOT_FOUND" and not result.value


@pytest.mark.parametrize("groups", [{"name": "wrong schema"}, [{}] * 101])
def test_unbounded_or_invalid_authority_groups_fail_closed(groups):
    result, _ = run(detail={"name": "Đăng ký Alpha", "unitGroupsExecuting": groups})
    assert result.status == "ERROR" and not result.value


@pytest.mark.parametrize("state", ["INACTIVE", "DELETED", "EXPIRED", None])
def test_non_active_identity_not_served(state):
    assert run([item(state=state)])[0].status == "NOT_FOUND"


def test_unknown_ward_not_assumed_hcm():
    assert (
        v2.source_scope(
            item(type="SPECIFIC", departmentPromulgate="UBND Thành phố Hồ Chí Minh"), "Phường Thử"
        )
        is None
    )


def test_fee_reference_is_partial_and_never_zero():
    result, _ = run(
        req=request(field="fees"),
        detail={
            "name": "Đăng ký Alpha",
            "executionMethods": [
                {
                    "submissionMethod": "DIRECT",
                    "fees": [{"value": 0, "description": "Theo Nghị quyết 09/2025/NQ-HĐND"}],
                }
            ],
        },
    )
    assert result.status == "PARTIAL" and "0 đồng" not in result.value
    assert any("mức tiền" in x for x in result.limitations)


def test_qualifiers_not_erased_by_fuzzy_match():
    assert v2.select([item(name="Cấp lại chứng nhận Alpha")], "Cấp chứng nhận Alpha") is None
    assert (
        v2.select([item(name="Đăng ký Alpha đối với tổ chức")], "Đăng ký Alpha đối với cá nhân")
        is None
    )


def test_pagination_finds_second_page_and_stops_repeated_cursor():
    calls = []

    def fetch(url, payload, timeout):
        calls.append((url, payload))
        if url == cp.DETAIL_URL:
            return url, {
                "code": "OK",
                "data": {"name": "Đăng ký Alpha", "unitGroupsExecuting": [{"name": "UBND cấp xã"}]},
            }
        items = (
            [item(str(n), name=f"Beta {n}") for n in range(20)]
            if not payload["lastId"]
            else [item()]
        )
        return url, {"code": "OK", "data": {"items": items, "lastId": "cursor"}}

    result = asyncio.run(v2.lookup(request(), enabled=True, fetch=fetch))
    assert result.status == "PARTIAL"
    assert len(calls) == 3 and calls[1][1]["lastId"] == "cursor"


def test_retry_bounded_and_errors_not_leaked():
    calls = []

    def broken(*args):
        calls.append(1)
        raise OSError("secret")

    result = asyncio.run(v2.lookup(request(), enabled=True, fetch=broken))
    assert len(calls) == 2 and result.status == "ERROR"
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("patch", [{"as_of": "2000-01-01"}, {"field": "steps"}])
def test_disabled_date_and_paused_field_no_io(patch):
    def never(*args):
        pytest.fail("unexpected network")

    assert asyncio.run(v2.lookup(request(**patch), enabled=True, fetch=never)).status == "NOT_FOUND"
    assert asyncio.run(v2.lookup(request(), fetch=never)).status == "ERROR"


def test_hcm_land_reads_document_and_preserves_scope():
    def no_api(*args):
        pytest.fail("document should resolve")

    result = asyncio.run(
        v2.lookup(
            request(procedure_name="Đăng ký đất đai, tài sản gắn liền với đất lần đầu"),
            enabled=True,
            fetch=no_api,
            document_reader=lambda: {"receiving_authority": "Cơ quan giả lập theo điều kiện A"},
        )
    )
    assert result.status == "PARTIAL" and result.scope == "PROVINCE"
    assert result.source_url == docs.PDF_URL and result.matched_procedure_name


def test_unknown_province_does_not_read_hcm_document():
    def never():
        pytest.fail("HCM rules cannot answer another province")

    result, _ = run(
        [],
        req=request(
            procedure_name="Đăng ký đất đai, tài sản gắn liền với đất lần đầu",
            jurisdiction="Gia Lai",
        ),
        document_reader=never,
    )
    assert result.status == "NOT_FOUND"


def test_document_withdrawal_blocks_pdf_before_download():
    calls = []

    def fetch(url):
        calls.append(url)
        return "text/html", "44/2026/QĐ-UBND Tình trạng hiệu lực: Đã hết hiệu lực".encode()

    with pytest.raises(ValueError, match="NOT_CURRENT"):
        docs.read_land_rules(fetch=fetch)
    assert calls == [docs.ATTRIBUTE_URL]


def test_section_drift_rejected_instead_of_cutting_wrong_paragraph():
    with pytest.raises(ValueError, match="SECTION_CHANGED"):
        docs.section("wrong content", "start", "end")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a",
        "https://evil.example/a",
        "http://vbpl.vn/a",
        "https://vbpl.vn/arbitrary",
    ],
)
def test_document_fetch_rejects_arbitrary_urls_before_network(url):
    with pytest.raises(ValueError):
        docs.download(url)


def test_document_wrong_mime_cannot_enter_pdf_parser():
    def fetch(url):
        if url == docs.ATTRIBUTE_URL:
            return (
                "text/html",
                (
                    "44/2026/QĐ-UBND Tình trạng hiệu lực: Đang hiệu lực Ngày hiệu lực: 01/07/2026"
                ).encode(),
            )
        return "text/html", b"not a PDF"

    with pytest.raises(ValueError, match="PDF_CONTENT_TYPE"):
        docs.read_land_rules(fetch=fetch)


def test_timeout_does_not_cache_error():
    async def go():
        def slow(*args):
            import time

            time.sleep(0.04)
            return cp.SEARCH_URL, {"code": "OK", "data": {"items": []}}

        return await v2.lookup(request(), enabled=True, timeout=0.005, fetch=slow)

    assert asyncio.run(go()).retrieval_note == "DVC_TIMEOUT"


def test_loan_rule_adapter_is_scoped_and_partial():
    result, _ = run(
        req=request(
            procedure_name=(
                "Vay vốn hỗ trợ tạo việc làm, duy trì và mở rộng việc làm "
                "từ Quỹ quốc gia về việc làm đối với người lao động"
            )
        ),
        loan_reader=lambda: {"receiving_authority": "Cơ quan tín dụng giả lập"},
    )
    assert result.status == "PARTIAL" and result.scope == "NATIONAL"
    assert result.source_url == docs.LOAN_PDF_URL


def test_cache_expires_and_capacity(monkeypatch):
    cache = v2.OrderedDict()
    monkeypatch.setattr(v2.time, "monotonic", lambda: 100)
    for n in range(v2.CAPACITY + 1):
        v2.cache_put(cache, n, n)
    assert len(cache) == v2.CAPACITY and v2.cache_get(cache, 0) is None
    monkeypatch.setattr(v2.time, "monotonic", lambda: 221)
    assert v2.cache_get(cache, v2.CAPACITY) is None


def test_partial_contract_requires_limits_and_binding():
    result, _ = run()
    with pytest.raises(ValueError):
        LookupResult.model_validate(result.model_dump() | {"limitations": []})


def test_cached_result_keeps_source_read_time_and_no_repeat_io(monkeypatch):
    v2.CACHE.clear()
    calls = []

    def fetch(url, payload, timeout):
        calls.append(1)
        data = (
            {"items": [item()]}
            if url == cp.SEARCH_URL
            else {"name": "Đăng ký Alpha", "unitGroupsExecuting": [{"name": "UBND cấp xã"}]}
        )
        return url, {"code": "OK", "data": data}

    monkeypatch.setattr(cp, "fetch_json", fetch)
    first = asyncio.run(v2.lookup(request(), enabled=True))
    second = asyncio.run(v2.lookup(request(), enabled=True))
    assert len(calls) == 2 and second.cache_hit
    assert first.source_checked_at == second.source_checked_at
    # Different locality cannot share an answer cache key.
    asyncio.run(v2.lookup(request(jurisdiction="Gia Lai"), enabled=True))
    assert len(calls) == 4
    v2.CACHE.clear()
