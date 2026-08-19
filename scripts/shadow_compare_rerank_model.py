"""Compare a replacement reranker against recorded, identical candidate pools."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rag.utils import (  # noqa: E402
    RERANK_API_KEY,
    RERANK_MODEL,
    RERANK_TIMEOUT_SECONDS,
    _get_rerank_endpoint,
)


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _filenames(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["filename"]) for item in items if item.get("filename")}


def _call_rerank(task: dict[str, Any], *, top_k: int) -> dict[str, Any]:
    candidates = task["candidates"]
    payload = {
        "model": RERANK_MODEL,
        "query": task["query"],
        "documents": [str(item.get("text") or "") for item in candidates],
        "top_n": min(top_k, len(candidates)),
        "return_documents": False,
    }
    result: dict[str, Any] = {
        "case_id": task["case_id"],
        "audit_index": task["audit_index"],
        "query": task["query"],
        "expected_evidence_filenames": task["expected_evidence_filenames"],
        "candidate_count": len(candidates),
        "candidate_chunk_ids": [item.get("chunk_id") for item in candidates],
        "candidate_filenames": [item.get("filename") for item in candidates],
        "old_returned_chunk_ids": task["old_returned_chunk_ids"],
        "old_returned_filenames": task["old_returned_filenames"],
        "new_returned_chunk_ids": [],
        "new_returned_filenames": [],
        "new_rerank_error": "",
    }
    try:
        response = requests.post(
            _get_rerank_endpoint(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {RERANK_API_KEY}",
            },
            json=payload,
            timeout=RERANK_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            result["new_rerank_error"] = f"HTTP {response.status_code}: {response.text}"
            return result
        returned: list[dict[str, Any]] = []
        for item in response.json().get("results", []):
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(candidates):
                returned.append(candidates[index])
        if not returned:
            result["new_rerank_error"] = "empty_rerank_results"
            return result
        result["new_returned_chunk_ids"] = [item.get("chunk_id") for item in returned[:top_k]]
        result["new_returned_filenames"] = [item.get("filename") for item in returned[:top_k]]
    except (requests.RequestException, ValueError, TypeError) as exc:
        result["new_rerank_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _coverage(expected: set[str], selected: set[str]) -> float:
    return len(expected & selected) / len(expected) if expected else 1.0


def _classify(expected: set[str], input_files: set[str], selected_files: set[str]) -> str:
    if expected - input_files:
        return "raw_candidate_missing"
    if expected - selected_files:
        return "rerank_drop"
    return "retained"


def main() -> int:
    parser = argparse.ArgumentParser(description="在固定候选池中影子比较 Rerank 模型。")
    parser.add_argument("--source-results", required=True, type=Path)
    parser.add_argument("--source-evaluation-config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.top_k < 1:
        raise ValueError("--top-k must be at least 1")
    if args.output_dir.exists():
        raise FileExistsError(f"output directory already exists: {args.output_dir}")
    if not args.source_results.is_file() or not args.source_evaluation_config.is_file():
        raise FileNotFoundError("source results or evaluation config is missing")
    if not RERANK_API_KEY:
        raise RuntimeError("RERANK_API_KEY is not configured")

    source_config = json.loads(args.source_evaluation_config.read_text(encoding="utf-8"))
    source_records = [
        json.loads(line)
        for line in args.source_results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tasks: list[dict[str, Any]] = []
    excluded_case_ids: set[str] = set()
    for record in source_records:
        audits = record.get("candidate_audits") or []
        usable_audits = 0
        for audit_index, audit in enumerate(audits):
            meta = audit.get("meta") or {}
            candidates = audit.get("rerank_input_candidates") or []
            returned = audit.get("rerank_returned_candidates") or []
            if not meta.get("rerank_applied") or meta.get("rerank_error") or not candidates or not returned:
                continue
            usable_audits += 1
            tasks.append({
                "case_id": str(record["case_id"]),
                "audit_index": audit_index,
                "query": str(audit.get("query") or ""),
                "expected_evidence_filenames": list(record.get("expected_evidence_filenames") or []),
                "candidates": candidates,
                "old_returned_chunk_ids": [item.get("chunk_id") for item in returned[:args.top_k]],
                "old_returned_filenames": [item.get("filename") for item in returned[:args.top_k]],
            })
        if not usable_audits:
            excluded_case_ids.add(str(record["case_id"]))

    args.output_dir.mkdir(parents=True)
    config = {
        "evaluation_id": args.output_dir.name,
        "changed_variable": "rerank_model",
        "comparison_type": "fixed_candidate_pool_shadow",
        "case_set": "analysis",
        "source_evaluation_id": source_config.get("evaluation_id"),
        "source_results_path": str(args.source_results),
        "source_results_sha256": _sha256_file(args.source_results),
        "baseline_rerank_model": source_config.get("rerank_model"),
        "candidate_rerank_model": RERANK_MODEL,
        "rerank_endpoint": _get_rerank_endpoint(),
        "rerank_timeout_seconds": RERANK_TIMEOUT_SECONDS,
        "top_k": args.top_k,
        "worker_count": args.workers,
        "source_case_count": len(source_records),
        "candidate_audit_count": len(tasks),
        "excluded_case_ids_without_usable_trace": sorted(excluded_case_ids),
        "secrets_recorded": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_json(args.output_dir / "evaluation-config.json", config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(lambda task: _call_rerank(task, top_k=args.top_k), tasks))
    results.sort(key=lambda item: (item["case_id"], item["audit_index"]))
    with (args.output_dir / "results.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")

    by_case: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_case.setdefault(str(result["case_id"]), []).append(result)
    case_rows: list[dict[str, Any]] = []
    for case_id, rows in sorted(by_case.items()):
        expected = set(rows[0]["expected_evidence_filenames"])
        input_files = {name for row in rows for name in row["candidate_filenames"] if name}
        old_files = {name for row in rows for name in row["old_returned_filenames"] if name}
        new_files = {name for row in rows for name in row["new_returned_filenames"] if name}
        errors = [str(row["new_rerank_error"]) for row in rows if row["new_rerank_error"]]
        case_rows.append({
            "case_id": case_id,
            "expected_evidence_filenames": sorted(expected),
            "input_expected_file_count": len(expected & input_files),
            "old_evidence_coverage": _coverage(expected, old_files),
            "new_evidence_coverage": _coverage(expected, new_files) if not errors else None,
            "old_classification": _classify(expected, input_files, old_files),
            "new_classification": _classify(expected, input_files, new_files) if not errors else "rerank_system_error",
            "audit_count": len(rows),
            "new_rerank_errors": errors,
        })
    _write_json(args.output_dir / "case-summary.json", case_rows)

    successful_cases = [row for row in case_rows if not row["new_rerank_errors"]]
    old_coverages = [float(row["old_evidence_coverage"]) for row in successful_cases]
    new_coverages = [float(row["new_evidence_coverage"]) for row in successful_cases]
    summary = {
        "evaluation_id": args.output_dir.name,
        "source_case_count": len(source_records),
        "traced_case_count": len(case_rows),
        "successful_case_count": len(successful_cases),
        "rerank_audit_count": len(results),
        "successful_rerank_audit_count": sum(not row["new_rerank_error"] for row in results),
        "rerank_system_error_count": sum(bool(row["new_rerank_error"]) for row in results),
        "old_evidence_full_coverage_rate": sum(value == 1.0 for value in old_coverages) / len(old_coverages) if old_coverages else None,
        "new_evidence_full_coverage_rate": sum(value == 1.0 for value in new_coverages) / len(new_coverages) if new_coverages else None,
        "old_evidence_average_coverage_rate": sum(old_coverages) / len(old_coverages) if old_coverages else None,
        "new_evidence_average_coverage_rate": sum(new_coverages) / len(new_coverages) if new_coverages else None,
        "improved_case_ids": [
            row["case_id"] for row in successful_cases
            if float(row["new_evidence_coverage"]) > float(row["old_evidence_coverage"])
        ],
        "worsened_case_ids": [
            row["case_id"] for row in successful_cases
            if float(row["new_evidence_coverage"]) < float(row["old_evidence_coverage"])
        ],
        "old_rerank_drop_case_ids": [
            row["case_id"] for row in successful_cases if row["old_classification"] == "rerank_drop"
        ],
        "new_rerank_drop_case_ids": [
            row["case_id"] for row in successful_cases if row["new_classification"] == "rerank_drop"
        ],
        "excluded_case_ids_without_usable_trace": sorted(excluded_case_ids),
    }
    _write_json(args.output_dir / "summary.json", summary)

    lines = [
        "# Fixed Candidate Pool Rerank Comparison",
        "",
        "This is an analysis-only shadow comparison. Each new-model request receives the exact query and candidate documents recorded for the old model.",
        "",
        f"- Baseline reranker: `{config['baseline_rerank_model']}`",
        f"- Candidate reranker: `{config['candidate_rerank_model']}`",
        f"- Usable cases: {summary['successful_case_count']}/{summary['source_case_count']}",
        f"- Successful rerank calls: {summary['successful_rerank_audit_count']}/{summary['rerank_audit_count']}",
        f"- Full evidence coverage: {summary['old_evidence_full_coverage_rate']:.2%} -> {summary['new_evidence_full_coverage_rate']:.2%}" if summary["successful_case_count"] else "- No successful cases",
        f"- Average evidence coverage: {summary['old_evidence_average_coverage_rate']:.2%} -> {summary['new_evidence_average_coverage_rate']:.2%}" if summary["successful_case_count"] else "",
        f"- Improved cases: {', '.join(summary['improved_case_ids']) or 'none'}",
        f"- Worsened cases: {', '.join(summary['worsened_case_ids']) or 'none'}",
        f"- Old rerank-drop cases: {', '.join(summary['old_rerank_drop_case_ids']) or 'none'}",
        f"- New rerank-drop cases: {', '.join(summary['new_rerank_drop_case_ids']) or 'none'}",
        "",
        "This result measures only rerank selection. It does not evaluate answer generation, grading, or validation-set generalization.",
    ]
    (args.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
