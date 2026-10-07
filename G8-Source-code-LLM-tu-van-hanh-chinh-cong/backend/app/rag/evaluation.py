import math
import time
from uuid import UUID

from app.rag.corpus import CorpusError, index_unique, read_jsonl
from app.rag.retrieval import retrieve
from app.rag.schemas import GoldQuestion, RetrievalInput


def percentile(values, q):
    if not values:
        return 0
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)]


def evaluate(corpus, gold_path, split="DEV", top_k=5):
    questions = index_unique(read_jsonl(gold_path, GoldQuestion), "question_id")
    # Validate all references, even rows outside selected split; never optimize on TEST here.
    for question in questions.values():
        if (
            question.author == question.reviewer
            or not set(question.gold_source_ids) <= corpus.sources.keys()
            or not set(question.gold_fragment_ids) <= corpus.fragments.keys()
            or (question.procedure_id and question.procedure_id not in corpus.procedures)
        ):
            raise CorpusError(f"GOLD_REFERENCE_OR_REVIEW_ERROR {question.question_id}")
        for fragment_id in question.gold_fragment_ids:
            frag = corpus.fragments[fragment_id]
            if frag.source_id not in question.gold_source_ids or (
                question.procedure_id and frag.procedure_id != question.procedure_id
            ):
                raise CorpusError(f"GOLD_SOURCE_MISMATCH {question.question_id}")
        if question.expected_status == "ANSWER" and not question.gold_fragment_ids:
            raise CorpusError(f"GOLD_ANSWER_WITHOUT_EVIDENCE {question.question_id}")
    results, times, recalls, ranks = [], [], [], []
    for question in questions.values():
        if question.split != split:
            continue
        started = time.perf_counter()
        # procedure_id is the gold label; do NOT leak it into retrieval filters.
        found = retrieve(
            corpus,
            RetrievalInput(query=question.question, top_k=top_k, filters=question.filters),
            UUID(int=1),
        )
        elapsed = (time.perf_counter() - started) * 1000
        times.append(elapsed)
        ids = [e.fragment_id for e in found.bundle.evidence]
        expected = set(question.gold_fragment_ids)
        recall = len(expected.intersection(ids)) / len(expected) if expected else None
        rank = next((1 / (i + 1) for i, value in enumerate(ids) if value in expected), 0)
        status = (
            "NEED_CLARIFICATION"
            if found.needs_clarification
            else "ANSWER"
            if ids
            else "INSUFFICIENT_DATA"
        )
        if expected:
            recalls.append(recall)
            ranks.append(rank)
        passed = status == question.expected_status and (recall is None or recall == 1)
        results.append(
            {
                "question_id": question.question_id,
                "retrieved_ids": ids,
                "gold_fragment_ids": sorted(expected),
                "recall_at_k": recall,
                "reciprocal_rank": rank if expected else None,
                "retrieval_status": status,
                "expected_status": question.expected_status,
                "pass": passed,
                "latency_ms": round(elapsed, 4),
            }
        )
    if not results:
        raise CorpusError("EMPTY_EVALUATION_SPLIT")
    return {
        "corpus_version": corpus.version,
        "corpus_sha256": corpus.fingerprint,
        "synthetic_fixture": corpus.fixture,
        "split": split,
        "top_k": top_k,
        "metric_scope": "retrieval only; not answer quality or legal correctness",
        "positive_gold_queries": len(recalls),
        "query_count": len(results),
        "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "mrr": sum(ranks) / len(ranks) if ranks else None,
        "latency_ms": {"p50": percentile(times, 0.5), "p95": percentile(times, 0.95)},
        "failed_question_ids": [r["question_id"] for r in results if not r["pass"]],
        "results": results,
    }
