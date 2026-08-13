"""RAG 检索流水线：召回、父块合并、可选精排、阈值过滤和诊断信息输出。

Environment variables:
    RERANK_MODEL: 外部精排服务使用的模型名称。与 RERANK_BINDING_HOST、RERANK_API_KEY
        同时配置后才启用精排；为空或教程占位符时不启用。
    RERANK_BINDING_HOST: 外部精排服务基础地址。代码会自动补齐 ``/v1/rerank``。
    RERANK_API_KEY: 调用精排服务时使用的 Bearer Token，不能提交到版本控制。
    RERANK_TIMEOUT_SECONDS: 精排 HTTP 请求超时秒数，默认 ``5``，最小 ``0.1``。
    RERANK_MIN_SCORE: 精排或召回结果的最低保留分数，默认 ``0.0``；设为正数会过滤低分结果。
    AUTO_MERGE_ENABLED: 是否启用 L3→L2→L1 父块上卷；默认 ``true``，仅 ``false`` 会关闭。
    AUTO_MERGE_THRESHOLD: 同一父块下至少命中多少个子块才上卷，默认 ``2``。
    LEAF_RETRIEVE_LEVEL: Milvus 初次召回的块层级，默认 ``3``，即 L3 叶子块。
    RETRIEVAL_TOP_K: 最终最多返回给上层的文档数量，默认 ``8``。
    RETRIEVAL_CANDIDATE_K: 可选的初次召回候选数量，优先级高于倍率配置，且不会小于 top_k。
    RETRIEVAL_CANDIDATE_MULTIPLIER: 未设置 CANDIDATE_K 时，候选数量相对于 top_k 的倍数，默认 ``3``。
"""

# defaultdict 用于按 parent_chunk_id 自动创建子块列表。
from collections import defaultdict
# 类型标注帮助说明检索结果、元数据和可选配置的结构。
from typing import Any, Dict, List, Literal, Optional, Tuple
# os 读取检索和精排相关环境变量；json 用于处理精排服务 JSON 异常。
import os
import json
# requests 用于调用可选的外部 Rerank HTTP 服务。
import requests
# init_chat_model 用于在需要查询改写时创建低延迟模型客户端。
from langchain.chat_models import init_chat_model
# BaseModel/Field 用于约束模型必须返回预期的重写计划结构。
from pydantic import BaseModel, Field

# Milvus 负责召回 L3 叶子块；嵌入服务负责生成查询向量；父块 Store 负责恢复 L1/L2。
from backend.indexing.milvus_client import get_milvus_store
from backend.indexing.embedding import embedding_service as _embedding_service
from backend.indexing.parent_chunk_store import ParentChunkStore


# 示例占位符按未配置处理，避免程序向 your-rerank-host 之类的假地址发请求。
def _optional_env(name: str) -> Optional[str]:
    """读取可选环境变量，并将教程占位符视为未配置。

    Args:
        name: 环境变量名称。

    Returns:
        Optional[str]: 可用配置值；变量为空或是 ``your_*`` 等占位符时返回 None。
    """
    # getenv 返回 None 时先转为空字符串，再去掉首尾空白。
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    normalized = value.lower()
    # 常见教程占位符不能作为真实服务地址或 API Key 使用。
    if (
        normalized.startswith(("your_", "your-", "replace-with"))
        or "your-rerank" in normalized
        or "your_rerank" in normalized
    ):
        return None
    return value


# RERANK_MODEL：外部精排服务使用的模型名称；未配置时精排关闭。
RERANK_MODEL = _optional_env("RERANK_MODEL")
# RERANK_BINDING_HOST 是外部精排服务的基础地址。
RERANK_BINDING_HOST = _optional_env("RERANK_BINDING_HOST")
# RERANK_API_KEY 用于 Bearer 认证。
RERANK_API_KEY = _optional_env("RERANK_API_KEY")
# 三项都存在时才真正启用精排，避免不完整配置导致请求失败。
RERANK_ENABLED = bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST)
try:
    # 超时至少为 0.1 秒，避免 0 或负数配置导致 requests 参数非法。
    RERANK_TIMEOUT_SECONDS = max(float(os.getenv("RERANK_TIMEOUT_SECONDS", "5")), 0.1)
