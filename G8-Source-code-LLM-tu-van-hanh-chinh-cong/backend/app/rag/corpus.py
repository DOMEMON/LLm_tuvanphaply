import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from app.rag.schemas import (
    BUSINESS_FIELDS,
    ID,
    ProcedureFragment,
    ProcedureRecord,
    SourceManifest,
)


class CorpusError(ValueError):
    """Safe diagnostic: no source text or raw credentials in error messages."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_jsonl(path: Path, schema):
    rows = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(schema.model_validate_json(line))
                except (ValidationError, ValueError):
                    raise CorpusError(f"SCHEMA_ERROR {path.name}:{number}") from None
    except OSError:
        raise CorpusError(f"FILE_NOT_READABLE {path.name}") from None
    return rows


def index_unique(rows, key):
    result = {}
    for row in rows:
        value = getattr(row, key)
        if value in result:
            raise CorpusError(f"DUPLICATE_ID {key}={value}")
        result[value] = row
    return result


@dataclass
class Corpus:
    version: str
    sources: dict[str, SourceManifest]
    procedures: dict[str, ProcedureRecord]
    fragments: dict[str, ProcedureFragment]
    fingerprint: str
    fixture: bool = False


def fingerprint(sources, procedures, fragments):
    content = [
        [row.model_dump(mode="json") for _, row in sorted(group.items())]
        for group in (sources, procedures, fragments)
    ]
    return sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def validate_corpus(
    repo_root: Path,
    dataset: Path,
    version: str,
    allowed_domains: set[str],
    allow_fixture: bool = False,
) -> Corpus:
    TypeAdapter(ID).validate_python(version)
    root, dataset = repo_root.resolve(), dataset.resolve()
    if not dataset.is_relative_to(root):
        raise CorpusError("DATASET_OUTSIDE_REPO")
    sources = index_unique(
        read_jsonl(dataset / "source_manifest.jsonl", SourceManifest), "source_id"
    )
    procedures = index_unique(
        read_jsonl(dataset / "normalized/procedures.jsonl", ProcedureRecord), "procedure_id"
    )
    fragments = index_unique(
        read_jsonl(dataset / "normalized/fragments.jsonl", ProcedureFragment), "fragment_id"
    )
    if not sources or not procedures or not fragments:
        raise CorpusError("EMPTY_CORPUS")
    fixture = False
    domains = {d.lower() for d in allowed_domains}
    for source in sources.values():
        url = urlsplit(source.source_url)
        if (
            url.scheme != "https"
            or url.hostname != source.source_domain.lower()
            or url.hostname not in domains
            or url.username
            or url.password
        ):
            raise CorpusError(f"SOURCE_URL_NOT_ALLOWED {source.source_id}")
        if source.review_status != "APPROVED":
            raise CorpusError(f"SOURCE_NOT_REVIEWED {source.source_id}")
        if source.usage == "D0_SYSTEM_SMOKE":
            if not allow_fixture or url.hostname != "fixture.invalid":
                raise CorpusError("FIXTURE_REQUIRES_EXPLICIT_OPT_IN")
            fixture = True
        elif url.hostname == "fixture.invalid":
            raise CorpusError("SYNTHETIC_SOURCE_CANNOT_BE_D1")
        raw = Path(source.raw_path)
        candidate = (root / raw).resolve()
        if raw.is_absolute() or not candidate.is_relative_to(dataset):
            raise CorpusError(f"RAW_PATH_OUTSIDE_DATASET {source.source_id}")
        try:
            digest = sha256(candidate.read_bytes())
        except OSError:
            raise CorpusError(f"RAW_MISSING {source.source_id}") from None
        if digest != source.content_sha256:
            raise CorpusError(f"RAW_HASH_MISMATCH {source.source_id}")
    if fixture and any(s.usage != "D0_SYSTEM_SMOKE" for s in sources.values()):
        raise CorpusError("MIXED_FIXTURE_AND_D1")
    ordinals = set()
    for fragment in fragments.values():
        if fragment.source_id not in sources or fragment.procedure_id not in procedures:
            raise CorpusError(f"BROKEN_FRAGMENT_REFERENCE {fragment.fragment_id}")
        if fragment.source_id not in procedures[fragment.procedure_id].source_ids:
            raise CorpusError(f"FRAGMENT_SOURCE_NOT_IN_PROCEDURE {fragment.fragment_id}")
        if sha256(fragment.text.encode("utf-8")) != fragment.content_sha256:
            raise CorpusError(f"FRAGMENT_HASH_MISMATCH {fragment.fragment_id}")
        ordinal = (
            fragment.procedure_id,
            fragment.source_id,
            fragment.section_type,
            fragment.ordinal,
        )
        if ordinal in ordinals:
            raise CorpusError(f"DUPLICATE_ORDINAL {fragment.fragment_id}")
        ordinals.add(ordinal)
    for proc in procedures.values():
        if len(proc.source_ids) != len(set(proc.source_ids)):
            raise CorpusError(f"DUPLICATE_SOURCE_REFERENCE {proc.procedure_id}")
        if not set(proc.source_ids) <= sources.keys():
            raise CorpusError(f"BROKEN_PROCEDURE_REFERENCE {proc.procedure_id}")
        if any(sources[s].jurisdiction != proc.jurisdiction for s in proc.source_ids):
            raise CorpusError(f"MIXED_JURISDICTION {proc.procedure_id}")
        codes = {sources[s].procedure_code for s in proc.source_ids if sources[s].procedure_code}
        if len(codes) > 1:
            raise CorpusError(f"MIXED_PROCEDURE_CODES {proc.procedure_id}")
        if set(proc.field_evidence) - set(BUSINESS_FIELDS):
            raise CorpusError(f"UNKNOWN_PROVENANCE_FIELD {proc.procedure_id}")
        for field in BUSINESS_FIELDS:
            values, refs = getattr(proc, field), proc.field_evidence.get(field, [])
            if len(values) != len(refs):
                raise CorpusError(f"PROVENANCE_LENGTH {proc.procedure_id}/{field}")
            for ids in refs:
                if not ids or any(
                    i not in fragments or fragments[i].procedure_id != proc.procedure_id
                    for i in ids
                ):
                    raise CorpusError(f"BROKEN_PROVENANCE {proc.procedure_id}/{field}")
    return Corpus(
        version,
        sources,
        procedures,
        fragments,
        fingerprint(sources, procedures, fragments),
        fixture,
    )
