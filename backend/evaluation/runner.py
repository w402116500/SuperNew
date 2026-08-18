"""离线 RAG 评测运行器：准备独立语料、执行评测并输出可复现实验记录。"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

from backend.evaluation.datasets import (
    ENTERPRISE_FULL_ORDINARY_COUNT,
    ENTERPRISE_SEED,
    ENTERPRISE_SMOKE_ORDINARY_COUNT,
    enterprise_document_filename,
    enterprise_document_count,
    enterprise_markdown,
    load_enterprise_canonical_markdown,
    load_enterprise_cases,
    load_enterprise_documents_by_ids,
    select_enterprise_hard_ids,
    select_enterprise_ordinary_ids,
    select_enterprise_smoke_cases,
    split_enterprise_cases,
    ecom_corpus_count,
    ecom_markdown,
    iter_ecom_corpus,
    load_ecom_cases,
    load_multihop_dataset,
    select_smoke_cases,
    stable_filename,
    write_markdown_documents,
)
from backend.evaluation.metrics import multihop_metrics, retrieval_metrics
from backend.evaluation.translation import TranslationClient, TranslationConfig
from backend.indexing.document_loader import (
    DEFAULT_CHUNKING_STRATEGY,
    STRUCTURED_MARKDOWN_CHUNKING_STRATEGY,
    SUPPORTED_CHUNKING_STRATEGIES,
    DocumentLoader,
)
from backend.indexing.embedding import embedding_public_config, embedding_service
from backend.indexing.chunk_metadata import structured_chunk_metadata
from backend.indexing.milvus_client import MilvusSettings, MilvusStore
from backend.indexing.milvus_writer import MilvusWriter
from backend.indexing.parent_chunk_store import ParentChunkStore
from backend.evaluation.storage import EvaluationParentChunkStore, EvaluationStorageConfig
from backend.model_settings import evaluation_case_timeout_seconds, model_timeout_seconds
from backend.rag.utils import RETRIEVAL_TOP_K, RERANK_ENABLED, RetrievalRuntime, retrieve_documents


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ROOT = PROJECT_ROOT / "output" / "rag-evaluations"
DATASET_ROOT = PROJECT_ROOT / "tmp" / "rag-benchmarks"
ENTERPRISE_ROOT = DATASET_ROOT / "enterprise-rag-bench"


@dataclass(frozen=True)
class JudgeConfig:
    """独立回答判卷请求所需的非敏感配置。"""

    base_url: str
    model: str
    timeout_seconds: float = 90


@dataclass
class _MultiHopWorker:
    """负责顺序处理评测题目的隔离子进程及其通信队列。"""

    process: Any
    request_queue: Any
    result_queue: Any


MAX_EVALUATION_WORKERS = 32


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法序列化 {type(value).__name__}")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _code_version() -> str:
    """Capture the source commit without making Git a runtime dependency."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _safe_run_id(value: str) -> str:
    run_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    if not run_id:
        raise ValueError("run_id 不能为空，且只能包含字母、数字、下划线或连字符")
    return run_id.lower()


def default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def run_directory(dataset: str, run_id: str) -> Path:
    return EVALUATION_ROOT / dataset / _safe_run_id(run_id)


def evaluation_directory(dataset: str, run_id: str, evaluation_id: str) -> Path:
    """Return a non-overlapping experiment directory for a prepared corpus."""
    return run_directory(dataset, run_id) / "evaluations" / _safe_run_id(evaluation_id)


def _collection_name(dataset: str, run_id: str) -> str:
    # Milvus collection 只接受字母、数字和下划线；运行目录仍保留连字符形式的 run_id。
    milvus_run_id = _safe_run_id(run_id).replace("-", "_")
    return f"rag_eval_{dataset}_{milvus_run_id}"


def _is_valid_milvus_collection_name(value: str) -> bool:
    """判断集合名是否满足 Milvus 的字母、数字、下划线约束。"""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _evaluation_store(collection_name: str) -> MilvusStore:
    base = MilvusSettings.from_env()
    return MilvusStore(replace(base, collection_name=collection_name))


_VECTOR_COPY_FIELDS = [
    "dense_embedding",
    "text",
    "filename",
    "file_type",
    "file_path",
    "page_number",
    "chunk_idx",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
]


