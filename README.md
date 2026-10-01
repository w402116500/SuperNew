# RagTrail

RagTrail（RAG + Trail，检索轨迹）是一个企业知识库智能问答系统，面向制度、流程、项目文档、产品手册和 FAQ 等内部资料。

RagTrail 的核心不是“让模型凭记忆回答”，而是把一次问答拆成一条可观察、可追溯的证据链：先把资料解析成可检索的 Markdown，使用分层分块建立索引；用户提问后进行混合检索、上下文恢复和证据判断；最后生成带 `[1]`、`[2]` 引用的回答，并在前端展示本次运行的检索过程、证据片段和评分信息。

## 你可以用它做什么

- 把 PDF、DOCX、PPTX、XLSX、图片、HTML、Markdown 和 TXT 统一纳入知识库。
- 对同一份资料同时保留原文件、Markdown、图片和解析清单，方便追溯解析结果。
- 用稠密向量与 BM25 混合检索，兼顾语义表达和关键词、编号、专有名词匹配。
- 在命中细粒度片段后恢复所属章节上下文，减少“找到一句话但缺少前后条件”的回答。
- 对证据的相关性、可回答性、歧义和置信度进行判断；资料不足时明确说明无法确认。
- 在回答中展示可点击引用，并把本次运行的步骤、候选漏斗、合并和重排结果呈现在前端。
- 对复杂问题拆分子问题并行检索；当问题范围或证据不明确时，通过澄清或范围选择继续检索。

## 一次完整请求

文档入库完成后，用户提问进入检索与证据判断流程。

### 文档入库流程

```mermaid
flowchart LR
    FILE["上传资料"] --> PARSE["原生解析 / MinerU<br/>Markdown 与解析产物"]
    PARSE --> CHUNK["L1 / L2 / L3<br/>三级父子分块"]
    CHUNK --> INGEST["入库编排<br/>验证 · 替换 · 写入"]
    INGEST --> PG[("PostgreSQL<br/>L1 / L2 父块")]
    INGEST --> WRITER["L3 向量入库<br/>MilvusWriter 调用 Embedding"]
    WRITER --> MV[("Milvus<br/>向量与 BM25 索引")]
    classDef process fill:#f1f5f9,stroke:#94a3b8,color:#1e293b
    classDef store fill:#edf7f2,stroke:#7fae97,color:#234f3f
    class FILE,PARSE,CHUNK,INGEST,WRITER process
    class PG,MV store
```

### 问答流程

```mermaid
flowchart TB
    Q["用户提问"] --> PLAN["问题规划<br/>复杂度判断 · 必要时拆分子问题"]
    PLAN --> SEARCH["混合检索<br/>向量 + BM25 → RRF 融合"]
    SEARCH --> CONTEXT["上下文恢复与精排<br/>可选 Auto-merging / Rerank"]
    CONTEXT --> GRADE{"证据是否支持回答？"}
    GRADE -->|"充分"| ANSWER["聊天 Agent 生成回答<br/>流式输出 · 来源引用 · Trace"]
    GRADE -->|"不足"| RETRY["最多一次 Step-back / HyDE<br/>改写后重新检索与判断"]
    RETRY -->|"证据充分"| ANSWER
    RETRY -->|"仍不足"| STOP["明确说明资料不足"]
    GRADE -->|"需要澄清"| HITL["等待用户澄清 / 选择范围<br/>结束本次检索 · 保存恢复状态"]
    HITL -.->|"下一轮补充后定向检索"| SEARCH
    classDef process fill:#f1f5f9,stroke:#94a3b8,color:#1e293b
    classDef result fill:#edf7f2,stroke:#7fae97,color:#234f3f
    classDef decision fill:#fff8ed,stroke:#c9a166,color:#674821
    class Q,PLAN,SEARCH,CONTEXT process
    class ANSWER,STOP result
    class GRADE,RETRY,HITL decision
```

