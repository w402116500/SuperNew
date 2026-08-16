"""将公开基准适配为本项目统一的 Markdown 语料与评测题目。"""

from __future__ import annotations

import json
import heapq
from itertools import islice
import re
import sqlite3
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable


def stable_filename(
    identifier: str,
    title: str,
    suffix: str = ".md",
    max_length: int = 120,
) -> str:
    """根据公开数据 ID 生成长度受限且跨系统稳定的文件名。"""
    raw_identifier = str(identifier)
    digest = sha256(raw_identifier.encode("utf-8")).hexdigest()[:16]
    max_length = max(max_length, 40)
    minimum_title = "untitled"
    overhead = len(digest) + 4 + len(suffix)
    identifier_budget = min(40, max_length - overhead - len(minimum_title))
    normalized_id = (
        re.sub(r"[^A-Za-z0-9._-]+", "_", raw_identifier).strip("._")[:identifier_budget]
        or "document"
    )
    title_budget = max_length - overhead - len(normalized_id)
    normalized_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:title_budget]
    return f"{normalized_id}__{digest}__{normalized_title or 'untitled'}{suffix}"


def ecom_markdown(record: dict[str, Any]) -> str:
    """将商品标题和详情原样转换为可读 Markdown，避免人为补充商品事实。"""
    title = str(record.get("title") or "未命名商品").strip()
    text = str(record.get("text") or "").strip()
    return f"# {title}\n\n{text}\n"


def multihop_markdown(record: dict[str, Any]) -> str:
    """将 MultiHopRAG 的来源元数据和正文转为单篇标准 Markdown。"""
    title = str(record.get("title") or "Untitled").strip()
    metadata = [
        ("Source", record.get("source")),
        ("Author", record.get("author")),
        ("Published", record.get("published_at")),
        ("Category", record.get("category")),
        ("URL", record.get("url")),
    ]
    lines = [f"# {title}", ""]
    for key, value in metadata:
        if value:
            lines.append(f"- {key}: {value}")
    lines.extend(["", "## Content", "", str(record.get("body") or "").strip(), ""])
    return "\n".join(lines)


ENTERPRISE_FULL_ORDINARY_COUNT = 6500
ENTERPRISE_SMOKE_ORDINARY_COUNT = 100
ENTERPRISE_SEED = 20260813
ENTERPRISE_ANALYSIS_QUOTAS = {
    "basic": 105,
    "semantic": 75,
    "intra_document_reasoning": 24,
    "project_related": 24,
    "constrained": 18,
    "completeness": 12,
    "conflicting_info": 12,
    "info_not_found": 12,
    "miscellaneous": 12,
    "high_level": 6,
}
ENTERPRISE_VALIDATION_QUOTAS = {
    "basic": 70,
    "semantic": 50,
    "intra_document_reasoning": 16,
    "project_related": 16,
    "constrained": 12,
    "completeness": 8,
    "conflicting_info": 8,
    "info_not_found": 8,
    "miscellaneous": 8,
    "high_level": 4,
}


def enterprise_markdown(record: dict[str, Any]) -> str:
    """把 EnterpriseRAG 文档转换为只含标题和正文的 Markdown。"""
    title = str(record.get("title") or "Untitled Document").strip()
    content = str(record.get("content") or "").strip()
    return f"# {title}\n\n{content}\n"


def enterprise_document_filename(doc_id: str) -> str:
    """为 EnterpriseRAG 文档生成不携带评测标签的稳定文件名。"""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(doc_id)).strip("._") or "document"
    return f"enterprise__{normalized}.md"


def enterprise_canonical_zh_document_dir(dataset_root: Path) -> Path:
    """返回人工复核通过的中文 EnterpriseRAG Markdown 目录。"""
    return (
        dataset_root
        / "derived"
        / "v1"
        / "zh-primary"
        / "manual-codex-retranslation"
        / "canonical"
        / "documents"
    )


