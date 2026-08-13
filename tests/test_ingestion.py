import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from backend.indexing.ingestion import DocumentIngestionService
from backend.indexing.mineru_client import MineruArtifactBundle


class DocumentIngestionServiceTests(unittest.TestCase):
    def test_rich_document_is_converted_then_loaded_from_markdown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "guide.pdf"
            source.write_bytes(b"pdf")
            markdown = root / "parsed" / "document.md"
            markdown.parent.mkdir()
            markdown.write_text("# Guide\n\nContent", encoding="utf-8")
            bundle = MineruArtifactBundle(
                directory=markdown.parent,
                markdown_path=markdown,
                content_list_path=markdown.parent / "content_list.json",
                profile_path=markdown.parent / "profile.json",
                export_path=None,
                markdown=markdown.read_text(encoding="utf-8"),
            )
            loader = Mock()
            loader.load_parsed_markdown.return_value = [
                {"chunk_level": 1},
                {"chunk_level": 2},
                {"chunk_level": 3},
            ]
            mineru = Mock()
            mineru.convert_to_bundle.return_value = bundle

            prepared = DocumentIngestionService(loader, mineru).prepare(
                source,
                "guide.pdf",
                root / "documents" / "guide.pdf",
                root,
            )

            mineru.convert_to_bundle.assert_called_once_with(source, root / "parsed")
            loader.load_parsed_markdown.assert_called_once_with(
                str(markdown),
                "guide.pdf",
                str(root / "documents" / "guide.pdf"),
                "PDF",
            )
            loader.load_document.assert_not_called()
            self.assertEqual(len(prepared.parent_chunks), 2)
            self.assertEqual(len(prepared.leaf_chunks), 1)
            self.assertIs(prepared.artifact_bundle, bundle)

    def test_native_document_skips_mineru(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "guide.md"
            source.write_text("# Guide", encoding="utf-8")
            loader = Mock()
            loader.load_document.return_value = [{"chunk_level": 3}]
            mineru = Mock()

            prepared = DocumentIngestionService(loader, mineru).prepare(
                source,
                "guide.md",
                root / "documents" / "guide.md",
                root,
            )

            mineru.convert_to_bundle.assert_not_called()
            loader.load_document.assert_called_once_with(
                str(source),
                "guide.md",
                str(root / "documents" / "guide.md"),
            )
            self.assertEqual(prepared.parent_chunks, [])
            self.assertEqual(len(prepared.leaf_chunks), 1)
            self.assertIsNone(prepared.artifact_bundle)


if __name__ == "__main__":
    unittest.main()