### 1. 文档入库

管理员上传资料后，系统先把文件写入独立的任务目录。Markdown、TXT、HTML 使用原生解析；PDF、DOCX、PPTX、XLSX 和常见图片交给 MinerU 转换为 Markdown。解析产物会保留原文件、Markdown、图片和 `content_list_json` 等信息，便于定位表格、图片和页面来源。

同名文件采用 staged replacement：新版本完成解析、分块并生成至少一个可检索的 L3 叶子块后，才会清理旧版本并提升新版本。如果解析或分块失败，旧版本继续保留。

### 2. 三级父子分块

默认策略是 `recursive_l1_l2_l3`，每个 L3 片段都关联到对应的 L2 和 L1 父块：

| 层级 | 默认长度 / 重叠 | 存储位置 | 作用 |
| --- | --- | --- | --- |
| L1 | 约 2400 / 400 字符 | PostgreSQL，并可由 Redis 缓存 | 保存较完整的章节语境。 |
| L2 | 约 1600 / 200 字符 | PostgreSQL，并可由 Redis 缓存 | 在定位精度与上下文之间折中。 |
| L3 | 约 800 / 100 字符 | Milvus | 作为最小检索单元，负责精确召回。 |

命中多个 L3 片段时，系统可以自动回取其所属的 L2/L1 内容，再把恢复后的上下文交给后续证据判断和回答模型。

### 3. 混合检索与证据判断

检索阶段同时使用 BGE-M3 等稠密向量和 Milvus 原生 BM25，之后使用 RRF 融合排序。复杂问题会先由快速模型拆成多个可独立检索的子问题，再合并各路结果。

候选证据可进一步经过 Rerank。Rerank 是可选能力，是否在某次运行中真正执行，应以该次回答 Trace 中的 `rerank_applied` 为准。随后由独立的判分模型判断证据的：

- `relevance`：内容是否与问题相关；
- `answerability`：证据是否足以支持回答；
- `ambiguity`：问题或资料是否存在歧义；
- `confidence`：当前证据链的整体置信度。

证据不足时最多进行一次查询改写，改写策略为 Step-back 或 HyDE 二选一。若仍无法得到充分证据，系统会明确返回资料不足，而不是用模型常识补齐结论。

### 4. 带引用回答

最终回答只使用当前检索到的证据，并通过 `[1]`、`[2]` 等标记关联来源。前端点击引用后，可以查看文件名、页码、RRF 排名、Rerank 分数和原文摘录，回答与证据之间的对应关系可以直接复核。

## 前端能看到什么

RagTrail 把 RAG 的中间过程作为产品界面的一部分，而不是只展示一个黑盒答案。

| 页面或区域 | 可见内容 |
| --- | --- |
| 对话页 | SSE 流式回答、生成状态、停止生成、会话历史和摘要记忆。 |
| 运行步骤时间线 | 当前运行处于规划、检索、合并、判断还是生成阶段，以及各步骤耗时。 |
| 证据上下文面板 | Session 标识、证据置信度、当前运行状态和引用来源。 |
| 引用来源 | 文件名、页码、原文摘录、RRF 名次和 Rerank 分数。 |
| 检索 Trace 详情 | 候选数量变化、混合召回、RRF、Auto-merging、Rerank、查询改写、复杂度、子问题和子 Agent 详情。 |
| 知识库管理 | 批量上传、后台进度轮询、文档状态、解析产物和删除操作。 |

系统支持登录、注册、JWT 鉴权、会话隔离，以及浅层的会话摘要记忆；深色和浅色模式均可使用。

## 技术架构

下图展示各层组件的职责，以及服务、存储和模型解析能力之间的依赖关系。

