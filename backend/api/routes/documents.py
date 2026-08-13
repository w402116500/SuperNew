"""知识库文档管理 API：列出、上传、异步任务轮询与删除。

所有接口要求管理员身份。上传后会分别保存 L1/L2 父块到 PostgreSQL、保存 L3 叶子块
及其向量到 Milvus；耗时操作可通过 FastAPI BackgroundTasks 在后台执行。
"""

# os 用于创建上传文件夹。
import os

# FastAPI 路由、后台任务、依赖注入、文件上传和 HTTP 错误类型。
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

# 资源模块提供所有路由共用的文件夹、加载器、存储服务和删除事务函数。
from backend.api.resources import (
    UPLOAD_DIR,
    ensure_upload_dir,
    loader,
    milvus_manager,
    milvus_writer,
    parent_chunk_store,
    delete_document_transactionally,
    save_upload_file,
)
# 文件名白名单和路径安全校验。
from backend.api.upload_validation import normalize_upload_filename
# User 用于标注当前管理员依赖返回的用户对象。
from backend.db.models import User
# require_admin 是 FastAPI 依赖：未通过管理员鉴权的请求不会进入下面的路由函数。
from backend.infra.auth import require_admin
# 导入上传/删除各自的步骤表和进度管理器；两类任务的内存状态彼此分开保存。
from backend.jobs.upload_jobs import DELETE_STEPS, delete_job_manager, upload_job_manager
# 这些 Pydantic 模型负责校验接口返回的数据结构，并生成 OpenAPI 文档。
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

# 文档接口在 Swagger 中归入 documents 标签。
router = APIRouter(tags=["documents"])


# 后台任务负责耗时解析和向量化，HTTP 请求只需返回 job_id。
def _process_upload_job(job_id: str, file_path: str, filename: str) -> None:
    """在后台执行上传后的旧版本清理、解析、父块入库和向量入库。

    Args:
        job_id: 上传任务管理器创建的可轮询任务 ID。
        file_path: 已经成功写入磁盘的临时上传文件路径。
        filename: 已验证的纯文件名，用于索引和删除同名旧版本。
    """
    # failed_step 会在每阶段前更新，异常时可把具体失败位置展示给前端。
    # 初始设为 cleanup，因为第一个可能抛出业务异常的阶段就是清理旧版本。
    failed_step = "cleanup"
    # try 包住完整后台流程；HTTP 响应已经发出，异常只能记录到任务状态中。
    try:
        # 将上传阶段最终改为 completed；文件已经在路由函数中成功写入磁盘。
        upload_job_manager.complete_step(job_id, "upload", "文件已保存到服务器")

        # 每进入一个阶段先更新 failed_step，异常时才能准确标记故障位置。
        failed_step = "cleanup"
        # 把 cleanup 步骤置为 running，并先显示一个初始进度给轮询中的前端。
        upload_job_manager.update_step(job_id, "cleanup", 10, "running", "正在清理同名旧文档")
        # 根据 filename 删除 Milvus、PostgreSQL 和 Redis 中已有的同名索引数据。
        delete_document_transactionally(filename)
        # 旧索引清理成功后，将 cleanup 进度设为 100%。
        upload_job_manager.complete_step(job_id, "cleanup", "旧版本清理完成")

        # 之后任何异常都应归属到 parse 阶段。
        failed_step = "parse"
        # 通知前端：现在开始读取原文件并将文本切为 L1/L2/L3。
        upload_job_manager.update_step(job_id, "parse", 5, "running", "正在解析文档并执行三级分块")
        # Loader 根据文件扩展名选择解析器，返回包含三级块和 metadata 的字典列表。
        new_docs = loader.load_document(file_path, filename)
        # 空列表意味着没有提取到可用正文，后续不能生成向量。
        if not new_docs:
            # 抛出异常会跳到 except，并将 upload job 标记为失败。
            raise ValueError("文档处理失败，未能提取内容")

        # L1/L2 父块进入 PostgreSQL，不能与 L3 叶子块写入同一存储。
        # get(..., 0) 缺少层级时回退为 0；or 0 还会处理值为 None 的情况。
        parent_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)]
        # 只有 L3 叶子块生成向量并进入 Milvus。
        leaf_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3]
        # 没有 L3 时无法提供向量检索，因此视为解析失败。
        if not leaf_docs:
            # 这个错误会由外围 except 记录到 parse 步骤。
            raise ValueError("文档处理失败，未生成可检索叶子分块")
        # 将解析阶段完成消息写入任务快照，len() 用于显示真实块数量。
        upload_job_manager.complete_step(
            job_id,
            "parse",
            f"解析完成：父级分块 {len(parent_docs)} 个，叶子分块 {len(leaf_docs)} 个",
        )

        # 后续失败应显示为“父级分块入库”失败。
        failed_step = "parent_store"
        # 先将父块步骤标记为执行中。
        upload_job_manager.update_step(job_id, "parent_store", 20, "running", "正在写入父级分块")
        # upsert 会把 L1/L2 写入 PostgreSQL，并在提交成功后尝试写入 Redis 缓存。
        parent_chunk_store.upsert_documents(parent_docs)
        # 父块存储完成后，向前端报告父块实际数量。
        upload_job_manager.complete_step(job_id, "parent_store", f"父级分块已入库：{len(parent_docs)} 个")

        # 后续失败应显示为“向量化入库”失败。
        failed_step = "vector_store"
        # 保存 L3 总数，后面的进度百分比和提示文本都依赖它。
        total_leaf = len(leaf_docs)
        # L3 尚未开始处理，因此进度为 0；同时初始化任务级块计数。
        upload_job_manager.update_step(
            job_id,
            "vector_store",
            0,
            "running",
            f"正在向量化入库：0 / {total_leaf}",
            total_chunks=total_leaf,
            processed_chunks=0,
        )

        # MilvusWriter 每完成一批就回调任务管理器，轮询进度来自真实处理量。
        def _on_vector_progress(processed: int, total: int) -> None:
            # 用已完成数量除以总数换算百分比；total 为 0 时避免除零并视为完成。
            percent = round(processed * 100 / total) if total else 100
            # 将当前批次结果写入共享任务状态，供 GET /documents/upload/jobs/{job_id} 读取。
            upload_job_manager.update_step(
                job_id,
                "vector_store",
                percent,
                "running",
                f"正在向量化入库：{processed} / {total}",
                total_chunks=total,
                processed_chunks=processed,
            )

        # 为所有 L3 生成 dense embedding，并批量插入 Milvus；每批结束会调用上面的回调。
        milvus_writer.write_documents(leaf_docs, progress_callback=_on_vector_progress)
        # 写入完成后，把最后一个步骤标记为 completed。
        upload_job_manager.complete_step(job_id, "vector_store", f"向量化入库完成：{total_leaf} 个叶子分块")
        # 所有步骤成功后，将整个 job 的 status 改为 completed。
        upload_job_manager.complete_job(job_id, f"成功上传并处理 {filename}")
    # 后台线程不能把异常返回给已结束的 HTTP 请求，因此把错误写入 job 状态。
    except Exception as e:
        # str(e) 转为可序列化、可在前端显示的错误文本。
        upload_job_manager.fail_job(job_id, failed_step, str(e))


