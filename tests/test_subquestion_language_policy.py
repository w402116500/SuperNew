from __future__ import annotations

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.evaluation.runner import (
    _build_multihop_runtime,
    _manifest_payload_hash,
    _subquestion_language_compliance,
    create_subquestion_language_manifest,
    evaluate_run,
)
from backend.evaluation.metrics import multihop_metrics
from backend.rag.pipeline import COMPLEXITY_PROMPT, _complexity_prompt
from backend.rag.utils import RetrievalRuntime


class SubquestionLanguagePolicyTests(unittest.TestCase):
    def test_legacy_prompt_is_unchanged_and_opt_in_adds_language_rule(self) -> None:
        question = "Compare the rollout safeguards in Europe and the US."

        self.assertEqual(
            _complexity_prompt(question, RetrievalRuntime()),
            COMPLEXITY_PROMPT.format(question=question),
        )
        self.assertIn(
            "用户问题是英文时，所有 sub_questions 必须是英文",
            _complexity_prompt(
                question,
                RetrievalRuntime(subquestion_language_policy="preserve_input_language_v1"),
            ),
        )

    def test_worker_runtime_receives_the_frozen_policy(self) -> None:
        with patch("backend.evaluation.runner._evaluation_store", return_value=object()), patch(
            "backend.evaluation.runner._evaluation_parent_store_for_runtime", return_value=object()
        ), patch("backend.evaluation.runner._judge_config", return_value=None):
            runtime, judge_config, judge_client = _build_multihop_runtime({
                "collection_name": "rag_eval_enterpriserag_test",
                "subquestion_language_policy": "preserve_input_language_v1",
            })

        self.assertEqual(runtime.subquestion_language_policy, "preserve_input_language_v1")
        self.assertIsNone(judge_config)
        self.assertIsNone(judge_client)

    def test_compliance_reads_actual_subquestions_and_candidate_queries(self) -> None:
        payload = _subquestion_language_compliance([
            {
                "case_id": "english-ok",
                "question": "Compare the rollout safeguards.",
                "rag_trace": {
                    "complexity": "complex",
                    "sub_questions": ["What safeguards apply before rollout?"],
                },
                "candidate_audits": [{"query": "What safeguards apply before rollout?"}],
                "answer_grade": {"verdict": "pass"},
            },
            {
                "case_id": "english-cjk",
                "question": "Compare the rollout safeguards.",
                "rag_trace": {"complexity": "complex", "sub_questions": ["欧洲发布有哪些保护措施？"]},
                "candidate_audits": [{"query": "欧洲发布有哪些保护措施？"}],
                "answer_grade": {"verdict": "fail"},
            },
        ])

        self.assertFalse(payload["language_policy_compliant"])
        self.assertEqual(payload["checked_case_count"], 2)
        self.assertEqual(
            {item["case_id"] for item in payload["noncompliant_observations"]},
            {"english-cjk"},
        )

        unobserved = _subquestion_language_compliance([
            {
                "case_id": "system-error",
                "question": "Compare the rollout safeguards.",
                "rag_trace": {},
                "candidate_audits": [],
                "evaluation_error": "worker failed",
            },
        ])
        self.assertIsNone(unobserved["language_policy_compliant"])
        self.assertEqual(unobserved["system_error_case_ids"], ["system-error"])

    def test_manifest_and_runner_require_the_fixed_30_case_policy_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case_ids = [f"qst_{index:04d}" for index in range(30)]
            cases = [
                {
                    "id": case_id,
                    "question": f"Compare safeguard {index}.",
                    "question_type": "basic",
                    "expected_evidence_filenames": [f"doc-{index}.md"],
                    "evidence_filenames": [f"doc-{index}.md"],
                    "case_set": "analysis",
                }
                for index, case_id in enumerate(case_ids)
            ]
            split = {"analysis_case_ids": case_ids, "validation_case_ids": []}
            split_path = root / "case-split.json"
            split_path.write_text(json.dumps(split), encoding="utf-8")
            split_hash = sha256(split_path.read_bytes()).hexdigest()
            (root / "cases.jsonl").write_text(
                "".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8"
            )
            (root / "config.json").write_text(json.dumps({
                "run_id": "corpus",
                "profile": "full",
                "collection_name": "rag_eval_enterpriserag_corpus",
                "language": "en",
                "corpus": "representative",
                "evaluation_mode": "rag",
                "document_chunking_strategy": "markdown_header_recursive_v1",
            }), encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps({
                "collection_name": "rag_eval_enterpriserag_corpus",
                "prepare_completed": True,
                "cleanup_completed": False,
                "case_split_sha256": split_hash,
            }), encoding="utf-8")
            source_results = root / "source-results.jsonl"
            source_results.write_text(
                "".join(json.dumps({"case_id": case_id, "case_set": "analysis"}) + "\n" for case_id in case_ids),
                encoding="utf-8",
            )
            source_target = root / "source-target.json"
            source_target_payload = {
                "manifest_type": "model_comparison",
                "case_set": "analysis",
                "case_count": 30,
                "case_ids": case_ids,
            }
            source_target_payload["manifest_sha256"] = _manifest_payload_hash(source_target_payload)
            source_target.write_text(json.dumps(source_target_payload), encoding="utf-8")
            target = root / "subquestion-language-target.json"
            create_subquestion_language_manifest(
                source_target_manifest_path=source_target,
                source_results_path=source_results,
                case_split_path=split_path,
                output_path=target,
                source_evaluation_id="structured-chunking-analysis-30-retry-005",
            )

            with patch("backend.evaluation.runner.run_directory", return_value=root):
                with self.assertRaisesRegex(ValueError, "evaluation_worker_count=10"):
                    evaluate_run(
                        dataset="enterpriserag",
                        run_id="corpus",
                        evaluation_id="language-low-workers",
                        case_set="analysis",
                        evaluation_mode="rag",
                        changed_variable="subquestion_language_policy",
                        target_manifest_path=target,
                        capture_candidate_trace=True,
                        evaluation_worker_count=1,
                        subquestion_language_policy="preserve_input_language_v1",
                    )

            def fake_rag(_output_dir, _config, selected_cases, **_kwargs):
                records = [
                    {
                        "case_id": case["id"],
                        "question": case["question"],
                        "question_type": case["question_type"],
                        "reference_answer": "A",
                        "answer": "A",
                        "expected_evidence_filenames": case["expected_evidence_filenames"],
                        "retrieved_filenames": case["expected_evidence_filenames"],
                        "evidence_ranks": {case["expected_evidence_filenames"][0]: 1},
                        "evidence_coverage": 1.0,
                        "answer_grade": {"verdict": "pass", "reason": "ok", "grader_error": ""},
                        "evaluation_error": "",
                        "answer_generation_error": "",
                        "rag_trace": {
                            "complexity": "complex",
                            "sub_questions": ["What safeguard applies before rollout?"],
                        },
                        "candidate_audits": [{"query": "What safeguard applies before rollout?"}],
                        "end_to_end_seconds": 0.1,
                    }
                    for case in selected_cases
                ]
                return records, multihop_metrics(records)

            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                evaluation_dir = evaluate_run(
                    dataset="enterpriserag",
                    run_id="corpus",
                    evaluation_id="language-snapshot",
                    case_set="analysis",
                    evaluation_mode="rag",
                    changed_variable="subquestion_language_policy",
                    target_manifest_path=target,
                    capture_candidate_trace=True,
                    evaluation_worker_count=10,
                    subquestion_language_policy="preserve_input_language_v1",
                )

            config_path = evaluation_dir / "evaluation-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["subquestion_language_policy"], "preserve_input_language_v1")
            compliance = json.loads(
                (evaluation_dir / "subquestion-language-compliance.json").read_text(encoding="utf-8")
            )
            self.assertTrue(compliance["language_policy_compliant"])
            self.assertTrue((evaluation_dir / "candidate-audit.md").is_file())

            config["subquestion_language_policy"] = "legacy"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ), self.assertRaisesRegex(FileExistsError, "subquestion_language_policy"):
                evaluate_run(
                    dataset="enterpriserag",
                    run_id="corpus",
                    evaluation_id="language-snapshot",
                    case_set="analysis",
                    evaluation_mode="rag",
                    changed_variable="subquestion_language_policy",
                    target_manifest_path=target,
                    capture_candidate_trace=True,
                    evaluation_worker_count=10,
                    subquestion_language_policy="preserve_input_language_v1",
                )


if __name__ == "__main__":
    unittest.main()
