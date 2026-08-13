from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.indexing.document_loader import DocumentLoader


class MarkdownDocumentLoaderTests(unittest.TestCase):
    def test_load_document_splits_markdown_into_retrievable_leaf_chunks(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "product.md"
            source.write_text(
                "# vivo Y200 商品资料\n\n"
                "## 电池与续航\n\n"
                "- 电池典型容量：6000mAh。\n"
                "- 有线充电规格：80W。\n",
                encoding="utf-8",
            )

            documents = DocumentLoader().load_document(str(source), source.name)
            levels = Counter(document["chunk_level"] for document in documents)

            self.assertGreaterEqual(levels[1], 1)
            self.assertGreaterEqual(levels[2], 1)
            self.assertGreaterEqual(levels[3], 1)
            self.assertEqual({document["file_type"] for document in documents}, {"Markdown"})
            self.assertTrue(any("6000mAh" in document["text"] for document in documents))
