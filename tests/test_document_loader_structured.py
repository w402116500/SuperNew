from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.indexing.document_loader import (
    DEFAULT_CHUNKING_STRATEGY,
    STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
    DocumentLoader,
)


class StructuredMarkdownDocumentLoaderTests(TestCase):
    def _load(self, markdown: str, *, strategy: str) -> tuple[str, list[dict]]:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "operations.md"
            path.write_text(markdown, encoding="utf-8")
            documents = DocumentLoader(chunking_strategy=strategy).load_document(
                str(path), path.name
            )
        return markdown, documents

    def test_structured_strategy_preserves_heading_path_offsets_and_parent_chain(self):
        paragraph_one = "First rollback condition. " * 45
        paragraph_two = "Second rollback condition. " * 45
        markdown = (
            "# Operations\n\n"
            "Overview of the service.\n\n"
            "## Rollback\n\n"
            f"{paragraph_one}\n\n{paragraph_two}\n\n"
            "## Monitoring\n\n"
            "Check the alert stream before declaring recovery.\n"
        )
        source, documents = self._load(
            markdown,
            strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        )
        chunk_ids = {document["chunk_id"] for document in documents}
        rollback_leaves = [
            document
            for document in documents
            if document["chunk_level"] == 3
            and document["heading_path"] == "Operations > Rollback"
        ]

        self.assertGreaterEqual(len(rollback_leaves), 2)
        self.assertTrue(all(document["chunking_strategy"] == STRUCTURED_MARKDOWN_CHUNKING_STRATEGY for document in documents))
        self.assertEqual({len(document["chunking_config_hash"]) for document in documents}, {64})
        self.assertTrue(all(document["source_start_index"] < document["source_end_index"] for document in documents))
        self.assertTrue(all(source[document["source_start_index"]:document["source_end_index"]].strip() in document["text"] for document in documents))
        self.assertTrue(all(document["parent_chunk_id"] in chunk_ids for document in rollback_leaves))
        self.assertTrue(all(document["root_chunk_id"] in chunk_ids for document in rollback_leaves))
        self.assertTrue(any(document["next_chunk_id"] for document in rollback_leaves))
        self.assertTrue(any(document["previous_chunk_id"] for document in rollback_leaves))
        self.assertTrue(all("Monitoring" not in document["text"] for document in rollback_leaves))

    def test_lists_tables_and_code_fences_are_never_cut_in_half(self):
        list_item = "x" * 420
        table_cell = "y" * 850
        code_body = "# Not a real heading\n" + ("print('z')\n" * 110)
        markdown = (
            "# Release\n\n"
            "## Checklist\n\n"
            f"- first item {list_item}\n"
            f"- second item {list_item}\n\n"
            "## Limits\n\n"
            "| Metric | Value |\n"
            "| --- | --- |\n"
            f"| timeout | {table_cell} |\n\n"
            "## Script\n\n"
            "```python\n"
            f"{code_body}"
            "```\n"
        )
        source, documents = self._load(
            markdown,
            strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        )
        leaves = [document for document in documents if document["chunk_level"] == 3]
        list_chunk = next(document for document in leaves if document["content_kind"] == "list")
        table_chunk = next(document for document in leaves if document["content_kind"] == "table")
        code_chunk = next(document for document in leaves if document["content_kind"] == "code")

        self.assertIn("- first item", list_chunk["text"])
        self.assertIn("- second item", list_chunk["text"])
        self.assertIn("| Metric | Value |", table_chunk["text"])
        self.assertIn("| timeout |", table_chunk["text"])
        self.assertEqual(code_chunk["text"].count("```"), 2)
        self.assertEqual(code_chunk["heading_path"], "Release > Script")
        self.assertNotIn("Release > Not a real heading", {item["heading_path"] for item in documents})
        self.assertTrue(all(source[item["source_start_index"]:item["source_end_index"]].strip() in item["text"] for item in (list_chunk, table_chunk, code_chunk)))

    def test_table_without_outer_pipes_stays_one_table_atom(self):
        value = "y" * 850
        markdown = (
            "# Metrics\n\n"
            "Config                    | p50 ms | p95 ms | $/1k tokens\n"
            "--------------------------|--------|--------|------------\n"
            f"edge-dispatch-prefetch    | 72     | 180    | {value}\n"
            f"core-tiling-multiplex     | 120    | 350    | {value}\n"
        )
        source, documents = self._load(
            markdown,
            strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        )
        table_chunks = [
            document
            for document in documents
            if document["chunk_level"] == 3 and document["content_kind"] == "table"
        ]

        self.assertEqual(len(table_chunks), 1)
        self.assertIn("Config                    | p50 ms", table_chunks[0]["text"])
        self.assertIn("core-tiling-multiplex", table_chunks[0]["text"])
        self.assertIn(
            source[table_chunks[0]["source_start_index"]:table_chunks[0]["source_end_index"]].strip(),
            table_chunks[0]["text"],
        )

    def test_default_strategy_remains_the_legacy_recursive_contract(self):
        markdown = "# Operations\n\n## Rollback\n\n- Restore the known-good snapshot.\n"
        _, documents = self._load(markdown, strategy=DEFAULT_CHUNKING_STRATEGY)

        self.assertTrue(all("chunking_strategy" not in document for document in documents))
        self.assertTrue(all("heading_path" not in document for document in documents))
        self.assertEqual({document["chunk_level"] for document in documents}, {1, 2, 3})

    def test_unknown_strategy_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "不支持的分块策略"):
            DocumentLoader(chunking_strategy="adaptive_without_approval")
