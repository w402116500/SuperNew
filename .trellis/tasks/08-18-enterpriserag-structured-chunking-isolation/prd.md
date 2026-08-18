# PRD：EnterpriseRAG 结构化分块与评测存储隔离

## 目标

验证“按 Markdown 结构切分，再在章节内部限制长度”是否能减少 EnterpriseRAG 中关键事实被拆散到不同 L3 的问题；当该方案进入真实评测时，必须将新语料的父块数据与原有业务、历史评测数据物理隔离，避免混写、误读和误清理。

用户可得到的结果不是一套未经验证的新分块，而是：

1. 可审计的新旧分块对照；
2. 独立 PostgreSQL、Milvus、Redis 和文件产物边界；
3. 只改变分块策略的可复现实验；
4. 能区分“关键材料进入上下文”和“回答刚好变对”的逐题证据。

## 已确认事实

### 当前分块与存储

- [backend/indexing/document_loader.py](../../../backend/indexing/document_loader.py) 当前使用 `RecursiveCharacterTextSplitter`，把整页/整篇文本递归切为 L1/L2/L3，目标长度为 `2400/1600/800`，重叠为 `400/200/100`；没有先解析 Markdown 标题树，也没有为最终块保存完整标题路径和原文起止范围。
- [backend/evaluation/runner.py](../../../backend/evaluation/runner.py) 在语料准备阶段只把 L1/L2 写入 `ParentChunkStore`，L3 写入独立 Milvus collection。
- [backend/indexing/parent_chunk_store.py](../../../backend/indexing/parent_chunk_store.py) 当前通过全局 `SessionLocal` 访问 PostgreSQL，Redis 键形如 `parent_chunk:<chunk_id>`；评测目前靠 `__rag_eval__<run_id>__` 文件名前缀做逻辑隔离。
- [backend/infra/database.py](../../../backend/infra/database.py) 的默认 `DATABASE_URL` 指向 PostgreSQL 数据库 `langchain_app`。业务路径和评测父块目前共用该数据库。
- [docker-compose.yml](../../../docker-compose.yml) 的 PostgreSQL 15 服务暴露宿主机端口 `15432`，默认创建 `langchain_app`。新评测库可以复用同一个 PostgreSQL 服务和端口，但必须是不同数据库。

### 已有评测证据

- `baseline-rag-010` 已完成 500 / 500 英文 Representative RAG 基线；回答通过率为 52.40%，证据全覆盖率为 70.00%，平均证据覆盖率为 74.66%。
- 97 道 analysis 证据链人工审计显示：30 道标准来源未完整进入原始候选；41 道标准文件进入但关键事实在其他 L3，其中 11 道在直接相邻 L3，30 道在同一文件更远 L3；19 道原始候选已经完整。
- T9 改写候选融合没有显示稳定、可归因的证据收益；T10 相邻 L3 扩展人工确认的直接收益为 5 / 147，无法解决远距离同文件材料、后续筛选丢失或回答整合失败。
- 因此，结构化分块是下一轮优先验证的单一变量；它尚未实施、尚未重新入库、尚未运行新评测。

## 范围

### 本任务计划内

1. 为 Markdown 及已解析 Markdown 设计结构化标题分块：标题路径、段落、列表、表格、代码块边界在章节内部得到保留，再使用递归切分控制 L1/L2/L3 最大长度。
2. 保持第一版 L1/L2/L3 长度、重叠、Embedding、模型、Prompt、top-k、Rerank 与 Auto-merging 不变，使 `document_chunking_strategy` 成为唯一 RAG 输入变量。
3. 建立新的 PostgreSQL 评测父块数据库，并通过评测专用数据库 URL 访问；不得回退到 `langchain_app`。
4. 对新语料继续使用新的 Milvus collection、独立 Redis 命名空间、新 corpus run ID、新 evaluation ID、新输出目录和不可变配置快照。
5. 先对 30 道“同文件更远 L3” analysis 题做离线新旧分块审计；该阶段不调用模型、不写 PostgreSQL、Redis 或 Milvus。
6. 只有离线审计满足验收条件并取得用户后续批准后，才向新评测数据库和新 Milvus collection 准备语料，随后运行这 30 道 analysis 的真实 RAG 对照。
7. 保存块结构、候选链路、最终上下文、回答、判卷、人工复核、延迟及系统异常记录；validation 详情保持隐藏。
8. 使用受控并发执行多题评测：推荐 10 个 worker，每个 worker 一次只处理一道题；并发数、服务限额、checkpoint、异常 worker 回收和重试范围必须写入运行配置与报告。

### 明确不在本任务计划内

- 不改默认业务 PostgreSQL 数据库 `langchain_app`，不迁移或删除业务父块。
- 不操作默认业务 Milvus collection `tutorial_verify_embeddings`。
- 不覆盖或清理已冻结的 `rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`，不删除 T9/T10 历史产物。
- 不同时改 Embedding、回答模型、Prompt、top-k、Rerank、Auto-merging、查询改写或相邻 L3 扩展。
- 不直接重跑 500 题，不在用户批准前运行 validation 200 题。
- 不把离线分块质量或 30 道 targeted analysis 的结果冒充全体 500 题或 validation 泛化结论。

## 强制约束

### 存储隔离

