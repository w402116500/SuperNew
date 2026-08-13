# 本模块负责将 DocumentLoader 产生的文本块转为密集向量，并写入 Milvus。
"""文档向量化并写入 Milvus。

代码显式生成密集向量；稀疏 BM25 向量由 Milvus 集合 schema 中的 Function 根据 ``text``
字段自动生成，因此可用于后续混合检索。
"""

# os 用于读取环境变量中的密集向量维度。
import os

# EmbeddingService 用于生成密集向量；默认单例避免重复加载模型权重。
from backend.indexing.embedding import EmbeddingService, embedding_service as _default_embedding_service
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

    def write_documents(self, documents: list[dict], batch_size: int = 50, progress_callback=None):
        """分批生成文档块的密集向量，并写入 Milvus。

        Args:
            documents: 待写入的块字典列表。每项至少应含 ``text``、``filename``、
                ``file_type``，并可包含页码、块 ID 与父子关系字段。
            batch_size: 每次送入嵌入模型和 Milvus 的块数量，默认 50。
            progress_callback: 可选进度回调函数，调用形式为 ``callback(processed, total)``。

        Returns:
            None: 写入成功后不返回数据；错误会由嵌入模型或 Milvus Store 向上传播。
        """
        # 没有待写入块时直接返回，避免创建空集合或发送空插入请求。
        if not documents:
            return

        # Milvus dense_embedding 字段的维度必须与嵌入模型输出长度一致。
        dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        # total 用于切片边界和进度回调。
        total = len(documents)
        # 写入前确保集合、schema 和索引存在；已存在时该操作不会重复创建。
        self.milvus_manager.init_collection(dense_dim)

        # 按批向量化和插入，防止大文档一次占满内存或超过 RPC 限制。
        for i in range(0, total, batch_size):
            # 切出当前批次，例如 i=50、batch_size=50 时取第 51 到第 100 项。
            batch = documents[i : i + batch_size]
            # 提取当前批次每个字典中的正文，保持与 batch 完全相同的顺序。
            texts = [doc["text"] for doc in batch]
            # 当前批次先一次性生成 Dense 向量，再与原文按位置配对。
            # 返回的 dense_embeddings 外层长度应与 texts、batch 的长度相同。
            dense_embeddings = self.embedding_service.get_embeddings(texts)

            # 构造 Milvus 插入行。写入 text 时，Milvus 的 BM25 Function 会自动生成 sparse_embedding。
            # 同时保留 parent/root ID，召回后才能沿血缘恢复父块。
            insert_data = [
                {
                    # 当前文本对应的密集浮点数向量。
                    "dense_embedding": dense_emb,
                    # 正文既用于展示，也作为 Milvus 自动生成 BM25 稀疏向量的输入。
                    "text": doc["text"],
                    # 以下字段用于来源展示、过滤和 Auto-merging 上卷。
                    "filename": doc["filename"],
                    "file_type": doc["file_type"],
                    "file_path": doc.get("file_path", ""),
                    "page_number": doc.get("page_number", 0),
                    "chunk_idx": doc.get("chunk_idx", 0),
                    "chunk_id": doc.get("chunk_id", ""),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": doc.get("chunk_level", 0),
                }
                # zip 按相同下标将第 i 个 doc 与第 i 个 dense_emb 配对。
                # 若两者长度不一致，zip 会按较短者截断，因此嵌入服务应保证一一对应。
                for doc, dense_emb in zip(batch, dense_embeddings)
            ]

            # 只有向量和 metadata 都组装完成后才提交这一批。
            # insert 会通过 MilvusStore 创建短连接、执行写入、再关闭连接。
            self.milvus_manager.insert(insert_data)

            if progress_callback:
                # min 处理最后一批不足 batch_size 的情况，确保 processed 不超过 total。
                processed = min(i + batch_size, total)
                # 通知调用方当前完成数量和总数量，例如 callback(50, 120)。
                progress_callback(processed, total)
