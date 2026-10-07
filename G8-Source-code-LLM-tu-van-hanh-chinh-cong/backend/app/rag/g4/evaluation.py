"""Offline DEV-only retrieval benchmark; no evaluation content is printed."""

import argparse
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from app.rag.corpus import CorpusError
from app.rag.g3.retrieval import fields_for_query, retrieve
from app.rag.g3.validator import SECTIONS, load_serving, pairs, require
from app.rag.g4.context import prepare_turn
from app.rag.schemas import RetrievalInput


def _read_scenarios(path, *, filename, split):
    path = Path(os.path.abspath(path))
    require(path.name == filename, f"{split}_FILENAME_REQUIRED")
    require(path.resolve() == path, f"{split}_SYMLINK_FORBIDDEN")
    raw = path.read_bytes()
    rows = [
        json.loads(line, object_pairs_hook=pairs) for line in raw.decode("utf-8-sig").splitlines()
    ]
    require(bool(rows), f"EMPTY_{split}")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and row.get("split") == split, f"{split}_ONLY")
        require(
            row.get("scenario_version") == "g4-eval-scenario-v1",
            f"{split}_SCENARIO_VERSION",
        )
        require(row["scenario_id"] not in seen, f"DUPLICATE_{split}_ID")
        seen.add(row["scenario_id"])
        require(type(row["is_multiturn"]) is bool, f"{split}_MULTITURN_TYPE")
        require(isinstance(row["question"], str), f"{split}_QUERY_TYPE")
        require(row["field_expected"] in SECTIONS, f"{split}_UNKNOWN_FIELD")
        require(
            row["expected_status"] in {"ANSWER", "NEED_CLARIFICATION", "INSUFFICIENT_DATA"},
            f"{split}_UNKNOWN_STATUS",
        )
        require(row["conversation_turns"] == len(row["turns"]), f"{split}_TURN_COUNT")
        roles = [turn.get("role") for turn in row["turns"]]
        require(
            all(isinstance(turn.get("text"), str) for turn in row["turns"]),
            f"{split}_TURN_TEXT",
        )
        require(roles[-1:] == ["user"], f"{split}_FINAL_USER_TURN")
        require(
            row["question"] == row["turns"][-1]["text"],
            f"{split}_TARGET_TURN_MISMATCH",
        )
        if row["is_multiturn"]:
            require(
                roles == ["user", "assistant", "user"],
                f"{split}_MULTITURN_STRUCTURE",
            )
        else:
            require(roles == ["user"], f"{split}_SINGLE_TURN_STRUCTURE")
        if split == "TEST_V3":
            require(row.get("scored_turn_index") == 2, "TEST_V3_SCORED_TURN_INDEX")
            require(row.get("scored_turn_role") == "user", "TEST_V3_SCORED_TURN_ROLE")
            require(
                row.get("answer_visibility") == "private_gold_only",
                "TEST_V3_ANSWER_VISIBILITY",
            )
        require(row["review"]["status"] == "APPROVED", f"{split}_REVIEW_REQUIRED")
        label = row["gold_label"]
        for a, b in [
            ("field", "field_expected"),
            ("procedure_id", "procedure_id"),
            ("fragment_ids", "gold_fragment_ids"),
            ("status", "expected_status"),
        ]:
            require(label[a] == row[b], f"{split}_LABEL_MISMATCH")
    return rows, hashlib.sha256(raw).hexdigest()


def read_dev(path):
    return _read_scenarios(path, filename="scenario_dev.jsonl", split="DEV")


def read_locked_test_v2(path, *, acknowledged=False):
    require(acknowledged, "LOCKED_TEST_ACKNOWLEDGEMENT_REQUIRED")
    return _read_scenarios(path, filename="scenario_test_v2.jsonl", split="TEST_V2")


def read_locked_test_v3(path, *, acknowledged=False):
    require(acknowledged, "LOCKED_TEST_ACKNOWLEDGEMENT_REQUIRED")
    return _read_scenarios(path, filename="scenario_test_v3.jsonl", split="TEST_V3")


def prepare_scenario_target(data, row, top_k):
    """Replay only user turns; assistant text never establishes retrieval state."""
    state = None
    prepared = None
    for index, turn in enumerate(row["turns"]):
        if turn["role"] != "user":
            continue
        message_id = uuid5(NAMESPACE_URL, f"{row['scenario_id']}:{index}")
        prepared, state, _ = prepare_turn(
            data,
            RetrievalInput(query=turn["text"], top_k=top_k),
            state,
            message_id,
        )
    require(prepared is not None, "DEV_USER_TURN_REQUIRED")
    return prepared


