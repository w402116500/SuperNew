# `virtual_legal_knowledge_base_test.docx` 快速入库执行记录

> 目的：面向初学者说明一次 Word 文档如何从前端“快速入库”进入本地磁盘、PostgreSQL、Redis 和 Milvus，并在后续 RAG 检索中被使用。
>
> 文档性质：上传的 Word 内容为虚构法律测试材料，不可作为真实法律意见。
>
> 时间说明：本文记录的是本地 `http://127.0.0.1:8050` 服务的一次真实执行。账号密码、JWT 和 LangSmith API Key 均未写入本文。

## 1. 本次实测结果

### 1.1 输入文件

| 项目 | 实测值 |
| --- | --- |
| 源文件 | `output/doc/virtual_legal_knowledge_base_test.docx` |
| 文件大小 | `38,905` bytes |
| 服务端原文件 | `data/documents/virtual_legal_knowledge_base_test.docx` |
| 服务端保存方式 | `save_upload_file()` 每次读取 1 MiB，再写入本地磁盘 |
| 文件类型 | `Word` |

### 1.2 异步任务结果

最终用于确认的上传任务：`be2623ce0c9644049d4129e38c74cb89`

| 阶段 key | 前端标签 | 最终状态 | 实测消息 |
| --- | --- | --- | --- |
| `upload` | 文档上传 | `completed` | 文件已保存到服务器 |
| `cleanup` | 清理旧版本 | `completed` | 旧版本清理完成 |
| `parse` | 解析与分块 | `completed` | 解析完成：父级分块 2 个，叶子分块 3 个 |
| `parent_store` | 父级分块入库 | `completed` | 父级分块已入库：2 个 |
| `vector_store` | 向量化入库 | `completed` | 向量化入库完成：3 个叶子分块 |

任务最终状态为 `completed`，最终消息为：`成功上传并处理 virtual_legal_knowledge_base_test.docx`。

### 1.3 最终可见性验证

上传完成后的 `GET /documents` 返回了以下文档摘要：

```json
{
  "filename": "virtual_legal_knowledge_base_test.docx",
  "file_type": "Word",
  "chunk_count": 3,
  "uploaded_at": null
}
```

这里的 `chunk_count=3` 是 Milvus 中按文件名聚合后的 L3 叶子块数。`uploaded_at` 为 `null`，因为当前文档列表接口没有从任何持久化字段读取上传时间。

### 1.4 本次请求、函数与运行参数

这一节列出“实际传了什么”和“代码以什么参数运行”。密钥、密码和 JWT 已替换为 `<redacted>`。

#### HTTP 请求参数

```http
POST http://127.0.0.1:8050/documents/upload/async
Authorization: Bearer <redacted>
Content-Type: multipart/form-data; boundary=<browser/generated>

--<boundary>
Content-Disposition: form-data; name="file";
  filename="virtual_legal_knowledge_base_test.docx"
Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document

<38,905 bytes Word binary>
--<boundary>--
```

后端返回的关键响应参数：

```json
{
  "job_id": "be2623ce0c9644049d4129e38c74cb89",
  "filename": "virtual_legal_knowledge_base_test.docx",
  "message": "文件已上传，正在后台解析和向量化入库"
}
```

轮询请求没有 request body，只将 `job_id` 放在 URL 路径中：

```http
GET /documents/upload/jobs/be2623ce0c9644049d4129e38c74cb89
Authorization: Bearer <redacted>
```

前端轮询间隔为 `1000 ms`。前端上传进度来自浏览器 `onUploadProgress`，后端解析和向量化进度来自任务接口返回的 `steps`。

#### 后端函数参数

| 函数 | 本次实参/默认参数 | 作用 |
| --- | --- | --- |
| `upload_document_async(background_tasks, file, _)` | `file=<UploadFile: virtual_legal_knowledge_base_test.docx>`；`_` 是管理员鉴权结果 | 接收 multipart 文件并创建任务 |
| `_process_upload_job(job_id, file_path, filename)` | job ID 如上；`file_path=data/documents/virtual_legal_knowledge_base_test.docx`；文件名如上 | 后台执行真正的入库 |
| `DocumentLoader(chunk_size=800, chunk_overlap=100)` | 使用默认值 | 创建三级切分器 |
| `ParentChunkStore.upsert_documents(docs)` | `docs` 是 2 条 L1/L2 字典 | 写 PostgreSQL 并尝试更新 Redis |
| `MilvusWriter.write_documents(documents, batch_size=50, progress_callback=...)` | `documents` 是 3 条 L3 字典；单批最多 50 条 | 生成向量、写 Milvus、更新任务进度 |
| `EmbeddingService.get_embeddings(texts)` | `texts` 长度为 3 | 生成与 L3 一一对应的 Dense 向量 |

