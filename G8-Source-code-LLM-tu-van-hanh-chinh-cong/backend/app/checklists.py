"""User-owned preparation state. Only reviewed local evidence seeds items.

Neither the model nor a web lookup can mark documents verified. Conversation
deletion leaves the user's preparation work intact (nullable origin links).
"""

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app import forms
from app.auth import current_user
from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import Base, Conversation, Message, User, utcnow
from app.rag.g3.runtime import load_active_dataset

ItemStatus = Literal["unknown", "have", "missing", "not_applicable"]


class PreparationChecklist(Base):
    __tablename__ = "preparation_checklists"
    __table_args__ = (UniqueConstraint("user_id", "origin_key", name="uq_checklist_origin"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    origin_key: Mapped[UUID] = mapped_column()
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    procedure_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    corpus_version: Mapped[str] = mapped_column(String(200))
    source_digest: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreparationItem(Base):
    __tablename__ = "preparation_items"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    checklist_id: Mapped[UUID] = mapped_column(
        ForeignKey("preparation_checklists.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    status_source: Mapped[str] = mapped_column(String(24), default="NOT_PROVIDED")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreparationEvent(Base):
    __tablename__ = "preparation_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    checklist_id: Mapped[UUID] = mapped_column(
        ForeignKey("preparation_checklists.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("preparation_items.id", ondelete="CASCADE")
    )
    revision: Mapped[int]
    action: Mapped[str] = mapped_column(String(30))
    before: Mapped[str | None] = mapped_column(String(20))
    after: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreateChecklist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: UUID
    procedure_id: str | None = None


class UpdateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    status: ItemStatus


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


router = APIRouter(prefix="/api/v1/checklists", tags=["preparation-checklists"])


def get_dataset():
    settings = get_settings()
    if not settings.g3_d2_enabled:
        raise APIError(503, "CHECKLIST_UNAVAILABLE", "Nguồn hồ sơ chưa sẵn sàng.")
    try:
        return load_active_dataset(
            str(settings.g3_private_dataset_path),
            settings.rag_corpus_version,
            settings.g3_expected_rows,
            settings.g3_corpus_sha256,
        )
    except (ValueError, OSError) as exc:
        raise APIError(503, "CHECKLIST_UNAVAILABLE", "Nguồn hồ sơ chưa sẵn sàng.") from exc


_ITEM_MARKER = re.compile(r"^(?:[-•+*]|\d+\s*[./)\-]|[a-zđ][)])\s*")
_DOCUMENT_START = re.compile(
    r"^(?:(?:Bản chính|Bản sao|Bản gốc)\s+)?(?:Đơn\b|Tờ khai\b|Bản khai\b|"
    r"Giấy\b|Văn bản\b|Biên bản\b|Danh sách\b|CCCD\b|Căn cước\b|Chứng minh\b|"
    r"Huân chương\b|Huy chương\b|Quyết định\b|Bằng\b|Công văn\b|Nội quy\b|"
    r"Một trong các (?:giấy tờ|loại giấy tờ)\b)", re.IGNORECASE,
)


def _source_lines(content):
    # Recognize an enumerated list flattened onto one line. Dates, citations and
    # ordinary semicolons are not item boundaries.
    content = re.sub(r";[ \t]+(?=\d{1,2}[./)\-]\s*[^\d\s])", ";\n", content)
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # A semicolon-separated list of documents has no list markers. Keep
        # alternatives ('one of ...') and qualifications together verbatim.
        start = depth = 0
        for index, char in enumerate(line):
            depth += char == "("
            depth -= char == ")" and depth > 0
            if char != ";" or depth:
                continue
            current, following = line[start:index + 1], line[index + 1:].lstrip()
            if (not _ITEM_MARKER.match(line) and following[:1].isupper()
                    and _DOCUMENT_START.match(following)
                    and not re.search(r"\b(?:một trong|hoặc)\b", current, re.IGNORECASE)):
                yield current.strip()
                start = index + 1
        yield line[start:].strip()


def _section_line(line):
    """Return the verbatim heading and optional inline requirement."""
    prefix = re.match(r"^(?:[*•+-]\s*|(?:[IVX]+|\d+)[./]\s*)", line)
    body = line[prefix.end():] if prefix else line
    heading = re.match(r"^(?:Thành phần|Đối với|Trường hợp|Các giấy tờ khác kèm theo|Hồ sơ)",
                       body, re.IGNORECASE)
    if not heading:
        return None
    if line.endswith(":"):
        return line, ""
    marked = prefix and (line.startswith("*") or re.match(r"^[IVX]+[./]", line))
    if marked:
        head, separator, rest = line.partition(":")
        return (head + separator, rest.strip()) if separator else (line, "")
    if (prefix and body.lower().startswith("đối với")
            and not re.search(r"[:.;]", body) and len(body) < 200):
        return line, ""
    if body.lower().startswith("thành phần"):
        return line, ""
    return None


def source_items(fragments):
    """Keep verbatim clauses and branch headings, never infer applicability.

    Split structural lists and standalone document lines, preserving wrapped
    text, alternatives and conditions. Never invent individual requirements.
    Repeated documents in different sections stay distinct.
    """
    items = []
    for fid, content in fragments:
        section = ""
        for line in _source_lines(content):
            heading = _section_line(line)
            if heading:
                section, line = heading
                if not line:
                    continue
            previous = items[-1]["text"] if items else ""
            wrapped = (previous.count("(") > previous.count(")")
                       or re.search(r"\b(?:của|và|hoặc|theo|gồm|kèm|từ)\s*$", previous,
                                    re.IGNORECASE))
            new_document = (_DOCUMENT_START.match(line) and not wrapped
                            and (_DOCUMENT_START.match(previous) or _ITEM_MARKER.match(previous)))
            if (
                items
                and items[-1]["evidence_ids"] == [fid]
                and items[-1]["section"] == section
                and not _ITEM_MARKER.match(line)
                and not new_document
                and not heading
            ):
                items[-1]["text"] += "\n" + line
            else:
                items.append(dict(text=line, section=section, evidence_ids=[fid]))
    if not items or len(items) > 150:
        raise APIError(422, "CHECKLIST_SOURCE_UNSUPPORTED", "Chưa tách được hồ sơ từ nguồn này.")
    return items


async def owned(db, checklist_id, user_id, *, lock=False):
    query = select(PreparationChecklist).where(
        PreparationChecklist.id == checklist_id, PreparationChecklist.user_id == user_id
    )
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy checklist.")
    return row


async def output(db, row):
    items = (
        await db.scalars(
            select(PreparationItem)
            .where(PreparationItem.checklist_id == row.id)
            .order_by(PreparationItem.position)
        )
    ).all()
    counts = {
        key: sum(i.status == key for i in items)
        for key in ("unknown", "have", "missing", "not_applicable")
    }

    def attach_forms():
        catalog = forms.registry()
        return {i.id: forms.for_item(row, i, catalog) for i in items}

    # Local PDF integrity I/O must not block the chat event loop.
    downloads = await asyncio.to_thread(attach_forms)
    return dict(
        id=row.id,
        title=row.title,
        procedure_id=row.procedure_id,
        conversation_id=row.conversation_id,
        corpus_version=row.corpus_version,
        source_changed=row.corpus_version != get_settings().rag_corpus_version,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        counts=counts,
        items=[
            dict(
                id=i.id,
                text=i.text,
                section=i.section,
                evidence_ids=i.evidence_ids,
                status=i.status,
                status_source=i.status_source,
                forms=downloads[i.id],
            )
            for i in items
        ],
        guidance=(
            f"Bạn đánh dấu còn thiếu {counts['missing']} mục; "
            f"{counts['unknown']} mục chưa rõ. Trạng thái do bạn cung cấp, "
            "chưa xác nhận hồ sơ đầy đủ hoặc hợp lệ."
        ),
    )


@router.post("", status_code=201)
async def create(body: CreateChecklist, user: User = Depends(current_user), db=Depends(get_db)):
    message = await db.scalar(
        select(Message)
        .join(Conversation)
        .where(
            Message.id == body.message_id,
            Conversation.user_id == user.id,
            Message.role == "assistant",
            Message.status == "completed",
        )
    )
    if message is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy câu trả lời của bạn.")
    g = message.grounding or {}
    if g.get("parts"):
        choices = [p for p in g["parts"] if p.get("procedure_id") == body.procedure_id]
        if len(choices) != 1:
            raise APIError(422, "PROCEDURE_REQUIRED", "Chọn thủ tục cần lưu checklist.")
        g = choices[0]["grounding"]
    origin_key = uuid5(message.id, body.procedure_id) if body.procedure_id else message.id
    # Lock owner for idempotent concurrent creates and the per-owner bound.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = await db.scalar(
        select(PreparationChecklist).where(
            PreparationChecklist.user_id == user.id, PreparationChecklist.origin_key == origin_key
        )
    )
    if existing is not None:
        return await output(db, existing)
    data = get_dataset()
    if g.get("status") not in {"ANSWER", "INSUFFICIENT_DATA"} or g.get("corpus_version") != data.version:
        raise APIError(409, "SOURCE_CHANGED", "Hãy hỏi lại hồ sơ để lấy nguồn hiện tại.")
    fids = [
        fid
        for c in g.get("checklist") or []
        if c.get("field") == "required_documents"
        for fid in c.get("evidence_ids", [])
    ]
    pids = {fid.split(":")[0] for fid in fids}
    if len(pids) != 1 or not fids:
        raise APIError(422, "DOCUMENTS_REQUIRED", "Hãy hỏi thành phần hồ sơ của một thủ tục trước.")
    pid = next(iter(pids))
    if body.procedure_id and body.procedure_id != pid:
        raise APIError(422, "PROCEDURE_MISMATCH", "Hồ sơ không thuộc thủ tục đã chọn.")
    procedure = data.procedures.get(pid, {})
    required = procedure.get("field_evidence", {}).get("required_documents", [])
    if pid not in data.accepted or set(required) != set(fids):
        raise APIError(409, "SOURCE_CHANGED", "Nguồn hồ sơ đã thay đổi, hãy hỏi lại.")
    fragments = [(fid, data.fragments[fid]["text"]) for fid in required]
    items = source_items(fragments)
    owned_ids = (
        await db.scalars(
            select(PreparationChecklist.id)
            .where(PreparationChecklist.user_id == user.id)
            .limit(100)
        )
    ).all()
    if len(owned_ids) >= 100:
        raise APIError(409, "CHECKLIST_LIMIT", "Bạn đã có 100 checklist; hãy xóa mục không dùng.")
    row = PreparationChecklist(
        user_id=user.id,
        origin_key=origin_key,
        conversation_id=message.conversation_id,
        procedure_id=pid,
        title=procedure["title"],
        corpus_version=data.version,
        source_digest=hashlib.sha256(repr(fragments).encode()).hexdigest(),
    )
    db.add(row)
    await db.flush()
    for position, item in enumerate(items):
        db.add(PreparationItem(checklist_id=row.id, position=position, **item))
    db.add(PreparationEvent(checklist_id=row.id, revision=1, action="created"))
    await db.commit()
    return await output(db, row)


@router.get("")
async def listing(user: User = Depends(current_user), db=Depends(get_db)):
    rows = (
        await db.scalars(
            select(PreparationChecklist)
            .where(PreparationChecklist.user_id == user.id)
            .order_by(PreparationChecklist.updated_at.desc())
            .limit(100)
        )
    ).all()
    return [await output(db, row) for row in rows]


@router.get("/{checklist_id}")
async def detail(checklist_id: UUID, user: User = Depends(current_user), db=Depends(get_db)):
    return await output(db, await owned(db, checklist_id, user.id))


def check_revision(row, revision):
    if row.revision != revision:
        raise APIError(409, "CHECKLIST_CONFLICT", "Checklist đã đổi ở tab khác. Hãy tải lại.")


@router.patch("/{checklist_id}/items/{item_id}")
async def change(
    checklist_id: UUID,
    item_id: UUID,
    body: UpdateItem,
    user: User = Depends(current_user),
    db=Depends(get_db),
):
    row = await owned(db, checklist_id, user.id, lock=True)
    check_revision(row, body.revision)
    item = await db.scalar(
        select(PreparationItem).where(
            PreparationItem.id == item_id, PreparationItem.checklist_id == row.id
        )
    )
    if item is None:
        raise APIError(404, "NOT_FOUND", "Không tìm thấy mục hồ sơ.")
    before = item.status
    item.status = body.status
    item.status_source = "NOT_PROVIDED" if body.status == "unknown" else "USER_REPORTED"
    row.revision += 1
    row.updated_at = item.updated_at = utcnow()
    db.add(
        PreparationEvent(
            checklist_id=row.id,
            item_id=item.id,
            revision=row.revision,
            action="user_update",
            before=before,
            after=item.status,
        )
    )
    await db.commit()
    return await output(db, row)


@router.post("/{checklist_id}/reset")
async def reset(
    checklist_id: UUID, body: RevisionInput, user: User = Depends(current_user), db=Depends(get_db)
):
    row = await owned(db, checklist_id, user.id, lock=True)
    check_revision(row, body.revision)
    items = (
        await db.scalars(select(PreparationItem).where(PreparationItem.checklist_id == row.id))
    ).all()
    for item in items:
        item.status, item.status_source, item.updated_at = "unknown", "NOT_PROVIDED", utcnow()
    row.revision += 1
    row.updated_at = utcnow()
    db.add(PreparationEvent(checklist_id=row.id, revision=row.revision, action="user_reset"))
    await db.commit()
    return await output(db, row)


@router.delete("/{checklist_id}", status_code=204)
async def remove(
    checklist_id: UUID, revision: int, user: User = Depends(current_user), db=Depends(get_db)
):
    row = await owned(db, checklist_id, user.id, lock=True)
    check_revision(row, revision)
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)