def _process_delete_job(job_id: str, filename: str) -> None:
    """在后台删除一个文件关联的 Milvus 向量、父块和缓存。

    Args:
        job_id: 删除任务管理器创建的任务 ID。
        filename: 已验证的目标文件名。
    """
    # 删除任务从 prepare 阶段开始；出错时至少能给前端一个明确的步骤名称。
    failed_step = "prepare"
    # 删除也在后台执行，因此同样需要捕获异常并更新 job，而不是向调用方抛出。
    try:
        # 依次清理 Milvus 向量、PostgreSQL 父块和 Redis 缓存，并返回删除的向量条数。
        chunks_deleted = delete_document_transactionally(filename, delete_job_manager, job_id)
        # 资源清理都成功后，将删除任务整体标记为 completed。
        delete_job_manager.complete_job(job_id, f"已删除 {filename}，向量数据 {chunks_deleted} 条")
    except Exception as e:
        # 读取当前步骤，确保错误写入最接近实际失败位置的任务步骤。
        # 读取当前任务快照，以确定错误最接近哪个删除步骤。
        job = delete_job_manager.get_job(job_id)
        # 任务可能已因进程重启丢失；这种情况下用 prepare 作为安全的默认步骤。
        current_step = job.get("current_step", "prepare") if job else "prepare"
        # 记录失败状态和错误文本，供前端轮询接口返回。
        delete_job_manager.fail_job(job_id, current_step, str(e))


