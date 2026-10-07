"""Immutable source snapshots with explicit review, publish and rollback states."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

FIELD_SECTIONS = {
    "receiving_authority": "RECEIVING_AUTHORITY",
    "required_documents": "REQUIRED_DOCUMENTS",
    "fees": "FEES",
    "processing_times": "PROCESSING_TIME",
    "submission_methods": "SUBMISSION_METHODS",
    "steps": "STEPS",
    "legal_bases": "LEGAL_BASES",
    "applicant_scope": "APPLICANT_SCOPE",
}
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?84|0)(?:[ .-]?\d){9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
)


class SourceStoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SourceStoreError("G5_EFFECTIVE_DATE_INVALID") from exc
    if parsed.isoformat() != value:
        raise SourceStoreError("G5_EFFECTIVE_DATE_INVALID")
    return value


class SourceVersionStore:
    def __init__(self, *, data_dir: Path, corpus_dir: Path, review_token: str) -> None:
        self.data_dir = data_dir.resolve()
        self.corpus_dir = corpus_dir.resolve()
        if not review_token or len(review_token) < 12:
            raise SourceStoreError("G5_REVIEW_TOKEN_TOO_SHORT")
        self._review_token_hash = hashlib.sha256(review_token.encode()).digest()
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = (self.data_dir / "registry.json").resolve()
        if not self.registry_path.is_relative_to(self.data_dir):
            raise SourceStoreError("G5_STORE_PATH_INVALID")
        self.allowed_procedures = self._load_allowed_procedures()
        if not self.registry_path.exists():
            self._write(
                {
                    "schema_version": "g5-source-registry-v1",
                    "sequence": 0,
                    "active": {},
                    "snapshots": [],
                }
            )

    def _load_allowed_procedures(self) -> set[str]:
        path = self.corpus_dir / "normalized/procedures.jsonl"
        if not path.is_file():
            raise SourceStoreError("G5_CORPUS_NOT_FOUND")
        procedures = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("review", {}).get("status") in {
                "APPROVED_FOR_DEMO",
                "APPROVED_FOR_PRODUCTION",
            }:
                procedures.add(row["procedure_id"])
        if not procedures:
            raise SourceStoreError("G5_NO_APPROVED_PROCEDURES")
        return procedures

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceStoreError("G5_REGISTRY_INVALID") from exc
        if value.get("schema_version") != "g5-source-registry-v1":
            raise SourceStoreError("G5_REGISTRY_VERSION_INVALID")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.data_dir / f"registry-{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, self.registry_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _check_token(self, token: str | None) -> None:
        supplied = hashlib.sha256((token or "").encode()).digest()
        import hmac

        if not hmac.compare_digest(supplied, self._review_token_hash):
            raise SourceStoreError("G5_REVIEW_TOKEN_INVALID")

    @staticmethod
    def _key(procedure_id: str, field: str) -> str:
        return f"{procedure_id}:{field}"

    def stage(
        self,
        *,
        review_token: str,
        procedure_id: str,
        field: str,
        text: str,
        title: str,
        source_url: str | None,
        effective_from: str | None,
        effective_to: str | None,
    ) -> dict[str, Any]:
        self._check_token(review_token)
        if procedure_id not in self.allowed_procedures:
            raise SourceStoreError("G5_PROCEDURE_NOT_ALLOWED")
        if field not in FIELD_SECTIONS:
            raise SourceStoreError("G5_FIELD_NOT_ALLOWED")
        text = " ".join(text.split())
        title = " ".join(title.split())
        if not text or len(text) > 8_000 or not title or len(title) > 500:
            raise SourceStoreError("G5_SOURCE_TEXT_INVALID")
        if any(pattern.search(value) for pattern in PII_PATTERNS for value in (text, title)):
            raise SourceStoreError("G5_SOURCE_PII_REJECTED")
        if source_url:
            parsed = urlsplit(source_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise SourceStoreError("G5_SOURCE_URL_INVALID")
        effective_from = _validated_date(effective_from)
        effective_to = _validated_date(effective_to)
        if effective_from and effective_to and effective_from > effective_to:
            raise SourceStoreError("G5_EFFECTIVE_RANGE_INVALID")
        with self._lock:
            registry = self._read()
            registry["sequence"] += 1
            sequence = registry["sequence"]
            snapshot_id = f"g5-snapshot-{uuid4().hex}"
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            snapshot = {
                "snapshot_id": snapshot_id,
                "version": f"g5-source-v{sequence:06d}",
                "procedure_id": procedure_id,
                "field": field,
                "section_type": FIELD_SECTIONS[field],
                "text": text,
                "title": title,
                "source_url": source_url,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "content_sha256": content_hash,
                "status": "PENDING_REVIEW",
                "created_at": _now(),
                "reviewed_at": None,
                "reviewer": None,
                "published_at": None,
            }
            registry["snapshots"].append(snapshot)
            self._write(registry)
            return deepcopy(snapshot)

    def transition(
        self, *, action: str, snapshot_id: str, reviewer: str, review_token: str
    ) -> dict[str, Any]:
        self._check_token(review_token)
        if not reviewer.strip() or len(reviewer) > 100:
            raise SourceStoreError("G5_REVIEWER_INVALID")
        action = action.upper()
        if action not in {"APPROVE", "PUBLISH", "REJECT", "ROLLBACK"}:
            raise SourceStoreError("G5_ACTION_INVALID")
        with self._lock:
            registry = self._read()
            snapshot = next(
                (item for item in registry["snapshots"] if item["snapshot_id"] == snapshot_id),
                None,
            )
            if snapshot is None:
                raise SourceStoreError("G5_SNAPSHOT_NOT_FOUND")
            key = self._key(snapshot["procedure_id"], snapshot["field"])
            if action == "APPROVE" and snapshot["status"] == "PENDING_REVIEW":
                snapshot["status"] = "APPROVED"
                snapshot["reviewed_at"] = _now()
                snapshot["reviewer"] = reviewer.strip()
            elif action == "REJECT" and snapshot["status"] == "PENDING_REVIEW":
                snapshot["status"] = "REJECTED"
                snapshot["reviewed_at"] = _now()
                snapshot["reviewer"] = reviewer.strip()
            elif action == "PUBLISH" and snapshot["status"] == "APPROVED":
                previous = registry["active"].get(key)
                registry["active"][key] = snapshot_id
                snapshot["status"] = "PUBLISHED"
                snapshot["published_at"] = _now()
                if previous and previous != snapshot_id:
                    previous_snapshot = next(
                        item for item in registry["snapshots"] if item["snapshot_id"] == previous
                    )
                    previous_snapshot["status"] = "SUPERSEDED"
            elif action == "ROLLBACK" and snapshot["status"] == "SUPERSEDED":
                current_id = registry["active"].get(key)
                if current_id:
                    current = next(
                        item for item in registry["snapshots"] if item["snapshot_id"] == current_id
                    )
                    current["status"] = "ROLLED_BACK"
                registry["active"][key] = snapshot_id
                snapshot["status"] = "PUBLISHED"
                snapshot["published_at"] = _now()
            elif (
                action == "ROLLBACK"
                and snapshot["status"] == "PUBLISHED"
                and registry["active"].get(key) == snapshot_id
            ):
                # With no older reviewed snapshot selected, rollback means return
                # to the immutable corpus mounted by the backend.
                registry["active"].pop(key)
                snapshot["status"] = "ROLLED_BACK"
            else:
                raise SourceStoreError("G5_INVALID_STATE_TRANSITION")
            self._write(registry)
            return deepcopy(snapshot)

    def get_active(self, *, procedure_id: str, field: str) -> dict[str, Any] | None:
        if procedure_id not in self.allowed_procedures or field not in FIELD_SECTIONS:
            return None
        with self._lock:
            registry = self._read()
            snapshot_id = registry["active"].get(self._key(procedure_id, field))
            if not snapshot_id:
                return None
            snapshot = next(
                item for item in registry["snapshots"] if item["snapshot_id"] == snapshot_id
            )
            if snapshot["status"] != "PUBLISHED":
                raise SourceStoreError("G5_ACTIVE_SOURCE_NOT_PUBLISHED")
            return deepcopy(snapshot)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            registry = self._read()
            counts = {}
            for snapshot in registry["snapshots"]:
                counts[snapshot["status"]] = counts.get(snapshot["status"], 0) + 1
            return {
                "schema_version": registry["schema_version"],
                "approved_procedure_count": len(self.allowed_procedures),
                "active_source_count": len(registry["active"]),
                "snapshot_count_by_status": dict(sorted(counts.items())),
            }