#### 实际环境配置参数

| 参数 | 本次值 | 对链路的影响 |
| --- | --- | --- |
| `DENSE_EMBEDDING_DIM` | `1024` | Milvus `dense_embedding` 的向量维度必须为 1024 |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 本地 Hugging Face 嵌入模型 |
| `EMBEDDING_DEVICE` | `cpu` | 向量推理运行在 CPU，不使用 GPU |
| `MILVUS_HOST` | `127.0.0.1` | 后端连接 Milvus 的主机 |
| `MILVUS_PORT` | `19531` | 后端连接 Milvus 的宿主机端口 |
| `MILVUS_COLLECTION` | `tutorial_verify_embeddings` | 本次 3 条 L3 数据所在集合 |
| `MILVUS_TIMEOUT` | `30` 秒 | 单次 Milvus 客户端操作超时 |
| `REDIS_URL` | `redis://127.0.0.1:16379/0` | 父块缓存所用 Redis 连接和 DB 0 |
| `REDIS_KEY_PREFIX` | 未配置，使用默认 `keyan_zhushou` | 缓存键以 `keyan_zhushou:` 开头 |
| `REDIS_CACHE_TTL_SECONDS` | 未配置，使用默认 `300` 秒 | 父块缓存五分钟后过期 |
| `DATABASE_URL` | 主机 `127.0.0.1`，端口 `15432`，库 `langchain_app` | `parent_chunks` 和 `users` 的 PostgreSQL 位置 |
| `JWT_EXPIRE_MINUTES` | `1440` 分钟 | 本次临时管理员 JWT 的有效期 |
| `LANGSMITH_TRACING` | `true` | 聊天 Agent 可追踪；本入库链路未显式 trace |
| `LANGSMITH_PROJECT` | `SuperNew` | LangSmith 项目名 |

`UPLOAD_MAX_FILE_SIZE` 没有配置，且上传路由没有额外的文件大小检查；当前只按 1 MiB 分片写盘。这意味着反向代理、FastAPI 部署参数或磁盘容量才是实际文件大小上限。

## 2. 按真实执行顺序展开

下面按一次点击“开始上传”后的真实先后顺序说明。每一步都写出执行位置、输入、输出和状态变化；本次实际文件是 `virtual_legal_knowledge_base_test.docx`。

### 第 0 步：服务启动时预先创建共享对象

**执行位置**：`backend/app.py` -> `backend/api/resources.py`

1. 应用入口调用 `load_env()`，把 `.env` 读入当前 Python 进程。
2. 导入 `backend.api.router` 时，文档路由会导入 `backend.api.resources`。
3. `resources.py` 创建并复用以下对象：`DocumentLoader()`、`ParentChunkStore()`、`MilvusStore`、`EmbeddingService`、`MilvusWriter`。
4. `EmbeddingService` 初始化本地 `BAAI/bge-m3` 模型，运行设备为 CPU。

这一步只发生在后端进程启动或模块首次导入时，不会在每次点击上传时重新加载模型。

### 第 1 步：用户在前端选择 Word 文件

**执行位置**：`frontend/src/components/Documents/UploadSection.vue`

1. 用户点击上传区域或将文件拖入区域。
2. 浏览器从 `<input type="file">` 或 `DragEvent.dataTransfer.files` 得到一个 `File` 对象。
3. `setSelectedFile(file)` 执行以下状态赋值：

```ts
documentStore.selectedFile = file;
documentStore.uploadProgress = '';
documentStore.uploadSteps = documentStore.createUploadSteps();
documentStore.uploadProgressCollapsed = false;
documentStore.activeUploadJobId = '';
```

**本次输入值**：