def load_enterprise_canonical_markdown(
    dataset_root: Path,
    document_ids: Iterable[str],
    *,
    canonical_dir: Path | None = None,
) -> dict[str, str]:
    """读取指定文档的规范中文译文，并拒绝静默回退到英文或旧缓存。"""
    directory = canonical_dir or enterprise_canonical_zh_document_dir(dataset_root)
    markdown_by_id: dict[str, str] = {}
    missing: list[str] = []
    empty: list[str] = []
    for raw_doc_id in sorted({str(value) for value in document_ids}):
        path = directory / enterprise_document_filename(raw_doc_id)
        if not path.is_file():
            missing.append(raw_doc_id)
            continue
        markdown = path.read_text(encoding="utf-8")
        if not markdown.strip():
            empty.append(raw_doc_id)
            continue
        markdown_by_id[raw_doc_id] = markdown if markdown.endswith("\n") else markdown + "\n"
    if missing or empty:
        problems = []
        if missing:
            problems.append(f"缺失 {len(missing)} 篇：{missing[:5]}")
        if empty:
            problems.append(f"空文件 {len(empty)} 篇：{empty[:5]}")
        raise FileNotFoundError(
            "EnterpriseRAG 中文 canonical 语料不完整（"
            + "；".join(problems)
            + f"）。目录：{directory}。请先完成整篇翻译和人工核验，不能回退到英文。"
        )
    return markdown_by_id


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    """读取 Parquet，并在缺失依赖时给出明确安装提示。"""
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("读取 EcomRetrieval 需要 pyarrow，请先执行 uv sync。") from exc
    return pq.read_table(path).to_pylist()


def _parquet_file(path: Path):
    """打开 Parquet 文件，并统一处理 pyarrow 缺失错误。"""
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("读取 EcomRetrieval 需要 pyarrow，请先执行 uv sync。") from exc
    return pq.ParquetFile(path)


def _enterprise_document_path(dataset_root: Path) -> Path:
    path = dataset_root / "data" / "documents" / "test.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"未找到 EnterpriseRAG 文档 Parquet：{path}")
    return path


def _enterprise_question_path(dataset_root: Path) -> Path:
    path = dataset_root / "data" / "questions" / "test.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"未找到 EnterpriseRAG 问题 Parquet：{path}")
    return path


def enterprise_document_count(dataset_root: Path) -> int:
    """读取 EnterpriseRAG 文档总数，不加载正文。"""
    return _parquet_file(_enterprise_document_path(dataset_root)).metadata.num_rows


def iter_enterprise_documents(dataset_root: Path, batch_size: int = 1000) -> Iterable[dict[str, Any]]:
    """流式读取 EnterpriseRAG 文档，避免 1.4GB Parquet 进入内存。"""
    parquet_file = _parquet_file(_enterprise_document_path(dataset_root))
    columns = ["doc_id", "source_type", "title", "content"]
    for batch in parquet_file.iter_batches(batch_size=batch_size, columns=columns):
        yield from batch.to_pylist()


def load_enterprise_cases(dataset_root: Path) -> list[dict[str, Any]]:
    """读取 EnterpriseRAG 问题、标准证据和参考答案。"""
    rows = _read_parquet(_enterprise_question_path(dataset_root))
    cases: list[dict[str, Any]] = []
    for row in rows:
        question = str(row.get("question") or "").strip()
        if not question:
            continue
        cases.append({
            "id": str(row.get("question_id") or ""),
            "question": question,
            "reference_answer": str(row.get("gold_answer") or ""),
            "question_type": str(row.get("question_type") or "unknown"),
            "expected_doc_ids": [str(value) for value in row.get("expected_doc_ids") or []],
            "answer_facts": [str(value) for value in row.get("answer_facts") or []],
            "source_types": [str(value) for value in row.get("source_types") or []],
        })
    return cases


def _largest_remainder_quotas(counts: dict[str, int], target: int) -> dict[str, int]:
    """按来源比例分配整数配额，使用最大余数法保证总和稳定。"""
    if target < 0:
        raise ValueError("抽样目标不能为负数")
    total = sum(counts.values())
    if not total or not target:
        return {key: 0 for key in counts}
    raw = {key: value * target / total for key, value in counts.items()}
    quotas = {key: int(value) for key, value in raw.items()}
    remainder = target - sum(quotas.values())
    order = sorted(
        counts,
        key=lambda key: (raw[key] - quotas[key], key),
        reverse=True,
    )
    for key in order[:remainder]:
        quotas[key] += 1
    return quotas


