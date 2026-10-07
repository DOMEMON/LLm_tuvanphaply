"""Read-only G3 D2 corpus activation and projection into the grounded contract."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid5

from app.rag.g3.retrieval import retrieve, unusable_placeholder_fields
from app.rag.g3.validator import SECTIONS, Dataset, load_serving, scan
from app.rag.retrieval import normalize
from app.rag.field_policy import serving_fields
from app.rag.schemas import Evidence, EvidenceBundle, RetrievalInput


@lru_cache(maxsize=4)
def load_active_dataset(
    path: str, version: str, expected_rows: int, expected_sha256: str
) -> Dataset:
    data = load_serving(Path(path), version, expected_rows)
    if data.fingerprint != expected_sha256:
        raise ValueError("G3_CORPUS_HASH_MISMATCH")
    if data.report()["readiness"] != "READY_FOR_DEMO":
        raise ValueError("G3_DATASET_NOT_READY")
    # Gold/evaluation files are never opened. Conflict records have been validated;
    # retrieval retains only the three corpus groups.
    return Dataset(
        version=data.version,
        groups=[data.sources, data.procedures, data.fragments, {}, {}],
        fingerprint=data.fingerprint,
        accepted=data.accepted,
        rejected=data.rejected,
        row_count=data.row_count,
    )


def _jurisdiction_label(value: dict) -> str | None:
    parts = [value.get("ward"), value.get("district"), value.get("province")]
    selected = [item for item in parts if item]
    return ", ".join(selected) if selected else None


def retrieve_active_dataset(
    data: Dataset,
    query: RetrievalInput,
    request_id: UUID,
) -> tuple[EvidenceBundle, str, list[str]]:
    found = retrieve(
        data,
        query.query,
        query.top_k,
        procedure_id=query.filters.procedure_id,
        as_of=query.filters.as_of.isoformat() if query.filters.as_of else None,
        jurisdiction=query.filters.jurisdiction,
        field_intents=query.field_intents,
    )
    evidence = []
    for item in found["evidence"]:
        source = data.sources[item["source_id"]]
        procedure = data.procedures[item["procedure_id"]]
        evidence.append(
            Evidence(
                fragment_id=item["fragment_id"],
                source_id=item["source_id"],
                procedure_id=item["procedure_id"],
                section_type=item["section_type"],
                title=item["title"],
                url=item["url"],
                text=item["text"],
                score=item["score"],
                jurisdiction=_jurisdiction_label(procedure["jurisdiction"]),
                effective_from=(
                    date.fromisoformat(source["effective_from"])
                    if source["effective_from"]
                    else None
                ),
                effective_to=(
                    date.fromisoformat(source["effective_to"]) if source["effective_to"] else None
                ),
                validity_status=source["validity_status"],
                metadata={"source_type": source["source_type"]},
            )
        )
    bundle = EvidenceBundle(
        request_id=request_id,
        evidence_bundle_id=uuid5(request_id, "g3-grounded-evidence-bundle"),
        corpus_version=data.version,
        data_classification="D2_COMPANY_REAL",
        query=query.query,
        evidence=evidence,
        as_of=query.filters.as_of,
    )
    return bundle, found["status"], found["missing_fields"]


def retrieve_direct_active_dataset(
    data: Dataset,
    query: RetrievalInput,
    request_id: UUID,
) -> tuple[EvidenceBundle, str, list[str]]:
    """Read reviewed field evidence directly after procedure/field resolution.

    Each requested field is projected atomically. A missing field does not discard
    other complete fields; an unverified jurisdiction still blocks the whole scope.
    """

    procedure_id = query.filters.procedure_id
    fields = serving_fields(list(dict.fromkeys(query.field_intents or [])), data.version)
    scan(query.query)
    status = "INSUFFICIENT_DATA"
    missing: list[str] = []
    selected: list[dict] = []

    if procedure_id is None:
        status, missing = "NEED_CLARIFICATION", ["procedure_id"]
    elif procedure_id not in data.accepted:
        missing = ["procedure_id"]
    elif not fields:
        status, missing = "NEED_CLARIFICATION", ["field_intents"]
    elif any(field not in SECTIONS for field in fields):
        missing = [field for field in fields if field not in SECTIONS]
    else:
        procedure = data.procedures[procedure_id]
        if query.filters.jurisdiction:
            requested_scope = normalize(query.filters.jurisdiction)
            available_scope = {
                normalize(str(value))
                for key, value in procedure["jurisdiction"].items()
                if value and key != "country_code"
            }
            combined_scope = normalize(
                " ".join(
                    str(value)
                    for key, value in procedure["jurisdiction"].items()
                    if value and key != "country_code"
                )
            )
            if requested_scope not in available_scope and requested_scope != combined_scope:
                missing.append("jurisdiction")

        unusable = unusable_placeholder_fields(data, procedure, set(fields))
        for field in fields:
            refs = procedure["field_evidence"].get(field, [])
            if field in procedure["missing_fields"] or field in unusable or not refs:
                missing.append(field)
                continue
            for fragment_id in refs:
                fragment = data.fragments.get(fragment_id)
                if (
                    fragment is None
                    or fragment["procedure_id"] != procedure_id
                    or fragment["section_type"] != SECTIONS[field]
                ):
                    missing.append(field)
                    continue
                source = data.sources[fragment["source_id"]]
                if source["validity_status"] in {"EXPIRED", "FUTURE"}:
                    missing.append(field)
                    continue
                if query.filters.as_of and (
                    source["validity_status"] != "CURRENT"
                    or not source["effective_from"]
                    or query.filters.as_of.isoformat() < source["effective_from"]
                    or (
                        source["effective_to"]
                        and query.filters.as_of.isoformat() > source["effective_to"]
                    )
                ):
                    missing.append(field)
                    continue
                if fragment["fragment_id"] not in {item["fragment_id"] for item in selected}:
                    selected.append(fragment)

        missing = list(dict.fromkeys(missing))
        # Never expose half of a field or evidence under an unverified locality.
        selected = [
            item for item in selected
            if item["section_type"] not in {SECTIONS[f] for f in missing if f in SECTIONS}
        ]
        if "jurisdiction" in missing:
            selected = []
        elif len(selected) > 8 or sum(len(item["text"]) for item in selected) > 32_000:
            selected, missing = [], fields
        elif selected and not missing:
            status = "ANSWER"

    evidence = []
    if selected:
        procedure = data.procedures[procedure_id]
        for fragment in selected:
            source = data.sources[fragment["source_id"]]
            evidence.append(
                Evidence(
                    fragment_id=fragment["fragment_id"],
                    source_id=fragment["source_id"],
                    procedure_id=procedure_id,
                    section_type=fragment["section_type"],
                    title=procedure["title"],
                    url=source["public_url"],
                    text=fragment["text"],
                    score=1.0,
                    jurisdiction=_jurisdiction_label(procedure["jurisdiction"]),
                    effective_from=(
                        date.fromisoformat(source["effective_from"])
                        if source["effective_from"]
                        else None
                    ),
                    effective_to=(
                        date.fromisoformat(source["effective_to"])
                        if source["effective_to"]
                        else None
                    ),
                    validity_status=source["validity_status"],
                    metadata={
                        "source_type": source["source_type"],
                        "acquisition": "direct_catalog",
                    },
                )
            )
    bundle = EvidenceBundle(
        request_id=request_id,
        evidence_bundle_id=uuid5(request_id, "g8-direct-evidence-bundle"),
        corpus_version=data.version,
        data_classification="D2_COMPANY_REAL",
        query=query.query,
        evidence=evidence,
        as_of=query.filters.as_of,
    )
    return bundle, status, missing
