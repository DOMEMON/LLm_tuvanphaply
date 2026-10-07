"""Offline exact/alias/code routing plus BM25; internal IDs never become display labels."""

import math
import unicodedata
from collections import Counter
from time import perf_counter

from app.rag.g3.validator import SECTIONS, require, scan
from app.rag.g4.intent import detect_fields
from app.rag.retrieval import normalize

INTENTS = {
    "required_documents": ("ho so", "giay to", "documents"),
    "fees": ("le phi", "phi", "bao nhieu tien", "fees"),
    "processing_times": ("thoi gian", "bao lau", "may ngay", "thoi han"),
    "receiving_authority": ("nop o dau", "noi nop", "noi tiep nhan", "co quan tiep nhan"),
    "submission_methods": ("hinh thuc", "truc tuyen", "online", "nop cach nao"),
    "steps": ("trinh tu", "cac buoc"),
    "legal_bases": ("can cu phap ly",),
    "applicant_scope": ("doi tuong", "ai duoc"),
}

# These words describe the user's request, not the procedure identity. Keeping
# them in procedure-name BM25 made generic words such as "hồ sơ" select several
# unrelated procedures and turned ordinary questions into false ambiguity.
ROUTING_STOPWORDS = frozenset(
    {
        "ai",
        "bao",
        "bi",
        "biet",
        "can",
        "cach",
        "cho",
        "chuan",
        "co",
        "con",
        "cua",
        "dia",
        "duoc",
        "gi",
        "giai",
        "han",
        "hanh",
        "hay",
        "hinh",
        "ho",
        "lam",
        "le",
        "ly",
        "may",
        "mat",
        "muc",
        "muon",
        "nao",
        "nay",
        "ngay",
        "nhan",
        "nhieu",
        "nhung",
        "nguon",
        "nua",
        "noi",
        "nop",
        "o",
        "online",
        "phan",
        "pham",
        "phi",
        "phuong",
        "qua",
        "quy",
        "quyet",
        "so",
        "sao",
        "tai",
        "thanh",
        "theo",
        "thoi",
        "thu",
        "tiep",
        "tien",
        "thi",
        "toi",
        "to",
        "tra",
        "trinh",
        "trong",
        "truc",
        "truong",
        "tuc",
        "tuyen",
        "vi",
        "vay",
        "vua",
        "ve",
        "voi",
        "xu",
        "gom",
        "hop",
        "dinh",
        "khong",
        "khoan",
    }
) | frozenset(
    token for phrases in INTENTS.values() for phrase in phrases for token in phrase.split()
)
GENERIC_PROCEDURE_PREFIXES = ("thu tuc hanh chinh ", "thu tuc ")
ROUTING_MIN_SCORE = 1.0
ROUTING_MIN_MARGIN = 0.75
ROUTING_MIN_RATIO = 1.35


def bm25(query, texts):
    docs = [Counter(normalize(text).split()) for text in texts]
    if not docs:
        return []
    avg = sum(sum(d.values()) for d in docs) / len(docs) or 1
    terms = set(normalize(query).split())
    df = {t: sum(t in d for d in docs) for t in terms}
    scores = []
    for doc in docs:
        score = 0.0
        for t in terms:
            tf = doc[t]
            if tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * sum(doc.values()) / avg))
        scores.append(score)
    return scores


def procedure_names(data, procedure):
    """Return normalized names plus safe variants derived from generic prefixes."""
    raw_names = [procedure["title"], *procedure["aliases"]]
    raw_names += [
        data.sources[source_id]["procedure_code"] for source_id in procedure["source_ids"]
    ]
    names = set()
    for raw_name in raw_names:
        if not raw_name:
            continue
        name = normalize(raw_name)
        if not name:
            continue
        names.add(name)
        for prefix in GENERIC_PROCEDURE_PREFIXES:
            if name.startswith(prefix):
                shortened = name.removeprefix(prefix).strip()
                if len(shortened.split()) >= 2:
                    names.add(shortened)
    return sorted(names)


def fields_for_query(data, query):
    return detect_fields(
        query, [name for p in data.procedures.values() for name in procedure_names(data, p)]
    )


def routing_query(query):
    return " ".join(token for token in normalize(query).split() if token not in ROUTING_STOPWORDS)


