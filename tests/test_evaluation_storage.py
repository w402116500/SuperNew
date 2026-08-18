from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from backend.evaluation.storage import (
    EVALUATION_PARENT_CHUNK_TABLE,
    EVALUATION_REDIS_PREFIX,
    EvaluationParentChunkStore,
    EvaluationStorageConfig,
    EvaluationStorageConfigurationError,
)


class _MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}
        self.events: list[tuple[str, str]] = []

    def get_json(self, key: str):
        self.events.append(("get", key))
        return self.values.get(key)

    def set_json(self, key: str, value: dict) -> None:
        self.events.append(("set", key))
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.events.append(("delete", key))
        self.values.pop(key, None)


def _parent(chunk_id: str, *, level: int = 2) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": "# Operations\n\nA complete parent block.",
        "filename": "__rag_eval__unit__operations.md",
        "file_type": "Markdown",
        "file_path": "C:/evaluation/operations.md",
        "page_number": 0,
        "parent_chunk_id": "root" if level == 2 else "",
        "root_chunk_id": "root",
        "chunk_level": level,
        "chunk_idx": 4,
        "heading_path": "Operations > Rollback",
        "heading_level": 2,
        "source_start_index": 12,
        "source_end_index": 48,
        "content_kind": "paragraph",
        "previous_chunk_id": "previous",
        "next_chunk_id": "next",
        "chunking_strategy": "markdown_header_recursive_v1",
        "chunking_config_hash": "a" * 64,
    }


class EvaluationStorageConfigTests(TestCase):
    def test_missing_evaluation_url_is_rejected_without_business_fallback(self):
        with self.assertRaisesRegex(EvaluationStorageConfigurationError, "缺少 EVALUATION_DATABASE_URL"):
            EvaluationStorageConfig.from_urls(
                "",
                "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
            )

    def test_business_database_name_is_rejected(self):
        with self.assertRaisesRegex(EvaluationStorageConfigurationError, "不能指向业务数据库"):
            EvaluationStorageConfig.from_urls(
                "postgresql+psycopg2://evaluation:secret@localhost:15432/langchain_app",
                "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
            )

    def test_public_metadata_excludes_connection_secret(self):
        config = EvaluationStorageConfig.from_urls(
            "postgresql+psycopg2://evaluation:secret@localhost:15432/enterprise_rag_evaluation",
            "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
        )
        metadata = config.public_metadata("structured-chunking-001")

        self.assertEqual(metadata["evaluation_database_name"], "enterprise_rag_evaluation")
        self.assertNotIn("secret", repr(metadata))
        self.assertEqual(
            metadata["evaluation_redis_prefix"],
            "rag_eval_chunking:structured-chunking-001",
        )


class EvaluationParentChunkStoreTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.config = EvaluationStorageConfig.from_urls(
            "sqlite+pysqlite:///enterprise_rag_evaluation.sqlite",
            "sqlite+pysqlite:///langchain_app.sqlite",
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.cache = _MemoryCache()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _store(self, run_id: str) -> EvaluationParentChunkStore:
        return EvaluationParentChunkStore(
            run_id,
            config=self.config,
            engine=self.engine,
            session_factory=self.session_factory,
        )

    def test_schema_creation_creates_only_evaluation_table(self):
        store = self._store("run-a")
        store.initialize_schema()

        self.assertEqual(inspect(self.engine).get_table_names(), [EVALUATION_PARENT_CHUNK_TABLE])

    def test_upsert_caches_only_after_commit_and_preserves_structured_metadata(self):
        store = self._store("run-a")
        store.initialize_schema()
        with patch("backend.evaluation.storage.cache", self.cache):
            self.assertEqual(store.upsert_documents([_parent("l2-1")]), 1)
            loaded = store.get_documents_by_ids(["l2-1"])

        self.assertEqual(loaded[0]["heading_path"], "Operations > Rollback")
        self.assertEqual(loaded[0]["source_start_index"], 12)
        self.assertEqual(loaded[0]["chunking_strategy"], "markdown_header_recursive_v1")
        self.assertEqual(
            self.cache.events[0],
            ("set", f"{EVALUATION_REDIS_PREFIX}:run-a:parent_chunk:l2-1"),
        )

    def test_failed_write_does_not_publish_cache_entry(self):
        store = self._store("run-a")
        store.initialize_schema()
        broken = _parent("l2-1")
        broken["page_number"] = "not-a-number"
        with patch("backend.evaluation.storage.cache", self.cache):
            with self.assertRaises(ValueError):
                store.upsert_documents([broken])

        self.assertEqual(self.cache.values, {})

    def test_run_scoped_delete_does_not_remove_another_runs_chunk(self):
        first = self._store("run-a")
        second = self._store("run-b")
        first.initialize_schema()
        with patch("backend.evaluation.storage.cache", self.cache):
            first.upsert_documents([_parent("same-id")])
            second.upsert_documents([_parent("same-id")])
            self.assertEqual(first.delete_by_corpus_run(), 1)
            self.assertEqual(first.get_documents_by_ids(["same-id"]), [])
            self.assertEqual(len(second.get_documents_by_ids(["same-id"])), 1)

    def test_leaf_chunks_are_rejected_by_parent_store(self):
        store = self._store("run-a")
        store.initialize_schema()
        with patch("backend.evaluation.storage.cache", self.cache):
            with self.assertRaisesRegex(ValueError, "只能保存 L1/L2"):
                store.upsert_documents([_parent("l3-1", level=3)])

    def test_parent_store_commits_bounded_batches_and_reports_progress(self):
        store = self._store("run-a")
        store.initialize_schema()
        progress: list[int] = []
        with patch("backend.evaluation.storage.cache", self.cache):
            self.assertEqual(
                store.upsert_documents(
                    [_parent("l2-1"), _parent("l2-2"), _parent("l2-3")],
                    batch_size=2,
                    progress_callback=progress.append,
                ),
                3,
            )
        self.assertEqual(progress, [2, 3])