except ValueError:
    # 环境变量不是数字时使用保守默认值 5 秒。
    RERANK_TIMEOUT_SECONDS = 5.0
# AUTO_MERGE_ENABLED：仅环境变量为字符串 "false" 时关闭，其他值或未配置时开启。
AUTO_MERGE_ENABLED = os.getenv("AUTO_MERGE_ENABLED", "true").lower() != "false"
# 命中同一父块的兄弟子块数量达到该阈值时，才向上合并父块。
AUTO_MERGE_THRESHOLD = int(os.getenv("AUTO_MERGE_THRESHOLD", "2"))
# Milvus 初次召回的层级，默认只召回 L3 叶子块。
LEAF_RETRIEVE_LEVEL = int(os.getenv("LEAF_RETRIEVE_LEVEL", "3"))


# 非法或非正整数退回默认值，检索参数错误不会在切片阶段才暴露。
def _read_positive_int_env(name: str, default: int) -> int:
    """读取一个必须为正整数的环境变量。

    Args:
        name: 环境变量名称。
        default: 缺失或非法时使用的默认值。

    Returns:
        int: 至少为 1 的整数。
    """
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except ValueError:
        return default


# RETRIEVAL_CANDIDATE_MULTIPLIER：默认 3，表示先召回 top_k 的 3 倍候选。
RETRIEVAL_CANDIDATE_MULTIPLIER = _read_positive_int_env("RETRIEVAL_CANDIDATE_MULTIPLIER", 3)
# 保留原始字符串，resolve_candidate_k() 需要区分“未设置”和“设置但格式错误”。
# RETRIEVAL_CANDIDATE_K：可选固定候选池数量；原始字符串留给后续区分未设置与非法配置。
_RETRIEVAL_CANDIDATE_K_RAW = os.getenv("RETRIEVAL_CANDIDATE_K", "").strip()
# RETRIEVAL_TOP_K：最终结果数上限，默认 8。
RETRIEVAL_TOP_K = _read_positive_int_env("RETRIEVAL_TOP_K", 8)


def _read_float_env(name: str, default: float) -> float:
    """读取浮点数环境变量；格式错误时回退默认值。"""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# RERANK_MIN_SCORE：最终保留结果的最低有效分，默认 0.0 表示不过滤有分结果。
RERANK_MIN_SCORE = _read_float_env("RERANK_MIN_SCORE", 0.0)

# 允许写入 API rag_trace 的诊断字段白名单。
RETRIEVAL_TRACE_FIELDS = (
    "retrieval_pipeline",
    "retrieval_mode",
    "candidate_k",
    "candidate_k_source",
    "candidate_k_config_error",
    "retrieval_candidate_multiplier",
    "retrieval_top_k",
    "leaf_retrieve_level",
    "recall_count",
    "post_merge_candidate_count",
    "candidate_count",
    "auto_merge_enabled",
    "auto_merge_applied",
    "auto_merge_threshold",
    "auto_merge_replaced_chunks",
    "auto_merge_steps",
    "rerank_enabled",
    "rerank_applied",
    "rerank_model",
    "rerank_endpoint",
    "rerank_error",
    "rerank_timeout_seconds",
    "rerank_min_score",
    "post_rerank_count",
    "post_threshold_count",
    "retrieval_empty",
)

# 全局初始化检索依赖（与 api 共用 embedding_service，保证 BM25 状态一致）
_milvus_manager = get_milvus_store()
_parent_chunk_store = ParentChunkStore()

# 候选池必须不少于最终 top_k，给合并、精排和阈值过滤留出余量。
def resolve_candidate_k(top_k: int) -> Tuple[int, Dict[str, Any]]:
    """解析 Milvus 候选池大小，并返回用于 trace 的配置来源。

    Args:
        top_k: 最终希望返回给上层的结果数量。

    Returns:
        Tuple[int, Dict[str, Any]]: 候选池大小和诊断配置。候选池永远不少于 top_k。
    """
    if _RETRIEVAL_CANDIDATE_K_RAW:
        try:
            candidate_k = max(int(_RETRIEVAL_CANDIDATE_K_RAW), top_k)
        except ValueError:
            # 显式配置无法转整数时，安全地改用倍率方案并在 trace 中记录错误。
            candidate_k = max(top_k * RETRIEVAL_CANDIDATE_MULTIPLIER, top_k)
            return candidate_k, {
                "candidate_k_source": "multiplier",
                "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
                "candidate_k_config_error": "invalid RETRIEVAL_CANDIDATE_K",
            }
        return candidate_k, {
            "candidate_k_source": "env",
            "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
        }
    candidate_k = max(top_k * RETRIEVAL_CANDIDATE_MULTIPLIER, top_k)
    return candidate_k, {
        "candidate_k_source": "multiplier",
        "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
    }


