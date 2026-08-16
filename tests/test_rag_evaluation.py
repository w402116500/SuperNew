"""离线 RAG 评测工具的单元测试，所有外部服务均使用 mock。"""

from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.evaluation.datasets import (
    ENTERPRISE_ANALYSIS_QUOTAS,
    ENTERPRISE_VALIDATION_QUOTAS,
    enterprise_document_filename,
    enterprise_markdown,
    load_enterprise_canonical_markdown,
    load_enterprise_cases,
    select_enterprise_hard_ids,
    select_enterprise_ordinary_ids,
    split_enterprise_cases,
    ecom_markdown,
    load_multihop_dataset,
    multihop_markdown,
    select_smoke_cases,
    stable_filename,
)
from backend.evaluation.metrics import multihop_metrics, retrieval_metrics
from backend.evaluation.runner import (
    _append_result_checkpoint,
    _write_case_review_report,
    _collection_name,
    _evidence_coverage,
    _evaluate_multihop_case,
    _prepare_enterprise_documents,
    _multihop_error_record,
    _public_config,
    _read_retry_source_records,
    _read_multihop_checkpoints,
    _run_case_in_worker,
    _stop_multihop_worker,
    _translate_markdown_documents,
    _translation_segment_worker_count,
    _translation_worker_count,
    cleanup_run,
    evaluate_multihop,
    evaluate_enterprise_retrieval,
    evaluate_run,
    judge_multihop_answer,
    prepare_run,
)
from backend.evaluation.translation import (
    TranslationClient,
    TranslationConfig,
    _mask_protected_tokens,
    _restore_protected_tokens,
    _split_translation_segments,
    validate_translation,
)
from backend.indexing.milvus_client import MilvusSettings, MilvusStore
from backend.model_settings import evaluation_case_timeout_seconds, model_timeout_seconds
from backend.rag.utils import RetrievalRuntime, retrieve_documents


