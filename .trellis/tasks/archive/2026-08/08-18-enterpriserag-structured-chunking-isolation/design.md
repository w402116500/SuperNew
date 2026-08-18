# 技术设计：结构化分块与评测存储隔离

## 设计目标

在不改变线上默认分块和正式基线语料的前提下，为 EnterpriseRAG 评测提供一条可选的 `markdown_header_recursive_v1` 分块路径。新路径必须保持 L1/L2/L3 父子关系，并让每个块带有足够的结构信息，能够定位“关键事实在哪个标题、哪个原文范围、哪一步丢失”。

真实评测的新父块数据使用独立 PostgreSQL 数据库 `enterprise_rag_evaluation`；现有 `langchain_app` 及默认 `ParentChunkStore` 保持不变。

## 边界与不变量

| 项目 | 设计决策 |
| --- | --- |
| 线上业务 | 继续使用现有 `DocumentLoader()` 默认 `recursive_l1_l2_l3` 和 `langchain_app`。 |
| 新分块 | 只由评测运行器通过显式 `chunking_strategy=markdown_header_recursive_v1` 选择。 |
| RAG 变量 | L1/L2/L3 目标长度、重叠、Embedding、模型、Prompt、top-k、Rerank、Auto-merging 不改变。 |
| PostgreSQL | 新评测库只保存评测 L1/L2；不能访问或回退到业务库。 |
| Milvus | 新 corpus run 创建新 collection；L3 继续存 Milvus。 |
| Redis | 评测父块键必须带 `rag_eval_chunking:<corpus_run_id>` 前缀。 |
| Validation | 本任务只使用冻结的 analysis 输入；validation 详情继续隐藏。 |

## 分块数据流

```text
Markdown
  -> MarkdownHeaderTextSplitter 识别 # / ## / ### / ####
  -> 每个 section 带 h1/h2/h3/h4 metadata
  -> 解析 section 内的段落、连续列表、连续表格、围栏代码块
  -> 同标题路径内合并短原子块
  -> 超过目标长度时以 RecursiveCharacterTextSplitter 受控拆分
  -> 每个叶块添加标题路径前缀和结构 metadata
  -> L1 -> L2 -> L3 的嵌套切分
  -> L1/L2: EvaluationParentChunkStore (独立 PostgreSQL)
  -> L3: 独立 Milvus collection
```

### 块内容和 metadata

每个块包含现有字段 `chunk_id`、`parent_chunk_id`、`root_chunk_id`、`chunk_level`、`chunk_idx`、`filename`、`file_type`、`file_path`、`page_number`、`text`，以及新字段：

| 字段 | 说明 |
| --- | --- |
| `heading_path` | 标题路径，例如 `Operations > Failover > Guardrails`。 |
| `heading_level` | 当前最深标题层级。 |
| `source_start_index` / `source_end_index` | 块在输入 Markdown 中的字符范围。 |
| `content_kind` | `paragraph`、`list`、`table`、`code` 或 `mixed`。 |
| `previous_chunk_id` / `next_chunk_id` | 同一标题路径、同一层级的前后块。 |
| `chunking_strategy` | 固定为 `markdown_header_recursive_v1`。 |
| `chunking_config_hash` | 标题级别、长度、重叠和分隔符的配置哈希。 |

为避免改动业务数据库的 `ParentChunk` ORM 模型，新字段由独立 `EvaluationParentChunk` ORM 模型写入独立表 `evaluation_parent_chunks`。线上 `ParentChunk` 表和代码不增加列。

## 独立评测存储

### PostgreSQL

新增 `EvaluationStorageConfig`：

1. 从 `EVALUATION_DATABASE_URL` 显式读取连接；
2. 用 SQLAlchemy URL 解析数据库名；
3. 与业务 `DATABASE_URL` 的数据库名相同、变量缺失或无效时，在任何写入前抛出配置错误；
4. 使用独立 `EvaluationBase` 和 `EvaluationParentChunk`，只创建 `evaluation_parent_chunks` 表，不调用业务 `init_db()`；
5. `EvaluationParentChunkStore` 接受 `corpus_run_id`，所有读取、写入和删除均按该 run ID 约束；
6. PostgreSQL 物理库由显式初始化命令创建，不由应用或评测运行自动创建。

这避免了给全局 `ParentChunk` 增加 metadata 列后导致业务 `langchain_app.parent_chunks` 缺列，也避免 `init_db()` 在评测库创建 users/chat 等业务表。

### Redis

`EvaluationParentChunkStore` 继续复用缓存客户端，但将逻辑键固定为：

```text
rag_eval_chunking:<corpus_run_id>:parent_chunk:<chunk_id>
```

业务缓存仍使用现有 `parent_chunk:<chunk_id>` 逻辑键。缓存写入在 PostgreSQL transaction commit 成功后发生。

### Milvus

新 collection 利用当前 `enable_dynamic_field=True` 接收新 L3 metadata；`MilvusWriter` 显式传递结构字段。检索返回及 candidate trace 必须保留这些字段，使 Rerank 和最终 top-8 可定位标题和原文范围。

## 运行器契约

评测运行器新增仅显式启用的结构化语料模式：

```text
chunking_strategy=markdown_header_recursive_v1
evaluation_storage_mode=isolated_postgresql
evaluation_corpus_run_id=<new run id>
```

语料 manifest 保存：分块策略、配置哈希、评测数据库名（不含凭据）、父块表名、Redis 命名空间、每层块数量和源文件哈希。清理逻辑只操作该 run ID 在新父块表、Redis 前缀和新 Milvus collection 中的数据。

## 并发与恢复

| 工作 | 实施方式 |
| --- | --- |
| 离线切分审计 | 文档并行，子进程只返回内存/文件结果；主进程顺序写 JSONL。 |
| 语料准备 | 可并发解析，父块单 writer 批量提交，L3 由受控批量 Embedding/Milvus 写入。 |
| 30 题 RAG | 使用既有 worker pool，固定 `evaluation_worker_count=10`；每个 worker 一次处理一题。 |
| 异常 | 逐题 checkpoint；异常 worker 关闭队列和进程；仅重试缺失或 error 题。 |

单题内部检索、Auto-merging、Rerank、生成和判卷保持顺序。worker 数是执行参数，保存在配置和报告，不是 `changed_variable`。

## 离线 30 题审计

新增离线命令只读取：冻结 30 题清单、基线 JSONL、原始 Markdown。它生成：

- `chunking-target-manifest.json`：稳定 case ID、输入/基线/分块配置哈希；
- `chunk-audit.jsonl`：每题新旧块映射、结构 metadata、事实审计模板；
- `chunk-audit.md`：供人工并排复核的报告；
- `summary.json`：块数、块长度、结构类型和人工状态汇总。

该命令拒绝 `validation` case ID，不构造 Embedding、不初始化评测存储、不访问 Milvus。

## 风险与回退

| 风险 | 防护 / 回退 |
| --- | --- |
| 新分块破坏业务路径 | 新策略默认关闭；线上继续用旧 loader。 |
| 配置误指向业务库 | 启动前比较数据库名，命中则失败。 |
| 新数据库表污染业务模型 | 使用独立 Base 和独立 ORM 表，不调用业务 `init_db()`。 |
| 分块看似更好但事实仍缺 | 先离线事实审计，再运行 30 题。 |
| 并发导致重复写入 | 父块单 writer，评价 ID 单写者，成功题 checkpoint 后不重跑。 |
| 评测运行中断 | 原集合不变；新 run 可按 manifest 清理，或按 checkpoint 只重试错误题。 |
