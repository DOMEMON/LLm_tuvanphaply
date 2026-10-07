from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utcnow


class IngestionRun(Base):
    __tablename__ = "rag_ingestion_runs"
    corpus_version: Mapped[str] = mapped_column(String(160), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    fixture: Mapped[bool] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Source(Base):
    __tablename__ = "rag_sources"
    corpus_version: Mapped[str] = mapped_column(
        ForeignKey("rag_ingestion_runs.corpus_version"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Procedure(Base):
    __tablename__ = "rag_procedures"
    corpus_version: Mapped[str] = mapped_column(
        ForeignKey("rag_ingestion_runs.corpus_version"), primary_key=True
    )
    procedure_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Fragment(Base):
    __tablename__ = "rag_fragments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["corpus_version", "source_id"], ["rag_sources.corpus_version", "rag_sources.source_id"]
        ),
        ForeignKeyConstraint(
            ["corpus_version", "procedure_id"],
            ["rag_procedures.corpus_version", "rag_procedures.procedure_id"],
        ),
    )
    corpus_version: Mapped[str] = mapped_column(String(160), primary_key=True)
    fragment_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(160))
    procedure_id: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSONB)
