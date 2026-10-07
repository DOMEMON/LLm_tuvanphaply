import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from uuid import UUID, uuid5

from app.rag.corpus import Corpus
from app.rag.schemas import Evidence, EvidenceBundle, RetrievalInput


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return " ".join(re.findall(r"\w+", "".join(c for c in text if not unicodedata.combining(c))))


@dataclass
class RetrievalResult:
    bundle: EvidenceBundle
    needs_clarification: bool = False


def retrieve(corpus: Corpus, query: RetrievalInput, request_id: UUID) -> RetrievalResult:
    q = normalize(query.query)
    candidates = []
    for fragment in corpus.fragments.values():
        proc, source = corpus.procedures[fragment.procedure_id], corpus.sources[fragment.source_id]
        f = query.filters
        if f.procedure_id and f.procedure_id != proc.procedure_id:
            continue
        if f.jurisdiction and f.jurisdiction != source.jurisdiction:
            continue
        if f.as_of:
            date_outside_known_interval = (
                source.effective_from is not None and f.as_of < source.effective_from
            ) or (source.effective_to is not None and f.as_of > source.effective_to)
            if (
                source.status != "CONFIRMED_FROM_SOURCE"
                or (source.effective_from is None and source.effective_to is None)
                or date_outside_known_interval
            ):
                continue
        candidates.append(fragment)
    exact = set()
    for fragment in candidates:
        proc = corpus.procedures[fragment.procedure_id]
        names = [proc.title, *proc.aliases] + [
            corpus.sources[s].procedure_code for s in proc.source_ids
        ]
        # A user normally embeds the procedure name/code in a longer question
        # (for example "lệ phí của <procedure> là bao nhiêu?"). Treat a full
        # normalized name phrase as an explicit scope, not only an exact query.
        if any(name and f" {normalize(name)} " in f" {q} " for name in names):
            exact.add(proc.procedure_id)
    empty = EvidenceBundle(
        request_id=request_id,
        evidence_bundle_id=uuid5(request_id, "grounded-evidence-bundle"),
        corpus_version=corpus.version,
        data_classification="D0_SYSTEM_SMOKE" if corpus.fixture else "D1_PUBLIC_DEMO",
        query=query.query,
        evidence=[],
        as_of=query.filters.as_of,
    )
    if len(exact) > 1:
        return RetrievalResult(empty, True)
    if exact:
        candidates = [f for f in candidates if f.procedure_id in exact]
    if not candidates:
        return RetrievalResult(empty)
    docs = [Counter(normalize(f.text).split()) for f in candidates]
    avg = sum(sum(d.values()) for d in docs) / len(docs) or 1
    terms = sorted(set(q.split()))
    frequencies = {t: sum(t in d for d in docs) for t in terms}
    scored = []
    for fragment, doc in zip(candidates, docs):
        score = 0.0
        for term in terms:
            tf = doc[term]
            if tf:
                idf = math.log(
                    1 + (len(docs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * sum(doc.values()) / avg))
        if score > 0 or exact:
            scored.append((score + (1 if exact else 0), fragment))
    scored.sort(key=lambda item: (-item[0], item[1].fragment_id))
    # Never mix procedures into one answer. Detect ambiguity before top_k truncation,
    # otherwise top_k=1 could silently hide the second matching procedure.
    matched_procedures = {fragment.procedure_id for _, fragment in scored}
    if len(matched_procedures) > 1 and not query.filters.procedure_id:
        return RetrievalResult(empty, True)
    evidence = []
    for score, fragment in scored[: query.top_k]:
        source, proc = corpus.sources[fragment.source_id], corpus.procedures[fragment.procedure_id]
        evidence.append(
            Evidence(
                fragment_id=fragment.fragment_id,
                source_id=source.source_id,
                procedure_id=proc.procedure_id,
                section_type=fragment.section_type,
                title=source.title,
                url=source.source_url,
                text=fragment.text,
                score=round(score, 10),
                jurisdiction=source.jurisdiction,
                effective_from=source.effective_from,
                effective_to=source.effective_to,
                validity_status="CURRENT" if query.filters.as_of else "UNKNOWN",
                metadata={
                    "status": source.status,
                    "retrieved_at": source.retrieved_at,
                    "procedure_version": proc.version,
                    "content_sha256": fragment.content_sha256,
                },
            )
        )
    return RetrievalResult(empty.model_copy(update={"evidence": evidence}))
