"""Explicitly initialize the parent-chunk table in an existing evaluation DB.

This command intentionally does not issue ``CREATE DATABASE``.  An operator
must provision the separate PostgreSQL database and grant the evaluation role
access before running it.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.storage import EvaluationParentChunkStore, EvaluationStorageConfig


def main() -> int:
    config = EvaluationStorageConfig.from_env()
    store = EvaluationParentChunkStore("schema-initialization", config=config)
    store.check_connection()
    store.initialize_schema()
    print(
        "Initialized evaluation parent-chunk table "
        f"in database '{config.database_name}' without creating a database."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
