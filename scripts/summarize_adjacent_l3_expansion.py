"""Compare a frozen baseline with an adjacent-L3 expansion evaluation.

This is a read-only audit helper.  It never invokes RAG, a model, Milvus, or
the default business collection.  It separates measurable evidence recovery
from answer changes that still need a person to inspect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
    return float(record.get("evidence_coverage") or 0.0)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _metric_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    coverages = [_coverage(record) for record in records]
    judged = [_verdict(record) for record in records]
    return {
        "case_count": len(records),
        "evidence_full_coverage_count": sum(value >= 1.0 for value in coverages),
        "evidence_full_coverage_rate": (
            sum(value >= 1.0 for value in coverages) / len(coverages) if coverages else None
        ),
        "evidence_average_coverage_rate": sum(coverages) / len(coverages) if coverages else None,
        "evidence_zero_coverage_count": sum(value == 0.0 for value in coverages),
        "verdict_counts": dict(sorted(Counter(judged).items())),
        "answer_pass_rate": sum(value == "pass" for value in judged) / len(judged) if judged else None,
    }


def _has_system_error(record: dict[str, Any]) -> bool:
    grade = record.get("answer_grade") or {}
    return bool(
        record.get("evaluation_error")
        or record.get("answer_generation_error")
        or grade.get("grader_error")
        or (record.get("rag_trace") or {}).get("evidence_reason")
        == "evidence_grading_unavailable"
    )


def _system_error_reasons(record: dict[str, Any]) -> list[str]:
    grade = record.get("answer_grade") or {}
    reasons: list[str] = []
    if record.get("evaluation_error"):
        reasons.append("evaluation_error")
    if record.get("answer_generation_error"):
        reasons.append("answer_generation_error")
    if grade.get("grader_error"):
        reasons.append("grader_error")
    if (record.get("rag_trace") or {}).get("evidence_reason") == "evidence_grading_unavailable":
        reasons.append("evidence_grading_unavailable")
    return reasons


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
) -> dict[str, Any]:
    manifest = _read_json(target_manifest)
    case_ids = [str(value) for value in manifest.get("case_ids") or []]
    if len(case_ids) != int(manifest.get("case_count") or 0) or len(case_ids) != len(set(case_ids)):
        raise ValueError("target manifest 的 case_ids 与 case_count 不一致")
    if manifest.get("case_set") != "analysis" or manifest.get("changed_variable") != "adjacent_l3_expansion":
        raise ValueError("target manifest 不是 analysis-only adjacent_l3_expansion 评测")

    baseline = _by_case_id(_read_jsonl(baseline_results), "baseline results")
    evaluation_results = evaluation_dir / "results.jsonl"
    evaluated = _by_case_id(_read_jsonl(evaluation_results), "evaluation results")
    target_ids = set(case_ids)
    if not target_ids.issubset(baseline):
        raise ValueError("baseline results 缺少 target manifest 题目")
    if set(evaluated) != target_ids:
        raise ValueError("evaluation results 与 target manifest 的题目集合不一致")
    if any(record.get("case_set") != "analysis" for record in evaluated.values()):
        raise ValueError("evaluation results 包含非 analysis 题目")

    baseline_rows = [baseline[case_id] for case_id in case_ids]
    evaluated_rows = [evaluated[case_id] for case_id in case_ids]
    baseline_metrics = _metric_summary(baseline_rows)
    evaluated_metrics = _metric_summary(evaluated_rows)
    attempt_path = evaluation_dir / "attempt-results.jsonl"
    attempt_records = _read_jsonl(attempt_path) if attempt_path.exists() else []
    attempt_case_ids = set(_by_case_id(attempt_records, "attempt results"))

    transition_counts: Counter[str] = Counter()
    transition_case_ids: dict[str, list[str]] = defaultdict(list)
    coverage_direction_counts: Counter[str] = Counter()
    attribution_counts: Counter[str] = Counter()
    attribution_case_ids: dict[str, list[str]] = defaultdict(list)
    system_errors: list[dict[str, Any]] = []
    added_candidate_counts: list[float] = []
    expansion_applied_count = 0
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
        coverage_direction_counts[coverage_direction] += 1

        if before_verdict != "pass" and after_verdict == "pass":
            attribution = (
                "evidence_recovered_and_answer_pass_candidate"
                if coverage_direction == "coverage_up"
                else "answer_pass_without_evidence_coverage_gain"
            )
        elif coverage_direction == "coverage_up":
            attribution = "evidence_recovered_but_answer_not_pass"
        elif coverage_direction == "coverage_down":
            attribution = "evidence_coverage_regressed"
        else:
            attribution = "no_measurable_evidence_coverage_change"
        attribution_counts[attribution] += 1
        attribution_case_ids[attribution].append(case_id)

        trace = after.get("rag_trace") or {}
        if trace.get("adjacent_l3_expansion_applied"):
            expansion_applied_count += 1
        added = trace.get("adjacent_l3_expansion_added_candidate_count")
        if isinstance(added, (int, float)):
            added_candidate_counts.append(float(added))
        if _has_system_error(after):
            system_errors.append({
                "case_id": case_id,
                "verdict": after_verdict,
                "reasons": _system_error_reasons(after),
                "source": "retry_attempt" if case_id in attempt_case_ids else "preserved_source",
            })

    manual_queue_path = evaluation_dir / "manual-review.jsonl"
    manual_queue = _read_jsonl(manual_queue_path) if manual_queue_path.exists() else []
    manual_reason_counts = Counter(
        reason for item in manual_queue for reason in item.get("review_reasons") or []
    )
    config = _read_json(evaluation_dir / "evaluation-config.json")
    progress = _read_json(evaluation_dir / "evaluation-progress.json")
    latencies = {
        key: {
            "p50_seconds": _percentile(
                [float(record.get(key) or 0.0) for record in evaluated_rows], 0.5
            ),
            "p95_seconds": _percentile(
                [float(record.get(key) or 0.0) for record in evaluated_rows], 0.95
            ),
        }
        for key in ("rag_seconds", "generation_seconds", "judge_seconds", "end_to_end_seconds")
    }
    return {
        "report_version": "adjacent-l3-impact-summary-v1",
        "evaluation_id": config.get("evaluation_id"),
        "changed_variable": config.get("changed_variable"),
        "case_set": config.get("case_set"),
        "case_count": len(case_ids),
        "input_hashes": {
            "baseline_results_sha256": _sha256(baseline_results),
            "evaluation_results_sha256": _sha256(evaluation_results),
            "target_manifest_sha256": _sha256(target_manifest),
        },
        "execution": {
            "evaluation_worker_count": config.get("evaluation_worker_count", 1),
            "preserved_case_count": config.get("preserved_case_count", 0),
            "retry_case_count": config.get("retry_case_count", 0),
            "retry_attempt_completed_case_count": len(attempt_records),
            "retry_attempt_evaluation_error_count": sum(
                bool(record.get("evaluation_error")) for record in attempt_records
            ),
            "elapsed_seconds": _duration_seconds(config, progress),
        },
        "baseline_metrics": baseline_metrics,
        "evaluation_metrics": evaluated_metrics,
        "metric_deltas": {
            "evidence_full_coverage_rate": (
                evaluated_metrics["evidence_full_coverage_rate"] - baseline_metrics["evidence_full_coverage_rate"]
            ),
            "evidence_average_coverage_rate": (
                evaluated_metrics["evidence_average_coverage_rate"] - baseline_metrics["evidence_average_coverage_rate"]
            ),
            "answer_pass_rate": (
                evaluated_metrics["answer_pass_rate"] - baseline_metrics["answer_pass_rate"]
            ),
        },
        "expansion": {
            "applied_case_count": expansion_applied_count,
            "added_candidate_count_p50": _percentile(added_candidate_counts, 0.5),
            "added_candidate_count_p95": _percentile(added_candidate_counts, 0.95),
            "added_candidate_count_average": (
                sum(added_candidate_counts) / len(added_candidate_counts) if added_candidate_counts else None
            ),
        },
        "coverage_direction_counts": dict(sorted(coverage_direction_counts.items())),
        "verdict_coverage_transitions": {
            key: {"count": count, "case_ids": transition_case_ids[key]}
            for key, count in sorted(transition_counts.items())
        },
        "automatic_attribution": {
            key: {"count": count, "case_ids": attribution_case_ids[key]}
            for key, count in sorted(attribution_counts.items())
        },
        "system_errors": system_errors,
        "manual_review_queue": {
            "count": len(manual_queue),
            "reason_counts": dict(sorted(manual_reason_counts.items())),
        },
        "latencies": latencies,
        "interpretation_boundary": (
            "All counts are analysis-only targeted results. Automatic pass plus coverage gain is an attribution candidate, "
            "not a final human-confirmed optimization gain."
        ),
    }


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _seconds(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f} s"


def _markdown(summary: dict[str, Any]) -> str:
    before = summary["baseline_metrics"]
    after = summary["evaluation_metrics"]
    delta = summary["metric_deltas"]
    execution = summary["execution"]
    expansion = summary["expansion"]
    attribution = summary["automatic_attribution"]
    lines = [
        "# 相邻 L3 扩展影响审计",
        "",
        f"范围：`{summary['evaluation_id']}`，只比较冻结的 {summary['case_count']} 道 analysis 非通过题。",
        "这不是 500 题总体结果，也不涉及 validation。自动通过和证据变化必须经人工复核后才能称为优化收益。",
        "",
        "## 执行完整性",
        "",
        f"- 相邻 L3 扩展实际执行：{expansion['applied_case_count']}/{summary['case_count']}。",
        f"- 保留原成功题：{execution['preserved_case_count']}；10 并发补跑：{execution['retry_attempt_completed_case_count']}/{execution['retry_case_count']}；补跑题级 evaluation_error：{execution['retry_attempt_evaluation_error_count']}。",
        f"- 本轮墙钟耗时：{_seconds(execution['elapsed_seconds'])}；worker 数：{execution['evaluation_worker_count']}。",
        f"- 每题新增相邻候选：P50 {expansion['added_candidate_count_p50']}，P95 {expansion['added_candidate_count_p95']}，平均 {expansion['added_candidate_count_average']:.2f}。",
        "",
        "## 指标变化",
        "",
        "| 指标 | 基线（147 道） | 相邻 L3 扩展 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        f"| 证据全覆盖率 | {_percent(before['evidence_full_coverage_rate'])} | {_percent(after['evidence_full_coverage_rate'])} | {_percent(delta['evidence_full_coverage_rate'])} |",
        f"| 平均证据覆盖率 | {_percent(before['evidence_average_coverage_rate'])} | {_percent(after['evidence_average_coverage_rate'])} | {_percent(delta['evidence_average_coverage_rate'])} |",
        f"| 自动回答通过率 | {_percent(before['answer_pass_rate'])} | {_percent(after['answer_pass_rate'])} | {_percent(delta['answer_pass_rate'])} |",
        f"| 证据完全未命中 | {before['evidence_zero_coverage_count']} | {after['evidence_zero_coverage_count']} | {after['evidence_zero_coverage_count'] - before['evidence_zero_coverage_count']} |",
        "",
        "## 自动归因候选",
        "",
        f"- 证据覆盖提升且自动通过：{attribution.get('evidence_recovered_and_answer_pass_candidate', {}).get('count', 0)} 道。这是最接近“相邻材料确实补回并帮助回答”的候选，但仍要抽查答案和阶段链路。",
        f"- 自动通过但证据覆盖未提升：{attribution.get('answer_pass_without_evidence_coverage_gain', {}).get('count', 0)} 道。它们不能直接归功于相邻扩展，可能是回答生成或判卷波动。",
        f"- 证据覆盖提升但仍未自动通过：{attribution.get('evidence_recovered_but_answer_not_pass', {}).get('count', 0)} 道。材料改善了，但回答组织、答案准确性或判卷仍是问题。",
        f"- 证据覆盖下降：{attribution.get('evidence_coverage_regressed', {}).get('count', 0)} 道。需要复核是否在 Auto-merging/Rerank/最终 top-8 阶段丢失材料。",
        "",
        "## 人工复核边界",
        "",
        f"- 人工复核队列：{summary['manual_review_queue']['count']} 道；原因分布：{json.dumps(summary['manual_review_queue']['reason_counts'], ensure_ascii=False)}。",
        f"- 系统异常：{len(summary['system_errors'])} 道，不能算作分块或回答质量结论。",
        "- 所有逐题 case ID、候选阶段快照、回答和判卷均保存在同目录 `results.jsonl`、`candidate-audit.md`、`case-review.md` 与 `manual-review.jsonl`。",
        "",
        "## 延迟",
        "",
        "| 阶段 | P50 | P95 |",
        "| --- | ---: | ---: |",
    ]
    for key, label in (
        ("rag_seconds", "RAG"),
        ("generation_seconds", "回答生成"),
        ("judge_seconds", "独立判卷"),
        ("end_to_end_seconds", "端到端"),
    ):
        latency = summary["latencies"][key]
        lines.append(f"| {label} | {_seconds(latency['p50_seconds'])} | {_seconds(latency['p95_seconds'])} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize an adjacent-L3 targeted evaluation without rerunning RAG.")
    parser.add_argument("--baseline-results", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    parser.add_argument("--target-manifest", type=Path, required=True)
    args = parser.parse_args()
    summary = build_summary(
        baseline_results=args.baseline_results.resolve(),
        evaluation_dir=args.evaluation_dir.resolve(),
        target_manifest=args.target_manifest.resolve(),
    )
    output_dir = args.evaluation_dir.resolve()
    (output_dir / "adjacent-l3-impact-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "adjacent-l3-impact-summary.md").write_text(
        _markdown(summary), encoding="utf-8", newline="\n"
    )
    print(output_dir / "adjacent-l3-impact-summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
