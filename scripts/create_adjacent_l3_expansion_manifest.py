"""Freeze adjacent-L3 analysis targets for a controlled evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.runner import (
    create_adjacent_l3_expansion_manifest,
    create_adjacent_l3_non_pass_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an immutable adjacent-L3 expansion target manifest.")
    parser.add_argument("--source-results", required=True, type=Path)
    parser.add_argument("--raw-candidate-fact-review", type=Path)
    parser.add_argument("--case-split", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-evaluation-id", required=True)
    parser.add_argument(
        "--selection-mode",
        choices=["adjacent_leaf_gap", "non_pass_analysis"],
        default="adjacent_leaf_gap",
    )
    args = parser.parse_args()
    if args.selection_mode == "non_pass_analysis":
        output = create_adjacent_l3_non_pass_manifest(
            source_results_path=args.source_results,
            case_split_path=args.case_split,
            output_path=args.output,
            source_evaluation_id=args.source_evaluation_id,
        )
    else:
        if args.raw_candidate_fact_review is None:
            parser.error("adjacent_leaf_gap 模式必须提供 --raw-candidate-fact-review")
        output = create_adjacent_l3_expansion_manifest(
            source_results_path=args.source_results,
            raw_candidate_fact_review_path=args.raw_candidate_fact_review,
            case_split_path=args.case_split,
            output_path=args.output,
            source_evaluation_id=args.source_evaluation_id,
        )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
