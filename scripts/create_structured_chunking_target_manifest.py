"""Freeze the 30 analysis cases used by the structured-chunking offline audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.chunking_audit import create_structured_chunking_target_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an immutable 30-case structured chunking audit manifest.")
    parser.add_argument("--raw-review", type=Path, required=True)
    parser.add_argument("--case-split", type=Path, required=True)
    parser.add_argument("--corpus-documents", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--baseline-results", type=Path, required=True)
    parser.add_argument("--markdown-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-corpus-run-id", required=True)
    parser.add_argument("--source-evaluation-id", required=True)
    args = parser.parse_args()
    output = create_structured_chunking_target_manifest(
        raw_review_path=args.raw_review,
        case_split_path=args.case_split,
        corpus_documents_path=args.corpus_documents,
        cases_path=args.cases,
        baseline_results_path=args.baseline_results,
        markdown_dir=args.markdown_dir,
        output_path=args.output,
        source_corpus_run_id=args.source_corpus_run_id,
        source_evaluation_id=args.source_evaluation_id,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