```mermaid
flowchart TB
    subgraph WEB["展示层 · Vue 3 + Vite"]
        direction LR
        CHAT["对话与会话"]
        DOCS["知识库管理"]
        TRACE["运行 Trace 与引用"]
        CHAT ~~~ DOCS
        DOCS ~~~ TRACE
    end
    subgraph API["接口层 · FastAPI"]
        direction LR
        REST["REST<br/>认证 · 会话 · 文档 · 停止生成"]
        SSE["SSE<br/>回答流 · 运行步骤"]
        REST ~~~ SSE
    end
    subgraph SERVICES["应用服务"]
        direction LR
        AGENT["ChatService / 聊天 Agent<br/>会话摘要 · 工具调用"]
        RAG["RAG 图编排<br/>检索 · 证据判断 · 改写 / 澄清"]
        UPLOAD["UploadJobManager / Ingestion<br/>解析 · 分块 · 索引"]
        AGENT ~~~ RAG
        RAG ~~~ UPLOAD
    end
    subgraph STORAGE["存储与缓存"]
        direction LR
        PG[("PostgreSQL<br/>用户 · 会话 · L1/L2")]
        REDIS[("Redis<br/>会话与父块缓存")]
        MV[("Milvus<br/>L3 · 向量 · BM25")]
        FILES["本地文件<br/>原文件与解析产物"]
        PG ~~~ REDIS
        REDIS ~~~ MV
        MV ~~~ FILES
    end
    subgraph MODELS["模型与解析服务"]
        direction LR
        LLM["OpenAI 兼容模型<br/>主模型 · 快速模型 · 判分模型"]
        EMB["Embedding<br/>远程服务 / 本地模型"]
        RERANK["Rerank<br/>可选"]
        MINERU["MinerU<br/>Gradio / 官方 API"]
        LLM ~~~ EMB
        EMB ~~~ RERANK
        RERANK ~~~ MINERU
    end
    WEB -->|"HTTP 请求 / SSE 事件"| API
    API --> SERVICES
    SERVICES -->|"持久化与读取"| STORAGE
    SERVICES -.->|"模型调用与文档解析"| MODELS
    STORAGE ~~~ MODELS
    classDef client fill:#eef4ff,stroke:#809dc8,color:#244364
    classDef service fill:#f1f5f9,stroke:#94a3b8,color:#1e293b
    classDef rag fill:#edf7f2,stroke:#7fae97,color:#234f3f
    classDef external fill:#fff8ed,stroke:#c9a166,color:#674821
    class CHAT,DOCS,TRACE client
    class REST,SSE,AGENT,UPLOAD service
    class RAG,PG,REDIS,MV,FILES rag
    class LLM,EMB,RERANK,MINERU external
    style WEB fill:#f8fafc,stroke:#cbd5e1,color:#334155
    style API fill:#f8fafc,stroke:#cbd5e1,color:#334155
    style SERVICES fill:#f8fafc,stroke:#cbd5e1,color:#334155
    style STORAGE fill:#f8fafc,stroke:#cbd5e1,color:#334155
    style MODELS fill:#f8fafc,stroke:#cbd5e1,color:#334155
```

| 层 | 技术 | 主要职责 |
| --- | --- | --- |
| Web 前端 | Vue 3、Vite、TypeScript | 对话、知识库、上传进度、引用和检索 Trace。 |
| API 服务 | FastAPI、SSE | 认证、会话、上传任务、聊天接口和流式响应。 |
| 编排与检索 | Python、LangChain 相关组件、Milvus SDK | 问题规划、混合召回、父子块恢复、证据判断和回答。 |
| 业务数据库 | PostgreSQL | 用户、会话消息、文档元数据和 L1/L2 父块。 |
| 缓存 | Redis | 会话消息、会话列表和父块缓存。 |
| 向量数据库 | Milvus 2.5+ 或 Zilliz Cloud | L3 叶子块、稠密向量和 BM25 稀疏检索。 |
| 文档解析 | MinerU | 富文档转 Markdown，并保留解析资源。 |
| 模型服务 | OpenAI-compatible API 或 Ollama | 主模型、快速模型、判分模型和 Embedding/Rerank 服务。 |

