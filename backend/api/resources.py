import os
import shutil
from pathlib import Path

from backend.indexing import (
    DocumentLoader,
    MilvusWriter,
    ParentChunkStore,
    embedding_service,
)
from backend.indexing.ingestion import DocumentIngestionService
from backend.indexing.milvus_client import get_milvus_store
from backend.indexing.mineru_client import MineruClient

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR.parent / "data"
UPLOAD_DIR = DATA_DIR / "documents"
PARSED_ARTIFACT_DIR = DATA_DIR / "parsed"
STAGING_DIR = DATA_DIR / ".staging"

# 路由复用同一组 loader、父块仓库和 Milvus 资源，上传与删除不会各自创建不同状态。
loader = DocumentLoader()
parent_chunk_store = ParentChunkStore()
milvus_manager = get_milvus_store()
milvus_writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)
mineru_client = MineruClient()
ingestion_service = DocumentIngestionService(loader, mineru_client)


def delete_document_transactionally(
    filename: str,
    job_manager=None,
    job_id=None,
    *,
    delete_local_files: bool = True,
) -> int:
    """
    一致性且事务性地删除文档的所有关联数据（Milvus 2.5+ 新版由服务端自动维护 BM25 索引统计）。
    包含以下步骤：
    1. 初始化 Milvus 集合。
    2. 删除 Milvus 向量数据。
    3. 删除 PostgreSQL 中的 L1/L2 父级分块以及对应的 Redis 缓存。
    4. 可选地删除原始文件和 MinerU 解析产物包。
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

    if delete_local_files:
        remove_document_files(filename)

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
    for directory in (UPLOAD_DIR, PARSED_ARTIFACT_DIR, STAGING_DIR):
        os.makedirs(directory, exist_ok=True)


def source_path_for(filename: str) -> Path:
    return UPLOAD_DIR / filename


def artifact_dir_for(filename: str) -> Path:
    return PARSED_ARTIFACT_DIR / f"{filename}.mineru"


def create_staging_dir(job_id: str) -> Path:
    ensure_upload_dir()
    # 每个任务独占暂存目录，避免同名文件的并发上传互相覆盖解析结果。
    staging_dir = STAGING_DIR / job_id
    staging_dir.mkdir(parents=True, exist_ok=False)
    return staging_dir


def cleanup_staging_dir(staging_dir: str | Path) -> None:
    shutil.rmtree(staging_dir, ignore_errors=True)


def remove_document_files(filename: str) -> None:
    source_path = source_path_for(filename)
    artifact_dir = artifact_dir_for(filename)
    if source_path.exists():
        source_path.unlink()
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)


def promote_staged_document(staging_dir: str | Path, filename: str, has_artifact_bundle: bool) -> Path:
    """Move a validated staged source and optional MinerU bundle into active storage."""
    staging_root = Path(staging_dir)
    staged_source = staging_root / filename
    if not staged_source.is_file():
        raise RuntimeError(f"暂存原文件不存在: {staged_source}")

    source_path = source_path_for(filename)
    artifact_dir = artifact_dir_for(filename)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.exists():
        source_path.unlink()
    # 调用方只有在解析和分块通过后才可提升，失败任务始终只清理暂存目录。
    shutil.move(str(staged_source), str(source_path))

    if has_artifact_bundle:
        staged_artifact_dir = staging_root / "parsed"
        if not staged_artifact_dir.is_dir():
            raise RuntimeError("暂存 MinerU 产物不存在")
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_artifact_dir), str(artifact_dir))
    return source_path
