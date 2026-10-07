"""Bounded, free, multi-source lookup v2. Read-only and independent of chat intent.

Search is discovery, not evidence. Only a verified detail/document can answer.
PARTIAL is deliberately not FOUND: national authority != a verified ward address.
"""

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote

from app import current_procedure as portal
from app.official_documents import LOAN_PDF_URL, PDF_URL, read_land_rules, read_loan_rules
from app.realtime_contract import LookupResult, official_url

logger = logging.getLogger(__name__)
CACHE = OrderedDict()
DOCUMENT_CACHE = OrderedDict()
TTL = 120
CAPACITY = 128
MAX_PAGES = 3
SUPPORTED = {
    "fees",
    "receiving_authority",
    "processing_times",
    "required_documents",
    "submission_methods",
}


def fold(value):
    text = unicodedata.normalize("NFD", str(value or "").casefold().replace("đ", "d"))
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c))).split()
    )


def title_key(value):
    return re.sub(r"^(?:thu tuc |ho so )", "", fold(value))


def region_text(jurisdiction):
    text = fold(jurisdiction)
    text = re.sub(r"\b(?:tp hcm|tphcm|tp ho chi minh)\b", "ho chi minh", text)
    # Routing alias only, not an address. Official HCM publication 2320/QĐ-UBND
    # lists Phường Tăng Nhơn Phú within HCM; no cross-province address inference.
    if text in {"tang nhon phu", "phuong tang nhon phu"}:
        text += " ho chi minh"
    return text


def source_scope(item, jurisdiction):
    if item.get("state") not in {"ACTIVE", "UPDATED"}:
        return None
    if item.get("type") == "STANDARD":
        return "NATIONAL"
    publisher = fold(item.get("departmentPromulgate"))
    region = re.sub(r"^(?:ubnd|uy ban nhan dan) (?:tinh|thanh pho) ", "", publisher)
    if (
        region
        and region != publisher
        and re.search(r"\b" + re.escape(region) + r"\b", region_text(jurisdiction))
    ):
        return "PROVINCE"
    return None


def select(items, name):
    exact = [x for x in items if title_key(x.get("name")) == title_key(name)]
    if len(exact) == 1:
        return exact[0], "NORMALIZED_TITLE"
    if len(exact) > 1:
        raise portal.AmbiguousIdentity()
    identity = portal._select_identity(items, name)
    if identity:
        candidate = next(x for x in items if x["id"] == identity[0])
        # Similarity alone must not erase a differentiating action/population.
        expected, actual = title_key(name), title_key(candidate["name"])
        for qualifier in (
            "cap lai",
            "cap doi",
            "lan dau",
            "nuoc ngoai",
            "to chuc",
            "ca nhan",
            "cham dut",
            "tam ngung",
        ):
            if (qualifier in expected) != (qualifier in actual):
                return None
        return candidate, identity[2]
    return None


def cache_get(cache, key):
    entry = cache.get(key)
    if not entry:
        return None
    deadline, value = entry
    if time.monotonic() >= deadline:
        del cache[key]
        return None
    cache.move_to_end(key)
    return value


def cache_put(cache, key, value, ttl=TTL):
    cache[key] = (time.monotonic() + ttl, value)
    cache.move_to_end(key)
    while len(cache) > CAPACITY:
        cache.popitem(last=False)


