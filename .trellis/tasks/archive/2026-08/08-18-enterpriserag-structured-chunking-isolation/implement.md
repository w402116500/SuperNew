# 实施计划：结构化分块与评测存储隔离

## P1：隔离存储基础设施

1. 新增评测专用数据库配置与 URL 校验；数据库名等于业务库、URL 缺失或解析失败时拒绝运行。
2. 新增独立 `EvaluationBase`、`EvaluationParentChunk` 和 `EvaluationParentChunkStore`；只创建评测父块表，不能调用业务 `init_db()`。
3. 为评测父块 store 加入 run-scoped Redis key、按 corpus run ID 查询和安全删除。
4. 添加单测：缺 URL、同业务数据库、独立表初始化、缓存前缀、commit 后缓存、run-scoped 删除。

## P2：结构化 Markdown 分块

1. 为 `DocumentLoader` 加入默认关闭的 `chunking_strategy`；旧路径保持逐字节兼容。
2. 实现 `markdown_header_recursive_v1`：标题解析、标题路径、原子块分类、短块合并、章节内受控递归切分、L1/L2/L3 嵌套。
3. 为块生成新 metadata、标题前缀、同路径前后块关系和不可变配置哈希。
4. 在 Milvus 写入和检索返回中保留结构 metadata，并扩展 candidate snapshot 字段。
5. 添加 Markdown 单测：标题路径、列表、表格、代码围栏、超长章节、短块合并、start/end、前后块、旧路径不变。

## P3：评测运行器与离线审计

1. 新增 `chunking-target-manifest` 生成脚本，从已审计的同文件更远 L3 analysis 子集冻结 30 题；拒绝 validation、重复 ID、哈希漂移和错误分类来源。
2. 新增离线新旧分块对照脚本；只读取本地 Markdown，生成 JSONL、Markdown 和 summary，不调用模型或存储服务。
3. 让 EnterpriseRAG 语料准备显式接受结构化 strategy 和独立 parent store；写入新的 corpus manifest。
4. 让真实评测复用既有 10 worker pool，记录并检查 `evaluation_worker_count=10`；成功 checkpoint 不重复执行。
5. 添加运行器测试：隔离配置不可回退、manifest 记录、validation 隐藏、未授权真实 prepare 拒绝或必须显式确认。