class DatasetAdapterTests(unittest.TestCase):
    def test_enterprise_markdown_contains_only_title_and_content(self):
        markdown = enterprise_markdown({
            "doc_id": "dsid_1",
            "source_type": "slack",
            "title": "Upload limits",
            "content": "10 MiB per file; /v1/uploads",
        })
        self.assertEqual(markdown, "# Upload limits\n\n10 MiB per file; /v1/uploads\n")
        self.assertNotIn("dsid_1", markdown)
        self.assertNotIn("slack", markdown)

    def test_enterprise_canonical_markdown_requires_every_requested_document(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_dir = root / "canonical"
            canonical_dir.mkdir()
            (canonical_dir / enterprise_document_filename("d1")).write_text(
                "# 中文标题\n\n中文正文\n", encoding="utf-8"
            )
            loaded = load_enterprise_canonical_markdown(
                root, {"d1"}, canonical_dir=canonical_dir
            )
            self.assertEqual(loaded["d1"], "# 中文标题\n\n中文正文\n")
            with self.assertRaisesRegex(FileNotFoundError, "d2"):
                load_enterprise_canonical_markdown(
                    root, {"d1", "d2"}, canonical_dir=canonical_dir
                )

    def test_enterprise_question_mapping_preserves_evidence_and_type(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "questions").mkdir(parents=True)
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError:  # pragma: no cover
                self.skipTest("pyarrow is not installed")
            pq.write_table(pa.Table.from_pylist([{
                "question_id": "q1",
                "question_type": "info_not_found",
                "source_types": ["slack"],
                "question": "What is missing?",
                "expected_doc_ids": ["d1"],
                "gold_answer": "Cannot determine",
                "answer_facts": [],
            }]), root / "data" / "questions" / "test.parquet")
            cases = load_enterprise_cases(root)
        self.assertEqual(cases[0]["question_type"], "info_not_found")
        self.assertEqual(cases[0]["expected_doc_ids"], ["d1"])

    def test_enterprise_ordinary_sampling_is_stable_and_stratified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "documents").mkdir(parents=True)
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError:  # pragma: no cover
                self.skipTest("pyarrow is not installed")
            rows = [
                {"doc_id": f"d{index}", "source_type": "slack" if index < 6 else "gmail", "title": "T", "content": "C"}
                for index in range(10)
            ]
            pq.write_table(pa.Table.from_pylist(rows), root / "data" / "documents" / "test.parquet")
            first = select_enterprise_ordinary_ids(root, {"d0"}, 4, seed=7)
            second = select_enterprise_ordinary_ids(root, {"d0"}, 4, seed=7)
        self.assertEqual(first, second)
        self.assertNotIn("d0", first[0])
        self.assertEqual(sum(first[2].values()), 4)

    def test_enterprise_title_bm25_excludes_required_and_deduplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "documents").mkdir(parents=True)
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError:  # pragma: no cover
                self.skipTest("pyarrow is not installed")
            rows = [
                {"doc_id": "gold", "source_type": "slack", "title": "GPU deployment guide", "content": "gold"},
                {"doc_id": "hard1", "source_type": "slack", "title": "GPU deployment rollback", "content": "hard"},
                {"doc_id": "hard2", "source_type": "slack", "title": "GPU deployment checklist", "content": "hard"},
            ]
            pq.write_table(pa.Table.from_pylist(rows), root / "data" / "documents" / "test.parquet")
            try:
                selected, by_case = select_enterprise_hard_ids(
                    root,
                    [{"id": "q1", "question": "GPU deployment", "expected_doc_ids": ["gold"]}],
                    {"gold"},
                    root / "title-index.sqlite",
                    per_case=2,
                )
            except sqlite3.OperationalError:  # pragma: no cover - platform SQLite may lack FTS5
                self.skipTest("SQLite FTS5 is not available")
        self.assertNotIn("gold", selected)
        self.assertEqual(len(by_case["q1"]), 2)
        self.assertEqual(len(selected), 2)

    def test_ecom_mapping_keeps_title_and_text_in_markdown(self):
        self.assertEqual(ecom_markdown({"title": "手机", "text": "支持 120Hz"}), "# 手机\n\n支持 120Hz\n")

    def test_multihop_mapping_keeps_metadata_and_body(self):
        markdown = multihop_markdown({"title": "Story", "source": "News", "body": "Body text"})
        self.assertIn("# Story", markdown)
        self.assertIn("- Source: News", markdown)
        self.assertIn("## Content", markdown)
        self.assertIn("Body text", markdown)

    def test_stable_filename_disambiguates_urls_with_same_safe_prefix(self):
        self.assertNotEqual(
            stable_filename("https://example.com/a?x=1", "same"),
            stable_filename("https://example.com/a?x=2", "same"),
        )

    def test_stable_filename_respects_prefix_budget_for_windows_paths(self):
        prefix = "__rag_eval__multihop-smoke-002__"
        filename = stable_filename(
            "https://example.com/" + "very-long-path/" * 20,
            "A very long article title " * 20,
            max_length=120 - len(prefix),
        )
        self.assertLessEqual(len(prefix + filename), 120)
        self.assertTrue(filename.endswith(".md"))

    def test_multihop_loader_extracts_urls_from_evidence_objects(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus.json").write_text("[]", encoding="utf-8")
            (root / "MultiHopRAG.json").write_text(
                json.dumps([{"query": "Q", "answer": "A", "question_type": "null_query", "evidence_list": [{"url": "https://x"}]}]),
                encoding="utf-8",
            )
            _, cases = load_multihop_dataset(root)
        self.assertEqual(cases[0]["question_type"], "null")
        self.assertEqual(cases[0]["evidence_urls"], ["https://x"])


class FailureClassificationTests(unittest.TestCase):
    def test_review_without_error_is_human_review_not_system_error(self):
        from scripts.analyze_rag_failures import _classify

        categories = _classify({
            "question_type": "semantic",
            "answer_grade": {"verdict": "review"},
        })
        self.assertIn("human_review", categories)
        self.assertNotIn("system_error", categories)

    def test_judge_error_is_system_error(self):
        from scripts.analyze_rag_failures import _classify

        categories = _classify({
            "question_type": "semantic",
            "answer_grade": {
                "verdict": "review",
                "grader_error": "read timeout",
            },
        })
        self.assertIn("system_error", categories)
        self.assertNotIn("human_review", categories)

    def test_multihop_smoke_samples_each_type(self):
        cases = [
            {"id": f"{kind}-{index}", "question_type": kind}
            for kind in ("comparison", "inference", "temporal", "null")
            for index in range(31)
        ]
        selected = select_smoke_cases(cases, "multihoprag")
        self.assertEqual(len(selected), 120)
        self.assertEqual({item["question_type"] for item in selected}, {"comparison", "inference", "temporal", "null"})

    def test_ecom_streaming_helpers_read_metadata_and_rows(self):
        from backend.evaluation.datasets import ecom_corpus_count, iter_ecom_corpus

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError:  # pragma: no cover - project dependency installs pyarrow
                self.skipTest("pyarrow is not installed")
            pq.write_table(pa.Table.from_pylist([{"_id": "d1"}, {"_id": "d2"}]), root / "corpus" / "data.parquet")
            self.assertEqual(ecom_corpus_count(root), 2)
            self.assertEqual([row["_id"] for row in iter_ecom_corpus(root, batch_size=1)], ["d1", "d2"])


class EvaluationMetricTests(unittest.TestCase):
    def test_public_config_records_runtime_timeout_budget(self):
        with patch.dict(os.environ, {
            "MODEL_TIMEOUT_SECONDS": "90",
            "EVALUATION_CASE_TIMEOUT_SECONDS": "600",
        }, clear=False):
            config = _public_config(
                "enterpriserag", "unit", "full", "rag_eval_enterpriserag_unit"
            )
        self.assertEqual(config["model_timeout_seconds"], 90.0)
        self.assertEqual(config["evaluation_case_timeout_seconds"], 600.0)

    def test_model_timeout_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {"MODEL_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(model_timeout_seconds(), 1.0)
        with patch.dict(os.environ, {"MODEL_TIMEOUT_SECONDS": "12.5"}):
            self.assertEqual(model_timeout_seconds(), 12.5)

    def test_evaluation_case_timeout_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {"EVALUATION_CASE_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(evaluation_case_timeout_seconds(), 1.0)
        with patch.dict(os.environ, {"EVALUATION_CASE_TIMEOUT_SECONDS": "123"}):
            self.assertEqual(evaluation_case_timeout_seconds(), 123.0)

    def test_retrieval_metrics_calculates_recall_mrr_and_fallback(self):
        summary = retrieval_metrics([
            {"relevant_ranks": [1], "retrieval_seconds": 1.0, "retrieval_mode": "hybrid"},
            {"relevant_ranks": [4], "retrieval_seconds": 3.0, "retrieval_mode": "dense_fallback"},
        ])
        self.assertEqual(summary["recall_at_1"], 0.5)
        self.assertEqual(summary["recall_at_3"], 0.5)
        self.assertEqual(summary["recall_at_5"], 1.0)
        self.assertEqual(summary["mrr_at_10"], 0.625)
        self.assertEqual(summary["dense_fallback_cases"], 1)

    def test_multihop_metrics_tracks_evidence_answer_and_process(self):
        summary = multihop_metrics([
            {
                "question_type": "inference",
                "evidence_coverage": 1.0,
                "answer_grade": {"verdict": "pass"},
                "rag_trace": {"rewrite_method": "hyde", "sub_agent_count": 2, "auto_merge_applied": True},
                "end_to_end_seconds": 3.0,
                "rag_seconds": 2.0,
                "generation_seconds": 1.0,
            },
            {
                "question_type": "null",
                "evidence_coverage": 0.0,
                "answer_grade": {"verdict": "review"},
                "null_refusal_correct": False,
                "rag_trace": {},
                "end_to_end_seconds": 4.0,
                "rag_seconds": 3.0,
                "generation_seconds": 1.0,
            },
        ])
        self.assertEqual(summary["evidence_full_coverage_rate"], 1.0)
        self.assertEqual(summary["answer_pass_rate"], 0.5)
        self.assertEqual(summary["null_refusal_correct_rate"], 0.0)
        self.assertEqual(summary["manual_review_rate"], 0.5)
        self.assertEqual(summary["rewrite_trigger_rate"], 0.5)

    def test_multihop_metrics_counts_enterprise_info_not_found_as_refusal(self):
        summary = multihop_metrics([
            {
                "question_type": "info_not_found",
                "evidence_coverage": 0.0,
                "answer_grade": {"verdict": "pass"},
                "null_refusal_correct": True,
                "rag_trace": {},
            },
            {
                "question_type": "basic",
                "evidence_coverage": 1.0,
                "answer_grade": {"verdict": "pass"},
                "rag_trace": {},
            },
        ])
        self.assertEqual(summary["evidence_full_coverage_rate"], 1.0)
        self.assertEqual(summary["null_refusal_correct_rate"], 1.0)

    def test_answerable_case_without_expected_evidence_is_not_full_coverage(self):
        self.assertEqual(
            _evidence_coverage(
                {"question_type": "high_level", "evidence_filenames": []},
                [],
            ),
            0.0,
        )
        self.assertEqual(
            _evidence_coverage(
                {"question_type": "info_not_found", "evidence_filenames": []},
                [],
            ),
            1.0,
        )


class RuntimeInjectionTests(unittest.TestCase):
    def test_bm25_runtime_calls_sparse_retrieval_without_embedding(self):
        store = Mock()
        store.bm25_retrieve.return_value = []
        embedding = Mock()
        result = retrieve_documents(
            "关键词",
            top_k=2,
            runtime=RetrievalRuntime(
                milvus_store=store,
                embedding_service=embedding,
                retrieval_mode="bm25",
                enable_auto_merge=False,
                enable_rerank=False,
            ),
        )
        store.bm25_retrieve.assert_called_once()
        self.assertEqual(store.bm25_retrieve.call_args.kwargs["query"], "关键词")
        self.assertEqual(store.bm25_retrieve.call_args.kwargs["filter_expr"], "chunk_level == 3")
        embedding.get_embeddings.assert_not_called()
        self.assertEqual(result["meta"]["retrieval_mode"], "bm25")

    def test_bm25_milvus_store_uses_sparse_field_and_bm25_metric(self):
        settings = MilvusSettings(host="h", port="p", collection_name="eval", uri="http://h:p", timeout=1)
        store = MilvusStore(settings)
        client = Mock()
        client.search.return_value = [[]]
        with patch.object(store, "_run", side_effect=lambda operation: operation(client)):
            store.bm25_retrieve("iphone", top_k=7, filter_expr="chunk_level == 3")
        kwargs = client.search.call_args.kwargs
        self.assertEqual(kwargs["anns_field"], "sparse_embedding")
        self.assertEqual(kwargs["search_params"]["metric_type"], "BM25")
        self.assertEqual(kwargs["data"], ["iphone"])


class CleanupAndJudgeTests(unittest.TestCase):
    def test_multihop_continues_after_a_single_case_error(self):
        cases = [
            {
                "id": "failed",
                "question": "first",
                "reference_answer": "first answer",
                "question_type": "inference",
                "evidence_filenames": ["first.md"],
            },
            {
                "id": "completed",
                "question": "second",
                "reference_answer": "second answer",
                "question_type": "inference",
                "evidence_filenames": [],
            },
        ]
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch("backend.evaluation.runner._evaluation_store", return_value=Mock()), patch(
                "backend.evaluation.runner.ParentChunkStore", return_value=Mock()
            ), patch(
                "backend.rag.pipeline.run_rag_graph",
                side_effect=[TimeoutError("model timed out"), {"docs": [], "rag_trace": {}}],
            ), patch(
                "backend.evaluation.runner.judge_multihop_answer",
                return_value={"verdict": "pass"},
            ):
                records, summary = evaluate_multihop(
                    output_dir,
                    {"collection_name": "rag_eval_unit", "run_id": "unit"},
                    cases,
                    case_runner=_evaluate_multihop_case,
                )

        self.assertEqual([record["case_id"] for record in records], ["failed", "completed"])
        self.assertEqual(records[0]["answer_grade"]["verdict"], "review")
        self.assertIn("TimeoutError", records[0]["evaluation_error"])
        self.assertEqual(records[1]["answer_grade"]["verdict"], "pass")
        self.assertEqual(summary["case_count"], 2)

    def test_timeout_record_is_marked_for_manual_review(self):
        record = _multihop_error_record(
            {
                "id": "slow",
                "question": "Q",
                "reference_answer": "A",
                "question_type": "inference",
                "evidence_filenames": ["expected.md"],
            },
            TimeoutError("评测单题超过 300 秒总时限"),
            elapsed_seconds=300.0,
        )
        self.assertEqual(record["answer_grade"]["verdict"], "review")
        self.assertEqual(record["evidence_coverage"], 0.0)
        self.assertIn("TimeoutError", record["evaluation_error"])

    def test_worker_exit_error_includes_exit_code(self):
        worker = Mock()
        worker.process.is_alive.return_value = False
        worker.process.exitcode = -9
        worker.result_queue.get.side_effect = queue.Empty

        with self.assertRaisesRegex(RuntimeError, r"exitcode=-9"):
            _run_case_in_worker(worker, {"id": "failed"}, timeout_seconds=1)

    def test_stop_worker_releases_queue_threads_and_process_handle(self):
        worker = Mock()
        worker.process.is_alive.return_value = False

        _stop_multihop_worker(worker)

        worker.request_queue.close.assert_called_once_with()
        worker.request_queue.join_thread.assert_called_once_with()
        worker.result_queue.close.assert_called_once_with()
        worker.result_queue.join_thread.assert_called_once_with()
        worker.process.close.assert_called_once_with()

    def test_multihop_restarts_worker_after_case_evaluation_error(self):
        cases = [
            {
                "id": "failed",
                "question": "first",
                "reference_answer": "first answer",
                "question_type": "basic",
                "evidence_filenames": ["first.md"],
            },
            {
                "id": "next",
                "question": "second",
                "reference_answer": "second answer",
                "question_type": "basic",
                "evidence_filenames": ["second.md"],
            },
        ]
        failed = _multihop_error_record(cases[0], RuntimeError("APIConnectionError"), elapsed_seconds=1)
        next_failed = _multihop_error_record(cases[1], RuntimeError("APIConnectionError"), elapsed_seconds=1)

        with TemporaryDirectory() as directory, patch(
            "backend.evaluation.runner._start_multihop_worker",
            side_effect=[Mock(), Mock()],
        ) as start_worker, patch(
            "backend.evaluation.runner._run_case_in_worker",
            side_effect=[failed, next_failed],
        ), patch("backend.evaluation.runner._stop_multihop_worker") as stop_worker:
            records, _ = evaluate_multihop(
                Path(directory),
                {"run_id": "unit"},
                cases,
            )

        self.assertEqual([record["case_id"] for record in records], ["failed", "next"])
        self.assertEqual(start_worker.call_count, 2)
        self.assertEqual(
            sum(call.args[0] is not None for call in stop_worker.call_args_list),
            2,
        )

    def test_multihop_stops_after_provider_quota_error(self):
        cases = [
            {"id": "quota", "question": "first", "reference_answer": "A", "question_type": "basic", "evidence_filenames": []},
            {"id": "not-run", "question": "second", "reference_answer": "B", "question_type": "basic", "evidence_filenames": []},
        ]
        quota = _multihop_error_record(
            cases[0],
            RuntimeError("APIStatusError: Error code: 402 - account balance is insufficient"),
            elapsed_seconds=1,
        )
        with TemporaryDirectory() as directory, patch(
            "backend.evaluation.runner._start_multihop_worker", return_value=Mock()
        ) as start_worker, patch(
            "backend.evaluation.runner._run_case_in_worker", return_value=quota
        ) as run_case:
            records, summary = evaluate_multihop(Path(directory), {"run_id": "unit"}, cases)
            progress = json.loads((Path(directory) / "evaluation-progress.json").read_text(encoding="utf-8"))

        self.assertEqual([record["case_id"] for record in records], ["quota"])
        self.assertEqual(start_worker.call_count, 1)
        run_case.assert_called_once()
        self.assertEqual(summary["evaluation_status"], "interrupted")
        self.assertEqual(progress["status"], "interrupted")

    def test_multihop_checkpoint_is_durable_and_deduplicated_by_case_id(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            _append_result_checkpoint(path, {"case_id": "1", "answer": "first"})
            _append_result_checkpoint(path, {"case_id": "1", "answer": "latest"})
            _append_result_checkpoint(path, {"case_id": "2", "answer": "second"})
            records = _read_multihop_checkpoints(path)
        self.assertEqual(len(records), 2)
        by_id = {record["case_id"]: record for record in records}
        self.assertEqual(by_id["1"]["answer"], "latest")
        self.assertEqual(by_id["2"]["answer"], "second")

    def test_cleanup_is_idempotent_from_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"collection_name": "rag_eval_ecomretrieval_unit", "parent_filenames": ["__rag_eval__unit__a.md"]}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            store = Mock()
            parent_store = Mock()
            parent_store.delete_by_filename.return_value = 0
            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner._evaluation_store", return_value=store
            ), patch("backend.evaluation.runner.ParentChunkStore", return_value=parent_store):
                first = cleanup_run(dataset="ecomretrieval", run_id="unit")
                second = cleanup_run(dataset="ecomretrieval", run_id="unit")
        self.assertEqual(first["deleted_parent_chunks"], 0)
        self.assertEqual(second["deleted_parent_chunks"], 0)
        self.assertEqual(store.drop_collection.call_count, 2)
        self.assertEqual(parent_store.delete_by_filename.call_count, 2)

    def test_cleanup_skips_legacy_invalid_collection_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"collection_name": "rag_eval_bad-run", "parent_filenames": []}),
                encoding="utf-8",
            )
            with patch("backend.evaluation.runner.run_directory", return_value=root), patch(
                "backend.evaluation.runner._evaluation_store"
            ) as store_factory, patch("backend.evaluation.runner.ParentChunkStore"):
                result = cleanup_run(dataset="ecomretrieval", run_id="unit")
        self.assertEqual(result["collection_drop_status"], "skipped_invalid_legacy_name")
        store_factory.assert_not_called()

    def test_evaluate_rejects_unfinished_prepare(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"run_id": "unit"}), encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps({"cleanup_completed": False, "prepare_completed": False}),
                encoding="utf-8",
            )
            with patch("backend.evaluation.runner.run_directory", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "尚未完成入库"):
                    evaluate_run(dataset="ecomretrieval", run_id="unit")

    def test_judge_parse_error_degrades_to_manual_review(self):
        client = Mock()
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
        client.post.return_value = response
        config = Mock(base_url="https://example.test", model="judge", timeout_seconds=1)
        grade = judge_multihop_answer(
            {"question": "Q", "reference_answer": "A", "question_type": "null"},
            "answer",
            client=client,
            config=config,
        )
        self.assertEqual(grade["verdict"], "review")
        self.assertTrue(grade["grader_error"])


