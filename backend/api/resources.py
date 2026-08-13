import os
from pathlib import Path

from backend.indexing import (
    DocumentLoader,
    MilvusWriter,
    ParentChunkStore,
    embedding_service,
)
from backend.indexing.milvus_client import get_milvus_store

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR.parent / "data"
UPLOAD_DIR = DATA_DIR / "documents"

# 路由复用同一组 loader、父块仓库和 Milvus 资源，上传与删除不会各自创建不同状态。
loader = DocumentLoader()
parent_chunk_store = ParentChunkStore()
milvus_manager = get_milvus_store()
milvus_writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)


def delete_document_transactionally(filename: str, job_manager=None, job_id=None) -> int:
    """
    一致性且事务性地删除文档的所有关联数据（Milvus 2.5+ 新版由服务端自动维护 BM25 索引统计）。
    包含以下步骤：
    1. 初始化 Milvus 集合。
    2. 删除 Milvus 向量数据。
    3. 删除 PostgreSQL 中的 L1/L2 父级分块以及对应的 Redis 缓存。
    """
    if job_manager and job_id:
        job_manager.update_step(job_id, "prepare", 50, "running", "正在初始化 Milvus 集合")

    milvus_manager.init_collection()
    # 删除表达式使用已经规范化的服务端文件名，不能直接拼接未校验的客户端路径。
    delete_expr = f'filename == "{filename}"'

    if job_manager and job_id:
        job_manager.complete_step(job_id, "prepare", "准备完成")
        # 兼容已有前端删除步骤
        job_manager.update_step(job_id, "bm25", 100, "completed", "BM25 全文检索统计已自动同步（Milvus 服务端自动维护）")

    # 删除 Milvus 向量
    if job_manager and job_id:
        job_manager.update_step(job_id, "milvus", 20, "running", "正在物理删除 Milvus 中的向量分块")

    chunks_deleted = 0
    try:
        # 先删除 L3 向量；失败时立即停止，避免只删父块留下不可解释状态。
        result = milvus_manager.delete(delete_expr)
        chunks_deleted = result.get("delete_count", 0) if isinstance(result, dict) else 0
    except Exception as e:
        raise RuntimeError(f"删除 Milvus 向量失败: {str(e)}") from e

    if job_manager and job_id:
        job_manager.complete_step(job_id, "milvus", f"向量数据清理完成，共删除 {chunks_deleted} 条记录")

    # 删除 Postgres 中的 ParentChunk 和 Redis 缓存
    if job_manager and job_id:
        job_manager.update_step(job_id, "parent_store", 20, "running",
                                "正在清理 PostgreSQL 数据库和 Redis 中的父级分块")

    try:
        # 向量删除成功后再清理 PostgreSQL 父块及其 Redis 缓存。
        parent_chunk_store.delete_by_filename(filename)
    except Exception as e:
        raise RuntimeError(f"清理 PostgreSQL 父级分块及缓存失败: {str(e)}") from e

    if job_manager and job_id:
        job_manager.complete_step(job_id, "parent_store", "父级分块及 Redis 缓存已清空")

    return chunks_deleted


async def save_upload_file(file, file_path: Path) -> None:
    with open(file_path, "wb") as f:
        while True:
            # 上传文件按 1 MiB 分块写盘，避免一次把整个文件读入内存。
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def ensure_upload_dir() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
