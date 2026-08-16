"""EnterpriseRAG 中文派生语料的可恢复翻译与保真校验。"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests


PROMPT_VERSION = "enterprise-zh-v2"
TRANSLATION_SEGMENT_MAX_CHARS = 500
_TIME_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<value>\d+(?:[.,]\d+)*)\s*"
    r"(?P<unit>milliseconds?|ms|seconds?|secs?|sec|minutes?|mins?|min|"
    r"hours?|hrs?|hr|h|days?|day|d|毫秒|秒|分钟|分|小时|天)(?![A-Za-z])",
    re.IGNORECASE,
)
_LITERAL_TOKEN_RE = re.compile(r"`[^`\n]+`|https?://[^\s)]+")
_TECHNICAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*\s*(?:KiB|MiB|GiB|KB|MB|GB|ms|us|μs|%)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MASKABLE_TOKEN_RE = re.compile(
    r"`[^`\n]+`|https?://[^\s)]+|"
    r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*\s*(?:KiB|MiB|GiB|KB|MB|GB|ms|us|μs|%)(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*",
    re.IGNORECASE,
)
_TIME_UNIT_ALIASES = {
    "millisecond": ("millisecond", "milliseconds", "ms", "毫秒"),
    "second": ("second", "seconds", "sec", "secs", "秒"),
    "minute": ("minute", "minutes", "min", "mins", "分钟", "分"),
    "hour": ("hour", "hours", "hr", "hrs", "h", "小时"),
    "day": ("day", "days", "d", "天"),
}


class TranslationError(RuntimeError):
    """翻译失败或译文未通过硬事实校验。"""


@dataclass(frozen=True)
class TranslationConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 90.0
    max_retries: int = 3
    whole_document_max_chars: int = 1000

    @classmethod
    def from_env(cls) -> "TranslationConfig":
        base_url = (os.getenv("BASE_URL") or "").strip()
        api_key = (os.getenv("ARK_API_KEY") or "").strip()
        model = (os.getenv("TRANSLATION_MODEL") or os.getenv("MODEL") or "").strip()
        if not (base_url and api_key and model):
            raise TranslationError("中文语料翻译需要 BASE_URL、ARK_API_KEY 和 TRANSLATION_MODEL/MODEL")
        try:
            timeout = max(float(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "150")), 1.0)
        except ValueError:
            timeout = 150.0
        try:
            whole_document_max_chars = max(
                int(os.getenv("TRANSLATION_WHOLE_DOCUMENT_MAX_CHARS", "1000")),
                0,
            )
        except ValueError:
            whole_document_max_chars = 1000
        return cls(
            base_url,
            api_key,
            model,
            timeout_seconds=timeout,
            whole_document_max_chars=whole_document_max_chars,
        )


def _protected_tokens(text: str) -> list[str]:
    literals = [
        token.rstrip(".,;:!?，。！？；：") if token.startswith("http") else token
        for token in _LITERAL_TOKEN_RE.findall(text or "")
    ]
    return literals + _TECHNICAL_TOKEN_RE.findall(text or "")


def _numeric_tokens(text: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*", text or "")


def _canonical_technical_token(token: str) -> str:
    return re.sub(r"\s+", "", token).lower()


def _mask_protected_tokens(text: str) -> tuple[str, dict[str, str]]:
    """用稳定占位符遮蔽不可翻译字面量，避免模型省略 URL、代码或数字。"""
    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        suffix = ""
        if token.startswith("http"):
            stripped = token.rstrip(".,;:!?，。！？；：")
            suffix = token[len(stripped):]
            token = stripped
        placeholder = f"[[RAG_KEEP_{len(replacements) + 1}]]"
        replacements[placeholder] = token
        return placeholder + suffix

    return _MASKABLE_TOKEN_RE.sub(replace, text or ""), replacements


def _restore_protected_tokens(text: str, replacements: dict[str, str]) -> tuple[str, list[str]]:
    missing = [placeholder for placeholder in replacements if placeholder not in text]
    restored = text
    for placeholder, token in replacements.items():
        restored = restored.replace(placeholder, token)
    return restored, missing


def _canonical_time_unit(value: str) -> str:
    unit = value.strip().lower()
    for canonical, aliases in _TIME_UNIT_ALIASES.items():
        if unit in aliases:
            return canonical
    return unit


def _time_tokens(text: str) -> list[tuple[str, str]]:
    return [
        (match.group("value"), _canonical_time_unit(match.group("unit")))
        for match in _TIME_TOKEN_RE.finditer(text or "")
    ]


def _split_translation_segments(text: str, max_characters: int = TRANSLATION_SEGMENT_MAX_CHARS) -> list[str]:
    """按换行优先拆分长文，保证合并后与原始文本字节顺序一致。"""
    if max_characters < 1:
        raise ValueError("max_characters 必须大于 0")
    if len(text) <= max_characters:
        return [text]

    segments: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_characters:
            if current:
                segments.append(current)
                current = ""
            split_at = line.rfind(" ", 0, max_characters)
            split_at = split_at + 1 if split_at > 0 else max_characters
            segments.append(line[:split_at])
            line = line[split_at:]
        if current and len(current) + len(line) > max_characters:
            segments.append(current)
            current = ""
        current += line
    if current:
        segments.append(current)
    return segments


def validate_translation(source: str, translated: str) -> list[str]:
    """检查翻译是否保留关键数字、代码片段、URL 与时间语义。"""
    if not translated.strip():
        return ["empty_translation"]

    # 时间单位可以自然地翻译成中文，例如 30 minutes -> 30 分钟；代码、URL 和技术单位仍要求原样保留。
    translated_time_tokens = Counter(_time_tokens(translated))
    missing = []
    for (value, unit), count in Counter(_time_tokens(source)).items():
        if translated_time_tokens[(value, unit)] < count:
            missing.extend([f"time:{value}{unit}"] * (count - translated_time_tokens[(value, unit)]))

    source_numeric = Counter(_numeric_tokens(source))
    translated_numeric = Counter(_numeric_tokens(translated))
    for token, count in source_numeric.items():
        if translated_numeric[token] < count:
            missing.extend([f"number:{token}"] * (count - translated_numeric[token]))

    source_literals = Counter(_protected_tokens(source))
    translated_literals = Counter(_protected_tokens(translated))
    for token, count in source_literals.items():
        canonical = _canonical_technical_token(token) if token[:1].isdigit() else token
        translated_count = sum(
            value
            for candidate, value in translated_literals.items()
            if (_canonical_technical_token(candidate) if candidate[:1].isdigit() else candidate) == canonical
        )
        if translated_count < count:
            missing.extend([f"literal:{token}"] * (count - translated_count))
    return [f"missing_protected_token:{token}" for token in missing]


def _cache_key(text: str, target_language: str, config: TranslationConfig) -> str:
    payload = "\n".join([
        sha256(text.encode("utf-8")).hexdigest(),
        target_language,
        config.model,
        PROMPT_VERSION,
    ])
    return sha256(payload.encode("utf-8")).hexdigest()


def _extract_content(response: requests.Response) -> str:
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content).strip()


class TranslationClient:
    """使用 OpenAI 兼容接口整篇翻译，并保留可恢复缓存和事实校验。"""

    def __init__(self, config: TranslationConfig, cache_dir: Path):
        self.config = config
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.api_key}"})

    def close(self) -> None:
        """释放当前翻译请求会话的网络连接。"""
        self.session.close()

    def _read_cache(self, path: Path, text: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        cached = json.loads(path.read_text(encoding="utf-8"))
        source_hash = sha256(text.encode("utf-8")).hexdigest()
        return cached if cached.get("source_hash") == source_hash else None

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        temporary_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(path)

    def _translate_segment(self, text: str, kind: str) -> str:
        segment_dir = self.cache_dir / "segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        cache_path = segment_dir / f"{_cache_key(text, 'zh-CN-segment', self.config)}.json"
        cached = self._read_cache(cache_path, text)
        if cached:
            return str(cached["text"])

        masked_text, replacements = _mask_protected_tokens(text)
        base_prompt = (
            "将下面的企业知识库内容翻译成简体中文。只输出译文，不要解释、总结或添加事实。"
            "保留 Markdown 结构、API 路径、指标名和专有名词；"
            "内容中的 [[RAG_KEEP_n]] 是不可翻译占位符，必须逐字原样保留；"
            "时间单位可译为中文，但数值和时间量级必须一致；"
            "不要输出代码围栏包裹的额外说明。内容类型：{kind}。\n\n{content}"
        )
        last_error = ""
        retry_literals: list[str] = []
        for attempt in range(self.config.max_retries):
            prompt = base_prompt.format(kind=kind, content=masked_text)
            if retry_literals:
                prompt += (
                    "\n\n上一次译文遗漏了下列受保护字面量。请重新翻译，并将它们逐字原样保留：\n"
                    + "\n".join(f"- {literal}" for literal in retry_literals)
                )
            try:
                response = self.session.post(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": self.config.model,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                raw_translated = _extract_content(response)
                translated, missing_placeholders = _restore_protected_tokens(raw_translated, replacements)
                missing_placeholders = [
                    placeholder
                    for placeholder in missing_placeholders
                    if replacements[placeholder] not in raw_translated
                ]
                if missing_placeholders:
                    retry_literals = [
                        f"{placeholder}（原值：{replacements[placeholder]}）"
                        for placeholder in missing_placeholders
                    ]
                    raise TranslationError("；".join(f"missing_placeholder:{item}" for item in missing_placeholders))
                errors = validate_translation(text, translated)
                if errors:
                    retry_literals = _protected_tokens(text) + _numeric_tokens(text)
                    raise TranslationError("；".join(errors))
                self._write_cache(cache_path, {
                    "source_hash": sha256(text.encode("utf-8")).hexdigest(),
                    "translated_hash": sha256(translated.encode("utf-8")).hexdigest(),
                    "target_language": "zh-CN",
                    "kind": kind,
                    "model": self.config.model,
                    "prompt_version": PROMPT_VERSION,
                    "temperature": 0,
                    "qa_status": "auto_pass",
                    "text": translated,
                })
                return translated
            except (requests.RequestException, KeyError, TypeError, ValueError, TranslationError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < self.config.max_retries:
                    time.sleep(2**attempt)
        raise TranslationError(f"翻译失败（{kind}）：{last_error}")

    def translate(
        self,
        text: str,
        *,
        kind: str = "document",
        segment_workers: int = 1,
    ) -> dict[str, Any]:
        """短文整篇发送，长文分段发送；保留 ``segment_workers`` 以兼容旧调用方。"""
        key = _cache_key(text, "zh-CN", self.config)
        cache_path = self.cache_dir / f"{key}.json"
        cached = self._read_cache(cache_path, text)
        if cached:
            return cached

        # 上下文窗口只解决“能否放下”，不保证长请求的服务端响应时间；短文整篇保留语义，
        # 长文仍按小段请求，避免单次读取超时。segment_workers 仅用于长文的段内并发。
        if len(text) <= self.config.whole_document_max_chars:
            segments = [text]
        else:
            segments = _split_translation_segments(text)

        def translate_one(segment: str) -> str:
            worker = self if segment_workers <= 1 or len(segments) == 1 else TranslationClient(self.config, self.cache_dir)
            try:
                translated = worker._translate_segment(segment, kind)
                # 模型响应会 strip 尾部空白；补回源段的换行以免拼接时破坏 Markdown 段落边界。
                return translated + ("\n" if segment.endswith("\n") else "")
            finally:
                if worker is not self:
                    worker.close()

        if segment_workers > 1 and len(segments) > 1:
            with ThreadPoolExecutor(max_workers=segment_workers, thread_name_prefix="translation-segment") as executor:
                translated_parts = list(executor.map(translate_one, segments))
        else:
            translated_parts = [translate_one(segment) for segment in segments]
        translated = "".join(translated_parts)
        errors = validate_translation(text, translated)
        if errors:
            raise TranslationError("；".join(errors))

        result = {
            "source_hash": sha256(text.encode("utf-8")).hexdigest(),
            "translated_hash": sha256(translated.encode("utf-8")).hexdigest(),
            "target_language": "zh-CN",
            "kind": kind,
            "model": self.config.model,
            "prompt_version": PROMPT_VERSION,
            "temperature": 0,
            "translation_strategy": (
                "whole_document"
                if len(text) <= self.config.whole_document_max_chars
                else "segmented"
            ),
            "segment_count": len(translated_parts),
            "qa_status": "auto_pass",
            "text": translated,
        }
        self._write_cache(cache_path, result)
        return result
