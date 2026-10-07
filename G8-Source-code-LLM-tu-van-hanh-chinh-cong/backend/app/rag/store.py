import hashlib

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.corpus import Corpus, CorpusError, fingerprint
from app.rag.models import Fragment, IngestionRun, Procedure, Source
from app.rag.schemas import ProcedureFragment, ProcedureRecord, SourceManifest


async def ingest(db: AsyncSession, corpus: Corpus) -> bool:
    """Caller commits/rolls back. A transaction lock serializes same-version imports."""
    key = int.from_bytes(hashlib.sha256(corpus.version.encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    old = await db.get(IngestionRun, corpus.version)
    if old:
        if old.fingerprint != corpus.fingerprint or old.fixture != corpus.fixture:
            raise CorpusError("CORPUS_VERSION_CONFLICT")
        return False
    db.add(
        IngestionRun(
            corpus_version=corpus.version, fingerprint=corpus.fingerprint, fixture=corpus.fixture
        )
    )
    await db.flush()
    for source in corpus.sources.values():
        db.add(
            Source(
                corpus_version=corpus.version,
                source_id=source.source_id,
                payload=source.model_dump(mode="json"),
            )
        )
    for proc in corpus.procedures.values():
        db.add(
            Procedure(
                corpus_version=corpus.version,
                procedure_id=proc.procedure_id,
                payload=proc.model_dump(mode="json"),
            )
        )
    await db.flush()
    for frag in corpus.fragments.values():
        db.add(
            Fragment(
                corpus_version=corpus.version,
                fragment_id=frag.fragment_id,
                source_id=frag.source_id,
                procedure_id=frag.procedure_id,
                payload=frag.model_dump(mode="json"),
            )
        )
    await db.flush()
    return True


async def load_corpus(db: AsyncSession, version: str, allow_fixture: bool = False) -> Corpus:
    run = await db.get(IngestionRun, version)
    if not run:
        raise CorpusError("CORPUS_NOT_INGESTED")
    if run.fixture and not allow_fixture:
        raise CorpusError("FIXTURE_DISABLED")
    groups = []
    for table, schema, field in (
        (Source, SourceManifest, "source_id"),
        (Procedure, ProcedureRecord, "procedure_id"),
        (Fragment, ProcedureFragment, "fragment_id"),
    ):
        rows = (await db.scalars(select(table).where(table.corpus_version == version))).all()
        groups.append({getattr(row, field): schema.model_validate(row.payload) for row in rows})
    if fingerprint(*groups) != run.fingerprint:
        raise CorpusError("STORED_CORPUS_HASH_MISMATCH")
    return Corpus(version, *groups, run.fingerprint, run.fixture)