def _stable_rank(seed: int, doc_id: str) -> int:
    digest = sha256(f"{seed}:{doc_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def split_enterprise_cases(
    cases: list[dict[str, Any]],
    *,
    seed: int = ENTERPRISE_SEED,
) -> dict[str, Any]:
    """Build the immutable 300-analysis / 200-validation EnterpriseRAG split.

    Case IDs are ranked with the same stable hash used for distractor sampling.
    This prevents input Parquet row order from changing the frozen split.
    """
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        question_type = str(case.get("question_type") or "")
        if not case_id or not question_type:
            raise ValueError("EnterpriseRAG case split requires non-empty id and question_type")
        if case_id in seen_ids:
            raise ValueError(f"EnterpriseRAG case ID is duplicated: {case_id}")
        seen_ids.add(case_id)
        by_type[question_type].append(case)

    expected_types = set(ENTERPRISE_ANALYSIS_QUOTAS)
    unexpected_types = sorted(set(by_type) - expected_types)
    missing_types = sorted(expected_types - set(by_type))
    if unexpected_types or missing_types:
        raise ValueError(
            "EnterpriseRAG question types do not match the formal split contract: "
            f"missing={missing_types}, unexpected={unexpected_types}"
        )

    analysis_ids: list[str] = []
    validation_ids: list[str] = []
    by_type_manifest: dict[str, dict[str, Any]] = {}
    for question_type in sorted(expected_types):
        analysis_quota = ENTERPRISE_ANALYSIS_QUOTAS[question_type]
        validation_quota = ENTERPRISE_VALIDATION_QUOTAS[question_type]
        ordered = sorted(
            by_type[question_type],
            key=lambda item: (_stable_rank(seed, f"case:{item['id']}"), str(item["id"])),
        )
        if len(ordered) != analysis_quota + validation_quota:
            raise ValueError(
                f"EnterpriseRAG {question_type} expected {analysis_quota + validation_quota} cases, "
                f"got {len(ordered)}"
            )
        analysis = [str(item["id"]) for item in ordered[:analysis_quota]]
        validation = [str(item["id"]) for item in ordered[analysis_quota:]]
        analysis_ids.extend(analysis)
        validation_ids.extend(validation)
        by_type_manifest[question_type] = {
            "analysis_quota": analysis_quota,
            "validation_quota": validation_quota,
            "analysis_case_ids": analysis,
            "validation_case_ids": validation,
        }

    if len(analysis_ids) != 300 or len(validation_ids) != 200:
        raise AssertionError("EnterpriseRAG formal split must contain exactly 300 analysis and 200 validation cases")
    if set(analysis_ids) & set(validation_ids) or len(set(analysis_ids) | set(validation_ids)) != len(cases):
        raise AssertionError("EnterpriseRAG formal case sets must be disjoint and exhaustive")
    return {
        "dataset": "enterpriserag",
        "schema_version": 1,
        "seed": seed,
        "case_count": len(cases),
        "analysis_case_ids": analysis_ids,
        "validation_case_ids": validation_ids,
        "question_types": by_type_manifest,
    }


def select_enterprise_ordinary_ids(
    dataset_root: Path,
    required_ids: set[str],
    target_count: int,
    *,
    seed: int = ENTERPRISE_SEED,
) -> tuple[set[str], dict[str, int], dict[str, int]]:
    """排除标准证据后按九类来源比例稳定抽取普通干扰文档。"""
    available_counts: Counter[str] = Counter()
    for row in iter_enterprise_documents(dataset_root):
        if str(row["doc_id"]) not in required_ids:
            available_counts[str(row.get("source_type") or "unknown")] += 1
    quotas = _largest_remainder_quotas(dict(available_counts), target_count)
    selected_by_source: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in iter_enterprise_documents(dataset_root):
        doc_id = str(row["doc_id"])
        source_type = str(row.get("source_type") or "unknown")
        quota = quotas.get(source_type, 0)
        if not quota or doc_id in required_ids:
            continue
        rank = _stable_rank(seed, doc_id)
        heap = selected_by_source[source_type]
        item = (-rank, doc_id)
        if len(heap) < quota:
            heapq.heappush(heap, item)
        elif rank < -heap[0][0]:
            heapq.heapreplace(heap, item)
    selected = {doc_id for heap in selected_by_source.values() for _, doc_id in heap}
    actual = Counter()
    for source_type, heap in selected_by_source.items():
        actual[source_type] = len(heap)
    if len(selected) < target_count:
        raise RuntimeError(f"普通干扰抽样不足：目标 {target_count}，实际 {len(selected)}")
    return selected, quotas, dict(actual)


