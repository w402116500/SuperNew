"""Offline, analysis-only audit helpers for structured Markdown chunking."""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from backend.indexing.document_loader import (
    STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
    DocumentLoader,
)


CHUNKING_TARGET_MANIFEST_VERSION = "structured-chunking-target-v1"
CHUNKING_AUDIT_VERSION = "structured-chunking-offline-audit-v1"
TARGET_CLASSIFICATION = "same_source_far_leaf_gap"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    return sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _runner_target_manifest_hash(payload: dict[str, Any]) -> str:
    """Match the runner's target-manifest hash without importing its private helper."""
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(f"冻结清单已存在且内容不同，拒绝覆盖：{path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8", newline="\n")
    return path


def create_structured_chunking_target_manifest(
    *,
    raw_review_path: Path,
    case_split_path: Path,
    corpus_documents_path: Path,
    cases_path: Path,
    baseline_results_path: Path,
    markdown_dir: Path,
    output_path: Path,
    source_corpus_run_id: str,
    source_evaluation_id: str,
    expected_case_count: int = 30,
) -> Path:
    """Freeze exactly the audited analysis cases whose evidence is farther in one file."""
    reviews = _read_jsonl(raw_review_path)
    selected = [
        record
        for record in reviews
        if record.get("classification") == TARGET_CLASSIFICATION
    ]
    selected.sort(key=lambda item: str(item.get("case_id") or ""))
    case_ids = [str(record.get("case_id") or "") for record in selected]
    if len(selected) != expected_case_count or len(set(case_ids)) != expected_case_count:
        raise ValueError(
            f"结构化分块离线审计必须冻结 {expected_case_count} 道唯一 far-leaf analysis 题，当前为 {len(selected)}"
        )
    if any(record.get("case_set") != "analysis" or not record.get("source_ref") for record in selected):
        raise ValueError("结构化分块 target 只能包含带 source_ref 的 analysis 审计记录")

    split = _read_json(case_split_path)
    analysis_ids = {str(item) for item in split.get("analysis_case_ids") or []}
    validation_ids = {str(item) for item in split.get("validation_case_ids") or []}
    if not set(case_ids).issubset(analysis_ids) or set(case_ids) & validation_ids:
        raise ValueError("结构化分块 target 发现 validation 或不属于 analysis 的 case ID")

    documents_by_id = {
        str(item.get("doc_id")): item
        for item in _read_json(corpus_documents_path)
    }
    cases_by_id = {str(item.get("id")): item for item in _read_jsonl(cases_path)}
    baseline_by_id = {str(item.get("case_id")): item for item in _read_jsonl(baseline_results_path)}
    targets: list[dict[str, Any]] = []
    target_document_ids: set[str] = set()
    for review in selected:
        case_id = str(review["case_id"])
        source_ref = str(review["source_ref"])
        document = documents_by_id.get(source_ref)
        case = cases_by_id.get(case_id)
        baseline = baseline_by_id.get(case_id)
        if document is None or case is None or baseline is None:
            raise ValueError(f"无法为 {case_id} 找到冻结所需的文档、题目或基线记录")
        stable_filename = str(document.get("stable_filename") or "")
        markdown_path = markdown_dir / stable_filename
        if not stable_filename or not markdown_path.is_file():
            raise FileNotFoundError(f"{case_id} 的 Markdown 原文不存在：{markdown_path}")
        markdown_hash = _sha256_file(markdown_path)
        expected_hash = str(document.get("markdown_hash") or "")
        if expected_hash and expected_hash != markdown_hash:
            raise ValueError(f"{case_id} 的 Markdown 哈希与冻结 corpus manifest 不一致")
        expected_document_ids = [str(value) for value in case.get("expected_doc_ids") or []]
        if source_ref not in expected_document_ids:
            expected_document_ids.insert(0, source_ref)
        target_document_ids.update(expected_document_ids)
        targets.append({
            "case_id": case_id,
            "case_set": "analysis",
            "source_ref": source_ref,
            "stable_filename": stable_filename,
            "source_markdown_sha256": markdown_hash,
            "missing_fact_summary": str(review.get("missing_fact_summary") or ""),
            "retrieved_l3_indices": list(review.get("retrieved_l3_indices") or []),
            "evidence_l3_indices": list(review.get("evidence_l3_indices") or []),
            "review_reason": str(review.get("reason") or ""),
            "review_confidence": str(review.get("confidence") or ""),
            "baseline_evidence_coverage": baseline.get("evidence_coverage"),
            "baseline_answer_verdict": (baseline.get("answer_grade") or {}).get("verdict"),
            "baseline_expected_evidence_filenames": list(case.get("expected_evidence_filenames") or []),
            "expected_document_ids": expected_document_ids,
        })

    payload = {
        "manifest_type": "structured_chunking_offline_audit",
        "manifest_version": CHUNKING_TARGET_MANIFEST_VERSION,
        "target_classification": TARGET_CLASSIFICATION,
        "changed_variable": "document_chunking_strategy",
        "case_set": "analysis",
        "case_count": len(targets),
        "case_ids": [target["case_id"] for target in targets],
        "source_corpus_run_id": source_corpus_run_id,
        "source_evaluation_id": source_evaluation_id,
        "source_raw_review": str(raw_review_path),
        "source_raw_review_sha256": _sha256_file(raw_review_path),
        "source_case_split": str(case_split_path),
        "source_case_split_sha256": _sha256_file(case_split_path),
        # Aliases match the existing controlled target-manifest contract used by
        # the runner when this offline-only manifest is later authorized for RAG.
        "source_case_split_path": str(case_split_path),
        "source_baseline_results": str(baseline_results_path),
        "source_baseline_results_sha256": _sha256_file(baseline_results_path),
        "source_results_path": str(baseline_results_path),
        "source_results_sha256": _sha256_file(baseline_results_path),
        "old_chunking_strategy": "recursive_l1_l2_l3",
        "new_chunking_strategy": STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        "targets": targets,
        # The source_ref is the primary missed document, but a question may
        # require additional answer documents.  Freeze the complete union so a
        # targeted re-chunk never silently leaves one evidence source on the
        # legacy strategy.
        "target_document_ids": sorted(target_document_ids),
        "target_document_count": len(target_document_ids),
    }
    payload["manifest_payload_sha256"] = _payload_hash(payload)
    payload["manifest_sha256"] = _runner_target_manifest_hash(payload)
    return _write_immutable_json(output_path, payload)


