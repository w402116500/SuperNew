"""嵌入服务的本地缓存和惰性初始化行为测试。"""

import os
import unittest
from unittest.mock import Mock, patch

from backend.indexing.embedding import EmbeddingService, _create_dense_embedder


class EmbeddingServiceTests(unittest.TestCase):
    def test_default_embedder_uses_local_files_only(self):
        with patch("backend.indexing.embedding.HuggingFaceEmbeddings") as embedder_class:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EMBEDDING_LOCAL_FILES_ONLY", None)
                _create_dense_embedder()

        self.assertTrue(embedder_class.call_args.kwargs["model_kwargs"]["local_files_only"])

    def test_model_is_loaded_once_when_first_needed(self):
        embedder = Mock()
        embedder.embed_documents.return_value = [[0.1], [0.2]]
        service = EmbeddingService()

        with patch("backend.indexing.embedding._create_dense_embedder", return_value=embedder) as create:
            self.assertEqual(service.get_embeddings(["a", "b"]), [[0.1], [0.2]])
            self.assertEqual(service.get_embeddings(["c"]), [[0.1], [0.2]])

        create.assert_called_once_with()
        self.assertEqual(embedder.embed_documents.call_count, 2)


if __name__ == "__main__":
    unittest.main()
