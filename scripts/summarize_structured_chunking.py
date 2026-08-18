"""Audit the targeted structured-chunking evaluation against the frozen baseline.

This script is read-only.  It compares already persisted JSONL artifacts and
never calls a model, embedding service, reranker, or storage service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_VARIABLE = "document_chunking_strategy"
EXPECTED_CASE_SET = "analysis"
EXPECTED_CASE_COUNT = 30


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _by_case_id(records: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        case_id = str(record.get("case_id") or "")
        if not case_id or case_id in output:
            raise ValueError(f"{label} 包含缺失或重复的 case_id：{case_id or '（空）'}")
        output[case_id] = record
    return output


def _verdict(record: dict[str, Any]) -> str:
    return str((record.get("answer_grade") or {}).get("verdict") or "review")


def _coverage(record: dict[str, Any]) -> float:
    try:
        return float(record.get("evidence_coverage") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _has_system_error(record: dict[str, Any]) -> bool:
    grade = record.get("answer_grade") or {}
    trace = record.get("rag_trace") or {}
    return bool(
        record.get("evaluation_error")
        or record.get("answer_generation_error")
        or grade.get("grader_error")
        or trace.get("evidence_reason") == "evidence_grading_unavailable"
    )


def _usable_case_ids(
    case_ids: list[str],
    baseline: dict[str, dict[str, Any]],
    evaluated: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return paired-comparable IDs and target-only successful retry IDs.

    A retry can turn a target timeout into a valid record while the original
    baseline still has no quality result. Such a case is useful for a
    diagnostic observation, but it cannot support a before/after delta.
    """
    paired = [
        case_id
        for case_id in case_ids
        if not _has_system_error(baseline[case_id])
        and not _has_system_error(evaluated[case_id])
    ]
    target_only_recovered = [
        case_id
        for case_id in case_ids
        if _has_system_error(baseline[case_id])
        and not _has_system_error(evaluated[case_id])
    ]
    return paired, target_only_recovered


def _system_error_reasons(record: dict[str, Any]) -> list[str]:
    grade = record.get("answer_grade") or {}
    trace = record.get("rag_trace") or {}
    reasons: list[str] = []
    if record.get("evaluation_error"):
        reasons.append("evaluation_error")
    if record.get("answer_generation_error"):
        reasons.append("answer_generation_error")
    if grade.get("grader_error"):
        reasons.append("grader_error")
    if trace.get("evidence_reason") == "evidence_grading_unavailable":
        reasons.append("evidence_grading_unavailable")
    return reasons


def _stable_document_key(filename: Any) -> str:
    """Remove an evaluation run prefix so baseline and target names compare."""
    value = str(filename or "")
    match = re.search(r"enterprise__dsid_[0-9a-f]+\.md$", value)
    return match.group(0) if match else value


def _stage_document_keys(trace: dict[str, Any], field: str) -> set[str]:
    values = trace.get(field) or []
    return {
        _stable_document_key(item.get("filename"))
        for item in values
        if isinstance(item, dict) and item.get("filename")
    }


def _candidate_audit_document_keys(record: dict[str, Any], field: str) -> set[str]:
    keys: set[str] = set()
    for audit in record.get("candidate_audits") or []:
        keys.update(_stable_document_key(item.get("filename")) for item in audit.get(field) or [] if item.get("filename"))
    return keys