def unusable_placeholder_fields(data, procedure, fields):
    """Fields whose reviewed raw value is only an opaque spreadsheet placeholder."""
    unusable = set()
    for field in fields & {"fees", "processing_times"}:
        values = procedure.get(field, [])
        raw_values = {
            str(item.get("raw_text", "")).strip().casefold()
            for item in values
            if isinstance(item, dict)
        }
        # The locked fees-v2 release adjudicated its twelve literal 0.0 fee
        # cells as zero-fee claims (field-decisions.jsonl). Older releases did
        # not make that decision, so retain their conservative placeholder guard.
        approved_zero_fee = (
            field == "fees"
            and data.version == "company-tthc-9c38dde8-v1-adjudicated-fees-v2"
            and procedure.get("fees")
            and all(item.get("amount_vnd") == 0 for item in procedure["fees"])
            and bool(procedure.get("field_evidence", {}).get("fees"))
        )
        if "05" in raw_values or ("0.0" in raw_values and not approved_zero_fee):
            unusable.add(field)
    return unusable


def retrieve(
    data,
    query,
    top_k=5,
    procedure_id=None,
    category=None,
    as_of=None,
    jurisdiction=None,
    field_intents=None,
):
    require(isinstance(query, str) and 0 < len(query.strip()) <= 4000, "INVALID_QUERY")
    require(type(top_k) is int and 1 <= top_k <= 8, "INVALID_TOP_K")
    # User input need not already satisfy the producer's NFC/trim invariant.
    # Normalize a copy before the PII tripwire and lexical retrieval.
    normalized_query = unicodedata.normalize("NFC", query.strip())
    scan(normalized_query)
    q = " " + normalize(normalized_query) + " "
    fields = (
        fields_for_query(data, normalized_query) if field_intents is None else set(field_intents)
    )
    require(fields <= SECTIONS.keys(), "INVALID_FIELD_INTENTS")
    from app.rag.field_policy import serving_fields

    fields = set(serving_fields(fields, data.version))
    result = {
        "status": "INSUFFICIENT_DATA",
        "procedure_id": None,
        "evidence": [],
        "missing_fields": [],
        "reason": "RETRIEVAL_MISS",
        "field_intents": sorted(fields),
    }
    pool = [
        p
        for p in data.procedures.values()
        if (procedure_id is None or p["procedure_id"] == procedure_id)
        and (category is None or p["category_code"] == category)
    ]
    exact = []
    for p in pool:
        matched_names = [name for name in procedure_names(data, p) if " " + name + " " in q]
        if matched_names:
            # A long procedure name can legitimately contain another procedure's
            # shorter name. Prefer the most specific explicit phrase; preserve
            # clarification when two distinct names have equal specificity.
            specificity = max(len(name.split()) for name in matched_names)
            exact.append((specificity, p))
    # Routing includes rejected procedures so an explicit pending title cannot
    # accidentally fall through to a different approved procedure.
    if exact:
        best_specificity = max(specificity for specificity, _ in exact)
        pool = [p for specificity, p in exact if specificity == best_specificity]
    elif not procedure_id:
        scoped_query = routing_query(normalized_query)
        if not scoped_query:
            return dict(result, status="NEED_CLARIFICATION", reason="PROCEDURE_NOT_SPECIFIED")
        scores = bm25(
            scoped_query,
            [" ".join([*procedure_names(data, p), normalize(p["category_label"])]) for p in pool],
        )
        ranked = sorted(
            ((score, p) for p, score in zip(pool, scores) if score > 0),
            key=lambda item: (-item[0], item[1]["procedure_id"]),
        )
        if not ranked:
            pool = []
        elif len(ranked) == 1:
            pool = [ranked[0][1]] if ranked[0][0] >= ROUTING_MIN_SCORE else []
        else:
            top_score, top = ranked[0]
            second_score = ranked[1][0]
            dominant = (
                top_score - second_score >= ROUTING_MIN_MARGIN
                and top_score / second_score >= ROUTING_MIN_RATIO
            )
            if not dominant:
                return dict(result, status="NEED_CLARIFICATION", reason="AMBIGUOUS_PROCEDURE")
            pool = [top] if top_score >= ROUTING_MIN_SCORE else []
    if len(pool) > 1:
        return dict(result, status="NEED_CLARIFICATION", reason="AMBIGUOUS_PROCEDURE")
    if not pool:
        return result
    p = pool[0]
    pid = p["procedure_id"]
    result["procedure_id"] = pid
    if pid not in data.accepted:
        return dict(result, reason=data.rejected[pid])
    if jurisdiction:
        requested_scope = normalize(jurisdiction)
        available_scope = {
            normalize(v) for k, v in p["jurisdiction"].items() if v and k != "country_code"
        }
        # Unknown scope cannot be asserted; no inference from evidence prose.
        combined_scope = normalize(
            " ".join(v for k, v in p["jurisdiction"].items() if v and k != "country_code")
        )
        if requested_scope not in available_scope and requested_scope != combined_scope:
            return dict(result, missing_fields=["jurisdiction"], reason="JURISDICTION_UNVERIFIED")
    if field_intents is not None and not fields:
        return dict(
            result,
            status="NEED_CLARIFICATION",
            missing_fields=["field_intents"],
            reason="FIELD_NOT_SPECIFIED",
        )
    missing = sorted(
        (fields & set(p["missing_fields"])) | unusable_placeholder_fields(data, p, fields)
    )
    # No fragment from a declared missing field may enter any evidence bundle.
    allowed = {
        fid
        for field, refs in p["field_evidence"].items()
        if field not in set(p["missing_fields"]) | set(missing) and (not fields or field in fields)
        for fid in refs
    }
    frags = [
        f
        for f in data.fragments.values()
        if f["procedure_id"] == pid and f["fragment_id"] in allowed
    ]
    if as_of:
        from datetime import date

        try:
            date.fromisoformat(as_of)
        except (TypeError, ValueError):
            require(False, "INVALID_AS_OF")
    usable = []
    for f in frags:
        s = data.sources[f["source_id"]]
        if s["validity_status"] in {"EXPIRED", "FUTURE"}:
            continue
        if as_of and (
            s["validity_status"] != "CURRENT"
            or not s["effective_from"]
            or as_of < s["effective_from"]
            or (s["effective_to"] and as_of > s["effective_to"])
        ):
            continue
        usable.append(f)
    scores = bm25(normalized_query, [f["text"] for f in usable])
    ranked = sorted(zip(scores, usable), key=lambda x: (-x[0], x[1]["fragment_id"]))
    # Reserve one hit per requested field before filling the remaining Top-K slots.
    # Do not raise K to disguise an intent error.
    reserved = []
    for field in sorted(fields):
        candidate = next(
            (item for item in ranked if item[1]["section_type"] == SECTIONS[field]), None
        )
        if candidate and candidate not in reserved:
            reserved.append(candidate)
    selection = (reserved + [item for item in ranked if item not in reserved])[:top_k]
    selected, budget = [], 0
    for score, f in selection:
        if budget + len(f["text"]) > 32000:
            break
        budget += len(f["text"])
        s = data.sources[f["source_id"]]
        selected.append(
            {
                "fragment_id": f["fragment_id"],
                "source_id": f["source_id"],
                "procedure_id": pid,
                "section_type": f["section_type"],
                "title": p["title"],
                "text": f["text"],
                "url": s["public_url"],
                "source_type": s["source_type"],
                "score": round(score, 10),
            }
        )
    covered = {f["section_type"] for f in selected}
    missing = sorted(set(missing) | {field for field in fields if SECTIONS[field] not in covered})
    return dict(
        result,
        status="ANSWER" if selected and not missing else "INSUFFICIENT_DATA",
        evidence=selected,
        missing_fields=missing,
        reason=None if selected and not missing else "DATA_MISSING",
    )


