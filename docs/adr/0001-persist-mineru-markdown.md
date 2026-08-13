# ADR 0001: Persist MinerU Markdown as the Ingestion Source

**Status:** Accepted

## Context

MinerU converts rich documents into structured Markdown before the existing L1/L2/L3 chunking and indexing pipeline. Keeping only the final vector chunks would make it difficult to distinguish parsing defects from chunking or retrieval defects, and would require another MinerU call to rebuild an index.

## Decision

Keep the original uploaded file and persist MinerU's `md_text` output as Parsed Markdown. For MinerU-supported files, the Parsed Markdown is the only input to chunking and indexing. Re-indexing uses the persisted Markdown rather than invoking MinerU again.

Deleting a document must remove its source file, Parsed Markdown, PostgreSQL parent chunks, Redis cache entries, and Milvus leaf chunks as one logical operation.

MinerU conversion failures fail the upload job at an explicit parsing step. The system must not silently fall back to a lower-fidelity parser. A development-only bypass, if introduced, must be explicit in configuration and visible in the upload result.

PDF, DOCX, PPTX, XLSX, and supported images are Rich Documents and must use MinerU. Markdown and TXT enter the existing text path; HTML enters the existing semantic HTML path. Legacy `.doc` and `.xls` files are not supported and must be converted to DOCX or XLSX before upload.

Persist the complete Parsed Artifact Bundle: Markdown, extracted images, and `content_list_json`. In the first release, only the Markdown body is chunked and indexed. Images and `content_list_json` are retained for preview, audit, and later multimodal retrieval; they are not separately vectorized or used as answer evidence.

The filename remains the document identity in the first release. A same-name upload performs a complete replacement: remove the old source file, Parsed Artifact Bundle, PostgreSQL parent chunks, Redis cache entries, and Milvus leaf chunks before persisting and indexing the new upload. The system does not keep document history or provide rollback yet.

MinerU parameters are owned by a server-side Parsing Profile, configured through environment variables. The first release does not expose OCR, page limits, backend, language, effort, formula, table, or image-analysis options in the upload UI. The effective profile must be recorded with the Parsed Artifact Bundle so a later re-index can be explained and reproduced.

The first release uses the existing FastAPI `BackgroundTasks` model. The upload UI must expose distinct progress steps for source upload, replacement cleanup, MinerU conversion, Markdown chunking, parent-chunk storage, and vector storage. Jobs and their progress remain in process memory and are not recovered after an application restart; durable queueing and recovery are deferred.

Same-name updates use Replacement Staging. Persist the new source file and prepare its Parsed Artifact Bundle and chunks in a temporary location before deleting the existing indexed document. If MinerU conversion or Markdown validation fails, clean up the temporary data and preserve the old source, artifacts, and index. Only a successfully prepared replacement may start the old-version cleanup and promotion sequence.

## Consequences

The system gains an inspectable parsing artifact and reproducible re-indexing. It must define stable storage paths, track the relationship between source files and Markdown artifacts, and handle partial failures without leaving ambiguous artifacts.
