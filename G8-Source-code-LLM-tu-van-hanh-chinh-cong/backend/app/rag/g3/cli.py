"""G3 normalized-only preflight. Real ingest stays closed pending review register."""

import argparse
import json
from pathlib import Path

from app.rag.corpus import CorpusError
from app.rag.g3.retrieval import evaluate, retrieve
from app.rag.g3.validator import load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "ingest", "query", "evaluate"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-rows", type=int, default=42)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--split", choices=["DEV", "TEST"], default="DEV")
    args = parser.parse_args(argv)
    try:
        data = load(args.dataset, args.version, args.expected_rows, args.production)
        result = data.report()
        if args.command == "ingest":
            # No bypass flag: the five-file frozen contract cannot independently
            # verify raw row/cell hashes. Never turn a preflight into active D2.
            raise CorpusError("INGEST_BLOCKED_REVIEW_REGISTER_G3_RV_001_002")
        if args.command == "query":
            found = retrieve(data, args.query, args.top_k)
            # CLI stdout is log-like: no question, evidence, title or URI.
            result = {
                "status": found["status"],
                "procedure_id": found["procedure_id"],
                "fragment_ids": [e["fragment_id"] for e in found["evidence"]],
                "reason": found["reason"],
                "missing_fields": found["missing_fields"],
            }
        if args.command == "evaluate":
            result = evaluate(data, args.split, args.top_k)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CorpusError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception:
        print('{"error":"G3_COMMAND_FAILED"}')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
