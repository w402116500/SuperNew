"""Shared structured-chunk metadata contract for indexing and evaluation traces."""

from __future__ import annotations

from typing import Any


STRUCTURED_CHUNK_METADATA_FIELDS = (
    "heading_path",
    "heading_level",
    "source_start_index",
    "source_end_index",
    "content_kind",
    "previous_chunk_id",
    "next_chunk_id",
    "chunking_strategy",
    "chunking_config_hash",
)


def structured_chunk_metadata(document: dict[str, Any]) -> dict[str, Any]:
    """Return only populated dynamic Milvus fields for structured Markdown chunks."""
    return {
        field: document[field]
        for field in STRUCTURED_CHUNK_METADATA_FIELDS
        if field in document and document[field] is not None
    }