# 只把白名单诊断字段写入 rag_trace，内部临时对象不会泄露给 API。
def retrieval_trace_fields(meta: Dict[str, Any]) -> Dict[str, Any]:
    """从完整检索元数据中提取允许写入 API rag_trace 的字段。

    Args:
        meta: 检索流程内部生成的完整元数据字典。

    Returns:
        Dict[str, Any]: 只包含 RETRIEVAL_TRACE_FIELDS 中非空字段的字典。
    """
    return {key: meta[key] for key in RETRIEVAL_TRACE_FIELDS if key in meta and meta[key] is not None}


def _get_rerank_endpoint() -> str:
    """将精排服务基础地址规范化为完整 ``/v1/rerank`` 端点。"""
    if not RERANK_BINDING_HOST:
        return ""
    host = RERANK_BINDING_HOST.strip().rstrip("/")
    # 兼容用户已配置完整端点或只配置服务根地址两种形式。
    return host if host.endswith("/v1/rerank") else f"{host}/v1/rerank"

# 有精排分时优先使用精排分，否则才退回 RRF/召回分。
def _effective_score(doc: dict) -> Optional[float]:
    """获取文档的有效排名分，优先使用 rerank_score。

    Args:
        doc: 包含召回分 ``score`` 和可选精排分 ``rerank_score`` 的结果字典。

    Returns:
        Optional[float]: 可比较的分数；两者都不存在时返回 None。
    """
    rerank_score = doc.get("rerank_score")
    if rerank_score is not None:
        return float(rerank_score)
    score = doc.get("score")
    if score is not None:
        return float(score)
    return None


# 没有可用分数时，只有非正阈值允许该结果继续进入答案候选。
def _meets_rerank_min_score(doc: dict) -> bool:
    """判断文档是否满足 RERANK_MIN_SCORE 阈值。"""
    score = _effective_score(doc)
    if score is None:
        return RERANK_MIN_SCORE <= 0
    return score >= RERANK_MIN_SCORE


# 多个子块合并到同一父块时保留最高有效分，父块仍能参与后续排序。
def _merge_rank_score_into(target: dict, source: dict) -> None:
    """将 source 的最高有效分聚合到 target，用于多个子块替换为一个父块。

    Args:
        target: 将被原地更新的父块或已有结果字典。
        source: 当前命中的子块或重复结果字典。
    """
    incoming = _effective_score(source)
    if incoming is None:
        return
    uses_rerank = source.get("rerank_score") is not None or target.get("rerank_score") is not None
    if uses_rerank:
        existing = target.get("rerank_score")
        if existing is None:
            target["rerank_score"] = incoming
        else:
            target["rerank_score"] = max(float(existing), incoming)
        return
    existing = target.get("score")
    if existing is None:
        target["score"] = incoming
    else:
        target["score"] = max(float(existing), incoming)

