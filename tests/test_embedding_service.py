"""嵌入服务的本地缓存和惰性初始化行为测试。"""

import os
import unittest
from unittest.mock import Mock, patch

from backend.indexing.embedding import (
    EmbeddingService,
    _create_dense_embedder,
    embedding_public_config,
)


class EmbeddingServiceTests(unittest.TestCase):
    def test_default_embedder_uses_local_files_only(self):
        with patch("backend.indexing.embedding.HuggingFaceEmbeddings") as embedder_class:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EMBEDDING_PROVIDER", None)
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

    def test_siliconflow_uses_openai_compatible_configuration(self):
        env = {
            "EMBEDDING_PROVIDER": "siliconflow",
            "EMBEDDING_MODEL": "BAAI/bge-m3",
            "EMBEDDING_API_KEY": "test-key",
            "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
            "EMBEDDING_BATCH_SIZE": "32",
            "EMBEDDING_TIMEOUT_SECONDS": "45",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("backend.indexing.embedding.OpenAIEmbeddings") as embedder_class:
                _create_dense_embedder()

        self.assertEqual(embedder_class.call_args.kwargs["model"], "BAAI/bge-m3")
        self.assertEqual(embedder_class.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(
            embedder_class.call_args.kwargs["base_url"],
            "https://api.siliconflow.cn/v1",
        )
        self.assertEqual(embedder_class.call_args.kwargs["chunk_size"], 32)
        self.assertEqual(embedder_class.call_args.kwargs["timeout"], 45.0)
        self.assertFalse(embedder_class.call_args.kwargs["check_embedding_ctx_length"])

    def test_siliconflow_requires_api_key(self):
        with patch.dict(
            os.environ,
            {"EMBEDDING_PROVIDER": "siliconflow"},
            clear=False,
        ):
            os.environ.pop("EMBEDDING_API_KEY", None)
            os.environ.pop("SILICONFLOW_API_KEY", None)
            with self.assertRaisesRegex(RuntimeError, "必须设置"):
                _create_dense_embedder()

    def test_public_config_does_not_include_api_key(self):
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_PROVIDER": "siliconflow",
                "EMBEDDING_API_KEY": "must-not-be-recorded",
            },
            clear=False,
        ):
            config = embedding_public_config()

        self.assertEqual(config["embedding_provider"], "siliconflow")
        self.assertNotIn("api_key", config)
        self.assertNotIn("must-not-be-recorded", str(config))


if __name__ == "__main__":
    unittest.main()
