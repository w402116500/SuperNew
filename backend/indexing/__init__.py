"""知识库索引层的公共导出。

其他模块可从 ``backend.indexing`` 直接导入常用的加载器、写入器、父块存储和嵌入服务，
而不需要了解它们各自所在的具体文件。
"""

# DocumentLoader：读取 PDF、Word、Excel、HTML 并生成带层级关系的文本块。
from backend.indexing.document_loader import DocumentLoader
# EmbeddingService 是嵌入服务类；embedding_service 是全进程复用的默认模型实例。
from backend.indexing.embedding import EmbeddingService, embedding_service
# MilvusWriter：将文本块向量化后批量写入 Milvus。
from backend.indexing.milvus_writer import MilvusWriter
# ParentChunkStore：将 L1/L2 父块保存到 PostgreSQL，并使用 Redis 缓存读取结果。
from backend.indexing.parent_chunk_store import ParentChunkStore

# __all__ 声明本包推荐对外使用的名称，也控制 from backend.indexing import * 的导入范围。
__all__ = [
    "DocumentLoader",
    "EmbeddingService",
    "MilvusWriter",
    "ParentChunkStore",
    "embedding_service",
]