## P4：质量门

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q backend scripts
git diff --check
python ./.trellis/scripts/task.py validate 08-18-enterpriserag-structured-chunking-isolation
```

## P5：离线 30 题审计运行

1. 生成并验证 30 题 manifest。
2. 运行离线新旧分块对照，不启动 PostgreSQL、Redis、Milvus、Embedding、Rerank、回答或判卷服务。
3. 复核 `chunk-audit.md`，报告事实完整性改善、退化和无法判断项。
4. 只有用户确认离线审计值得继续后，才进入 P6。

## P6：受控真实准备与 30 题评测（后续门禁）

1. 由显式运维命令创建 `enterprise_rag_evaluation` 与 `rag_evaluation_owner`，再初始化评测父块表。
2. 创建新的 corpus run、Milvus collection 和 Redis namespace，重建完整 7,222 篇 Representative 语料的新分块。
3. 只运行冻结的 30 道 analysis 题，使用 10 worker；保存配置、逐题 trace、结果和人工复核。
4. 对比材料层、回答层、人工归因层和延迟；不重跑 500 题，不运行 validation。

## 当前执行记录（2026-08-18）

- [x] P1 隔离存储基础设施已实现，并有 URL 防回退、独立表、Redis 前缀和 run-scoped 清理测试。
- [x] P2 `markdown_header_recursive_v1` 已实现；旧 `recursive_l1_l2_l3` 仍为默认路径；结构 metadata 已贯通父块、Milvus、检索和候选 trace。
- [x] P3 运行器门禁和离线审计脚本已实现；结构化 target 必须是 analysis、`changed_variable=document_chunking_strategy`、开启 candidate trace，并固定 10 worker。
- [x] P4 质量门已通过：全量单测、`uv run python -m compileall -q backend scripts`、`git diff --check` 和 Trellis validate。
- [x] P5 已重新生成并运行 `structured-chunking-offline-audit-002`，并完成 30 道题人工复核；只读取冻结 analysis 题和本地 Markdown，没有连接 PostgreSQL、Redis、Milvus，也没有调用模型、Embedding、Rerank、回答或判卷服务。
- [x] P6 实验准备代码和真实运行已完成：`prepare --rechunk-scope targeted --target-manifest ...` 冻结 30 道 analysis 题对应的完整答案文档集合（由 `expected_doc_ids` 推导，当前为 43 篇），仅这些文档使用新策略，其余文档保持旧策略；解析使用有界 worker，Embedding 最多 10 个 batch 并行，Milvus 保持单写者按 batch 顺序提交，父块批量单写者、批次重试和 checkpoint 均已实现。
- [x] targeted 旧向量迁移已实现：先只读校验 baseline Representative collection，再按页/批次复制 7,179 篇非目标文档的 L3 dense vector、正文和 metadata；43 篇目标文档旧向量按 source filename 排除，只对目标新 L3 调用 Embedding。复制 checkpoint 保存 source collection、page offset、copied chunk IDs 和 vector count；恢复时按 `chunk_id` 去重，缺源集合/向量字段直接失败，不回退到全量重嵌入。
- [x] P6 受控真实运行已完成：独立 PostgreSQL 已由运维步骤创建并初始化；`enterpriserag-en-representative-structured-targeted-004` 的 targeted 语料准备完成。最终复制 checkpoint 为 `source_page_offset=81402`、`source_page_count=82`、`vector_copy_count=80621`；43 篇目标文档排除旧向量并生成 734 个新 L3，父块 `63172`、总 L3 `81355`，manifest 为 `prepare_completed=true`。真实评测主运行是 `structured-chunking-analysis-30-001`，随后按缺失/异常题链式重试 `retry-002`、`retry-003`、`retry-004`、`retry-005`，始终只处理异常题。retry-005 仍有 3 道整题超时；另外 3 道在回答请求阶段超时，因此最终有 24 道可判题，整体仍为 `interrupted`。重试没有改变 RAG 参数，也没有把系统异常当作分块失败。此前误启动的 `structured-chunking-targeted-004` 只留下 0 / 30 配置与进度文件并已终止，不作为结果。

### P6 真实运行结果与重试链（2026-08-18）

- 30 题整体（包含系统异常记录）：结构化分块证据全覆盖率 `50.00%`（15 / 30），平均证据覆盖率 `53.98%`，回答通过率 `26.67%`（8 / 30）；系统错误 6 道，状态 `interrupted`。
- 只保留 baseline 和结构化结果都没有系统错误的 24 道同题对照：证据全覆盖率 `8.33% -> 54.17%`（`+45.83` 个百分点），平均证据覆盖率 `14.70% -> 59.14%`（`+44.44` 个百分点），回答通过率 `4.17% -> 33.33%`（`+29.17` 个百分点）。这些是 targeted analysis 结果，不能外推到 500 题总体或 validation。
- 系统异常 6 道中，`qst_0100`、`qst_0115`、`qst_0345` 超过单题 600 秒；`qst_0120`、`qst_0133`、`qst_0221` 的单次模型回答请求超过 90 秒。它们均不计入分块收益。
- 自动归因候选：7 道“证据补回且回答通过”、5 道“证据覆盖提升但回答仍失败”、1 道“证据覆盖退化”、11 道“没有可测证据收益”、6 道系统异常。7 道只能作为待人工确认的因果候选，不能直接等同于分块收益。
- 目标答案文档在候选阶段的出现数：初始候选 `14 / 30`，原始候选 `21 / 30`，合并后候选 `21 / 30`，Rerank 输入 `21 / 30`，最终上下文 `20 / 30`。文件出现不代表关键事实完整进入最终上下文。
- retry-005 的完整链式逐题结果是离线派生文件 `structured-chunking-merged-results.jsonl`，原始 retry 仍遵守中断规则并只保留 `attempt-results.jsonl`；汇总和人工预复核分别为 `structured-chunking-impact-summary.json/.md`、`structured-chunking-manual-review.jsonl/.md`。
- 本轮只改变 `document_chunking_strategy=markdown_header_recursive_v1`；未改变模型、Embedding、Prompt、top-k、Rerank、Auto-merging、语料范围或旧集合。没有重跑 500 题、没有运行 validation、没有写入默认业务集合。

P6 已执行的受控命令顺序（后续若重试，只处理缺失/异常题）：

```powershell
# 1. 环境中显式设置 EVALUATION_DATABASE_URL；不得与 DATABASE_URL 同库
uv run python scripts/init_evaluation_parent_store.py
# 2. 使用全新的 corpus run 和新 collection 名称；不使用 validation
uv run python scripts/run_rag_evaluation.py prepare `
  --dataset enterpriserag --run-id <new-corpus-run-id> --profile full `
  --language en --corpus representative --mode rag `
  --chunking-strategy markdown_header_recursive_v1 `
  --rechunk-scope targeted `
  --target-manifest <structured-chunking-offline-audit-002/chunking-target-manifest.json>
# 3. 语料 manifest prepare_completed=true 后，才允许用 10 worker 运行冻结 30 题
uv run python scripts/run_rag_evaluation.py evaluate `
  --dataset enterpriserag --run-id <new-corpus-run-id> `
  --evaluation-id <new-evaluation-id> --case-set analysis --mode rag `
  --changed-variable document_chunking_strategy --target-manifest <same-manifest> `
  --capture-candidate-trace --workers 10
```

其中第 2 步会重建完整 7,222 篇语料，但只有冻结清单推导出的 43 篇答案文档使用新分块；其余 7,179 篇在新隔离环境中重新生成旧策略分块，不读取旧向量。这样不会污染 baseline，也不会产生无 manifest 的新旧向量混合。

`structured-chunking-offline-audit-002` 的 target manifest 文件 SHA-256 为
`7565e1f478f81956224d9cab601883053a8fd2719e84e8c4626906bf7ae5bfe1`；离线汇总为旧 L3 `500`、新 L3 `462`、关键旧 L3 `61`、边界不变 `17`、边界变化 `44`。这些是分块结构变化，不是回答通过率或证据覆盖率提升。

人工复核产物为 `manual-review.jsonl` 和 `manual-review.md`：8 题有明确标题/列表/代码/表格结构信号，7 题关键边界基本不变，14 题只是普通文本边界重新分配，1 题（`qst_0265`）存在旧块重复定位歧义。复核还发现 `qst_0300` 使用无首尾管道符的 Markdown 表格，已修复识别逻辑并重新生成审计产物。

## 高风险文件与回退点

| 文件 / 模块 | 风险 | 回退点 |
| --- | --- | --- |
| `backend/indexing/document_loader.py` | 改动默认业务切分 | 新策略必须显式选择；默认旧策略不变。 |
| `backend/evaluation/runner.py` | 误用业务父块库或集合 | 独立 URL 和 collection 强制校验；新 run 不影响旧 run。 |
| `backend/rag/utils.py` | candidate trace 丢失结构字段 | 仅扩展 evaluation trace；普通 API 输出保持兼容。 |
| PostgreSQL / Redis | 错误清理业务数据 | 新库、run-scoped 表记录和缓存前缀；拒绝 business URL。 |
| Milvus | 集合误删 | collection 名来自新 corpus manifest；不允许默认业务集合。 |
