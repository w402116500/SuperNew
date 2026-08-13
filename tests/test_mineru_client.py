import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from backend.indexing.mineru_client import MineruClient, MineruSettings


class MineruClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = MineruSettings(
            base_url="http://127.0.0.1:7860",
            end_pages=1000,
            is_ocr=False,
            formula_enable=True,
            table_enable=True,
            image_analysis=True,
            effort="medium",
            language="ch (Chinese, English, Japanese, Chinese Traditional, Latin)",
            backend="hybrid-engine",
            engine_url="http://localhost:30000",
        )

    def test_persists_markdown_content_list_profile_and_export_bundle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "guide.pdf"
            source.write_bytes(b"placeholder")
            export = root / "result.zip"
            with ZipFile(export, "w") as archive:
                archive.writestr("guide/auto/guide.md", "# Exported")
                archive.writestr("guide/images/page-1.png", b"image")

            client = Mock()
            client.predict.return_value = (
                "status",
                str(export),
                "rendered markdown",
                "# Parsed Guide\n\nUseful text",
                '[{"type":"text","text":"Useful text"}]',
                "preview.pdf",
            )
            with patch("backend.indexing.mineru_client.Client", return_value=client):
                bundle = MineruClient(self.settings).convert_to_bundle(source, root / "bundle")

            self.assertEqual(bundle.markdown_path.read_text(encoding="utf-8"), "# Parsed Guide\n\nUseful text\n")
            self.assertEqual(json.loads(bundle.content_list_path.read_text(encoding="utf-8"))[0]["type"], "text")
            self.assertEqual(json.loads(bundle.profile_path.read_text(encoding="utf-8"))["backend"], "hybrid-engine")
            self.assertTrue((bundle.directory / "mineru-export" / "guide" / "images" / "page-1.png").is_file())
            self.assertEqual(client.predict.call_args.kwargs["api_name"], "/convert_to_markdown_stream")

    def test_rejects_empty_markdown(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "guide.pdf"
            source.write_bytes(b"placeholder")
            client = Mock()
            client.predict.return_value = ("status", None, "", "   ", "[]", None)
            with patch("backend.indexing.mineru_client.Client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "empty Markdown"):
                    MineruClient(self.settings).convert_to_bundle(source, Path(directory) / "bundle")

    def test_rejects_export_archives_with_unsafe_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "guide.pdf"
            source.write_bytes(b"placeholder")
            export = root / "unsafe.zip"
            with ZipFile(export, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")

            client = Mock()
            client.predict.return_value = ("status", str(export), "", "# Parsed", "[]", None)
            with patch("backend.indexing.mineru_client.Client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                    MineruClient(self.settings).convert_to_bundle(source, root / "bundle")


if __name__ == "__main__":
    unittest.main()
