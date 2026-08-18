"""Generate a local old/new Markdown chunk comparison for the frozen analysis target."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.chunking_audit import run_structured_chunking_offline_audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the no-service structured Markdown chunk audit.")
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument("--markdown-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run_structured_chunking_offline_audit(
        target_manifest_path=args.target_manifest,
        markdown_dir=args.markdown_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