@router.get("/documents", response_model=DocumentListResponse)
# 文档管理接口全部依赖 require_admin，普通用户无法枚举或修改知识库。
async def list_documents(_: User = Depends(require_admin)):
    """列出 Milvus 中已索引文档，并按文件名聚合叶子块数量。

    Returns:
        DocumentListResponse: 每个文件一项的文档摘要列表。

    Raises:
        HTTPException: 查询或初始化 Milvus 失败时返回 500。
    """
    # 列表数据来自 Milvus；发生连接或查询错误时应转换为 HTTP 500。
    try:
        # 首次使用或服务重启后，确保目标 collection 已创建并可查询。
        milvus_manager.init_collection()
        # 只读取展示列表所需的标量字段，不返回文本或向量，避免不必要的数据传输。
        results = milvus_manager.query(
            output_fields=["filename", "file_type"],
            limit=10000,
        )

        # Milvus 返回的是块记录，接口按 filename 聚合成文档级统计。
        # 字典键是文件名，值是该文件当前累计的展示信息和叶子块数量。
        file_stats = {}
        # 逐条处理 Milvus 返回的 L3 块记录。
        for item in results:
            # get() 在字段缺失时返回空字符串，避免 KeyError 中断整个列表接口。
            filename = item.get("filename", "")
            # 文件类型用于前端显示，例如 Word、PDF 或 Excel。
            file_type = item.get("file_type", "")
            # 第一次见到某个文件名时，先创建它的聚合桶。
            if filename not in file_stats:
                # 初始块数从 0 开始，下面会对当前记录加 1。
                file_stats[filename] = {
                    "filename": filename,
                    "file_type": file_type,
                    "chunk_count": 0,
                }
            # 当前 Milvus 记录代表一个 L3，因此该文件的块数加 1。
            file_stats[filename]["chunk_count"] += 1

        # 将普通字典转换为 Pydantic DocumentInfo，顺便校验返回字段。
        documents = [DocumentInfo(**stats) for stats in file_stats.values()]
        # 外层响应模型统一包住文档列表，保持 API 返回结构稳定。
        return DocumentListResponse(documents=documents)
    except Exception as e:
        # 不把原始 Python 异常直接抛出，而是转换为 FastAPI 可返回的 500 响应。
        raise HTTPException(status_code=500, detail=f"获取文档列表失败: {str(e)}")


@router.post("/documents/upload/async", response_model=DocumentUploadStartResponse)
# background_tasks 由 FastAPI 注入，用于登记“响应发送后再运行”的函数。
async def upload_document_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    """接收文件并创建后台上传任务，立即返回可轮询的 job_id。

    Args:
        background_tasks: FastAPI 请求完成后执行后台函数的队列。
        file: multipart/form-data 上传的文件。
        _: require_admin 注入的当前管理员，仅用于权限校验。

    Returns:
        DocumentUploadStartResponse: 包含任务 ID、文件名和提示消息。

    Raises:
        HTTPException: 文件名非法时返回 400，保存文件失败时返回 500。
    """
    # 文件名校验失败是客户端输入问题，应返回 400 而不是服务器错误。
    try:
        # 先验证文件名再创建路径，目录穿越不会触碰磁盘。
        filename = normalize_upload_filename(file.filename or "")
    except ValueError as exc:
        # from exc 保留原始 ValueError 异常链，方便服务端排查。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 确保 data/documents 目录存在；重复调用不会报错。
    ensure_upload_dir()
    # 先创建可轮询任务，再保存文件和提交后台处理。
    # create_job 返回的是深拷贝快照，job_id 是前端后续轮询的唯一凭据。
    job = upload_job_manager.create_job(filename)
    # Path 的 / 运算符按当前系统规则安全地拼接上传目录和已校验的纯文件名。
    file_path = UPLOAD_DIR / filename

    # 文件落盘失败时不能启动后台解析，否则后台会读取不存在或不完整的文件。
    try:
        # 将第一阶段从 pending 变为 running；1% 表示已开始但尚未写完。
        upload_job_manager.update_step(job["job_id"], "upload", 1, "running", "正在保存文件到服务器")
        # await 让出事件循环，save_upload_file 会分块读取 UploadFile 并写入 file_path。
        await save_upload_file(file, file_path)
        # 文件完整写入后，将 upload 阶段标记完成。
        upload_job_manager.complete_step(job["job_id"], "upload", "文件已上传，等待后台处理")
    except Exception as e:
        # 即使 HTTP 请求会失败，也先更新内存任务，便于已拿到任务 ID 的调用方查看错误。
        upload_job_manager.fail_job(job["job_id"], "upload", f"文件保存失败: {e}")
        # 返回 HTTP 500，表示保存服务器文件时发生意外。
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    # 文件保存成功后才排入后台队列，worker 不会读取尚未落盘的文件。
    # 注意这里传函数对象而非 _process_upload_job(...) 调用结果；FastAPI 稍后才会执行它。
    background_tasks.add_task(_process_upload_job, job["job_id"], str(file_path), filename)
    # 立即返回，不等待解析、Embedding 和 Milvus 写入完成。
    return DocumentUploadStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message="文件已上传，正在后台解析和向量化入库",
    )