```text
name = virtual_legal_knowledge_base_test.docx
size = 38905 bytes
browser MIME type = application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

此时文件还只存在于浏览器内存中，尚未发送到后端。

### 第 2 步：点击“开始上传”，Pinia 创建 `FormData`

**执行位置**：`frontend/src/stores/documents.ts` 的 `uploadDocument()`

1. 检查 `selectedFile` 是否存在；不存在则直接抛出“请先选择文件”。
2. 设置前端状态：`isUploading=true`，第一步 `upload` 变为 `running`。
3. 创建浏览器原生 multipart 容器：

```ts
const formData = new FormData();
formData.append('file', this.selectedFile);
```

4. 使用 Axios 发起 `POST /documents/upload/async`。
5. 上传字节传输期间，浏览器多次调用 `onUploadProgress`，前端只更新“文档上传”这个步骤的百分比。

### 第 3 步：FastAPI 先完成管理员鉴权

**执行位置**：`backend/api/routes/documents.py` 的 `upload_document_async()`

请求先进入 `require_admin` 依赖：

1. 从 `Authorization: Bearer <JWT>` 读取令牌。
2. 验证 JWT 签名和过期时间。
3. 根据 JWT 的 `sub` 到 PostgreSQL 的 `users` 表查询用户。
4. 检查 `users.role == "admin"`；否则返回 HTTP 403。

本次请求使用临时管理员账号通过鉴权。鉴权通过后，路由函数才会接收 `UploadFile`。

### 第 4 步：校验文件名并创建内存任务

**执行位置**：`upload_document_async(background_tasks, file, _)`

1. 调用 `normalize_upload_filename(file.filename)`。
2. 校验规则：非空、不能含 `/`、`\\`、双引号、控制字符；扩展名必须位于白名单。
3. 本次 `.docx` 通过校验，得到安全文件名：`virtual_legal_knowledge_base_test.docx`。
4. 调用 `ensure_upload_dir()`，确保 `data/documents/` 存在。
5. 调用 `upload_job_manager.create_job(filename)`，在当前 Python 进程的 `_jobs` 字典新增任务。

新任务的重要初值：

```text
status = pending
current_step = upload
total_chunks = 0
processed_chunks = 0
steps = [upload, cleanup, parse, parent_store, vector_store]
```

本次最终的任务 ID 是：`be2623ce0c9644049d4129e38c74cb89`。

### 第 5 步：把浏览器上传流写入服务器磁盘

**执行位置**：`backend/api/resources.py` 的 `save_upload_file()`

1. 后端目标路径被计算为：`data/documents/virtual_legal_knowledge_base_test.docx`。
2. 循环执行 `await file.read(1024 * 1024)`，每次最多读取 1 MiB。
3. 将每块二进制数据写入目标文件，直到读取到空字节串。
4. 本次文件小于 1 MiB，因此实际只需一次读取和写入。
5. 任务步骤 `upload` 更新为 `completed`，消息为“文件已保存到服务器”。

此时原始 Word 文件已经落在服务器磁盘，但还没有被切分、向量化或写入数据库。

### 第 6 步：接口立即返回 `job_id`，前端开始轮询

**执行位置**：`upload_document_async()` 返回；`startUploadJobPolling(jobId)` 开始运行

1. 后端调用 `background_tasks.add_task(_process_upload_job, job_id, file_path, filename)`。
2. 后端立刻返回 HTTP 200 和 `job_id`，不会等待耗时的解析与 embedding。
3. 前端收到响应后保存 `activeUploadJobId`。
4. 前端立刻执行一次轮询，然后每隔 `1000 ms` 再执行：

```text
GET /documents/upload/jobs/be2623ce0c9644049d4129e38c74cb89
```

5. 每次响应中的 `steps` 会覆盖前端当前进度条数据。

### 第 7 步：后台任务清理同名旧版本

**执行位置**：`_process_upload_job(job_id, file_path, filename)`

后台任务开始后，第一项真正的数据操作不是解析，而是：

```python
delete_document_transactionally("virtual_legal_knowledge_base_test.docx")
```

依次执行：

1. `milvus_manager.init_collection()` 确保 collection 已存在。
2. Milvus 执行过滤删除：`filename == "virtual_legal_knowledge_base_test.docx"`。
3. PostgreSQL 删除：`parent_chunks` 中同名文件的所有 L1/L2 行。
4. Redis 删除这些父块对应的缓存键。
5. `cleanup` 步骤变为 `completed`。

本次由于前面已经有一次同名验证上传，第二次上传确实执行了“先清理旧版本，再写新版本”。

### 第 8 步：读取 Word 并生成标准文档对象

**执行位置**：`DocumentLoader.load_document(file_path, filename)`

1. 通过扩展名 `.docx` 选择 `Docx2txtLoader(file_path)`。
2. Loader 把 Word 内容读取成 LangChain `Document` 列表。
3. `_load_from_langchain_docs()` 为每个文档单元构造基础 metadata：

```python
{
  "filename": "virtual_legal_knowledge_base_test.docx",
  "file_path": "data/documents/virtual_legal_knowledge_base_test.docx",
  "file_type": "Word",
  "page_number": 0,
}
```

4. 调用 `sanitize_text()` 清理文本后交给三级切分函数。

### 第 9 步：从原文依次生成 L1、L2、L3

**执行位置**：`DocumentLoader._split_page_to_three_levels()`

处理顺序不是“把全文平铺切三次”，而是严格嵌套：

```text
原 Word 文本
  -> 先切 L1
     -> 在每一个 L1 内切 L2
        -> 在每一个 L2 内切 L3