def evaluate(data, split="DEV", top_k=5):
    results = []
    for q in data.groups[4].values():
        if q["split"] != split:
            continue
        started = perf_counter()
        found = retrieve(data, q["question"], top_k)
        latency = (perf_counter() - started) * 1000
        ids = {e["fragment_id"] for e in found["evidence"]}
        expected = set(q["gold_fragment_ids"])
        recall = len(ids & expected) / len(expected) if expected else None
        routing = found["procedure_id"] == q["procedure_id"] if q["procedure_id"] else None
        status = found["status"] == q["expected_status"]
        error = (
            "ROUTING_ERROR"
            if routing is False
            else "RETRIEVAL_MISS"
            if recall is not None and recall < 1
            else "DATA_MISSING"
            if not status
            else None
        )
        results.append(
            {
                "question_id": q["question_id"],
                "routing_correct": routing,
                "status_correct": status,
                "recall_at_k": recall,
                "retrieved_ids": sorted(ids),
                "error": error,
                "pass": error is None,
                "latency_ms": round(latency, 4),
            }
        )
    require(bool(results), "EMPTY_EVALUATION_SPLIT")
    recalls = [r["recall_at_k"] for r in results if r["recall_at_k"] is not None]
    routing = [r["routing_correct"] for r in results if r["routing_correct"] is not None]
    times = sorted(r["latency_ms"] for r in results)
    return {
        "dataset_version": data.version,
        "corpus_sha256": data.fingerprint,
        "split": split,
        "top_k": top_k,
        "query_count": len(results),
        "scope": "offline retrieval only; no generation or legal-quality assessment",
        "routing_accuracy": sum(routing) / len(routing) if routing else None,
        "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
        "results": results,
    }
