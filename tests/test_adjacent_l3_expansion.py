"""Contract tests for the isolated T10 adjacent-L3 expansion path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.evaluation.runner import (
    create_adjacent_l3_expansion_manifest,
    create_adjacent_l3_non_pass_manifest,
)
from backend.rag.pipeline import _route_after_complexity, _route_after_grade
from backend.rag.utils import (
    RetrievalRuntime,
    _expand_adjacent_l3_candidates,
    retrieve_documents,
)


def _leaf(name: str, index: int, *, score: float | None = None) -> dict:
    item = {
        "chunk_id": f"{name}::p0::l3::{index}",
        "filename": name,
        "text": f"{name} L3 {index}",
        "chunk_level": 3,
        "chunk_idx": index,
        "parent_chunk_id": f"{name}::p0::l2::0",
    }
    if score is not None:
        item["score"] = score
    return item


class _FakeEmbedding:
    def get_embeddings(self, texts):
        return [[0.0] for _ in texts]


class _NeighborStore:
    def __init__(self, raw: list[dict], rows: list[dict], *, fail_lookup: bool = False):
        self.raw = raw
        self.rows = {row["chunk_id"]: row for row in rows}
        self.fail_lookup = fail_lookup
        self.requested_ids: list[str] = []

    def hybrid_retrieve(self, **kwargs):
        return [dict(item) for item in self.raw]

    def get_chunks_by_ids(self, chunk_ids):
        self.requested_ids = list(chunk_ids)
        if self.fail_lookup:
            raise RuntimeError("lookup unavailable")
        return [dict(self.rows[chunk_id]) for chunk_id in chunk_ids if chunk_id in self.rows]


class AdjacentL3ExpansionTests(unittest.TestCase):
    def test_runtime_flag_is_off_by_default(self):
        self.assertFalse(RetrievalRuntime().enable_adjacent_l3_expansion)

    def test_expansion_reads_only_immediate_ids_and_keeps_original_first(self):
        raw = [_leaf("a.md", 1, score=0.9)]
        store = _NeighborStore(raw, [_leaf("a.md", 0), _leaf("a.md", 2), _leaf("a.md", 3)])
        expanded, meta, audit_neighbors = _expand_adjacent_l3_candidates(raw, milvus_store=store)

        self.assertEqual(store.requested_ids, ["a.md::p0::l3::0", "a.md::p0::l3::2"])
        self.assertEqual([item["chunk_id"] for item in expanded], [
            "a.md::p0::l3::1", "a.md::p0::l3::0", "a.md::p0::l3::2",
        ])
        self.assertTrue(meta["adjacent_l3_expansion_applied"])
        self.assertEqual(meta["adjacent_l3_expansion_added_candidate_count"], 2)
        self.assertEqual(audit_neighbors[1]["_adjacent_l3_origins"], [{
            "adjacent_to_chunk_id": "a.md::p0::l3::1", "relative_position": "next",
        }])

    def test_first_leaf_has_no_negative_index_and_other_file_is_never_requested(self):
        raw = [_leaf("a.md", 0, score=0.9)]
        store = _NeighborStore(raw, [_leaf("a.md", 1), _leaf("other.md", 1)])
        _expand_adjacent_l3_candidates(raw, milvus_store=store)

        self.assertEqual(store.requested_ids, ["a.md::p0::l3::1"])
        self.assertNotIn("other.md::p0::l3::1", store.requested_ids)

    def test_duplicate_neighbor_is_read_once_and_retains_all_lineage(self):
        raw = [_leaf("a.md", 1, score=0.9), _leaf("a.md", 3, score=0.8)]
        store = _NeighborStore(raw, [_leaf("a.md", 0), _leaf("a.md", 2), _leaf("a.md", 4)])
        expanded, meta, audit_neighbors = _expand_adjacent_l3_candidates(raw, milvus_store=store)

        self.assertEqual(store.requested_ids, [
            "a.md::p0::l3::0", "a.md::p0::l3::2", "a.md::p0::l3::4",
        ])
        self.assertEqual(len(expanded), 5)
        middle = next(item for item in audit_neighbors if item["chunk_id"].endswith("::2"))
        self.assertEqual(len(middle["_adjacent_l3_origins"]), 2)
        self.assertTrue(middle["_adjacent_l3_added"])
        self.assertEqual(meta["adjacent_l3_expansion_deduplicated_candidate_count"], 0)

    def test_lookup_failure_preserves_original_candidates(self):
        raw = [_leaf("a.md", 1, score=0.9)]
        store = _NeighborStore(raw, [], fail_lookup=True)
        expanded, meta, audit_neighbors = _expand_adjacent_l3_candidates(raw, milvus_store=store)

        self.assertEqual(expanded, raw)
        self.assertEqual(audit_neighbors, [])
        self.assertFalse(meta["adjacent_l3_expansion_applied"])
        self.assertEqual(meta["adjacent_l3_expansion_fallback_reason"], "adjacent_lookup_failed:RuntimeError")

    def test_unparseable_candidate_is_skipped_with_diagnostic_metadata(self):
        raw = [_leaf("a.md", 1, score=0.9), {"chunk_id": "legacy-id", "chunk_level": 3}]
        store = _NeighborStore(raw, [_leaf("a.md", 0), _leaf("a.md", 2)])
        expanded, meta, _ = _expand_adjacent_l3_candidates(raw, milvus_store=store)

        self.assertEqual(len(expanded), 4)
        self.assertEqual(meta["adjacent_l3_expansion_skipped_candidate_count"], 1)
        self.assertEqual(meta["adjacent_l3_expansion_fallback_reason"], "unparseable_l3_candidates_skipped")
        self.assertEqual(
            meta["adjacent_l3_expansion_skipped_candidate_reasons"],
            [{"candidate_index": 2, "reason": "unparseable_l3_chunk_id"}],
        )

    def test_default_path_never_uses_scalar_neighbor_lookup(self):
        raw = [_leaf("a.md", 1, score=0.9)]
        store = _NeighborStore(raw, [], fail_lookup=True)
        result = retrieve_documents(
            "question",
            top_k=1,
            runtime=RetrievalRuntime(
                milvus_store=store,
                embedding_service=_FakeEmbedding(),
                enable_auto_merge=False,
                enable_rerank=False,
            ),
        )

        self.assertEqual(store.requested_ids, [])
        self.assertEqual([item["chunk_id"] for item in result["docs"]], ["a.md::p0::l3::1"])
        self.assertNotIn("adjacent_l3_expansion_enabled", result["meta"])

    def test_expansion_keeps_final_top_k_and_writes_candidate_audit_lineage(self):
        raw = [_leaf("a.md", 1, score=0.9)]
        store = _NeighborStore(raw, [_leaf("a.md", 0), _leaf("a.md", 2)])
        result = retrieve_documents(
            "question",
            top_k=1,
            runtime=RetrievalRuntime(
                milvus_store=store,
                embedding_service=_FakeEmbedding(),
                enable_auto_merge=False,
                enable_rerank=False,
                enable_adjacent_l3_expansion=True,
                capture_candidate_trace=True,
            ),
        )

        self.assertEqual(len(result["docs"]), 1)
        self.assertEqual(result["meta"]["adjacent_l3_expansion_added_candidate_count"], 2)
        audit = result["candidate_audit"]
        self.assertEqual(len(audit["original_raw_leaf_candidates"]), 1)
        self.assertEqual(len(audit["adjacent_l3_candidates"]), 2)
        self.assertEqual(len(audit["post_adjacent_expansion_candidates"]), 3)
        self.assertEqual(
            audit["adjacent_l3_candidates"][0]["adjacent_l3_origins"][0]["relative_position"],
            "previous",
        )

    def test_t10_runtime_stops_follow_up_query_routes(self):
        runtime = RetrievalRuntime(enable_adjacent_l3_expansion=True)
        self.assertEqual(_route_after_complexity({"complexity": "complex", "retrieval_runtime": runtime}), "retrieve_initial")
        self.assertEqual(_route_after_grade({"route": "rewrite", "retrieval_runtime": runtime}), "end")

    def test_manifest_freezes_exact_manual_adjacent_analysis_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.jsonl"
            review_path = root / "manual-review.jsonl"
            split_path = root / "case-split.json"
            output_path = root / "target.json"
            case_ids = [f"a{index:02d}" for index in range(11)]
            results_path.write_text("\n".join(json.dumps({
                "case_id": case_id,
                "case_set": "analysis",
                "answer_grade": {"verdict": "fail"},
                "evidence_coverage": 0.0,
            }) for case_id in case_ids) + "\n", encoding="utf-8")
            review_path.write_text("\n".join(json.dumps({
                "case_id": case_id,
                "case_set": "analysis",
                "classification": "adjacent_leaf_gap",
                "source_ref": f"source-{case_id}",
                "retrieved_l3_indices": [0],
                "evidence_l3_indices": [1],
                "confidence": "high",
            }) for case_id in case_ids) + "\n", encoding="utf-8")
            split_path.write_text(json.dumps({
                "analysis_case_ids": case_ids,
                "validation_case_ids": ["v1"],
            }), encoding="utf-8")

            create_adjacent_l3_expansion_manifest(
                source_results_path=results_path,
                raw_candidate_fact_review_path=review_path,
                case_split_path=split_path,
                output_path=output_path,
                source_evaluation_id="baseline-rag-test",
            )

            manifest = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["manifest_type"], "adjacent_l3_expansion")
            self.assertEqual(manifest["changed_variable"], "adjacent_l3_expansion")
            self.assertEqual(manifest["case_count"], 11)
            self.assertEqual(manifest["case_ids"], case_ids)
            self.assertEqual(manifest["manual_selection"]["a00"]["evidence_l3_indices"], [1])

    def test_manifest_rejects_validation_adjacent_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.jsonl"
            review_path = root / "manual-review.jsonl"
            split_path = root / "case-split.json"
            output_path = root / "target.json"
            analysis_ids = [f"a{index:02d}" for index in range(10)]
            all_ids = analysis_ids + ["v1"]
            results_path.write_text("\n".join(json.dumps({
                "case_id": case_id,
                "case_set": "analysis" if case_id != "v1" else "validation",
            }) for case_id in all_ids) + "\n", encoding="utf-8")
            review_path.write_text("\n".join(json.dumps({
                "case_id": case_id,
                "case_set": "analysis" if case_id != "v1" else "validation",
                "classification": "adjacent_leaf_gap",
            }) for case_id in all_ids) + "\n", encoding="utf-8")
            split_path.write_text(json.dumps({
                "analysis_case_ids": analysis_ids,
                "validation_case_ids": ["v1"],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "validation"):
                create_adjacent_l3_expansion_manifest(
                    source_results_path=results_path,
                    raw_candidate_fact_review_path=review_path,
                    case_split_path=split_path,
                    output_path=output_path,
                    source_evaluation_id="baseline-rag-test",
                )

    def test_non_pass_manifest_selects_analysis_fail_and_review_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.jsonl"
            split_path = root / "case-split.json"
            output_path = root / "target.json"
            records = [
                {"case_id": "a-pass", "case_set": "analysis", "answer_grade": {"verdict": "pass"}},
                {"case_id": "a-fail", "case_set": "analysis", "answer_grade": {"verdict": "fail"}},
                {"case_id": "a-review", "case_set": "analysis", "answer_grade": {"verdict": "review"}},
                {"case_id": "v-fail", "case_set": "validation", "answer_grade": {"verdict": "fail"}},
            ]
            results_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            split_path.write_text(json.dumps({
                "analysis_case_ids": ["a-pass", "a-fail", "a-review"],
                "validation_case_ids": ["v-fail"],
            }), encoding="utf-8")

            create_adjacent_l3_non_pass_manifest(
                source_results_path=results_path,
                case_split_path=split_path,
                output_path=output_path,
                source_evaluation_id="baseline-rag-test",
                expected_count=2,
            )

            manifest = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["selection_mode"], "non_pass_analysis")
            self.assertEqual(manifest["case_ids"], ["a-fail", "a-review"])
            self.assertEqual(manifest["case_count"], 2)
            self.assertEqual(manifest["excluded_pass_count"], 1)
            self.assertEqual(manifest["baseline_verdict_counts"], {"pass": 1, "fail": 1, "review": 1})


if __name__ == "__main__":
    unittest.main()