@router.get("/documents/upload/jobs/{job_id}", response_model=DocumentUploadJobResponse)
# job_id 来自上传起始接口的响应；管理员才能读取该任务进度。
async def get_upload_job(job_id: str, _: User = Depends(require_admin)):
    """查询一个异步上传任务的当前进度快照。"""
    # get_job 返回深拷贝，路由层不能直接修改管理器内部的 _jobs 字典。
    job = upload_job_manager.get_job(job_id)
    # 任务不存在或进程重启丢失内存状态时明确返回 404。
    if not job:
        # 404 表示该 ID 当前无法在本进程的内存任务字典中找到。
        raise HTTPException(status_code=404, detail="上传任务不存在或已过期")
    # 用响应模型序列化任务及其 steps 数组。
    return DocumentUploadJobResponse(**job)


@router.get("/documents/upload/jobs", response_model=list[DocumentUploadJobResponse])
# 该接口用于管理端查看当前 Python 进程仍保存的全部上传任务。
async def list_upload_jobs(_: User = Depends(require_admin)):
    """列出当前进程内所有上传任务，按创建时间从新到旧排序。"""
    # list_jobs 会返回每个任务的深拷贝快照。
    jobs = upload_job_manager.list_jobs()
    # ISO 8601 UTC 字符串按字典序即可按时间排序；reverse=True 让最新任务排前面。
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    # 为列表中的每个字典创建一个经过校验的响应对象。
    return [DocumentUploadJobResponse(**job) for job in jobs]


@router.delete("/documents/delete/async/{filename}", response_model=DocumentDeleteStartResponse)
# filename 是 URL 路径参数；当前实现应与上传入口一样在后续演进中统一做文件名规范化。
async def delete_document_async(
    filename: str,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
):
    """创建后台删除任务，立即返回可轮询的删除 job_id。"""
    # 为删除流程创建专用任务；删除步骤和上传步骤不同，因此显式传入 DELETE_STEPS。
    job = delete_job_manager.create_job(
        filename,
        steps=DELETE_STEPS,
        current_step="prepare",
        message="等待删除",
        completion_step="parent_store",
    )
    # 先更新 prepare，使前端刚收到 job_id 时也能看到任务已开始。
    delete_job_manager.update_step(job["job_id"], "prepare", 1, "running", "删除任务已提交")
    # 登记后台删除函数；接口返回后才会执行实际的数据清理。
    background_tasks.add_task(_process_delete_job, job["job_id"], filename)
    # 立即返回删除任务 ID，而不等待 Milvus 和数据库删除完成。
    return DocumentDeleteStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message=f"正在删除 {filename}",
    )


@router.get("/documents/delete/jobs/{job_id}", response_model=DocumentDeleteJobResponse)
# 删除任务与上传任务使用不同管理器，因此也需要单独的查询入口。
async def get_delete_job(job_id: str, _: User = Depends(require_admin)):
    """查询一个异步删除任务的当前进度快照。"""
    # 从 delete_job_manager 的进程内字典读取任务状态。
    job = delete_job_manager.get_job(job_id)
    # 内存中不存在该任务时，不能返回一个伪造的成功状态。
    if not job:
        # 进程重启、错误 job_id 或任务未来被清理时都会进入这里。
        raise HTTPException(status_code=404, detail="删除任务不存在或已过期")
    # 通过 Pydantic 模型返回任务状态和删除步骤数组。
    return DocumentDeleteJobResponse(**job)


