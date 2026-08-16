"""Create an auditable, analysis-set-only failure classification report."""

from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any


CATEGORY_META = OrderedDict([
    ("retrieval_miss", ("未召回标准证据", "标准证据列表非空，但召回排名为空。")),
    ("retrieval_late", ("证据召回但排序靠后", "标准证据已召回，但最前证据排名大于 3。")),
    ("hard_distractor", ("标题相近干扰误排", "Representative 不含题级 hard distractor；本轮该类应为 0。")),
    ("evidence_incomplete", ("证据不完整或上下文组织失败", "有部分证据或覆盖率低于 1；包括回答通过但证据不完整的题。")),
    ("answer_failure", ("回答事实遗漏、扩展或编造", "证据覆盖完整，但独立判卷为 fail。")),
    ("refusal_failure", ("正确拒答失败", "info_not_found 题没有得到正确拒答。")),
    ("human_review", ("自动判卷不确定，待人工复核", "独立判卷为 review 且没有 API、生成或判卷错误字段。")),
    ("system_error", ("判卷异常、API 异常或超时", "存在 evaluation_error、回答生成异常或判卷请求/解析异常。")),
])


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _analysis_ids(split_path: Path) -> set[str]:
    split = json.loads(split_path.read_text(encoding="utf-8"))
    return {str(case_id) for case_id in split.get("analysis_case_ids", [])}


def _ranks(record: dict[str, Any]) -> list[int]:
    ranks = record.get("evidence_ranks") or []
    return sorted(int(rank) for rank in ranks if str(rank).isdigit())


def _classify(record: dict[str, Any]) -> set[str]:
    categories: set[str] = set()
    grade = record.get("answer_grade") or {}
    verdict = str(grade.get("verdict") or "review").lower()
    evaluation_error = str(record.get("evaluation_error") or "")
    answer_error = str(record.get("answer_generation_error") or "")
    grader_error = str(grade.get("grader_error") or "")

    has_system_error = bool(evaluation_error or answer_error or grader_error)
    if has_system_error:
        categories.add("system_error")
    elif verdict == "review":
        categories.add("human_review")

    if record.get("question_type") == "info_not_found" and (
        record.get("null_refusal_correct") is not True or verdict != "pass"
    ):
        categories.add("refusal_failure")

    expected = record.get("expected_evidence_filenames") or []
    ranks = _ranks(record)
    coverage = record.get("evidence_coverage")
    if expected and not ranks:
        categories.add("retrieval_miss")
    elif ranks and min(ranks) > 3:
        categories.add("retrieval_late")

    if expected and coverage is not None and 0 < float(coverage) < 1:
        categories.add("evidence_incomplete")

    if verdict == "fail" and not evaluation_error and not answer_error and not grader_error:
        categories.add("answer_failure")

    return categories


def _sample(record: dict[str, Any], categories: set[str]) -> dict[str, Any]:
    grade = record.get("answer_grade") or {}
    return {
        "case_id": record.get("case_id"),
        "question_type": record.get("question_type"),
        "categories": sorted(categories),
        "question": record.get("question", ""),
        "reference_answer": record.get("reference_answer", ""),
        "answer": record.get("answer", ""),
        "expected_evidence_filenames": record.get("expected_evidence_filenames", []),
        "retrieved_filenames": record.get("retrieved_filenames", []),
        "evidence_ranks": record.get("evidence_ranks", []),
        "evidence_coverage": record.get("evidence_coverage"),
        "verdict": grade.get("verdict"),
        "grade_reason": grade.get("reason", ""),
        "evaluation_error": record.get("evaluation_error", ""),
    }


def build_report(results_path: Path, split_path: Path) -> tuple[dict[str, Any], str]:
    analysis_ids = _analysis_ids(split_path)
    records = [
        record
        for record in _load_jsonl(results_path)
        if str(record.get("case_id")) in analysis_ids
    ]
    by_category: dict[str, list[dict[str, Any]]] = {
        key: [] for key in CATEGORY_META
    }
    for record in records:
        sample = _sample(record, _classify(record))
        for category in sample["categories"]:
            by_category[category].append(sample)

    verdict_counts = Counter(
        str((record.get("answer_grade") or {}).get("verdict") or "review")
        for record in records
    )
    summary = {
        "source_results": str(results_path),
        "source_case_split": str(split_path),
        "case_set": "analysis",
        "analysis_case_count": len(records),
        "expected_analysis_case_count": 300,
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "category_counts": {key: len(items) for key, items in by_category.items()},
        "categories": {
            key: {
                "label": label,
                "automatic_rule": rule,
                "case_ids": [str(item["case_id"]) for item in by_category[key]],
                "samples": by_category[key][:3],
            }
            for key, (label, rule) in CATEGORY_META.items()
        },
        "classification_status": "automatic_triage_pending_human_review",
    }

    lines = [
        "# T8：Representative 分析集失败分类",
        "",
        "本报告只读取固定 300 道 analysis 题；validation 题不展示题目、答案或失败详情。",
        "分类是基于逐题 JSONL 的自动初筛，不能替代人工复核，也不构成 RAG 优化结论。",
        "",
        "## 范围与判卷概览",
        "",
        f"- 分析题数：{len(records)} / 预期 300",
        f"- 自动判卷：{json.dumps(dict(sorted(verdict_counts.items())), ensure_ascii=False)}",
        "- 语料：Representative；本轮不含题级 hard distractor，因此标题相近干扰类预期为 0。",
        "",
        "## 分类结果",
        "",
        "| 类别 | 影响题数 | 自动规则 | 代表样本 |",
        "| --- | ---: | --- | --- |",
    ]
    for key, (label, rule) in CATEGORY_META.items():
        items = by_category[key]
        sample_ids = ", ".join(str(item["case_id"]) for item in items[:5]) or "无"
        lines.append(f"| {label} ({key}) | {len(items)} | {rule} | {sample_ids} |")

    lines.extend(["", "## 代表样本", ""])
    for key, (label, rule) in CATEGORY_META.items():
        items = by_category[key]
        lines.extend([f"### {label}", "", f"自动规则：{rule}", ""])
        if not items:
            lines.extend(["本轮没有自动归入该类别的分析题。", ""])
            continue
        for item in items[:3]:
            question = str(item.get("question") or "").replace("\n", " ")[:300]
            answer = str(item.get("answer") or "").replace("\n", " ")[:300]
            reason = str(item.get("grade_reason") or "").replace("\n", " ")[:300]
            lines.extend([
                f"- {item['case_id']}（{item.get('question_type') or 'unknown'}，判卷 {item.get('verdict')}，覆盖率 {item.get('evidence_coverage')}）",
                f"  - 问题：{question}",
                f"  - 回答：{answer}",
                f"  - 判卷理由：{reason}",
                f"  - 标准证据排名：{item.get('evidence_ranks') or '未召回'}",
            ])
        lines.append("")

    lines.extend([
        "## 解释边界与下一步",
        "",
        "同一题可以同时落入多个类别，例如证据不完整且回答被判 fail；类别计数不是互斥总分。",
        "系统异常、自动判卷不确定、证据完整但回答失败、回答通过但证据不完整都必须人工复核。",
        "下一轮优化变量必须由人工确认的失败根因决定；在此之前不修改 Embedding、top-k、Rerank、提示词或分块。",
    ])
    return summary, "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--case-split", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary, report = build_report(args.results, args.case_split)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "analysis-failure-classification.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "analysis-failure-classification.md").write_text(
        report,
        encoding="utf-8",
    )
    print(args.output_dir / "analysis-failure-classification.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
