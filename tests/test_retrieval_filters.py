"""Tests for request-scoped retrieval filename filters."""

import unittest
from unittest.mock import Mock, patch

from backend.rag.utils import build_filename_filter_expression, retrieve_documents


class RetrievalFilterTests(unittest.TestCase):
    def test_filename_filter_is_empty_without_an_allowlist(self):
        self.assertEqual(build_filename_filter_expression(None), "")

    def test_filename_filter_deduplicates_and_quotes_names(self):
        expression = build_filename_filter_expression(["vivo-y200.md", "vivo-s19.md", "vivo-y200.md"])
        self.assertEqual(expression, 'filename in ["vivo-y200.md", "vivo-s19.md"]')

    def test_retrieve_documents_passes_filename_filter_to_hybrid_search(self):
        embedding_service = Mock()
        embedding_service.get_embeddings.return_value = [[0.1, 0.2]]
        milvus_store = Mock()
        milvus_store.hybrid_retrieve.return_value = []

        with patch("backend.rag.utils._embedding_service", embedding_service):
            with patch("backend.rag.utils._milvus_manager", milvus_store):
                result = retrieve_documents("Y200 battery", top_k=1, knowledge_filenames=["vivo-y200.md"])

        self.assertEqual(result["docs"], [])
        self.assertEqual(
            milvus_store.hybrid_retrieve.call_args.kwargs["filter_expr"],
            'chunk_level == 3 and filename in ["vivo-y200.md"]',
        )


if __name__ == "__main__":
    unittest.main()