def benchmark(
    data,
    rows,
    retriever=retrieve,
    detector=None,
    include_multiturn=True,
    expected_split="DEV",
):
    stats, failures, per_field = [], Counter(), {}
    skipped = 0
    for row in rows:
        require(row["split"] == expected_split, f"{expected_split}_ONLY")
        require(row["dataset_version"] == data.version, "DEV_VERSION_MISMATCH")
        require(set(row["gold_fragment_ids"]) <= data.fragments.keys(), "DEV_REFERENCE_ERROR")
        require(
            row["procedure_id"] is None or row["procedure_id"] in data.procedures,
            "DEV_PROCEDURE_REFERENCE",
        )
        require(
            all(
                data.fragments[fid]["procedure_id"] == row["procedure_id"]
                for fid in row["gold_fragment_ids"]
            ),
            "DEV_FRAGMENT_BINDING",
        )
        require(
            row["expected_status"] != "ANSWER" or bool(row["gold_fragment_ids"]),
            "DEV_ANSWER_WITHOUT_EVIDENCE",
        )
        if row["is_multiturn"] and not include_multiturn:
            skipped += 1
            continue
        started = perf_counter()
        if row["is_multiturn"]:
            require(retriever is retrieve and detector is None, "MULTITURN_CONTEXT_REQUIRED")
            query = prepare_scenario_target(data, row, 5)
            found = retriever(
                data,
                query.query,
                top_k=query.top_k,
                procedure_id=query.filters.procedure_id,
                jurisdiction=query.filters.jurisdiction,
                field_intents=query.field_intents,
                as_of=query.filters.as_of.isoformat() if query.filters.as_of else None,
            )
            predicted_fields = set(query.field_intents or [])
        else:
            found = retriever(data, row["question"], top_k=5)
            predicted_fields = (
                detector(row["question"]) if detector else fields_for_query(data, row["question"])
            )
        elapsed = (perf_counter() - started) * 1000
        ids = [e["fragment_id"] for e in found["evidence"]]
        gold = set(row["gold_fragment_ids"])
        field = row["field_expected"]
        field_ok = predicted_fields == {field}
        route_ok = found["procedure_id"] == row["procedure_id"]
        status_ok = found["status"] == row["expected_status"]
        recall = len(set(ids) & gold) / len(gold) if gold else None
        mrr = (
            next((1 / rank for rank, fid in enumerate(ids, 1) if fid in gold), 0) if gold else None
        )
        for code, ok in [("FIELD", field_ok), ("ROUTING", route_ok), ("STATUS", status_ok)]:
            if not ok:
                failures[code] += 1
        if recall is not None and recall < 1:
            failures["RECALL"] += 1
        stats.append((field_ok, route_ok, status_ok, recall, mrr, elapsed))
        group = per_field.setdefault(field, {"count": 0, "field_correct": 0, "status_correct": 0})
        group["count"] += 1
        group["field_correct"] += int(field_ok)
        group["status_correct"] += int(status_ok)
    require(bool(stats), "NO_SCORABLE_DEV")
    times = sorted(s[5] for s in stats)
    recalls = [s[3] for s in stats if s[3] is not None]
    mrrs = [s[4] for s in stats if s[4] is not None]
    return {
        "scope": (
            "DEV offline retrieval and conversation routing; "
            "no generation or business-answer review"
        ),
        "top_k": 5,
        "scenario_count": len(rows),
        "scored_scenarios": len(stats),
        "scored_multiturn": sum(row["is_multiturn"] for row in rows) - skipped,
        "unscored_multiturn": skipped,
        "field_accuracy": sum(s[0] for s in stats) / len(stats),
        "routing_accuracy": sum(s[1] for s in stats) / len(stats),
        "status_accuracy": sum(s[2] for s in stats) / len(stats),
        "recall_at_5": sum(recalls) / len(recalls) if recalls else None,
        "mrr_at_5": sum(mrrs) / len(mrrs) if mrrs else None,
        "recall_denominator": len(recalls),
        "p50_ms": times[math.ceil(0.5 * len(times)) - 1],
        "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
        "errors_by_stage": dict(failures),
        "per_field": per_field,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate-corpus", "evaluate-dev"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-rows", type=int, default=10)
    parser.add_argument("--dev", type=Path)
    args = parser.parse_args(argv)
    try:
        data = load_serving(args.dataset, args.version, args.expected_rows)
        result = {
            "schema_version": "g3-data-v1",
            "fingerprint_format": "g4-serving-corpus-v1",
            "dataset_version": data.version,
            "corpus_sha256": data.fingerprint,
            "source_count": len(data.sources),
            "procedure_count": len(data.procedures),
            "fragment_count": len(data.fragments),
            "accepted_count": len(data.accepted),
            "rejected_count": len(data.rejected),
            "readiness": data.report()["readiness"],
        }
        if args.command == "evaluate-dev":
            require(args.dev is not None, "DEV_PATH_REQUIRED")
            rows, sha = read_dev(args.dev)
            result.update(benchmark(data, rows))
            result["dev_sha256"] = sha
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CorpusError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception:
        print('{"error":"G4_VALIDATION_OR_EVALUATION_FAILED"}')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
