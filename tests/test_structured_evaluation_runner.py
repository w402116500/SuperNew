from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.evaluation.runner import (
    _copy_non_target_vectors,
    _evaluation_parent_store_for_runtime,
    cleanup_run,
    prepare_run,
)
from backend.evaluation.storage import (
    EvaluationStorageConfig,
    EvaluationStorageConfigurationError,
)
from backend.indexing.document_loader import STRUCTURED_MARKDOWN_CHUNKING_STRATEGY


def _enterprise_metadata(directory: Path) -> dict:
    return {
        "required_document_count": 1,
        "ordinary_document_count": 0,
        "hard_document_count": 0,
        "document_map": {"doc-1": "__rag_eval__structured__doc-1.md"},
        "document_manifest": [],
        "case_split_manifest": None,
        "artifact_dir": str(directory / "markdown"),
        "chunking_strategy": STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        "chunking_config_hash": "c" * 64,
    }


class StructuredEvaluationRunnerTests(TestCase):
    def test_targeted_vector_copy_excludes_target_documents_and_is_checkpointed(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "vector-copy.json"
            source = Mock()
            source.collection_name = "rag_eval_enterpriserag_source"
            source.has_collection.return_value = True
            source.query_iterator.return_value = iter([
                [
                    {
                        "dense_embedding": [0.1, 0.2],
                        "text": "legacy",
                        "filename": "__rag_eval__source__enterprise__doc-other.md",
                        "file_type": "Markdown",
                        "file_path": "other.md",
                        "page_number": 0,
                        "chunk_idx": 1,
                        "chunk_id": "doc-other::l3::1",
                        "parent_chunk_id": "doc-other::l2::0",
                        "root_chunk_id": "doc-other::l1::0",
                        "chunk_level": 3,
                    },
                    {
                        "dense_embedding": [0.3, 0.4],
                        "text": "must not copy",
                        "filename": "__rag_eval__source__enterprise__doc-target.md",
                        "file_type": "Markdown",
                        "chunk_id": "doc-target::l3::1",
                        "chunk_level": 3,
                    },
                ],
            ])
            target = Mock()
            target.collection_name = "rag_eval_enterpriserag_target"
            target.get_chunks_by_ids.return_value = []
            result = _copy_non_target_vectors(
                source_store=source,
                target_store=target,
                source_collection_name=source.collection_name,
                source_corpus_run_id="source",
                target_corpus_run_id="target",
                target_document_ids={"doc-target"},
                checkpoint_path=checkpoint,
                batch_size=10,
                page_size=2,
            )
            checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["vector_copy_count"], 1)
        inserted = target.insert.call_args.args[0]
        self.assertEqual([row["chunk_id"] for row in inserted], ["doc-other::l3::1"])
        self.assertEqual(source.insert.call_count, 0)
        self.assertEqual(checkpoint_payload["vector_copy_count"], 1)

    def test_targeted_vector_copy_fails_closed_when_source_vector_is_missing(self):
        with TemporaryDirectory() as directory:
            source = Mock()
            source.collection_name = "rag_eval_enterpriserag_source"
            source.has_collection.return_value = True
            source.query_iterator.return_value = iter([[{
                "text": "missing vector",
                "filename": "__rag_eval__source__enterprise__doc-other.md",
                "chunk_id": "doc-other::l3::1",
                "chunk_level": 3,
            }]])
            target = Mock()
            target.collection_name = "rag_eval_enterpriserag_target"
            with self.assertRaisesRegex(RuntimeError, "缺少可安全复制"):
                _copy_non_target_vectors(
                    source_store=source,
                    target_store=target,
                    source_collection_name=source.collection_name,
                    source_corpus_run_id="source",
                    target_corpus_run_id="target",
                    target_document_ids=set(),
                    checkpoint_path=Path(directory) / "vector-copy.json",
                    batch_size=10,
                    page_size=2,
                )
            target.insert.assert_not_called()

    def test_targeted_rechunk_requires_frozen_manifest_before_storage_access(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "EVALUATION_DATABASE_URL": "postgresql+psycopg2://evaluation:secret@localhost:15432/enterprise_rag_evaluation",
            "DATABASE_URL": "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
        }, clear=False), patch("backend.evaluation.runner._evaluation_store") as milvus_store:
            output = Path(directory) / "targeted-run"
            with patch("backend.evaluation.runner.run_directory", return_value=output):
                with self.assertRaisesRegex(ValueError, "必须提供 --target-manifest"):
                    prepare_run(
                        dataset="enterpriserag",
                        run_id="targeted-run",
                        profile="full",
                        chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
                        rechunk_scope="targeted",
                    )
        self.assertFalse(output.exists())
        milvus_store.assert_not_called()

    def test_unsafe_evaluation_url_fails_before_collection_or_output_creation(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "EVALUATION_DATABASE_URL": "postgresql+psycopg2://evaluation:secret@localhost:15432/langchain_app",
            "DATABASE_URL": "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
        }, clear=False), patch("backend.evaluation.runner._evaluation_store") as milvus_store:
            output = Path(directory) / "new-run"
            with patch("backend.evaluation.runner.run_directory", return_value=output):
                with self.assertRaises(EvaluationStorageConfigurationError):
                    prepare_run(
                        dataset="enterpriserag",
                        run_id="new-run",
                        profile="full",
                        chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
                    )

        self.assertFalse(output.exists())
        milvus_store.assert_not_called()

    def test_structured_prepare_uses_dedicated_parent_store_and_safe_manifest_fields(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "structured-run"
            storage_config = EvaluationStorageConfig.from_urls(
                "postgresql+psycopg2://evaluation:secret@localhost:15432/enterprise_rag_evaluation",
                "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
            )
            parent_store = Mock()
            milvus_store = Mock()
            writer = Mock()
            parent_chunks = [{"chunk_id": "l1", "chunk_level": 1, "filename": "doc.md"}]
            leaf_chunks = [{"chunk_id": "l3", "chunk_level": 3, "filename": "doc.md", "text": "leaf"}]
            with patch("backend.evaluation.runner.run_directory", return_value=output), patch(
                "backend.evaluation.runner.EvaluationStorageConfig.from_env", return_value=storage_config
            ), patch(
                "backend.evaluation.runner.EvaluationParentChunkStore", return_value=parent_store
            ), patch(
                "backend.evaluation.runner._prepare_enterprise_documents",
                return_value=([], parent_chunks, leaf_chunks, _enterprise_metadata(output)),
            ), patch("backend.evaluation.runner._evaluation_store", return_value=milvus_store), patch(
                "backend.evaluation.runner.MilvusWriter", return_value=writer
            ):
                prepare_run(
                    dataset="enterpriserag",
                    run_id="structured-run",
                    profile="full",
                    chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
                )

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            config = json.loads((output / "config.json").read_text(encoding="utf-8"))

        parent_store.check_connection.assert_called_once()
        parent_store.upsert_documents.assert_called_once()
        self.assertEqual(parent_store.upsert_documents.call_args.args[0], parent_chunks)
        self.assertEqual(parent_store.upsert_documents.call_args.kwargs["batch_size"], 500)
        writer.write_documents.assert_called_once()
        self.assertEqual(writer.write_documents.call_args.args[0], leaf_chunks)
        self.assertGreater(writer.write_documents.call_args.kwargs["batch_size"], 0)
        self.assertEqual(manifest["evaluation_storage_mode"], "isolated_postgresql")
        self.assertEqual(manifest["evaluation_database_name"], "enterprise_rag_evaluation")
        self.assertNotIn("secret", json.dumps(manifest))
        self.assertEqual(config["document_chunking_strategy"], STRUCTURED_MARKDOWN_CHUNKING_STRATEGY)

    def test_structured_runtime_rejects_business_store_fallback(self):
        storage_config = EvaluationStorageConfig.from_urls(
            "postgresql+psycopg2://evaluation:secret@localhost:15432/enterprise_rag_evaluation",
            "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
        )
        parent_store = Mock()
        with patch("backend.evaluation.runner.EvaluationStorageConfig.from_env", return_value=storage_config), patch(
            "backend.evaluation.runner.EvaluationParentChunkStore", return_value=parent_store
        ):
            actual = _evaluation_parent_store_for_runtime({
                "run_id": "structured-run",
                "document_chunking_strategy": STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
            })

        self.assertIs(actual, parent_store)
        parent_store.check_connection.assert_called_once()

    def test_isolated_cleanup_only_deletes_its_run_scoped_parent_records(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "structured-run"
            output.mkdir()
            (output / "manifest.json").write_text(json.dumps({
                "collection_name": "rag_eval_enterpriserag_structured_run",
                "evaluation_storage_mode": "isolated_postgresql",
            }), encoding="utf-8")
            storage_config = EvaluationStorageConfig.from_urls(
                "postgresql+psycopg2://evaluation:secret@localhost:15432/enterprise_rag_evaluation",
                "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
            )
            parent_store = Mock()
            parent_store.delete_by_corpus_run.return_value = 3
            milvus_store = Mock()
            with patch("backend.evaluation.runner.run_directory", return_value=output), patch(
                "backend.evaluation.runner.EvaluationStorageConfig.from_env", return_value=storage_config
            ), patch(
                "backend.evaluation.runner.EvaluationParentChunkStore", return_value=parent_store
            ), patch("backend.evaluation.runner._evaluation_store", return_value=milvus_store):
                result = cleanup_run(dataset="enterpriserag", run_id="structured-run")

        parent_store.check_connection.assert_called_once()
        parent_store.delete_by_corpus_run.assert_called_once()
        self.assertEqual(result["deleted_parent_chunks"], 3)