# 同一层只处理一次子块到直接父块的上卷，不跨层跳跃。
def _merge_to_parent_level(docs: List[dict], threshold: int = 2) -> Tuple[List[dict], int]:
    """在同一层级中，将命中足够多兄弟块的结果上卷为其直接父块。

    Args:
        docs: 当前候选块列表。
        threshold: 同一 parent_chunk_id 至少命中多少个子块才上卷。

    Returns:
        Tuple[List[dict], int]: 上卷后的候选列表，以及被父块替换/合并的子块计数。
    """
    groups: Dict[str, List[dict]] = defaultdict(list)
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if parent_id:
            groups[parent_id].append(doc)

    # 只有命中兄弟数达到阈值才上卷，单个偶然命中仍保留叶子粒度。
    merge_parent_ids = [parent_id for parent_id, children in groups.items() if len(children) >= threshold]
    if not merge_parent_ids:
        return docs, 0

    # Milvus 只保存 L3，父块正文要按 ID 从 PostgreSQL/Redis 恢复。
    parent_docs = _parent_chunk_store.get_documents_by_ids(merge_parent_ids)
    # 以 chunk_id 建索引，方便 O(1) 取回某个父块正文。
    parent_map = {item.get("chunk_id", ""): item for item in parent_docs if item.get("chunk_id")}

    merged_docs: List[dict] = []
    parent_slot: Dict[str, int] = {}
    merged_count = 0
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if not parent_id or parent_id not in parent_map:
            # 没有父块、父块未加载成功或未达到合并条件的文档保持原样。
            merged_docs.append(doc)
            continue

        # 同一父块只占一个结果位置，后续兄弟只更新聚合分数和命中计数。
        if parent_id in parent_slot:
            existing = merged_docs[parent_slot[parent_id]]
            _merge_rank_score_into(existing, doc)
            merged_count += 1
            continue

        parent_doc = dict(parent_map[parent_id])
        # 复制父块字典，避免直接修改缓存/数据库读取结果。
        _merge_rank_score_into(parent_doc, doc)
        # 诊断字段标记该结果由子块合并而来，便于 trace 解释召回变化。
        parent_doc["merged_from_children"] = True
        parent_doc["merged_child_count"] = len(groups[parent_id])
        parent_slot[parent_id] = len(merged_docs)
        merged_docs.append(parent_doc)
        merged_count += 1

    return merged_docs, merged_count


def _empty_merge_meta() -> Dict[str, Any]:
    """创建 Auto-merge 未执行时使用的默认诊断元数据。"""
    return {
        "auto_merge_enabled": AUTO_MERGE_ENABLED,
        "auto_merge_applied": False,
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "auto_merge_replaced_chunks": 0,
        "auto_merge_steps": 0,
        "post_merge_candidate_count": 0,
    }


# Auto-merging 在完整候选池上执行，不能等截断到 top_k 后再恢复上下文。
def _auto_merge_candidates(docs: List[dict]) -> Tuple[List[dict], Dict[str, Any]]:
    """在完整候选池执行两次直接父级上卷：L3→L2，再 L2→L1。

    Args:
        docs: Milvus 初次召回的完整候选列表，尚未截断为 top_k。

    Returns:
        Tuple[List[dict], Dict[str, Any]]: 合并后的候选和 Auto-merge 诊断元数据。
    """
    meta = _empty_merge_meta()
    meta["post_merge_candidate_count"] = len(docs)
    if not AUTO_MERGE_ENABLED or not docs:
        # 配置关闭或没有召回结果时，不修改候选。
        return docs, meta

    # 第一步把满足阈值的 L3 叶子块替换为 L2。
    merged_docs, merged_count_l3_l2 = _merge_to_parent_level(docs, threshold=AUTO_MERGE_THRESHOLD)
    # 第二步再检查 L2 是否足以继续上卷到 L1。
    merged_docs, merged_count_l2_l1 = _merge_to_parent_level(merged_docs, threshold=AUTO_MERGE_THRESHOLD)

    replaced_count = merged_count_l3_l2 + merged_count_l2_l1
    # update 将本次两步合并的统计覆盖到默认 meta 上。
    meta.update({
        "auto_merge_applied": replaced_count > 0,
        "auto_merge_replaced_chunks": replaced_count,
        "auto_merge_steps": int(merged_count_l3_l2 > 0) + int(merged_count_l2_l1 > 0),
        "post_merge_candidate_count": len(merged_docs),
    })
    return merged_docs, meta


def _sort_by_rank_score(docs: List[dict]) -> List[dict]:
    """按有效排名分从高到低排序；无分数的结果按 0 处理。"""
    return sorted(docs, key=lambda item: _effective_score(item) or 0.0, reverse=True)


# 相同 chunk_id 只保留一个结果，并合并更高排名分，避免重复引用。
def dedupe_documents(docs: List[dict]) -> List[dict]:
    """按 chunk_id 去重；重复项保留更高排名分，同时保留首次出现位置。

    Args:
        docs: 可能包含相同 chunk_id 的结果列表。

    Returns:
        List[dict]: 去重后的结果列表。
    """
    by_key: Dict[str, dict] = {}
    order: List[str] = []
    for item in docs:
        chunk_id = (item.get("chunk_id") or "").strip()
        key = chunk_id or f"{item.get('filename')}|{item.get('page_number')}|{item.get('text')}"
        # chunk_id 缺失时使用文件名、页码和文本作为退化去重键。
        if key not in by_key:
            by_key[key] = item
            order.append(key)
            continue
        _merge_rank_score_into(by_key[key], item)
    return [by_key[key] for key in order]