def _title_fts_query(question: str) -> str:
    tokens = []
    for token in re.findall(r"[\w]+", question, flags=re.UNICODE):
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 64:
            break
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def build_enterprise_title_index(dataset_root: Path, index_path: Path) -> Path:
    """用 SQLite FTS5 建立可复用的标题 BM25 索引，不保存正文。"""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        return index_path
    connection = sqlite3.connect(index_path)
    try:
        connection.execute("CREATE VIRTUAL TABLE titles USING fts5(doc_id UNINDEXED, title)")
        rows = (
            (str(row["doc_id"]), str(row.get("title") or ""))
            for row in iter_enterprise_documents(dataset_root)
        )
        while batch := list(islice(rows, 2000)):
            connection.executemany("INSERT INTO titles(doc_id, title) VALUES (?, ?)", batch)
        connection.commit()
    finally:
        connection.close()
    return index_path


def select_enterprise_hard_ids(
    dataset_root: Path,
    cases: list[dict[str, Any]],
    excluded_ids: set[str],
    index_path: Path,
    *,
    per_case: int = 2,
) -> tuple[set[str], dict[str, list[str]]]:
    """用全量标题的 SQLite BM25 为每题选择不在证据集中的难干扰文档。"""
    build_enterprise_title_index(dataset_root, index_path)
    connection = sqlite3.connect(index_path)
    selected: set[str] = set()
    by_case: dict[str, list[str]] = {}
    try:
        for case in cases:
            query = _title_fts_query(case["question"])
            if not query:
                by_case[case["id"]] = []
                continue
            rows = connection.execute(
                "SELECT doc_id FROM titles WHERE titles MATCH ? ORDER BY bm25(titles) LIMIT ?",
                (query, max(per_case * 8, per_case)),
            ).fetchall()
            case_ids: list[str] = []
            for (doc_id,) in rows:
                doc_id = str(doc_id)
                if doc_id in excluded_ids or doc_id in selected:
                    continue
                case_ids.append(doc_id)
                selected.add(doc_id)
                if len(case_ids) >= per_case:
                    break
            by_case[case["id"]] = case_ids
    finally:
        connection.close()
    return selected, by_case


def load_enterprise_documents_by_ids(
    dataset_root: Path,
    document_ids: set[str],
) -> list[dict[str, Any]]:
    """第二次流式扫描只加载已选中文档的正文。"""
    documents = []
    remaining = set(document_ids)
    for row in iter_enterprise_documents(dataset_root):
        doc_id = str(row["doc_id"])
        if doc_id in remaining:
            documents.append(row)
            remaining.remove(doc_id)
            if not remaining:
                break
    if remaining:
        raise RuntimeError(f"EnterpriseRAG 缺少文档：{sorted(remaining)[:5]}")
    return documents


def select_enterprise_smoke_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每类固定抽取第一道题，得到 10 类共 10 道冒烟题。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[case["question_type"]].append(case)
    return [
        sorted(grouped[question_type], key=lambda item: item["id"])[0]
        for question_type in sorted(grouped)
        if grouped[question_type]
    ]


def _find_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"未找到 {root / pattern}")
    return matches[0]