## 快速开始

### 环境要求

- Windows 10/11、PowerShell 5.1 或 PowerShell 7；Linux 部署可使用项目中的 Shell 脚本。
- Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 20+ 和 npm。
- Docker Desktop，用于启动 PostgreSQL、Redis 和本地 Milvus 依赖。
- 一个可用的 OpenAI-compatible 模型服务，或本地 Ollama。
- 一个 MinerU Gradio 服务，或 MinerU 官方 API。项目启动脚本不会自动启动 MinerU。

### 安装依赖

```powershell
uv sync
Set-Location .\frontend
npm install
Set-Location ..
Copy-Item .env.example .env
```

编辑根目录 `.env`，至少确认模型、向量服务、数据库、Redis 和 JWT 配置。真实密钥只放在 `.env`，不要提交到 Git。

最小配置示例：

```dotenv
BASE_URL=https://api.example.com/v1
ARK_API_KEY=your_model_api_key
MODEL=your-main-model
FAST_MODEL=your-fast-model
GRADE_MODEL=your-grader-model

EMBEDDING_PROVIDER=siliconflow
SILICONFLOW_API_KEY=your_embedding_api_key
EMBEDDING_MODEL=BAAI/bge-m3

DATABASE_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:15432/langchain_app
REDIS_URL=redis://127.0.0.1:16379/0
JWT_SECRET_KEY=replace-with-a-long-random-secret

MINERU_PROVIDER=gradio
MINERU_URL=http://127.0.0.1:7860
```

### 启动开发环境

默认启动脚本会依次启动 Docker 依赖、Ollama、后端和前端：

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action start
```

如果使用远程 OpenAI-compatible 模型服务，不需要启动本地 Ollama：

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action start -NoOllama
```

常用操作：

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action status
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action logs
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action restart
pwsh -NoLogo -NoProfile -File .\scripts\ragtrail.ps1 -Action stop
```

启动后访问：

- 前端：<http://127.0.0.1:3000/>
- 后端健康检查：<http://127.0.0.1:8050/health>
- FastAPI 文档：<http://127.0.0.1:8050/docs>

也可以使用根目录的 `start.bat`、`stop.bat`。如果只想手动启动基础设施，可运行：

```powershell
docker compose up -d
```

### 生产部署

Linux 服务器可使用生产 Compose 和脚本：

```bash
./start.sh
./start.sh --build
./stop.sh
```

生产配置请参考 `.env.server.example`。它面向云端 Milvus、远程 MinerU 和远程模型服务，不要求在服务器上同时运行本地 Milvus、etcd、MinIO 和 Attu。`stop.sh` 只停止项目容器，不会删除数据库卷。

## 配置要点

### 模型与向量

`BASE_URL`、`MODEL`、`FAST_MODEL` 和 `GRADE_MODEL` 控制主模型、快速模型和独立判分模型。它们使用同一个 OpenAI-compatible 网关时可以共用 `ARK_API_KEY`；如果服务商使用不同密钥，请按对应适配配置。

Embedding 支持远程服务和本地 Hugging Face 模型。更换向量模型时，需要同步确认 `DENSE_EMBEDDING_DIM`，并重新建立 Milvus 集合，不能直接复用旧集合。

### 检索与重排

以下选项控制候选池和上下文恢复：

```dotenv
RETRIEVAL_TOP_K=8
RETRIEVAL_CANDIDATE_K=30
RETRIEVAL_CANDIDATE_MULTIPLIER=3
AUTO_MERGE_ENABLED=true
AUTO_MERGE_THRESHOLD=2
LEAF_RETRIEVE_LEVEL=3
```

Rerank 需要额外的模型服务：

```dotenv
RERANK_MODEL=Qwen/Qwen3-Reranker-4B
RERANK_BINDING_HOST=https://api.example.com
RERANK_API_KEY=your_rerank_api_key
```

配置文件中的 Rerank、结构化分块、语义分块、改写候选融合和相邻片段扩展属于可选策略。默认索引策略仍是 `recursive_l1_l2_l3`；某次运行是否应用某个策略，应以该次 Trace 为准。

### Milvus

本地 Docker 模式使用 `MILVUS_HOST` 和 `MILVUS_PORT`。使用云端 Milvus 或 Zilliz Cloud 时配置完整地址和令牌：

```dotenv
MILVUS_URI=https://your-milvus-endpoint
MILVUS_TOKEN=your-milvus-token
MILVUS_COLLECTION=embeddings_collection
```

设置 `MILVUS_URI` 后，后端优先使用云端地址；云端模式下可以不启动本地 Milvus、etcd、MinIO 和 Attu。

### MinerU

默认使用本地或自建的 Gradio 服务：

```dotenv
MINERU_PROVIDER=gradio
MINERU_URL=http://127.0.0.1:7860
MINERU_BACKEND=hybrid-engine
```

如果使用 MinerU 官方 API：

```dotenv
MINERU_PROVIDER=official_api
MINERU_API_BASE_URL=https://mineru.net/api/v4
MINERU_API_KEY=your_mineru_api_key
```

两种模式都会产出 Markdown 和解析产物，后续三级分块和入库流程相同。`MINERU_API_KEY` 只应写入本地 `.env`。

## 评测

项目提供通用 RAG 评测脚本，但不绑定某个行业的数据集。应为目标知识库准备 JSONL 题集，并在题目中明确期望文档、事实和禁止臆测的结论：

```json
{"id":"hr-probation-001","question":"试用期最长多久？","expected_docs":["员工手册.md"],"expected_facts":["试用期最长为六个月"],"must_not_claim":["试用期必须为六个月"]}
```

运行评测时显式传入题集和评测账号：

```powershell
$env:EVAL_PASSWORD = '<评测账户密码>'
uv run python .\scripts\run_rag_baseline.py `
  --cases .\data\evaluation\test-cases.jsonl `
  --username '<评测账户用户名>' `
  --output .\output\rag-evaluations\run.jsonl
```

