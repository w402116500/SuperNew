from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.indexing.document_loader import DocumentLoader
from backend.indexing.semantic_chunking import (
    SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
    build_semantic_boundary_plan,
    split_english_sentences,
)


class SemanticChunkingTests(TestCase):
    def test_sentence_ranges_cover_complete_sentences(self):
        text = 'First sentence. "Second sentence!" Third sentence? Tail without punctuation'
        ranges = split_english_sentences(text)
        self.assertEqual(
            [span.text for span in ranges],
            ['First sentence.', '"Second sentence!"', 'Third sentence?', 'Tail without punctuation'],
        )

    def test_long_paragraph_uses_one_batch_embedding_call_and_records_boundaries(self):
        text = " ".join(f"Topic {index} explains an operational detail with enough context." for index in range(35))
        calls: list[list[str]] = []

        def embedder(windows: list[str]) -> list[list[float]]:
            calls.append(windows)
            # Deliberate low similarity around the middle of the paragraph.
            return [[1.0, 0.0] if index != 4 else [0.0, 1.0] for index in range(len(windows))]

        plan = build_semantic_boundary_plan(
            text,
            document_id="operations.md",
            source_start=100,
            embedder=embedder,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan["decision"], "semantic_applied")
        self.assertEqual(plan["source_start"], 100)
        self.assertTrue(plan["sentence_ranges"])
        self.assertTrue(plan["candidate_boundaries"])
        self.assertTrue(plan["selected_boundaries"])
        self.assertEqual(plan["parameters"]["sentence_window"], 3)

    def test_loader_consumes_frozen_plan_and_keeps_structural_atoms(self):
        paragraph = " ".join(f"Sentence {index} carries a separate fact." for index in range(35))
        markdown = (
            "# Operations\n\n"
            f"{paragraph}\n\n"
            "## Checklist\n\n"
            "- Keep the rollback snapshot.\n"
            "- Verify the alert stream.\n"
        )
        paragraph_start = markdown.index("Sentence")
        paragraph_end = paragraph_start + len(paragraph)
        # The plan uses absolute offsets, matching source_start/source_end in JSONL.
        plan = {
            "operations.md": {
                "document_id": "operations.md",
                "source_start": paragraph_start,
                "source_end": paragraph_end,
                "decision": "planned",
                "selected_boundaries": [paragraph_start + len(paragraph) // 2],
            }
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "operations.md"
            path.write_text(markdown, encoding="utf-8")
            documents = DocumentLoader(
                chunking_strategy=SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
                semantic_boundary_plan=plan,
            ).load_document(str(path), path.name)

        self.assertTrue(documents)
        self.assertTrue(all(item["chunking_strategy"] == SEMANTIC_MARKDOWN_CHUNKING_STRATEGY for item in documents))
        self.assertTrue(all(item["chunking_config_hash"] for item in documents))
        self.assertTrue(any(item["content_kind"] == "paragraph" for item in documents))
        list_chunks = [item for item in documents if "- Keep the rollback snapshot." in item["text"]]
        self.assertTrue(list_chunks)
        self.assertIn("- Verify the alert stream.", list_chunks[0]["text"])
        self.assertTrue(all(item["heading_path"] == "Operations" for item in documents if item["content_kind"] == "paragraph"))

    def test_long_semantic_segments_stay_within_the_l3_limit(self):
        text = " ".join(
            f"Sentence {index} explains a distinct operational detail for the audit trail."
            for index in range(60)
        )

        def embedder(windows: list[str]) -> list[list[float]]:
            # Cluster the only obvious topic changes at the start. The planner
            # still needs additional scored sentence boundaries for size.
            return [
                [0.0, 1.0]
                if index % 2 and index // 2 < 12
                else [1.0, 0.0]
                for index in range(len(windows))
            ]

        plan = build_semantic_boundary_plan(text, document_id="limits.md", embedder=embedder)
        offsets = [0, *(item["offset"] for item in plan["selected_boundaries"]), len(text)]
        self.assertTrue(all(end - start <= 800 for start, end in zip(offsets, offsets[1:])))
        self.assertGreater(plan["semantic_size_guard_boundary_count"], 0)

    def test_sentence_longer_than_l3_uses_explicit_fallback_without_overlap(self):
        oversized_sentence = "X" * 810 + "."
        paragraph = f"{oversized_sentence} A short complete sentence follows. " * 3
        plan = build_semantic_boundary_plan(
            paragraph,
            document_id="oversized.md",
            embedder=lambda texts: [[1.0, 0.0] for _ in texts],
        )
        self.assertEqual(plan["decision"], "semantic_not_applicable")
        self.assertEqual(plan["fallback_reason"], "sentence_exceeds_l3_limit")
        markdown = f"# Operations\n\n{paragraph}\n"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.md"
            path.write_text(markdown, encoding="utf-8")
            documents = DocumentLoader(
                chunking_strategy=SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
                semantic_boundary_plan=[{
                    **plan,
                    "source_start": markdown.index("X"),
                    "source_end": markdown.index("X") + len(paragraph),
                }],
            ).load_document(str(path), path.name)
        leaves = [item for item in documents if item["chunk_level"] == 3]
        ranges = [(item["source_start_index"], item["source_end_index"]) for item in leaves]
        self.assertEqual(len(ranges), len(set(ranges)))

    def test_semantic_strategy_without_plan_falls_back_without_embedding(self):
        paragraph = "A long operational sentence with a stable fact. " * 30
        markdown = f"# Operations\n\n{paragraph}\n"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "operations.md"
            path.write_text(markdown, encoding="utf-8")
            documents = DocumentLoader(
                chunking_strategy=SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
            ).load_document(str(path), path.name)
        self.assertTrue(documents)
        self.assertTrue(all(item["chunking_strategy"] == SEMANTIC_MARKDOWN_CHUNKING_STRATEGY for item in documents))