```

本次默认参数的实际派生值：

```text
输入 chunk_size = 800
输入 chunk_overlap = 100
L1 chunk_size = 2400，overlap = 400
L2 chunk_size = 1600，overlap = 200
L3 chunk_size = 800，overlap = 100
```

本次输出按生成顺序为：

| 生成序号 | 块 ID | 层级 | 直接父块 | 根块 | 后续去向 |
| ---: | --- | ---: | --- | --- | --- |
| 0 | `...::p0::l1::0` | L1 | 空字符串 | 自身 | PostgreSQL + Redis |
| 1 | `...::p0::l2::0` | L2 | `...::p0::l1::0` | `...::p0::l1::0` | PostgreSQL + Redis |
| 2 | `...::p0::l3::0` | L3 | `...::p0::l2::0` | `...::p0::l1::0` | Milvus |
| 3 | `...::p0::l3::1` | L3 | `...::p0::l2::0` | `...::p0::l1::0` | Milvus |
| 4 | `...::p0::l3::2` | L3 | `...::p0::l2::0` | `...::p0::l1::0` | Milvus |

任务步骤 `parse` 更新为 `completed`，实际消息是“父级分块 2 个，叶子分块 3 个”。

### 第 10 步：L1/L2 事务性写入 PostgreSQL

**执行位置**：`ParentChunkStore.upsert_documents(parent_docs)`

1. 后台任务从全部块中筛选 `chunk_level in (1, 2)`，得到 2 条 `parent_docs`。
2. 创建一个 SQLAlchemy `SessionLocal()` 会话。
3. 对每条块按 `chunk_id` 查询是否已存在。
4. 已存在则逐字段更新；不存在则执行 `INSERT`。
5. 两条数据在同一个 `db.commit()` 中提交；任一条失败会 `rollback()`。
6. 任务步骤 `parent_store` 更新为 `completed`。

本次真正落库的两条记录与第 7 章的实际 PostgreSQL 查询结果一致。

### 第 11 步：PostgreSQL 提交成功后，尝试写 Redis 缓存

**执行位置**：`ParentChunkStore.upsert_documents()` 的 commit 之后

1. 只有 PostgreSQL `commit` 成功后，才循环调用 `cache.set_json()`。
2. 缓存 key 由 `_cache_key(chunk_id)` 生成：`parent_chunk:<chunk_id>`。
3. `RedisCache` 再加上前缀 `keyan_zhushou:`，所以最终 key 是 `keyan_zhushou:parent_chunk:<chunk_id>`。
4. 使用 Redis `SETEX` 写 JSON，默认 TTL 为 `300` 秒。
5. Redis 不可用时，`set_json()` 吞掉异常；任务仍可能继续成功，因为 PostgreSQL 是事实来源。

### 第 12 步：L3 文本生成 1024 维 Dense 向量

**执行位置**：`MilvusWriter.write_documents(leaf_docs, batch_size=50, progress_callback=...)`

1. 后台任务筛选 `chunk_level == 3`，得到 3 条 `leaf_docs`。
2. 因 `batch_size=50`，3 条文本放入同一个 batch。
3. 提取 `texts = [doc["text"] for doc in batch]`。
4. 调用 `EmbeddingService.get_embeddings(texts)`。
5. `HuggingFaceEmbeddings.embed_documents(texts)` 使用 `BAAI/bge-m3`、CPU，生成 3 个长度为 1024 的浮点数组。
6. 向量已启用归一化，因此后续 HNSW 使用内积相似度。

向量数据是大量浮点数，本文不打印具体 1024 个数；它们保存在 Milvus 的 `dense_embedding` 字段。

### 第 13 步：L3 与元数据批量写入 Milvus

**执行位置**：`MilvusWriter.write_documents()` -> `MilvusStore.insert()`

每个 L3 块组装成如下插入行：

```python
{
  "dense_embedding": [1024 个 float],
  "text": "当前 L3 正文",
  "filename": "virtual_legal_knowledge_base_test.docx",
  "file_type": "Word",
  "file_path": "data/documents/virtual_legal_knowledge_base_test.docx",
  "page_number": 0,
  "chunk_idx": 2 或 3 或 4,
  "chunk_id": "...::p0::l3::0/1/2",
  "parent_chunk_id": "...::p0::l2::0",
  "root_chunk_id": "...::p0::l1::0",
  "chunk_level": 3,
}
```

插入目标 collection 是 `tutorial_verify_embeddings`。Milvus 为每条记录自动生成 `id` 主键。

### 第 14 步：Milvus 自动生成 BM25 稀疏索引

**执行位置**：Milvus collection 中定义的 `FunctionType.BM25`

1. 插入行中的 `text` 写入 collection。
2. Milvus 内置 BM25 Function 消费 `text`。
3. Milvus 生成 `sparse_embedding`，不需要 Python 再发起一个分词或稀疏向量请求。
4. collection 同时拥有 HNSW Dense 索引和 `SPARSE_INVERTED_INDEX` BM25 索引。

### 第 15 步：后台任务标记完成

**执行位置**：`_process_upload_job()` 的最后三行

1. `progress_callback(3, 3)` 将 `vector_store` 进度更新为 100%。
2. `complete_step(job_id, "vector_store", ...)` 把第五阶段标记为 `completed`。
3. `complete_job(job_id, ...)` 把整个任务 `status` 设置为 `completed`。
4. 轮询接口返回这个最终任务快照。

### 第 16 步：前端停止轮询并刷新列表

**执行位置**：`startUploadJobPolling()` 的成功分支

1. 前端下一次轮询读到 `job.status === "completed"`。
2. 调用 `stopUploadJobPolling()` 清除 `setInterval`。
3. 设置 `isUploading=false`，并将 `selectedFile=null`。
4. 调用 `loadDocuments()`。
5. `GET /documents` 通过 Milvus 标量查询读取 `filename/file_type`，按文件名聚合 L3 行数。
6. 本次最终显示为 `virtual_legal_knowledge_base_test.docx / Word / 3`。

### 第 17 步：后续聊天检索时如何回到这些数据

这一步不属于“上传完成”本身，但说明为什么要保存父子关系：

```text
用户问题
  -> 用同一 embedding 模型生成查询向量
  -> Milvus Dense + BM25 混合召回 L3
  -> 命中记录给出 parent_chunk_id/root_chunk_id
  -> ParentChunkStore 先查 Redis，未命中再查 PostgreSQL
  -> 取回 L2/L1 更完整文本
  -> RAG/Agent 基于证据回答
