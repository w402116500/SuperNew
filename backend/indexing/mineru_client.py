"""MinerU Gradio API client for Rich Document conversion."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gradio_client import Client, handle_file

from backend.indexing.document_types import RICH_DOCUMENT_SUFFIXES, is_rich_document


@dataclass(frozen=True)
class MineruSettings:
    """Server-owned MinerU parsing profile."""

    base_url: str
    end_pages: int
    is_ocr: bool
    formula_enable: bool
    table_enable: bool
    image_analysis: bool
    effort: str
    language: str
    backend: str
    engine_url: str

    @classmethod
    def from_env(cls) -> "MineruSettings":
        return cls(
            base_url=os.getenv("MINERU_URL", "http://127.0.0.1:7860").rstrip("/"),
            end_pages=_positive_int("MINERU_END_PAGES", 1000),
            is_ocr=_read_bool("MINERU_FORCE_OCR", False),
            formula_enable=_read_bool("MINERU_FORMULA_ENABLE", True),
            table_enable=_read_bool("MINERU_TABLE_ENABLE", True),
            image_analysis=_read_bool("MINERU_IMAGE_ANALYSIS", True),
            effort=os.getenv("MINERU_EFFORT", "medium").strip() or "medium",
            language=(
                os.getenv(
                    "MINERU_LANGUAGE",
                    "ch (Chinese, English, Japanese, Chinese Traditional, Latin)",
                ).strip()
                or "ch (Chinese, English, Japanese, Chinese Traditional, Latin)"
            ),
            backend=os.getenv("MINERU_BACKEND", "hybrid-engine").strip() or "hybrid-engine",
            engine_url=os.getenv("MINERU_ENGINE_URL", "http://localhost:30000").strip(),
        )

    def profile(self) -> dict[str, Any]:
        """Return serializable settings stored next to a parsed artifact bundle."""
        return {
            "base_url": self.base_url,
            "end_pages": self.end_pages,
            "is_ocr": self.is_ocr,
            "formula_enable": self.formula_enable,
            "table_enable": self.table_enable,
            "image_analysis": self.image_analysis,
            "effort": self.effort,
            "language": self.language,
            "backend": self.backend,
            "engine_url": self.engine_url,
        }


@dataclass(frozen=True)
class MineruArtifactBundle:
    """Persisted MinerU output for one source document."""

    directory: Path
    markdown_path: Path
    content_list_path: Path
    profile_path: Path
    export_path: Path | None
    markdown: str


class MineruClient:
    """Convert a Rich Document with MinerU and persist an inspectable bundle."""

    def __init__(self, settings: MineruSettings | None = None):
        self._settings = settings or MineruSettings.from_env()

    @property
    def settings(self) -> MineruSettings:
        return self._settings

    @staticmethod
    def supports(filename: str) -> bool:
        return is_rich_document(filename)

    def convert_to_bundle(self, source_path: str | Path, bundle_dir: str | Path) -> MineruArtifactBundle:
        """Convert a local source file and persist MinerU's Markdown and artifacts.

        Raises:
            RuntimeError: MinerU is unavailable, returns malformed data, or produces empty Markdown.
        """
        source = Path(source_path)
        target = Path(bundle_dir)
        if not source.is_file():
            raise ValueError(f"MinerU source file does not exist: {source}")

        try:
            client = Client(self._settings.base_url)
            result = client.predict(
                file_path=handle_file(str(source.resolve())),
                end_pages=self._settings.end_pages,
                is_ocr=self._settings.is_ocr,
                formula_enable=self._settings.formula_enable,
                table_enable=self._settings.table_enable,
                image_analysis=self._settings.image_analysis,
                effort=self._settings.effort,
                language=self._settings.language,
                backend=self._settings.backend,
                url=self._settings.engine_url,
                api_name="/convert_to_markdown_stream",
            )
        except Exception as exc:
            raise RuntimeError(f"MinerU conversion failed: {exc}") from exc

        markdown, content_list_json, export_path = self._validate_result(result)
        target.mkdir(parents=True, exist_ok=True)
        # 固定产物命名让后续排障、重建索引和同名文件替换不依赖 MinerU 的临时返回路径。
        markdown_path = target / "document.md"
        content_list_path = target / "content_list.json"
        profile_path = target / "profile.json"
        markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
        content_list_path.write_text(content_list_json, encoding="utf-8", newline="\n")
        profile_path.write_text(
            json.dumps(self._settings.profile(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        persisted_export = self._copy_export(export_path, target)
        return MineruArtifactBundle(
            directory=target,
            markdown_path=markdown_path,
            content_list_path=content_list_path,
            profile_path=profile_path,
            export_path=persisted_export,
            markdown=markdown,
        )

    @staticmethod
    def _validate_result(result: Any) -> tuple[str, str, Path | None]:
        if not isinstance(result, (tuple, list)) or len(result) < 5:
            raise RuntimeError("MinerU returned an unexpected response shape")

        markdown = result[3]
        if not isinstance(markdown, str) or not markdown.strip():
            raise RuntimeError("MinerU returned empty Markdown")

        content_list_json = result[4]
        if not isinstance(content_list_json, str):
            content_list_json = json.dumps(content_list_json, ensure_ascii=False, indent=2)

        export_path = _extract_file_path(result[1])
        return markdown.strip() + "\n", content_list_json, export_path

    @staticmethod
    def _copy_export(export_path: Path | None, target: Path) -> Path | None:
        if export_path is None or not export_path.exists():
            return None

        destination = target / "mineru-export"
        if zipfile.is_zipfile(export_path):
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(export_path) as archive:
                destination_root = destination.resolve()
                # 必须在 extractall 前检查每个成员，防止 "../" 路径写出当前文档产物目录。
                for member in archive.infolist():
                    member_path = (destination / member.filename).resolve()
                    if member_path != destination_root and destination_root not in member_path.parents:
                        raise RuntimeError("MinerU export archive contains an unsafe path")
                archive.extractall(destination)
            return destination
        if export_path.is_dir():
            shutil.copytree(export_path, destination, dirs_exist_ok=True)
            return destination

        destination.mkdir(parents=True, exist_ok=True)
        copied_file = destination / export_path.name
        shutil.copy2(export_path, copied_file)
        return copied_file


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _extract_file_path(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path:
            return Path(path)
    return None
