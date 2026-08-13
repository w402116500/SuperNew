import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.api.routes.documents import _process_upload_job
from backend.jobs.upload_jobs import upload_job_manager


class StagedUploadTests(unittest.TestCase):
    def test_parse_failure_keeps_existing_document_untouched(self):
        job = upload_job_manager.create_job("guide.pdf")
        with TemporaryDirectory() as staging_dir:
            with patch(
                "backend.api.routes.documents.ingestion_service.prepare",
                side_effect=RuntimeError("MinerU conversion failed"),
            ):
                with patch("backend.api.routes.documents.delete_document_transactionally") as delete_old:
                    _process_upload_job(job["job_id"], staging_dir, "guide.pdf")

        snapshot = upload_job_manager.get_job(job["job_id"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["current_step"], "mineru")
        self.assertIn("MinerU conversion failed", snapshot["error"])
        delete_old.assert_not_called()


if __name__ == "__main__":
    unittest.main()
