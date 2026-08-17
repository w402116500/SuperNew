"""Freeze the 15 analysis cases that entered the existing rewrite branch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.runner import create_rewrite_candidate_fusion_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an immutable T9 analysis target manifest.")
    parser.add_argument("--source-results", required=True, type=Path)
    parser.add_argument("--case-split", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-evaluation-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output = create_rewrite_candidate_fusion_manifest(
        source_results_path=args.source_results,
        case_split_path=args.case_split,
        output_path=args.output,
        source_evaluation_id=args.source_evaluation_id,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