```

因此，Milvus 中的 L3 是“找到哪里”，PostgreSQL/Redis 中的 L1/L2 是“把上下文扩展到多大”。

## 3. 一张完整链路图

```text
浏览器 UploadSection.vue
    |
    | 选择/拖放 .docx 文件
    v
Pinia documentStore.uploadDocument()
    |
    | POST /documents/upload/async
    | Content-Type: multipart/form-data
    v
FastAPI upload_document_async()
    |
    | 1. 管理员鉴权
    | 2. 校验文件名和扩展名
    | 3. 保存 data/documents/<filename>
    | 4. 创建内存 UploadJob，立刻返回 job_id
    v
FastAPI BackgroundTasks
    |
    v
_process_upload_job(job_id, file_path, filename)
    |
    | A. 删除同名旧数据：Milvus + PostgreSQL + Redis
    | B. Docx2txtLoader 读取 Word
    | C. DocumentLoader 生成 L1 -> L2 -> L3 分块树
    | D. L1/L2 upsert 到 PostgreSQL parent_chunks
    | E. L1/L2 副本写 Redis，带 TTL
    | F. L3 调用本地 HuggingFace embedding
    | G. L3 + dense vector + 元数据写入 Milvus
    | H. Milvus 基于 text 自动生成 BM25 sparse vector
    v
UploadJobManager 更新内存任务状态
    |
    | GET /documents/upload/jobs/<job_id>，前端每秒轮询
    v
前端显示五阶段进度；成功后 GET /documents 刷新列表
```

## 4. 前端执行过程

### 3.1 选择文件

入口组件是 `frontend/src/components/Documents/UploadSection.vue`。

1. 隐藏的 `<input type="file">` 接受 `.pdf/.doc/.docx/.xls/.xlsx/.html/.htm`。
2. 用户点击上传区域或拖入文件。
3. `setSelectedFile(file)` 把浏览器的 `File` 对象保存到 Pinia 的 `documentStore.selectedFile`。
4. 点击“开始上传”后调用 `documentStore.uploadDocument()`。

`accept` 属性只负责浏览器层面的选择提示，不能作为安全边界；后端仍会重新验证文件名和扩展名。

### 3.2 发起 HTTP 上传

`frontend/src/stores/documents.ts` 中的 `uploadDocument()` 做了以下事：

```ts
const formData = new FormData();
formData.append('file', this.selectedFile);
await api.post('/documents/upload/async', formData, {
  headers: { 'Content-Type': 'multipart/form-data' },
  onUploadProgress: ...,
});
```

浏览器上传字节时，`onUploadProgress` 更新第一个阶段 `upload` 的百分比。后端返回 `job_id` 后，前端不会等待解析和向量化完成，而是开始轮询：

```text
每 1 秒 GET /documents/upload/jobs/<job_id>
    -> 用服务端 steps 覆盖本地步骤状态
    -> status=completed：停止轮询、清空 selectedFile、刷新文档列表
    -> status=failed：停止轮询、保留失败信息