# 精排只接收合并后的候选，并始终返回诊断 metadata。
def _rerank_documents(query: str, docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    """调用可选外部 Rerank 服务对合并后的候选重排；失败时保留原排序。

    Args:
        query: 用户原始问题。
        docs: 已完成 Auto-merge 的候选列表。
        top_k: 最终最多保留的结果数量。

    Returns:
        Tuple[List[dict], Dict[str, Any]]: 重排或降级排序后的文档与精排诊断元数据。
    """
    # rrf_rank 记录精排前的位置，供精排服务 index 映射回原文档。
    docs_with_rank = [{**doc, "rrf_rank": i} for i, doc in enumerate(docs, 1)]
    meta: Dict[str, Any] = {
        "rerank_enabled": RERANK_ENABLED,
        "rerank_applied": False,
        "rerank_model": RERANK_MODEL,
        "rerank_endpoint": _get_rerank_endpoint(),
        "rerank_error": None,
        "rerank_timeout_seconds": RERANK_TIMEOUT_SECONDS,
        "candidate_count": len(docs_with_rank),
    }
    # 未配置 Rerank 时按现有分数排序，不调用任何外部服务。
    if not docs_with_rank or not meta["rerank_enabled"]:
        return _sort_by_rank_score(docs_with_rank)[:top_k], meta

    payload = {
        # Rerank 服务常见的 OpenAI/Cohere 风格请求体。
        "model": RERANK_MODEL,
        "query": query,
        "documents": [doc.get("text", "") for doc in docs_with_rank],
        "top_n": min(top_k, len(docs_with_rank)),
        "return_documents": False,
    }

    headers = {
        # JSON 请求体和 Bearer Token 认证头。
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RERANK_API_KEY}",
    }
    try:
        meta["rerank_applied"] = True
        # 外部 Rerank 必须设置超时，避免上游卡住整条聊天请求。
        response = requests.post(
            meta["rerank_endpoint"],
            headers=headers,
            json=payload,
            timeout=RERANK_TIMEOUT_SECONDS,
        )
        # 上游返回错误时记录原因并保留原排名，检索主线仍可继续。
        if response.status_code >= 400:
            meta["rerank_error"] = f"HTTP {response.status_code}: {response.text}"
            return _sort_by_rank_score(docs_with_rank)[:top_k], meta

        items = response.json().get("results", [])
        # 精排服务每项通常包含原 documents 数组的 index 与 relevance_score。
        reranked = []
        for item in items:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs_with_rank):
                doc = dict(docs_with_rank[idx])
                # 复制原字典后写入 rerank_score，避免改变输入候选。
                score = item.get("relevance_score")
                if score is not None:
                    doc["rerank_score"] = score
                reranked.append(doc)

        if reranked:
            return reranked[:top_k], meta

        meta["rerank_error"] = "empty_rerank_results"
        return _sort_by_rank_score(docs_with_rank)[:top_k], meta
    # 网络、JSON 或字段异常都走同一降级路径，不把精排故障升级为检索失败。
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        meta["rerank_error"] = str(e)
        return _sort_by_rank_score(docs_with_rank)[:top_k], meta


