import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.api.resources import promote_staged_document, remove_document_files


class DocumentResourceTests(unittest.TestCase):
    def test_promotes_and_removes_source_and_mineru_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "guide.pdf").write_bytes(b"new source")
            parsed = staging / "parsed"
            parsed.mkdir()
            (parsed / "document.md").write_text("# Guide", encoding="utf-8")
            (parsed / "content_list.json").write_text("[]", encoding="utf-8")

            with patch("backend.api.resources.UPLOAD_DIR", root / "documents"), patch(
                "backend.api.resources.PARSED_ARTIFACT_DIR", root / "artifacts"
            ):
                source_path = promote_staged_document(staging, "guide.pdf", has_artifact_bundle=True)

                self.assertEqual(source_path.read_bytes(), b"new source")
                artifact_dir = root / "artifacts" / "guide.pdf.mineru"
                self.assertEqual((artifact_dir / "document.md").read_text(encoding="utf-8"), "# Guide")
                self.assertFalse((staging / "guide.pdf").exists())
                self.assertFalse(parsed.exists())

                remove_document_files("guide.pdf")
                self.assertFalse(source_path.exists())
                self.assertFalse(artifact_dir.exists())


if __name__ == "__main__":
    unittest.main()
