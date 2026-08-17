"""Contract tests for the opt-in T9 rewrite candidate fusion path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.rag.utils import (
    RetrievalRuntime,
    fuse_rewrite_candidate_results,
    retrieve_documents,
)
from backend.evaluation.runner import _evaluate_multihop_case, create_rewrite_candidate_fusion_manifest


class RewriteCandidateFusionTests(unittest.TestCase):
    def test_runtime_flag_is_off_by_default(self):
        self.assertFalse(RetrievalRuntime().enable_rewrite_candidate_fusion)

    def test_fusion_deduplicates_candidates_and_reranks_with_original_question(self):
        initial = {
            "docs": [{"chunk_id": "old", "filename": "old.md"}],
            "raw_candidates": [
                {"chunk_id": "old", "filename": "old.md", "score": 0.2},
                {"chunk_id": "keep", "filename": "keep.md", "score": 0.1},
            ],
            "raw_retrieval_meta": {
                "retrieval_mode": "hybrid",
                "candidate_k": 24,
                "candidate_config": {"candidate_k_source": "multiplier"},
            },
            "meta": {"retrieval_mode": "hybrid"},
        }
        rewritten = {
            "raw_candidates": [
                {"chunk_id": "old", "filename": "old.md", "score": 0.9},
                {"chunk_id": "new", "filename": "new.md", "score": 0.8},
            ],
            "raw_retrieval_meta": {
                "retrieval_mode": "hybrid",
                "candidate_k": 24,
                "candidate_config": {"candidate_k_source": "multiplier"},
            },
        }
        with patch("backend.rag.utils._finalize_retrieval") as finalize:
            finalize.return_value = {
                "docs": [{"chunk_id": "new", "filename": "new.md", "_rewrite_candidate_sources": ["rewritten"]}],
                "meta": {},
            }
            result = fuse_rewrite_candidate_results(
                original_query="the original question",
                initial_retrieval=initial,
                rewritten_retrieval=rewritten,
                top_k=8,
                runtime=RetrievalRuntime(enable_rewrite_candidate_fusion=True),
            )
        self.assertEqual(finalize.call_args.kwargs["query"], "the original question")
        self.assertEqual(finalize.call_args.kwargs["top_k"], 8)
        self.assertEqual(result["meta"]["rewrite_candidate_fusion_fused_candidate_count"], 3)
        self.assertEqual(result["meta"]["rewrite_candidate_fusion_deduplicated_candidate_count"], 1)
        self.assertNotIn("_rewrite_candidate_sources", result["docs"][0])

    def test_failed_rewrite_keeps_initial_final_documents(self):
        initial = {
            "docs": [{"chunk_id": "initial", "filename": "initial.md"}],
            "raw_candidates": [{"chunk_id": "initial", "filename": "initial.md"}],
            "meta": {},
        }
        result = fuse_rewrite_candidate_results(
            original_query="question",
            initial_retrieval=initial,
            rewritten_retrieval=None,
            top_k=8,
            runtime=RetrievalRuntime(enable_rewrite_candidate_fusion=True),
            fallback_reason="rewrite_query_failed:TimeoutError",
        )
        self.assertEqual(result["docs"], initial["docs"])
        self.assertFalse(result["meta"]["rewrite_candidate_fusion_applied"])
        self.assertEqual(
            result["meta"]["rewrite_candidate_fusion_fallback_reason"],
            "rewrite_query_failed:TimeoutError",
        )

    def test_failed_rewritten_retrieval_preserves_error_trace(self):
        initial = {
            "docs": [{"chunk_id": "initial", "filename": "initial.md"}],
            "raw_candidates": [{"chunk_id": "initial", "filename": "initial.md"}],
            "meta": {},
        }
        rewritten = {
            "docs": [],
            "raw_candidates": [],
            "raw_retrieval_meta": {"retrieval_mode": "failed"},
            "meta": {"retrieval_error": "retrieve_failed:MilvusException"},
        }
        result = fuse_rewrite_candidate_results(
            original_query="question",
            initial_retrieval=initial,
            rewritten_retrieval=rewritten,
            top_k=8,
            runtime=RetrievalRuntime(enable_rewrite_candidate_fusion=True),
        )
        self.assertEqual(result["docs"], initial["docs"])
        self.assertEqual(result["meta"]["retrieval_error"], "retrieve_failed:MilvusException")

    def test_retrieval_infrastructure_failure_has_a_traceable_error(self):
        class FailingStore:
            def hybrid_retrieve(self, *args, **kwargs):
                raise ConnectionError("Milvus unavailable")

            def dense_retrieve(self, *args, **kwargs):
                raise ConnectionError("Milvus unavailable")

        class FakeEmbedding:
            def get_embeddings(self, texts):
                return [[0.0] for _ in texts]

        result = retrieve_documents(
            "health check",
            runtime=RetrievalRuntime(
                milvus_store=FailingStore(),
                embedding_service=FakeEmbedding(),
                enable_auto_merge=False,
                enable_rerank=False,
            ),
        )
        self.assertEqual(result["meta"]["retrieval_mode"], "failed")
        self.assertIn("ConnectionError", result["meta"]["retrieval_error"])

    def test_evaluation_marks_a_retrieval_failure_as_system_error(self):
        case = {
            "id": "analysis-case",
            "question": "question",
            "question_type": "basic",
            "reference_answer": "answer",
            "evidence_filenames": ["source.md"],
        }
        state = {
            "rag_trace": {"retrieval_error": "retrieve_failed:hybrid=MilvusException"},
            "docs": [],
            "route": "no_knowledge",
            "retrieval_status": "no_knowledge",
        }
        with patch("backend.rag.pipeline.run_rag_graph", return_value=state):
            result = _evaluate_multihop_case(
                case,
                {"run_id": "t9-test", "dataset": "enterpriserag"},
                RetrievalRuntime(),
                None,
                None,
            )
        self.assertIn("retrieve_failed", result["evaluation_error"])
        self.assertEqual(result["answer_grade"]["verdict"], "review")

    def test_manifest_freezes_analysis_rewrite_cases_without_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.jsonl"
            split_path = root / "case-split.json"
            output_path = root / "target.json"
            records = [
                {
                    "case_id": "a1",
                    "case_set": "analysis",
                    "evidence_coverage": 0.5,
                    "rag_trace": {"rewrite_method": "step_back"},
                },
                {
                    "case_id": "v1",
                    "case_set": "validation",
                    "evidence_coverage": 0.2,
                    "rag_trace": {"rewrite_method": "hyde"},
                },
                {
                    "case_id": "a2",
                    "case_set": "analysis",
                    "evidence_coverage": 0.75,
                    "rag_trace": {"rewrite_method": "hyde"},
                },
            ]
            results_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            split_path.write_text(json.dumps({
                "analysis_case_ids": ["a1", "a2"],
                "validation_case_ids": ["v1"],
            }), encoding="utf-8")
            create_rewrite_candidate_fusion_manifest(
                source_results_path=results_path,
                case_split_path=split_path,
                output_path=output_path,
                source_evaluation_id="baseline-rag-test",
                expected_count=2,
            )
            manifest = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["case_ids"], ["a1", "a2"])
            self.assertEqual(manifest["case_set"], "analysis")
            self.assertNotIn("v1", manifest["case_ids"])

    def test_manifest_rejects_duplicate_source_case_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.jsonl"
            split_path = root / "case-split.json"
            output_path = root / "target.json"
            record = {
                "case_id": "a1",
                "case_set": "analysis",
                "evidence_coverage": 0.5,
                "rag_trace": {"rewrite_method": "step_back"},
            }
            results_path.write_text(
                "\n".join(json.dumps(record) for _ in range(2)) + "\n",
                encoding="utf-8",
            )
            split_path.write_text(
                json.dumps({"analysis_case_ids": ["a1"], "validation_case_ids": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "重复 case ID"):
                create_rewrite_candidate_fusion_manifest(
                    source_results_path=results_path,
                    case_split_path=split_path,
                    output_path=output_path,
                    source_evaluation_id="baseline-rag-test",
                    expected_count=1,
                )


if __name__ == "__main__":
    unittest.main()