# 固定流水线顺序为召回、父块合并、精排、阈值过滤，顺序变化会改变结果含义。
def _finalize_retrieval(
    query: str,
    retrieved: List[dict],
    top_k: int,
    retrieval_mode: str,
    candidate_k: int,
    candidate_config: Dict[str, Any],
) -> Dict[str, Any]:
    """执行召回后的固定流水线：合并、精排、阈值过滤和诊断元数据组装。

    Args:
        query: 用户问题。
        retrieved: Milvus 召回的候选列表。
        top_k: 最终结果数量上限。
        retrieval_mode: 实际召回模式，例如 hybrid 或 dense_fallback。
        candidate_k: 初次召回候选数量上限。
        candidate_config: 候选池配置来源诊断信息。

    Returns:
        Dict[str, Any]: 包含 ``docs`` 最终结果和 ``meta`` 全流程诊断数据的字典。
    """
    # 先在完整候选池恢复父上下文。
    candidates, merge_meta = _auto_merge_candidates(retrieved)
    reranked_docs, rerank_meta = _rerank_documents(query=query, docs=candidates, top_k=top_k)
    post_rerank_count = len(reranked_docs)
    # 精排后才执行最低分阈值，避免低相关结果进入回答上下文。
    final_docs = [d for d in reranked_docs if _meets_rerank_min_score(d)]
    meta = {
        **rerank_meta,
        **merge_meta,
        **candidate_config,
        "retrieval_mode": retrieval_mode,
        "retrieval_pipeline": "recall_merge_rerank",
        "candidate_k": candidate_k,
        "retrieval_top_k": top_k,
        "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
        "recall_count": len(retrieved),
        "rerank_min_score": RERANK_MIN_SCORE,
        "post_rerank_count": post_rerank_count,
        "post_threshold_count": len(final_docs),
        "retrieval_empty": len(final_docs) == 0,
    }
    return {"docs": final_docs, "meta": meta}

def build_filename_filter_expression(filenames: List[str] | tuple[str, ...] | None) -> str:
    """Build a Milvus-safe filename allowlist expression for a request-scoped retrieval."""
    unique_filenames = list(dict.fromkeys(
        filename.strip()
        for filename in (filenames or [])
        if isinstance(filename, str) and filename.strip()
    ))
    if not unique_filenames:
        return ""
    quoted_filenames = ", ".join(json.dumps(filename, ensure_ascii=False) for filename in unique_filenames)
    return f"filename in [{quoted_filenames}]"


def retrieve_documents(
    query: str,
    top_k: int = RETRIEVAL_TOP_K,
    knowledge_filenames: List[str] | tuple[str, ...] | None = None,
) -> Dict[str, Any]:
    """执行一次完整 RAG 检索，并在失败时返回可解释的空结果而非抛出异常。

    Args:
        query: 用户的自然语言问题。
        top_k: 最终返回的最大文档数量，默认读取 RETRIEVAL_TOP_K。
        knowledge_filenames: 可选文件名白名单；为空时检索全部已入库文件。

    Returns:
        Dict[str, Any]: ``docs`` 为最终检索块列表，``meta`` 为检索模式和各阶段统计。
    """
    # 先解析候选池大小，通常大于 top_k，给合并和精排留出余量。
    candidate_k, candidate_config = resolve_candidate_k(top_k)
    filter_expr = f"chunk_level == {LEAF_RETRIEVE_LEVEL}"
    filename_filter = build_filename_filter_expression(knowledge_filenames)
    if filename_filter:
        filter_expr = f"{filter_expr} and {filename_filter}"
    try:
        # 查询向量只计算一次，Hybrid 失败转 Dense 时直接复用。
        dense_embeddings = _embedding_service.get_embeddings([query])
        dense_embedding = dense_embeddings[0]
    except Exception:
        # 嵌入模型不可用时，Milvus 无法进行密集/混合检索，返回标准空结果结构。
        return {
            "docs": [],
            "meta": {
                "rerank_enabled": RERANK_ENABLED,
                "rerank_applied": False,
                "rerank_model": RERANK_MODEL,
                "rerank_endpoint": _get_rerank_endpoint(),
                "rerank_error": "embedding_failed",
                "rerank_timeout_seconds": RERANK_TIMEOUT_SECONDS,
                "retrieval_mode": "failed",
                "retrieval_pipeline": "recall_merge_rerank",
                "candidate_k": candidate_k,
                **candidate_config,
                "retrieval_top_k": top_k,
                "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                "recall_count": 0,
                **_empty_merge_meta(),
                "candidate_count": 0,
                "rerank_min_score": RERANK_MIN_SCORE,
                "post_rerank_count": 0,
                "post_threshold_count": 0,
                "retrieval_empty": True,
            },
        }

    try:
        # 优先使用 Dense + BM25 Hybrid，并把真实模式写入 metadata。
        retrieved = _milvus_manager.hybrid_retrieve(
            dense_embedding=dense_embedding,
            query=query,
            top_k=candidate_k,
            filter_expr=filter_expr,
        )
        return _finalize_retrieval(
            query=query,
            retrieved=retrieved,
            top_k=top_k,
            retrieval_mode="hybrid",
            candidate_k=candidate_k,
            candidate_config=candidate_config,
        )
    except Exception:
        try:
            # 只有 Hybrid 抛异常才降级 Dense；两路都失败时返回可解释的空结果。
            retrieved = _milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            return _finalize_retrieval(
                query=query,
                retrieved=retrieved,
                top_k=top_k,
                retrieval_mode="dense_fallback",
                candidate_k=candidate_k,
                candidate_config=candidate_config,
            )
        except Exception:
            # Hybrid 与 Dense 都不可用时同样返回可追踪的标准空结果。
            return {
                "docs": [],
                "meta": {
                    "rerank_enabled": RERANK_ENABLED,
                    "rerank_applied": False,
                    "rerank_model": RERANK_MODEL,
                    "rerank_endpoint": _get_rerank_endpoint(),
                    "rerank_error": "retrieve_failed",
                    "rerank_timeout_seconds": RERANK_TIMEOUT_SECONDS,
                    "retrieval_mode": "failed",
                    "retrieval_pipeline": "recall_merge_rerank",
                    "candidate_k": candidate_k,
                    **candidate_config,
                    "retrieval_top_k": top_k,
                    "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                    "recall_count": 0,
                    **_empty_merge_meta(),
                    "candidate_count": 0,
                    "rerank_min_score": RERANK_MIN_SCORE,
                    "post_rerank_count": 0,
                    "post_threshold_count": 0,
                    "retrieval_empty": True,
                },
            }


