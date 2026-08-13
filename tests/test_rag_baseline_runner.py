"""Unit tests for evaluation result normalization without invoking the live API."""

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_rag_baseline.py"
SPEC = importlib.util.spec_from_file_location("run_rag_baseline", SCRIPT_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class RagBaselineRunnerTests(unittest.TestCase):
    def test_expected_doc_names_uses_uploaded_filename(self):
        case = {"expected_docs": ["knowledge/products/vivo-y200.md"]}
        self.assertEqual(RUNNER.expected_doc_names(case), ["vivo-y200.md"])

    def test_source_filenames_deduplicates_in_trace_order(self):
        trace = {
            "retrieved_chunks": [
                {"filename": "vivo-y200.md"},
                {"filename": "vivo-s19.md"},
                {"filename": "vivo-y200.md"},
            ]
        }
        self.assertEqual(RUNNER.source_filenames(trace), ["vivo-y200.md", "vivo-s19.md"])

    def test_expected_doc_ranks_are_one_based(self):
        case = {"expected_docs": ["knowledge/products/vivo-s19.md", "knowledge/products/vivo-x200.md"]}
        trace = {"retrieved_chunks": [{"filename": "vivo-y200.md"}, {"filename": "vivo-s19.md"}]}
        self.assertEqual(
            RUNNER.expected_doc_ranks(case, trace),
            {"vivo-s19.md": 2, "vivo-x200.md": None},
        )

    def test_answer_grade_rejects_pass_when_fact_result_is_missing(self):
        case = {
            "expected_facts": ["Y200 的价格为 1799 元", "这不是实时成交价"],
            "must_not_claim": ["1799 元是当前成交价"],
        }
        grade = RUNNER.normalize_answer_grade(
            {
                "verdict": "pass",
                "fact_results": [
                    {"index": 1, "status": "met", "reason": "价格正确"},
                    {"index": 2, "status": "missing", "reason": "遗漏价格边界"},
                ],
                "forbidden_claim_results": [{"index": 1, "mentioned": False, "reason": "未出现"}],
                "reason": "模型初步判定通过",
            },
            case,
        )
        self.assertEqual(grade["verdict"], "fail")
        self.assertEqual(grade["fact_results"][1]["status"], "missing")

    def test_summary_includes_answer_and_forbidden_claim_metrics(self):
        records = [
            {
                "http_status": 200,
                "all_expected_docs_recalled": True,
                "expected_doc_ranks": {"vivo-y200.md": 1},
                "elapsed_seconds": 2.0,
                "answer_grade": {
                    "verdict": "pass",
                    "fact_results": [{"status": "met"}],
                    "forbidden_claim_results": [{"mentioned": False}],
                },
            },
            {
                "http_status": 200,
                "all_expected_docs_recalled": False,
                "expected_doc_ranks": {"vivo-s19.md": None},
                "elapsed_seconds": 4.0,
                "answer_grade": {
                    "verdict": "fail",
                    "fact_results": [{"status": "missing"}],
                    "forbidden_claim_results": [{"mentioned": True}],
                },
            },
        ]
        summary = RUNNER.build_summary(records, Path("output.jsonl"))
        self.assertEqual(summary["answer_judged_cases"], 2)
        self.assertEqual(summary["answer_passed_cases"], 1)
        self.assertEqual(summary["expected_facts_met"], 1)
        self.assertEqual(summary["forbidden_claim_violations"], 1)


if __name__ == "__main__":
    unittest.main()
