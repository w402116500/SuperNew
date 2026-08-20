"""Contracts specific to the visible 500-question semantic chunking comparison."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.evaluation.runner import (
    DEVELOPMENT_ALL_500_CASE_SET,
    _select_experiment_cases,
    evaluate_run,
)
from backend.indexing.document_loader import SEMANTIC_MARKDOWN_CHUNKING_STRATEGY


class SemanticEvaluationContractTests(TestCase):
    def _cases(self) -> list[dict]:
        return [
            {
                "id": f"case-{index:03d}",
                "case_set": "analysis" if index < 300 else "validation",
                "question": f"Question {index}",
                "question_type": "basic",
                "reference_answer": "Answer",
                "evidence_filenames": [f"doc-{index}.md"],
                "expected_evidence_filenames": [f"doc-{index}.md"],
            }
            for index in range(500)
        ]

    def _corpus_files(self, root: Path) -> list[dict]:
        cases = self._cases()
        root.mkdir(parents=True, exist_ok=True)
        (root / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8"
        )
        split = {
            "analysis_case_ids": [case["id"] for case in cases[:300]],
            "validation_case_ids": [case["id"] for case in cases[300:]],
        }
        split_path = root / "case-split.json"
        split_path.write_text(json.dumps(split), encoding="utf-8")
        split_hash = sha256(split_path.read_bytes()).hexdigest()
        (root / "config.json").write_text(json.dumps({
            "profile": "full",
            "language": "en",
            "corpus": "representative",
            "evaluation_mode": "rag",
            "document_chunking_strategy": SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
            "rechunk_scope": "full",
            "ordinary_document_count": 300,
        }), encoding="utf-8")
        (root / "manifest.json").write_text(json.dumps({
            "collection_name": "rag_eval_enterpriserag_semantic_test",
            "prepare_completed": True,
            "cleanup_completed": False,
            "case_split_sha256": split_hash,
            "required_document_count": 722,
            "ordinary_document_count": 300,
            "hard_document_count": 0,
        }), encoding="utf-8")
        return cases

    def test_visible_development_set_is_frozen_and_keeps_formal_origin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cases = self._corpus_files(root)
            selected, split_hash = _select_experiment_cases(
                root, cases, DEVELOPMENT_ALL_500_CASE_SET
            )

        self.assertEqual(len(selected), 500)
        self.assertTrue(split_hash)
        self.assertEqual({case["case_set"] for case in selected}, {DEVELOPMENT_ALL_500_CASE_SET})
        self.assertEqual(selected[0]["formal_case_set"], "analysis")
        self.assertEqual(selected[-1]["formal_case_set"], "validation")

    def test_development_run_records_300_distractors_and_candidate_trace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            self._corpus_files(root)
            captured_case_sets: list[str] = []

            def fake_rag(_output: Path, _config: dict, cases: list[dict], **_kwargs):
                captured_case_sets.extend(case["case_set"] for case in cases)
                records = [
                    {
                        "case_id": case["id"],
                        "question": case["question"],
                        "question_type": case["question_type"],
                        "reference_answer": case["reference_answer"],
                        "answer": "Answer",
                        "expected_evidence_filenames": case["evidence_filenames"],
                        "retrieved_filenames": case["evidence_filenames"],
                        "evidence_coverage": 1.0,
                        "answer_grade": {"verdict": "pass"},
                        "rag_trace": {"raw_candidates": []},
                        "end_to_end_seconds": 0.01,
                    }
                    for case in cases
                ]
                return records, {"dataset": "enterpriserag", "run_id": _config["run_id"]}

            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                output = evaluate_run(
                    dataset="enterpriserag",
                    run_id="corpus",
                    evaluation_id="semantic-development-500",
                    case_set=DEVELOPMENT_ALL_500_CASE_SET,
                    evaluation_mode="rag",
                    changed_variable="document_chunking_strategy",
                    capture_candidate_trace=True,
                    evaluation_worker_count=5,
                )
            config = json.loads((output / "evaluation-config.json").read_text(encoding="utf-8"))
            traces = [
                json.loads(line)
                for line in (output / "candidate-trace.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(captured_case_sets, [DEVELOPMENT_ALL_500_CASE_SET] * 500)
        self.assertEqual(config["ordinary_document_count"], 300)
        self.assertEqual(config["case_set"], DEVELOPMENT_ALL_500_CASE_SET)
        self.assertEqual(len(traces), 500)
        self.assertTrue(all(trace["case_set"] == DEVELOPMENT_ALL_500_CASE_SET for trace in traces))

    def test_development_run_requires_candidate_trace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            self._corpus_files(root)
            with patch("backend.evaluation.runner.run_directory", return_value=root):
                with self.assertRaisesRegex(ValueError, "candidate trace"):
                    evaluate_run(
                        dataset="enterpriserag",
                        run_id="corpus",
                        evaluation_id="semantic-development-500",
                        case_set=DEVELOPMENT_ALL_500_CASE_SET,
                        evaluation_mode="rag",
                        changed_variable="document_chunking_strategy",
                    )

    def test_recursive_analysis_300_control_requires_isolation_and_keeps_trace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            self._corpus_files(root)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["document_chunking_strategy"] = "recursive_l1_l2_l3"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evaluation_storage_mode"] = "isolated_postgresql"
            manifest["evaluation_database_name"] = "enterprise_rag_evaluation"
            manifest["evaluation_redis_prefix"] = "rag_eval_chunking:recursive-test"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            captured_case_sets: list[str] = []

            def fake_rag(_output: Path, _config: dict, cases: list[dict], **_kwargs):
                captured_case_sets.extend(case["case_set"] for case in cases)
                records = [
                    {
                        "case_id": case["id"],
                        "question": case["question"],
                        "question_type": case["question_type"],
                        "reference_answer": case["reference_answer"],
                        "answer": "Answer",
                        "expected_evidence_filenames": case["evidence_filenames"],
                        "retrieved_filenames": case["evidence_filenames"],
                        "evidence_coverage": 1.0,
                        "answer_grade": {"verdict": "pass"},
                        "rag_trace": {"raw_candidates": []},
                        "end_to_end_seconds": 0.01,
                    }
                    for case in cases
                ]
                return records, {"dataset": "enterpriserag", "run_id": _config["run_id"]}

            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                output = evaluate_run(
                    dataset="enterpriserag",
                    run_id="corpus",
                    evaluation_id="recursive-analysis-300",
                    case_set="analysis",
                    evaluation_mode="rag",
                    changed_variable="document_chunking_strategy",
                    capture_candidate_trace=True,
                    evaluation_worker_count=10,
                )

            evaluation_config = json.loads(
                (output / "evaluation-config.json").read_text(encoding="utf-8")
            )
            traces = (output / "candidate-trace.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(captured_case_sets, ["analysis"] * 300)
        self.assertEqual(evaluation_config["evaluation_storage_mode"], "isolated_postgresql")
        self.assertEqual(evaluation_config["document_chunking_strategy"], "recursive_l1_l2_l3")
        self.assertEqual(evaluation_config["evaluation_worker_count"], 10)
        self.assertEqual(len(traces), 300)
