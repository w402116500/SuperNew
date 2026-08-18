from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.evaluation.chunking_audit import (
    create_structured_chunking_target_manifest,
    run_structured_chunking_offline_audit,
)


class StructuredChunkingAuditTests(TestCase):
    def _inputs(self, root: Path, *, analysis: bool = True) -> dict[str, Path]:
        markdown_dir = root / "markdown"
        markdown_dir.mkdir()
        stable_filename = "enterprise__doc-1.md"
        markdown = "# Rollback\n\n" + ("Restore the known-good snapshot after validation. " * 45)
        markdown_path = markdown_dir / stable_filename
        markdown_path.write_text(markdown, encoding="utf-8")
        raw_review = root / "review.jsonl"
        raw_review.write_text(json.dumps({
            "case_id": "analysis-1",
            "case_set": "analysis",
            "source_ref": "doc-1",
            "classification": "same_source_far_leaf_gap",
            "missing_fact_summary": "the complete rollback sequence",
            "retrieved_l3_indices": [0],
            "evidence_l3_indices": [1],
            "reason": "details are later in the source",
            "confidence": "high",
        }) + "\n", encoding="utf-8")
        case_split = root / "case-split.json"
        case_split.write_text(json.dumps({
            "analysis_case_ids": ["analysis-1"] if analysis else [],
            "validation_case_ids": [] if analysis else ["analysis-1"],
        }), encoding="utf-8")
        corpus_documents = root / "corpus-documents.json"
        corpus_documents.write_text(json.dumps([{
            "doc_id": "doc-1",
            "stable_filename": stable_filename,
            "markdown_hash": sha256(markdown_path.read_bytes()).hexdigest(),
        }]), encoding="utf-8")
        cases = root / "cases.jsonl"
        cases.write_text(json.dumps({
            "id": "analysis-1",
            "case_set": "analysis",
            "expected_evidence_filenames": ["runtime.md"],
        }) + "\n", encoding="utf-8")
        baseline = root / "baseline.jsonl"
        baseline.write_text(json.dumps({
            "case_id": "analysis-1",
            "evidence_coverage": 0.0,
            "answer_grade": {"verdict": "fail"},
        }) + "\n", encoding="utf-8")
        return {
            "raw_review": raw_review,
            "case_split": case_split,
            "corpus_documents": corpus_documents,
            "cases": cases,
            "baseline": baseline,
            "markdown_dir": markdown_dir,
        }

    def test_manifest_freezes_analysis_only_source_hashes_and_offline_audit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            manifest_path = root / "target.json"
            create_structured_chunking_target_manifest(
                raw_review_path=inputs["raw_review"],
                case_split_path=inputs["case_split"],
                corpus_documents_path=inputs["corpus_documents"],
                cases_path=inputs["cases"],
                baseline_results_path=inputs["baseline"],
                markdown_dir=inputs["markdown_dir"],
                output_path=manifest_path,
                source_corpus_run_id="corpus-1",
                source_evaluation_id="baseline-1",
                expected_case_count=1,
            )
            summary = run_structured_chunking_offline_audit(
                target_manifest_path=manifest_path,
                markdown_dir=inputs["markdown_dir"],
                output_dir=root / "audit",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            records = [
                json.loads(line)
                for line in (root / "audit" / "chunk-audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(manifest["case_set"], "analysis")
        self.assertEqual(manifest["case_count"], 1)
        self.assertEqual(manifest["targets"][0]["case_id"], "analysis-1")
        self.assertEqual(summary["case_count"], 1)
        self.assertGreaterEqual(summary["evidence_leaf_count"], 1)
        self.assertIn("changed_boundary_evidence_leaf_count", summary)
        self.assertEqual(records[0]["case_set"], "analysis")
        self.assertEqual(records[0]["comparison_status"], "requires_human_review")

    def test_manifest_rejects_validation_case_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root, analysis=False)
            with self.assertRaisesRegex(ValueError, "validation"):
                create_structured_chunking_target_manifest(
                    raw_review_path=inputs["raw_review"],
                    case_split_path=inputs["case_split"],
                    corpus_documents_path=inputs["corpus_documents"],
                    cases_path=inputs["cases"],
                    baseline_results_path=inputs["baseline"],
                    markdown_dir=inputs["markdown_dir"],
                    output_path=root / "target.json",
                    source_corpus_run_id="corpus-1",
                    source_evaluation_id="baseline-1",
                    expected_case_count=1,
                )
