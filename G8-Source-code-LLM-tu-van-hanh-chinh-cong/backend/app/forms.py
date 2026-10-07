"""Read-only, hash-pinned public forms. No search, fuzzy matching or model writes.

Only exact reviewed checklist items can suggest downloads. Missing, stale or
tampered artifacts fail closed without disabling chat/checklist state.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import quote, urlsplit

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings
from app.errors import APIError

router = APIRouter(prefix="/api/v1/forms", tags=["reviewed-public-forms"])
MAX_BYTES = 8 * 1024 * 1024


def official_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not (
            host.endswith(".gov.vn")
            or host.endswith(".chinhphu.vn")
            or host == "chinhphu.vn"
            or host.endswith(".cdnchinhphu.vn")
        )
    ):
        raise ValueError("Unofficial form source")
    return value


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    title: str = Field(min_length=5, max_length=400)
    filename: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}\.pdf$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(gt=0, le=MAX_BYTES)
    form_number: str = Field(min_length=1, max_length=100)
    legal_basis: str = Field(min_length=3, max_length=200)
    source_url: str
    original_download_url: str
    crosscheck_url: str
    issuing_authority: str = Field(min_length=3, max_length=200)
    jurisdiction: str = Field(min_length=3, max_length=100)
    applicability: str = Field(min_length=5, max_length=800)
    extraction_note: str = Field(min_length=5, max_length=500)
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_pages: list[int] = Field(min_length=1, max_length=20)
    checked_at: datetime
    review_due_at: datetime

    _urls = field_validator("source_url", "original_download_url", "crosscheck_url")(official_url)


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    procedure_id: str = Field(pattern=r"^d2_title_[a-f0-9]{12}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    item_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    form_ids: list[str] = Field(min_length=1, max_length=20)


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^reviewed-forms-v1$")
    corpus_version: str
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    forms: list[Form] = Field(max_length=100)
    bindings: list[Binding] = Field(max_length=300)


def item_digest(text: str, section: str) -> str:
    return hashlib.sha256(json.dumps([text, section], ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=4)
def _parse(raw: bytes) -> Registry:
    registry = Registry.model_validate_json(raw)
    ids = [form.id for form in registry.forms]
    keys = [(b.procedure_id, b.source_digest, b.item_digest) for b in registry.bindings]
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise ValueError("Duplicate form or binding")
    if any(set(b.form_ids) - set(ids) for b in registry.bindings):
        raise ValueError("Unreviewed binding")
    for form in registry.forms:
        if (
            form.checked_at.tzinfo is None
            or form.review_due_at.tzinfo is None
            or form.review_due_at <= form.checked_at
            or any(n < 1 for n in form.original_pages)
        ):
            raise ValueError("Invalid review dates/pages")
    return registry


def registry() -> Registry | None:
    settings = get_settings()
    if not settings.g6_forms_enabled:
        return None
    try:
        root = settings.g6_forms_path.resolve()
        path = (root / "manifest.json").resolve()
        if not path.is_relative_to(root) or path.stat().st_size > 512 * 1024:
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != settings.g6_forms_manifest_sha256:
            return None
        result = _parse(raw)
        if (
            result.corpus_version != settings.rag_corpus_version
            or result.corpus_sha256 != settings.g3_corpus_sha256
        ):
            return None
        return result
    except (OSError, ValueError):
        # A forms outage must not prevent users updating their checklist.
        return None


def file_bytes(form: Form) -> bytes | None:
    try:
        now = datetime.now(timezone.utc)
        if not form.checked_at <= now < form.review_due_at:
            return None
        catalog_root = get_settings().g6_forms_path.resolve()
        root = (catalog_root / "verified").resolve()
        path = (root / form.filename).resolve()
        if (
            not root.is_relative_to(catalog_root)
            or not path.is_relative_to(root)
            or path.stat().st_size != form.bytes
        ):
            return None
        raw = path.read_bytes()
        if raw.startswith(b"%PDF-") and hashlib.sha256(raw).hexdigest() == form.sha256:
            return raw
    except OSError:
        pass
    return None


def for_item(row, item, catalog: Registry | None) -> list[dict]:
    if catalog is None or item.status != "missing" or row.corpus_version != catalog.corpus_version:
        return []
    digest = item_digest(item.text, item.section)
    ids = next(
        (
            b.form_ids
            for b in catalog.bindings
            if b.procedure_id == row.procedure_id
            and b.source_digest == row.source_digest
            and b.item_digest == digest
        ),
        [],
    )
    result = []
    for form in catalog.forms:
        if form.id not in ids or file_bytes(form) is None:
            continue
        result.append(
            form.model_dump(
                include={
                    "id",
                    "title",
                    "form_number",
                    "legal_basis",
                    "source_url",
                    "issuing_authority",
                    "jurisdiction",
                    "applicability",
                    "extraction_note",
                    "checked_at",
                    "review_due_at",
                    "bytes",
                },
                mode="json",
            )
        )
    return result


def download_filename(form: Form) -> str:
    """Human-readable download name; the stored hash-pinned path stays internal."""
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "-", form.title)
    title = re.sub(r"\s+", " ", title).strip(" .-")[:160].rstrip(" .")
    return (title or form.id) + ".pdf"


def content_disposition(form: Form) -> str:
    name = download_filename(form)
    fallback = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    fallback = fallback.encode("ascii", "ignore").decode()
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


@router.get("/{form_id}/download")
def download(form_id: str):
    # These are public blank government forms, never uploaded user documents.
    # No user-supplied URL or path is ever opened, and no runtime outbound fetch.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", form_id):
        raise APIError(404, "FORM_UNAVAILABLE", "Chưa có mẫu tải đã đối chiếu.")
    catalog = registry()
    form = next((f for f in catalog.forms if f.id == form_id), None) if catalog else None
    content = file_bytes(form) if form else None
    if content is None:
        raise APIError(404, "FORM_UNAVAILABLE", "Mẫu chưa sẵn sàng; hãy kiểm tra nguồn gốc.")
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(form),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox",
        },
    )
