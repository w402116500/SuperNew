"""Preparation of staged uploads before they replace an indexed document."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend.indexing.document_loader import DocumentLoader
from backend.indexing.document_types import document_type_for_filename, is_rich_document
from backend.indexing.mineru_client import MineruArtifactBundle, MineruClient


ProgressCallback = Callable[[str, int, str], None]


@dataclass(frozen=True)
class PreparedDocument:
    """Validated chunks and optional MinerU bundle awaiting promotion."""

    parent_chunks: list[dict]
    leaf_chunks: list[dict]
    artifact_bundle: MineruArtifactBundle | None


class DocumentIngestionService:
    """Prepare staged source files without mutating the active knowledge base."""

    def __init__(self, loader: DocumentLoader, mineru_client: MineruClient):
        self._loader = loader
        self._mineru_client = mineru_client

    def prepare(
        self,
        staged_source_path: str | Path,
        filename: str,
        final_source_path: str | Path,
        staging_dir: str | Path,
        progress: ProgressCallback | None = None,
    ) -> PreparedDocument:
        """Convert and chunk a staged upload before old data is removed."""
        source = Path(staged_source_path)
        canonical_source = str(Path(final_source_path))
        artifact_bundle: MineruArtifactBundle | None = None

        if is_rich_document(filename):
            self._report(progress, "mineru", 5, "正在通过 MinerU 转换为 Markdown")
            artifact_bundle = self._mineru_client.convert_to_bundle(source, Path(staging_dir) / "parsed")
            self._report(progress, "mineru", 100, "MinerU Markdown 与解析产物已生成")
            self._report(progress, "chunk", 10, "正在对 MinerU Markdown 执行三级分块")
            documents = self._loader.load_parsed_markdown(
                str(artifact_bundle.markdown_path),
                filename,
                canonical_source,
                document_type_for_filename(filename),
            )
        else:
            self._report(progress, "mineru", 100, "原生文本无需 MinerU 转换")
            self._report(progress, "chunk", 10, "正在执行三级分块")
            documents = self._loader.load_document(str(source), filename, canonical_source)

        if not documents:
            raise ValueError("文档处理失败，未能提取内容")

        parent_chunks = [chunk for chunk in documents if int(chunk.get("chunk_level", 0) or 0) in (1, 2)]
        leaf_chunks = [chunk for chunk in documents if int(chunk.get("chunk_level", 0) or 0) == 3]
        if not leaf_chunks:
            raise ValueError("文档处理失败，未生成可检索叶子分块")

        self._report(
            progress,
            "chunk",
            100,
            f"三级分块完成：父级分块 {len(parent_chunks)} 个，叶子分块 {len(leaf_chunks)} 个",
        )
        return PreparedDocument(parent_chunks, leaf_chunks, artifact_bundle)

    @staticmethod
    def _report(progress: ProgressCallback | None, step: str, percent: int, message: str) -> None:
        if progress:
            progress(step, percent, message)
