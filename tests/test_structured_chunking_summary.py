from __future__ import annotations

from unittest import TestCase

from scripts.summarize_structured_chunking import _has_system_error, _seconds, _usable_case_ids


class StructuredChunkingSummaryTests(TestCase):
    def test_seconds_formats_missing_and_numeric_values(self):
        self.assertEqual(_seconds(None), "N/A")
        self.assertEqual(_seconds(14.5119), "14.51")

    def test_comparison_excludes_system_errors_on_either_side(self):
        baseline = {
            "ok": {"answer_grade": {"verdict": "fail"}},
            "baseline_timeout": {"evaluation_error": "timeout"},
            "target_timeout": {"answer_grade": {"verdict": "fail"}},
        }
        evaluated = {
            "ok": {"answer_grade": {"verdict": "pass"}},
            "baseline_timeout": {"answer_grade": {"verdict": "fail"}},
            "target_timeout": {"evaluation_error": "timeout"},
        }
        paired, recovered = _usable_case_ids(
            ["ok", "baseline_timeout", "target_timeout"], baseline, evaluated
        )
        self.assertEqual(paired, ["ok"])
        self.assertEqual(recovered, ["baseline_timeout"])
        self.assertFalse(_has_system_error(baseline["ok"]))
