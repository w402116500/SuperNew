"""Freeze the structured DeepSeek 30-case target for an English-only query run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.runner import create_subquestion_language_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="创建英文子问题语言控制的冻结 30 题清单。")
    parser.add_argument("--source-target-manifest", required=True, type=Path)
    parser.add_argument("--source-results", required=True, type=Path)
    parser.add_argument("--case-split", required=True, type=Path)
    parser.add_argument("--source-evaluation-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = create_subquestion_language_manifest(
        source_target_manifest_path=args.source_target_manifest,
        source_results_path=args.source_results,
        case_split_path=args.case_split,
        output_path=args.output,
        source_evaluation_id=args.source_evaluation_id,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