def _copy_non_target_vectors(
    *,
    source_store: MilvusStore,
    target_store: MilvusStore,
    source_collection_name: str,
    source_corpus_run_id: str,
    target_corpus_run_id: str,
    target_document_ids: set[str],
    checkpoint_path: Path,
    batch_size: int,
    page_size: int = 1000,
) -> dict[str, Any]:
    """Copy only legacy non-target L3 vectors into a new collection.

    This path never calls EmbeddingService and never writes to ``source_store``.
    It is resumable by chunk ID: after an interrupted page, existing target IDs
    are queried before insertion so a committed page is not submitted again.
    Missing vector/text/ID fields fail closed; callers must not silently fall
    back to re-embedding the 7,179 legacy documents.
    """
    if not source_collection_name or source_collection_name == target_store.collection_name:
        raise RuntimeError("旧向量复制源集合与目标集合不能相同")
    if not source_store.has_collection():
        raise RuntimeError(f"旧 Representative Milvus 集合不存在，拒绝回退到全量重嵌入：{source_collection_name}")
    if batch_size <= 0 or page_size <= 0:
        raise ValueError("vector copy batch/page size 必须是正整数")

    excluded_filenames = {
        f"__rag_eval__{source_corpus_run_id}__{enterprise_document_filename(doc_id)}"
        for doc_id in target_document_ids
    }
    immutable = {
        "source_collection_name": source_collection_name,
        "source_corpus_run_id": source_corpus_run_id,
        "target_corpus_run_id": target_corpus_run_id,
        "excluded_document_ids": sorted(target_document_ids),
        "excluded_filenames": sorted(excluded_filenames),
        "batch_size": batch_size,
        "page_size": page_size,
    }
    checkpoint: dict[str, Any] = {}
    copied_ids: set[str] = set()
    offset = 0
    if checkpoint_path.is_file():
        checkpoint = _read_json(checkpoint_path)
        for key, value in immutable.items():
            if checkpoint.get(key) != value:
                raise RuntimeError(f"vector copy checkpoint 配置不匹配：{key}")
        copied_ids = {str(value) for value in checkpoint.get("copied_chunk_ids") or []}
        offset = int(checkpoint.get("source_page_offset") or 0)

    copied_count = int(checkpoint.get("vector_copy_count") or len(copied_ids))
    page_count = int(checkpoint.get("source_page_count") or 0)

    def flush_insert_rows(rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        existing = target_store.get_chunks_by_ids(
            [str(item["chunk_id"]) for item in rows]
        )
        existing_ids = {str(item.get("chunk_id") or "") for item in (existing or [])}
        recovered_ids = existing_ids - copied_ids
        copied_ids.update(recovered_ids)
        remaining = [item for item in rows if str(item["chunk_id"]) not in existing_ids]
        if not remaining:
            return len(recovered_ids)
        target_store.insert(remaining)
        copied_ids.update(str(item["chunk_id"]) for item in remaining)
        return len(recovered_ids) + len(remaining)

    # ``query`` offset pagination stops at Milvus' 16,384-row window.  Use the
    # native server-side cursor so the complete legacy collection can be copied.
    for rows in source_store.query_iterator(
        filter_expr="chunk_level == 3",
        output_fields=_VECTOR_COPY_FIELDS,
        batch_size=page_size,
    ):
        page_count += 1
        insert_rows: list[dict[str, Any]] = []
        for row in rows:
            filename = str(row.get("filename") or "")
            if filename in excluded_filenames:
                continue
            chunk_id = str(row.get("chunk_id") or "")
            vector = row.get("dense_embedding")
            if not chunk_id or not filename or not isinstance(vector, (list, tuple)) or not vector:
                raise RuntimeError(
                    f"旧向量记录缺少可安全复制的 chunk_id/filename/dense_embedding：{row!r}"
                )
            if chunk_id in copied_ids:
                continue
            insert_rows.append({
                "dense_embedding": list(vector),
                "text": str(row.get("text") or ""),
                "filename": filename,
                "file_type": str(row.get("file_type") or ""),
                "file_path": str(row.get("file_path") or ""),
                "page_number": int(row.get("page_number", 0) or 0),
                "chunk_idx": int(row.get("chunk_idx", 0) or 0),
                "chunk_id": chunk_id,
                "parent_chunk_id": str(row.get("parent_chunk_id") or ""),
                "root_chunk_id": str(row.get("root_chunk_id") or ""),
                "chunk_level": int(row.get("chunk_level", 3) or 3),
                **structured_chunk_metadata(row),
            })
            if len(insert_rows) >= batch_size:
                copied_count += flush_insert_rows(insert_rows)
                insert_rows = []
        if insert_rows:
            # The same read-back guard handles a partial page after a crash.
            copied_count += flush_insert_rows(insert_rows)

        offset += len(rows)
        checkpoint = {
            **immutable,
            "status": "running",
            "source_page_offset": offset,
            "source_page_count": page_count,
            "vector_copy_count": copied_count,
            # IDs make recovery safe if a process dies after Milvus accepts an
            # insert but before this checkpoint is flushed.
            "copied_chunk_ids": sorted(copied_ids),
        }
        _write_json(checkpoint_path, checkpoint)
    checkpoint = {
        **immutable,
        "status": "completed",
        "source_page_offset": offset,
        "source_page_count": page_count,
        "vector_copy_count": copied_count,
        "copied_chunk_ids": sorted(copied_ids),
    }
    _write_json(checkpoint_path, checkpoint)
    return checkpoint


def _public_config(
    dataset: str,
    run_id: str,
    profile: str,
    collection_name: str,
    *,
    language: str = "en",
    corpus: str = "representative",
    evaluation_mode: str = "rag",
    chunking_strategy: str = DEFAULT_CHUNKING_STRATEGY,
    rechunk_scope: str = "full",
    document_parse_workers: int | None = None,
) -> dict[str, Any]:
    """保存可复现实验配置，明确排除所有密钥类环境变量。"""
    return {
        "dataset": dataset,
        "run_id": run_id,
        "profile": profile,
        "collection_name": collection_name,
        "prepared_at": datetime.now(UTC).isoformat(),
        "data_root": str(
            ENTERPRISE_ROOT
            if dataset == "enterpriserag"
            else DATASET_ROOT / ("ecom-retrieval" if dataset == "ecomretrieval" else "multihop-rag")
        ),
        **embedding_public_config(),
        "dense_embedding_dim": os.getenv("DENSE_EMBEDDING_DIM", "1024"),
        "main_model": os.getenv("MODEL", ""),
        "fast_model": os.getenv("FAST_MODEL", ""),
        "grade_model": os.getenv("GRADE_MODEL", ""),
        "rerank_model": os.getenv("RERANK_MODEL", ""),
        "rerank_configured": RERANK_ENABLED,
        "rerank_timeout_seconds": os.getenv("RERANK_TIMEOUT_SECONDS", "5"),
        "rerank_min_score": os.getenv("RERANK_MIN_SCORE", "0"),
        "model_timeout_seconds": model_timeout_seconds(),
        "evaluation_case_timeout_seconds": evaluation_case_timeout_seconds(),
        "retrieval_top_k": RETRIEVAL_TOP_K,
        "retrieval_candidate_k": os.getenv("RETRIEVAL_CANDIDATE_K", ""),
        "retrieval_candidate_multiplier": os.getenv("RETRIEVAL_CANDIDATE_MULTIPLIER", "3"),
        "auto_merge_enabled": os.getenv("AUTO_MERGE_ENABLED", "true"),
        "auto_merge_threshold": os.getenv("AUTO_MERGE_THRESHOLD", "2"),
        "source_format": "markdown",
        "language": language,
        "corpus": corpus,
        "evaluation_mode": evaluation_mode,
        "document_chunking_strategy": chunking_strategy,
        "rechunk_scope": rechunk_scope,
        "document_parse_workers": _normalize_enterprise_document_parse_workers(document_parse_workers),
        "embedding_rate_limit_rpm": os.getenv("EMBEDDING_MAX_RPM", "2000"),
        "embedding_rate_limit_tpm": os.getenv("EMBEDDING_MAX_TPM", "500000"),
        "embedding_workers": _embedding_worker_count(),
        "embedding_batch_retry_count": os.getenv("EMBEDDING_BATCH_RETRIES", "3"),
        "milvus_batch_size": os.getenv("MILVUS_WRITE_BATCH_SIZE", os.getenv("EMBEDDING_BATCH_SIZE", "50")),
        "milvus_batch_retry_count": os.getenv("MILVUS_WRITE_RETRIES", "3"),
        "secrets_recorded": False,
    }


def _write_cases(path: Path, cases: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")


def read_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ecom_leaf_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """EcomRetrieval 只写单层 L3 文档，避免父块合并影响纯排序对比。"""
    return [
        {
            "text": item["markdown"],
            "filename": item["filename"],
            "file_type": "Markdown",
            "file_path": item["path"],
            "page_number": 0,
            "chunk_idx": index,
            "chunk_id": f"{item['filename']}::p0::l3::0",
            "parent_chunk_id": "",
            "root_chunk_id": "",
            "chunk_level": 3,
        }
        for index, item in enumerate(documents)
    ]


def _iter_ecom_leaf_chunks(
    source_root: Path,
    markdown_dir: Path,
    id_to_filename: dict[str, str],
) -> Iterable[dict[str, Any]]:
    """边转换 Markdown 边产生 L3 块，确保完整 Ecom 语料不会同时驻留内存。"""
    markdown_dir.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(iter_ecom_corpus(source_root)):
        source_id = str(record["_id"])
        filename = stable_filename(source_id, str(record.get("title") or ""))
        markdown = ecom_markdown(record)
        path = markdown_dir / filename
        path.write_text(markdown, encoding="utf-8", newline="\n")
        id_to_filename[source_id] = filename
        yield {
            "text": markdown,
            "filename": filename,
            "file_type": "Markdown",
            "file_path": str(path),
            "page_number": 0,
            "chunk_idx": index,
            "chunk_id": f"{filename}::p0::l3::0",
            "parent_chunk_id": "",
            "root_chunk_id": "",
            "chunk_level": 3,
        }


def _translation_worker_count() -> int:
    """限制中文语料翻译并发，避免离线任务挤占模型服务。"""
    try:
        configured = int(os.getenv("TRANSLATION_MAX_WORKERS", "1"))
    except ValueError:
        configured = 1
    return min(max(configured, 1), 6)


def _translation_segment_worker_count() -> int:
    """限制长文分段翻译的并发，避免离线任务挤占模型服务。"""
    try:
        configured = int(os.getenv("TRANSLATION_SEGMENT_MAX_WORKERS", "2"))
    except ValueError:
        configured = 2
    return min(max(configured, 1), 4)


def _enterprise_document_parse_worker_count() -> int:
    """Return the bounded local Markdown parse pool size.

    Parsing is CPU/memory work and is intentionally separate from the RAG
    evaluation worker count.  A bad environment value fails closed to the
    conservative default instead of creating an unbounded executor.
    """
    try:
        configured = int(os.getenv("ENTERPRISE_DOCUMENT_PARSE_WORKERS", "8"))
    except ValueError:
        configured = 8
    return min(max(configured, 1), 32)


def _normalize_enterprise_document_parse_workers(value: int | None) -> int:
    if value is None:
        return _enterprise_document_parse_worker_count()
    return min(max(int(value), 1), 32)


def _embedding_worker_count() -> int:
    try:
        configured = int(os.getenv("EMBEDDING_MAX_WORKERS", "10"))
    except ValueError:
        configured = 10
    return min(max(configured, 1), 10)


def _enterprise_document_parse_retries() -> int:
    try:
        configured = int(os.getenv("ENTERPRISE_DOCUMENT_PARSE_RETRIES", "2"))
    except ValueError:
        configured = 2
    return min(max(configured, 0), 5)


def _validate_structured_target_manifest_header(path: Path) -> dict[str, Any]:
    """Validate the immutable, analysis-only target before any output is written."""
    if not path.is_file():
        raise FileNotFoundError(f"缺少结构化分块 target manifest：{path}")
    payload = _read_json(path)
    if payload.get("manifest_type") != "structured_chunking_offline_audit":
        raise ValueError("target manifest 不是结构化分块离线审计清单")
    if payload.get("case_set") != "analysis":
        raise ValueError("结构化分块 targeted scope 只能使用 analysis target")
    if payload.get("changed_variable") != "document_chunking_strategy":
        raise ValueError("结构化分块 targeted scope 的 changed_variable 必须是 document_chunking_strategy")
    if payload.get("new_chunking_strategy") != STRUCTURED_MARKDOWN_CHUNKING_STRATEGY:
        raise ValueError("target manifest 的新分块策略不是 markdown_header_recursive_v1")
    case_ids = [str(value) for value in payload.get("case_ids") or []]
    if len(case_ids) != 30 or len(set(case_ids)) != len(case_ids):
        raise ValueError("结构化分块 targeted scope 必须冻结 30 道唯一 analysis 题")
    if payload.get("manifest_sha256") != _manifest_payload_hash(payload):
        raise ValueError("结构化分块 target manifest 自身哈希校验失败")
    target_document_ids = {
        str(value) for value in payload.get("target_document_ids") or [] if str(value).strip()
    }
    declared_count = payload.get("target_document_count")
    if target_document_ids and declared_count is not None and int(declared_count) != len(target_document_ids):
        raise ValueError("target_document_count 与 target_document_ids 不一致")
    return payload


def _translate_markdown_documents(
    documents: list[str],
    config: TranslationConfig,
    cache_dir: Path,
    workers: int,
    segment_workers: int = 2,
) -> list[str]:
    """并发翻译独立文档；每个工作项独享 HTTP 会话，返回顺序保持不变。"""
    def translate_one(markdown: str) -> str:
        client = TranslationClient(config, cache_dir)
        try:
            return str(client.translate(markdown, kind="document", segment_workers=segment_workers)["text"])
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enterprise-translation") as executor:
        return list(executor.map(translate_one, documents))


def _prepare_enterprise_documents(
    *,
    run_id: str,
    profile: Literal["smoke", "full"],
    language: Literal["en", "zh"],
    corpus: Literal["representative", "challenge"],
    chunking_strategy: str = DEFAULT_CHUNKING_STRATEGY,
    target_document_ids: set[str] | None = None,
    target_case_ids: set[str] | None = None,
    checkpoint_dir: Path | None = None,
    parse_workers: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """选择 EnterpriseRAG 语料、生成 Markdown，并返回三级分块与审计元数据。"""
    all_cases = load_enterprise_cases(ENTERPRISE_ROOT)
    case_split = split_enterprise_cases(all_cases) if profile == "full" else None
    selected_cases = (
        all_cases if profile == "full" else select_enterprise_smoke_cases(all_cases)
    )
    case_set_by_id = {
        case_id: case_set
        for case_set, case_ids in (
            ("analysis", case_split["analysis_case_ids"] if case_split else []),
            ("validation", case_split["validation_case_ids"] if case_split else []),
        )
        for case_id in case_ids
    }
    required_ids = {
        doc_id
        for case in selected_cases
        for doc_id in case["expected_doc_ids"]
    }
    ordinary_target = (
        ENTERPRISE_FULL_ORDINARY_COUNT
        if profile == "full"
        else ENTERPRISE_SMOKE_ORDINARY_COUNT
    )
    ordinary_ids, source_quotas, source_actual = select_enterprise_ordinary_ids(
        ENTERPRISE_ROOT,
        required_ids,
        ordinary_target,
        seed=ENTERPRISE_SEED,
    )
    hard_ids: set[str] = set()
    hard_by_case: dict[str, list[str]] = {}
    if corpus == "challenge":
        title_index = ENTERPRISE_ROOT / "derived" / "v1" / "title-index.sqlite"
        hard_ids, hard_by_case = select_enterprise_hard_ids(
            ENTERPRISE_ROOT,
            selected_cases,
            required_ids | ordinary_ids,
            title_index,
            per_case=2,
        )
    selected_ids = required_ids | ordinary_ids | hard_ids
    records = load_enterprise_documents_by_ids(ENTERPRISE_ROOT, selected_ids)

    language_dir = "en-control" if language == "en" else "zh-primary"
    artifact_dir = (
        ENTERPRISE_ROOT
        / "derived"
        / "v1"
        / language_dir
        / corpus
        / profile
        / "documents"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    translation_client = None
    translation_config: TranslationConfig | None = None
    translated_documents: list[str] | None = None
    translation_workers = 0
    translation_segment_workers = 0
    if language == "zh":
        # canonical 是已整篇翻译并人工核验的固定数据版本；评测准备阶段不重复调用翻译 API。
        canonical_by_id = load_enterprise_canonical_markdown(
            ENTERPRISE_ROOT,
            [str(record["doc_id"]) for record in records],
        )
        translated_documents = [canonical_by_id[str(record["doc_id"])] for record in records]
        translation_config = TranslationConfig.from_env()

    run_prefix = f"__rag_eval__{run_id}__"
    all_chunks: list[dict[str, Any]] = []
    document_map: dict[str, str] = {}
    document_manifest: list[dict[str, Any]] = []
    document_specs: list[dict[str, Any]] = []
    target_document_ids = set(target_document_ids or set())
    target_case_ids = set(target_case_ids or set())
    if target_case_ids:
        # Older frozen manifests predate target_document_ids.  Derive the full
        # answer-document union from the immutable case set for compatibility.
        target_document_ids.update(
            str(doc_id)
            for case in selected_cases
            if str(case.get("id") or "") in target_case_ids
            for doc_id in case.get("expected_doc_ids") or []
        )
    targeted_mode = bool(target_case_ids or target_document_ids)
    for index, record in enumerate(records):
        doc_id = str(record["doc_id"])
        stable_name = enterprise_document_filename(doc_id)
        markdown = enterprise_markdown(record)
        if translated_documents is not None:
            markdown = translated_documents[index].rstrip() + "\n"
        stable_path = artifact_dir / stable_name
        if not stable_path.exists() or stable_path.read_text(encoding="utf-8") != markdown:
            stable_path.write_text(markdown, encoding="utf-8", newline="\n")
        runtime_filename = f"{run_prefix}{stable_name}"
        document_specs.append({
            "index": index,
            "doc_id": doc_id,
            "stable_path": stable_path,
            "runtime_filename": runtime_filename,
            "source_hash": sha256(markdown.encode("utf-8")).hexdigest(),
            "chunking_strategy": (
                STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
                if chunking_strategy == STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
                and (not targeted_mode or doc_id in target_document_ids)
                else DEFAULT_CHUNKING_STRATEGY
            ),
        })
        document_map[doc_id] = runtime_filename
        roles = []
        if doc_id in required_ids:
            roles.append("required_evidence")
        if doc_id in ordinary_ids:
            roles.append("ordinary_distractor")
        if doc_id in hard_ids:
            roles.append("hard_distractor")
        document_manifest.append({
            "doc_id": doc_id,
            "filename": runtime_filename,
            "stable_filename": stable_name,
            "source_type": str(record.get("source_type") or "unknown"),
            "title": str(record.get("title") or ""),
            "roles": roles,
            "source_hash": sha256(enterprise_markdown(record).encode("utf-8")).hexdigest(),
            "markdown_hash": sha256(markdown.encode("utf-8")).hexdigest(),
            "translation_source": (
                "canonical_manual_retranslation" if language == "zh" else "source_parquet"
            ),
            "chunking_strategy": document_specs[-1]["chunking_strategy"],
            "targeted_rechunk": targeted_mode and doc_id in target_document_ids,
        })

    # Each worker owns its loader because LangChain splitters are not shared
    # mutable state.  Results are reassembled in source order and only the
    # main thread writes checkpoint files, keeping persistence single-writer.
    parse_workers = _normalize_enterprise_document_parse_workers(parse_workers)
    parse_retries = _enterprise_document_parse_retries()
    checkpoint_root = checkpoint_dir / "documents" if checkpoint_dir else None
    if checkpoint_root:
        checkpoint_root.mkdir(parents=True, exist_ok=True)

    def parse_one(spec: dict[str, Any]) -> tuple[int, list[dict[str, Any]], int]:
        last_error: Exception | None = None
        for attempt in range(parse_retries + 1):
            try:
                loader = DocumentLoader(chunking_strategy=spec["chunking_strategy"])
                chunks = loader.load_document(
                    str(spec["stable_path"]),
                    spec["runtime_filename"],
                    str(spec["stable_path"]),
                )
                return spec["index"], chunks, attempt
            except Exception as exc:  # retry only this document
                last_error = exc
                if attempt < parse_retries:
                    time.sleep(min(2 ** attempt, 4))
        raise RuntimeError(
            f"EnterpriseRAG 文档解析失败：{spec['doc_id']}，已重试 {parse_retries} 次"
        ) from last_error

    with ThreadPoolExecutor(
        max_workers=parse_workers,
        thread_name_prefix="enterprise-markdown-parse",
    ) as executor:
        # Keep both the worker pool and the pending queue bounded.  ``map`` on
        # older Python versions eagerly submits every document, which would
        # retain thousands of futures for the 7,222-document corpus.
        pending = {
            executor.submit(parse_one, spec): spec
            for spec in document_specs[:parse_workers]
        }
        next_spec_index = len(pending)
        parsed_results: dict[int, tuple[list[dict[str, Any]], int]] = {}
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                spec = pending.pop(future)
                index, chunks, attempts = future.result()
                parsed_results[index] = (chunks, attempts)
                if next_spec_index < len(document_specs):
                    next_spec = document_specs[next_spec_index]
                    pending[executor.submit(parse_one, next_spec)] = next_spec
                    next_spec_index += 1

        # Restore source order so chunk IDs, manifests and downstream inserts
        # remain reproducible regardless of completion timing.
        for index in sorted(parsed_results):
            chunks, attempts = parsed_results[index]
            all_chunks.extend(chunks)
            if checkpoint_root:
                spec = document_specs[index]
                checkpoint = {
                    "status": "completed",
                    "doc_id": spec["doc_id"],
                    "stable_filename": spec["stable_path"].name,
                    "source_markdown_sha256": spec["source_hash"],
                    "chunking_strategy": spec["chunking_strategy"],
                    "chunk_count": len(chunks),
                    "retry_count": attempts,
                }
                _write_json(checkpoint_root / f"{index:06d}.json", checkpoint)

    prepared_cases: list[dict[str, Any]] = []
    if language == "zh":
        translation_client = TranslationClient(
            translation_config,
            artifact_dir.parents[2] / "translation-cache",
        )
    try:
        for original_case in selected_cases:
            case = dict(original_case)
            case["case_set"] = case_set_by_id.get(str(case["id"]), "smoke")
            if translation_client:
                case["source_question"] = case["question"]
                case["source_reference_answer"] = case["reference_answer"]
                case["question"] = translation_client.translate(
                    case["question"], kind="question"
                )["text"]
                case["reference_answer"] = translation_client.translate(
                    case["reference_answer"], kind="reference_answer"
                )["text"]
                case["translation_source"] = "translation_api_cached"
            else:
                case["translation_source"] = "source_parquet"
            missing_expected = [doc_id for doc_id in case["expected_doc_ids"] if doc_id not in document_map]
            if missing_expected:
                raise RuntimeError(f"标准证据文档未进入 EnterpriseRAG 语料：{missing_expected[:5]}")
            expected = [document_map[doc_id] for doc_id in case["expected_doc_ids"]]
            case["expected_evidence_filenames"] = expected
            case["evidence_filenames"] = expected
            case["hard_distractor_doc_ids"] = hard_by_case.get(case["id"], [])
            prepared_cases.append(case)
    finally:
        if translation_client:
            translation_client.close()

    metadata = {
        "language": language,
        "language_dir": language_dir,
        "corpus": corpus,
        "profile": profile,
        "seed": ENTERPRISE_SEED,
        "source_document_count": enterprise_document_count(ENTERPRISE_ROOT),
        "selected_case_count": len(prepared_cases),
        "required_document_count": len(required_ids),
        "ordinary_document_count": len(ordinary_ids),
        "hard_document_count": len(hard_ids),
        "source_quotas": source_quotas,
        "source_actual": source_actual,
        "hard_documents_by_case": hard_by_case,
        "case_split_manifest": case_split,
        "document_map": document_map,
        "document_manifest": document_manifest,
        "artifact_dir": str(artifact_dir),
        "translation_workers": translation_workers,
        "translation_segment_workers": translation_segment_workers,
        "translation_strategy": (
            "canonical_manual_retranslation" if language == "zh" else "not_applicable"
        ),
        "translation_whole_document_max_chars": (
            translation_config.whole_document_max_chars if translation_config else None
        ),
        "canonical_document_dir": (
            str(ENTERPRISE_ROOT / "derived" / "v1" / "zh-primary" / "manual-codex-retranslation" / "canonical" / "documents")
            if language == "zh" else None
        ),
        "chunking_strategy": chunking_strategy,
        "chunking_scope": "targeted_rechunk" if targeted_mode else "full_corpus",
        "targeted_rechunk_document_count": len(target_document_ids),
        "targeted_rechunk_document_ids": sorted(target_document_ids),
        "document_parse_workers": parse_workers,
        "document_parse_retries": parse_retries,
        "chunking_config_hash": DocumentLoader(
            chunking_strategy=STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
        ).chunking_config_hash
        if chunking_strategy == STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
        else None,
    }
    return prepared_cases, [chunk for chunk in all_chunks if chunk["chunk_level"] in (1, 2)], [
        chunk for chunk in all_chunks if chunk["chunk_level"] == 3
    ], metadata


def prepare_run(
    *,
    dataset: Literal["ecomretrieval", "multihoprag", "enterpriserag"],
    run_id: str,
    profile: Literal["smoke", "full"],
    language: Literal["en", "zh"] = "en",
    corpus: Literal["representative", "challenge"] = "representative",
    evaluation_mode: Literal["retrieval", "rag"] = "rag",
    chunking_strategy: str = DEFAULT_CHUNKING_STRATEGY,
    rechunk_scope: Literal["full", "targeted"] = "full",
    target_manifest_path: Path | None = None,
    document_parse_workers: int | None = None,
) -> Path:
    """转换语料、写入独立集合，并持久化样本和清理清单。"""
    run_id = _safe_run_id(run_id)
    if chunking_strategy not in SUPPORTED_CHUNKING_STRATEGIES:
        raise ValueError(f"不支持的 document_chunking_strategy：{chunking_strategy}")
    if dataset != "enterpriserag" and chunking_strategy != DEFAULT_CHUNKING_STRATEGY:
        raise ValueError("结构化 Markdown 分块只允许用于 EnterpriseRAG 评测")
    if rechunk_scope not in {"full", "targeted"}:
        raise ValueError("rechunk_scope 必须是 full 或 targeted")
    if rechunk_scope == "targeted":
        if dataset != "enterpriserag" or chunking_strategy != STRUCTURED_MARKDOWN_CHUNKING_STRATEGY:
            raise ValueError("targeted_rechunk 只允许用于 EnterpriseRAG 的结构化 Markdown 分块")
        if target_manifest_path is None:
            raise ValueError("targeted_rechunk 必须提供 --target-manifest")
        target_manifest_path = target_manifest_path.resolve()
        target_manifest = _validate_structured_target_manifest_header(target_manifest_path)
        target_document_ids = {
            str(value) for value in target_manifest.get("target_document_ids") or []
        }
        target_case_ids = {str(value) for value in target_manifest.get("case_ids") or []}
    elif target_manifest_path is not None:
        raise ValueError("只有 rechunk_scope=targeted 时才能提供 target manifest")
    else:
        target_manifest = None
        target_document_ids = set()
        target_case_ids = set()

    evaluation_storage_config: EvaluationStorageConfig | None = None
    isolated_parent_store: EvaluationParentChunkStore | None = None
    if chunking_strategy == STRUCTURED_MARKDOWN_CHUNKING_STRATEGY:
        # This runs before the output directory, Milvus collection, or any chunk
        # write exists, so a missing/unsafe URL cannot fall back to business data.
        evaluation_storage_config = EvaluationStorageConfig.from_env()
        isolated_parent_store = EvaluationParentChunkStore(
            run_id,
            config=evaluation_storage_config,
        )
        isolated_parent_store.check_connection()

    output_dir = run_directory(dataset, run_id)
    resume_incomplete = False
    if output_dir.exists():
        existing_manifest_path = output_dir / "manifest.json"
        if not (
            rechunk_scope == "targeted"
            and existing_manifest_path.is_file()
        ):
            raise FileExistsError(f"评测运行目录已存在：{output_dir}。请换一个 run_id，或先执行 cleanup。")
        existing_manifest = _read_json(existing_manifest_path)
        if existing_manifest.get("prepare_completed"):
            raise FileExistsError(f"评测运行已完成，拒绝覆盖：{output_dir}")
        expected_collection_name = _collection_name(dataset, run_id)
        if (
            existing_manifest.get("run_id") != run_id
            or existing_manifest.get("collection_name") != expected_collection_name
            or existing_manifest.get("rechunk_scope") != "targeted"
            or existing_manifest.get("document_chunking_strategy") != STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
            or existing_manifest.get("target_manifest_sha256") != _sha256_file(target_manifest_path)
        ):
            raise RuntimeError("未完成 targeted run 的不可变配置不匹配，拒绝恢复或覆盖")
        resume_incomplete = True
    else:
        output_dir.mkdir(parents=True)
    collection_name = _collection_name(dataset, run_id)
    markdown_dir = output_dir / "markdown"
    source_root = DATASET_ROOT / ("ecom-retrieval" if dataset == "ecomretrieval" else "multihop-rag")
    if dataset == "ecomretrieval":
        cases = load_ecom_cases(source_root)
        selected_cases = cases if profile == "full" else select_smoke_cases(cases, dataset)
        id_to_filename: dict[str, str] = {}
        document_count = ecom_corpus_count(source_root)
        parent_chunks: list[dict[str, Any]] = []
        leaf_chunks = _iter_ecom_leaf_chunks(source_root, markdown_dir, id_to_filename)
    elif dataset == "multihoprag":
        corpus, cases = load_multihop_dataset(source_root)
        prefix = f"__rag_eval__{run_id}__"
        documents, url_to_filename = write_markdown_documents(
            corpus,
            markdown_dir,
            dataset=dataset,
            filename_prefix=prefix,
        )
        selected_cases = cases if profile == "full" else select_smoke_cases(cases, dataset)
        for case in selected_cases:
            case["evidence_filenames"] = [
                url_to_filename[url] for url in case["evidence_urls"] if url in url_to_filename
            ]
        loader = DocumentLoader()
        all_chunks = [
            chunk
            for item in documents
            for chunk in loader.load_document(item["path"], item["filename"], item["path"])
        ]
        parent_chunks = [chunk for chunk in all_chunks if chunk["chunk_level"] in (1, 2)]
        leaf_chunks = [chunk for chunk in all_chunks if chunk["chunk_level"] == 3]
        document_count = len(documents)
    else:
        selected_cases, parent_chunks, leaf_chunks, enterprise_metadata = _prepare_enterprise_documents(
            run_id=run_id,
            profile=profile,
            language=language,
            corpus=corpus,
            chunking_strategy=chunking_strategy,
            target_document_ids=target_document_ids,
            target_case_ids=target_case_ids,
            checkpoint_dir=output_dir / "preparation-checkpoints",
            parse_workers=document_parse_workers,
        )
        document_count = (
            enterprise_metadata["required_document_count"]
            + enterprise_metadata["ordinary_document_count"]
            + enterprise_metadata["hard_document_count"]
        )
        if rechunk_scope == "targeted":
            # Older frozen manifests may omit target_document_ids; the
            # preparation helper derives the complete 43-document union from
            # the immutable case records and returns it in metadata.
            target_document_ids = set(
                enterprise_metadata.get("targeted_rechunk_document_ids") or target_document_ids
            )

    if dataset != "ecomretrieval" and not leaf_chunks:
        raise RuntimeError("未生成任何 L3 叶子块，已终止入库")

    manifest = {
        "dataset": dataset,
        "run_id": run_id,
        "collection_name": collection_name,
        "markdown_dir": str(markdown_dir),
        "document_map": "document-map.json",
        "document_count": document_count,
        "leaf_chunk_count": document_count if dataset == "ecomretrieval" else len(leaf_chunks),
        "parent_chunk_count": len(parent_chunks),
        "parent_filenames": sorted({chunk["filename"] for chunk in parent_chunks}),
        "cleanup_completed": False,
        # 先落盘未完成状态，进程被系统终止时 evaluate 不会误用部分入库的数据。
        "prepare_completed": False,
        "prepared_at": datetime.now(UTC).isoformat(),
        "document_chunking_strategy": chunking_strategy,
        "rechunk_scope": rechunk_scope,
    }
    config = _public_config(
        dataset,
        run_id,
        profile,
        collection_name,
        language=language,
        corpus=corpus,
        evaluation_mode=evaluation_mode,
        chunking_strategy=chunking_strategy,
        rechunk_scope=rechunk_scope,
        document_parse_workers=document_parse_workers,
    )
    if evaluation_storage_config:
        manifest.update(evaluation_storage_config.public_metadata(run_id))
    if target_manifest is not None:
        manifest.update({
            "target_manifest_path": str(target_manifest_path),
            "target_manifest_sha256": _sha256_file(target_manifest_path),
            "target_case_set": target_manifest.get("case_set"),
            "target_case_count": target_manifest.get("case_count"),
            "target_document_count": len(target_document_ids),
            "target_document_ids": sorted(target_document_ids),
        })
    _write_json(output_dir / "config.json", config)
    _write_json(output_dir / "manifest.json", manifest)
    if dataset == "multihoprag":
        _write_json(output_dir / "document-map.json", {item["source_id"]: item["filename"] for item in documents})
        _write_cases(output_dir / "cases.jsonl", selected_cases)
    elif dataset == "enterpriserag":
        manifest.update({
            key: value
            for key, value in enterprise_metadata.items()
            if key not in {"document_map", "document_manifest", "case_split_manifest"}
        })
        _write_json(output_dir / "document-map.json", enterprise_metadata["document_map"])
        _write_json(output_dir / "corpus-documents.json", enterprise_metadata["document_manifest"])
        _write_cases(output_dir / "cases.jsonl", selected_cases)
        case_split = enterprise_metadata.get("case_split_manifest")
        if case_split:
            case_split_path = output_dir / "case-split.json"
            _write_json(case_split_path, case_split)
            split_hash = _sha256_file(case_split_path)
            (output_dir / "case-split.sha256").write_text(
                split_hash + "\n", encoding="ascii", newline="\n"
            )
            manifest.update({
                "case_split": case_split_path.name,
                "case_split_sha256": split_hash,
                "analysis_case_count": len(case_split["analysis_case_ids"]),
                "validation_case_count": len(case_split["validation_case_ids"]),
            })
        manifest["markdown_dir"] = enterprise_metadata["artifact_dir"]
        if target_manifest is not None:
            manifest["target_document_count"] = enterprise_metadata.get(
                "targeted_rechunk_document_count", len(target_document_ids)
            )
            manifest["target_document_ids"] = enterprise_metadata.get(
                "targeted_rechunk_document_ids", sorted(target_document_ids)
            )
        _write_json(output_dir / "manifest.json", manifest)

    store = _evaluation_store(collection_name)
    parent_store = isolated_parent_store or ParentChunkStore()
    leaf_chunks_to_write = leaf_chunks
    vector_copy_checkpoint_path = output_dir / "preparation-checkpoints" / "vector-copy.json"
    vector_copy_result: dict[str, Any] | None = None
    source_collection_name = ""
    try:
        if isolated_parent_store and rechunk_scope == "targeted":
            source_corpus_run_id = str(target_manifest.get("source_corpus_run_id") or "")
            if not source_corpus_run_id:
                raise RuntimeError("target manifest 缺少 source_corpus_run_id，拒绝复制旧向量")
            source_collection_name = _collection_name("enterpriserag", source_corpus_run_id)
            if source_collection_name == collection_name or source_collection_name == os.getenv(
                "MILVUS_COLLECTION", "embeddings_collection"
            ):
                raise RuntimeError("targeted 旧向量源集合命中了目标/默认业务集合，拒绝复制")
            source_store = _evaluation_store(source_collection_name)
            # Check source existence before creating or writing the target
            # collection.  A missing source must fail, never trigger full
            # re-embedding as an implicit fallback.
            if not source_store.has_collection():
                raise RuntimeError(
                    f"旧 Representative Milvus 集合不存在，拒绝回退到全量重嵌入：{source_collection_name}"
                )
            store.init_collection(int(os.getenv("DENSE_EMBEDDING_DIM", "1024")))
            vector_copy_result = _copy_non_target_vectors(
                source_store=source_store,
                target_store=store,
                source_collection_name=source_collection_name,
                source_corpus_run_id=source_corpus_run_id,
                target_corpus_run_id=run_id,
                target_document_ids=set(target_document_ids),
                checkpoint_path=vector_copy_checkpoint_path,
                batch_size=int(os.getenv("MILVUS_COPY_BATCH_SIZE", "500")),
                page_size=int(os.getenv("MILVUS_COPY_PAGE_SIZE", "1000")),
            )
            target_filenames = {
                str(enterprise_metadata["document_map"][doc_id])
                for doc_id in target_document_ids
                if doc_id in enterprise_metadata["document_map"]
            }
            leaf_chunks_to_write = [
                chunk for chunk in leaf_chunks if str(chunk.get("filename") or "") in target_filenames
            ]
            expected_target_leaf_count = sum(
                1 for chunk in leaf_chunks if str(chunk.get("filename") or "") in target_filenames
            )
            if len(leaf_chunks_to_write) != expected_target_leaf_count:
                raise RuntimeError("target 文档 L3 分块过滤结果不一致，拒绝部分入库")
            manifest.update({
                "source_collection_name": source_collection_name,
                "vector_copy_count": vector_copy_result["vector_copy_count"],
                "vector_copy_checkpoint": str(vector_copy_checkpoint_path),
                "vector_copy_status": vector_copy_result["status"],
                "target_leaf_chunk_count": len(leaf_chunks_to_write),
                "legacy_vector_copy_enabled": True,
            })
            config.update({
                "source_collection_name": source_collection_name,
                "vector_copy_count": vector_copy_result["vector_copy_count"],
                "vector_copy_checkpoint": str(vector_copy_checkpoint_path),
                "legacy_vector_copy_enabled": True,
            })
            _write_json(output_dir / "manifest.json", manifest)
            _write_json(output_dir / "config.json", config)
        if parent_chunks:
            if isolated_parent_store:
                parent_store.upsert_documents(
                    parent_chunks,
                    batch_size=int(os.getenv("EVALUATION_PARENT_BATCH_SIZE", "500")),
                    progress_callback=lambda processed: _write_json(
                        output_dir / "preparation-checkpoints" / "parents.json",
                        {"status": "completed", "processed": processed},
                    ),
                )
            else:
                parent_store.upsert_documents(parent_chunks)
        writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=store)
        if isolated_parent_store:
            writer.write_documents(
                leaf_chunks_to_write,
                batch_size=int(os.getenv("MILVUS_WRITE_BATCH_SIZE", os.getenv("EMBEDDING_BATCH_SIZE", "50"))),
                embedding_workers=_embedding_worker_count(),
                progress_callback=lambda processed, total: _write_json(
                    output_dir / "preparation-checkpoints" / "milvus.json",
                    {"status": "completed", "processed": processed, "total": total},
                ),
            )
        else:
            # Keep the legacy preparation call contract unchanged.
            writer.write_documents(leaf_chunks)
    except Exception:
        # 失败时尽量回收已写入的孤立数据；原异常继续向上抛出，不能伪造准备成功。
        store.drop_collection()
        if isolated_parent_store:
            isolated_parent_store.delete_by_corpus_run()
        else:
            for filename in {chunk["filename"] for chunk in parent_chunks}:
                parent_store.delete_by_filename(filename)
        raise

    if dataset == "ecomretrieval":
        for case in selected_cases:
            case["relevant_filenames"] = [
                id_to_filename[item] for item in case["relevant_document_ids"] if item in id_to_filename
            ]
        _write_json(output_dir / "document-map.json", id_to_filename)
        _write_cases(output_dir / "cases.jsonl", selected_cases)
    manifest["prepare_completed"] = True
    manifest["prepare_completed_at"] = datetime.now(UTC).isoformat()
    _write_json(output_dir / "manifest.json", manifest)
    return output_dir


def _retrieved_filenames(docs: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(doc.get("filename") or "") for doc in docs if doc.get("filename")))


def _ranks(expected: Iterable[str], docs: list[dict[str, Any]]) -> list[int]:
    filenames = _retrieved_filenames(docs)
    return [filenames.index(filename) + 1 for filename in expected if filename in filenames]


def _evidence_coverage(case: dict[str, Any], docs: list[dict[str, Any]]) -> float:
    """计算证据覆盖率；可回答题没有标准证据时不能伪装成满覆盖。"""
    expected = [str(value) for value in case.get("evidence_filenames") or []]
    if not expected:
        return 1.0 if case.get("question_type") in {"null", "null_query", "info_not_found"} else 0.0
    return len(_ranks(expected, docs)) / len(expected)


def _normalize_checkpoint_record(record: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """恢复旧 checkpoint 时按当前证据口径重算覆盖率。"""
    normalized = dict(record)
    retrieved = [{"filename": value} for value in record.get("retrieved_filenames") or []]
    normalized["evidence_coverage"] = _evidence_coverage(case, retrieved)
    return normalized


def _write_results(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")


def _append_result_checkpoint(path: Path, record: dict[str, Any]) -> None:
    """将单题结果同步落盘，避免长时间评测因中断丢失已完成样本。"""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_multihop_checkpoints(path: Path) -> list[dict[str, Any]]:
    """读取已完成的 MultiHopRAG 样本，并按题号去重以支持重复恢复。"""
    if not path.exists():
        return []
    records_by_case_id: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if case_id:
            records_by_case_id[case_id] = record
    return list(records_by_case_id.values())


def _has_evaluation_error(record: dict[str, Any]) -> bool:
    """Return whether a checkpoint is a transport/runtime failure, not a model review."""
    return bool(str(record.get("evaluation_error") or "").strip())


def _is_provider_quota_error(record: dict[str, Any]) -> bool:
    """Stop a run immediately when the provider rejects every further call for quota."""
    error = str(record.get("evaluation_error") or "")
    return "APIStatusError: Error code: 402" in error or "account balance is insufficient" in error


def _retry_case_selection(
    cases: list[dict[str, Any]],
    source_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Select only missing or runtime-failed cases while preserving valid source records."""
    source_by_id = {
        str(record.get("case_id")): record
        for record in source_records
        if record.get("case_id")
    }
    retry_ids = {
        str(case["id"])
        for case in cases
        if str(case["id"]) not in source_by_id
        or _has_evaluation_error(source_by_id[str(case["id"])])
    }
    preserved_ids = {
        str(case["id"])
        for case in cases
        if str(case["id"]) in source_by_id
        and not _has_evaluation_error(source_by_id[str(case["id"])])
    }
    return [case for case in cases if str(case["id"]) in retry_ids], preserved_ids, retry_ids


def _read_retry_source_records(
    dataset: str,
    corpus_run_id: str,
    evaluation_id: str,
) -> tuple[list[dict[str, Any]], Path]:
    """Read a logical source result, including an earlier repair chain if needed."""
    source_dir = evaluation_directory(dataset, corpus_run_id, evaluation_id)
    result_path = source_dir / "results.jsonl"
    if result_path.is_file():
        return _read_multihop_checkpoints(result_path), result_path
    manifest_path = source_dir / "retry-manifest.json"
    attempt_path = source_dir / "attempt-results.jsonl"
    if not manifest_path.is_file() or not attempt_path.is_file():
        raise FileNotFoundError(f"retry source 缺少 results.jsonl 或完整 attempt 记录：{source_dir}")
    manifest = _read_json(manifest_path)
    parent_id = str(manifest.get("source_evaluation_id") or "")
    if not parent_id:
        raise RuntimeError(f"retry manifest 缺少 source_evaluation_id：{manifest_path}")
    parent_records, _ = _read_retry_source_records(dataset, corpus_run_id, parent_id)
    merged = {
        str(record.get("case_id")): record
        for record in parent_records
        if record.get("case_id")
    }
    merged.update({
        str(record.get("case_id")): record
        for record in _read_multihop_checkpoints(attempt_path)
        if record.get("case_id")
    })
    return list(merged.values()), attempt_path


def _select_experiment_cases(
    corpus_dir: Path,
    cases: list[dict[str, Any]],
    case_set: Literal["all", "analysis", "validation"],
) -> tuple[list[dict[str, Any]], str | None]:
    """Select formal cases from the frozen split without changing the corpus run."""
    if case_set == "all":
        return cases, None
    split_path = corpus_dir / "case-split.json"
    if not split_path.is_file():
        raise RuntimeError("该 corpus run 没有冻结 case-split.json，不能选择分析集或验证集")
    split_hash = _sha256_file(split_path)
    split = _read_json(split_path)
    expected_hash = (corpus_dir / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(expected_hash)
    if manifest.get("case_split_sha256") and manifest["case_split_sha256"] != split_hash:
        raise RuntimeError("case-split.json 哈希与 corpus manifest 不匹配，拒绝使用可能被改写的题集")
    requested_ids = split.get(f"{case_set}_case_ids") or []
    by_id = {str(case.get("id")): case for case in cases}
    missing_ids = [str(case_id) for case_id in requested_ids if str(case_id) not in by_id]
    if missing_ids:
        raise RuntimeError(f"冻结题集引用了缺失题目：{missing_ids[:5]}")
    selected = []
    for case_id in requested_ids:
        case = dict(by_id[str(case_id)])
        case["case_set"] = case_set
        selected.append(case)
    return selected, split_hash


def _manifest_payload_hash(payload: dict[str, Any]) -> str:
    """Hash a target manifest without allowing the hash field to self-reference."""
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


# Target manifests are analysis-only experiments.  The expected count and the
# one permitted behavior variable stay in one table so creation and execution
# cannot silently drift apart.
_TARGET_MANIFEST_CONTRACTS: dict[str, tuple[int, str]] = {
    "rewrite_candidate_fusion": (15, "rewrite_candidate_fusion"),
    "evidence_candidate_audit": (97, "rewrite_candidate_fusion"),
    "adjacent_l3_expansion": (11, "adjacent_l3_expansion"),
    "structured_chunking_offline_audit": (30, "document_chunking_strategy"),
}


def create_rewrite_candidate_fusion_manifest(
    *,
    source_results_path: Path,
    case_split_path: Path,
    output_path: Path,
    source_evaluation_id: str,
    expected_count: int = 15,
) -> Path:
    """Freeze analysis cases that actually entered the existing rewrite branch.

    The manifest intentionally stores case IDs and baseline coverage only. It does
    not copy questions, answers, or retrieved evidence, and it rejects validation
    records before writing any output.
    """
    if not source_results_path.is_file():
        raise FileNotFoundError(f"缺少源评测逐题结果：{source_results_path}")
    if not case_split_path.is_file():
        raise FileNotFoundError(f"缺少冻结 case split：{case_split_path}")
    split = _read_json(case_split_path)
    analysis_ids = {str(case_id) for case_id in split.get("analysis_case_ids") or []}
    validation_ids = {str(case_id) for case_id in split.get("validation_case_ids") or []}
    # This manifest is an immutable source contract.  Unlike resume loading,
    # it must reject duplicate case IDs instead of silently keeping the last
    # checkpoint from a repeated JSONL stream.
    records: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for line_number, line in enumerate(source_results_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if not case_id:
            continue
        if case_id in seen_case_ids:
            raise ValueError(f"源评测包含重复 case ID：{case_id}（第 {line_number} 行）")
        seen_case_ids.add(case_id)
        records.append(record)
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        case_id = str(record.get("case_id") or "")
        if not case_id:
            continue
        if case_id in by_id:
            raise ValueError(f"源评测包含重复 case ID：{case_id}")
        by_id[case_id] = record
    selected: list[dict[str, Any]] = []
    for case_id, record in by_id.items():
        if case_id in validation_ids or record.get("case_set") == "validation":
            continue
        if case_id not in analysis_ids or record.get("case_set") != "analysis":
            continue
        trace = record.get("rag_trace") or {}
        if trace.get("rewrite_method") not in {"step_back", "hyde"}:
            continue
        if _has_evaluation_error(record):
            continue
        selected.append(record)
    selected.sort(key=lambda record: str(record["case_id"]))
    if len(selected) != expected_count:
        raise ValueError(
            f"源评测实际触发改写的 analysis 题数为 {len(selected)}，预期 {expected_count}；拒绝冻结清单"
        )
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "source_evaluation_id": source_evaluation_id,
        "source_results_path": str(source_results_path),
        "source_results_sha256": _sha256_file(source_results_path),
        "source_case_split_path": str(case_split_path),
        "source_case_split_sha256": _sha256_file(case_split_path),
        "case_set": "analysis",
        "case_count": len(selected),
        "case_ids": [str(record["case_id"]) for record in selected],
        "baseline_evidence_coverage": {
            str(record["case_id"]): record.get("evidence_coverage")
            for record in selected
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest["manifest_sha256"] = _manifest_payload_hash(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, manifest)
    return output_path


def create_evidence_candidate_audit_manifest(
    *,
    source_results_path: Path,
    failure_classification_path: Path,
    case_split_path: Path,
    output_path: Path,
    source_evaluation_id: str,
) -> Path:
    """Freeze the 97-case union of retrieval-miss/late/incomplete analysis cases."""
    for path in (source_results_path, failure_classification_path, case_split_path):
        if not path.is_file():
            raise FileNotFoundError(f"缺少 manifest 源文件：{path}")
    classification = _read_json(failure_classification_path)
    categories = classification.get("categories") or {}
    selected_categories = ("retrieval_miss", "retrieval_late", "evidence_incomplete")
    category_ids = {
        category: {str(case_id) for case_id in (categories.get(category, {}).get("case_ids") or [])}
        for category in selected_categories
    }
    expected_category_counts = {"retrieval_miss": 63, "retrieval_late": 19, "evidence_incomplete": 19}
    actual_category_counts = {key: len(value) for key, value in category_ids.items()}
    if actual_category_counts != expected_category_counts:
        raise ValueError(f"T8 证据链分类数量变化，预期 {expected_category_counts}，实际 {actual_category_counts}")
    case_ids = sorted(set().union(*category_ids.values()))
    if len(case_ids) != 97:
        raise ValueError(f"证据链审计清单应为 97 题，实际为 {len(case_ids)}")
    split = _read_json(case_split_path)
    analysis_ids = {str(case_id) for case_id in split.get("analysis_case_ids") or []}
    validation_ids = {str(case_id) for case_id in split.get("validation_case_ids") or []}
    if set(case_ids) - analysis_ids or set(case_ids) & validation_ids:
        raise ValueError("证据链审计清单包含非 analysis 或 validation 题目")
    records = {}
    for line in source_results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if case_id in records:
            raise ValueError(f"源评测包含重复 case ID：{case_id}")
        records[case_id] = record
    missing = [case_id for case_id in case_ids if case_id not in records]
    if missing:
        raise ValueError(f"源评测缺少目标题：{missing[:5]}")
    non_analysis_records = [
        case_id for case_id in case_ids
        if records[case_id].get("case_set") != "analysis"
    ]
    if non_analysis_records:
        raise ValueError(f"源结果中的目标题不是 analysis：{non_analysis_records[:5]}")
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "manifest_type": "evidence_candidate_audit",
        "source_evaluation_id": source_evaluation_id,
        "source_results_path": str(source_results_path),
        "source_results_sha256": _sha256_file(source_results_path),
        "source_failure_classification_path": str(failure_classification_path),
        "source_failure_classification_sha256": _sha256_file(failure_classification_path),
        "source_case_split_path": str(case_split_path),
        "source_case_split_sha256": _sha256_file(case_split_path),
        "case_set": "analysis",
        "case_count": len(case_ids),
        "category_case_counts": {key: len(value) for key, value in category_ids.items()},
        "case_ids": case_ids,
        "case_categories": {case_id: [key for key, ids in category_ids.items() if case_id in ids] for case_id in case_ids},
        "baseline_comparison": {
            case_id: {
                "verdict": records[case_id].get("answer_grade", {}).get("verdict"),
                "evidence_coverage": records[case_id].get("evidence_coverage"),
                "retrieved_filenames": records[case_id].get("retrieved_filenames", []),
                "expected_evidence_filenames": records[case_id].get("expected_evidence_filenames", records[case_id].get("evidence_filenames", [])),
            }
            for case_id in case_ids
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest["manifest_sha256"] = _manifest_payload_hash(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, manifest)
    return output_path


def create_adjacent_l3_expansion_manifest(
    *,
    source_results_path: Path,
    raw_candidate_fact_review_path: Path,
    case_split_path: Path,
    output_path: Path,
    source_evaluation_id: str,
    expected_count: int = 11,
) -> Path:
    """Freeze the manually confirmed analysis cases with an adjacent-L3 gap.

    The manual fact review is the experiment-selection source.  The baseline
    JSONL and frozen split are still hashed and checked so a later rerun cannot
    quietly point the same case IDs at a changed corpus or evaluation result.
    """
    for path in (source_results_path, raw_candidate_fact_review_path, case_split_path):
        if not path.is_file():
            raise FileNotFoundError(f"缺少相邻 L3 清单源文件：{path}")
    split = _read_json(case_split_path)
    analysis_ids = {str(case_id) for case_id in split.get("analysis_case_ids") or []}
    validation_ids = {str(case_id) for case_id in split.get("validation_case_ids") or []}

    source_records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(source_results_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if not case_id:
            continue
        if case_id in source_records:
            raise ValueError(f"源评测包含重复 case ID：{case_id}（第 {line_number} 行）")
        source_records[case_id] = record

    review_records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(raw_candidate_fact_review_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if not case_id:
            raise ValueError(f"人工复核缺少 case ID（第 {line_number} 行）")
        if case_id in review_records:
            raise ValueError(f"人工复核包含重复 case ID：{case_id}（第 {line_number} 行）")
        review_records[case_id] = record

    selected_ids = sorted(
        case_id
        for case_id, record in review_records.items()
        if record.get("classification") == "adjacent_leaf_gap"
    )
    if len(selected_ids) != expected_count:
        raise ValueError(f"相邻 L3 人工复核题数应为 {expected_count}，实际为 {len(selected_ids)}")
    if set(selected_ids) - analysis_ids or set(selected_ids) & validation_ids:
        raise ValueError("相邻 L3 清单包含非 analysis 或 validation 题目")
    missing_records = [case_id for case_id in selected_ids if case_id not in source_records]
    if missing_records:
        raise ValueError(f"源评测缺少相邻 L3 目标题：{missing_records[:5]}")
    non_analysis_records = [
        case_id for case_id in selected_ids
        if source_records[case_id].get("case_set") != "analysis"
    ]
    if non_analysis_records:
        raise ValueError(f"源结果中的相邻 L3 目标题不是 analysis：{non_analysis_records[:5]}")

    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "manifest_type": "adjacent_l3_expansion",
        "changed_variable": "adjacent_l3_expansion",
        "source_evaluation_id": source_evaluation_id,
        "source_results_path": str(source_results_path),
        "source_results_sha256": _sha256_file(source_results_path),
        "source_raw_candidate_fact_review_path": str(raw_candidate_fact_review_path),
        "source_raw_candidate_fact_review_sha256": _sha256_file(raw_candidate_fact_review_path),
        "source_case_split_path": str(case_split_path),
        "source_case_split_sha256": _sha256_file(case_split_path),
        "case_set": "analysis",
        "case_count": len(selected_ids),
        "case_ids": selected_ids,
        "manual_selection": {
            case_id: {
                "classification": review_records[case_id].get("classification"),
                "source_ref": review_records[case_id].get("source_ref"),
                "retrieved_l3_indices": review_records[case_id].get("retrieved_l3_indices", []),
                "evidence_l3_indices": review_records[case_id].get("evidence_l3_indices", []),
                "confidence": review_records[case_id].get("confidence"),
            }
            for case_id in selected_ids
        },
        "baseline_comparison": {
            case_id: {
                "verdict": source_records[case_id].get("answer_grade", {}).get("verdict"),
                "evidence_coverage": source_records[case_id].get("evidence_coverage"),
                "retrieved_filenames": source_records[case_id].get("retrieved_filenames", []),
                "expected_evidence_filenames": source_records[case_id].get(
                    "expected_evidence_filenames", source_records[case_id].get("evidence_filenames", [])
                ),
            }
            for case_id in selected_ids
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest["manifest_sha256"] = _manifest_payload_hash(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, manifest)
    return output_path


def create_adjacent_l3_non_pass_manifest(
    *,
    source_results_path: Path,
    case_split_path: Path,
    output_path: Path,
    source_evaluation_id: str,
    expected_count: int = 147,
) -> Path:
    """Freeze every non-pass analysis result for an adjacent-L3 rerun.

    This is deliberately separate from the manually selected 11-case T10
    manifest.  The source results remain the selection authority, so a pass
    case or validation case cannot be added by editing a target list.
    """
    for path in (source_results_path, case_split_path):
        if not path.is_file():
            raise FileNotFoundError(f"缺少相邻 L3 非通过题清单源文件：{path}")
    split = _read_json(case_split_path)
    analysis_ids = {str(case_id) for case_id in split.get("analysis_case_ids") or []}
    validation_ids = {str(case_id) for case_id in split.get("validation_case_ids") or []}

    source_records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(source_results_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = str(record.get("case_id") or "")
        if not case_id:
            continue
        if case_id in source_records:
            raise ValueError(f"源评测包含重复 case ID：{case_id}（第 {line_number} 行）")
        source_records[case_id] = record

    baseline_verdict_counts: dict[str, int] = {}
    non_pass_ids: list[str] = []
    pass_ids: list[str] = []
    for case_id, record in source_records.items():
        if case_id in validation_ids or record.get("case_set") != "analysis":
            continue
        verdict = str((record.get("answer_grade") or {}).get("verdict") or "")
        baseline_verdict_counts[verdict] = baseline_verdict_counts.get(verdict, 0) + 1
        if verdict == "pass":
            pass_ids.append(case_id)
        else:
            non_pass_ids.append(case_id)
    selected_ids = sorted(non_pass_ids)
    if len(selected_ids) != expected_count:
        raise ValueError(f"analysis 非通过题数应为 {expected_count}，实际为 {len(selected_ids)}")
    if set(selected_ids) - analysis_ids or set(selected_ids) & validation_ids:
        raise ValueError("相邻 L3 非通过清单包含非 analysis 或 validation 题目")

    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "manifest_type": "adjacent_l3_expansion",
        "selection_mode": "non_pass_analysis",
        "source_selection_rule": "case_set=analysis and answer_grade.verdict != pass",
        "changed_variable": "adjacent_l3_expansion",
        "source_evaluation_id": source_evaluation_id,
        "source_results_path": str(source_results_path),
        "source_results_sha256": _sha256_file(source_results_path),
        "source_case_split_path": str(case_split_path),
        "source_case_split_sha256": _sha256_file(case_split_path),
        "case_set": "analysis",
        "case_count": len(selected_ids),
        "case_ids": selected_ids,
        "excluded_pass_count": len(pass_ids),
        "baseline_verdict_counts": baseline_verdict_counts,
        "baseline_comparison": {
            case_id: {
                "verdict": (source_records[case_id].get("answer_grade") or {}).get("verdict"),
                "evidence_coverage": source_records[case_id].get("evidence_coverage"),
                "retrieved_filenames": source_records[case_id].get("retrieved_filenames", []),
                "expected_evidence_filenames": source_records[case_id].get(
                    "expected_evidence_filenames", source_records[case_id].get("evidence_filenames", [])
                ),
            }
            for case_id in selected_ids
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest["manifest_sha256"] = _manifest_payload_hash(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, manifest)
    return output_path


def _select_target_manifest_cases(
    *,
    corpus_dir: Path,
    all_cases: list[dict[str, Any]],
    case_set: Literal["all", "analysis", "validation"],
    target_manifest_path: Path,
) -> tuple[list[dict[str, Any]], str]:
    """Validate a frozen analysis target manifest against the current corpus split."""
    if case_set != "analysis":
        raise ValueError("target manifest 只能用于 analysis case set")
    payload = _read_json(target_manifest_path)
    if payload.get("case_set") != "analysis":
        raise ValueError("target manifest 的 case_set 必须是 analysis")
    if payload.get("manifest_sha256") != _manifest_payload_hash(payload):
        raise ValueError("target manifest 自身哈希校验失败，拒绝使用")
    case_ids = [str(case_id) for case_id in payload.get("case_ids") or []]
    if len(case_ids) != int(payload.get("case_count") or 0) or len(set(case_ids)) != len(case_ids):
        raise ValueError("target manifest 的 case_count 与唯一 case_ids 不一致")
    manifest_type = str(payload.get("manifest_type") or "rewrite_candidate_fusion")
    if manifest_type not in _TARGET_MANIFEST_CONTRACTS:
        raise ValueError(f"不支持的 target manifest 类型：{manifest_type}")
    expected_count, expected_variable = _TARGET_MANIFEST_CONTRACTS[manifest_type]
    selection_mode = str(payload.get("selection_mode") or "")
    if manifest_type == "adjacent_l3_expansion" and not selection_mode:
        selection_mode = "adjacent_leaf_gap"
    if manifest_type == "adjacent_l3_expansion" and selection_mode not in {
        "adjacent_leaf_gap", "non_pass_analysis"
    }:
        raise ValueError("相邻 L3 target manifest 的 selection_mode 不受支持")
    # T10 manifests created after this contract was introduced carry their
    # variable explicitly.  Keep accepting older T9/T8 manifests that predate
    # the field, but reject a declared value that could route the target to a
    # different runtime behavior.
    declared_variable = payload.get("changed_variable")
    if declared_variable is not None and declared_variable != expected_variable:
        raise ValueError(
            f"target manifest 的 changed_variable 必须为 {expected_variable}"
        )
    if not (manifest_type == "adjacent_l3_expansion" and selection_mode == "non_pass_analysis") and len(case_ids) != expected_count:
        raise ValueError(f"target manifest 必须固定包含 {expected_count} 道 analysis 题")
    selected_analysis, split_hash = _select_experiment_cases(corpus_dir, all_cases, "analysis")
    analysis_ids = {str(case["id"]) for case in selected_analysis}
    if not set(case_ids).issubset(analysis_ids):
        raise ValueError("target manifest 包含非 analysis 题目或不属于当前冻结 split 的题目")
    if payload.get("source_case_split_sha256") != split_hash:
        raise ValueError("target manifest 的 case-split 哈希与当前 corpus 不匹配")
    source_results = Path(str(payload.get("source_results_path") or ""))
    if not source_results.is_file() or _sha256_file(source_results) != payload.get("source_results_sha256"):
        raise ValueError("target manifest 的源 results.jsonl 哈希不匹配")
    source_result_records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(source_results.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        result_id = str(record.get("case_id") or "")
        if result_id in source_result_records:
            raise ValueError(f"target manifest 的源 results.jsonl 包含重复 case ID：{result_id}（第 {line_number} 行）")
        if result_id:
            source_result_records[result_id] = record
    if manifest_type == "evidence_candidate_audit":
        classification_path = Path(str(payload.get("source_failure_classification_path") or ""))
        if not classification_path.is_file() or _sha256_file(classification_path) != payload.get("source_failure_classification_sha256"):
            raise ValueError("target manifest 的失败分类源文件哈希不匹配")
        classification = _read_json(classification_path)
        categories = classification.get("categories") or {}
        selected_categories = ("retrieval_miss", "retrieval_late", "evidence_incomplete")
        expected_ids = {
            str(case_id)
            for category in selected_categories
            for case_id in (categories.get(category, {}).get("case_ids") or [])
        }
        if set(case_ids) != expected_ids:
            raise ValueError("target manifest 与失败分类的三类证据链题目不一致")
        expected_category_counts = {category: len({str(case_id) for case_id in (categories.get(category, {}).get("case_ids") or [])}) for category in selected_categories}
        if payload.get("category_case_counts") != expected_category_counts:
            raise ValueError("target manifest 的分类数量与失败分类源文件不一致")
        manifest_categories = payload.get("case_categories") or {}
        for case_id in case_ids:
            expected_membership = [category for category in selected_categories if case_id in {str(value) for value in (categories.get(category, {}).get("case_ids") or [])}]
            if manifest_categories.get(case_id) != expected_membership:
                raise ValueError(f"target manifest 的分类成员关系不一致：{case_id}")
    if manifest_type == "adjacent_l3_expansion" and selection_mode == "non_pass_analysis":
        expected_ids = sorted(
            case_id
            for case_id, record in source_result_records.items()
            if record.get("case_set") == "analysis"
            and str((record.get("answer_grade") or {}).get("verdict") or "") != "pass"
        )
        if case_ids != expected_ids:
            raise ValueError("target manifest 与源 results.jsonl 的 analysis 非通过题集合不一致")
        if payload.get("source_selection_rule") != "case_set=analysis and answer_grade.verdict != pass":
            raise ValueError("target manifest 的 source_selection_rule 不正确")
        if payload.get("excluded_pass_count") != sum(
            1
            for record in source_result_records.values()
            if record.get("case_set") == "analysis"
            and str((record.get("answer_grade") or {}).get("verdict") or "") == "pass"
        ):
            raise ValueError("target manifest 的 excluded_pass_count 与源结果不一致")
        expected_counts: dict[str, int] = {}
        for source_record in source_result_records.values():
            if source_record.get("case_set") != "analysis":
                continue
            verdict = str((source_record.get("answer_grade") or {}).get("verdict") or "")
            expected_counts[verdict] = expected_counts.get(verdict, 0) + 1
        if payload.get("baseline_verdict_counts") != expected_counts:
            raise ValueError("target manifest 的 baseline_verdict_counts 与源结果不一致")
        baseline_comparison = payload.get("baseline_comparison") or {}
        for case_id in case_ids:
            source_record = source_result_records[case_id]
            expected_baseline = {
                "verdict": (source_record.get("answer_grade") or {}).get("verdict"),
                "evidence_coverage": source_record.get("evidence_coverage"),
                "retrieved_filenames": source_record.get("retrieved_filenames", []),
                "expected_evidence_filenames": source_record.get(
                    "expected_evidence_filenames", source_record.get("evidence_filenames", [])
                ),
            }
            if baseline_comparison.get(case_id) != expected_baseline:
                raise ValueError(f"target manifest 的基线摘要与源结果不一致：{case_id}")
    elif manifest_type == "structured_chunking_offline_audit":
        review_path = Path(str(payload.get("source_raw_review") or ""))
        if not review_path.is_file() or _sha256_file(review_path) != payload.get("source_raw_review_sha256"):
            raise ValueError("结构化分块 target manifest 的人工复核源文件哈希不匹配")
        review_by_id = {
            str(record.get("case_id")): record
            for record in (
                json.loads(line)
                for line in review_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        }
        targets_by_id = {
            str(target.get("case_id")): target
            for target in payload.get("targets") or []
        }
        if set(targets_by_id) != set(case_ids):
            raise ValueError("结构化分块 target 的 targets 与 case_ids 不一致")
        for case_id in case_ids:
            review = review_by_id.get(case_id)
            target = targets_by_id[case_id]
            if review is None or review.get("case_set") != "analysis":
                raise ValueError(f"结构化分块 target 的人工复核缺少 analysis 题：{case_id}")
            if review.get("classification") != "same_source_far_leaf_gap":
                raise ValueError(f"结构化分块 target 含非远距离叶块缺口题：{case_id}")
            if target.get("case_set") != "analysis" or target.get("source_ref") != review.get("source_ref"):
                raise ValueError(f"结构化分块 target 的来源摘要不匹配：{case_id}")
    elif manifest_type == "adjacent_l3_expansion":
        review_path = Path(str(payload.get("source_raw_candidate_fact_review_path") or ""))
        if not review_path.is_file() or _sha256_file(review_path) != payload.get("source_raw_candidate_fact_review_sha256"):
            raise ValueError("target manifest 的相邻 L3 人工复核源文件哈希不匹配")
        review_ids: set[str] = set()
        review_records: dict[str, dict[str, Any]] = {}
        seen_review_ids: set[str] = set()
        for line_number, line in enumerate(review_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            review = json.loads(line)
            review_id = str(review.get("case_id") or "")
            if not review_id:
                raise ValueError(f"相邻 L3 人工复核缺少 case ID（第 {line_number} 行）")
            if review_id in seen_review_ids:
                raise ValueError(f"相邻 L3 人工复核包含重复 case ID：{review_id}")
            seen_review_ids.add(review_id)
            review_records[review_id] = review
            if review.get("classification") == "adjacent_leaf_gap":
                review_ids.add(review_id)
        if set(case_ids) != review_ids:
            raise ValueError("target manifest 与相邻 L3 人工复核结论不一致")
        manual_selection = payload.get("manual_selection") or {}
        baseline_comparison = payload.get("baseline_comparison") or {}
        for case_id in case_ids:
            review = review_records[case_id]
            expected_selection = {
                "classification": review.get("classification"),
                "source_ref": review.get("source_ref"),
                "retrieved_l3_indices": review.get("retrieved_l3_indices", []),
                "evidence_l3_indices": review.get("evidence_l3_indices", []),
                "confidence": review.get("confidence"),
            }
            if manual_selection.get(case_id) != expected_selection:
                raise ValueError(f"target manifest 的人工选择摘要与源复核不一致：{case_id}")
            source_record = source_result_records.get(case_id)
            if source_record is None:
                raise ValueError(f"target manifest 的源 results.jsonl 缺少题目：{case_id}")
            expected_baseline = {
                "verdict": source_record.get("answer_grade", {}).get("verdict"),
                "evidence_coverage": source_record.get("evidence_coverage"),
                "retrieved_filenames": source_record.get("retrieved_filenames", []),
                "expected_evidence_filenames": source_record.get(
                    "expected_evidence_filenames",
                    source_record.get("evidence_filenames", []),
                ),
            }
            if baseline_comparison.get(case_id) != expected_baseline:
                raise ValueError(f"target manifest 的基线摘要与源结果不一致：{case_id}")
    by_id = {str(case["id"]): dict(case) for case in selected_analysis}
    baseline_comparison = payload.get("baseline_comparison") or {}
    for case_id, case in by_id.items():
        if case_id in baseline_comparison:
            case["_baseline_comparison"] = baseline_comparison[case_id]
    return [by_id[case_id] for case_id in case_ids], split_hash


def _add_case_set_to_records(records: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    case_sets = {str(case["id"]): str(case.get("case_set") or "all") for case in cases}
    for record in records:
        record["case_set"] = case_sets.get(str(record.get("case_id")), "all")


def _enterprise_question_type_summary(
    records: list[dict[str, Any]],
    *,
    evaluation_mode: Literal["retrieval", "rag"],
    included_strategies: list[str] | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if evaluation_mode == "retrieval" and included_strategies is not None:
            if record.get("strategy") not in included_strategies:
                continue
        grouped[str(record.get("question_type") or "unknown")].append(record)
    if evaluation_mode == "retrieval":
        strategies = included_strategies or []
        return {
            question_type: {
                strategy: retrieval_metrics([
                    record for record in records
                    if record.get("question_type") == question_type and record.get("strategy") == strategy
                ])
                for strategy in strategies
            }
            for question_type in sorted(grouped)
        }
    return {
        question_type: multihop_metrics(type_records)
        for question_type, type_records in sorted(grouped.items())
    }


def _manual_review_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Export every outcome that cannot be safely treated as an automatic conclusion."""
    queue_records: list[dict[str, Any]] = []
    for record in records:
        reasons: list[str] = []
        grade = record.get("answer_grade") or {}
        verdict = grade.get("verdict")
        coverage = float(record.get("evidence_coverage") or 0.0)
        if verdict == "review":
            reasons.append("grader_review")
        if record.get("case_set") == "validation" and verdict != "pass":
            reasons.append("validation_failure")
        if verdict == "fail" and coverage >= 1.0:
            reasons.append("evidence_complete_answer_failed")
        if verdict == "pass" and coverage < 1.0:
            reasons.append("answer_passed_evidence_incomplete")
        evidence_grading_error = (
            (record.get("rag_trace") or {}).get("evidence_reason")
            == "evidence_grading_unavailable"
        )
        if (
            record.get("evaluation_error")
            or record.get("answer_generation_error")
            or (grade.get("grader_error") or "")
            or evidence_grading_error
        ):
            reasons.append("system_error")
        if reasons:
            queue_records.append({"review_reasons": reasons, "record": record})
    return queue_records


def _write_manual_review_queue(
    output_dir: Path,
    records: list[dict[str, Any]],
    *,
    suppress_validation_details: bool = False,
) -> list[dict[str, Any]]:
    """Write the review queue without exposing blind validation-case details."""
    queue_records = _manual_review_records(records)
    suppressed_validation_count = 0
    if suppress_validation_details:
        suppressed_validation_count = sum(
            item["record"].get("case_set") == "validation"
            for item in queue_records
        )
        # Keep validation candidates in the machine queue so blind review does
        # not silently discard work. Details remain available only in the
        # controlled results.jsonl audit source.
        queue_records = [
            item if item["record"].get("case_set") != "validation" else {
                "review_reasons": ["validation_case_suppressed"],
                "record": {
                    "case_set": "validation",
                    "redacted": True,
                },
            }
            for item in queue_records
        ]
    _write_results(output_dir / "manual-review.jsonl", queue_records)
    visible_queue_records = (
        [
            item for item in queue_records
            if item["record"].get("case_set") != "validation"
        ]
        if suppress_validation_details
        else queue_records
    )
    lines = ["# 人工复核队列", "", f"待复核样本数：{len(visible_queue_records)}", ""]
    if suppressed_validation_count:
        lines.extend([
            f"验证集待复核条目：{suppressed_validation_count}（按盲态规则隐藏）",
            "",
        ])
    for item in visible_queue_records:
        record = item["record"]
        case_id = str(record.get("case_id") or "unknown")
        lines.append(
            f"- [`{case_id}`](case-review.md#{case_id.lower()}) "
            f"({record.get('question_type')}, "
            f"{record.get('case_set')}): {', '.join(item['review_reasons'])}"
        )
    (output_dir / "manual-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return queue_records


def _display_value(value: Any, empty: str = "（无）") -> str:
    """将逐题报告中的空值统一成可读文本。"""
    if value is None:
        return empty
    text = str(value).strip()
    return text or empty


def _quote_markdown(value: Any) -> list[str]:
    """以 Markdown 引用块展示题目、答案等可能包含换行的长文本。"""
    text = _display_value(value)
    return [f"> {line}" if line else ">" for line in text.splitlines()]


def _filename_lines(filenames: Iterable[Any]) -> list[str]:
    values = [str(filename) for filename in filenames if filename]
    if not values:
        return ["（无召回文档）"]
    return [f"{index}. `{filename}`" for index, filename in enumerate(values, 1)]


def _case_review_rag_lines(record: dict[str, Any]) -> list[str]:
    grade = record.get("answer_grade") or {}
    expected = record.get("expected_evidence_filenames") or []
    retrieved = record.get("retrieved_filenames") or []
    ranks = record.get("evidence_ranks") or []
    errors = [
        str(record.get(name))
        for name in ("evaluation_error", "answer_generation_error", "grader_error")
        if record.get(name)
    ]
    verdict = str(grade.get("verdict") or "review")
    human_review_status = "待人工复核" if verdict == "review" else "未进入人工复核队列"
    lines = [
        f"### {_display_value(record.get('case_id'))}",
        "",
        f"- 题型：`{_display_value(record.get('question_type'))}`",
        f"- 题集：`{_display_value(record.get('case_set'))}`",
        f"- 自动判定：`{verdict}`",
        "",
        "#### 问题",
        "",
        *_quote_markdown(record.get("question")),
        "",
        "#### 参考答案",
        "",
        *_quote_markdown(record.get("reference_answer")),
        "",
        "#### 模型回答",
        "",
        *_quote_markdown(record.get("answer")),
        "",
        "#### 证据检查",
        "",
        f"- 标准证据数：{len(expected)}",
        f"- 标准证据召回排名：{', '.join(str(rank) for rank in ranks) or '未召回'}",
        f"- 证据覆盖率：{_format_metric(record.get('evidence_coverage', 0.0))}",
        "- 标准证据文件：",
        *_filename_lines(expected),
        "- 实际召回文件（按排名）：",
        *_filename_lines(retrieved),
        "",
        "#### 判定理由",
        "",
        *_quote_markdown(grade.get("reason")),
        "",
        "#### 人工复核状态",
        "",
        f"- {human_review_status}",
        "",
        "#### 耗时",
        "",
        f"- RAG：{_format_metric(record.get('rag_seconds', 0.0))} s",
        f"- 生成：{_format_metric(record.get('generation_seconds', 0.0))} s",
        f"- 判卷：{_format_metric(record.get('judge_seconds', 0.0))} s",
        f"- 端到端：{_format_metric(record.get('end_to_end_seconds', 0.0))} s",
    ]
    if errors:
        lines.extend(["", "#### 系统异常", "", *_quote_markdown("\n".join(errors))])
    lines.extend(["", "---", ""])
    return lines


def _case_review_retrieval_lines(case_id: str, records: list[dict[str, Any]]) -> list[str]:
    first = records[0] if records else {}
    expected = first.get("expected_evidence_filenames") or first.get("relevant_filenames") or []
    lines = [
        f"### {_display_value(case_id)}",
        "",
        f"- 题型：`{_display_value(first.get('question_type'))}`",
        "",
        "#### 问题",
        "",
        *_quote_markdown(first.get("question")),
        "",
        "#### 标准证据",
        "",
        *_filename_lines(expected),
        "",
        "#### 各检索策略",
        "",
    ]
    for record in records:
        ranks = record.get("relevant_ranks") or []
        retrieved = record.get("retrieved_filenames") or []
        lines.extend([
            f"##### {_display_value(record.get('strategy'))}",
            "",
            f"- 标准证据排名：{', '.join(str(rank) for rank in ranks) or '未召回'}",
            f"- 检索耗时：{_format_metric(record.get('retrieval_seconds', 0.0))} s",
            f"- Rerank 是否生效：{_display_value(record.get('rerank_applied'), '否')}",
            "- 召回文件（按排名）：",
            *_filename_lines(retrieved),
            "",
        ])
    lines.extend(["---", ""])
    return lines


def _write_case_review_report(
    output_dir: Path,
    records: list[dict[str, Any]],
    *,
    dataset: str,
    evaluation_mode: Literal["retrieval", "rag"],
    suppress_validation_details: bool = False,
) -> Path:
    """Write a human-readable per-case report beside the machine JSONL checkpoint."""
    visible_records = [
        record
        for record in records
        if not (suppress_validation_details and record.get("case_set") == "validation")
    ]
    lines = [
        f"# {dataset} 逐题评测报告",
        "",
        f"评测模式：`{evaluation_mode}`",
        f"可读题目数：{len({str(record.get('case_id')) for record in visible_records})}",
        "",
        "本文件把逐题 JSONL 转成便于人工阅读的题目、答案、证据和判定记录。",
    ]
    if suppress_validation_details:
        lines.extend([
            "验证集详情已按盲态规则隐藏；完整原始记录仅保存在受控的 `results.jsonl`。",
        ])
    lines.extend(["", "---", ""])

    if evaluation_mode == "retrieval":
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in visible_records:
            grouped.setdefault(str(record.get("case_id")), []).append(record)
        for case_id, case_records in grouped.items():
            lines.extend(_case_review_retrieval_lines(case_id, case_records))
    else:
        for record in visible_records:
            lines.extend(_case_review_rag_lines(record))

    path = output_dir / "case-review.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


def _write_evaluation_progress(
    output_dir: Path,
    *,
    total_cases: int,
    completed_case_ids: set[str],
    status: Literal["running", "interrupted", "completed"],
    failed_case_id: str | None = None,
    error: str = "",
) -> None:
    """保存无敏感信息的恢复状态，便于区分外部调用失败与已完成评测。"""
    _write_json(output_dir / "evaluation-progress.json", {
        "status": status,
        "total_cases": total_cases,
        "completed_cases": len(completed_case_ids),
        "completed_case_ids": sorted(completed_case_ids),
        "failed_case_id": failed_case_id,
        "error": error,
        "updated_at": datetime.now(UTC).isoformat(),
    })


def _format_metric(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def _ecom_report(summary: dict[str, Any]) -> str:
    lines = ["# EcomRetrieval 检索评测报告", "", "仅比较检索排序，不调用回答模型。", ""]
    lines.extend(["| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50(s) | P95(s) | 失败 | Dense 降级 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for strategy, metrics in summary["strategies"].items():
        lines.append(
            "| " + " | ".join([
                strategy,
                _format_metric(metrics["recall_at_1"]),
                _format_metric(metrics["recall_at_3"]),
                _format_metric(metrics["recall_at_5"]),
                _format_metric(metrics["mrr_at_10"]),
                _format_metric(metrics["retrieval_latency_p50_seconds"]),
                _format_metric(metrics["retrieval_latency_p95_seconds"]),
                str(metrics["failed_cases"]),
                str(metrics["dense_fallback_cases"]),
            ]) + " |"
        )
    if summary.get("by_question_type"):
        lines.extend(["", "## 按题型", ""])
        for question_type, strategies in summary["by_question_type"].items():
            lines.append(f"### {question_type}")
            lines.append("")
            lines.append("| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for strategy, metrics in strategies.items():
                lines.append(
                    "| " + " | ".join([
                        strategy,
                        _format_metric(metrics["recall_at_1"]),
                        _format_metric(metrics["recall_at_3"]),
                        _format_metric(metrics["recall_at_5"]),
                        _format_metric(metrics["mrr_at_10"]),
                    ]) + " |"
                )
            lines.append("")
    lines.extend([
        "",
        f"精排状态：`{summary['rerank_strategy_status']}`；"
        f"已成功执行 {summary['rerank_successful_calls']}/{summary['rerank_attempted_cases']} 题。",
        "`hybrid_rerank` 仅在精排服务配置完整且全部题目实际调用成功时参与指标对比。"
        "未生效题目的诊断仍保留在 `results.jsonl`，但不会混入精排策略指标。",
        "",
    ])
    return "\n".join(lines)


def evaluate_ecom(output_dir: Path, config: dict[str, Any], cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在同一个隔离集合中运行四种检索策略。"""
    store = _evaluation_store(config["collection_name"])
    strategies = ["bm25", "dense", "hybrid"]
    if RERANK_ENABLED:
        strategies.append("hybrid_rerank")
    records: list[dict[str, Any]] = []
    for strategy in strategies:
        runtime = RetrievalRuntime(
            milvus_store=store,
            embedding_service=embedding_service,
            retrieval_mode="hybrid" if strategy == "hybrid_rerank" else strategy,  # type: ignore[arg-type]
            enable_auto_merge=False,
            enable_rerank=strategy == "hybrid_rerank",
        )
        for case in cases:
            started = time.perf_counter()
            result = retrieve_documents(case["question"], top_k=10, runtime=runtime)
            elapsed = time.perf_counter() - started
            docs = result.get("docs", [])
            meta = result.get("meta", {})
            records.append({
                "dataset": "ecomretrieval",
                "strategy": strategy,
                "case_id": case["id"],
                "question": case["question"],
                "relevant_filenames": case["relevant_filenames"],
                "retrieved_filenames": _retrieved_filenames(docs),
                "relevant_ranks": _ranks(case["relevant_filenames"], docs),
                "retrieval_mode": meta.get("retrieval_mode"),
                "rerank_applied": meta.get("rerank_applied", False),
                "rerank_error": meta.get("rerank_error"),
                "retrieval_seconds": elapsed,
            })
    rerank_records = [record for record in records if record["strategy"] == "hybrid_rerank"]
    rerank_success_count = sum(
        bool(record["rerank_applied"]) and not record["rerank_error"] for record in rerank_records
    )
    rerank_attempted_cases = len(rerank_records)
    rerank_effective = bool(rerank_attempted_cases) and rerank_success_count == rerank_attempted_cases
    if "hybrid_rerank" in strategies and not rerank_effective:
        strategies.remove("hybrid_rerank")
    if not RERANK_ENABLED:
        rerank_status = "not_configured"
    elif rerank_effective:
        rerank_status = "effective"
    else:
        rerank_status = "not_effective"
    summary = {
        "dataset": "ecomretrieval",
        "run_id": config["run_id"],
        "case_count": len(cases),
        "strategies": {
            strategy: retrieval_metrics([record for record in records if record["strategy"] == strategy])
            for strategy in strategies
        },
        "rerank_strategy_configured": RERANK_ENABLED,
        "rerank_strategy_status": rerank_status,
        "rerank_attempted_cases": rerank_attempted_cases,
        "rerank_successful_calls": rerank_success_count,
        "rerank_unsuccessful_calls": rerank_attempted_cases - rerank_success_count,
        "rerank_strategy_effective": rerank_effective,
    }
    return records, summary


def evaluate_enterprise_retrieval(
    output_dir: Path,
    config: dict[str, Any],
    cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在 EnterpriseRAG 语料上隔离比较检索排序，不调用回答模型。"""
    store = _evaluation_store(config["collection_name"])
    strategies = ["bm25", "dense", "hybrid"]
    if RERANK_ENABLED:
        strategies.append("hybrid_rerank")
    records: list[dict[str, Any]] = []
    for strategy in strategies:
        runtime = RetrievalRuntime(
            milvus_store=store,
            embedding_service=embedding_service,
            retrieval_mode="hybrid" if strategy == "hybrid_rerank" else strategy,  # type: ignore[arg-type]
            enable_auto_merge=False,
            enable_rerank=strategy == "hybrid_rerank",
        )
        for case in cases:
            started = time.perf_counter()
            result = retrieve_documents(case["question"], top_k=RETRIEVAL_TOP_K, runtime=runtime)
            elapsed = time.perf_counter() - started
            docs = result.get("docs", [])
            meta = result.get("meta", {})
            expected = case["expected_evidence_filenames"]
            records.append({
                "dataset": "enterpriserag",
                "language": config.get("language", "en"),
                "corpus": config.get("corpus", "representative"),
                "strategy": strategy,
                "case_id": case["id"],
                "question": case["question"],
                "question_type": case["question_type"],
                "expected_evidence_filenames": expected,
                "retrieved_filenames": _retrieved_filenames(docs),
                "relevant_ranks": _ranks(expected, docs),
                "retrieval_mode": meta.get("retrieval_mode"),
                "rerank_applied": meta.get("rerank_applied", False),
                "rerank_error": meta.get("rerank_error"),
                "retrieval_seconds": elapsed,
            })
    rerank_records = [record for record in records if record["strategy"] == "hybrid_rerank"]
    rerank_success_count = sum(
        bool(record["rerank_applied"]) and not record["rerank_error"]
        for record in rerank_records
    )
    rerank_attempted_cases = len(rerank_records)
    rerank_effective = bool(rerank_attempted_cases) and rerank_success_count == rerank_attempted_cases
    if "hybrid_rerank" in strategies and not rerank_effective:
        strategies.remove("hybrid_rerank")
    rerank_status = (
        "not_configured"
        if not RERANK_ENABLED
        else "effective" if rerank_effective else "not_effective"
    )
    summary = {
        "dataset": "enterpriserag",
        "run_id": config["run_id"],
        "case_count": len(cases),
        "language": config.get("language", "en"),
        "corpus": config.get("corpus", "representative"),
        "strategies": {
            strategy: retrieval_metrics([record for record in records if record["strategy"] == strategy])
            for strategy in strategies
        },
        "rerank_strategy_configured": RERANK_ENABLED,
        "rerank_strategy_status": rerank_status,
        "rerank_attempted_cases": rerank_attempted_cases,
        "rerank_successful_calls": rerank_success_count,
        "rerank_unsuccessful_calls": rerank_attempted_cases - rerank_success_count,
        "rerank_strategy_effective": rerank_effective,
    }
    summary["by_question_type"] = _enterprise_question_type_summary(
        records,
        evaluation_mode="retrieval",
        included_strategies=strategies,
    )
    return records, summary


def _enterprise_retrieval_report(summary: dict[str, Any]) -> str:
    lines = [
        "# EnterpriseRAG 检索评测报告",
        "",
        f"语言：`{summary.get('language', 'en')}`；语料：`{summary.get('corpus', 'representative')}`。",
        "仅比较检索排序，不调用回答模型。",
        "",
        "| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50(s) | P95(s) | 失败 | Dense 降级 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for strategy, metrics in summary["strategies"].items():
        lines.append(
            "| " + " | ".join([
                strategy,
                _format_metric(metrics["recall_at_1"]),
                _format_metric(metrics["recall_at_3"]),
                _format_metric(metrics["recall_at_5"]),
                _format_metric(metrics["mrr_at_10"]),
                _format_metric(metrics["retrieval_latency_p50_seconds"]),
                _format_metric(metrics["retrieval_latency_p95_seconds"]),
                str(metrics["failed_cases"]),
                str(metrics["dense_fallback_cases"]),
            ]) + " |"
        )
    if summary.get("by_question_type"):
        lines.extend(["", "## 按题型", ""])
        for question_type, strategies in summary["by_question_type"].items():
            lines.append(f"### {question_type}")
            lines.append("")
            lines.append("| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for strategy, metrics in strategies.items():
                lines.append(
                    "| " + " | ".join([
                        strategy,
                        _format_metric(metrics["recall_at_1"]),
                        _format_metric(metrics["recall_at_3"]),
                        _format_metric(metrics["recall_at_5"]),
                        _format_metric(metrics["mrr_at_10"]),
                    ]) + " |"
                )
            lines.append("")
    lines.extend([
        "",
        f"精排状态：`{summary['rerank_strategy_status']}`；"
        f"已成功执行 {summary['rerank_successful_calls']}/{summary['rerank_attempted_cases']} 题。",
        "精排未全量成功时不纳入策略对比，但逐题诊断仍保留。",
    ])
    return "\n".join(lines)


def _first_json_object(text: str) -> dict[str, Any]:
    payload, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
    if not isinstance(payload, dict):
        raise ValueError("判卷响应不是 JSON 对象")
    return payload


def _judge_config() -> JudgeConfig | None:
    base_url = (os.getenv("BASE_URL") or "").strip()
    model = (os.getenv("GRADE_MODEL") or "").strip()
    api_key = (os.getenv("ARK_API_KEY") or "").strip()
    if not (base_url and model and api_key):
        return None
    return JudgeConfig(
        base_url=base_url,
        model=model,
        timeout_seconds=model_timeout_seconds(),
    )


def judge_multihop_answer(
    case: dict[str, Any],
    answer: str,
    *,
    client: requests.Session | None,
    config: JudgeConfig | None,
) -> dict[str, Any]:
    """以独立 GRADE_MODEL 请求判卷；任何异常都显式转人工复核。"""
    if not client or not config:
        return {
            "verdict": "review",
            "reason": "GRADE_MODEL 未完整配置，需人工复核。",
            "grader_error": "judge_not_configured",
            "judge_request_type": "independent_grade_model",
        }
    rubric = {
        "question": case["question"],
        "reference_answer": case["reference_answer"],
        "question_type": case["question_type"],
        "assistant_answer": answer,
    }
    prompt = (
        "你是独立 RAG 回答阅卷器。只能依据参考答案评分，不能引入外部知识。"
        "null 或 info_not_found 类型题只有明确拒答、说明资料不足且不编造事实才算 pass。"
        "只返回 JSON：{\"verdict\":\"pass|fail|review\",\"reason\":\"...\"}。\n\n"
        + json.dumps(rubric, ensure_ascii=False)
    )
    try:
        response = client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            json={
                "model": config.model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        payload = _first_json_object(content)
        verdict = str(payload.get("verdict", "review")).lower()
        if verdict not in {"pass", "fail", "review"}:
            verdict = "review"
        return {
            "verdict": verdict,
            "reason": str(payload.get("reason") or ""),
            "grader_error": "",
            "grader_model": config.model,
            "judge_request_type": "independent_grade_model",
        }
    except (requests.RequestException, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "verdict": "review",
            "reason": "独立判卷请求失败或无法解析，需人工复核。",
            "grader_error": str(exc),
            "grader_model": config.model,
            "judge_request_type": "independent_grade_model",
        }


def _answer_from_evidence(question: str, state: dict[str, Any]) -> tuple[str, str]:
    """复用主回答模型，仅根据完整 RAG 图输出的最终证据作答。"""
    if not state.get("docs"):
        return "现有知识库中没有足够的可靠资料回答这个问题。", ""
    try:
        from backend.chat.runtime import model

        prompt = (
            "你是企业知识库智能问答助手。仅依据以下检索证据回答问题；"
            "证据不足时明确说明无法确认，不要补充外部事实。使用 [编号] 引用证据。\n\n"
            f"问题：{question}\n\n证据：\n{state.get('context') or ''}"
        )
        response = model.invoke(prompt)
        content = getattr(response, "content", response)
        return str(content), ""
    except Exception as exc:
        return "", str(exc)


def _multihop_error_record(
    case: dict[str, Any],
    exc: BaseException,
    *,
    elapsed_seconds: float,
    state: dict[str, Any] | None = None,
    dataset_label: str = "multihoprag",
) -> dict[str, Any]:
    """将单题执行异常转为可人工复核、可聚合的标准结果。"""
    current_state = state or {}
    trace = current_state.get("rag_trace") or {}
    docs = current_state.get("docs") or []
    expected = case["evidence_filenames"]
    evidence_hits = _ranks(expected, docs)
    coverage = _evidence_coverage(case, docs)
    return {
        "dataset": dataset_label,
        "case_id": case["id"],
        "question": case["question"],
        "question_type": case["question_type"],
        "reference_answer": case["reference_answer"],
        "expected_evidence_filenames": expected,
        "retrieved_filenames": _retrieved_filenames(docs),
        "evidence_ranks": evidence_hits,
        "evidence_coverage": coverage,
        "answer": "",
        "answer_generation_error": "",
        "answer_grade": {
            "verdict": "review",
            "reason": "评测链路执行异常，需人工复核。",
            "grader_error": "",
            "judge_request_type": "independent_grade_model",
        },
        "evaluation_error": f"{type(exc).__name__}: {exc}",
        "null_refusal_correct": False,
        "rag_trace": trace,
        "candidate_audits": current_state.get("candidate_audits") or [],
        "expected_fact_audit": [{
            "fact": fact, "first_seen_stage": None, "supporting_chunk_ids": [],
            "used_in_answer": None, "root_cause": None,
            "reason": "pending_manual_review", "confidence": None,
        } for fact in (case.get("answer_facts") or [])],
        "baseline_comparison": case.get("_baseline_comparison"),
        "route": current_state.get("route"),
        "retrieval_status": current_state.get("retrieval_status"),
        "rag_seconds": 0.0,
        "generation_seconds": 0.0,
        "judge_seconds": 0.0,
        "end_to_end_seconds": elapsed_seconds,
    }


def _evaluate_multihop_case(
    case: dict[str, Any],
    config: dict[str, Any],
    runtime: RetrievalRuntime,
    judge_config: JudgeConfig | None,
    judge_client: requests.Session | None,
) -> dict[str, Any]:
    """执行单题完整 RAG 流程；普通异常直接写为 review 结果。"""
    from backend.chat.request_context import ChatRequestContext
    from backend.rag.pipeline import run_rag_graph

    started = time.perf_counter()
    state: dict[str, Any] = {}
    try:
        rag_started = time.perf_counter()
        ctx = ChatRequestContext.for_sync(
            user_id="rag-evaluation",
            session_id=f"rag-eval-{config['run_id']}-{case['id']}",
        )
        state = run_rag_graph(case["question"], ctx, retrieval_runtime=runtime)
        rag_seconds = time.perf_counter() - rag_started
        trace = state.get("rag_trace") or {}
        retrieval_error = str(trace.get("retrieval_error") or "").strip()
        if retrieval_error:
            # A failed vector-store call is infrastructure failure, not a valid
            # "no knowledge" answer. Preserve the trace and mark the record so
            # retry and manual review keep it out of model-quality metrics.
            record = _multihop_error_record(
                case,
                RuntimeError(retrieval_error),
                elapsed_seconds=time.perf_counter() - started,
                state=state,
                dataset_label=config.get("dataset", "multihoprag"),
            )
            record["rag_seconds"] = rag_seconds
            return record
        generation_started = time.perf_counter()
        answer, generation_error = _answer_from_evidence(case["question"], state)
        generation_seconds = time.perf_counter() - generation_started
        judge_started = time.perf_counter()
        answer_grade = judge_multihop_answer(case, answer, client=judge_client, config=judge_config)
        judge_seconds = time.perf_counter() - judge_started
        docs = state.get("docs") or []
        expected = case["evidence_filenames"]
        evidence_hits = _ranks(expected, docs)
        coverage = _evidence_coverage(case, docs)
        return {
            "dataset": config.get("dataset", "multihoprag"),
            "case_id": case["id"],
            "question": case["question"],
            "question_type": case["question_type"],
            "reference_answer": case["reference_answer"],
            "expected_evidence_filenames": expected,
            "retrieved_filenames": _retrieved_filenames(docs),
            "evidence_ranks": evidence_hits,
            "evidence_coverage": coverage,
            "answer": answer,
            "answer_generation_error": generation_error,
            "answer_grade": answer_grade,
            "null_refusal_correct": case["question_type"] in {"null", "null_query", "info_not_found"}
            and answer_grade["verdict"] == "pass",
            "rag_trace": trace,
            "candidate_audits": state.get("candidate_audits") or [],
            "expected_fact_audit": [{
                "fact": fact, "first_seen_stage": None, "supporting_chunk_ids": [],
                "used_in_answer": None, "root_cause": None,
                "reason": "pending_manual_review", "confidence": None,
            } for fact in (case.get("answer_facts") or [])],
            "baseline_comparison": case.get("_baseline_comparison"),
            "route": state.get("route"),
            "retrieval_status": state.get("retrieval_status"),
            "rag_seconds": rag_seconds,
            "generation_seconds": generation_seconds,
            "judge_seconds": judge_seconds,
            "end_to_end_seconds": time.perf_counter() - started,
        }
    except Exception as exc:
        return _multihop_error_record(
            case,
            exc,
            elapsed_seconds=time.perf_counter() - started,
            state=state,
            dataset_label=config.get("dataset", "multihoprag"),
        )


def _evaluation_parent_store_for_runtime(config: dict[str, Any]) -> ParentChunkStore | EvaluationParentChunkStore:
    """Choose the parent store recorded by the immutable corpus configuration."""
    strategy = str(config.get("document_chunking_strategy") or DEFAULT_CHUNKING_STRATEGY)
    if strategy != STRUCTURED_MARKDOWN_CHUNKING_STRATEGY:
        return ParentChunkStore()
    corpus_run_id = str(config.get("corpus_run_id") or config.get("run_id") or "")
    store = EvaluationParentChunkStore(
        corpus_run_id,
        config=EvaluationStorageConfig.from_env(),
    )
    store.check_connection()
    return store


def _build_multihop_runtime(config: dict[str, Any]) -> tuple[RetrievalRuntime, JudgeConfig | None, requests.Session | None]:
    """在评测工作进程中创建只访问独立 Milvus 集合的运行时依赖。"""
    store = _evaluation_store(config["collection_name"])
    runtime = RetrievalRuntime(
        milvus_store=store,
        embedding_service=embedding_service,
        parent_chunk_store=_evaluation_parent_store_for_runtime(config),
        retrieval_mode="hybrid",
        enable_rewrite_candidate_fusion=bool(config.get("rewrite_candidate_fusion_enabled")),
        enable_adjacent_l3_expansion=bool(config.get("adjacent_l3_expansion_enabled")),
        capture_candidate_trace=bool(config.get("candidate_trace_capture_enabled")),
    )
    judge_config = _judge_config()
    judge_client = requests.Session() if judge_config else None
    if judge_client:
        judge_client.headers.update({"Authorization": f"Bearer {os.getenv('ARK_API_KEY', '')}"})
    return runtime, judge_config, judge_client


def _multihop_worker_main(request_queue: Any, result_queue: Any, config: dict[str, Any]) -> None:
    """顺序执行评测题目，主进程保留终止工作进程的控制权。"""
    runtime, judge_config, judge_client = _build_multihop_runtime(config)
    while True:
        case = request_queue.get()
        if case is None:
            return
        try:
            record = _evaluate_multihop_case(case, config, runtime, judge_config, judge_client)
            result_queue.put({"case_id": case["id"], "record": record})
        except BaseException as exc:
            result_queue.put({
                "case_id": case["id"],
                "worker_error": f"{type(exc).__name__}: {exc}",
            })


def _start_multihop_worker(config: dict[str, Any]) -> _MultiHopWorker:
    """启动一个常驻工作进程，正常题可复用已加载模型。"""
    context = multiprocessing.get_context("spawn")
    request_queue = context.Queue()
    result_queue = context.Queue()
    process = context.Process(
        target=_multihop_worker_main,
        args=(request_queue, result_queue, config),
        daemon=True,
    )
    process.start()
    return _MultiHopWorker(process=process, request_queue=request_queue, result_queue=result_queue)


def _stop_multihop_worker(worker: _MultiHopWorker | None) -> None:
    """关闭工作进程；超过题级时限时强制回收，避免残留阻塞调用。"""
    if not worker:
        return
    try:
        if worker.process.is_alive():
            worker.request_queue.put(None)
            worker.process.join(timeout=5)
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(timeout=5)
    finally:
        # Windows 的 spawn 队列会各自保留一个 feeder 线程和句柄。题级超时后必须
        # 等待它们退出，否则同一评测进程反复重启 worker 会累积失效资源。
        for worker_queue in (worker.request_queue, worker.result_queue):
            worker_queue.close()
            worker_queue.join_thread()
        if not worker.process.is_alive():
            worker.process.close()


def _run_case_in_worker(
    worker: _MultiHopWorker,
    case: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """等待一题结果，超时则由调用方回收整个工作进程。"""
    deadline = time.perf_counter() + timeout_seconds
    worker.request_queue.put(case)
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(f"评测单题超过 {timeout_seconds:.0f} 秒总时限")
        try:
            payload = worker.result_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            if not worker.process.is_alive():
                exitcode = worker.process.exitcode
                detail = f"（exitcode={exitcode}）" if exitcode is not None else ""
                raise RuntimeError(f"评测工作进程在返回结果前退出{detail}")
            continue
        if payload.get("case_id") != case["id"]:
            raise RuntimeError("评测工作进程返回了不匹配的题目结果")
        if "worker_error" in payload:
            raise RuntimeError(payload["worker_error"])
        return payload["record"]


def _evaluation_worker_count(config: dict[str, Any]) -> int:
    """Read and validate the execution-only worker count from a config snapshot."""
    raw_value = config.get("evaluation_worker_count", 1)
    if isinstance(raw_value, bool):
        raise ValueError("evaluation_worker_count 必须是 1 到 32 的整数")
    try:
        worker_count = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation_worker_count 必须是 1 到 32 的整数") from exc
    if not 1 <= worker_count <= MAX_EVALUATION_WORKERS:
        raise ValueError(
            f"evaluation_worker_count 必须在 1 到 {MAX_EVALUATION_WORKERS} 之间"
        )
    return worker_count


def _finalize_multihop_evaluation(
    output_dir: Path,
    config: dict[str, Any],
    cases: list[dict[str, Any]],
    records: list[dict[str, Any]],
    completed_case_ids: set[str],
    interruption_error: str,
) -> dict[str, Any]:
    """Write the shared terminal progress and metrics for serial or pooled execution."""
    expected_case_ids = {str(case["id"]) for case in cases}
    missing_case_count = len(expected_case_ids - completed_case_ids)
    evaluation_error_count = sum(_has_evaluation_error(record) for record in records)
    terminal_error = interruption_error
    if not terminal_error and missing_case_count:
        terminal_error = f"仍有 {missing_case_count} 道题没有成功记录"
    if not terminal_error and evaluation_error_count:
        terminal_error = f"有 {evaluation_error_count} 道题包含 evaluation_error"
    summary = {
        "dataset": config.get("dataset", "multihoprag"),
        "run_id": config["run_id"],
        "judge_request_type": "independent_grade_model",
        "evaluation_worker_count": _evaluation_worker_count(config),
        "evaluation_error_count": evaluation_error_count,
        "unresolved_case_count": missing_case_count,
        **multihop_metrics(records),
    }
    summary["by_question_type"] = _enterprise_question_type_summary(
        records,
        evaluation_mode="rag",
    )
    _write_evaluation_progress(
        output_dir,
        total_cases=len(cases),
        completed_case_ids=completed_case_ids,
        status="interrupted" if terminal_error else "completed",
        error=terminal_error,
    )
    summary["evaluation_status"] = "interrupted" if terminal_error else "completed"
    if terminal_error:
        summary["interruption_error"] = terminal_error
    return summary


def _evaluate_multihop_concurrently(
    output_dir: Path,
    config: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    checkpoint_filename: str,
    worker_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run independent cases in separate workers while checkpointing in the parent.

    A worker owns at most one in-flight case.  This keeps the existing per-case
    timeout and worker-recovery guarantees intact while allowing independent
    evaluation cases to overlap.
    """
    checkpoint_path = output_dir / checkpoint_filename
    records = _read_multihop_checkpoints(checkpoint_path)
    cases_by_id = {str(case["id"]): case for case in cases}
    records = [
        _normalize_checkpoint_record(record, cases_by_id[str(record["case_id"])])
        if str(record.get("case_id")) in cases_by_id else record
        for record in records
    ]
    completed_case_ids = {str(record["case_id"]) for record in records}
    pending_cases = [case for case in cases if str(case["id"]) not in completed_case_ids]
    _write_evaluation_progress(
        output_dir,
        total_cases=len(cases),
        completed_case_ids=completed_case_ids,
        status="running",
    )

    case_timeout = evaluation_case_timeout_seconds()
    active_workers: list[_MultiHopWorker] = []
    inflight: dict[Any, tuple[_MultiHopWorker, dict[str, Any], float]] = {}
    interruption_error = ""
    next_case_index = 0

    def start_worker() -> _MultiHopWorker:
        worker = _start_multihop_worker(config)
        active_workers.append(worker)
        return worker

    def retire_worker(worker: _MultiHopWorker) -> None:
        _stop_multihop_worker(worker)
        active_workers[:] = [item for item in active_workers if item is not worker]

    executor = ThreadPoolExecutor(
        max_workers=min(worker_count, len(pending_cases) or 1),
        thread_name_prefix="rag-evaluation-case",
    )
    try:
        for _ in range(min(worker_count, len(pending_cases))):
            worker = start_worker()
            case = pending_cases[next_case_index]
            next_case_index += 1
            future = executor.submit(_run_case_in_worker, worker, case, case_timeout)
            inflight[future] = (worker, case, time.perf_counter())

        while inflight:
            completed_futures, _ = wait(inflight, return_when=FIRST_COMPLETED)
            reusable_workers: list[_MultiHopWorker | None] = []
            for future in completed_futures:
                worker, case, started = inflight.pop(future)
                try:
                    record = future.result()
                except Exception as exc:
                    record = _multihop_error_record(
                        case,
                        exc,
                        elapsed_seconds=time.perf_counter() - started,
                        dataset_label=config.get("dataset", "multihoprag"),
                    )
                    retire_worker(worker)
                    worker = None
                else:
                    if record.get("evaluation_error"):
                        # An API/model failure can leave this worker's clients in
                        # an unhealthy state, so replace only this worker slot.
                        retire_worker(worker)
                        worker = None

                _append_result_checkpoint(checkpoint_path, record)
                records.append(record)
                completed_case_ids.add(str(case["id"]))
                _write_evaluation_progress(
                    output_dir,
                    total_cases=len(cases),
                    completed_case_ids=completed_case_ids,
                    status="running",
                )
                if _is_provider_quota_error(record):
                    interruption_error = str(record.get("evaluation_error") or "provider quota error")
                reusable_workers.append(worker)

            if interruption_error:
                # Stop concurrent work immediately.  Its uncheckpointed cases
                # remain missing and are the only cases selected by a later retry.
                break

            for worker in reusable_workers:
                if next_case_index >= len(pending_cases):
                    continue
                if worker is None:
                    worker = start_worker()
                case = pending_cases[next_case_index]
                next_case_index += 1
                future = executor.submit(_run_case_in_worker, worker, case, case_timeout)
                inflight[future] = (worker, case, time.perf_counter())
    finally:
        for worker in list(active_workers):
            _stop_multihop_worker(worker)
        # Stopping the process workers above wakes any waiting coordinator
        # threads.  Their aborted cases deliberately stay absent from JSONL.
        executor.shutdown(wait=True, cancel_futures=True)

    records.sort(key=lambda record: next(
        (index for index, case in enumerate(cases) if str(case["id"]) == str(record.get("case_id"))),
        len(cases),
    ))
    return records, _finalize_multihop_evaluation(
        output_dir,
        config,
        cases,
        records,
        completed_case_ids,
        interruption_error,
    )


def evaluate_multihop(
    output_dir: Path,
    config: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    case_runner: Callable[[dict[str, Any], dict[str, Any], RetrievalRuntime, JudgeConfig | None, requests.Session | None], dict[str, Any]] | None = None,
    checkpoint_filename: str = "results.jsonl",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """运行完整图评测，并以进程级单题总时限保证整轮可恢复。"""
    worker_count = _evaluation_worker_count(config)
    if worker_count > 1:
        if case_runner:
            raise ValueError("并发评测不支持注入 case_runner；请使用真实隔离 worker")
        return _evaluate_multihop_concurrently(
            output_dir,
            config,
            cases,
            checkpoint_filename=checkpoint_filename,
            worker_count=worker_count,
        )

    runtime: RetrievalRuntime | None = None
    judge_config: JudgeConfig | None = None
    judge_client: requests.Session | None = None
    worker: _MultiHopWorker | None = None
    case_timeout = evaluation_case_timeout_seconds()
    if case_runner:
        runtime, judge_config, judge_client = _build_multihop_runtime(config)

    checkpoint_path = output_dir / checkpoint_filename
    records = _read_multihop_checkpoints(checkpoint_path)
    cases_by_id = {str(case["id"]): case for case in cases}
    records = [
        _normalize_checkpoint_record(record, cases_by_id[str(record["case_id"])])
        if str(record.get("case_id")) in cases_by_id else record
        for record in records
    ]
    completed_case_ids = {str(record["case_id"]) for record in records}
    _write_evaluation_progress(
        output_dir,
        total_cases=len(cases),
        completed_case_ids=completed_case_ids,
        status="running",
    )
    interruption_error = ""
    try:
        for case in cases:
            if case["id"] in completed_case_ids:
                continue
            started = time.perf_counter()
            try:
                if case_runner:
                    assert runtime is not None
                    record = case_runner(case, config, runtime, judge_config, judge_client)
                else:
                    if not worker or not worker.process.is_alive():
                        _stop_multihop_worker(worker)
                        worker = _start_multihop_worker(config)
                    record = _run_case_in_worker(worker, case, case_timeout)
                    if record.get("evaluation_error"):
                        # _evaluate_multihop_case 捕获 API/model 异常后会返回 review
                        # 记录；不能让同一个可能已损坏的客户端继续服务后续题目。
                        _stop_multihop_worker(worker)
                        worker = None
            except Exception as exc:
                record = _multihop_error_record(
                    case,
                    exc,
                    elapsed_seconds=time.perf_counter() - started,
                    dataset_label=config.get("dataset", "multihoprag"),
                )
                if not case_runner:
                    _stop_multihop_worker(worker)
                    worker = None
            _append_result_checkpoint(checkpoint_path, record)
            records.append(record)
            completed_case_ids.add(case["id"])
            _write_evaluation_progress(
                output_dir,
                total_cases=len(cases),
                completed_case_ids=completed_case_ids,
                status="running",
            )
            if _is_provider_quota_error(record):
                interruption_error = str(record.get("evaluation_error") or "provider quota error")
                break
    finally:
        _stop_multihop_worker(worker)
    return records, _finalize_multihop_evaluation(
        output_dir,
        config,
        cases,
        records,
        completed_case_ids,
        interruption_error,
    )


def _multihop_report(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    suppress_validation_details: bool = False,
) -> str:
    title = "EnterpriseRAG" if summary.get("dataset") == "enterpriserag" else "MultiHopRAG"
    lines = [f"# {title} 完整链路评测报告", "", "回答正确性来自独立 GRADE_MODEL 判卷；所有 `review` 样本必须人工复核。", ""]
    lines.append("| 指标 | 结果 |")
    lines.append("| --- | --- |")
    for key, value in summary.items():
        if key not in {"dataset", "run_id", "judge_request_type", "by_question_type"}:
            lines.append(f"| {key} | {_format_metric(value)} |")
    if summary.get("by_question_type"):
        lines.extend([
            "",
            "## 按题型",
            "",
            "| 题型 | 题数 | 证据全覆盖率 | 回答通过率 | 人工复核率 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for question_type, metrics in summary["by_question_type"].items():
            lines.append(
                "| " + " | ".join([
                    question_type,
                    str(metrics["case_count"]),
                    _format_metric(metrics["evidence_full_coverage_rate"]),
                    _format_metric(metrics["answer_pass_rate"]),
                    _format_metric(metrics["manual_review_rate"]),
                ]) + " |"
            )
    review_ids = [
        str(item["case_id"])
        for item in results
        if item["answer_grade"]["verdict"] == "review"
        and not (suppress_validation_details and item.get("case_set") == "validation")
    ]
    lines.extend(["", f"人工复核样本数：{len(review_ids)}", f"人工复核样本 ID：{', '.join(review_ids) or '无'}", ""])
    if suppress_validation_details:
        lines.append("验证集题号和失败详情已从自动报告及人工复核文件抑制；完整记录仅保存在受控逐题 JSONL 中。")
        lines.append("")
    return "\n".join(lines)


def _candidate_audit_report(results: list[dict[str, Any]]) -> str:
    """Human-readable index for candidate snapshots; full text remains in JSONL."""
    lines = [
        "# Candidate Audit",
        "",
        "本报告只展示查询、阶段数量、候选标识和事实点索引；候选全文保存在同目录 `results.jsonl`。",
        "",
        "| Case | Trigger | 原始 L3 | 扩展后候选 | 合并后 | 最终 | Verdict |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]

    def identifiers(items: list[dict[str, Any]], limit: int = 8) -> str:
        values: list[str] = []
        for item in items[:limit]:
            value = item.get("chunk_id") or item.get("id") or item.get("filename") or "?"
            values.append(f"`{value}`")
        if len(items) > limit:
            values.append(f"…（另 {len(items) - limit} 个，见 JSONL）")
        return ", ".join(values) or "（无）"

    def inline(value: Any, empty: str = "（无）") -> str:
        text = str(value or empty).replace("`", "'").replace("\n", " ")
        return f"`{text}`"

    for record in results:
        audits = record.get("candidate_audits") or []
        fused = next((item for item in audits if item.get("stage") == "fused"), None)
        primary = fused or (audits[0] if audits else {})
        raw_count = len((primary or {}).get("original_raw_leaf_candidates") or [])
        if not raw_count:
            raw_count = len((primary or {}).get("raw_leaf_candidates") or [])
        if not raw_count:
            raw_count = sum(len(item.get("raw_leaf_candidates") or []) for item in audits)
        expanded_count = len((primary or {}).get("post_adjacent_expansion_candidates") or [])
        if not expanded_count:
            expanded_count = len((primary or {}).get("raw_leaf_candidates") or [])
        merged_count = len((primary or {}).get("post_merge_candidates") or [])
        final_count = len((primary or {}).get("final_context_candidates") or [])
        trace = record.get("rag_trace") or {}
        trigger = trace.get("rewrite_method") or "未触发"
        verdict = (record.get("answer_grade") or {}).get("verdict", "review")
        lines.append(
            f"| `{record.get('case_id')}` | `{trigger}` | {raw_count} | {expanded_count} | "
            f"{merged_count} | {final_count} | `{verdict}` |"
        )
        baseline = record.get("baseline_comparison") or {}
        primary_meta = (primary or {}).get("meta") or {}
        lines.extend([
            "",
            f"### `{record.get('case_id')}`",
            "",
            f"- 问题：{str(record.get('question') or '（无）').replace(chr(10), ' ')}",
            f"- 改写查询：{inline(trace.get('rewritten_query'), '未触发')}",
            f"- 基线对照：verdict={inline(baseline.get('verdict'))}，证据覆盖率={inline(baseline.get('evidence_coverage'))}",
            f"- 标准事实点：{len(record.get('expected_fact_audit') or [])} 个；人工审计：`待复核`",
        ])
        if trace.get("adjacent_l3_expansion_enabled"):
            lines.extend([
                f"- 相邻 L3 扩展是否执行：{inline(primary_meta.get('adjacent_l3_expansion_applied', False))}",
                f"- 相邻 L3 新增候选：{inline(primary_meta.get('adjacent_l3_expansion_added_candidate_count', 0))}",
                f"- 相邻 L3 回退原因：{inline(primary_meta.get('adjacent_l3_expansion_fallback_reason'))}",
            ])
        else:
            lines.append(
                f"- 融合是否实际执行：{inline(primary_meta.get('rewrite_candidate_fusion_applied', False))}"
            )
        for audit in audits:
            lines.extend([
                "",
                f"#### 阶段：`{audit.get('stage') or 'unknown'}`",
                f"- 查询来源：{inline(audit.get('query_origin'), 'unknown')}；查询：{inline(audit.get('query'))}",
                f"- 原始 L3（{len(audit.get('original_raw_leaf_candidates') or audit.get('raw_leaf_candidates') or [])}）：{identifiers(audit.get('original_raw_leaf_candidates') or audit.get('raw_leaf_candidates') or [])}",
                f"- 相邻扩展块（{len(audit.get('adjacent_l3_candidates') or [])}）：{identifiers(audit.get('adjacent_l3_candidates') or [])}",
                f"- 原始候选（{len(audit.get('raw_leaf_candidates') or [])}）：{identifiers(audit.get('raw_leaf_candidates') or [])}",
                f"- Auto-merging 后（{len(audit.get('post_merge_candidates') or [])}）：{identifiers(audit.get('post_merge_candidates') or [])}",
                f"- Rerank 返回（{len(audit.get('rerank_returned_candidates') or [])}）：{identifiers(audit.get('rerank_returned_candidates') or [])}",
                f"- 阈值拒绝（{len(audit.get('threshold_rejected_candidates') or [])}）：{identifiers(audit.get('threshold_rejected_candidates') or [])}",
                f"- 最终上下文（{len(audit.get('final_context_candidates') or [])}）：{identifiers(audit.get('final_context_candidates') or [])}",
            ])
        lines.extend(["", "---"])
    return "\n".join(lines) + "\n"


def _new_experiment_config(
    *,
    dataset: str,
    corpus_run_id: str,
    evaluation_id: str,
    corpus_config: dict[str, Any],
    corpus_manifest_path: Path,
    case_set: Literal["all", "analysis", "validation"],
    case_split_sha256: str | None,
    evaluation_mode: Literal["retrieval", "rag"],
    changed_variable: str | None,
    case_count: int,
    evaluation_worker_count: int,
) -> dict[str, Any]:
    """Capture the complete, non-secret input snapshot for one experiment."""
    corpus_manifest = _read_json(corpus_manifest_path)
    config = _public_config(
        dataset,
        evaluation_id,
        str(corpus_config.get("profile") or "full"),
        str(corpus_manifest["collection_name"]),
        language=str(corpus_config.get("language") or "en"),
        corpus=str(corpus_config.get("corpus") or "representative"),
        evaluation_mode=evaluation_mode,
        chunking_strategy=str(
            corpus_config.get("document_chunking_strategy") or DEFAULT_CHUNKING_STRATEGY
        ),
        rechunk_scope=str(corpus_config.get("rechunk_scope") or "full"),
        document_parse_workers=corpus_config.get("document_parse_workers"),
    )
    config.update({
        "evaluation_id": evaluation_id,
        "corpus_run_id": corpus_run_id,
        "source_corpus_manifest": str(corpus_manifest_path),
        "source_corpus_manifest_sha256": _sha256_file(corpus_manifest_path),
        "source_case_split_sha256": case_split_sha256,
        "case_set": case_set,
        "case_count": case_count,
        "changed_variable": changed_variable,
        # Execution-only setting: it changes throughput, not the frozen RAG
        # retrieval/generation inputs.  Keep it in every experiment snapshot.
        "evaluation_worker_count": evaluation_worker_count,
        "rewrite_candidate_fusion_enabled": changed_variable == "rewrite_candidate_fusion",
        "adjacent_l3_expansion_enabled": changed_variable == "adjacent_l3_expansion",
        "candidate_trace_capture_enabled": False,
        "code_version": _code_version(),
        "created_at": datetime.now(UTC).isoformat(),
    })
    return config


def evaluate_run(
    *,
    dataset: Literal["ecomretrieval", "multihoprag", "enterpriserag"],
    run_id: str,
    evaluation_id: str | None = None,
    case_set: Literal["all", "analysis", "validation"] = "all",
    evaluation_mode: Literal["retrieval", "rag"] | None = None,
    changed_variable: str | None = None,
    retry_from_evaluation_id: str | None = None,
    target_manifest_path: Path | None = None,
    capture_candidate_trace: bool = False,
    evaluation_worker_count: int = 1,
) -> Path:
    """Evaluate a prepared corpus, optionally in a non-overlapping experiment run.

    ``evaluation_id`` is the formal path: its artifacts never overwrite corpus
    configuration or another experiment. The no-ID branch preserves old runs.
    """
    if isinstance(evaluation_worker_count, bool) or not 1 <= evaluation_worker_count <= MAX_EVALUATION_WORKERS:
        raise ValueError(
            f"evaluation_worker_count 必须在 1 到 {MAX_EVALUATION_WORKERS} 之间"
        )

    corpus_dir = run_directory(dataset, run_id)
    corpus_config = _read_json(corpus_dir / "config.json")
    manifest_path = corpus_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("cleanup_completed"):
        raise RuntimeError("该运行已清理，不能继续评测；请重新 prepare。")
    if not manifest.get("prepare_completed"):
        raise RuntimeError("该运行尚未完成入库，不能评测；请执行 cleanup 后重新 prepare。")
    if dataset != "enterpriserag" and case_set != "all":
        raise ValueError("只有 EnterpriseRAG 正式语料支持 analysis/validation case set")

    all_cases = read_cases(corpus_dir / "cases.jsonl")
    target_manifest_type: str | None = None
    if changed_variable == "adjacent_l3_expansion" and target_manifest_path is None:
        raise ValueError(
            "adjacent_l3_expansion 只能通过固定的 11 题 target manifest 评测"
        )
    if changed_variable == "document_chunking_strategy" and target_manifest_path is None:
        raise ValueError(
            "document_chunking_strategy 只能通过冻结的 30 题 structured target manifest 评测"
        )
    if target_manifest_path is not None:
        if evaluation_id is None:
            raise ValueError("target manifest 评测必须提供独立 evaluation_id，不能写入 corpus 根目录")
        if dataset != "enterpriserag":
            raise ValueError("target manifest 评测只支持 EnterpriseRAG 正式语料")
        cases, selected_split_hash = _select_target_manifest_cases(
            corpus_dir=corpus_dir,
            all_cases=all_cases,
            case_set=case_set,
            target_manifest_path=target_manifest_path,
        )
        target_manifest_type = str(_read_json(target_manifest_path).get("manifest_type") or "rewrite_candidate_fusion")
    else:
        cases, selected_split_hash = _select_experiment_cases(corpus_dir, all_cases, case_set)
    if not cases:
        raise RuntimeError("所选 case set 为空，拒绝生成没有题目的实验结果")
    mode = evaluation_mode or str(corpus_config.get("evaluation_mode") or "rag")
    if mode not in {"retrieval", "rag"}:
        raise ValueError(f"不支持的评测模式：{mode}")
    if target_manifest_path is not None:
        if mode != "rag":
            raise ValueError("target manifest 评测必须使用完整 RAG 模式")
        expected_variable = _TARGET_MANIFEST_CONTRACTS[target_manifest_type or "rewrite_candidate_fusion"][1]
        if changed_variable != expected_variable:
            raise ValueError(
                f"{target_manifest_type} target manifest 必须明确声明 changed_variable={expected_variable}"
            )
    if target_manifest_type == "structured_chunking_offline_audit" and (
        corpus_config.get("document_chunking_strategy")
        != STRUCTURED_MARKDOWN_CHUNKING_STRATEGY
    ):
        raise ValueError(
            "结构化分块 target 只能使用 document_chunking_strategy=markdown_header_recursive_v1 的新 corpus run"
        )
    if target_manifest_type in {
        "evidence_candidate_audit",
        "adjacent_l3_expansion",
        "structured_chunking_offline_audit",
    } and not capture_candidate_trace:
        raise ValueError(f"{target_manifest_type} target manifest 必须显式开启 candidate trace")
    if capture_candidate_trace and (
        target_manifest_path is None
        or changed_variable not in {
            "rewrite_candidate_fusion",
            "adjacent_l3_expansion",
            "document_chunking_strategy",
        }
    ):
        raise ValueError("candidate trace 只能在受控的单变量 target manifest 评测中开启")
    if (
        target_manifest_type == "structured_chunking_offline_audit"
        and evaluation_worker_count != 10
    ):
        raise ValueError("结构化分块 30 题真实评测必须使用 evaluation_worker_count=10")
    if retry_from_evaluation_id and (evaluation_id is None or mode != "rag" or dataset != "enterpriserag"):
        raise ValueError("retry 只支持 EnterpriseRAG 完整 RAG，且必须提供新的 evaluation_id")

    retry_source_dir: Path | None = None
    retry_source_records: list[dict[str, Any]] = []
    retry_cases = cases
    preserved_case_ids: set[str] = set()
    retry_case_ids: set[str] = set()
    if retry_from_evaluation_id:
        retry_source_id = _safe_run_id(retry_from_evaluation_id)
        if retry_source_id == _safe_run_id(evaluation_id or ""):
            raise ValueError("retry source evaluation_id 必须与新的 evaluation_id 不同")
        retry_source_dir = evaluation_directory(dataset, run_id, retry_source_id)
        source_config_path = retry_source_dir / "evaluation-config.json"
        if not source_config_path.is_file():
            raise FileNotFoundError(f"retry source 缺少 evaluation-config.json：{retry_source_dir}")
        source_config = _read_json(source_config_path)
        retry_source_records, source_results_path = _read_retry_source_records(
            dataset, run_id, retry_source_id
        )
        retry_cases, preserved_case_ids, retry_case_ids = _retry_case_selection(cases, retry_source_records)
        if not retry_cases:
            raise RuntimeError("retry source 没有缺失或运行失败题目，无需补跑")

    if evaluation_id is None:
        output_dir = corpus_dir
        config = dict(corpus_config)
        config["evaluation_mode"] = mode
    else:
        evaluation_id = _safe_run_id(evaluation_id)
        output_dir = evaluation_directory(dataset, run_id, evaluation_id)
        requested_config = _new_experiment_config(
            dataset=dataset,
            corpus_run_id=_safe_run_id(run_id),
            evaluation_id=evaluation_id,
            corpus_config=corpus_config,
            corpus_manifest_path=manifest_path,
            case_set=case_set,
            case_split_sha256=selected_split_hash or manifest.get("case_split_sha256"),
            evaluation_mode=mode,  # type: ignore[arg-type]
            changed_variable=changed_variable,
            case_count=len(cases),
            evaluation_worker_count=evaluation_worker_count,
        )
        if target_manifest_path is not None:
            requested_config.update({
                "target_manifest_path": str(target_manifest_path),
                "target_manifest_sha256": _sha256_file(target_manifest_path),
            })
        if capture_candidate_trace:
            requested_config["candidate_trace_capture_enabled"] = True
        if retry_from_evaluation_id:
            retry_compatibility_ignored = {
                "run_id", "evaluation_id", "prepared_at", "created_at", "code_version",
                # Retry metadata describes the current repair hop.  It may
                # change when a later retry continues from an interrupted
                # retry, while the actual corpus and RAG inputs remain fixed.
                "retry_source_evaluation_id", "retry_source_results_sha256",
                "retry_case_count", "preserved_case_count",
                # Worker count is an execution budget, not a RAG input.  A
                # retry may safely use more workers while preserving all model,
                # retrieval, corpus, and target-manifest inputs.
                "evaluation_worker_count",
            }
            # A retry may intentionally change only the outer evaluation budget;
            # all model, retrieval, corpus, and case-set inputs must still match.
            if changed_variable == "evaluation_case_timeout_seconds":
                retry_compatibility_ignored.update({
                    "changed_variable", "evaluation_case_timeout_seconds",
                })
            changed = [
                key for key in requested_config
                if key in source_config
                and key not in retry_compatibility_ignored
                and requested_config[key] != source_config[key]
            ]
            if changed:
                raise FileExistsError(
                    f"retry source 与当前配置不兼容，拒绝混合结果：{changed}"
                )
            if (
                changed_variable == "evaluation_case_timeout_seconds"
                and source_config.get("changed_variable") not in {None, "evaluation_case_timeout_seconds"}
            ):
                raise FileExistsError(
                    "retry source 已包含其他 changed_variable，不能再混合评测运行预算"
                )
            requested_config.update({
                "retry_source_evaluation_id": retry_source_id,
                "retry_source_results_sha256": _sha256_file(source_results_path),
                "retry_case_count": len(retry_cases),
                "preserved_case_count": len(preserved_case_ids),
            })
        config_path = output_dir / "evaluation-config.json"
        if output_dir.exists():
            if not config_path.is_file():
                raise FileExistsError(f"实验目录已存在但没有配置快照：{output_dir}")
            config = _read_json(config_path)
            immutable_keys = (
                "corpus_run_id", "source_corpus_manifest_sha256", "source_case_split_sha256",
                "case_set", "evaluation_mode", "changed_variable", "case_count",
                "document_chunking_strategy", "rechunk_scope", "document_parse_workers",
                "model_timeout_seconds", "evaluation_case_timeout_seconds",
                "retry_source_evaluation_id", "retry_source_results_sha256", "retry_case_count",
                "preserved_case_count",
                "evaluation_worker_count",
                "rewrite_candidate_fusion_enabled", "adjacent_l3_expansion_enabled",
                "target_manifest_path", "target_manifest_sha256",
                "candidate_trace_capture_enabled",
            )
            changed = [
                key for key in immutable_keys
                if config.get(key, False if key == "candidate_trace_capture_enabled" else None)
                != requested_config.get(key)
            ]
            if changed:
                raise FileExistsError(
                    f"evaluation_id 已被不同配置使用，拒绝覆盖：{evaluation_id}（差异字段：{changed}）"
                )
        else:
            output_dir.mkdir(parents=True)
            config = requested_config
            _write_json(config_path, config)

    suppress_validation_details = (
        dataset == "enterpriserag"
        and bool(manifest.get("case_split_sha256"))
        and case_set in {"all", "validation"}
    )

    if retry_from_evaluation_id:
        source_by_id = {
            str(record.get("case_id")): record
            for record in retry_source_records
            if record.get("case_id")
        }
        retry_reasons = {
            str(case["id"]): (
                "missing_source_record"
                if str(case["id"]) not in source_by_id
                else "source_evaluation_error"
            )
            for case in retry_cases
        }
        _write_json(output_dir / "retry-manifest.json", {
            "source_evaluation_id": retry_from_evaluation_id,
            "source_results_sha256": _sha256_file(source_results_path),
            "case_set": case_set,
            "total_case_count": len(cases),
            "preserved_case_ids": sorted(preserved_case_ids),
            "retry_case_ids": sorted(retry_case_ids),
            "retry_reasons": retry_reasons,
            "created_at": datetime.now(UTC).isoformat(),
        })

    if dataset == "ecomretrieval":
        results, summary = evaluate_ecom(output_dir, config, cases)
        report = _ecom_report(summary)
    elif dataset == "enterpriserag" and mode == "retrieval":
        results, summary = evaluate_enterprise_retrieval(output_dir, config, cases)
        _add_case_set_to_records(results, cases)
        summary["by_question_type"] = _enterprise_question_type_summary(
            results,
            evaluation_mode="retrieval",
            included_strategies=list(summary["strategies"]),
        )
        report = _enterprise_retrieval_report(summary)
    else:
        if retry_from_evaluation_id:
            attempt_results, attempt_summary = evaluate_multihop(
                output_dir,
                config,
                retry_cases,
                checkpoint_filename="attempt-results.jsonl",
            )
            _write_results(output_dir / "attempt-results.jsonl", attempt_results)
            attempt_by_id = {
                str(record.get("case_id")): record
                for record in attempt_results
                if record.get("case_id")
            }
            merged_by_id: dict[str, dict[str, Any]] = {}
            unresolved_ids: list[str] = []
            for case in cases:
                case_id = str(case["id"])
                source_record = source_by_id.get(case_id)
                record = attempt_by_id.get(case_id) if case_id in retry_case_ids else source_record
                if record is None or _has_evaluation_error(record):
                    if record is not None:
                        merged_by_id[case_id] = record
                    unresolved_ids.append(case_id)
                    continue
                merged_by_id[case_id] = _normalize_checkpoint_record(record, case)
            if unresolved_ids or len(merged_by_id) != len(cases):
                _add_case_set_to_records(attempt_results, retry_cases)
                _write_manual_review_queue(
                    output_dir,
                    attempt_results,
                    suppress_validation_details=suppress_validation_details,
                )
                _write_case_review_report(
                    output_dir,
                    attempt_results,
                    dataset=dataset,
                    evaluation_mode="rag",
                    suppress_validation_details=suppress_validation_details,
                )
                summary = {
                    "dataset": dataset,
                    "run_id": config["run_id"],
                    "judge_request_type": "independent_grade_model",
                    # attempt_summary describes only this retry attempt.  Even when
                    # that attempt reaches its last selected case, the overall
                    # evaluation remains interrupted until every formal case has a
                    # successful record.
                    **attempt_summary,
                    "evaluation_status": "interrupted",
                    "source_evaluation_id": retry_from_evaluation_id,
                    "retry_case_count": len(retry_cases),
                    "retry_completed_case_count": len(attempt_results),
                    "unresolved_case_count": len(unresolved_ids),
                    "unresolved_runtime_error_count": sum(
                        _has_evaluation_error(record) for record in attempt_results
                    ),
                    "source_preserved_case_count": len(preserved_case_ids),
                }
                summary.update({
                    "corpus_run_id": _safe_run_id(run_id),
                    "evaluation_id": evaluation_id,
                    "case_set": case_set,
                    "source_corpus_manifest_sha256": _sha256_file(manifest_path),
                    "source_case_split_sha256": selected_split_hash or manifest.get("case_split_sha256"),
                })
                _write_json(output_dir / "summary.json", summary)
                (output_dir / "report.md").write_text(
                    "# EnterpriseRAG 补跑未完成\n\n"
                    "源运行和本次 attempt 均已保留；未形成 500 题正式质量基线。\n",
                    encoding="utf-8",
                    newline="\n",
                )
                _write_evaluation_progress(
                    output_dir,
                    total_cases=len(cases),
                    completed_case_ids=set(preserved_case_ids) | set(attempt_by_id),
                    status="interrupted",
                    error="retry cases remain missing or contain evaluation_error",
                )
                return output_dir
            results = list(merged_by_id.values())
            _add_case_set_to_records(results, cases)
            summary = {
                "dataset": dataset,
                "run_id": config["run_id"],
                "judge_request_type": "independent_grade_model",
                "evaluation_status": "completed",
                "source_evaluation_id": retry_from_evaluation_id,
                "retry_case_count": len(retry_cases),
                "preserved_case_count": len(preserved_case_ids),
                **multihop_metrics(results),
            }
            summary["by_question_type"] = _enterprise_question_type_summary(results, evaluation_mode="rag")
            _write_evaluation_progress(
                output_dir,
                total_cases=len(cases),
                completed_case_ids={str(case["id"]) for case in cases},
                status="completed",
            )
        else:
            results, summary = evaluate_multihop(output_dir, config, cases)
            _add_case_set_to_records(results, cases)
            summary["by_question_type"] = _enterprise_question_type_summary(results, evaluation_mode="rag")
        report = _multihop_report(
            summary,
            results,
            suppress_validation_details=suppress_validation_details,
        )
        _write_manual_review_queue(
            output_dir,
            results,
            suppress_validation_details=suppress_validation_details,
        )

    summary.update({
        "corpus_run_id": _safe_run_id(run_id),
        "evaluation_id": evaluation_id,
        "case_set": case_set,
        "source_corpus_manifest_sha256": _sha256_file(manifest_path),
        "source_case_split_sha256": selected_split_hash or manifest.get("case_split_sha256"),
    })
    _write_results(output_dir / "results.jsonl", results)
    if config.get("candidate_trace_capture_enabled"):
        (output_dir / "candidate-audit.md").write_text(
            _candidate_audit_report(results), encoding="utf-8", newline="\n"
        )
    _write_case_review_report(
        output_dir,
        results,
        dataset=dataset,
        evaluation_mode="retrieval" if dataset == "ecomretrieval" else mode,  # type: ignore[arg-type]
        suppress_validation_details=suppress_validation_details,
    )
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(report, encoding="utf-8", newline="\n")
    return output_dir


def cleanup_run(*, dataset: Literal["ecomretrieval", "multihoprag", "enterpriserag"], run_id: str) -> dict[str, Any]:
    """按 manifest 清理评测集合和父块；集合不存在时仍可安全重复执行。"""
    output_dir = run_directory(dataset, run_id)
    manifest_path = output_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    collection_name = manifest["collection_name"]
    uses_isolated_parent_store = (
        manifest.get("evaluation_storage_mode") == "isolated_postgresql"
    )
    if uses_isolated_parent_store:
        parent_store: ParentChunkStore | EvaluationParentChunkStore = EvaluationParentChunkStore(
            _safe_run_id(run_id),
            config=EvaluationStorageConfig.from_env(),
        )
        # Validate the dedicated target before deleting the corresponding Milvus
        # collection.  Never fall back to the business parent store during cleanup.
        parent_store.check_connection()
    else:
        parent_store = ParentChunkStore()
    collection_drop_status = "dropped"
    if _is_valid_milvus_collection_name(collection_name):
        _evaluation_store(collection_name).drop_collection()
    else:
        # 仅兼容修复前生成的非法名称；Milvus 从未能创建这种集合，因此可安全跳过。
        collection_drop_status = "skipped_invalid_legacy_name"
    if uses_isolated_parent_store:
        deleted_parent_chunks = parent_store.delete_by_corpus_run()
    else:
        deleted_parent_chunks = 0
        for filename in manifest.get("parent_filenames", []):
            deleted_parent_chunks += parent_store.delete_by_filename(filename)
    manifest.update({
        "cleanup_completed": True,
        "cleanup_at": datetime.now(UTC).isoformat(),
        "deleted_parent_chunks": deleted_parent_chunks,
        "collection_drop_status": collection_drop_status,
    })
    _write_json(manifest_path, manifest)
    return {
        "collection_name": collection_name,
        "collection_drop_status": collection_drop_status,
        "deleted_parent_chunks": deleted_parent_chunks,
    }