def load_ecom_dataset(dataset_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """加载 EcomRetrieval 语料和带标准文档 ID 的查询。"""
    corpus = _read_parquet(_find_one(dataset_root / "corpus", "*.parquet"))
    return corpus, load_ecom_cases(dataset_root)


def load_ecom_cases(dataset_root: Path) -> list[dict[str, Any]]:
    """只读取 EcomRetrieval 的查询和标准关联，供流式入库前固定题目使用。"""
    queries = _read_parquet(_find_one(dataset_root / "queries", "*.parquet"))
    qrels = _read_parquet(_find_one(dataset_root / "data", "*.parquet"))
    relevant_by_query: dict[str, list[str]] = {}
    for row in qrels:
        if float(row.get("score", 0) or 0) > 0:
            relevant_by_query.setdefault(str(row["query-id"]), []).append(str(row["corpus-id"]))
    cases = [
        {
            "id": str(item["_id"]),
            "question": str(item.get("text") or ""),
            "relevant_document_ids": relevant_by_query.get(str(item["_id"]), []),
        }
        for item in queries
        if relevant_by_query.get(str(item["_id"]))
    ]
    return cases


def ecom_corpus_count(dataset_root: Path) -> int:
    """从 Parquet 元数据读取语料数量，不把全量商品文本加载到内存。"""
    return _parquet_file(_find_one(dataset_root / "corpus", "*.parquet")).metadata.num_rows


def iter_ecom_corpus(dataset_root: Path, batch_size: int = 500) -> Iterable[dict[str, Any]]:
    """流式读取商品语料，避免完整基准在向量化前占用数 GB 内存。"""
    path = _find_one(dataset_root / "corpus", "*.parquet")
    for batch in _parquet_file(path).iter_batches(batch_size=batch_size):
        yield from batch.to_pylist()


def load_multihop_dataset(dataset_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """加载 MultiHopRAG，并将 gold evidence URL 解析为题目标准证据。"""
    corpus = json.loads((dataset_root / "corpus.json").read_text(encoding="utf-8"))
    raw_cases = json.loads((dataset_root / "MultiHopRAG.json").read_text(encoding="utf-8"))
    cases = [
        {
            "id": str(index),
            "question": str(item.get("query") or ""),
            "reference_answer": str(item.get("answer") or ""),
            "question_type": str(item.get("question_type") or "unknown").removesuffix("_query"),
            "evidence_urls": [
                evidence.get("url") if isinstance(evidence, dict) else evidence
                for evidence in item.get("evidence_list") or []
            ],
        }
        for index, item in enumerate(raw_cases, start=1)
        if item.get("query")
    ]
    return corpus, cases


def select_smoke_cases(cases: list[dict[str, Any]], dataset: str) -> list[dict[str, Any]]:
    """按计划选择可复现冒烟集，不依赖输入文件的原始顺序。"""
    if dataset == "ecomretrieval":
        import random

        rng = random.Random(20260813)
        return sorted(rng.sample(cases, min(100, len(cases))), key=lambda item: item["id"])

    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(case["question_type"], []).append(case)
    selected: list[dict[str, Any]] = []
    for question_type in sorted(grouped):
        selected.extend(sorted(grouped[question_type], key=lambda item: item["id"])[:30])
    return selected


def write_markdown_documents(
    records: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    dataset: str,
    filename_prefix: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """输出语料 Markdown，并返回入库元数据和原始 ID 到文件名的稳定映射。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    id_to_filename: dict[str, str] = {}
    for index, record in enumerate(records):
        if dataset == "ecomretrieval":
            source_id = str(record["_id"])
            title = str(record.get("title") or "")
            markdown = ecom_markdown(record)
        else:
            source_id = str(record.get("url") or index)
            title = str(record.get("title") or "")
            markdown = multihop_markdown(record)
        filename_limit = max(40, 120 - len(filename_prefix))
        filename = f"{filename_prefix}{stable_filename(source_id, title, max_length=filename_limit)}"
        (output_dir / filename).write_text(markdown, encoding="utf-8", newline="\n")
        id_to_filename[source_id] = filename
        documents.append({
            "source_id": source_id,
            "filename": filename,
            "path": str(output_dir / filename),
            "markdown": markdown,
        })
    return documents, id_to_filename