```

## 5. 后端 API 与任务状态

### 4.1 `POST /documents/upload/async`

实现文件：`backend/api/routes/documents.py`。

接口要求 `require_admin`，因此普通用户即使知道 URL 也不能上传。

执行顺序如下：

1. `normalize_upload_filename()` 拒绝空文件名、路径分隔符、控制字符和不在白名单中的扩展名。
2. `ensure_upload_dir()` 创建 `data/documents/`。
3. `upload_job_manager.create_job(filename)` 创建任务对象。
4. `save_upload_file(file, file_path)` 将上传流写到 `data/documents/<filename>`。
5. `background_tasks.add_task(_process_upload_job, ...)` 注册后台处理函数。
6. HTTP 立刻返回 `{ job_id, filename, message }`。

### 4.2 `UploadJobManager` 的数据在哪里

任务状态目前仅保存在 Python 进程内存的字典 `_jobs` 中，不会写入 PostgreSQL、Redis 或 Milvus。

一个任务对象的字段为：

| 字段 | 含义 |
| --- | --- |
| `job_id` | 32 位十六进制任务 ID |
| `filename` | 当前上传文件名 |
| `status` | `pending`、`running`、`completed` 或 `failed` |
| `current_step` | 当前阶段 key，例如 `vector_store` |
| `message` | 当前阶段给前端展示的文本 |
| `completion_step` | 成功时应完成的阶段，本上传流程是 `vector_store` |
| `total_chunks` | L3 叶子块总数 |
| `processed_chunks` | 已向量化/写入的 L3 块数 |
| `error` | 失败时的错误文本，成功时为 `null` |
| `created_at` / `updated_at` | UTC ISO 8601 时间 |
| `steps` | 五个阶段的数组，每项含 `key`、`label`、`percent`、`status`、`message` |

这解释了为什么后端重启后，`GET /documents/upload/jobs/<job_id>` 可能返回 404：文档数据可能仍在数据库中，但“任务进度”已随进程内存消失。

## 6. 清理旧版本

后台任务第一步会按同名文件执行：

```text
delete_document_transactionally(filename)
    -> Milvus: delete filename == "<filename>"
    -> PostgreSQL: DELETE FROM parent_chunks WHERE filename = <filename>
    -> Redis: 删除对应父块缓存键
```

这样重传同名文件不会让旧块和新块同时被检索到。

本次为了验证链路，对同一文件发起过两次同名上传；第二次上传先清理第一次的结果，最终数据库保持的是一份最新版本，不是两份重复记录。

重要限制：这是“先删旧版本，再处理新版本”的顺序。若新文件在解析、向量化或 Milvus 写入时失败，旧版本已经被删除。这是当前快速入库链路需要优先改进的可靠性风险。

## 7. Word 解析与三级分块

### 6.1 文件解析

该文件扩展名是 `.docx`，`DocumentLoader.load_document()` 选择 `Docx2txtLoader`，将 Word 内容转换为 LangChain `Document` 列表。

随后 `_load_from_langchain_docs()` 为每个解析单元补充基础元数据：

```python
{
  "filename": "virtual_legal_knowledge_base_test.docx",
  "file_path": ".../data/documents/virtual_legal_knowledge_base_test.docx",
  "file_type": "Word",
  "page_number": 0
}
```

Word 没有可靠的 PDF 页码概念，因此本次记录中的 `page_number` 是 `0`。

### 6.2 三层结构

默认分块参数是 `chunk_size=800`、`chunk_overlap=100`，实际派生为：

| 层级 | 目标大小 | 重叠大小 | 用途 |
| --- | ---: | ---: | --- |
| L1 | 至少 2000 字符 | 至少 400 字符 | 保留完整大上下文，作为根块 |
| L2 | 至少 1000 字符 | 至少 200 字符 | 中间父块 |
| L3 | 至少 800 字符 | 至少 100 字符 | 最小检索单元，向量化并写入 Milvus |

本次 Word 被拆成：

```text
L1: virtual_legal_knowledge_base_test.docx::p0::l1::0
 |
 +-- L2: virtual_legal_knowledge_base_test.docx::p0::l2::0
       |
       +-- L3: ...::p0::l3::0
       +-- L3: ...::p0::l3::1
       +-- L3: ...::p0::l3::2
