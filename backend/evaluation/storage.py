"""Isolated parent-chunk storage for offline EnterpriseRAG evaluations.

The application-facing ``ParentChunkStore`` deliberately remains on the
business database.  Structured-chunking experiments use this module instead:
it requires a dedicated database URL, a run-scoped table key, and a separate
Redis namespace.  Nothing in this module creates a PostgreSQL database; the
schema creation method is an explicit operator action.
"""

from __future__ import annotations

import os
from itertools import islice
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable

from sqlalchemy import DateTime, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend.infra.cache import cache


EVALUATION_STORAGE_SCHEMA_VERSION = "1"
EVALUATION_REDIS_PREFIX = "rag_eval_chunking"
EVALUATION_PARENT_CHUNK_TABLE = "evaluation_parent_chunks"
DEFAULT_BUSINESS_DATABASE_URL = (
    "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app"
)


class EvaluationStorageConfigurationError(ValueError):
    """Raised before an evaluation can write to an unsafe parent-chunk store."""


def _utc_now_naive() -> datetime:
    """Match the existing PostgreSQL ``DateTime`` convention used by the app."""
    return datetime.now(UTC).replace(tzinfo=None)


class EvaluationBase(DeclarativeBase):
    """Metadata isolated from the business ORM's ``Base``."""


class EvaluationParentChunk(EvaluationBase):
    """L1/L2 evaluation chunks plus the audit metadata for structured splitting."""

    __tablename__ = EVALUATION_PARENT_CHUNK_TABLE

    corpus_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parent_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    root_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    chunk_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    heading_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_start_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_end_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_kind: Mapped[str] = mapped_column(String(32), default="mixed", nullable=False)
    previous_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    next_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    chunking_strategy: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    chunking_config_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)


@dataclass(frozen=True)
class EvaluationStorageConfig:
    """Non-secret configuration required to access the dedicated evaluation DB."""

    database_url: str
    database_name: str

    @classmethod
    def from_env(cls) -> "EvaluationStorageConfig":
        """Read the explicit evaluation URL and reject a business-DB fallback."""
        return cls.from_urls(
            os.getenv("EVALUATION_DATABASE_URL", ""),
            os.getenv("DATABASE_URL", DEFAULT_BUSINESS_DATABASE_URL),
        )

    @classmethod
    def from_urls(
        cls,
        evaluation_database_url: str,
        business_database_url: str,
    ) -> "EvaluationStorageConfig":
        """Validate two URLs without connecting to either database."""
        if not evaluation_database_url or not evaluation_database_url.strip():
            raise EvaluationStorageConfigurationError(
                "缺少 EVALUATION_DATABASE_URL；结构化评测不得回退到业务数据库"
            )
        try:
            evaluation_url = make_url(evaluation_database_url)
            business_url = make_url(business_database_url)
        except (ArgumentError, TypeError, ValueError) as exc:
            raise EvaluationStorageConfigurationError(
                "评测或业务数据库 URL 无法解析"
            ) from exc

        evaluation_name = (evaluation_url.database or "").strip()
        business_name = (business_url.database or "").strip()
        if not evaluation_name:
            raise EvaluationStorageConfigurationError("EVALUATION_DATABASE_URL 必须包含数据库名")
        if not business_name:
            raise EvaluationStorageConfigurationError("DATABASE_URL 必须包含业务数据库名")
        if evaluation_name.casefold() == business_name.casefold():
            raise EvaluationStorageConfigurationError(
                "EVALUATION_DATABASE_URL 不能指向业务数据库"
            )
        return cls(database_url=evaluation_database_url, database_name=evaluation_name)

    def public_metadata(self, corpus_run_id: str) -> dict[str, str]:
        """Return the safe, credential-free storage facts recorded in manifests."""
        return {
            "evaluation_storage_mode": "isolated_postgresql",
            "evaluation_database_name": self.database_name,
            "evaluation_parent_chunk_table": EVALUATION_PARENT_CHUNK_TABLE,
            "evaluation_storage_schema_version": EVALUATION_STORAGE_SCHEMA_VERSION,
            "evaluation_redis_prefix": f"{EVALUATION_REDIS_PREFIX}:{corpus_run_id}",
        }