class TranslationTests(unittest.TestCase):
    def test_translation_protected_tokens_are_checked(self):
        self.assertEqual(validate_translation("Use `stream.timebox_finalized` on 10 MiB", "使用 `stream.timebox_finalized`，大小为 10 MiB"), [])
        self.assertTrue(validate_translation("Use `stream.timebox_finalized`", "使用指标"))

    def test_translation_accepts_equivalent_chinese_time_units(self):
        source = "Wait 30 minutes and keep the release stable for 24h; cap is 10 MiB."
        translated = "等待 30 分钟，并让发布稳定 24 小时；上限为 10 MiB。"
        self.assertEqual(validate_translation(source, translated), [])
        self.assertTrue(validate_translation(source, "等待 30，并让发布稳定 24；上限为 10 MiB。"))

    def test_translation_keeps_status_codes_without_preserving_descriptions(self):
        source = "HTTP 429s use 429 classification and 429 overload handling."
        translated = "HTTP 429 使用 429 分类和 429 过载处理。"
        self.assertEqual(validate_translation(source, translated), [])

    def test_translation_segments_keep_original_boundaries(self):
        source = "first line\nsecond line\nthird line\n"
        segments = _split_translation_segments(source, max_characters=18)
        self.assertEqual("".join(segments), source)
        self.assertTrue(all(len(segment) <= 18 for segment in segments))

    def test_translation_masks_and_restores_protected_values(self):
        source = "Use `stream.limit` at 10 MiB: https://linear.app/example/ENG-1。"
        masked, replacements = _mask_protected_tokens(source)
        self.assertNotIn("stream.limit", masked)
        self.assertNotIn("10 MiB", masked)
        self.assertNotIn("https://linear.app", masked)
        restored, missing = _restore_protected_tokens(masked, replacements)
        self.assertEqual(restored, source)
        self.assertEqual(missing, [])

    def test_document_translation_uses_isolated_clients_and_keeps_order(self):
        created = []

        class FakeClient:
            def __init__(self, config, cache_dir):
                created.append((config, cache_dir))

            def translate(self, text, *, kind, segment_workers=1):
                return {"text": f"中文：{text}"}

            def close(self):
                return None

        config = TranslationConfig("https://example.test/v1", "secret", "translator", timeout_seconds=1)
        with TemporaryDirectory() as directory, patch("backend.evaluation.runner.TranslationClient", FakeClient):
            translated = _translate_markdown_documents(["first", "second"], config, Path(directory), workers=2)
        self.assertEqual(translated, ["中文：first", "中文：second"])
        self.assertEqual(len(created), 2)

    def test_translation_worker_count_is_bounded(self):
        with patch.dict("os.environ", {"TRANSLATION_MAX_WORKERS": "20"}):
            self.assertEqual(_translation_worker_count(), 6)
        with patch.dict("os.environ", {"TRANSLATION_MAX_WORKERS": "invalid"}):
            self.assertEqual(_translation_worker_count(), 1)

    def test_translation_sends_long_document_in_one_request(self):
        source = "企业知识库内容\n" * 80
        with TemporaryDirectory() as directory:
            config = TranslationConfig("https://example.test/v1", "secret", "translator", timeout_seconds=1)
            client = TranslationClient(config, Path(directory))
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"choices": [{"message": {"content": "整篇译文"}}]}
            with patch.object(client.session, "post", return_value=response) as post:
                result = client.translate(source, segment_workers=4)
            self.assertEqual(result["translation_strategy"], "whole_document")
            self.assertEqual(result["segment_count"], 1)
            post.assert_called_once()
            self.assertIn("企业知识库内容", post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_translation_uses_its_own_timeout_setting(self):
        values = {
            "BASE_URL": "https://example.test/v1",
            "ARK_API_KEY": "secret",
            "MODEL": "translator",
            "TRANSLATION_TIMEOUT_SECONDS": "135",
        }
        with patch.dict("os.environ", values, clear=True):
            self.assertEqual(TranslationConfig.from_env().timeout_seconds, 135.0)

    def test_translation_cache_reuses_valid_payload(self):
        with TemporaryDirectory() as directory:
            config = TranslationConfig("https://example.test/v1", "secret", "translator", timeout_seconds=1)
            client = TranslationClient(config, Path(directory))
            def preserve_placeholders(*args, **kwargs):
                prompt = kwargs["json"]["messages"][0]["content"]
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {
                    "choices": [{"message": {"content": "使用 " + " ".join(re.findall(r"\[\[RAG_KEEP_\d+\]\]", prompt))}}]
                }
                return response

            with patch.object(client.session, "post", side_effect=preserve_placeholders) as post:
                first = client.translate("Use `x` 10 MiB")
                second = client.translate("Use `x` 10 MiB")
            self.assertEqual(first["text"], second["text"])
            post.assert_called_once()

    def test_translation_segment_cache_reuses_completed_segment(self):
        with TemporaryDirectory() as directory:
            config = TranslationConfig("https://example.test/v1", "secret", "translator", timeout_seconds=1)
            client = TranslationClient(config, Path(directory))

            def preserve_placeholder(*args, **kwargs):
                prompt = kwargs["json"]["messages"][0]["content"]
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {
                    "choices": [{"message": {"content": "使用 " + " ".join(re.findall(r"\[\[RAG_KEEP_\d+\]\]", prompt))}}]
                }
                return response

            with patch.object(client.session, "post", side_effect=preserve_placeholder) as post:
                first = client._translate_segment("Use `x` 10 MiB", "document")
                second = client._translate_segment("Use `x` 10 MiB", "document")
            self.assertEqual(first, second)
            post.assert_called_once()

    def test_translation_retries_with_missing_literal_hint(self):
        with TemporaryDirectory() as directory:
            config = TranslationConfig("https://example.test/v1", "secret", "translator", timeout_seconds=1)
            client = TranslationClient(config, Path(directory))
            missing = Mock()
            missing.raise_for_status.return_value = None
            missing.json.return_value = {"choices": [{"message": {"content": "请查看关联工单。"}}]}
            calls = 0

            def retry_response(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return missing
                prompt = kwargs["json"]["messages"][0]["content"]
                placeholder = re.search(r"\[\[RAG_KEEP_1\]\]", prompt).group(0)
                valid = Mock()
                valid.raise_for_status.return_value = None
                valid.json.return_value = {"choices": [{"message": {"content": f"请查看 {placeholder}。"}}]}
                return valid

            with patch.object(client.session, "post", side_effect=retry_response) as post:
                result = client.translate("See https://linear.app/example/ENG-1")
            self.assertIn("https://linear.app/example/ENG-1", result["text"])
            self.assertEqual(post.call_count, 2)
            second_prompt = post.call_args_list[1].kwargs["json"]["messages"][0]["content"]
            self.assertIn("[[RAG_KEEP_1]]", second_prompt)


class PrepareRunTests(unittest.TestCase):
    def test_collection_name_converts_run_id_hyphens_for_milvus(self):
        self.assertEqual(
            _collection_name("ecomretrieval", "ecom-smoke-001"),
            "rag_eval_ecomretrieval_ecom_smoke_001",
        )

    def test_enterprise_zh_prepare_uses_canonical_docs_and_translates_only_cases(self):
        source_case = {
            "id": "q1",
            "question": "What is the limit?",
            "reference_answer": "The limit is 10 MiB.",
            "question_type": "basic",
            "expected_doc_ids": ["d1"],
            "answer_facts": ["10 MiB"],
            "source_types": ["slack"],
        }
        source_record = {
            "doc_id": "d1",
            "source_type": "slack",
            "title": "Upload limits",
            "content": "The English source.",
        }
        translated_calls = []

        class FakeTranslationClient:
            def __init__(self, config, cache_dir):
                self.config = config
                self.cache_dir = cache_dir

            def translate(self, text, *, kind, segment_workers=1):
                translated_calls.append((text, kind))
                return {"text": f"中文 {kind}: {text}"}

            def close(self):
                return None

        with TemporaryDirectory() as directory:
            root = Path(directory)
            loader = Mock()
            loader.load_document.return_value = [{
                "filename": "__rag_eval__unit__enterprise__d1.md",
                "text": "# 中文\n",
                "chunk_level": 3,
            }]
            config = TranslationConfig("https://example.test/v1", "secret", "translator")
            with patch("backend.evaluation.runner.ENTERPRISE_ROOT", root), patch(
                "backend.evaluation.runner.load_enterprise_cases", return_value=[source_case]
            ), patch(
                "backend.evaluation.runner.select_enterprise_smoke_cases", return_value=[source_case]
            ), patch(
                "backend.evaluation.runner.select_enterprise_ordinary_ids", return_value=(set(), {}, {})
            ), patch(
                "backend.evaluation.runner.load_enterprise_documents_by_ids", return_value=[source_record]
            ), patch(
                "backend.evaluation.runner.enterprise_document_count", return_value=1
            ), patch(
                "backend.evaluation.runner.load_enterprise_canonical_markdown",
                return_value={"d1": "# 中文上传限制\n\n中文正文\n"},
            ) as canonical_loader, patch(
                "backend.evaluation.runner.TranslationConfig.from_env", return_value=config
            ), patch(
                "backend.evaluation.runner.TranslationClient", FakeTranslationClient
            ), patch("backend.evaluation.runner.DocumentLoader", return_value=loader):
                cases, parent_chunks, leaf_chunks, metadata = _prepare_enterprise_documents(
                    run_id="unit",
                    profile="smoke",
                    language="zh",
                    corpus="representative",
                )

        canonical_loader.assert_called_once_with(root, ["d1"])
        self.assertEqual(translated_calls, [
            (source_case["question"], "question"),
            (source_case["reference_answer"], "reference_answer"),
        ])
        self.assertEqual(cases[0]["translation_source"], "translation_api_cached")
        self.assertEqual(metadata["translation_strategy"], "canonical_manual_retranslation")
        self.assertEqual(metadata["document_manifest"][0]["translation_source"], "canonical_manual_retranslation")
        self.assertEqual(parent_chunks, [])
        self.assertEqual(len(leaf_chunks), 1)

    def test_ecom_manifest_is_written_before_indexing_without_parent_chunks(self):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "ecom-run"
            store = Mock()
            parent_store = Mock()
            writer = Mock()

            def fake_leaf_chunks(source_root, markdown_dir, id_to_filename):
                id_to_filename["doc-1"] = "doc-1.md"
                yield {"filename": "doc-1.md", "text": "# 商品\n", "chunk_level": 3}

            def assert_manifest_before_write(documents):
                manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["collection_name"], "rag_eval_ecomretrieval_unit")
                self.assertFalse(manifest["cleanup_completed"])
                self.assertEqual(len(list(documents)), 1)

            writer.write_documents.side_effect = assert_manifest_before_write
            with patch("backend.evaluation.runner.run_directory", return_value=output_dir), patch(
                "backend.evaluation.runner.load_ecom_cases",
                return_value=[{"id": "query-1", "question": "问题", "relevant_document_ids": ["doc-1"]}],
            ), patch(
                "backend.evaluation.runner.ecom_corpus_count", return_value=1
            ), patch(
                "backend.evaluation.runner._iter_ecom_leaf_chunks",
                side_effect=fake_leaf_chunks,
            ), patch("backend.evaluation.runner._evaluation_store", return_value=store), patch(
                "backend.evaluation.runner.ParentChunkStore", return_value=parent_store
            ), patch("backend.evaluation.runner.MilvusWriter", return_value=writer):
                prepare_run(dataset="ecomretrieval", run_id="unit", profile="smoke")
            cases = [
                json.loads(line)
                for line in (output_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(cases[0]["relevant_filenames"], ["doc-1.md"])

        parent_store.upsert_documents.assert_not_called()
        writer.write_documents.assert_called_once()

    def test_multihop_parent_chunks_use_evaluation_filename_prefix(self):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "multihop-run"
            store = Mock()
            parent_store = Mock()
            writer = Mock()
            filename = "__rag_eval__unit__article.md"
            chunks = [
                {"filename": filename, "chunk_level": 1},
                {"filename": filename, "chunk_level": 2},
                {"filename": filename, "chunk_level": 3},
            ]
            loader = Mock()
            loader.load_document.return_value = chunks
            with patch("backend.evaluation.runner.run_directory", return_value=output_dir), patch(
                "backend.evaluation.runner.load_multihop_dataset",
                return_value=(
                    [{"url": "https://example.test/article", "title": "Article", "body": "Body"}],
                    [{"id": "1", "question": "Q", "reference_answer": "A", "question_type": "inference", "evidence_urls": ["https://example.test/article"]}],
                ),
            ), patch(
                "backend.evaluation.runner.write_markdown_documents",
                return_value=([
                    {"source_id": "https://example.test/article", "filename": filename, "path": "unused", "markdown": "# Article\n"}
                ], {"https://example.test/article": filename}),
            ) as write_documents, patch("backend.evaluation.runner.DocumentLoader", return_value=loader), patch(
                "backend.evaluation.runner._evaluation_store", return_value=store
            ), patch("backend.evaluation.runner.ParentChunkStore", return_value=parent_store), patch(
                "backend.evaluation.runner.MilvusWriter", return_value=writer
            ):
                prepare_run(dataset="multihoprag", run_id="unit", profile="smoke")

        self.assertEqual(write_documents.call_args.kwargs["filename_prefix"], "__rag_eval__unit__")
        parent_chunks = parent_store.upsert_documents.call_args.args[0]
        self.assertTrue(all(chunk["filename"].startswith("__rag_eval__unit__") for chunk in parent_chunks))
        writer.write_documents.assert_called_once_with([chunks[-1]])


class EnterpriseFormalEvaluationTests(unittest.TestCase):
    @staticmethod
    def _formal_cases() -> list[dict[str, str]]:
        cases = []
        for question_type in sorted(ENTERPRISE_ANALYSIS_QUOTAS):
            count = ENTERPRISE_ANALYSIS_QUOTAS[question_type] + ENTERPRISE_VALIDATION_QUOTAS[question_type]
            cases.extend(
                {"id": f"{question_type}-{index:03d}", "question_type": question_type}
                for index in range(count)
            )
        return cases

    @staticmethod
    def _corpus_files(root: Path) -> None:
        cases = [
            {"id": "analysis-1", "question": "A", "question_type": "basic", "expected_evidence_filenames": ["a.md"], "evidence_filenames": ["a.md"], "case_set": "analysis"},
            {"id": "validation-1", "question": "V", "question_type": "basic", "expected_evidence_filenames": ["v.md"], "evidence_filenames": ["v.md"], "case_set": "validation"},
        ]
        root.mkdir(parents=True)
        (root / "config.json").write_text(json.dumps({
            "run_id": "corpus", "profile": "full", "collection_name": "rag_eval_enterpriserag_corpus",
            "language": "en", "corpus": "representative", "evaluation_mode": "rag",
        }), encoding="utf-8")
        split_path = root / "case-split.json"
        split_path.write_text(json.dumps({"analysis_case_ids": ["analysis-1"], "validation_case_ids": ["validation-1"]}), encoding="utf-8")
        split_hash = __import__("hashlib").sha256(split_path.read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps({
            "collection_name": "rag_eval_enterpriserag_corpus", "prepare_completed": True,
            "cleanup_completed": False, "case_split_sha256": split_hash,
        }), encoding="utf-8")
        (root / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8"
        )

    def test_formal_case_split_is_stable_disjoint_and_matches_quotas(self):
        cases = self._formal_cases()
        first = split_enterprise_cases(cases)
        second = split_enterprise_cases(list(reversed(cases)))
        self.assertEqual(first, second)
        self.assertEqual(len(first["analysis_case_ids"]), 300)
        self.assertEqual(len(first["validation_case_ids"]), 200)
        self.assertFalse(set(first["analysis_case_ids"]) & set(first["validation_case_ids"]))
        for question_type, quota in first["question_types"].items():
            self.assertEqual(len(quota["analysis_case_ids"]), ENTERPRISE_ANALYSIS_QUOTAS[question_type])
            self.assertEqual(len(quota["validation_case_ids"]), ENTERPRISE_VALIDATION_QUOTAS[question_type])

    def test_formal_experiments_reuse_corpus_and_do_not_overwrite_config(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            self._corpus_files(corpus_dir)
            observed = []

            def fake_retrieval(output_dir, config, cases):
                observed.append((output_dir, config["collection_name"], [case["id"] for case in cases]))
                records = [{
                    "case_id": case["id"], "question_type": case["question_type"], "strategy": "hybrid",
                    "relevant_ranks": [1], "retrieval_seconds": 0.1,
                } for case in cases]
                return records, {
                    "dataset": "enterpriserag", "run_id": config["run_id"], "strategies": {},
                    "rerank_strategy_status": "not_configured", "rerank_successful_calls": 0,
                    "rerank_attempted_cases": 0,
                }

            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir), patch(
                "backend.evaluation.runner.evaluate_enterprise_retrieval", side_effect=fake_retrieval
            ):
                first = evaluate_run(dataset="enterpriserag", run_id="corpus", evaluation_id="retrieval-a", evaluation_mode="retrieval")
                second = evaluate_run(dataset="enterpriserag", run_id="corpus", evaluation_id="retrieval-b", evaluation_mode="retrieval")
                with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
                    evaluate_run(
                        dataset="enterpriserag", run_id="corpus", evaluation_id="retrieval-a",
                        evaluation_mode="retrieval", changed_variable="top_k",
                    )
                config_written = (first / "evaluation-config.json").is_file()
                corpus_results_written = (corpus_dir / "results.jsonl").is_file()

        self.assertNotEqual(first, second)
        self.assertEqual([item[1] for item in observed], ["rag_eval_enterpriserag_corpus"] * 2)
        self.assertTrue(config_written)
        self.assertFalse(corpus_results_written)

    def test_retry_failed_cases_preserves_source_and_merges_successes(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            self._corpus_files(corpus_dir)
            calls = []

            def fake_rag(output_dir, config, selected_cases, **kwargs):
                calls.append([case["id"] for case in selected_cases])
                records = []
                for case in selected_cases:
                    failed = config.get("retry_source_evaluation_id") is None and case["id"] == "validation-1"
                    records.append({
                        "case_id": case["id"],
                        "question": case["question"],
                        "question_type": case["question_type"],
                        "reference_answer": "A",
                        "answer": "A",
                        "expected_evidence_filenames": case["evidence_filenames"],
                        "retrieved_filenames": case["evidence_filenames"],
                        "evidence_coverage": 1.0,
                        "answer_grade": {"verdict": "review" if failed else "pass", "reason": "quota" if failed else "ok"},
                        "evaluation_error": "APIStatusError: Error code: 402" if failed else "",
                        "rag_trace": {},
                        "end_to_end_seconds": 0.1,
                    })
                return records, {"dataset": "enterpriserag", "run_id": config["run_id"], "judge_request_type": "independent_grade_model"}

            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                source = evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="baseline-rag-003", evaluation_mode="rag"
                )
                repaired = evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="baseline-rag-004",
                    evaluation_mode="rag", retry_from_evaluation_id="baseline-rag-003",
                )

            self.assertEqual(calls, [["analysis-1", "validation-1"], ["validation-1"]])
            source_records = [json.loads(line) for line in (source / "results.jsonl").read_text(encoding="utf-8").splitlines()]
            merged_records = [json.loads(line) for line in (repaired / "results.jsonl").read_text(encoding="utf-8").splitlines()]
            attempt_records = [json.loads(line) for line in (repaired / "attempt-results.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(source_records[1]["evaluation_error"], "APIStatusError: Error code: 402")
            self.assertEqual({record["case_id"] for record in merged_records}, {"analysis-1", "validation-1"})
            self.assertTrue(all(not record.get("evaluation_error") for record in merged_records))
            self.assertEqual([record["case_id"] for record in attempt_records], ["validation-1"])
            retry_manifest = json.loads((repaired / "retry-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(retry_manifest["retry_case_ids"], ["validation-1"])

    def test_retry_with_unresolved_errors_keeps_overall_status_interrupted(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            self._corpus_files(corpus_dir)

            def fake_rag(output_dir, config, selected_cases, **kwargs):
                records = []
                for case in selected_cases:
                    failed = case["id"] == "validation-1"
                    records.append({
                        "case_id": case["id"],
                        "question": case["question"],
                        "question_type": case["question_type"],
                        "reference_answer": "A",
                        "answer": "",
                        "expected_evidence_filenames": case["evidence_filenames"],
                        "retrieved_filenames": [],
                        "evidence_coverage": 0.0 if failed else 1.0,
                        "answer_grade": {"verdict": "review" if failed else "pass", "reason": "timeout" if failed else "ok"},
                        "evaluation_error": "TimeoutError: test timeout" if failed else "",
                        "rag_trace": {},
                        "end_to_end_seconds": 0.1,
                    })
                return records, {
                    "dataset": "enterpriserag",
                    "run_id": config["run_id"],
                    "evaluation_status": "completed",
                }

            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="baseline-rag-003", evaluation_mode="rag"
                )
                repaired = evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="baseline-rag-004",
                    evaluation_mode="rag", retry_from_evaluation_id="baseline-rag-003",
                )

            summary = json.loads((repaired / "summary.json").read_text(encoding="utf-8"))
            progress = json.loads((repaired / "evaluation-progress.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["evaluation_status"], "interrupted")
            self.assertEqual(summary["unresolved_case_count"], 1)
            self.assertEqual(progress["status"], "interrupted")
            self.assertFalse((repaired / "results.jsonl").exists())

    def test_retry_source_chain_merges_parent_and_attempt_records(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            parent_dir = corpus_dir / "evaluations" / "baseline-rag-003"
            child_dir = corpus_dir / "evaluations" / "baseline-rag-004"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir(parents=True)
            parent_record = {"case_id": "kept", "answer": "parent"}
            child_record = {"case_id": "retried", "answer": "child"}
            (parent_dir / "results.jsonl").write_text(json.dumps(parent_record) + "\n", encoding="utf-8")
            (child_dir / "retry-manifest.json").write_text(
                json.dumps({"source_evaluation_id": "baseline-rag-003"}), encoding="utf-8"
            )
            (child_dir / "attempt-results.jsonl").write_text(json.dumps(child_record) + "\n", encoding="utf-8")
            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir):
                records, checkpoint = _read_retry_source_records(
                    "enterpriserag", "corpus", "baseline-rag-004"
                )

        self.assertEqual({record["case_id"] for record in records}, {"kept", "retried"})
        self.assertEqual(checkpoint.name, "attempt-results.jsonl")

    def test_analysis_experiment_does_not_pass_validation_cases_to_evaluator(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            self._corpus_files(corpus_dir)
            captured = []

            def fake_retrieval(output_dir, config, cases):
                captured.extend(case["id"] for case in cases)
                return [], {
                    "dataset": "enterpriserag", "run_id": config["run_id"], "strategies": {},
                    "rerank_strategy_status": "not_configured", "rerank_successful_calls": 0,
                    "rerank_attempted_cases": 0,
                }

            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir), patch(
                "backend.evaluation.runner.evaluate_enterprise_retrieval", side_effect=fake_retrieval
            ):
                evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="analysis-only",
                    case_set="analysis", evaluation_mode="retrieval",
                )
        self.assertEqual(captured, ["analysis-1"])

    def test_validation_failures_are_hidden_from_automatic_review_artifacts(self):
        with TemporaryDirectory() as directory:
            corpus_dir = Path(directory) / "corpus"
            self._corpus_files(corpus_dir)

            def fake_rag(output_dir, config, cases):
                records = []
                for case in cases:
                    verdict = "fail" if case["id"] == "validation-1" else "pass"
                    records.append({
                        "case_id": case["id"], "question_type": case["question_type"],
                        "evidence_coverage": 1.0, "answer_grade": {"verdict": verdict},
                        "rag_trace": {}, "end_to_end_seconds": 0.1,
                    })
                return records, {"dataset": "enterpriserag", "run_id": config["run_id"], "judge_request_type": "independent_grade_model"}

            with patch("backend.evaluation.runner.run_directory", return_value=corpus_dir), patch(
                "backend.evaluation.runner.evaluate_multihop", side_effect=fake_rag
            ):
                output = evaluate_run(
                    dataset="enterpriserag", run_id="corpus", evaluation_id="rag-baseline",
                    evaluation_mode="rag",
                )
            report = (output / "report.md").read_text(encoding="utf-8")
            review_queue = (output / "manual-review.jsonl").read_text(encoding="utf-8")
            case_review = (output / "case-review.md").read_text(encoding="utf-8")

        self.assertNotIn("validation-1", report)
        self.assertNotIn("validation-1", case_review)
        self.assertNotIn("validation-1", review_queue)
        queue_records = [json.loads(line) for line in review_queue.splitlines() if line.strip()]
        self.assertEqual(len(queue_records), 1)
        self.assertEqual(queue_records[0]["record"], {"case_set": "validation", "redacted": True})
        self.assertEqual(queue_records[0]["review_reasons"], ["validation_case_suppressed"])
        self.assertTrue(report.startswith("# EnterpriseRAG 完整链路评测报告"))

    def test_case_review_report_exposes_question_answer_reference_and_evidence(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            report_path = _write_case_review_report(
                output,
                [{
                    "case_id": "qst_0381",
                    "case_set": "smoke",
                    "question_type": "basic",
                    "question": "What caused the oscillation?",
                    "reference_answer": "Noisy health signals caused it.",
                    "answer": "The routing signals were noisy.",
                    "expected_evidence_filenames": ["gold.md"],
                    "retrieved_filenames": ["gold.md", "noise.md"],
                    "evidence_ranks": [1],
                    "evidence_coverage": 1.0,
                    "answer_grade": {
                        "verdict": "review",
                        "reason": "Extra unsupported ticket.",
                    },
                    "end_to_end_seconds": 1.25,
                }],
                dataset="enterpriserag",
                evaluation_mode="rag",
            )
            report = report_path.read_text(encoding="utf-8")

        self.assertIn("What caused the oscillation?", report)
        self.assertIn("Noisy health signals caused it.", report)
        self.assertIn("The routing signals were noisy.", report)
        self.assertIn("`gold.md`", report)
        self.assertIn("Extra unsupported ticket.", report)
        self.assertIn("`review`", report)

    def test_retrieval_case_review_groups_strategies_by_question(self):
        with TemporaryDirectory() as directory:
            report_path = _write_case_review_report(
                Path(directory),
                [
                    {
                        "case_id": "q1",
                        "question_type": "basic",
                        "question": "Find the answer.",
                        "strategy": "bm25",
                        "expected_evidence_filenames": ["gold.md"],
                        "retrieved_filenames": ["other.md"],
                        "relevant_ranks": [],
                        "retrieval_seconds": 0.01,
                    },
                    {
                        "case_id": "q1",
                        "question_type": "basic",
                        "question": "Find the answer.",
                        "strategy": "dense",
                        "expected_evidence_filenames": ["gold.md"],
                        "retrieved_filenames": ["gold.md"],
                        "relevant_ranks": [1],
                        "retrieval_seconds": 0.02,
                    },
                ],
                dataset="enterpriserag",
                evaluation_mode="retrieval",
            )
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(report.count("### q1"), 1)
        self.assertIn("##### bm25", report)
        self.assertIn("##### dense", report)
        self.assertIn("未召回", report)
        self.assertIn("标准证据排名：1", report)

    def test_partial_rerank_is_not_a_comparison_strategy(self):
        case = {"id": "q1", "question": "Q", "question_type": "basic", "expected_evidence_filenames": ["gold.md"]}
        with TemporaryDirectory() as directory, patch("backend.evaluation.runner.RERANK_ENABLED", True), patch(
            "backend.evaluation.runner._evaluation_store", return_value=Mock()
        ), patch("backend.evaluation.runner.retrieve_documents", return_value={
            "docs": [{"filename": "gold.md"}],
            "meta": {"retrieval_mode": "hybrid", "rerank_applied": False, "rerank_error": "timeout"},
        }):
            _, summary = evaluate_enterprise_retrieval(
                Path(directory), {"collection_name": "rag_eval_unit", "run_id": "unit"}, [case]
            )
        self.assertEqual(summary["rerank_strategy_status"], "not_effective")
        self.assertNotIn("hybrid_rerank", summary["strategies"])


if __name__ == "__main__":
    unittest.main()
