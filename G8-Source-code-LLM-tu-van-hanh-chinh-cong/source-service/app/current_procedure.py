"""Bounded, read-only lookup against the public National Public Service API.

The portal is a SPA. Its public procedure search and detail screens call the JSON
endpoints below. We use only those observed public contracts, require an exact
procedure title, validate every URL, cap response sizes, and fail closed.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.error import HTTPError
from urllib.parse import quote, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.realtime_contract import LookupRequest, LookupResult, official_url

API_ROOT = "https://dichvucong.gov.vn/api/v1"
SEARCH_URL = API_ROOT + "/submitting/formality/list-all-public-formality-by-citizen"
DETAIL_URL = API_ROOT + "/configuring/formality/get-formality-by-citizen"
PUBLIC_DETAIL_ROOT = "https://dichvucong.gov.vn/thu-tuc-hanh-chinh/"
MAX_BYTES = 2_097_152
MAX_ITEMS = 20

METHOD_LABELS = {"ONLINE": "Trực tuyến", "POSTAL": "Qua bưu chính", "DIRECT": "Trực tiếp"}
TIME_UNIT_LABELS = {
    "WORKING_DAY": "ngày làm việc",
    "DAY": "ngày",
    "WORKING_HOUR": "giờ làm việc",
    "HOUR": "giờ",
}


class AmbiguousIdentity(ValueError):
    pass


def norm(text: object) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = re.sub(r"\s*([,;/()])\s*", r"\1", value)
    return " ".join(value.split()).strip(" :")


def _text(value: object, *, limit: int = 8000) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url: str, payload: dict, timeout: float = 4) -> tuple[str, dict]:
    """POST JSON with hop-by-hop allowlist checks and strict size/content limits."""
    opener = build_opener(NoRedirect())
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    for _ in range(4):
        official_url(url)
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": "HCC-CurrentProcedure/2.0",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code in {301, 302, 307, 308}:
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise ValueError("DVC_REDIRECT_MISSING_LOCATION") from None
                url = official_url(urljoin(url, location))
                continue
            raise
        with response:
            content_type = response.headers.get_content_type()
            if content_type not in {"application/json", "application/problem+json"}:
                raise ValueError("DVC_UNSUPPORTED_CONTENT")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("DVC_RESPONSE_TOO_LARGE")
            decoded = json.loads(data.decode("utf-8", errors="strict"))
            if not isinstance(decoded, dict):
                raise ValueError("DVC_INVALID_JSON_ROOT")
            return official_url(response.geturl()), decoded
    raise ValueError("DVC_TOO_MANY_REDIRECTS")


def _api_data(document: dict) -> object:
    if document.get("code") != "OK" or "data" not in document:
        raise ValueError("DVC_API_ERROR")
    return document["data"]


def _fallback_query(title: str) -> str:
    first_clause = re.split(r"[/;]", title, maxsplit=1)[0].strip()
    words = first_clause.split()
    return " ".join(words[:8]) if len(words) >= 3 else ""


def _select_identity(items: list[dict], title: str) -> tuple[str, str, str] | None:
    """Return a unique exact or high-similarity identity; never pick a close tie."""
    valid = [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and 1 <= len(item["id"]) <= 200
        and isinstance(item.get("name"), str)
        and 1 <= len(item["name"]) <= 500
    ]
    exact = list(
        dict.fromkeys(
            (item["id"], item["name"])
            for item in valid
            if norm(item["name"]) == norm(title)
        )
    )
    if len(exact) == 1:
        return exact[0][0], exact[0][1], "EXACT_TITLE"
    if len(exact) > 1:
        raise AmbiguousIdentity("DVC_MULTIPLE_EXACT_PUBLIC_API_RESULTS")
    scored = sorted(
        (
            SequenceMatcher(None, norm(title), norm(item["name"])).ratio(),
            item["id"],
            item["name"],
        )
        for item in valid
    )
    if not scored:
        return None
    best = scored[-1]
    runner_up = scored[-2][0] if len(scored) > 1 else 0.0
    if best[0] < 0.88 or best[0] - runner_up < 0.08:
        return None
    return best[1], best[2], "UNIQUE_HIGH_SIMILARITY_TITLE"


def _variant_titles(items: list[dict], title: str) -> list[str]:
    """Suggest only distinct published names under an exact generic heading."""
    prefix = norm(title) + " "
    names = _unique(
        [
            _text(item["name"], limit=500)
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
            and norm(item["name"]).startswith(prefix)
        ]
    )
    return names if 2 <= len(names) <= 5 else []


def _method_lines(detail: dict, *, include_description: bool) -> list[str]:
    methods = detail.get("executionMethods")
    if not isinstance(methods, list) or len(methods) > 20:
        return []
    lines = []
    for item in methods:
        if not isinstance(item, dict):
            continue
        label = METHOD_LABELS.get(item.get("submissionMethod"), _text(item.get("submissionMethod")))
        quantity = item.get("processingTime")
        unit = TIME_UNIT_LABELS.get(item.get("processingTimeUnit"), "")
        duration = ""
        if isinstance(quantity, (int, float)) and not isinstance(quantity, bool) and unit:
            duration = f"{quantity:g} {unit}"
        description = _text(item.get("description"), limit=2500) if include_description else ""
        content = ". ".join(part for part in (duration, description) if part)
        if label and content:
            lines.append(f"{label}: {content}")
        elif label:
            lines.append(label)
    return _unique(lines)


def _profile_lines(detail: dict) -> list[str]:
    cases = detail.get("executionCases")
    if not isinstance(cases, list) or len(cases) > 30:
        return []
    lines = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        heading = _text(case.get("name"), limit=500)
        if heading:
            lines.append(heading)
        components = case.get("profileComponents")
        if not isinstance(components, list) or len(components) > 100:
            continue
        for component in components:
            if isinstance(component, dict):
                name = _text(component.get("name"), limit=1500)
                if name:
                    lines.append(name)
    return _unique(lines)


def extract_value(detail: dict, request: LookupRequest) -> str | None:
    """Extract only structured fields rendered by the official procedure page."""
    field = request.field
    values: list[str] = []
    if field == "processing_times":
        values = _method_lines(detail, include_description=False)
        methods = detail.get("executionMethods")
        if isinstance(methods, list) and len(methods) <= 20:
            notes = _unique(
                [
                    _text(item.get("description"), limit=2500)
                    for item in methods
                    if isinstance(item, dict)
                ]
            )
            values.extend(f"Ghi chú: {note}" for note in notes)
        if not values:
            cases = detail.get("cases")
            if isinstance(cases, list) and len(cases) <= 50:
                for case in cases:
                    if not isinstance(case, dict):
                        continue
                    day = case.get("processingDay")
                    if isinstance(day, dict):
                        qty = day.get("qty")
                        unit = TIME_UNIT_LABELS.get(day.get("type"), "")
                        if isinstance(qty, (int, float)) and not isinstance(qty, bool) and unit:
                            values.append(f"{_text(case.get('name'), limit=500)}: {qty:g} {unit}")
    elif field == "receiving_authority":
        values.append(_text(detail.get("dossierReceivingAddresses"), limit=5000))
        for key in ("departmentsExecuting", "unitGroupsExecuting"):
            entries = detail.get(key)
            if isinstance(entries, list) and len(entries) <= 100:
                values.extend(
                    _text(item.get("name"), limit=500)
                    for item in entries
                    if isinstance(item, dict)
                )
        values = _unique(values)
        locality = norm(request.jurisdiction)
        locality_pattern = r"(?<!\w)" + re.escape(locality) + r"(?!\w)"
        if not any(re.search(locality_pattern, norm(value)) for value in values):
            return None
    elif field == "submission_methods":
        values = _method_lines(detail, include_description=True)
    elif field == "required_documents":
        values = _profile_lines(detail)
    elif field == "fees":
        methods = detail.get("executionMethods")
        if isinstance(methods, list) and len(methods) <= 20:
            for method in methods:
                if not isinstance(method, dict):
                    continue
                method_label = METHOD_LABELS.get(method.get("submissionMethod"), "")
                fees = method.get("fees")
                if not isinstance(fees, list) or len(fees) > 50:
                    continue
                for fee in fees:
                    if not isinstance(fee, dict):
                        continue
                    amount = fee.get("value")
                    description = _text(fee.get("description"), limit=1000)
                    parts = []
                    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                        # Public API can use 0 as a placeholder next to a fee
                        # resolution/conditional description. Preserve that text;
                        # do not prepend a misleading universal zero amount.
                        if amount != 0 or not description:
                            parts.append(f"{amount:g} đồng")
                    if description:
                        parts.append(description)
                    if parts:
                        values.append(": ".join(([method_label] if method_label else []) + parts))
    elif field == "steps":
        steps = detail.get("executionSteps")
        if isinstance(steps, list) and len(steps) <= 50:
            values = [
                ": ".join(
                    part
                    for part in (
                        _text(step.get("name"), limit=500),
                        _text(step.get("description"), limit=2500),
                    )
                    if part
                )
                for step in steps
                if isinstance(step, dict)
            ]
    elif field == "legal_bases":
        entries = detail.get("legalBasisesDetails")
        if isinstance(entries, list) and len(entries) <= 100:
            values = [
                " - ".join(
                    part
                    for part in (
                        _text(item.get("code"), limit=200),
                        _text(item.get("name"), limit=1000),
                    )
                    if part
                )
                for item in entries
                if isinstance(item, dict)
            ]
    elif field == "applicant_scope":
        values.append(_text(detail.get("note"), limit=3000))
        entries = detail.get("subjectTypesDetails")
        if isinstance(entries, list) and len(entries) <= 100:
            values.extend(
                _text(item.get("name"), limit=500)
                for item in entries
                if isinstance(item, dict)
            )
    value = "\n".join(_unique(values)).strip()
    return value[:8000] or None


async def lookup(request: LookupRequest, *, enabled=False, timeout=6, fetch=fetch_json):
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
    if request.as_of != datetime.now(timezone.utc).date():
        return result("NOT_FOUND", "DVC_CURRENT_ONLY_NO_HISTORICAL_OR_FUTURE_ASSERTION")
    try:
        async with asyncio.timeout(timeout):

            async def post(url: str, payload: dict) -> dict:
                official_url(url)
                final_url, document = await asyncio.to_thread(fetch, url, payload, min(timeout, 4))
                official_url(final_url)
                return document

            async def search(query: str) -> list[dict]:
                document = await post(
                    SEARCH_URL,
                    {
                        "limit": MAX_ITEMS,
                        "lastId": "",
                        "q": query,
                        "categoryId": "",
                        "departmentCode": "",
                    },
                )
                data = _api_data(document)
                if not isinstance(data, dict):
                    raise ValueError("DVC_INVALID_SEARCH_DATA")
                found = data.get("items")
                if not isinstance(found, list) or len(found) > MAX_ITEMS:
                    raise ValueError("DVC_INVALID_SEARCH_ITEMS")
                return found

            items = await search(request.procedure_name)
            try:
                identity = _select_identity(items, request.procedure_name)
            except AmbiguousIdentity:
                return result("AMBIGUOUS", "DVC_MULTIPLE_EXACT_PUBLIC_API_RESULTS")
            fallback_query = _fallback_query(request.procedure_name)
            if identity is None and fallback_query and norm(fallback_query) != norm(
                request.procedure_name
            ):
                fallback_items = await search(fallback_query)
                known_ids = {
                    item.get("id") for item in items if isinstance(item, dict)
                }
                items.extend(
                    item
                    for item in fallback_items
                    if isinstance(item, dict) and item.get("id") not in known_ids
                )
                try:
                    identity = _select_identity(items, request.procedure_name)
                except AmbiguousIdentity:
                    return result("AMBIGUOUS", "DVC_MULTIPLE_EXACT_PUBLIC_API_RESULTS")
            if identity is None:
                alternatives = _variant_titles(items, request.procedure_name)
                if alternatives:
                    return result(
                        "AMBIGUOUS", "DVC_PUBLISHED_VARIANTS_REQUIRE_USER_CHOICE",
                        alternatives=alternatives,
                    )
                return result("NOT_FOUND", "DVC_NO_UNIQUE_PUBLIC_API_TITLE_MATCH")

            remote_id, remote_name, identity_mode = identity
            detail_document = await post(DETAIL_URL, {"id": remote_id})
            detail = _api_data(detail_document)
            if not isinstance(detail, dict) or norm(detail.get("name")) != norm(remote_name):
                raise ValueError("DVC_DETAIL_IDENTITY_MISMATCH")
            value = extract_value(detail, request)
            if not value:
                return result("NOT_FOUND", "DVC_FIELD_OR_EXPLICIT_LOCALITY_NOT_VERIFIED")
            source_url = official_url(PUBLIC_DETAIL_ROOT + quote(remote_id, safe=""))
            return result(
                "FOUND",
                f"DVC_PUBLIC_JSON_API_{identity_mode}; UNREVIEWED_WEB",
                value=value,
                source_title=_text(detail.get("name"), limit=500),
                source_url=source_url,
            )
    except TimeoutError:
        return result("ERROR", "DVC_TIMEOUT")
    except Exception:
        return result("ERROR", "DVC_FETCH_OR_SCHEMA_ERROR")
