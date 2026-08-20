"""Semantic boundary planning for long Markdown prose atoms.

The planner deliberately knows nothing about Milvus or answer generation. It
only turns long paragraph text into an auditable list of sentence boundaries.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable


SEMANTIC_MARKDOWN_CHUNKING_STRATEGY = "markdown_header_semantic_fallback_v1"
SEMANTIC_SENTENCE_WINDOW = 3
SEMANTIC_MIN_L3_CHARS = 300
SEMANTIC_BREAKPOINT_PERCENTILE = 20
SEMANTIC_PARAGRAPH_MIN_CHARS = 800

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])(?:[\"'\)\]]+)?(?=\s|$)")


@dataclass(frozen=True)
class SentenceSpan:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class SemanticChunkingConfig:
    """固定参数快照，供 loader 计算稳定配置哈希。"""

    sentence_window: int = SEMANTIC_SENTENCE_WINDOW
    min_l3_chars: int = SEMANTIC_MIN_L3_CHARS
    breakpoint_percentile: float = float(SEMANTIC_BREAKPOINT_PERCENTILE)
    level_sizes: tuple[int, int, int] = (2400, 1600, 800)

    def as_dict(self) -> dict:
        return {
            "sentence_window": self.sentence_window,
            "min_l3_chars": self.min_l3_chars,
            "breakpoint_percentile": self.breakpoint_percentile,
            "level_sizes": list(self.level_sizes),
        }


def split_english_sentences(text: str) -> list[SentenceSpan]:
    """Split prose into complete sentence spans while retaining offsets."""
    value = str(text)
    if not value.strip():
        return []
    spans: list[SentenceSpan] = []
    cursor = 0
    for match in _SENTENCE_END_RE.finditer(value):
        end = match.end()
        raw = value[cursor:end]
        left_trim = len(raw) - len(raw.lstrip())
        right_trim = len(raw) - len(raw.rstrip())
        start = cursor + left_trim
        final_end = end - right_trim
        if final_end > start:
            spans.append(SentenceSpan(start, final_end, value[start:final_end]))
        cursor = end
    tail = value[cursor:]
    left_trim = len(tail) - len(tail.lstrip())
    start = cursor + left_trim
    if value[start:].strip():
        final_end = len(value.rstrip())
        spans.append(SentenceSpan(start, final_end, value[start:final_end]))
    return spans


def _dot(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if not left_values or len(left_values) != len(right_values):
        raise ValueError("语义断点向量维度不一致")
    score = sum(float(a) * float(b) for a, b in zip(left_values, right_values))
    if not math.isfinite(score):
        raise ValueError("语义断点相似度不是有限数字")
    return score


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("没有可计算的语义断点分数")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    ratio = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * ratio


def _window(spans: list[SentenceSpan], start: int, end: int) -> str:
    return " ".join(span.text for span in spans[start:end]).strip()


def plan_paragraph_boundaries(
    paragraph: dict,
    embed_texts: Callable[[list[str]], list[list[float]]],
    *,
    sentence_window: int = SEMANTIC_SENTENCE_WINDOW,
    min_l3_chars: int = SEMANTIC_MIN_L3_CHARS,
    breakpoint_percentile: float = SEMANTIC_BREAKPOINT_PERCENTILE,
    paragraph_min_chars: int = SEMANTIC_PARAGRAPH_MIN_CHARS,
) -> dict:
    """Create an auditable semantic plan for one paragraph.

    The returned boundary offsets are relative to ``paragraph["text"]``.
    ``semantic_not_applicable`` is an explicit, safe fallback; provider or
    malformed-vector failures are raised so prepare cannot mislabel a run.
    """
    text = str(paragraph.get("text") or "")
    result = {
        "document_id": str(paragraph.get("document_id") or ""),
        "source_start": int(paragraph.get("source_start", 0)),
        "source_end": int(paragraph.get("source_end", 0)),
        "paragraph_chars": len(text),
        "sentence_ranges": [],
        "candidate_boundaries": [],
        "selected_boundaries": [],
        "boundary_scores": [],
        "parameters": {
            "sentence_window": sentence_window,
            "min_l3_chars": min_l3_chars,
            "breakpoint_percentile": breakpoint_percentile,
            "paragraph_min_chars": paragraph_min_chars,
        },
        "decision": "semantic_not_applicable",
        "fallback_reason": "",
    }
    if len(text) <= paragraph_min_chars:
        result["fallback_reason"] = "paragraph_below_threshold"
        return result
    if sentence_window < 1 or min_l3_chars < 1:
        raise ValueError("语义断点参数必须为正数")
    spans = split_english_sentences(text)
    result["sentence_ranges"] = [{"start": span.start, "end": span.end} for span in spans]
    if len(spans) < max(2, sentence_window + 1):
        result["fallback_reason"] = "not_enough_complete_sentences"
        return result
    if any(span.end - span.start > paragraph_min_chars for span in spans):
        # A paragraph may contain one generated/log-style sentence that is
        # longer than the L3 limit by itself. We cannot keep both the sentence
        # intact and the size contract, so mark it explicitly for the loader's
        # no-overlap safe recursive fallback instead of producing partial
        # semantic pieces that would later be duplicated under two parents.
        result["fallback_reason"] = "sentence_exceeds_l3_limit"
        return result

    windows: list[str] = []
    pairs: list[tuple[int, str, str]] = []
    for boundary in range(1, len(spans)):
        left = _window(spans, max(0, boundary - sentence_window), boundary)
        right = _window(spans, boundary, min(len(spans), boundary + sentence_window))
        pairs.append((boundary, left, right))
        windows.extend([left, right])
    vectors = embed_texts(windows)
    if len(vectors) != len(windows):
        raise ValueError("语义断点 Embedding 返回数量与输入不一致")
    scores: list[tuple[int, int, float]] = []
    for index, (boundary, _, _) in enumerate(pairs):
        score = _dot(vectors[index * 2], vectors[index * 2 + 1])
        offset = spans[boundary - 1].end
        scores.append((boundary, offset, score))
    threshold = _percentile([score for _, _, score in scores], breakpoint_percentile)
    result["boundary_scores"] = [
        {"sentence_index": boundary, "offset": offset, "score": score}
        for boundary, offset, score in scores
    ]
    candidates = [item for item in scores if item[2] <= threshold]
    result["candidate_boundaries"] = [
        {"sentence_index": boundary, "offset": offset, "score": score}
        for boundary, offset, score in candidates
    ]

    selected: list[tuple[int, int, float, str]] = []
    previous_offset = 0
    for boundary, offset, score in candidates:
        if offset - previous_offset < min_l3_chars:
            continue
        if len(text) - offset < min_l3_chars:
            continue
        selected.append((boundary, offset, score, "semantic_percentile"))
        previous_offset = offset
    # A low-score boundary can still leave a piece longer than the L3 limit.
    # In that case choose the lowest-score complete-sentence boundary that fits
    # in the remaining space.  This is still based on the same embedding
    # comparison, but the reason is recorded separately for audit.
    selected_offsets = {offset for _, offset, _, _ in selected}
    while True:
        ordered_offsets = [0, *sorted(selected_offsets), len(text)]
        oversized_range = next(
            (
                (range_start, range_end)
                for range_start, range_end in zip(ordered_offsets, ordered_offsets[1:])
                if range_end - range_start > paragraph_min_chars
            ),
            None,
        )
        if oversized_range is None:
            break
        range_start, range_end = oversized_range
        viable = [
            item
            for item in scores
            if (
                item[1] not in selected_offsets
                and range_start + min_l3_chars <= item[1] <= range_start + paragraph_min_chars
                and range_end - item[1] >= min_l3_chars
            )
        ]
        if viable:
            boundary, offset, score = min(viable, key=lambda item: (item[2], -item[1]))
            selected.append((boundary, offset, score, "semantic_size_guard"))
            selected_offsets.add(offset)
            continue
        # No boundary can satisfy both the maximum and the preferred 300-char
        # minimum. Keep the sentence intact and prefer the maximum-size rule;
        # the short tail is explicit in the plan instead of being silently cut.
        fallback = [
            item
            for item in scores
            if (
                item[1] not in selected_offsets
                and range_start < item[1] <= range_start + paragraph_min_chars
            )
        ]
        if not fallback:
            # This is defensive: the sentence-length check above should make
            # it unreachable, but a malformed sentence range must still be
            # transparent rather than silently emitting an oversized leaf.
            result["fallback_reason"] = "no_sentence_boundary_within_l3_limit"
            break
        boundary, offset, score = max(fallback, key=lambda item: item[1])
        selected.append((boundary, offset, score, "sentence_size_fallback"))
        selected_offsets.add(offset)
    result["selected_boundaries"] = [
        {
            "sentence_index": boundary,
            "offset": offset,
            "score": score,
            "reason": reason,
        }
        for boundary, offset, score, reason in sorted(selected, key=lambda item: item[1])
    ]
    result["semantic_percentile_boundary_count"] = sum(
        reason == "semantic_percentile" for _, _, _, reason in selected
    )
    result["semantic_size_guard_boundary_count"] = sum(
        reason == "semantic_size_guard" for _, _, _, reason in selected
    )
    result["sentence_size_fallback_boundary_count"] = sum(
        reason == "sentence_size_fallback" for _, _, _, reason in selected
    )
    result["decision"] = "semantic_applied" if selected else "semantic_no_boundary"
    if not selected:
        result["fallback_reason"] = result["fallback_reason"] or "no_boundary_met_minimum"
    elif not result["semantic_percentile_boundary_count"]:
        result["fallback_reason"] = "no_percentile_boundary_size_guard"
    return result


def build_semantic_boundary_plan(
    text: str,
    *,
    document_id: str = "",
    source_start: int = 0,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
    config: SemanticChunkingConfig | None = None,
) -> dict:
    """兼容更直观的文本入口；计划中的断点仍是相对段落偏移。"""
    if config is None:
        config = SemanticChunkingConfig()
    if embedder is None and len(str(text or "")) > config.level_sizes[-1]:
        raise ValueError("长普通段落需要传入 embedder 才能生成语义断点计划")
    result = plan_paragraph_boundaries(
        {
            "document_id": document_id,
            "source_start": source_start,
            "source_end": source_start + len(str(text or "")),
            "text": text,
        },
        embedder or (lambda _texts: []),
        sentence_window=config.sentence_window,
        min_l3_chars=config.min_l3_chars,
        breakpoint_percentile=config.breakpoint_percentile,
        paragraph_min_chars=config.level_sizes[-1],
    )
    return result


def split_by_boundaries(atom: dict, plan: dict) -> list[dict]:
    """Split an atom at selected relative sentence offsets."""
    selected = sorted(
        int(item["offset"])
        for item in plan.get("selected_boundaries") or []
        if isinstance(item, dict) and 0 < int(item.get("offset", 0)) < len(str(atom.get("text") or ""))
    )
    if not selected:
        return [atom]
    text = str(atom["text"])
    pieces: list[dict] = []
    starts = [0, *selected]
    ends = [*selected, len(text)]
    for start, end in zip(starts, ends):
        body = text[start:end].strip()
        if not body:
            continue
        leading = len(text[start:end]) - len(text[start:end].lstrip())
        trailing = len(text[start:end]) - len(text[start:end].rstrip())
        pieces.append({
            **atom,
            "text": body,
            "start": int(atom["start"]) + start + leading,
            "end": int(atom["start"]) + end - trailing,
            "kind": "paragraph",
        })
    return pieces or [atom]
