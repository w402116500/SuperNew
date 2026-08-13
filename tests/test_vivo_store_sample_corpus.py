"""vivo 店铺样例语料和评测集必须保持来源可追溯。"""

import json
from pathlib import Path
import unittest

from backend.indexing.document_loader import DocumentLoader


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = PROJECT_ROOT / "data" / "rag-samples" / "vivo-store-v1"
PRODUCTS_DIR = CORPUS_ROOT / "knowledge" / "products"
CASES_PATH = CORPUS_ROOT / "evaluation" / "test-cases.jsonl"
EXPECTED_PRODUCTS = {
    "vivo-y200.md",
    "vivo-y300-pro.md",
    "vivo-s19.md",
    "vivo-s20.md",
    "vivo-x200.md",
    "vivo-x200-pro.md",
    "vivo-x-fold3-pro.md",
}


class VivoStoreSampleCorpusTests(unittest.TestCase):
    def test_product_markdown_files_are_complete_and_loadable(self):
        product_paths = sorted(PRODUCTS_DIR.glob("*.md"))
        self.assertEqual({path.name for path in product_paths}, EXPECTED_PRODUCTS)

        loader = DocumentLoader()
        for path in product_paths:
            documents = loader.load_document(str(path), path.name)
            self.assertTrue(any(document["chunk_level"] == 3 for document in documents), path.name)
            self.assertEqual({document["file_type"] for document in documents}, {"Markdown"})

    def test_evaluation_cases_reference_existing_product_documents(self):
        cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(cases), 27)

        referenced = {
            Path(document_path).name
            for case in cases
            for document_path in case["expected_docs"]
        }
        self.assertTrue(referenced.issubset(EXPECTED_PRODUCTS))
        self.assertEqual(referenced, EXPECTED_PRODUCTS)


if __name__ == "__main__":
    unittest.main()