class EvaluationParentChunkStore:
    """Run-scoped parent store backed by the dedicated evaluation database."""

    def __init__(
        self,
        corpus_run_id: str,
        *,
        config: EvaluationStorageConfig | None = None,
        engine: Engine | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.corpus_run_id = (corpus_run_id or "").strip()
        if not self.corpus_run_id:
            raise EvaluationStorageConfigurationError("评测父块存储必须提供 corpus_run_id")
        self.config = config or EvaluationStorageConfig.from_env()
        self._engine = engine or create_engine(self.config.database_url, pool_pre_ping=True)
        self._session_factory = session_factory or sessionmaker(
            bind=self._engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def check_connection(self) -> None:
        """Verify the dedicated DB before a caller creates a collection or writes chunks."""
        try:
            with self._engine.connect():
                pass
        except Exception as exc:
            raise EvaluationStorageConfigurationError(
                f"无法连接评测父块数据库 {self.config.database_name}"
            ) from exc

    def initialize_schema(self) -> None:
        """Create only the evaluation parent-chunk table in an existing database."""
        EvaluationBase.metadata.create_all(
            self._engine,
            tables=[EvaluationParentChunk.__table__],
        )

    def _cache_key(self, chunk_id: str) -> str:
        return f"{EVALUATION_REDIS_PREFIX}:{self.corpus_run_id}:parent_chunk:{chunk_id}"

    @staticmethod
    def _to_dict(item: EvaluationParentChunk) -> dict[str, Any]:
        return {
            "text": item.text,
            "filename": item.filename,
            "file_type": item.file_type,
            "file_path": item.file_path,
            "page_number": item.page_number,
            "chunk_id": item.chunk_id,
            "parent_chunk_id": item.parent_chunk_id,
            "root_chunk_id": item.root_chunk_id,
            "chunk_level": item.chunk_level,
            "chunk_idx": item.chunk_idx,
            "heading_path": item.heading_path,
            "heading_level": item.heading_level,
            "source_start_index": item.source_start_index,
            "source_end_index": item.source_end_index,
            "content_kind": item.content_kind,
            "previous_chunk_id": item.previous_chunk_id,
            "next_chunk_id": item.next_chunk_id,
            "chunking_strategy": item.chunking_strategy,
            "chunking_config_hash": item.chunking_config_hash,
        }

    @staticmethod
    def _payload(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": str(doc.get("text") or ""),
            "filename": str(doc.get("filename") or ""),
            "file_type": str(doc.get("file_type") or ""),
            "file_path": str(doc.get("file_path") or ""),
            "page_number": int(doc.get("page_number", 0) or 0),
            "parent_chunk_id": str(doc.get("parent_chunk_id") or ""),
            "root_chunk_id": str(doc.get("root_chunk_id") or ""),
            "chunk_level": int(doc.get("chunk_level", 0) or 0),
            "chunk_idx": int(doc.get("chunk_idx", 0) or 0),
            "heading_path": str(doc.get("heading_path") or ""),
            "heading_level": int(doc.get("heading_level", 0) or 0),
            "source_start_index": int(doc.get("source_start_index", 0) or 0),
            "source_end_index": int(doc.get("source_end_index", 0) or 0),
            "content_kind": str(doc.get("content_kind") or "mixed"),
            "previous_chunk_id": str(doc.get("previous_chunk_id") or ""),
            "next_chunk_id": str(doc.get("next_chunk_id") or ""),
            "chunking_strategy": str(doc.get("chunking_strategy") or ""),
            "chunking_config_hash": str(doc.get("chunking_config_hash") or ""),
            "updated_at": _utc_now_naive(),
        }

    def upsert_documents(
        self,
        docs: Iterable[dict[str, Any]],
        *,
        batch_size: int = 500,
        progress_callback: Callable[[int], None] | None = None,
    ) -> int:
        """Commit L1/L2 chunks in bounded single-writer batches.

        PostgreSQL writes remain serialized so two parser workers cannot race on
        the same parent ID.  Each committed batch publishes its Redis entries
        only after commit; callers can checkpoint the cumulative count.
        """
        if batch_size <= 0:
            raise ValueError("EvaluationParentChunkStore batch_size 必须是正整数")
        iterator = iter(docs)
        total_upserted = 0
        while batch := list(islice(iterator, batch_size)):
            total_upserted += self._upsert_batch(batch)
            if progress_callback:
                progress_callback(total_upserted)
        return total_upserted

    def _upsert_batch(self, docs: list[dict[str, Any]]) -> int:
        """Write one transaction and publish only its committed cache entries."""
        if not docs:
            return 0
        db = self._session_factory()
        cache_updates: list[tuple[str, dict[str, Any]]] = []
        upserted = 0
        try:
            for doc in docs:
                chunk_id = str(doc.get("chunk_id") or "").strip()
                if not chunk_id:
                    continue
                if int(doc.get("chunk_level", 0) or 0) not in {1, 2}:
                    raise ValueError("EvaluationParentChunkStore 只能保存 L1/L2 父块")
                payload = self._payload(doc)
                record = db.get(EvaluationParentChunk, (self.corpus_run_id, chunk_id))
                if record is None:
                    record = EvaluationParentChunk(
                        corpus_run_id=self.corpus_run_id,
                        chunk_id=chunk_id,
                        **payload,
                    )
                    db.add(record)
                else:
                    for field, value in payload.items():
                        setattr(record, field, value)
                cache_updates.append((
                    self._cache_key(chunk_id),
                    {"chunk_id": chunk_id, **{key: value for key, value in payload.items() if key != "updated_at"}},
                ))
                upserted += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        for cache_key, payload in cache_updates:
            cache.set_json(cache_key, payload)
        return upserted

    def get_documents_by_ids(self, chunk_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Read only this corpus run's parents, retaining the caller's ID order."""
        requested_ids = [str(item or "").strip() for item in chunk_ids]
        requested_ids = [item for item in requested_ids if item]
        if not requested_ids:
            return []

        found: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for chunk_id in requested_ids:
            cached = cache.get_json(self._cache_key(chunk_id))
            if cached:
                found[chunk_id] = cached
            else:
                missing.append(chunk_id)

        if missing:
            db = self._session_factory()
            try:
                rows = (
                    db.query(EvaluationParentChunk)
                    .filter(EvaluationParentChunk.corpus_run_id == self.corpus_run_id)
                    .filter(EvaluationParentChunk.chunk_id.in_(missing))
                    .all()
                )
                for row in rows:
                    payload = self._to_dict(row)
                    found[row.chunk_id] = payload
                    cache.set_json(self._cache_key(row.chunk_id), payload)
            finally:
                db.close()
        return [found[chunk_id] for chunk_id in requested_ids if chunk_id in found]

    def delete_by_corpus_run(self) -> int:
        """Delete only rows and known cache keys belonging to this corpus run."""
        db = self._session_factory()
        try:
            rows = (
                db.query(EvaluationParentChunk.chunk_id)
                .filter(EvaluationParentChunk.corpus_run_id == self.corpus_run_id)
                .all()
            )
            chunk_ids = [row[0] for row in rows]
            if not chunk_ids:
                return 0
            db.query(EvaluationParentChunk).filter(
                EvaluationParentChunk.corpus_run_id == self.corpus_run_id
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        for chunk_id in chunk_ids:
            cache.delete(self._cache_key(chunk_id))
        return len(chunk_ids)
