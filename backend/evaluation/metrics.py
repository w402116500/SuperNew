"""RAG 离线评测的纯函数指标，方便以固定样本单元测试。"""

from __future__ import annotations

from math import ceil
from typing import Any, Iterable


def percentile(values: Iterable[float], ratio: float) -> float | None:
    """计算最近秩百分位；空输入返回 None，避免伪造 0 延迟。"""
    items = sorted(float(value) for value in values)
    if not items:
        return None
    return items[max(0, ceil(len(items) * ratio) - 1)]


def retrieval_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """计算单标准文档或多标准文档均可使用的检索排序指标。"""
    total = len(results)
    recalls = {k: 0 for k in (1, 3, 5)}
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    failures = 0
    fallbacks = 0
    for item in results:
        ranks = [rank for rank in item.get("relevant_ranks", []) if isinstance(rank, int)]
        for k in recalls:
            if any(rank <= k for rank in ranks):
                recalls[k] += 1
        reciprocal_ranks.append(1 / min(ranks) if ranks and min(ranks) <= 10 else 0.0)
        if item.get("retrieval_seconds") is not None:
            latencies.append(float(item["retrieval_seconds"]))
        failures += int(item.get("retrieval_mode") == "failed")
        fallbacks += int(item.get("retrieval_mode") == "dense_fallback")
    return {
        "case_count": total,
        **{f"recall_at_{k}": recalls[k] / total if total else None for k in recalls},
        "mrr_at_10": sum(reciprocal_ranks) / total if total else None,
        "retrieval_latency_p50_seconds": percentile(latencies, 0.5),
        "retrieval_latency_p95_seconds": percentile(latencies, 0.95),
        "failed_cases": failures,
        "dense_fallback_cases": fallbacks,
    }


def _effective_evidence_coverage(item: dict[str, Any]) -> float:
    """修正可回答题缺少标准证据时的默认覆盖率。"""
    expected = item.get("expected_evidence_filenames")
    if expected == [] and item.get("question_type") not in {"null", "null_query", "info_not_found"}:
        return 0.0
    return float(item.get("evidence_coverage", 0.0))


def multihop_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合多证据、回答、拒答和 RAG 过程的可解释指标。"""
    total = len(results)
    evidence_coverages = [_effective_evidence_coverage(item) for item in results]
    # MultiHopRAG 使用 null，EnterpriseRAG 使用 info_not_found；两者都表示无答案题。
    refusal_types = {"null", "null_query", "info_not_found"}
    non_null = [item for item in results if item.get("question_type") not in refusal_types]
    null_cases = [item for item in results if item.get("question_type") in refusal_types]
    grades = [item.get("answer_grade", {}).get("verdict") for item in results]
    traces = [item.get("rag_trace") or {} for item in results]
    review_cases = sum(grade == "review" for grade in grades)
    answer_judged = sum(grade in {"pass", "fail", "review"} for grade in grades)
    return {
        "case_count": total,
        "evidence_full_coverage_rate": (
            sum(_effective_evidence_coverage(item) == 1.0 for item in non_null) / len(non_null)
            if non_null else None
        ),
        "evidence_average_coverage_rate": sum(evidence_coverages) / total if total else None,
        "answer_pass_rate": sum(grade == "pass" for grade in grades) / answer_judged if answer_judged else None,
        "null_refusal_correct_rate": (
            sum(bool(item.get("null_refusal_correct")) for item in null_cases) / len(null_cases)
            if null_cases else None
        ),
        "manual_review_rate": review_cases / total if total else None,
        "end_to_end_latency_p50_seconds": percentile([item.get("end_to_end_seconds", 0) for item in results], 0.5),
        "rag_latency_p50_seconds": percentile([item.get("rag_seconds", 0) for item in results], 0.5),
        "generation_latency_p50_seconds": percentile([item.get("generation_seconds", 0) for item in results], 0.5),
        "rewrite_trigger_rate": sum(bool(trace.get("rewrite_method")) for trace in traces) / total if total else None,
        "sub_question_trigger_rate": sum(bool(trace.get("sub_agent_count")) for trace in traces) / total if total else None,
        "auto_merge_trigger_rate": sum(bool(trace.get("auto_merge_applied")) for trace in traces) / total if total else None,
    }