def _metric_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    coverages = [_coverage(record) for record in records]
    verdicts = [_verdict(record) for record in records]
    return {
        "case_count": len(records),
        "evidence_full_coverage_count": sum(value >= 1.0 for value in coverages),
        "evidence_full_coverage_rate": (
            sum(value >= 1.0 for value in coverages) / len(coverages) if coverages else None
        ),
        "evidence_average_coverage_rate": sum(coverages) / len(coverages) if coverages else None,
        "answer_pass_count": sum(value == "pass" for value in verdicts),
        "answer_pass_rate": sum(value == "pass" for value in verdicts) / len(verdicts) if verdicts else None,
        "verdict_counts": dict(sorted(Counter(verdicts).items())),
        "system_error_count": sum(_has_system_error(record) for record in records),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _latencies(records: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    output: dict[str, dict[str, float | None]] = {}
    for field in ("rag_seconds", "generation_seconds", "judge_seconds", "end_to_end_seconds"):
        values = [float(record.get(field) or 0.0) for record in records]
        output[field] = {
            "p50_seconds": _percentile(values, 0.5),
            "p95_seconds": _percentile(values, 0.95),
        }
    return output


def _duration_seconds(config: dict[str, Any], progress: dict[str, Any]) -> float | None:
    try:
        started = datetime.fromisoformat(str(config["created_at"]))
        finished = datetime.fromisoformat(str(progress["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (finished - started).total_seconds()


def build_summary(
    *,
    baseline_results: Path,
    evaluation_dir: Path,
    target_manifest: Path,
    evaluation_results: Path | None = None,
) -> dict[str, Any]:
    manifest = _read_json(target_manifest)
    case_ids = [str(value) for value in manifest.get("case_ids") or []]
    if (
        manifest.get("case_set") != EXPECTED_CASE_SET
        or manifest.get("changed_variable") != EXPECTED_VARIABLE
        or len(case_ids) != EXPECTED_CASE_COUNT
        or len(case_ids) != len(set(case_ids))
    ):
        raise ValueError("结构化分块 target manifest 必须是恰好 30 道唯一 analysis 题")

    baseline = _by_case_id(_read_jsonl(baseline_results), "baseline results")
    evaluation_results_path = evaluation_results or (
        evaluation_dir / "structured-chunking-merged-results.jsonl"
    )
    if not evaluation_results_path.is_file():
        evaluation_results_path = evaluation_dir / "results.jsonl"
    evaluated = _by_case_id(_read_jsonl(evaluation_results_path), "evaluation results")
    if not set(case_ids).issubset(baseline):
        raise ValueError("baseline results 缺少结构化分块 target 题目")
    if set(evaluated) != set(case_ids):
        raise ValueError("evaluation results 与结构化 target manifest 题目集合不一致")
    if any(str(record.get("case_set")) != EXPECTED_CASE_SET for record in evaluated.values()):
        raise ValueError("evaluation results 包含非 analysis 题目")

    baseline_rows = [baseline[case_id] for case_id in case_ids]
    evaluated_rows = [evaluated[case_id] for case_id in case_ids]
    usable_case_ids, target_only_recovered_case_ids = _usable_case_ids(
        case_ids, baseline, evaluated
    )
    target_usable_case_ids = [
        case_id for case_id in case_ids if not _has_system_error(evaluated[case_id])
    ]
    transition_counts: Counter[str] = Counter()
    transition_case_ids: dict[str, list[str]] = defaultdict(list)
    attribution_counts: Counter[str] = Counter()
    attribution_case_ids: dict[str, list[str]] = defaultdict(list)
    stage_counts: Counter[str] = Counter()
    stage_case_ids: dict[str, list[str]] = defaultdict(list)
    system_errors: list[dict[str, Any]] = []
    case_audit: list[dict[str, Any]] = []

    for case_id in case_ids:
        before = baseline[case_id]
        after = evaluated[case_id]
        before_verdict = _verdict(before)
        after_verdict = _verdict(after)
        before_coverage = _coverage(before)
        after_coverage = _coverage(after)
        coverage_direction = (
            "coverage_up" if after_coverage > before_coverage
            else "coverage_down" if after_coverage < before_coverage
            else "coverage_same"
        )
        transition = f"{before_verdict}_to_{after_verdict};{coverage_direction}"
        transition_counts[transition] += 1
        transition_case_ids[transition].append(case_id)

        trace = after.get("rag_trace") or {}
        expected_docs = {
            _stable_document_key(filename)
            for filename in after.get("expected_evidence_filenames") or []
        }
        stages = {
            "initial_candidates": _stage_document_keys(trace, "initial_retrieved_chunks"),
            "raw_candidates": _candidate_audit_document_keys(after, "raw_leaf_candidates"),
            "post_merge_candidates": _candidate_audit_document_keys(after, "post_merge_candidates"),
            "rerank_input_candidates": _candidate_audit_document_keys(after, "rerank_input_candidates"),
            "final_context": _candidate_audit_document_keys(after, "final_context_candidates"),
        }
        stage_hits = {
            stage: sorted(expected_docs & filenames)
            for stage, filenames in stages.items()
        }
        for stage, hits in stage_hits.items():
            if hits:
                stage_counts[f"{stage}_contains_expected_document"] += 1
                stage_case_ids[f"{stage}_contains_expected_document"].append(case_id)

        if _has_system_error(after):
            attribution = "system_error"
            system_errors.append({
                "case_id": case_id,
                "verdict": after_verdict,
                "reasons": _system_error_reasons(after),
                "evaluation_error": str(after.get("evaluation_error") or ""),
            })
        elif _has_system_error(before):
            attribution = "baseline_system_error_no_comparison"
        elif before_verdict != "pass" and after_verdict == "pass" and coverage_direction == "coverage_up":
            attribution = "evidence_recovered_and_answer_pass_candidate"
        elif coverage_direction == "coverage_up":
            attribution = "evidence_recovered_but_answer_not_pass"
        elif coverage_direction == "coverage_down":
            attribution = "evidence_coverage_regressed"
        elif before_verdict != after_verdict and after_verdict == "pass":
            attribution = "answer_pass_without_coverage_gain"
        else:
            attribution = "no_measurable_evidence_coverage_change"
        attribution_counts[attribution] += 1
        attribution_case_ids[attribution].append(case_id)

        case_audit.append({
            "case_id": case_id,
            "question_type": after.get("question_type"),
            "baseline": {
                "verdict": before_verdict,
                "evidence_coverage": before_coverage,
                "system_error": _has_system_error(before),
            },
            "structured": {
                "verdict": after_verdict,
                "evidence_coverage": after_coverage,
                "system_error": _has_system_error(after),
                "expected_document_keys": sorted(expected_docs),
                "stage_expected_document_hits": stage_hits,
                "raw_candidate_count": sum(len(audit.get("raw_leaf_candidates") or []) for audit in after.get("candidate_audits") or []),
                "final_context_count": sum(len(audit.get("final_context_candidates") or []) for audit in after.get("candidate_audits") or []),
                "auto_merge_mappings": sum(len(audit.get("auto_merge_mappings") or []) for audit in after.get("candidate_audits") or []),
            },
            "coverage_direction": coverage_direction,
            "automatic_attribution": attribution,
        })

    config = _read_json(evaluation_dir / "evaluation-config.json")
    progress = _read_json(evaluation_dir / "evaluation-progress.json")
    summary_artifact = _read_json(evaluation_dir / "summary.json")
    manual_queue = _read_jsonl(evaluation_dir / "manual-review.jsonl") if (evaluation_dir / "manual-review.jsonl").exists() else []
    baseline_metrics = _metric_summary(baseline_rows)
    structured_metrics = _metric_summary(evaluated_rows)
    usable_baseline_metrics = _metric_summary([baseline[case_id] for case_id in usable_case_ids])
    usable_structured_metrics = _metric_summary([evaluated[case_id] for case_id in usable_case_ids])
    return {
        "report_version": "structured-chunking-impact-summary-v1",
        "evaluation_id": config.get("evaluation_id"),
        "corpus_run_id": config.get("corpus_run_id"),
        "changed_variable": config.get("changed_variable"),
        "document_chunking_strategy": config.get("document_chunking_strategy"),
        "rechunk_scope": config.get("rechunk_scope"),
        "case_set": config.get("case_set"),
        "case_count": len(case_ids),
        "input_hashes": {
            "baseline_results_sha256": _sha256(baseline_results),
            "evaluation_results_sha256": _sha256(evaluation_results_path),
            "target_manifest_sha256": _sha256(target_manifest),
        },
        "execution": {
            "evaluation_worker_count": config.get("evaluation_worker_count"),
            "evaluation_status": summary_artifact.get("evaluation_status"),
            "evaluation_error_count": summary_artifact.get("evaluation_error_count", 0),
            "unresolved_case_count": summary_artifact.get("unresolved_case_count", 0),
            "elapsed_seconds": _duration_seconds(config, progress),
        },
        "baseline_metrics": baseline_metrics,
        "structured_metrics": structured_metrics,
        "usable_case_count": len(usable_case_ids),
        "target_usable_case_count": len(target_usable_case_ids),
        "target_only_recovered_case_ids": target_only_recovered_case_ids,
        "baseline_system_error_case_ids": [
            case_id for case_id in case_ids if _has_system_error(baseline[case_id])
        ],
        "baseline_metrics_excluding_system_errors": usable_baseline_metrics,
        "structured_metrics_excluding_system_errors": usable_structured_metrics,
        "metric_deltas": {
            "evidence_full_coverage_rate": structured_metrics["evidence_full_coverage_rate"] - baseline_metrics["evidence_full_coverage_rate"],
            "evidence_average_coverage_rate": structured_metrics["evidence_average_coverage_rate"] - baseline_metrics["evidence_average_coverage_rate"],
            "answer_pass_rate": structured_metrics["answer_pass_rate"] - baseline_metrics["answer_pass_rate"],
        },
        "metric_deltas_excluding_system_errors": {
            "evidence_full_coverage_rate": usable_structured_metrics["evidence_full_coverage_rate"] - usable_baseline_metrics["evidence_full_coverage_rate"],
            "evidence_average_coverage_rate": usable_structured_metrics["evidence_average_coverage_rate"] - usable_baseline_metrics["evidence_average_coverage_rate"],
            "answer_pass_rate": usable_structured_metrics["answer_pass_rate"] - usable_baseline_metrics["answer_pass_rate"],
        },
        "verdict_coverage_transitions": {
            key: {"count": count, "case_ids": transition_case_ids[key]}
            for key, count in sorted(transition_counts.items())
        },
        "automatic_attribution": {
            key: {"count": count, "case_ids": attribution_case_ids[key]}
            for key, count in sorted(attribution_counts.items())
        },
        "candidate_stage_hits": {
            key: {"count": count, "case_ids": stage_case_ids[key]}
            for key, count in sorted(stage_counts.items())
        },
        "system_errors": system_errors,
        "manual_review_queue": {
            "count": len(manual_queue),
            "case_ids": [
                str((item.get("record") or {}).get("case_id") or item.get("case_id"))
                for item in manual_queue
            ],
        },
        "latencies": _latencies(evaluated_rows),
        "latencies_excluding_system_errors": _latencies(
            [evaluated[case_id] for case_id in target_usable_case_ids]
        ),
        "cases": case_audit,
        "interpretation_boundary": (
            "All counts are analysis-only targeted results. System-error cases are excluded from any final optimization gain claim. "
            "A pass plus coverage gain is an attribution candidate and still requires human review; this report is not a 500-question or validation result."
        ),
    }


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _seconds(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _markdown(summary: dict[str, Any]) -> str:
    baseline = summary["baseline_metrics"]
    structured = summary["structured_metrics"]
    delta = summary["metric_deltas"]
    usable_baseline = summary["baseline_metrics_excluding_system_errors"]
    usable_structured = summary["structured_metrics_excluding_system_errors"]
    usable_delta = summary["metric_deltas_excluding_system_errors"]
    execution = summary["execution"]
    attribution = summary["automatic_attribution"]
    usable_latencies = summary["latencies_excluding_system_errors"]
    lines = [
        "# 结构化分块 30 题影响审计",
        "",
        f"范围：`{summary['evaluation_id']}`，只比较冻结的 {summary['case_count']} 道 `analysis` 题。",
        f"唯一变量：`{summary['changed_variable']}`；分块策略：`{summary['document_chunking_strategy']}`；范围：`{summary['rechunk_scope']}`。",
        "这不是 500 题总体结果，也不涉及 validation。系统异常不能计入分块收益。",
        "",
        "## 执行完整性",
        "",
        f"- 评测状态：`{execution['evaluation_status']}`；worker：{execution['evaluation_worker_count']}；已记录题数：{summary['case_count']}。",
        f"- 当前目标结果中的系统错误：{structured['system_error_count']} 道，其中 evaluation_error {execution['evaluation_error_count']} 道；仍未形成可判定结果的题：{structured['system_error_count']} 道。",
        f"- 评测墙钟时间（按配置/进度时间戳）：{execution['elapsed_seconds']:.2f} 秒。" if execution["elapsed_seconds"] is not None else "- 评测墙钟时间：无法从配置时间戳计算。",
        f"- 仍超时题保留在逐题 JSONL 和人工队列中，不能与答案失败混算；另有 {len(summary['target_only_recovered_case_ids'])} 道从基线超时中恢复，但没有可比的 baseline 质量记录。",
        f"- 排除当前目标系统错误后的有效题延迟 P50：RAG {_seconds(usable_latencies['rag_seconds']['p50_seconds'])} 秒，生成 {_seconds(usable_latencies['generation_seconds']['p50_seconds'])} 秒，端到端 {_seconds(usable_latencies['end_to_end_seconds']['p50_seconds'])} 秒。",
        "",
        "## 指标对照",
        "",
        "| 指标 | baseline 30 题对应记录 | 结构化分块 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        f"| 证据全覆盖率 | {_percent(baseline['evidence_full_coverage_rate'])} | {_percent(structured['evidence_full_coverage_rate'])} | {_percent(delta['evidence_full_coverage_rate'])} |",
        f"| 平均证据覆盖率 | {_percent(baseline['evidence_average_coverage_rate'])} | {_percent(structured['evidence_average_coverage_rate'])} | {_percent(delta['evidence_average_coverage_rate'])} |",
        f"| 回答通过率 | {_percent(baseline['answer_pass_rate'])} | {_percent(structured['answer_pass_rate'])} | {_percent(delta['answer_pass_rate'])} |",
        f"| 结构化结果中的系统错误 | {baseline['system_error_count']} | {structured['system_error_count']} | {structured['system_error_count'] - baseline['system_error_count']} |",
        "",
        f"只保留 baseline 和结构化结果都没有系统错误的 {summary['usable_case_count']} 道题，才计算真正可比的同题对照：",
        "",
        f"| 指标 | baseline 同 {summary['usable_case_count']} 题 | 结构化分块同 {summary['usable_case_count']} 题 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        f"| 证据全覆盖率 | {_percent(usable_baseline['evidence_full_coverage_rate'])} | {_percent(usable_structured['evidence_full_coverage_rate'])} | {_percent(usable_delta['evidence_full_coverage_rate'])} |",
        f"| 平均证据覆盖率 | {_percent(usable_baseline['evidence_average_coverage_rate'])} | {_percent(usable_structured['evidence_average_coverage_rate'])} | {_percent(usable_delta['evidence_average_coverage_rate'])} |",
        f"| 回答通过率 | {_percent(usable_baseline['answer_pass_rate'])} | {_percent(usable_structured['answer_pass_rate'])} | {_percent(usable_delta['answer_pass_rate'])} |",
        "",
        "## 自动归因候选",
        "",
        f"- 证据覆盖提升且答案通过：{attribution.get('evidence_recovered_and_answer_pass_candidate', {}).get('count', 0)} 道。只能作为候选，不能直接称为分块收益。",
        f"- 证据覆盖提升但答案仍未通过：{attribution.get('evidence_recovered_but_answer_not_pass', {}).get('count', 0)} 道。材料可能补回，但回答整合仍失败。",
        f"- 证据覆盖下降：{attribution.get('evidence_coverage_regressed', {}).get('count', 0)} 道。属于退化候选，不能计入分块收益。",
        f"- 答案通过但证据覆盖没有提升：{attribution.get('answer_pass_without_coverage_gain', {}).get('count', 0)} 道。不能归因给分块。",
        f"- 系统错误：{attribution.get('system_error', {}).get('count', 0)} 道。排除在优化收益之外。",
        f"- 基线系统错误、无法比较：{attribution.get('baseline_system_error_no_comparison', {}).get('count', 0)} 道。重试结果只作补充观察。",
        "",
        "## 候选链路",
        "",
        "以下统计的是目标答案文档在新评测 trace 中是否出现，不等于关键事实已经进入上下文：",
    ]
    for stage, value in summary["candidate_stage_hits"].items():
        lines.append(f"- `{stage}`：{value['count']}/{summary['case_count']} 道。")
    lines.extend([
        "",
        "## 人工复核",
        "",
        f"- 人工队列：{summary['manual_review_queue']['count']} 道；详情在同目录 `manual-review.jsonl`、`manual-review.md`。",
        "- 逐题候选、最终上下文、Auto-merging 映射、回答、判卷和耗时在 `results.jsonl`、`candidate-audit.md`、`case-review.md`。",
        "- 只有人工确认“关键事实补回且回答因此变对”的题，才可计入最终收益。",
        "",
        "## 边界",
        "",
        "本报告不能外推到 500 题或 validation 200 题，也不能替代人工结论。仍有系统超时题必须单独列为异常，不能算作分块失败或优化收益。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize structured chunking impact without rerunning RAG.")
    parser.add_argument("--baseline-results", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument(
        "--evaluation-results",
        type=Path,
        help="离线合并的逐题结果；默认优先读取 evaluation-dir/structured-chunking-merged-results.jsonl。",
    )
    args = parser.parse_args()
    summary = build_summary(
        baseline_results=args.baseline_results.resolve(),
        evaluation_dir=args.evaluation_dir.resolve(),
        target_manifest=args.target_manifest.resolve(),
        evaluation_results=(
            args.evaluation_results.resolve() if args.evaluation_results is not None else None
        ),
    )
    output_dir = args.evaluation_dir.resolve()
    (output_dir / "structured-chunking-impact-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "structured-chunking-impact-summary.md").write_text(
        _markdown(summary), encoding="utf-8", newline="\n"
    )
    print(output_dir / "structured-chunking-impact-summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
