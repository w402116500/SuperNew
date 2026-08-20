from __future__ import annotations

import threading
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.indexing.chunk_metadata import STRUCTURED_CHUNK_METADATA_FIELDS
from backend.indexing.milvus_client import MilvusSettings, MilvusStore
from backend.indexing.milvus_writer import MilvusWriter
from backend.rag.utils import _candidate_snapshot


def _leaf() -> dict:
    return {
        "text": "Section: Operations > Rollback\n\nRestore the known-good snapshot.",
        "filename": "__rag_eval__structured__operations.md",
        "file_type": "Markdown",
        "file_path": "C:/evaluation/operations.md",
        "page_number": 0,
        "chunk_idx": 7,
        "chunk_id": "operations.md::p0::l3::2",
        "parent_chunk_id": "operations.md::p0::l2::1",
        "root_chunk_id": "operations.md::p0::l1::0",
        "chunk_level": 3,
        "heading_path": "Operations > Rollback",
        "heading_level": 2,
        "source_start_index": 41,
        "source_end_index": 78,
        "content_kind": "paragraph",
        "previous_chunk_id": "operations.md::p0::l3::1",
        "next_chunk_id": "operations.md::p0::l3::3",
        "chunking_strategy": "markdown_header_recursive_v1",
        "chunking_config_hash": "b" * 64,
    }


class StructuredChunkMetadataTests(TestCase):
    def test_writer_overlaps_embedding_batches_but_inserts_in_batch_order(self):
        embedding = Mock()
        state = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        def embed(texts):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return [[0.1, 0.2] for _ in texts]

        embedding.get_embeddings.side_effect = embed
        store = Mock()
        docs = [{**_leaf(), "chunk_id": f"chunk-{index}", "text": f"text-{index}"} for index in range(4)]
        with patch.dict("os.environ", {"EMBEDDING_MAX_WORKERS": "2"}, clear=False):
            MilvusWriter(embedding_service=embedding, milvus_manager=store).write_documents(
                docs,
                batch_size=1,
                embedding_workers=2,
            )
        self.assertGreaterEqual(state["max_active"], 2)
        inserted_ids = [call.args[0][0]["chunk_id"] for call in store.insert.call_args_list]
        self.assertEqual(inserted_ids, ["chunk-0", "chunk-1", "chunk-2", "chunk-3"])

    def test_writer_retries_one_failed_embedding_batch(self):
        embedding = Mock()
        embedding.get_embeddings.side_effect = [RuntimeError("temporary"), [[0.1, 0.2]]]
        store = Mock()
        with patch("backend.indexing.milvus_writer.time.sleep"):
            MilvusWriter(
                embedding_service=embedding,
                milvus_manager=store,
            ).write_documents([_leaf()], max_retries=1)
        self.assertEqual(embedding.get_embeddings.call_count, 2)
        store.insert.assert_called_once()

    def test_writer_rejects_partial_embedding_batch(self):
        embedding = Mock()
        embedding.get_embeddings.return_value = []
        store = Mock()
        with patch("backend.indexing.milvus_writer.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "返回数量与输入文档数量不一致"):
                MilvusWriter(
                    embedding_service=embedding,
                    milvus_manager=store,
                ).write_documents([_leaf()], max_retries=0)
        store.insert.assert_not_called()

    def test_writer_retries_one_failed_milvus_batch(self):
        embedding = Mock()
        embedding.get_embeddings.return_value = [[0.1, 0.2]]
        store = Mock()
        store.insert.side_effect = [RuntimeError("temporary"), {"insert_count": 1}]
        with patch("backend.indexing.milvus_writer.time.sleep"):
            MilvusWriter(
                embedding_service=embedding,
                milvus_manager=store,
            ).write_documents([_leaf()], max_retries=1)
        self.assertEqual(store.insert.call_count, 2)

    def test_writer_persists_structured_fields_in_dynamic_milvus_payload(self):
        embedding = Mock()
        embedding.get_embeddings.return_value = [[0.1, 0.2]]
        store = Mock()
        with patch("backend.indexing.milvus_writer.os.getenv", return_value="2"):
            MilvusWriter(embedding_service=embedding, milvus_manager=store).write_documents([_leaf()])

        inserted = store.insert.call_args.args[0][0]
        self.assertTrue(all(field in inserted for field in STRUCTURED_CHUNK_METADATA_FIELDS))
        self.assertEqual(inserted["heading_path"], "Operations > Rollback")

    def test_milvus_result_and_candidate_trace_keep_structured_fields(self):
        store = MilvusStore(MilvusSettings("localhost", "19530", "unit", "http://localhost:19530", 1.0))
        client = Mock()
        hit = {"id": 1, "distance": 0.9, **_leaf()}
        client.hybrid_search.return_value = [[hit]]
        with patch.object(store, "_run", side_effect=lambda operation: operation(client)):
            results = store.hybrid_retrieve([0.1, 0.2], "rollback", top_k=1)

        self.assertTrue(all(field in results[0] for field in STRUCTURED_CHUNK_METADATA_FIELDS))
        snapshot = _candidate_snapshot(results[0], raw_rank=1)
        self.assertEqual(snapshot["heading_path"], "Operations > Rollback")
        self.assertEqual(snapshot["source_start_index"], 41)