# 查询重写模型沿用环境中的地址和凭据，避免把敏感信息写入源码。
ARK_API_KEY = os.getenv("ARK_API_KEY")
FAST_MODEL = os.getenv("FAST_MODEL")
BASE_URL = os.getenv("BASE_URL")


class RewritePlan(BaseModel):
    """一次查询重写的结构化计划，强制 Step-back 与 HyDE 二选一。

    Attributes:
        method: 选择 ``step_back`` 或 ``hyde`` 中的一种重写策略。
        step_back_question: 更抽象的退步问题，仅 Step-back 策略可填写。
        hyde_document: 用于检索的假设性答案文档，仅 HyDE 策略可填写。
    """

    method: Literal["step_back", "hyde"] = Field(description="本轮唯一使用的查询重写方式")
    step_back_question: str = Field(
        default="",
        max_length=300,
        description="仅在 method=step_back 时填写的抽象退步问题",
    )
    hyde_document: str = Field(
        default="",
        max_length=1200,
        description="仅在 method=hyde 时填写的假设性答案文档",
    )


# 模型根据初次检索证据不足的原因，只选择一种改写方式，避免两种查询混在一起。
REWRITE_PROMPT = (
    "你是 RAG 查询重写规划器。初次检索已经找到相关信号，但证据不足。"
    "请在 step_back 和 hyde 中只选择一种重写方式，并同时生成该方式需要的内容。\n\n"
    "选择规则：\n"
    "- step_back：原问题过于具体，包含实体名、型号、时间、条件或细节，"
    "需要提升到更概括的概念、机制或原理后再检索。\n"
    "- hyde：原问题模糊、概念性强、缺少知识库常用术语，"
    "适合先生成一段可能的答案式文档，再用这段文档检索真实证据。\n\n"
    "约束：\n"
    "- method=step_back 时，只填写 step_back_question，hyde_document 必须留空。\n"
    "- method=hyde 时，只填写 hyde_document，step_back_question 必须留空。\n"
    "- HyDE 文档只能用于检索，不代表真实证据，不要编造引用或来源。\n\n"
    "用户问题：{query}"
)

# 仅缓存已创建的客户端；模块导入本身不应因查询改写而触发模型初始化。
_rewrite_model: Any | None = None


