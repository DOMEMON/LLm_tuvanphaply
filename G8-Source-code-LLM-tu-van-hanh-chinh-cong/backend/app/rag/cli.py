"""Run from backend/: python -m app.rag.cli --help."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.rag.corpus import CorpusError, validate_corpus
from app.rag.evaluation import evaluate
from app.rag.retrieval import retrieve
from app.rag.schemas import Filters, RetrievalInput
from app.rag.store import ingest


async def ingest_once(corpus):
    engine = create_async_engine(get_settings().connection_url)
    try:
        async with async_sessionmaker(engine)() as db:
            async with db.begin():
                return await ingest(db, corpus)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "ingest", "query", "evaluate"])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--dataset", type=Path, required=True, help="Relative to repo-root")
    parser.add_argument("--version", required=True)
    parser.add_argument("--allow-domain", action="append", default=[], required=True)
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--procedure-id")
    parser.add_argument("--jurisdiction")
    parser.add_argument("--as-of")
    parser.add_argument("--split", choices=["TRAIN", "DEV", "TEST"], default="DEV")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        started = time.perf_counter()
        dataset = args.repo_root / args.dataset
        corpus = validate_corpus(
            args.repo_root, dataset, args.version, set(args.allow_domain), args.allow_fixture
        )
        validation_ms = (time.perf_counter() - started) * 1000
        if not 1 <= args.top_k <= 8:
            raise CorpusError("TOP_K_OUT_OF_RANGE")
        result = {
            "corpus_version": corpus.version,
            "fingerprint": corpus.fingerprint,
            "synthetic_fixture": corpus.fixture,
            "sources": len(corpus.sources),
            "procedures": len(corpus.procedures),
            "fragments": len(corpus.fragments),
            "validation_ms": validation_ms,
        }
        if args.command == "ingest":
            started = time.perf_counter()
            result["inserted"] = asyncio.run(ingest_once(corpus))
            result["ingest_ms"] = (time.perf_counter() - started) * 1000
        elif args.command == "query":
            if not args.query:
                raise CorpusError("QUERY_REQUIRED")
            found = retrieve(
                corpus,
                RetrievalInput(
                    query=args.query,
                    top_k=args.top_k,
                    filters=Filters(
                        procedure_id=args.procedure_id,
                        jurisdiction=args.jurisdiction,
                        as_of=args.as_of,
                    ),
                ),
                uuid4(),
            )
            result = {
                "bundle": found.bundle.model_dump(mode="json"),
                "needs_clarification": found.needs_clarification,
            }
        elif args.command == "evaluate":
            result = evaluate(corpus, dataset / "gold/questions.jsonl", args.split, args.top_k)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0
    except CorpusError as exc:
        print(f"RAG_VALIDATION_FAILED: {exc}")
    except ValidationError:
        print("RAG_SCHEMA_ERROR: check version/filters/input types; no raw values logged.")
    except Exception as exc:
        print(f"RAG_COMMAND_FAILED: {type(exc).__name__}; run app.doctor and check migration.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
