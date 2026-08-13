"""Administrator-only document upload, indexing, and deletion routes."""

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from backend.api.resources import (
    cleanup_staging_dir,
    create_staging_dir,
    delete_document_transactionally,
    ensure_upload_dir,
    ingestion_service,
    milvus_manager,
    milvus_writer,
    parent_chunk_store,
    promote_staged_document,
    save_upload_file,
    source_path_for,
)
from backend.api.upload_validation import normalize_upload_filename
from backend.db.models import User
from backend.infra.auth import require_admin
from backend.jobs.upload_jobs import DELETE_STEPS, delete_job_manager, upload_job_manager
from backend.schemas import (
    DocumentDeleteJobResponse,
    DocumentDeleteResponse,
    DocumentDeleteStartResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadJobResponse,
    DocumentUploadResponse,
    DocumentUploadStartResponse,
)


router = APIRouter(tags=["documents"])


def _prepare_progress(job_id: str, step: str, percent: int, message: str) -> None:
    status = "completed" if percent >= 100 else "running"
    upload_job_manager.update_step(job_id, step, percent, status, message)


def _process_upload_job(job_id: str, staging_dir: str, filename: str) -> None:
    """Prepare a staged upload, then replace the active document and index it."""
    failed_step = "mineru"
    try:
        upload_job_manager.complete_step(job_id, "upload", "文件已保存到暂存区")
        prepared = ingestion_service.prepare(
            Path(staging_dir) / filename,
            filename,
            source_path_for(filename),
            staging_dir,
            lambda step, percent, message: _prepare_progress(job_id, step, percent, message),
        )

        # 先验证新版本再删除旧索引，确保 MinerU 或 Markdown 处理失败不会影响线上文档。
        failed_step = "cleanup"
        upload_job_manager.update_step(job_id, "cleanup", 10, "running", "新版本已验证，正在清理旧版本")
        delete_document_transactionally(filename)
        upload_job_manager.update_step(job_id, "cleanup", 70, "running", "正在提升已验证的新版本")
        promote_staged_document(staging_dir, filename, prepared.artifact_bundle is not None)
        upload_job_manager.complete_step(job_id, "cleanup", "旧版本已替换，新版本已启用")

        # 父块先落 PostgreSQL，叶子向量随后写入 Milvus，保持 Auto-merging 所需的父子链路。
        failed_step = "parent_store"
        upload_job_manager.update_step(job_id, "parent_store", 20, "running", "正在写入父级分块")
        parent_chunk_store.upsert_documents(prepared.parent_chunks)
        upload_job_manager.complete_step(
            job_id,
            "parent_store",
            f"父级分块已入库：{len(prepared.parent_chunks)} 个",
        )

        failed_step = "vector_store"
        total_leaf = len(prepared.leaf_chunks)
        upload_job_manager.update_step(
            job_id,
            "vector_store",
            0,
            "running",
            f"正在向量化入库：0 / {total_leaf}",
            total_chunks=total_leaf,
            processed_chunks=0,
        )

        def on_vector_progress(processed: int, total: int) -> None:
            percent = round(processed * 100 / total) if total else 100
            upload_job_manager.update_step(
                job_id,
                "vector_store",
                percent,
                "running",
                f"正在向量化入库：{processed} / {total}",
                total_chunks=total,
                processed_chunks=processed,
            )

        milvus_writer.write_documents(prepared.leaf_chunks, progress_callback=on_vector_progress)
        upload_job_manager.complete_step(job_id, "vector_store", f"向量化入库完成：{total_leaf} 个叶子分块")
        upload_job_manager.complete_job(job_id, f"成功上传并处理 {filename}")
    except Exception as exc:
        upload_job_manager.fail_job(job_id, failed_step, str(exc))
    finally:
        cleanup_staging_dir(staging_dir)


def _process_delete_job(job_id: str, filename: str) -> None:
    try:
        chunks_deleted = delete_document_transactionally(filename, delete_job_manager, job_id)
        delete_job_manager.complete_job(job_id, f"已删除 {filename}，向量数据 {chunks_deleted} 条")
    except Exception as exc:
        job = delete_job_manager.get_job(job_id)
        delete_job_manager.fail_job(job_id, job.get("current_step", "prepare") if job else "prepare", str(exc))


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(_: User = Depends(require_admin)):
    try:
        milvus_manager.init_collection()
        file_stats: dict[str, dict] = {}
        for item in milvus_manager.query(output_fields=["filename", "file_type"], limit=10000):
            filename = item.get("filename", "")
            if filename not in file_stats:
                file_stats[filename] = {
                    "filename": filename,
                    "file_type": item.get("file_type", ""),
                    "chunk_count": 0,
                }
            file_stats[filename]["chunk_count"] += 1
        return DocumentListResponse(documents=[DocumentInfo(**stats) for stats in file_stats.values()])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取文档列表失败: {exc}") from exc


