"""Audit semantic Markdown chunking without creating an index or evaluation run."""

from __future__ import annotations

import argparse
import json
import random
import sys
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.datasets import (
    ENTERPRISE_SEED,
    enterprise_document_filename,
    enterprise_markdown,
    iter_enterprise_documents,
    load_enterprise_documents_by_ids,
)
from backend.evaluation.runner import ENTERPRISE_ROOT
from backend.indexing.document_loader import (
    SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
    STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
    DocumentLoader,
    sanitize_text,
)
from backend.indexing.embedding import embedding_service
from backend.indexing.semantic_chunking import (
    SEMANTIC_PARAGRAPH_MIN_CHARS,
    plan_paragraph_boundaries,
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _target_ids(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    payload = _read_json(path)
    selected = {str(value) for value in payload.get("target_document_ids") or []}
    selected.update(
        str(target.get("source_ref"))
        for target in payload.get("targets") or []
        if isinstance(target, dict) and str(target.get("source_ref") or "").strip()
    )
    return selected


def _select_ids(target_manifest: Path | None, random_count: int) -> set[str]:
    selected = _target_ids(target_manifest)
    all_ids = [str(row["doc_id"]) for row in iter_enterprise_documents(ENTERPRISE_ROOT)]
    candidates = [doc_id for doc_id in all_ids if doc_id not in selected]
    rng = random.Random(ENTERPRISE_SEED)
    rng.shuffle(candidates)
    selected.update(candidates[: max(0, random_count)])
    return selected


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线审计结构保护语义切分，不写任何数据库。")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target-manifest", type=Path, default=None)
    parser.add_argument("--random-documents", type=int, default=50)
    args = parser.parse_args()
    if args.random_documents < 0:
        raise ValueError("--random-documents 不能为负数")

    selected_ids = _select_ids(args.target_manifest, args.random_documents)
    records = load_enterprise_documents_by_ids(ENTERPRISE_ROOT, selected_ids)
    control_loader = DocumentLoader(chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY)
    semantic_loader = DocumentLoader(chunking_strategy=SEMANTIC_MARKDOWN_CHUNKING_STRATEGY)
    semantic_plans: dict[str, list[dict]] = {}
    control_chunks: list[dict] = []
    semantic_chunks: list[dict] = []
    source_by_filename: dict[str, str] = {}
    structural_atoms_by_filename: dict[str, list[dict]] = {}
    plan_input_count = 0

    for record in records:
        filename = enterprise_document_filename(str(record["doc_id"]))
        markdown = enterprise_markdown(record)
        temp_path = args.output_dir / "markdown" / filename
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(markdown, encoding="utf-8", newline="\n")
        source_text = sanitize_text(markdown.strip())
        source_by_filename[filename] = source_text
        structural_atoms_by_filename[filename] = [
            atom
            for section in semantic_loader._markdown_sections(source_text)
            for atom in semantic_loader._markdown_atoms(section)
            if str(atom.get("kind") or "") in {"list", "table", "code"}
        ]
        paragraphs = semantic_loader.markdown_paragraph_atoms(source_text)
        plans: list[dict] = []
        for atom in paragraphs:
            if len(str(atom.get("text") or "")) <= SEMANTIC_PARAGRAPH_MIN_CHARS:
                continue
            paragraph = {
                "document_id": filename,
                "source_start": int(atom["start"]),
                "source_end": int(atom["end"]),
                "text": str(atom["text"]),
            }
            plan = plan_paragraph_boundaries(paragraph, embedding_service.get_embeddings)
            plans.append(plan)
            plan_input_count += sum(
                2 for _ in plan.get("boundary_scores") or []
            )
        semantic_plans[filename] = plans
        control_chunks.extend(control_loader.load_document(str(temp_path), filename, str(temp_path)))
        semantic_chunks.extend(
            semantic_loader.load_document(
                str(temp_path), filename, str(temp_path),
            )
            if not plans
            else DocumentLoader(
                chunking_strategy=SEMANTIC_MARKDOWN_CHUNKING_STRATEGY,
                semantic_boundary_plan=plans,
            ).load_document(str(temp_path), filename, str(temp_path))
        )

    plan_path = args.output_dir / "semantic-boundaries.jsonl"
    plan_path.write_text(
        "".join(
            json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n"
            for filename in sorted(semantic_plans)
            for plan in semantic_plans[filename]
        ),
        encoding="utf-8",
        newline="\n",
    )

    failures: list[str] = []
    leaf_ranges_by_filename: dict[str, list[tuple[int, int]]] = {}
    semantic_chunk_fingerprints: set[tuple[str, int, int, int, str]] = set()
    for chunk in semantic_chunks:
        filename = str(chunk.get("filename") or "")
        start = int(chunk.get("source_start_index", -1))
        end = int(chunk.get("source_end_index", -1))
        if start < 0 or end <= start:
            failures.append(f"invalid_source_range:{chunk.get('chunk_id')}")
            continue
        if chunk.get("chunk_level") == 3:
            leaf_ranges_by_filename.setdefault(filename, []).append((start, end))
            fingerprint = (
                filename,
                3,
                start,
                end,
                str(chunk.get("text") or ""),
            )
            if fingerprint in semantic_chunk_fingerprints:
                failures.append(f"duplicate_semantic_leaf:{chunk.get('chunk_id')}")
            semantic_chunk_fingerprints.add(fingerprint)
            if (
                chunk.get("content_kind") == "paragraph"
                and end - start > 800
            ):
                failures.append(f"overlong_semantic_leaf:{chunk.get('chunk_id')}")
        if not str(chunk.get("heading_path") or "").strip():
            failures.append(f"missing_heading_path:{chunk.get('chunk_id')}")
        if chunk.get("content_kind") in {"list", "table", "code"}:
            source = source_by_filename.get(filename, "")
            raw = source[start:end].strip()
            if raw and raw not in str(chunk.get("text") or ""):
                failures.append(f"structural_atom_changed:{chunk.get('chunk_id')}")
    for filename, atoms in structural_atoms_by_filename.items():
        leaf_ranges = leaf_ranges_by_filename.get(filename, [])
        for atom in atoms:
            start = int(atom["start"])
            end = int(atom["end"])
            if not any(chunk_start <= start and end <= chunk_end for chunk_start, chunk_end in leaf_ranges):
                failures.append(f"structural_atom_split:{filename}:{start}:{end}")
    control_ids = {str(chunk.get("chunk_id")) for chunk in control_chunks}
    semantic_ids = [str(chunk.get("chunk_id")) for chunk in semantic_chunks]
    if len(semantic_ids) != len(set(semantic_ids)):
        failures.append("duplicate_semantic_chunk_id")

    summary = {
        "audit_version": "semantic-chunking-offline-audit-v1",
        "target_manifest": str(args.target_manifest) if args.target_manifest else None,
        "random_seed": ENTERPRISE_SEED,
        "document_count": len(records),
        "control_chunk_count": len(control_chunks),
        "semantic_chunk_count": len(semantic_chunks),
        "semantic_paragraph_count": sum(len(items) for items in semantic_plans.values()),
        "semantic_applied_count": sum(
            plan.get("decision") == "semantic_applied"
            for items in semantic_plans.values()
            for plan in items
        ),
        "semantic_boundary_count": sum(
            len(plan.get("selected_boundaries") or [])
            for items in semantic_plans.values()
            for plan in items
        ),
        "semantic_percentile_boundary_count": sum(
            int(plan.get("semantic_percentile_boundary_count") or 0)
            for items in semantic_plans.values()
            for plan in items
        ),
        "semantic_size_guard_boundary_count": sum(
            int(plan.get("semantic_size_guard_boundary_count") or 0)
            for items in semantic_plans.values()
            for plan in items
        ),
        "sentence_size_fallback_boundary_count": sum(
            int(plan.get("sentence_size_fallback_boundary_count") or 0)
            for items in semantic_plans.values()
            for plan in items
        ),
        "embedding_window_count": plan_input_count,
        "semantic_boundary_sha256": sha256(plan_path.read_bytes()).hexdigest(),
        "control_chunk_ids_unique": len(control_ids) == len(control_chunks),
        "failures": failures,
        "audit_passed": not failures,
    }
    _write_json(args.output_dir / "semantic-offline-audit.json", summary)
    failure_lines = [f"- {item}" for item in failures] or ["- 无"]
    (args.output_dir / "semantic-offline-audit.md").write_text(
        "\n".join([
            "# 语义切分离线审计",
            "",
            "本审计不写 Milvus、PostgreSQL 或 Redis，也不调用回答和判卷模型。",
            "",
            f"- 文档数：{summary['document_count']}",
            f"- 当前 Markdown 块数：{summary['control_chunk_count']}",
            f"- 语义切分块数：{summary['semantic_chunk_count']}",
            f"- 参与语义计算的长段落：{summary['semantic_paragraph_count']}",
            f"- 实际产生语义断点的段落：{summary['semantic_applied_count']}",
            f"- 审计结果：{'通过' if summary['audit_passed'] else '失败'}",
            "",
            "失败项：",
            *failure_lines,
            "",
        ]) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["audit_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