默认会调用独立模型进行判卷；使用 `--answer-grading off` 可以只验证检索结果。脚本会生成逐题 JSONL、汇总 JSON 和 Markdown 成绩单。

README 不宣称固定准确率。比较不同模型或不同检索策略时，应同时记录数据版本、题目、模型配置、运行状态和评分口径，并把指标限定在对应数据集和运行条件内。

## 项目边界

- 更适合静态或低频变化的企业知识。实时指标、订单、库存、审批和业务交易应通过独立 API 或工具接入。
- 富文档依赖 MinerU；`.doc` 和 `.xls` 等旧格式需要先转换为 DOCX 或 XLSX。
- Rerank、结构化/语义分块和部分检索增强策略都是配置项，不能仅凭代码存在就认为每次请求都会执行。
- 当前同名替换会在新版本验证成功后再清理旧版本；跨 PostgreSQL 与 Milvus 的写入失败尚未提供完整的分布式回滚。
- 评测结果依赖题集、语料、模型、提示词和运行配置，不能脱离这些条件外推为普遍准确率。

## 项目结构

```text
RagTrail/
├── backend/                 # FastAPI、认证、会话、RAG 与上传流程
├── frontend/                # Vue 3 + Vite + TypeScript 前端
├── database/                # 数据库模型、迁移或初始化逻辑
├── scripts/                 # 开发启动、评测和运维脚本
├── data/                    # 评测题集及运行所需数据目录
├── docs/                    # 设计与部署相关文档
├── tests/                   # 自动化测试
├── docker-compose.yml       # 本地 PostgreSQL、Redis、Milvus 依赖
├── docker-compose.prod.yml  # 生产容器编排
├── .env.example             # 本地开发配置模板
└── .env.server.example      # 服务器部署配置模板
```