def _leaf_index(chunk: dict[str, Any]) -> int:
    try:
        return int(str(chunk["chunk_id"]).rsplit("::", 1)[-1])
    except (KeyError, ValueError):
        return int(chunk.get("chunk_idx", 0))


def _legacy_leaf_spans(markdown: str, chunks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Locate legacy text in the original file without calling any external service."""
    leaves: list[dict[str, Any]] = []
    last_start = 0
    for chunk in sorted(chunks, key=_leaf_index):
        text = str(chunk.get("text") or "")
        start = markdown.find(text, max(0, last_start - len(text)))
        if start < 0:
            start = markdown.find(text)
        end = start + len(text) if start >= 0 else -1
        if start >= 0:
            last_start = start
        leaves.append({
            "l3_index": _leaf_index(chunk),
            "chunk_id": chunk["chunk_id"],
            "source_start_index": start,
            "source_end_index": end,
            "text_excerpt": text[:300],
        })
    return leaves


def _structured_leaf_snapshot(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "heading_path": chunk.get("heading_path", ""),
        "heading_level": chunk.get("heading_level", 0),
        "source_start_index": chunk.get("source_start_index", 0),
        "source_end_index": chunk.get("source_end_index", 0),
        "content_kind": chunk.get("content_kind", "mixed"),
        "previous_chunk_id": chunk.get("previous_chunk_id", ""),
        "next_chunk_id": chunk.get("next_chunk_id", ""),
        "text_excerpt": str(chunk.get("text") or "")[:300],
    }


def _overlapping_structured_leaves(old_leaf: dict[str, Any], structured_leaves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_start = int(old_leaf.get("source_start_index", -1))
    old_end = int(old_leaf.get("source_end_index", -1))
    if old_start < 0 or old_end <= old_start:
        return []
    return [
        _structured_leaf_snapshot(chunk)
        for chunk in structured_leaves
        if int(chunk["source_start_index"]) < old_end
        and int(chunk["source_end_index"]) > old_start
    ]


def _write_audit_markdown(path: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    lines = [
        "# 结构化 Markdown 分块离线审计",
        "",
        "范围：冻结的 30 道 `analysis` 题，均为已人工确认的同文件远距离 L3 材料缺口。",
        "本报告只读取原始 Markdown 和已冻结审计记录；没有调用模型、Embedding、Milvus、PostgreSQL 或 Redis。",
        "这里的‘重叠’仅表示新旧块覆盖同一段原文，不表示回答已经变对；最终质量仍需在后续批准的真实 RAG 对照中验证。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| 题目数 | {summary['case_count']} |",
        f"| 旧 L3 块数 | {summary['old_leaf_count']} |",
        f"| 新 L3 块数 | {summary['new_leaf_count']} |",
        f"| 关键旧 L3 记录数 | {summary['evidence_leaf_count']} |",
        f"| 新旧边界完全相同的关键 L3 | {summary['exact_old_to_new_evidence_leaf_count']} |",
        f"| 边界变化的关键 L3 | {summary['changed_boundary_evidence_leaf_count']} |",
        f"| 关键材料处于二级及以上标题的题目 | {summary['cases_with_nested_heading_for_key_material']} |",
        f"| 关键旧 L3 块能映射到新块的题目 | {summary['cases_with_new_overlap']} |",
        f"| 无法定位旧块原文范围的题目 | {summary['cases_without_old_span']} |",
        "",
        "## 逐题并排记录",
    ]
    for record in records:
        lines.extend([
            "",
            f"### {record['case_id']}",
            "",
            f"- 缺失事实：{record['missing_fact_summary'] or '（审计未提供摘要）'}",
            f"- 基线：证据覆盖 `{record['baseline_evidence_coverage']}`；回答判定 `{record['baseline_answer_verdict']}`。",
            f"- 旧关键 L3：{', '.join(str(item['l3_index']) for item in record['old_evidence_leaves']) or '（未定位）'}。",
            f"- 新块重叠数：`{record['new_overlap_count']}`；标题路径：{', '.join(record['new_overlap_heading_paths']) or '（无）'}。",
        ])
        for old_leaf in record["old_evidence_leaves"]:
            lines.extend([
                "",
                f"旧 L3 #{old_leaf['l3_index']} `{old_leaf['chunk_id']}`，原文范围 `{old_leaf['source_start_index']}:{old_leaf['source_end_index']}`：",
                "```text",
                old_leaf["text_excerpt"],
                "```",
            ])
            overlaps = old_leaf.get("new_overlaps") or []
            if not overlaps:
                lines.append("- 新块：未找到可定位的原文重叠。")
            for overlap in overlaps:
                lines.extend([
                    f"- 新 L3 `{overlap['chunk_id']}`；标题 `{overlap['heading_path'] or '（无）'}`；"
                    f"范围 `{overlap['source_start_index']}:{overlap['source_end_index']}`；类型 `{overlap['content_kind']}`。",
                ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def run_structured_chunking_offline_audit(
    *,
    target_manifest_path: Path,
    markdown_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Compare legacy and structured chunks without initializing any external client."""
    manifest = _read_json(target_manifest_path)
    if manifest.get("manifest_type") != "structured_chunking_offline_audit":
        raise ValueError("不是结构化分块离线审计 target manifest")
    targets = list(manifest.get("targets") or [])
    if not targets or any(target.get("case_set") != "analysis" for target in targets):
        raise ValueError("离线分块审计只能读取冻结的 analysis target")
    if manifest.get("case_count") != len(targets):
        raise ValueError("target manifest case_count 与 targets 不一致")
    expected_hash = manifest.get("manifest_payload_sha256")
    payload_without_hash = dict(manifest)
    payload_without_hash.pop("manifest_payload_sha256", None)
    payload_without_hash.pop("manifest_sha256", None)
    if expected_hash != _payload_hash(payload_without_hash):
        raise ValueError("target manifest payload 哈希不匹配")

    legacy_loader = DocumentLoader()
    structured_loader = DocumentLoader(
        chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
    )
    records: list[dict[str, Any]] = []
    for target in targets:
        markdown_path = markdown_dir / str(target["stable_filename"])
        if not markdown_path.is_file() or _sha256_file(markdown_path) != target["source_markdown_sha256"]:
            raise ValueError(f"{target['case_id']} 的 Markdown 输入缺失或哈希漂移")
        markdown = markdown_path.read_text(encoding="utf-8-sig")
        old_chunks = legacy_loader.load_document(str(markdown_path), str(target["stable_filename"]))
        new_chunks = structured_loader.load_document(str(markdown_path), str(target["stable_filename"]))
        old_leaves = _legacy_leaf_spans(
            markdown,
            [chunk for chunk in old_chunks if chunk["chunk_level"] == 3],
        )
        new_leaves = [chunk for chunk in new_chunks if chunk["chunk_level"] == 3]
        old_by_index = {leaf["l3_index"]: leaf for leaf in old_leaves}
        evidence_leaves = [
            dict(old_by_index[index])
            for index in target.get("evidence_l3_indices") or []
            if index in old_by_index
        ]
        for old_leaf in evidence_leaves:
            old_leaf["new_overlaps"] = _overlapping_structured_leaves(old_leaf, new_leaves)
        overlaps = [
            item
            for old_leaf in evidence_leaves
            for item in old_leaf.get("new_overlaps") or []
        ]
        records.append({
            "audit_version": CHUNKING_AUDIT_VERSION,
            "case_id": target["case_id"],
            "case_set": "analysis",
            "source_ref": target["source_ref"],
            "stable_filename": target["stable_filename"],
            "source_markdown_sha256": target["source_markdown_sha256"],
            "missing_fact_summary": target.get("missing_fact_summary", ""),
            "baseline_evidence_coverage": target.get("baseline_evidence_coverage"),
            "baseline_answer_verdict": target.get("baseline_answer_verdict"),
            "old_leaf_count": len(old_leaves),
            "new_leaf_count": len(new_leaves),
            "old_evidence_leaves": evidence_leaves,
            "unlocated_old_evidence_indices": [
                index for index in target.get("evidence_l3_indices") or [] if index not in old_by_index
            ],
            "new_overlap_count": len(overlaps),
            "new_overlap_heading_paths": sorted({str(item.get("heading_path") or "") for item in overlaps}),
            "new_content_kinds": dict(sorted(Counter(str(chunk.get("content_kind")) for chunk in new_leaves).items())),
            "comparison_status": "requires_human_review",
        })

    evidence_leaves = [
        leaf
        for record in records
        for leaf in record["old_evidence_leaves"]
    ]
    exact_old_to_new_evidence_leaf_count = sum(
        any(
            int(overlap["source_start_index"]) == int(leaf["source_start_index"])
            and int(overlap["source_end_index"]) == int(leaf["source_end_index"])
            for overlap in leaf.get("new_overlaps") or []
        )
        for leaf in evidence_leaves
    )
    summary = {
        "audit_version": CHUNKING_AUDIT_VERSION,
        "case_set": "analysis",
        "case_count": len(records),
        "target_manifest_sha256": _sha256_file(target_manifest_path),
        "old_chunking_strategy": "recursive_l1_l2_l3",
        "new_chunking_strategy": STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
        "old_leaf_count": sum(record["old_leaf_count"] for record in records),
        "new_leaf_count": sum(record["new_leaf_count"] for record in records),
        "evidence_leaf_count": len(evidence_leaves),
        "exact_old_to_new_evidence_leaf_count": exact_old_to_new_evidence_leaf_count,
        "changed_boundary_evidence_leaf_count": (
            len(evidence_leaves) - exact_old_to_new_evidence_leaf_count
        ),
        "cases_with_nested_heading_for_key_material": sum(
            any(" > " in path for path in record["new_overlap_heading_paths"])
            for record in records
        ),
        "cases_with_new_overlap": sum(bool(record["new_overlap_count"]) for record in records),
        "cases_without_old_span": sum(bool(record["unlocated_old_evidence_indices"]) for record in records),
        "automatic_conclusion": "requires_human_review",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "chunk-audit.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "chunk-audit-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_audit_markdown(output_dir / "chunk-audit.md", summary, records)
    return summary
