"""Write the human review for the offline structured-chunking audit."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


EXPECTED_CASE_COUNT = 30

# These conclusions are about the old/new chunk boundary, not RAG quality.  No
# model or retrieval result is available in the offline audit, so attribution
# remains deliberately conservative.
MANUAL_DECISIONS = {
    "qst_0023": ("meaningful_structural_boundary", "high", "关键事实位于 `## Review, launch gating, and update cadence` 下。新分块把前置对齐内容与 Review 章节分开，并把 reviewer 列表留在 Review 章节内，标题路径有明确审计价值；是否能被检索到仍不能由离线审计判断。"),
    "qst_0037": ("meaningful_structural_boundary", "high", "mitigation 部分被新块识别为连续列表，旧块把列表边界切在中间。新的列表原子保持完整，但同一文档仍只有一级标题，远距离材料是否更容易召回尚未证明。"),
    "qst_0092": ("unchanged_boundary", "high", "关键旧 L3 的原文范围在新 L3 中完全保留，只多出一个重叠块；没有新的标题层级或结构原子信号，不能把它算成分块收益。"),
    "qst_0100": ("meaningful_structural_boundary", "high", "热修复命令所在的围栏代码被完整识别为 `code`，没有把围栏从中间切断；这是明确的结构保护，但仍未验证检索排序和回答。"),
    "qst_0115": ("changed_without_clear_structural_gain", "medium", "边界重新排列并覆盖了原始材料，但所有关键块仍属于同一一级标题下的 mixed 内容，没有出现更细标题或列表/代码原子，结构收益不明确。"),
    "qst_0120": ("changed_without_clear_structural_gain", "medium", "新块重新切分了同一段客户沟通，SOC 2、subprocessor 和 DPA 文本均有重叠映射；文档没有更深标题，变化主要是长度边界调整。"),
    "qst_0133": ("changed_without_clear_structural_gain", "medium", "阈值材料被两个普通 paragraph 块覆盖，边界发生变化但没有标题、列表或表格结构可利用，不能直接认为更利于召回。"),
    "qst_0136": ("unchanged_boundary", "high", "两个关键旧 L3 都与新 L3 原文范围完全一致；新块只补充标题路径和 metadata，没有改变关键材料边界。"),
    "qst_0142": ("meaningful_structural_boundary", "high", "Hosted/Dedicated 指标位于连续列表中，新块明确标记为 `list` 并保持列表整体；这是比旧递归边界更清晰的结构单元。"),
    "qst_0189": ("changed_without_clear_structural_gain", "medium", "origin-mapper 和签名信息被多个 paragraph 块覆盖，但文档只有一级标题，变化没有形成可验证的章节级收益。"),
    "qst_0193": ("changed_without_clear_structural_gain", "medium", "standby assignment 被多个普通 paragraph 重叠覆盖，边界更细但没有标题或列表结构，不能据此判断召回会改善。"),
    "qst_0221": ("changed_without_clear_structural_gain", "medium", "SLA 数值被新块覆盖，但新块仍是 mixed，编号段落没有形成可独立验证的标题结构；材料保留，结构收益不明确。"),
    "qst_0227": ("changed_without_clear_structural_gain", "medium", "根因和机制文本从旧块映射到两个 paragraph 块，仍处于单一一级标题下；没有新增结构边界证据。"),
    "qst_0231": ("changed_without_clear_structural_gain", "medium", "12 个月保留承诺被两个 paragraph 块覆盖，边界变化只体现为长度重新分配，没有更深标题或受保护原子。"),
    "qst_0233": ("changed_without_clear_structural_gain", "medium", "演练标准附近包含列表内容，但新块标记为 mixed，未能证明列表作为独立原子被保留；只能确认材料没有从映射中消失。"),
    "qst_0241": ("unchanged_boundary", "high", "四个关键旧 L3 中三个与新块完全同范围，只有一个发生边界调整；主要事实仍在原位置，不能算明确分块收益。"),
    "qst_0247": ("changed_without_clear_structural_gain", "medium", "交付承诺被两个普通 paragraph 块覆盖，虽然边界变化，但文档没有更深标题或特殊结构。"),
    "qst_0265": ("audit_ambiguity", "medium", "旧 L3#8 和 L3#9 被定位到完全相同的原文范围，说明旧分块审计存在重复文本定位歧义。新块完整覆盖该段，但无法仅凭此记录比较两个旧块的真实边界变化。"),
    "qst_0275": ("unchanged_boundary", "high", "五个关键旧 L3 全部与新 L3 原文范围一致；新策略增加标题 metadata，但没有改变关键材料边界。"),
    "qst_0295": ("unchanged_boundary", "high", "回滚事实的关键范围在新块中完全保留，额外重叠来自相邻范围；没有明确结构性变化。"),
    "qst_0300": ("meaningful_structural_boundary", "high", "修复无首尾管道符的 Markdown 表格识别后，价格/性能表作为一个 `table` 原子保留了表头和数据行；这是本轮明确的结构修复，修复前结果不再作为结论。"),
    "qst_0308": ("changed_without_clear_structural_gain", "medium", "后续会议时间被多个普通 paragraph 覆盖，边界发生变化但没有更深标题或列表原子，收益无法从离线结果推出。"),
    "qst_0312": ("unchanged_boundary", "high", "关键 action-item 块与新块完全同范围；虽然内容含列表，但本题边界没有改变。"),
    "qst_0320": ("changed_without_clear_structural_gain", "medium", "商业条款被多个 paragraph 块覆盖，跨越关系被保留，但文档没有更深标题或独立结构原子。"),
    "qst_0331": ("unchanged_boundary", "high", "Dedicated 合同细节的关键旧 L3 与新 L3 完全同范围；只增加标题路径和 metadata。"),
    "qst_0335": ("changed_without_clear_structural_gain", "medium", "最终 briefing 时间被两个 paragraph 块覆盖，边界变化未形成章节级结构收益。"),
    "qst_0345": ("meaningful_structural_boundary", "high", "证据包内容、共享规则和请求流程被拆到对应的二级/三级标题路径，列表也被保留为结构块；这是 30 题中最明确的章节结构收益之一。"),
    "qst_0356": ("meaningful_structural_boundary", "high", "`capacity_fleet.*` 子类型位于明确的三级标题下，新块保留该标题路径并把子类型段落作为 mixed 结构块；关键枚举不再只依赖全文位置。"),
    "qst_0366": ("meaningful_structural_boundary", "high", "隔离检查和诊断结论覆盖了多个新块，其中 admission 指标和客户摘要列表被明确识别为 list；结构边界比旧递归块更可解释。"),
    "qst_0423": ("changed_without_clear_structural_gain", "medium", "更新计划和 12 个月规则被两个 mixed 块覆盖，材料仍在，但没有更深标题或独立列表原子，结构收益不明确。"),
}


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _mapping(record: dict) -> list[dict]:
    output = []
    for old in record.get("old_evidence_leaves") or []:
        overlaps = []
        for new in old.get("new_overlaps") or []:
            overlaps.append({
                "chunk_id": new.get("chunk_id"),
                "source_start_index": new.get("source_start_index"),
                "source_end_index": new.get("source_end_index"),
                "heading_path": new.get("heading_path"),
                "content_kind": new.get("content_kind"),
            })
        exact = sum(
            int(item.get("source_start_index", -1)) == int(old.get("source_start_index", -2))
            and int(item.get("source_end_index", -1)) == int(old.get("source_end_index", -2))
            for item in overlaps
        )
        output.append({
            "old_l3_index": old.get("l3_index"),
            "old_chunk_id": old.get("chunk_id"),
            "old_source_start_index": old.get("source_start_index"),
            "old_source_end_index": old.get("source_end_index"),
            "exact_new_span_count": exact,
            "new_overlaps": overlaps,
        })
    return output


def build_review(audit_path: Path) -> list[dict]:
    source = _read_jsonl(audit_path)
    if len(source) != EXPECTED_CASE_COUNT or any(item.get("case_set") != "analysis" for item in source):
        raise ValueError("结构化分块人工复核必须恰好包含 30 道 analysis 题")
    case_ids = {str(item.get("case_id")) for item in source}
    if case_ids != set(MANUAL_DECISIONS):
        raise ValueError("人工结论表与冻结的 30 道题不一致")
    records = []
    for audit in source:
        case_id = str(audit["case_id"])
        classification, confidence, conclusion = MANUAL_DECISIONS[case_id]
        overlaps = [
            item
            for old in audit.get("old_evidence_leaves") or []
            for item in old.get("new_overlaps") or []
        ]
        exact_count = sum(
            int(item.get("source_start_index", -1)) == int(old.get("source_start_index", -2))
            and int(item.get("source_end_index", -1)) == int(old.get("source_end_index", -2))
            for old in audit.get("old_evidence_leaves") or []
            for item in old.get("new_overlaps") or []
        )
        records.append({
            "review_version": "structured-chunking-manual-review-v1",
            "case_id": case_id,
            "case_set": "analysis",
            "evaluation_id": "structured-chunking-offline-audit-002",
            "source_ref": audit.get("source_ref"),
            "stable_filename": audit.get("stable_filename"),
            "baseline": {
                "evidence_coverage": audit.get("baseline_evidence_coverage"),
                "answer_verdict": audit.get("baseline_answer_verdict"),
            },
            "chunk_comparison": {
                "old_leaf_count": audit.get("old_leaf_count"),
                "new_leaf_count": audit.get("new_leaf_count"),
                "old_evidence_leaf_count": len(audit.get("old_evidence_leaves") or []),
                "new_overlap_count": len(overlaps),
                "exact_new_span_count": exact_count,
                "heading_paths": audit.get("new_overlap_heading_paths") or [],
                "content_kinds": audit.get("new_content_kinds") or {},
                "mapping": _mapping(audit),
            },
            "classification": classification,
            "attribution": "not_determinable_without_real_rag",
            "confidence": confidence,
            "conclusion": conclusion,
        })
    return records


def _markdown(records: list[dict]) -> str:
    counts = Counter(str(record["classification"]) for record in records)
    lines = [
        "# 结构化分块离线审计人工复核",
        "",
        "范围：`structured-chunking-offline-audit-002` 冻结的 30 道 `analysis` 题。",
        "本复核只检查旧/新分块的原文范围、标题路径和结构类型；没有调用模型、Embedding、Milvus、PostgreSQL 或 Redis。",
        "因此这里的‘有结构收益’不等于回答通过率提升，真实收益必须由后续受控 RAG 对照验证。",
        "",
        "## 总结",
        "",
        "| 人工分类 | 数量 |",
        "| --- | ---: |",
    ]
    for key in (
        "meaningful_structural_boundary",
        "unchanged_boundary",
        "changed_without_clear_structural_gain",
        "audit_ambiguity",
    ):
        lines.append(f"| `{key}` | {counts.get(key, 0)} |")
    lines.extend([
        "",
        "人工结论：30 道题的关键旧材料都能映射到新块；其中 8 道出现明确的标题、列表、代码或表格结构信号，7 道关键边界基本不变，14 道只是普通文本边界重新分配，1 道存在旧块重复定位歧义。",
        "`qst_0300` 的表格识别在本轮复核中发现并修复：无首尾管道符的表格现在会作为 `table` 原子保留，离线审计已在修复后重新生成。",
        "不能从本报告得出召回、证据覆盖率或回答通过率提升；下一步真实评测仍只运行这 30 道 analysis 题。",
        "",
        "## 逐题结论",
        "",
        "| Case | 基线 | 关键旧 L3 | 新重叠 | 精确范围匹配 | 人工分类 | 结论 |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ])
    for record in records:
        baseline = record["baseline"]
        comparison = record["chunk_comparison"]
        lines.append(
            f"| `{record['case_id']}` | coverage={baseline['evidence_coverage']} / {baseline['answer_verdict']} | "
            f"{comparison['old_evidence_leaf_count']} | {comparison['new_overlap_count']} | "
            f"{comparison['exact_new_span_count']} | `{record['classification']}` | {record['conclusion']} |"
        )
    lines.extend([
        "",
        "## 后续判定",
        "",
        "- 可以进入下一步受控真实对照，但不能把离线结构变化当成收益证明。",
        "- 真实运行前仍必须使用新的 PostgreSQL 数据库、新 Milvus collection、新 Redis namespace、新 corpus/evaluation ID，以及 `evaluation_worker_count=10`。",
        "- 真实运行仍只使用 analysis 30 题；validation 200 题继续保持盲态。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize human conclusions for the offline structured chunk audit.")
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    args = parser.parse_args()
    evaluation_dir = args.evaluation_dir.resolve()
    records = build_review(evaluation_dir / "chunk-audit.jsonl")
    (evaluation_dir / "manual-review.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    (evaluation_dir / "manual-review.md").write_text(
        _markdown(records), encoding="utf-8", newline="\n"
    )
    print(evaluation_dir / "manual-review.jsonl")
    print(evaluation_dir / "manual-review.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