async def lookup(
    request, *, enabled=False, timeout=20, fetch=None, document_reader=None, loan_reader=None
):
    now = datetime.now(timezone.utc)
    trace = {"search_pages": 0, "details": 0, "retries": 0, "document": False}

    def result(status, note, **kwargs):
        return LookupResult(
            status=status,
            procedure_id=request.procedure_id,
            field=request.field,
            jurisdiction=request.jurisdiction,
            checked_at=datetime.now(timezone.utc),
            retrieval_note=note,
            **kwargs,
        )

    if not enabled:
        return result("ERROR", "DVC_REALTIME_DISABLED")
    if request.as_of != now.date():
        return result("NOT_FOUND", "DVC_CURRENT_ONLY_NO_HISTORICAL_OR_FUTURE_ASSERTION")
    if request.field not in SUPPORTED:
        return result("NOT_FOUND", "LOOKUP_FIELD_PAUSED")
    production = fetch is None and document_reader is None and loan_reader is None
    key = request.model_dump_json()
    cached = cache_get(CACHE, key) if production else None
    if cached:
        return cached.model_copy(update={"checked_at": now, "cache_hit": True}, deep=True)
    fetch = fetch or portal.fetch_json
    document_reader = document_reader or read_land_rules
    loan_reader = loan_reader or read_loan_rules

    async def post(url, payload):
        for attempt in range(2):
            try:
                final, document = await asyncio.to_thread(fetch, url, payload, 4)
                official_url(final)
                return portal._api_data(document)
            except (OSError, TimeoutError):
                if attempt:
                    raise
                trace["retries"] += 1

    async def search(query):
        cursor, seen, items = "", set(), []
        for _ in range(MAX_PAGES):
            data = await post(
                portal.SEARCH_URL,
                {"limit": 20, "lastId": cursor, "q": query, "categoryId": "", "departmentCode": ""},
            )
            trace["search_pages"] += 1
            page = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page, list) or len(page) > 20:
                raise ValueError("SEARCH_SCHEMA")
            items.extend(
                x
                for x in page
                if isinstance(x, dict)
                and isinstance(x.get("id"), str)
                and isinstance(x.get("name"), str)
            )
            next_cursor = data.get("lastId")
            if (
                len(page) < 20
                or not isinstance(next_cursor, str)
                or next_cursor in seen
                or len(next_cursor) > 200
            ):
                break
            seen.add(next_cursor)
            cursor = next_cursor
        return list({x["id"]: x for x in items}.values())

    async def document_fallback():
        name = title_key(request.procedure_name)
        if (
            request.field == "receiving_authority"
            and name.startswith(
                "vay von ho tro tao viec lam duy tri va mo rong viec lam "
                "tu quy quoc gia ve viec lam doi voi "
            )
            and name.endswith(("nguoi lao dong", "co so san xuat kinh doanh"))
        ):
            trace["document"] = True
            doc = cache_get(DOCUMENT_CACHE, LOAN_PDF_URL) if production else None
            if doc is None:
                doc = (await asyncio.to_thread(loan_reader), datetime.now(timezone.utc))
                if production:
                    cache_put(DOCUMENT_CACHE, LOAN_PDF_URL, doc)
            rules, checked = doc
            return result(
                "PARTIAL",
                "OFFICIAL_DOCUMENT_CURRENT; UNREVIEWED_WEB",
                value=rules[request.field],
                source_title="Nghị định 338/2025/NĐ-CP — Điều 11.1",
                source_url=LOAN_PDF_URL,
                matched_procedure_name=request.procedure_name,
                scope="NATIONAL",
                source_checked_at=checked,
                limitations=[
                    "Nguồn xác nhận cơ quan Ngân hàng Chính sách xã hội; "
                    "chưa xác minh chi nhánh/điểm giao dịch cụ thể tại địa phương.",
                    "Đây là quy định hiện tại của chương trình hỗ trợ việc làm; "
                    "không tự thay thế các mục khác của hồ sơ trong dataset cũ.",
                ],
            )
        if (
            request.field not in {"receiving_authority", "fees"}
            or not name.startswith("dang ky dat dai tai san gan lien voi dat lan dau")
            or "ho chi minh" not in region_text(request.jurisdiction)
        ):
            return None
        trace["document"] = True
        doc = cache_get(DOCUMENT_CACHE, PDF_URL) if production else None
        if doc is None:
            doc = (await asyncio.to_thread(document_reader), datetime.now(timezone.utc))
            if production:
                cache_put(DOCUMENT_CACHE, PDF_URL, doc)
        rules, checked = doc
        limitation = (
            "Nguồn xác nhận cơ quan tiếp nhận theo quy định TP.HCM; "
            "chưa xác minh địa chỉ đường/số nhà cụ thể."
            if request.field == "receiving_authority"
            else "Văn bản dẫn sang quy định phí và nghị quyết HĐND; "
            "chưa xác minh mức tiền và điều kiện miễn/giảm áp dụng. Không có nghĩa là miễn phí."
        )
        return result(
            "PARTIAL",
            "OFFICIAL_DOCUMENT_CURRENT; UNREVIEWED_WEB",
            value=rules[request.field],
            source_title="Quyết định 44/2026/QĐ-UBND TP.HCM — "
            + ("Phụ lục IV, A.I" if request.field == "receiving_authority" else "Điều 3.3"),
            source_url=PDF_URL,
            matched_procedure_name=request.procedure_name,
            scope="PROVINCE",
            limitations=[limitation],
            source_checked_at=checked,
        )

    async def run():
        # Local rules are better evidence for these two fields than similarly
        # named records from other provinces. Failure still permits portal lookup.
        try:
            document_result = await document_fallback()
            if document_result:
                return document_result
        except Exception as exc:
            logger.info("lookup_document_fallback_failed type=%s", type(exc).__name__)
        items = await search(request.procedure_name)
        filtered = [x for x in items if source_scope(x, request.jurisdiction)]
        # Prefer provincial publication over the standard national template.
        provincial = [x for x in filtered if source_scope(x, request.jurisdiction) == "PROVINCE"]
        try:
            selected = select(provincial, request.procedure_name) or select(
                filtered, request.procedure_name
            )
        except portal.AmbiguousIdentity:
            return result("AMBIGUOUS", "DVC_MULTIPLE_EXACT_PUBLIC_API_RESULTS")
        if not selected:
            # Strip only generic heading prefixes. Never remove population/action qualifiers.
            query = re.sub(r"^(?:thủ tục |hồ sơ )", "", request.procedure_name, flags=re.I).strip()
            fallback = query if query != request.procedure_name else portal._fallback_query(query)
            if fallback and portal.norm(fallback) != portal.norm(request.procedure_name):
                items.extend(await search(fallback))
                items = list({x["id"]: x for x in items}.values())
                filtered = [x for x in items if source_scope(x, request.jurisdiction)]
                try:
                    selected = select(filtered, request.procedure_name)
                except portal.AmbiguousIdentity:
                    return result("AMBIGUOUS", "DVC_MULTIPLE_EXACT_PUBLIC_API_RESULTS")
        if not selected:
            alternatives = portal._variant_titles(filtered, request.procedure_name)
            if alternatives:
                return result(
                    "AMBIGUOUS",
                    "DVC_PUBLISHED_VARIANTS_REQUIRE_USER_CHOICE",
                    alternatives=alternatives,
                )
            note = (
                "DVC_WRONG_OR_UNKNOWN_JURISDICTION"
                if items and not filtered
                else "DVC_NO_UNIQUE_PUBLIC_API_TITLE_MATCH"
            )
            return result("NOT_FOUND", note)
        item, mode = selected
        trace["details"] += 1
        detail = await post(portal.DETAIL_URL, {"id": item["id"]})
        if not isinstance(detail, dict) or portal.norm(detail.get("name")) != portal.norm(
            item["name"]
        ):
            raise ValueError("DETAIL_IDENTITY")
        if detail.get("id") is not None and detail["id"] != item["id"]:
            raise ValueError("DETAIL_IDENTITY")
        if detail.get("state") and detail["state"] not in {"ACTIVE", "UPDATED"}:
            return result("NOT_FOUND", "DVC_DETAIL_INACTIVE")
        scope = source_scope(item, request.jurisdiction)
        value = portal.extract_value(detail, request)
        # The legacy extractor caps text. Never present a possibly cut condition
        # as complete evidence; fail closed at that boundary in the v2 path.
        if value and len(value) >= 8000:
            return result("NOT_FOUND", "DVC_FIELD_EXCEEDS_SAFE_EVIDENCE_LIMIT")
        limitations = []
        if request.field == "receiving_authority":
            if value:
                scope = "LOCALITY"
            else:
                # Do not mix a generic authority with an unverified street address.
                groups = detail.get("unitGroupsExecuting") or []
                if not isinstance(groups, list) or len(groups) > 100:
                    raise ValueError("DETAIL_GROUPS_SCHEMA")
                value = "\n".join(
                    portal._unique(
                        [
                            portal._text(x.get("name"), limit=500)
                            for x in groups
                            if isinstance(x, dict)
                        ]
                    )
                )
                limitations.append(
                    "Chỉ xác nhận cơ quan chung của thủ tục; "
                    "chưa xác minh nơi nộp/địa chỉ cụ thể tại địa phương bạn nêu."
                )
        elif scope == "NATIONAL":
            limitations.append(
                "Đây là thông tin thủ tục cấp quốc gia, "
                "chưa xác minh quy định riêng của địa phương bạn nêu."
            )
        if (
            request.field == "fees"
            and value
            and re.search(r"theo quy dinh|nghi quyet|hoi dong nhan dan|tuy|mien", fold(value))
        ):
            if not re.search(r"\b\d[\d.,]*\s*(?:đồng|vnđ|vnd)\b", value, re.I):
                limitations.append(
                    "Chưa có mức tiền áp dụng được xác minh; "
                    "cần đối chiếu nghị quyết và điều kiện miễn/giảm, không mặc định miễn phí."
                )
        if not value:
            return result("NOT_FOUND", "DVC_FIELD_OR_EXPLICIT_LOCALITY_NOT_VERIFIED")
        return result(
            "PARTIAL" if limitations else "FOUND",
            "DVC_PUBLIC_JSON_API_" + mode + "; UNREVIEWED_WEB",
            value=value,
            source_title=item["name"],
            source_url=portal.PUBLIC_DETAIL_ROOT + quote(item["id"], safe=""),
            matched_procedure_name=request.procedure_name,
            scope=scope,
            limitations=limitations,
            source_checked_at=datetime.now(timezone.utc),
        )

    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            answer = await run()
    except TimeoutError:
        answer = result("ERROR", "DVC_TIMEOUT")
    except Exception:
        answer = result("ERROR", "DVC_FETCH_OR_SCHEMA_ERROR")
    logger.info(
        "lookup_v2 status=%s field=%s elapsed_ms=%d trace=%s",
        answer.status,
        request.field,
        int((time.monotonic() - started) * 1000),
        trace,
    )
    if production and answer.status in {"FOUND", "PARTIAL", "NOT_FOUND", "AMBIGUOUS"}:
        cache_put(CACHE, key, answer, TTL if answer.status in {"FOUND", "PARTIAL"} else 15)
    return answer
