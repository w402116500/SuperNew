"""MinerU Gradio API client for Rich Document conversion."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
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
    provider: str = "gradio"
    api_base_url: str = "https://mineru.net/api/v1/agent"
    api_key: str = ""
    poll_interval_seconds: float = 3.0
    api_timeout_seconds: float = 60.0

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
            provider=os.getenv("MINERU_PROVIDER", "gradio").strip().lower() or "gradio",
            api_base_url=(
                os.getenv("MINERU_API_BASE_URL", "https://mineru.net/api/v1/agent").strip()
                or "https://mineru.net/api/v1/agent"
            ),
            api_key=os.getenv("MINERU_API_KEY", "").strip(),
            poll_interval_seconds=_positive_float("MINERU_POLL_INTERVAL_SECONDS", 3.0),
            api_timeout_seconds=_positive_float("MINERU_API_TIMEOUT_SECONDS", 60.0),
        )

    def profile(self) -> dict[str, Any]:
        """Return serializable settings stored next to a parsed artifact bundle."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "api_base_url": self.api_base_url,
            "end_pages": self.end_pages,
            "is_ocr": self.is_ocr,
            "formula_enable": self.formula_enable,
            "table_enable": self.table_enable,
            "image_analysis": self.image_analysis,
            "effort": self.effort,
            "language": self.language,
            "backend": self.backend,
            "engine_url": self.engine_url,
            "poll_interval_seconds": self.poll_interval_seconds,
            "api_timeout_seconds": self.api_timeout_seconds,
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

        self._validate_provider()
        temporary_export: Path | None = None
        if self._settings.provider == "official_api":
            markdown, content_list_json, export_path = self._convert_official_api(source)
            temporary_export = export_path
        else:
            markdown, content_list_json, export_path = self._convert_gradio(source)

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

        try:
            persisted_export = self._copy_export(export_path, target)
        finally:
            if temporary_export is not None:
                temporary_export.unlink(missing_ok=True)
        return MineruArtifactBundle(
            directory=target,
            markdown_path=markdown_path,
            content_list_path=content_list_path,
            profile_path=profile_path,
            export_path=persisted_export,
            markdown=markdown,
        )

    def _validate_provider(self) -> None:
        if self._settings.provider not in {"gradio", "official_api"}:
            raise RuntimeError(
                "Unsupported MINERU_PROVIDER; expected 'gradio' or 'official_api'"
            )
        if self._settings.provider == "official_api" and not self._settings.api_key:
            raise RuntimeError(
                "MINERU_API_KEY is required when MINERU_PROVIDER=official_api"
            )

    def _convert_gradio(self, source: Path) -> tuple[str, str, Path | None]:
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
        return self._validate_result(result)

    def _convert_official_api(self, source: Path) -> tuple[str, str, Path | None]:
        base_url = self._settings.api_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        payload: dict[str, Any] = {
            "file_name": source.name,
            "language": self._official_language(),
            "enable_table": self._settings.table_enable,
            "is_ocr": self._settings.is_ocr,
            "enable_formula": self._settings.formula_enable,
        }
        if source.suffix.lower() == ".pdf" and self._settings.end_pages:
            payload["page_range"] = f"1-{self._settings.end_pages}"

        create_url = f"{base_url}/parse/file"
        try:
            response = requests.post(
                create_url,
                json=payload,
                headers=headers,
                timeout=self._settings.api_timeout_seconds,
            )
            task_data = self._official_response_data(response, "create task")
            task_id = task_data.get("task_id")
            upload_url = task_data.get("file_url")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("MinerU official API create task returned no task_id")
            if not isinstance(upload_url, str) or not upload_url:
                raise RuntimeError("MinerU official API create task returned no file_url")

            with source.open("rb") as file_handle:
                # MinerU 返回的 OSS 地址已经包含完整签名；额外设置 Content-Type
                # 会改变签名计算，导致 OSS 返回 SignatureDoesNotMatch。
                upload_response = requests.put(
                    upload_url,
                    data=file_handle,
                    headers={},
                    timeout=self._settings.api_timeout_seconds,
                )
            upload_response.raise_for_status()

            deadline = time.monotonic() + self._settings.api_timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"MinerU official API task timed out after {self._settings.api_timeout_seconds:g} seconds"
                    )
                status_response = requests.get(
                    f"{base_url}/parse/{task_id}",
                    headers=headers,
                    timeout=min(self._settings.api_timeout_seconds, remaining),
                )
                status_data = self._official_response_data(status_response, "poll task")
                state = str(status_data.get("state", "")).strip().lower()
                if state == "done":
                    return self._download_official_result(status_data)
                if state == "failed":
                    detail = status_data.get("err_msg") or status_data.get("message") or "unknown error"
                    raise RuntimeError(f"MinerU official API task failed: {detail}")
                time.sleep(min(self._settings.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            raise RuntimeError(f"MinerU official API request failed: {exc}") from exc
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"MinerU official API conversion failed: {exc}") from exc

    def _official_language(self) -> str:
        language = self._settings.language.strip().lower()
        if not language:
            return "ch"
        return language.split("(", 1)[0].strip().split()[0] or "ch"

    @staticmethod
    def _official_response_data(response: requests.Response, phase: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"MinerU official API {phase} request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"MinerU official API {phase} returned an invalid response")
        code = body.get("code")
        if code not in (None, 0, "0"):
            message = body.get("msg") or body.get("message") or "unknown error"
            raise RuntimeError(f"MinerU official API {phase} failed: {message}")
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise RuntimeError(f"MinerU official API {phase} returned invalid data")
        return data

    def _download_official_result(self, data: dict[str, Any]) -> tuple[str, str, Path | None]:
        markdown_url = data.get("markdown_url")
        if not isinstance(markdown_url, str) or not markdown_url:
            raise RuntimeError("MinerU official API completed without markdown_url")
        markdown_response = requests.get(
            markdown_url,
            timeout=self._settings.api_timeout_seconds,
        )
        try:
            markdown_response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"MinerU official API Markdown download failed: {exc}") from exc
        markdown = markdown_response.text
        if not isinstance(markdown, str) or not markdown.strip():
            raise RuntimeError("MinerU official API returned empty Markdown")

        content_list_json = "[]"
        content_list_url = data.get("content_list_url")
        if isinstance(content_list_url, str) and content_list_url:
            content_response = requests.get(
                content_list_url,
                timeout=self._settings.api_timeout_seconds,
            )
            try:
                content_response.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"MinerU official API content list download failed: {exc}") from exc
            content_list_json = content_response.text

        export_url = next(
            (
                data.get(name)
                for name in ("full_zip_url", "zip_url", "export_url")
                if isinstance(data.get(name), str) and data.get(name)
            ),
            None,
        )
        export_path: Path | None = None
        if export_url:
            export_path = self._download_export(export_url)
            if content_list_json == "[]":
                content_list_json = self._content_list_from_export(export_path)
        return markdown.strip() + "\n", content_list_json, export_path

    def _download_export(self, export_url: str) -> Path:
        response = requests.get(export_url, timeout=self._settings.api_timeout_seconds)
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"MinerU official API export download failed: {exc}") from exc
        with tempfile.NamedTemporaryFile(prefix="mineru-export-", suffix=".zip", delete=False) as handle:
            handle.write(response.content)
            return Path(handle.name)

    @staticmethod
    def _content_list_from_export(export_path: Path) -> str:
        if not zipfile.is_zipfile(export_path):
            return "[]"
        with ZipFile(export_path) as archive:
            member = next(
                (item for item in archive.namelist() if item.lower().endswith("content_list.json")),
                None,
            )
            if member is None:
                return "[]"
            return archive.read(member).decode("utf-8")

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


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _extract_file_path(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path:
            return Path(path)
    return None