```

`chunk_id` 是由“文件名、页码、层级、该层序号”组合得到的稳定 ID。相同文件内容重传时，它仍能定位相同的父块身份。

## 8. PostgreSQL：父块事实数据

### 7.1 表和字段

表名：`parent_chunks`。只保存 L1/L2 父块，不保存 L3 叶子块。

| 列 | 类型/约束 | 作用 |
| --- | --- | --- |
| `chunk_id` | `VARCHAR(512)`，主键 | 父块稳定 ID |
| `text` | `TEXT`，非空 | 块的完整正文 |
| `filename` | `VARCHAR(255)`，索引，非空 | 原始文件名 |
| `file_type` | `VARCHAR(50)`，非空 | 本次为 `Word` |
| `file_path` | `VARCHAR(1024)`，非空 | 服务端原文件路径 |
| `page_number` | `INTEGER`，非空 | Word 本次为 `0` |
| `parent_chunk_id` | `VARCHAR(512)`，非空 | 直接父块 ID；L1 为空 |
| `root_chunk_id` | `VARCHAR(512)`，非空 | 最顶层 L1 ID |
| `chunk_level` | `INTEGER`，非空 | `1` 或 `2` |
| `chunk_idx` | `INTEGER`，非空 | 文件内的全局顺序 |
| `updated_at` | `DATETIME`，非空 | 最后写入时间 |

### 7.2 本次实际记录

直接查询 PostgreSQL 得到 2 条记录：

| chunk_id | level | idx | parent_chunk_id | root_chunk_id | text_length |
| --- | ---: | ---: | --- | --- | ---: |
| `virtual_legal_knowledge_base_test.docx::p0::l1::0` | 1 | 0 | 空字符串 | 自身 ID | 1534 |
| `virtual_legal_knowledge_base_test.docx::p0::l2::0` | 2 | 1 | `...::p0::l1::0` | `...::p0::l1::0` | 1534 |

两个父块的实际 `filename` 均为 `virtual_legal_knowledge_base_test.docx`，`file_type` 为 `Word`，`page_number` 为 `0`。数据库查询证明父块已持久化，不只是任务状态显示成功。

写入方法是 upsert：查询同一个 `chunk_id` 已存在时更新字段；不存在时插入。因此同名重传可更新稳定父块，而不会无限增长相同 ID 的记录。

## 9. Redis：父块读取缓存

PostgreSQL commit 成功后，代码尝试为每个父块写 Redis JSON 缓存。

缓存键格式为：

```text
keyan_zhushou:parent_chunk:<chunk_id>
```

例如 L1 的理论键为：

```text
keyan_zhushou:parent_chunk:virtual_legal_knowledge_base_test.docx::p0::l1::0
```

缓存 JSON 字段为：

```text
chunk_id, text, filename, file_type, file_path, page_number,
parent_chunk_id, root_chunk_id, chunk_level, chunk_idx
```

默认 TTL 是 300 秒。本文写作时的延迟扫描没有找到本次对应的键；由于扫描发生在上传完成五分钟后，符合缓存自然过期的预期。并且 `RedisCache.set_json()` 会吞掉连接异常，因此仅凭“键不存在”不能证明当时写缓存一定成功或失败；PostgreSQL 才是父块的事实来源。

## 10. Milvus：L3 叶子块与混合索引

### 9.1 为什么只有 L3 进入 Milvus

L3 足够小，适合精确检索。L1/L2 放 PostgreSQL 后，RAG 在命中 L3 时可以沿 `parent_chunk_id` 和 `root_chunk_id` 向上恢复更完整的上下文，这就是 Auto-merging 的基础。

### 9.2 本次实际数量

`GET /documents` 对 Milvus 的查询结果按 `filename` 聚合，确认本文件有 **3 条 L3 叶子记录**。

### 9.3 Milvus collection 字段

默认 collection 名来自 `MILVUS_COLLECTION` 环境变量。其 schema 包含：

| 字段 | 类型 | 写入来源/用途 |
| --- | --- | --- |
| `id` | `INT64`，主键，自动生成 | Milvus 自增主键 |
| `dense_embedding` | `FLOAT_VECTOR`，维度 1024 | 本地嵌入模型为每个 L3 文本生成的归一化向量 |
| `sparse_embedding` | `SPARSE_FLOAT_VECTOR` | Milvus 内置 BM25 Function 自动生成 |
| `text` | `VARCHAR(65535)` | L3 正文；同时是 BM25 输入 |
| `filename` | `VARCHAR(255)` | 原文件名，用于展示、过滤、删除和列表聚合 |
| `file_type` | `VARCHAR(50)` | `Word` |
| `file_path` | `VARCHAR(1024)` | 服务端原文件路径 |
| `page_number` | `INT64` | Word 本次为 0 |
| `chunk_idx` | `INT64` | L3 在文件中的全局顺序 |
| `chunk_id` | `VARCHAR(512)` | L3 稳定 ID |
| `parent_chunk_id` | `VARCHAR(512)` | 指向 L2 |
| `root_chunk_id` | `VARCHAR(512)` | 指向 L1 |
| `chunk_level` | `INT64` | L3 固定为 3 |

写入器按最多 50 个 L3 块组成一批。本次仅有 3 个叶子块，因此在一个 embedding 批次和一个 Milvus insert 批次中完成。

### 9.4 Dense 与 BM25 同时存在的原因

```text
L3 text
  -> HuggingFaceEmbeddings.embed_documents()
  -> dense_embedding：适合语义相近但措辞不同的问题