- 新评测父块必须使用独立 PostgreSQL 数据库，建议名称为 `enterprise_rag_evaluation`；使用专用 owner/role，建议为 `rag_evaluation_owner`。
- 评测代码必须从专用 `EVALUATION_DATABASE_URL` 建立会话；当该变量缺失、不可连接，或解析后的数据库名等于业务 `DATABASE_URL` 的数据库名时，必须在创建集合和写入任何块之前失败。
- 新数据库由显式初始化命令或受控运维步骤创建，不允许在 FastAPI 启动或普通评测运行时隐式执行 `CREATE DATABASE`。PostgreSQL 官方文档规定创建数据库需要 superuser 或 `CREATEDB` 权限，且可显式指定 owner：[CREATE DATABASE](https://www.postgresql.org/docs/current/sql-createdatabase.html)。
- Redis 的评测父块键必须带独立前缀，例如 `rag_eval_chunking:<corpus_run_id>:parent_chunk:<chunk_id>`；不使用业务 `parent_chunk:` 前缀。
- 所有配置快照只写数据库名称、隔离模式和 schema 版本，不写连接密码或完整含密 URL。

### 评测纪律

- 所有真实运行必须使用新 corpus run ID、新 collection、新 evaluation ID、`changed_variable=document_chunking_strategy` 和冻结题目清单。
- `evaluation_worker_count=10` 只能作为执行吞吐参数记录，不得作为 RAG 优化变量；单题内部的检索、Auto-merging、Rerank、生成和判卷顺序不得并行打乱。
- 运行器不得因配置漏传而回退到默认业务 collection、默认业务 PostgreSQL 或非评测 Redis 前缀。
- `analysis` 与 `validation` 保持隔离；报告和人工文件不得暴露 validation 题目、答案、证据或失败原因。
- API、生成、证据评分、判卷和超时异常单独归为 `system_error`，不能计为分块失败或回答失败。

## 验收标准

### A. 离线分块审计

- [ ] 冻结 30 个唯一 `analysis` case ID，均来自“标准文件已进入但关键事实位于同文件更远 L3”的已审计子集；清单保存 case split、基线结果、原文和输入哈希。
- [ ] 同一批原文能产出旧 `recursive_l1_l2_l3` 与新 `markdown_header_recursive_v1` 两套块，不触发模型请求、Embedding、Milvus、PostgreSQL 或 Redis 写入。
- [ ] 每个关键事实记录旧/新块 ID、标题路径、原文范围、结构类型及人工判断；并生成可阅读的并排审计报告。
- [ ] 新策略不跨标题无意义合并；列表不在条目中间截断；表格不丢表头或把列名与数据拆散；代码块不在围栏中间截断。
- [ ] 审计结论明确列出改善、不变、退化和无法判断的题，不能只写“新块看起来更好”。

### B. 存储隔离与语料准备

- [ ] `enterprise_rag_evaluation` 与 `langchain_app` 是 PostgreSQL 服务中的不同数据库；新评测连接没有访问业务库写权限。
- [ ] 新评测 L1/L2 块只写入新数据库；L3 只写入新 Milvus collection；Redis 只使用新命名空间。
- [ ] 新块最少保存：`heading_path`、原文起止范围、结构类型、前后块 ID、父子块 ID、`chunking_strategy`、配置哈希。
- [ ] 配置错误时，在任何 PostgreSQL/Milvus/Redis 写入前失败；失败清理只触及新的 run 标识范围。
- [ ] 历史业务库、默认集合、旧 Representative 集合和历史输出的校验值保持不变。

### C. 30 题真实对照（需要后续单独批准）

- [ ] 使用新的 corpus run 和 evaluation ID，只运行冻结的 30 道 analysis 题。
- [ ] 使用受控 10 worker 并发；每题有独立 checkpoint；成功题不重复请求；异常 worker 可单独回收，缺失/异常题可单独重试。
- [ ] 逐题记录原始候选、结构化块信息、Auto-merging 映射、Rerank 输入输出、最终 top-8、关键事实首次出现阶段、回答、判卷和分段耗时。
- [ ] 报告材料层、回答层、人工可归因层三种结果；系统异常单独列出。
- [ ] 不重跑 500 题，不运行 validation，不把 targeted 结果外推为总体结论。

### D. 工程质量

- [ ] 新旧分块、标题/列表/表格/代码块边界、metadata、数据库 URL 防回退、Redis 命名空间、独立 parent store、清理范围和 validation 隐藏均有自动测试。
- [ ] 运行全量单测、受影响 Python 文件编译、`git diff --check` 和 Trellis task 校验。
- [ ] 新数据库创建、初始化、连接检查、拒绝业务库回退、安全清理和并发重试均有可重复操作说明。

## 已查阅的资料与关联产物

- [docs/rag-chunking-analysis.md](../../../docs/rag-chunking-analysis.md)：本任务的评测背景、外部方案链接、T9/T10 结果和推荐链路。
- [docs/rag-evaluation-implementation.md](../../../docs/rag-evaluation-implementation.md)：正式评测运行记录与产物口径。
- [LangChain Markdown Header Metadata Splitter](https://docs.langchain.com/oss/python/integrations/splitters/markdown_header_metadata_splitter)：标题切分后应使用 `split_documents()` 继续限制块长度，以保留 metadata；重叠不跨标题边界。
- [PostgreSQL CREATE DATABASE](https://www.postgresql.org/docs/current/sql-createdatabase.html)：创建独立数据库及 owner 的官方语法与权限条件。

## 已确认的范围决策

新的 PostgreSQL 数据库只服务本次及未来离线 EnterpriseRAG 评测分块实验，不承载线上业务知识库。线上业务迁移需要独立任务、迁移计划和回归验证。
