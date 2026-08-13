# ADR 0001: Persist MinerU Markdown as the Ingestion Source

**Status:** Accepted

## Context

MinerU converts rich documents into structured Markdown before the existing L1/L2/L3 chunking and indexing pipeline. Keeping only the final vector chunks would make it difficult to distinguish parsing defects from chunking or retrieval defects, and would require another MinerU call to rebuild an index.

## Decision

Keep the original uploaded file and persist MinerU's `md_text` output as Parsed Markdown. For MinerU-supported files, the Parsed Markdown is the only input to chunking and indexing. Re-indexing uses the persisted Markdown rather than invoking MinerU again.

Deleting a document must remove its source file, Parsed Markdown, PostgreSQL parent chunks, Redis cache entries, and Milvus leaf chunks as one logical operation.

MinerU conversion failures fail the upload job at an explicit parsing step. The system must not silently fall back to a lower-fidelity parser. A development-only bypass, if introduced, must be explicit in configuration and visible in the upload result.

## Consequences

The system gains an inspectable parsing artifact and reproducible re-indexing. It must define stable storage paths, track the relationship between source files and Markdown artifacts, and handle partial failures without leaving ambiguous artifacts.