同一个 L3 text
  -> Milvus BM25 Function
  -> sparse_embedding：适合关键词、文件名、术语精确匹配
```

后续检索会将 Dense 和 BM25 的候选结果混合，而不是只依赖其中一种。

## 11. 本地 embedding 与 LangSmith

入库阶段使用 `langchain_huggingface.HuggingFaceEmbeddings`，模型由 `EMBEDDING_MODEL` 指定，默认是 `BAAI/bge-m3`；设备由 `EMBEDDING_DEVICE` 指定，默认 `cpu`。

当前快速入库没有调用聊天模型、Agent 或显式 `@traceable` 包装，因此这次文件解析、分块、本地 embedding 与 Milvus insert **不会自动成为 LangSmith trace**。LangSmith 当前主要会记录聊天/RAG Agent 调用。

若希望在 LangSmith 中观察入库耗时、块数和失败位置，应在 `_process_upload_job()` 或 `MilvusWriter.write_documents()` 外围显式添加 tracing。

## 12. 本次额外数据库副作用

为了完成管理员保护的真实 HTTP 上传，测试过程中创建了两条一次性管理员用户记录：

| users.id | username | role | password_hash_length |
| ---: | --- | --- | ---: |
| 11 | `upload_trace_1784711200` | `admin` | 90 |
| 12 | `ingest_trace_1784711214` | `admin` | 90 |

`users` 表保存的是 `id`、`username`、`password_hash`、`role`、`created_at`；没有保存明文密码。本文不记录密码或访问令牌。

如果这些一次性账号不再需要，可以由数据库管理员删除。删除用户会级联删除该用户的聊天会话和消息，但不会删除知识库文档，因为文档索引不是按上传用户分区的。

## 13. 当前链路的关键风险与改进方向

1. **先删除旧版本，再处理新版本**：新版本失败会导致旧知识丢失。应改为版本化临时写入，全部成功后原子切换。
2. **任务仅在内存**：服务重启后前端无法再读取 job 状态。生产环境应将 UploadJob 持久化到 Redis 或 PostgreSQL。
3. **缓存写入吞异常**：Redis 不可用时任务仍显示成功，只有后续读取退回 PostgreSQL。应增加日志、指标和可观测错误。
4. **`.doc` 与 `.docx` 使用同一 Loader**：旧式二进制 `.doc` 可能不能被 `Docx2txtLoader` 正确读取。建议前端仅允许 `.docx`，或先用 LibreOffice 转换 `.doc`。
5. **文档没有上传时间字段**：列表接口返回 `uploaded_at: null`。若需要审计，应持久化上传任务/文档元数据并按文件版本记录时间、大小、哈希和上传人。

## 14. 关键源文件索引

| 文件 | 负责内容 |
| --- | --- |
| `frontend/src/components/Documents/UploadSection.vue` | 文件选择、拖放、上传按钮和进度展示 |
| `frontend/src/stores/documents.ts` | FormData 提交、上传进度和每秒任务轮询 |
| `backend/api/routes/documents.py` | 上传 API、后台任务、五个执行阶段 |
| `backend/api/resources.py` | 上传目录、共享 Loader、PostgreSQL 父块仓库和 Milvus Writer |
| `backend/api/upload_validation.py` | 文件名与扩展名白名单 |
| `backend/jobs/upload_jobs.py` | 进程内任务状态机 |
| `backend/indexing/document_loader.py` | Word/PDF/Excel/HTML 解析与 L1/L2/L3 分块 |
| `backend/indexing/parent_chunk_store.py` | PostgreSQL 父块 upsert 与 Redis 缓存 |
| `backend/indexing/embedding.py` | 本地 Hugging Face dense embedding |
| `backend/indexing/milvus_writer.py` | L3 批量向量化并写入 Milvus |
| `backend/indexing/milvus_client.py` | Milvus schema、Dense/BM25 索引和查询接口 |
