# 本模块负责将 DocumentLoader 产生的文本块转为密集向量，并写入 Milvus。
"""文档向量化并写入 Milvus。

代码显式生成密集向量；稀疏 BM25 向量由 Milvus 集合 schema 中的 Function 根据 ``text``
字段自动生成，因此可用于后续混合检索。
"""

# os 用于读取环境变量中的密集向量维度。
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import chain, islice
from typing import Iterable

# EmbeddingService 用于生成密集向量；默认单例避免重复加载模型权重。
from backend.indexing.embedding import EmbeddingService, embedding_service as _default_embedding_service
# Keep structured Markdown fields visible in dynamic-field Milvus collections.
from backend.indexing.chunk_metadata import structured_chunk_metadata
# MilvusStore 封装集合写入；get_milvus_store 获取全局 Store 实例。
from backend.indexing.milvus_client import MilvusStore, get_milvus_store


class MilvusWriter:
    """批量向量化文本块并写入 Milvus 的服务。

    写入的每条数据包含密集向量、正文、来源文件信息和 L1/L2/L3 父子关系字段。
    一般由上层传入 L3 叶子块用于检索，但本类不会自行按 ``chunk_level`` 过滤数据。
    """

    def __init__(self, embedding_service: EmbeddingService = None, milvus_manager: MilvusStore = None):
        """初始化写入器，可选注入模型服务和 Milvus Store。

        Args:
            embedding_service: 可选的向量化服务；不传时使用模块全局默认模型实例。
            milvus_manager: 可选的 Milvus Store；不传时使用全局默认 Store。
        """
        # 使用调用方注入的服务，或回退到预创建的全局嵌入模型单例。
        self.embedding_service = embedding_service or _default_embedding_service
        # 使用调用方注入的 Store，或通过工厂取得全局配置 Store。
        self.milvus_manager = milvus_manager or get_milvus_store()

    def write_documents(
        self,
        documents: Iterable[dict],
        batch_size: int = 50,
        progress_callback=None,
        max_retries: int | None = None,
        embedding_workers: int | None = None,
    ):
        """分批生成文档块的密集向量，并写入 Milvus。

        Args:
            documents: 待写入的块字典迭代器。每项至少应含 ``text``、``filename``、
                ``file_type``，并可包含页码、块 ID 与父子关系字段。
            batch_size: 每次送入嵌入模型和 Milvus 的块数量，默认 50。
            progress_callback: 可选进度回调函数，调用形式为 ``callback(processed, total)``。

        Returns:
            None: 写入成功后不返回数据；错误会由嵌入模型或 Milvus Store 向上传播。
        """
        # 允许评测传入流式迭代器，避免大语料同时保留原始文本、Markdown 和分块列表。
        iterator = iter(documents)
        first_document = next(iterator, None)
        if first_document is None:
            return
        iterator = chain((first_document,), iterator)
        total = len(documents) if hasattr(documents, "__len__") else None

        # Milvus dense_embedding 字段的维度必须与嵌入模型输出长度一致。
        dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))
        if batch_size <= 0:
            raise ValueError("MilvusWriter batch_size 必须是正整数")
        if max_retries is None:
            try:
                max_retries = int(os.getenv("MILVUS_WRITE_RETRIES", "3"))
            except ValueError:
                max_retries = 3
        max_retries = min(max(int(max_retries), 0), 5)
        if embedding_workers is None:
            try:
                # Legacy callers keep the previous serial behavior unless the
                # evaluation runner opts into its explicit 10-worker setting.
                embedding_workers = int(os.getenv("EMBEDDING_MAX_WORKERS", "1"))
            except ValueError:
                embedding_workers = 1
        embedding_workers = min(max(int(embedding_workers), 1), 10)

        # total 用于切片边界和进度回调。
        # 写入前确保集合、schema 和索引存在；已存在时该操作不会重复创建。
        self.milvus_manager.init_collection(dense_dim)

        # 按批向量化和插入，支持生成器以控制大语料的峰值内存。
        def embed_batch(batch_index: int, batch: list[dict]) -> tuple[int, list[dict], list[list[float]]]:
            texts = [doc["text"] for doc in batch]
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    vectors = self.embedding_service.get_embeddings(texts)
                    if len(vectors) != len(batch):
                        raise RuntimeError(
                            "Embedding 返回数量与输入文档数量不一致："
                            f"expected={len(batch)}, actual={len(vectors)}"
                        )
                    return batch_index, batch, vectors
                except Exception as exc:
                    last_error = exc
                    if attempt < max_retries:
                        time.sleep(min(2 ** attempt, 4))
            raise RuntimeError(
                f"Embedding 批次失败，已重试 {max_retries} 次，batch_size={len(batch)}：{last_error}"
            ) from last_error

        def insert_batch(batch: list[dict], dense_embeddings: list[list[float]]) -> None:
            insert_data = [
                {
                    "dense_embedding": dense_emb,
                    "text": doc["text"],
                    "filename": doc["filename"],
                    "file_type": doc["file_type"],
                    "file_path": doc.get("file_path", ""),
                    "page_number": doc.get("page_number", 0),
                    "chunk_idx": doc.get("chunk_idx", 0),
                    "chunk_id": doc.get("chunk_id", ""),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": doc.get("chunk_level", 0),
                    **structured_chunk_metadata(doc),
                }
                for doc, dense_emb in zip(batch, dense_embeddings)
            ]
            remaining = insert_data
            last_error = None
            for attempt in range(max_retries + 1):
                if not remaining:
                    return
                try:
                    self.milvus_manager.insert(remaining)
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt < max_retries:
                        # A transport timeout may have committed the batch.  Query
                        # stable chunk IDs before retrying, so only missing rows are
                        # sent again instead of duplicating an accepted batch.
                        try:
                            ids = [str(row.get("chunk_id") or "") for row in remaining]
                            existing = self.milvus_manager.get_chunks_by_ids(ids)
                            existing_ids = {
                                str(row.get("chunk_id") or "") for row in (existing or [])
                            }
                            remaining = [
                                row for row in remaining
                                if str(row.get("chunk_id") or "") not in existing_ids
                            ]
                        except Exception:
                            # If the read-back is unavailable, retain the full
                            # batch and surface the original write error on failure.
                            remaining = insert_data
                        if remaining:
                            time.sleep(min(2 ** attempt, 4))
            raise RuntimeError(
                f"Milvus 批量写入失败，已重试 {max_retries} 次，batch_size={len(insert_data)}"
            ) from last_error

        # Keep at most embedding_workers batches in flight.  Embedding calls can
        # overlap, while this loop remains the single Milvus writer and commits
        # batches in deterministic input order.
        processed = 0
        next_batch_index = 0
        next_to_write = 0
        pending = {}
        completed: dict[int, tuple[list[dict], list[list[float]]]] = {}
        exhausted = False
        with ThreadPoolExecutor(
            max_workers=embedding_workers,
            thread_name_prefix="enterprise-embedding",
        ) as executor:
            while pending or not exhausted:
                while not exhausted and len(pending) < embedding_workers:
                    batch = list(islice(iterator, batch_size))
                    if not batch:
                        exhausted = True
                        break
                    future = executor.submit(embed_batch, next_batch_index, batch)
                    pending[future] = next_batch_index
                    next_batch_index += 1
                if not pending:
                    continue
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    pending.pop(future)
                    batch_index, batch, vectors = future.result()
                    completed[batch_index] = (batch, vectors)
                while next_to_write in completed:
                    batch, vectors = completed.pop(next_to_write)
                    insert_batch(batch, vectors)
                    processed += len(batch)
                    next_to_write += 1
                    if progress_callback and total is not None:
                        progress_callback(processed, total)