def _json_object_from_model_content(content: Any) -> Dict[str, Any]:
    """从 OpenAI 兼容模型的文本响应中提取并解析第一个 JSON 对象。

    当模型未返回函数调用而直接返回 JSON 文本时使用。代码也兼容部分模型用 Markdown
    代码块包裹 JSON 的情况；普通自然语言不会被误认为有效结构化结果。
    """
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        # 多模态模型可能把文本拆成多个内容块，此处只拼接可读文本部分。
        text = "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, dict))
        ).strip()
    else:
        raise ValueError("structured response has no text content")

    # 去掉可选的 Markdown 围栏，剩余文本再交给 JSON 解码器。
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])

    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise ValueError("structured response does not contain a JSON object")
    payload, _ = decoder.raw_decode(text[start:])
    if not isinstance(payload, dict):
        raise ValueError("structured response JSON must be an object")
    return payload


def invoke_structured_output(model: Any, schema: type[BaseModel], messages: List[dict]) -> BaseModel:
    """调用结构化模型；函数调用失败时仅额外进行一次 JSON 格式修复重试。

    先使用 LangChain 的 function calling 得到 Pydantic 已解析结果。某些兼容接口会忽略
    工具调用而返回 JSON 文本，因此会回退为文本解析；第二轮只补充格式约束，不会无限重试。
    """
    tool_name = schema.__name__
    repair_prompt = (
        f"上一条响应无法解析。请调用 {tool_name}，或只输出一个合法 JSON 对象；"
        f"必须包含这些字段：{', '.join(schema.model_fields)}。不要解释，不要使用 Markdown。"
    )
    errors: List[Exception] = []

    # 第一轮为正常请求，第二轮才要求模型修复其返回格式。
    for attempt_messages in (messages, [*messages, {"role": "user", "content": repair_prompt}]):
        result = model.with_structured_output(
            schema,
            method="function_calling",
            tool_choice={"type": "function", "function": {"name": tool_name}},
            include_raw=True,
        ).invoke(attempt_messages)

        if isinstance(result, schema):
            return result
        if not isinstance(result, dict):
            errors.append(RuntimeError("unsupported structured result"))
            continue

        parsed = result.get("parsed")
        if isinstance(parsed, schema):
            return parsed

        try:
            raw = result.get("raw")
            return schema.model_validate(_json_object_from_model_content(getattr(raw, "content", None)))
        except Exception as exc:
            errors.append(exc)

    raise RuntimeError(
        f"{tool_name} returned neither a tool call nor valid JSON after a format retry"
    ) from errors[-1]


def _get_rewrite_model() -> Any | None:
    """按需创建并缓存低延迟查询重写模型；缺少配置时返回 None。"""
    global _rewrite_model
    if not ARK_API_KEY or not FAST_MODEL:
        return None
    if _rewrite_model is None:
        _rewrite_model = init_chat_model(
            model=FAST_MODEL,
            model_provider="openai",
            api_key=ARK_API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _rewrite_model


def rewrite_query_once(query: str) -> Dict[str, str]:
    """根据问题生成一次 Step-back 或 HyDE 查询，并返回可写入 rag trace 的字段。

    返回的 ``rewritten_query`` 只能用来发起下一次检索。特别是 HyDE 文档是模型假设，
    不能被当成最终答案的事实依据或引用来源。
    """
    model = _get_rewrite_model()
    if not model:
        raise RuntimeError("FAST_MODEL is required for query rewriting")

    result = invoke_structured_output(
        model,
        RewritePlan,
        [{"role": "user", "content": REWRITE_PROMPT.format(query=query)}],
    )
    method = result.method
    step_back_question = result.step_back_question.strip()
    hyde_document = result.hyde_document.strip()

    if method == "step_back":
        # 防御性校验模型是否遵守二选一的协议，即使 Pydantic 验证已通过也不能省略。
        if not step_back_question or hyde_document:
            raise ValueError("Step-back rewrite plan must contain only step_back_question")
        rewritten_query = f"{query}\n\n退步问题：{step_back_question}"
    elif method == "hyde":
        if not hyde_document or step_back_question:
            raise ValueError("HyDE rewrite plan must contain only hyde_document")
        rewritten_query = f"{query}\n\n假设性答案文档：{hyde_document}"
    else:
        # Literal 已限制合法值，这里用于防御运行时异常或未来类型变更。
        raise ValueError(f"Unsupported rewrite method: {method}")

    return {
        "rewrite_method": method,
        "rewritten_query": rewritten_query,
        "step_back_question": step_back_question,
        "hyde_document": hyde_document,
    }