@router.post("/documents/upload", response_model=DocumentUploadResponse)
# 同步入口复用相同校验和存储边界，只是当前请求会等待全部处理完成。
# 这个函数适合调用方愿意等待完整入库结果的场景，不返回 job_id。
async def upload_document(file: UploadFile = File(...), _: User = Depends(require_admin)):
    """同步上传并完成文档解析、父块保存和 L3 向量入库。

    与异步入口使用相同的安全校验和存储边界，不同点是 HTTP 响应会等待全部处理完成。
    """
    # 外层 try 统一处理保存、解析、数据库和 Milvus 的未预期异常。
    try:
        # 内层 try 专门把非法文件名映射为 HTTP 400。
        try:
            # file.filename 可能为 None，因此使用空字符串作为校验函数的安全输入。
            filename = normalize_upload_filename(file.filename or "")
        except ValueError as exc:
            # 保留 400，让客户端知道需要修改文件名或扩展名。
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 创建上传目录，后面的 open() 才能成功创建目标文件。
        ensure_upload_dir()

        # 清理同名旧文档以保证一致性。
        # 写入新版本前先清理同名旧数据，避免新旧分块同时被召回。
        # 注意：这不是跨多存储的原子事务；新版本后续失败时旧索引已经被删除。
        delete_document_transactionally(filename)

        # 计算服务器磁盘上的最终文件路径。
        file_path = UPLOAD_DIR / filename
        # wb 表示以二进制写入模式打开文件，存在同名文件时会覆盖它。
        with open(file_path, "wb") as f:
            # 同步接口一次性读取整个上传内容；大文件会占用较多内存。
            content = await file.read()
            # 将读取到的 bytes 写入服务器磁盘。
            f.write(content)

        # 解析器可能因损坏文件或不支持的内容失败，需要给出更明确的错误提示。
        try:
            # str(file_path) 将 pathlib.Path 转为 Loader 需要的字符串路径。
            new_docs = loader.load_document(str(file_path), filename)
        except Exception as doc_err:
            # 将底层解析异常转换为 HTTP 500 响应。
            raise HTTPException(status_code=500, detail=f"文档处理失败: {doc_err}")

        # 空列表表示没有可入库的内容，不能继续写入存储。
        if not new_docs:
            # 明确告诉客户端失败原因，而不是返回看似成功的空文档。
            raise HTTPException(status_code=500, detail="文档处理失败，未能提取内容")

        # 推导 L1/L2 父块列表，供 PostgreSQL 与 Redis 使用。
        parent_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)]
        # 推导 L3 叶子块列表，只有它们会生成向量并进入 Milvus。
        leaf_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3]
        # 没有检索叶子块时，知识库无法回答与此文件有关的问题。
        if not leaf_docs:
            # 返回 500，因为这是服务端解析结果不符合入库要求。
            raise HTTPException(status_code=500, detail="文档处理失败，未生成可检索叶子分块")

        # 父块先持久化，随后写入的 L3 才能在召回时找到父级正文。
        # 此方法会执行 PostgreSQL upsert，并在成功后尝试刷新 Redis 缓存。
        parent_chunk_store.upsert_documents(parent_docs)
        # Milvus 只接收叶子块，返回的 chunks_processed 也按叶子数量计算。
        # 写入器会调用 embedding 服务为每个 L3 生成 dense vector。
        milvus_writer.write_documents(leaf_docs)

        # 同步流程到此成功，直接向客户端返回文件名、叶子块数量和说明文本。
        return DocumentUploadResponse(
            filename=filename,
            chunks_processed=len(leaf_docs),
            message=(
                f"成功上传并处理 {filename}，叶子分块 {len(leaf_docs)} 个，"
                f"父级分块 {len(parent_docs)} 个（存入 PostgreSQL）"
            ),
        )
    # 已经带有准确状态码的 HTTPException 要原样抛出，不能被通用 500 覆盖。
    except HTTPException:
        # 重新抛出，FastAPI 会按异常中已有的 status_code 构造响应。
        raise
    except Exception as e:
        # 其他未预期错误统一映射为 500，并保留原错误文本帮助调试。
        raise HTTPException(status_code=500, detail=f"文档上传失败: {str(e)}")


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
# 这是同步删除入口：调用方会一直等待到三个存储的删除流程结束。
async def delete_document(filename: str, _: User = Depends(require_admin)):
    """同步删除指定文件的 Milvus 向量、父级分块和 Redis 缓存。

    本地上传文件不会被此接口删除，便于保留原始文件或后续重新导入。
    """
    # 删除存储数据时可能发生 Milvus、PostgreSQL 或 Redis 连接异常。
    try:
        # 按文件名删除关联索引，并接收 Milvus 返回的 L3 删除数量。
        chunks_deleted = delete_document_transactionally(filename)

        # 返回给客户端的结果不包含原文件内容，只包含删除统计和展示消息。
        return DocumentDeleteResponse(
            filename=filename,
            chunks_deleted=chunks_deleted,
            message=f"成功删除文档 {filename} 的向量数据（本地文件已保留）",
        )
    except Exception as e:
        # 将任意删除失败转换为 HTTP 500，使调用方明确知道操作没有完全成功。
        raise HTTPException(status_code=500, detail=f"删除文档失败: {str(e)}")