@router.post("/documents/upload/async", response_model=DocumentUploadStartResponse)
async def upload_document_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    try:
        filename = normalize_upload_filename(file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ensure_upload_dir()
    job = upload_job_manager.create_job(filename)
    staging_dir = create_staging_dir(job["job_id"])
    try:
        upload_job_manager.update_step(job["job_id"], "upload", 1, "running", "正在保存文件到暂存区")
        await save_upload_file(file, staging_dir / filename)
        upload_job_manager.complete_step(job["job_id"], "upload", "文件已上传，等待 MinerU 解析")
    except Exception as exc:
        cleanup_staging_dir(staging_dir)
        upload_job_manager.fail_job(job["job_id"], "upload", f"文件保存失败: {exc}")
        raise HTTPException(status_code=500, detail=f"文件保存失败: {exc}") from exc

    background_tasks.add_task(_process_upload_job, job["job_id"], str(staging_dir), filename)
    return DocumentUploadStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message="文件已上传，正在后台解析并入库",
    )


@router.get("/documents/upload/jobs/{job_id}", response_model=DocumentUploadJobResponse)
async def get_upload_job(job_id: str, _: User = Depends(require_admin)):
    job = upload_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="上传任务不存在或已过期")
    return DocumentUploadJobResponse(**job)


@router.get("/documents/upload/jobs", response_model=list[DocumentUploadJobResponse])
async def list_upload_jobs(_: User = Depends(require_admin)):
    jobs = upload_job_manager.list_jobs()
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return [DocumentUploadJobResponse(**job) for job in jobs]


@router.delete("/documents/delete/async/{filename}", response_model=DocumentDeleteStartResponse)
async def delete_document_async(
    filename: str,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
):
    try:
        filename = normalize_upload_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = delete_job_manager.create_job(
        filename,
        steps=DELETE_STEPS,
        current_step="prepare",
        message="等待删除",
        completion_step="parent_store",
    )
    delete_job_manager.update_step(job["job_id"], "prepare", 1, "running", "删除任务已提交")
    background_tasks.add_task(_process_delete_job, job["job_id"], filename)
    return DocumentDeleteStartResponse(job_id=job["job_id"], filename=filename, message=f"正在删除 {filename}")


@router.get("/documents/delete/jobs/{job_id}", response_model=DocumentDeleteJobResponse)
async def get_delete_job(job_id: str, _: User = Depends(require_admin)):
    job = delete_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="删除任务不存在或已过期")
    return DocumentDeleteJobResponse(**job)


@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(file: UploadFile = File(...), _: User = Depends(require_admin)):
    try:
        filename = normalize_upload_filename(file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ensure_upload_dir()
    staging_dir = create_staging_dir(f"sync-{uuid4().hex}")
    try:
        staged_source_path = staging_dir / filename
        await save_upload_file(file, staged_source_path)
        prepared = ingestion_service.prepare(staged_source_path, filename, source_path_for(filename), staging_dir)
        # 同步接口与异步任务遵循相同顺序，不能为了快捷跳过新版本的预校验。
        delete_document_transactionally(filename)
        promote_staged_document(staging_dir, filename, prepared.artifact_bundle is not None)
        parent_chunk_store.upsert_documents(prepared.parent_chunks)
        milvus_writer.write_documents(prepared.leaf_chunks)
        return DocumentUploadResponse(
            filename=filename,
            chunks_processed=len(prepared.leaf_chunks),
            message=(
                f"成功上传并处理 {filename}，叶子分块 {len(prepared.leaf_chunks)} 个，"
                f"父级分块 {len(prepared.parent_chunks)} 个（存入 PostgreSQL）"
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"文档上传失败: {exc}") from exc
    finally:
        cleanup_staging_dir(staging_dir)


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
async def delete_document(filename: str, _: User = Depends(require_admin)):
    try:
        filename = normalize_upload_filename(filename)
        chunks_deleted = delete_document_transactionally(filename)
        return DocumentDeleteResponse(
            filename=filename,
            chunks_deleted=chunks_deleted,
            message=f"成功删除文档 {filename} 及其解析产物",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除文档失败: {exc}") from exc
