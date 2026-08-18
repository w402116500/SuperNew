"""Create an auditable pre-review for the structured-chunking RAG run."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


DECISIONS = {
    "qst_0037": ("evidence_complete_answer_failure", "目标文档已进入最终上下文，证据覆盖达到 1.0；回答仍漏掉 hedged retries、prefetch.window_size 和 pthread/SIGABRT 细节，属于回答整合失败，不归因于分块。"),
    "qst_0136": ("evidence_complete_answer_failure", "目标文档已进入最终上下文，证据覆盖达到 1.0；回答把客户和临时措施说错，属于证据使用/回答失败，不归因于分块。"),
    "qst_0227": ("evidence_complete_answer_failure", "目标文档已进入最终上下文，证据覆盖达到 1.0；回答抓到部分网络现象但漏掉 KV cache 内存压力根因，属于回答整合失败。"),
    "qst_0308": ("evidence_complete_answer_failure", "目标文档已进入最终上下文，证据覆盖达到 1.0；混合网络方案基本覆盖，但会议日期缺少 2026 年，自动判卷仍为 fail，需人工确认严格性。"),
    "qst_0023": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行中目标文档从初始候选到最终上下文均出现，覆盖从 0.0 到 1.0，回答与参考事实一致；这是最明确的收益候选。"),
    "qst_0142": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行保留 Hosted/Reserved-Dedicated 指标材料到最终上下文，覆盖从 0.0 到 1.0，回答列出 uptime 与 latency 目标；待人工确认是否有多余表述影响精确性。"),
    "qst_0189": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行把 origins、origin-mapper、cosign 校验和 enforce 开关材料带入最终上下文，覆盖从 0.0 到 1.0，回答与参考答案一致。"),
    "qst_0312": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行把 Confluence 日期和路径带入最终上下文，覆盖从 0.0 到 1.0，回答与参考答案一致。"),
    "qst_0331": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行将 rollout 和 Dedicated 合同事实带入最终上下文，覆盖从 0.0 到 1.0，回答与参考答案一致。"),
    "qst_0335": ("evidence_recovered_answer_pass_candidate", "基线未命中，结构化运行将 sponsor 和最终会议时间带入最终上下文，覆盖从 0.0 到 1.0；中文输出存在显示乱码，事实需人工确认。"),
    "qst_0423": ("evidence_recovered_answer_pass_candidate", "基线只覆盖 0.5，结构化运行补齐 12 个月事实并通过自动判卷；回答未提及已被 supersede 的 18 个月旧建议，需人工确认问题是否要求该限定。"),
    "qst_0092": ("no_measurable_chunking_gain", "目标文档没有进入新运行原始候选，结构化分块没有解决该题的召回缺口。"),
    "qst_0193": ("no_measurable_chunking_gain", "目标文档虽进入最终上下文，但证据覆盖仍为 0.0，关键事实没有被补回。"),
    "qst_0231": ("no_measurable_chunking_gain", "目标文档没有进入原始候选，覆盖仍为 0.0。"),
    "qst_0233": ("no_measurable_chunking_gain", "目标文档只出现在初始候选，后续 raw/final 阶段消失，覆盖仍为 0.0；问题在后续筛选链路或材料定位。"),
    "qst_0241": ("no_measurable_chunking_gain", "基线已经完整覆盖，结构化运行保持 1.0，不能把无变化算作优化收益。"),
    "qst_0247": ("no_measurable_chunking_gain", "目标文档进入最终上下文但覆盖仍为 0.0，关键事实不完整。"),
    "qst_0265": ("no_measurable_chunking_gain", "基线和结构化运行均通过且覆盖为 1.0；本题没有可归因的增益，另有旧块定位歧义。"),
    "qst_0275": ("no_measurable_chunking_gain", "目标文档没有进入新运行候选，覆盖仍为 0.0。"),
    "qst_0295": ("no_measurable_chunking_gain", "目标文档进入 raw/post-merge，但未进入最终上下文，覆盖仍为 0.0，说明后续筛选仍丢材料。"),
    "qst_0300": ("no_measurable_chunking_gain", "目标文档只在初始候选出现，最终上下文未保留，表格结构修复本题没有转化成最终证据收益。"),
    "qst_0320": ("no_measurable_chunking_gain", "目标文档进入最终上下文但覆盖仍为 0.0，关键商业条款未被判为完整证据。"),
    "qst_0100": ("system_error_timeout", "单题超过 600 秒总时限，无法判断分块或回答质量。"),
    "qst_0115": ("system_error_timeout", "单题超过 600 秒总时限；基线曾通过，但本次没有可比较的结构化结果。"),
    "qst_0120": ("system_error_generation_timeout", "检索已返回证据，但单次模型回答请求超过 90 秒；没有生成答案，不能判断分块或回答质量。"),
    "qst_0133": ("system_error_generation_timeout", "检索已返回证据，但单次模型回答请求超过 90 秒；没有生成答案，不能判断分块或回答质量。"),
    "qst_0221": ("system_error_generation_timeout", "本次运行在模型回答请求阶段超时，未形成可判定答案；不能判断分块或回答质量。"),
    "qst_0345": ("system_error_timeout", "单题超过 600 秒总时限，无法判断分块或回答质量。"),
    "qst_0356": ("evidence_recovered_but_answer_not_pass", "基线证据覆盖 0.25，结构化运行提升到 0.75，但回答仍 fail；材料可能补回，回答整合仍失败，不能计为答案收益。"),
    "qst_0366": ("evidence_coverage_regressed", "基线证据覆盖约 0.78，结构化运行降到约 0.44，回答仍 fail；这是证据退化，不能计为分块收益，需要复核后续筛选链路。"),
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_records(evaluation_dir: Path) -> list[dict[str, Any]]:
    summary = _read_json(evaluation_dir / "structured-chunking-impact-summary.json")
    records = []
    for case in summary["cases"]:
        case_id = str(case["case_id"])
        if case_id not in DECISIONS:
            raise ValueError(f"缺少人工预复核结论：{case_id}")
        classification, conclusion = DECISIONS[case_id]
        records.append({
            "review_version": "structured-chunking-rag-manual-review-v1",
            "review_status": "assistant_pre_review_pending_human_confirmation",
            "case_id": case_id,
            "case_set": "analysis",
            "evaluation_id": summary["evaluation_id"],
            "classification": classification,
            "baseline": case["baseline"],
            "structured": case["structured"],
            "coverage_direction": case["coverage_direction"],
            "automatic_attribution": case["automatic_attribution"],
            "pre_review_conclusion": conclusion,
        })
    return records


def render_markdown(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    counts = Counter(record["classification"] for record in records)
    lines = [
        "# 结构化分块真实 RAG 人工预复核",
        "",
        f"范围：`{summary['evaluation_id']}`，冻结的 30 道 `analysis` 题；不含 validation。",
        "这里是基于逐题候选 trace、证据覆盖、回答和自动判卷的人工预复核草稿，状态为待人工最终确认；系统超时不计入优化收益。",
        "",
        "## 分类汇总",
        "",
        "| 分类 | 数量 | 说明 |",
        "| --- | ---: | --- |",
        f"| `evidence_recovered_answer_pass_candidate` | {counts['evidence_recovered_answer_pass_candidate']} | 证据覆盖提升且回答通过候选，需人工确认因果 |",
        f"| `evidence_recovered_but_answer_not_pass` | {counts.get('evidence_recovered_but_answer_not_pass', 0)} | 证据覆盖提升但回答仍未通过，不能直接计为收益 |",
        f"| `evidence_complete_answer_failure` | {counts['evidence_complete_answer_failure']} | 材料已完整但回答仍错，优先归为回答层问题 |",
        f"| `no_measurable_chunking_gain` | {counts['no_measurable_chunking_gain']} | 没有可测证据覆盖增益，不能计入分块收益 |",
        f"| `evidence_coverage_regressed` | {counts.get('evidence_coverage_regressed', 0)} | 新分块后的证据覆盖下降，属于退化候选 |",
        f"| `baseline_system_error_no_comparison` | {counts.get('baseline_system_error_no_comparison', 0)} | 基线本身超时，重试结果只能作补充观察，不能做前后对比 |",
        f"| `system_error_timeout` | {counts['system_error_timeout']} | 超过 600 秒，不能做质量结论 |",
        f"| `system_error_generation_timeout` | {counts.get('system_error_generation_timeout', 0)} | 单次模型请求超过 90 秒，不能做质量结论 |",
        "",
        "## 逐题预复核",
        "",
        "| Case | 分类 | 基线覆盖/判定 | 新分块覆盖/判定 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in records:
        before = record["baseline"]
        after = record["structured"]
        lines.append(
            f"| `{record['case_id']}` | `{record['classification']}` | "
            f"{before['evidence_coverage']} / {before['verdict']} | "
            f"{after['evidence_coverage']} / {after['verdict']} | {record['pre_review_conclusion']} |"
        )
    lines.extend([
        "",
        "## 结论边界",
        "",
        "- 7 道题是“证据补回且回答变对”的候选，不等于已经证明分块因果；需人工逐题确认关键事实确实来自新分块材料。",
        f"- {counts.get('evidence_recovered_but_answer_not_pass', 0)} 道题证据覆盖提升但回答仍未通过，材料改善没有转化为答案收益。",
        "- 4 道题证明“文档进入并不等于回答正确”：材料覆盖已到 1.0，但回答仍漏事实、答错实体或缺少必要限定。",
        f"- {counts['system_error_timeout']} 道题超过单题 600 秒总时限，{counts.get('system_error_generation_timeout', 0)} 道题的单次模型请求超过 90 秒；这些系统异常不能算作分块失败。",
        f"- {counts.get('evidence_coverage_regressed', 0)} 道题出现证据覆盖下降，不能计为优化收益，需检查后续筛选链路。",
        "- 本预复核不能外推到 500 题，也不能代表 validation 200 题；T9/T10 均未被本轮改变。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize structured-chunking RAG pre-review without rerunning RAG.")
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    args = parser.parse_args()
    evaluation_dir = args.evaluation_dir.resolve()
    summary = _read_json(evaluation_dir / "structured-chunking-impact-summary.json")
    records = build_records(evaluation_dir)
    (evaluation_dir / "structured-chunking-manual-review.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    (evaluation_dir / "structured-chunking-manual-review.md").write_text(
        render_markdown(records, summary), encoding="utf-8", newline="\n"
    )
    print(evaluation_dir / "structured-chunking-manual-review.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
