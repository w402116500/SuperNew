# 实施计划：EnterpriseRAG 英文正式评测

## 目标

用原始英文 EnterpriseRAG 语料建立一套可复现、可解释、可逐题核查的正式评测。最终回答：

1. 正确证据能否被召回并排到前面；
2. 证据充分时，RAG 是否能正确回答；
3. 失败发生在召回、排序、证据组织、回答生成还是评测链路；
4. 单变量改动是否在盲态题上仍然有效。

正式结论不扩展为 511,962 篇全量排行榜。

## 固定口径

- 数据：EnterpriseRAG 原始英文 `title + content`，中文 smoke 只保留审计记录。
- 题目：500 题固定拆分为分析集 300 题、验证集 200 题；十类题型配额和 `case-split.json` SHA-256 不得改写。
- Representative：标准证据文档约 722 篇，加 6,500 篇普通干扰。
- Challenge：Representative 加每题最多 2 篇标题相近难干扰，实际去重数量以 manifest 为准。
- 基线：分块、top-k、Auto-merging、回答模型、判卷模型、Rerank 和 Embedding 配置冻结；用户选择 SiliconFlow 后必须在正式 `prepare` 前完成切换。
- 隔离：仅使用 `rag_eval_enterpriserag_*` 集合和带 run 前缀的父块；绝不操作 `tutorial_verify_embeddings`。
- 变更：正式基线后，每轮只允许改变一个变量；只用分析集诊断和调参，验证集详情保持盲态。

## 任务清单

状态说明：`[x]` 已完成，`[~]` 代码已完成但尚未真实运行，`[ ]` 待执行。

### T0：运行器控制面与测试 `[x]`

**已完成**

- 固定 300/200 题集和十类题型配额；校验题目互斥与 split 哈希。
- 分离 corpus run 和 experiment run；同一语料可以复用多个实验，实验配置不可覆盖。
- 保存 manifest、配置快照、逐题 JSONL、汇总、报告和人工复核队列。
- 验证集详情在自动报告和人工复核产物中抑制；人工队列保留不含题目 ID、问题、答案、证据或失败理由的盲态占位，完整数据仍保留在受控 JSONL。
- Rerank 只有全题成功时才进入策略比较。

**验收证据**

- `uv run python -m unittest discover -s tests`：已通过 75 项。
- `uv run python -m py_compile backend/evaluation/*.py scripts/run_rag_evaluation.py`：已通过。
- 默认业务集合路径没有被评测代码调用。

### T1：逐题人类可读报告 `[x]`

**已完成**

- 新增 `case-review.md`，每题展示问题、参考答案、模型回答、标准证据、召回排名、覆盖率、自动判定、判定理由、人工复核状态和耗时。
- 检索-only 报告按题目聚合 BM25、Dense、Hybrid、Rerank 各策略。
- `manual-review.md` 可跳转到对应题目；`results.jsonl` 仍是完整机器审计源。
- 已用英文 smoke 真实结果生成 RAG 和 retrieval 两份 `case-review.md`。

**验收条件**

- 报告可以回答“问了什么、参考答案是什么、模型答了什么、证据在哪里、为什么这样判”。
- 冻结正式 split 时，验证集问题和答案不出现在自动可读报告中。

### T2：Embedding provider 切换 `[x]`

**已完成**

- `backend/indexing/embedding.py` 支持 `huggingface`（默认）和 `siliconflow`。
- SiliconFlow 使用 OpenAI 兼容 Embeddings API；密钥不写入配置快照。
- 已增加 provider、API 地址、批量大小、超时和缺少密钥的 mock 测试。

**验收条件**

- 用 1-2 条英文文本做一次真实 Embedding 探针，确认模型名、维度、返回数量和超时行为。
- 探针通过后才允许正式 `prepare`；探针失败不得进入正式入库。

**真实验收结果**：已使用 `.env` 中的 SiliconFlow 配置发送 2 条英文文本；返回 2/2 条向量，模型 `BAAI/bge-m3`，维度 1024，耗时约 0.629 秒；未写入 Milvus。

### T3：冻结运行配置 `[x]`

**输入**：最终 `.env`、模型公开标识、分块参数、检索参数、Rerank 参数。

**动作**

1. 确认 `EMBEDDING_PROVIDER`、`EMBEDDING_MODEL`、`DENSE_EMBEDDING_DIM` 和 SiliconFlow 地址。
2. 确认主回答模型、`GRADE_MODEL`、`RERANK_MODEL` 和公开超时配置。
3. 运行 1-2 条英文 Embedding 探针，不写入 Milvus。
4. 将最终公开配置写入本轮准备的 `config.json`；密钥只存在 `.env`。

**闸门**：配置快照和 Embedding 探针通过后，才可创建正式 corpus run。配置变化必须新建 run ID。

### T4：构造 Representative `[x]`

**输入**：冻结的 500 题、标准证据文档、6,500 篇普通干扰。

**动作**

1. 使用原始英文 Markdown，不翻译、不混入中文 canonical 文档。
2. 写入 L3 Dense 向量和 BM25；L1/L2 保存到带 run 前缀的父块存储。
3. 生成 `manifest.json`、`corpus-documents.json`、`document-map.json`、`case-split.json`。
4. 核验文档角色、来源配额、文件哈希、L1/L2/L3 数量、Milvus 行数和父块数量。

**产物**：一个未清理的 Representative corpus run 和独立集合。

**闸门**：manifest、split 哈希和集合行数一致后冻结；发现构造错误必须新建 run，不能原地改写。

**真实验收结果**：`enterpriserag-en-representative-sf-001` 已完成；722 篇标准证据、6,500 篇普通干扰、7,222 篇文档、81,402 个 L3、62,987 个父块；500 题 split 为 300/200，hash 匹配；Milvus 和父块数量一致；默认业务集合为 0 行。

### T5：构造 Challenge `[x]`

**动作**

1. 复用同一 500 题、同一普通干扰抽样规则和同一英文配置。
2. 为每题选择最多 2 篇不属于标准证据的标题相近难干扰。
3. 保存题级 hard-distractor 映射和去重后的实际文档数量。
4. 核验难干扰与标准证据不重叠，且可以从 manifest 追溯。

**闸门**：Challenge manifest 完成后不得修改；正式检索前先确认 Representative 与 Challenge 的普通干扰和标准证据边界。

**真实验收结果**：`enterpriserag-en-challenge-sf-001` 已完成；722 篇标准证据、6,500 篇普通干扰、1,000 篇 hard distractor，共 8,222 篇文档；96,592 个 L3、74,375 个父块；500 题 split 为 300/200，case-split hash 与 Representative 一致。每题恰好 2 篇 hard distractor（共 1,000 条映射），均带 `hard_distractor` 角色且与该题标准证据零重叠。Milvus flush 后实际行数为 96,592，默认业务集合为 0 行。

### T6：检索正式基线 `[x]`

**Representative** 和 **Challenge** 各执行 500 题 retrieval-only：

- BM25；
- Dense；
- Hybrid；
- 全题 Rerank 成功时才加入 Hybrid + Rerank。

**逐题记录**：问题、题型、标准证据、每策略召回排名、耗时、Rerank 状态和错误。

**汇总指标**：Recall@1/3/5、MRR@10、P50/P95、失败数、按题型统计。

**闸门**：两套检索基线完成后，先分析证据召回失败和 Challenge 排序退化；不直接修改 RAG。

**Representative 真实结果**：`baseline-retrieval-001` 已完成 500 题、四策略各 500 条逐题记录。BM25 Recall@1/3/5 为 `0.6440/0.7540/0.7840`，Dense 为 `0.5960/0.6740/0.7140`，Hybrid 为 `0.6560/0.7640/0.8040`；对应 MRR@10 为 `0.7032/0.6420/0.7163`。三次 Rerank 请求读取超时（`qst_0072`、`qst_0157`、`qst_0160`），成功 `497/500`，因此 `hybrid_rerank` 未纳入比较指标，诊断仍保留在逐题 JSONL。验证集详情在可读报告中已隐藏。

**Challenge 真实结果**：`baseline-retrieval-001` 已完成 500 题、四策略各 500 条逐题记录。BM25 Recall@1/3/5 为 `0.5940/0.7220/0.7640`，Dense 为 `0.5340/0.6560/0.6980`，Hybrid 为 `0.5880/0.7360/0.7680`；对应 MRR@10 为 `0.6635/0.6036/0.6647`。两次 Rerank 请求读取超时（`qst_0164`、`qst_0207`），成功 `498/500`，因此同样未纳入比较指标。与 Representative 相比，Hybrid Recall@5 下降 `0.0360`、MRR@10 下降 `0.0516`；这只是难干扰压力测试的观察，不是优化结论。

### T7：Representative 完整 RAG 基线 `[x]`

**动作**

1. 使用 Representative 同一集合执行 500 题完整 RAG。
2. 冻结问题改写、子问题、Auto-merging、回答提示词和判卷配置。
3. 保存每题的 RAG trace、最终上下文、回答、参考答案、判卷结果和耗时。

**汇总指标**：证据全覆盖率、平均覆盖率、回答通过率、拒答正确率、人工复核率、P50/P95、降级和过程触发率、十类题型统计。

**闸门**：基线完成后向用户展示真实失败类别和逐题样本；在讨论前不做 RAG 优化。

**首次真实运行结论**：`baseline-rag-001` 写完 500 条记录，但 3 条达到 300 秒题级时限，后续有 367 条在 worker 返回前退出；它不是有效的 500 题 RAG 性能基线。分析集 300 题中有 226 条属于系统错误，完整分类见该实验目录的 `analysis-failure-classification.md`。旧运行保留审计；已修复 worker 回收资源与退出码记录，需在同一集合用新的 evaluation ID 重跑完整基线。此修复不改变任何 RAG 配置，T8/T9 不提前推进。

**重跑状态**：`baseline-rag-002` 于 2026-08-15 16:53:11 +08:00 启动，复用同一 Representative 集合和冻结配置，`changed_variable=null`；启动健康检查已完成 2 / 500 且没有错误。完成前 T7 仍保持进行中。

**第二次重跑结论**：`baseline-rag-002` 在 305 / 500 时中断，保留 305 条唯一记录（133 条正常、3 条题级超时、169 条 `APIConnectionError`）。最小 SiliconFlow chat probe 返回 HTTP 200，因此不将其归因为服务整体不可用；根因是 worker 在返回 `evaluation_error` 后仍被复用。运行器已改为每次题级 `evaluation_error` 都回收 worker，下一题重新启动干净进程；全量 78 项测试、编译和 diff 检查通过。此次仍未修改 RAG 配置，下一次用新的 evaluation ID 重跑，T8/T9 继续等待有效基线。

**第三次重跑状态**：`baseline-rag-003` 于 2026-08-15 20:23:36 +08:00 启动，复用同一 Representative 集合与冻结配置，`changed_variable=null`；启动检查完成 2 / 500，无题级错误或 stderr 输出。T7 仍在进行中。

**第三次重跑中断结论（真实 checkpoint）**：截至 2026-08-16 00:08 +08:00，`baseline-rag-003` 已写入 `478 / 500` 条唯一逐题记录后停止，保留原始 `results.jsonl` 和配置。记录中有 350 条 `APIStatusError`（HTTP 402，SiliconFlow 返回 `account balance is insufficient`）和 1 条单题 300 秒超时；仅 127 条没有 `evaluation_error`。这是外部账户额度造成的系统性评测链路失败，不是 RAG 质量结果，因此该运行已标记为 `interrupted`，不纳入正式基线或 T8 失败分类。恢复额度后必须使用新的 evaluation ID 从第 1 题重跑，并先通过最小 chat health probe；在此之前不做 RAG 优化。

**最终基线完成（真实结果）**：`baseline-rag-010` 复用同一 Representative 独立集合，从 `baseline-rag-009` 保留 497 个成功题，只重试剩余 3 题。唯一变更为外层单题评测总时限 `EVALUATION_CASE_TIMEOUT_SECONDS=600`；单次模型请求仍为 90 秒，其他配置冻结。最终 `500/500` 题完成，`evaluation_error=0`。汇总结果：证据全覆盖率 `0.7000`、平均覆盖率 `0.7466190476`、自动回答通过率 `0.5240`、正确拒答率 `0.9500`、人工复核率 `0.0700`、端到端 P50 `72.1867s`。结果、配置、manifest 引用、逐题报告和人工队列均已落盘；这组指标才是当前 Representative 完整 RAG 质量基线。

### T8：分析集失败分类 `[x]`

只读取 300 道分析题的详细记录，分类：

- 未召回标准证据；
- 证据召回但排序靠后；
- 标题相近干扰误排；
- 证据不完整或上下文组织失败；
- 回答事实遗漏、扩展或编造；
- 正确拒答失败；
- 判卷异常、API 异常或超时。

每类输出题目 ID、代表性样本、影响范围和可行动性，不提前指定优化变量。

**自动初筛已完成（真实结果）**：`scripts/analyze_rag_failures.py` 已读取 `baseline-rag-010/results.jsonl` 的固定 300 道 analysis 题，生成 `analysis-failure-classification.json` 和 `analysis-failure-classification.md`。自动判卷为 `153 pass / 123 fail / 24 review`；分类为未召回 63、排序靠后 19、证据不完整 19、回答失败 122、自动判卷不确定 23、系统异常 2，hard distractor 和拒答失败均为 0。分类是自动分诊，`human_review` 与 `system_error` 已分开，必须人工复核后才能形成根因结论。

**人工复核已完成（真实结果）**：固定 300 道 analysis 题中，71 条进入本轮人工队列；validation 候选只保留无题目 ID、问题、答案、证据和失败理由的盲态占位，细节不写入可读复核文件。人工结论为：21 条自动 `fail/review` 改判为核心回答成立，4 条保持通过但证据不充分，37 条确认回答合成失败，7 条确认最终证据上下文/组织失败，1 条确认答案失败但伴随判卷 402，1 条生成超时且无答案、质量不可判。详细逐题结论、根因和行动性写入实验目录的两个 analysis `manual-review` 文件，并同步到 `docs/rag-evaluation-implementation.md`。

### T9：单变量实验 `[ ]`

**前置条件**：T8 完成，用户确认一个优化假设。

每轮必须记录：

- 假设和唯一变更变量；
- 不变配置清单；
- 新的 `evaluation_id` 和 `changed_variable`；
- 分析集前后指标与逐题变化；
- 保留、回退或继续观察的决定。

禁止同时修改 Embedding、top-k、Rerank、提示词或分块参数。

### T10：Challenge 分析压力测试 `[ ]`

首轮稳定方案在 Challenge 上运行 300 道分析题完整 RAG，重点观察标题相近难干扰造成的错误。是否扩大到 500 题由真实结果和用户讨论决定。

### T11：盲态验证 `[ ]`

停止参数修改后运行 200 道验证题：Representative 必跑，Challenge 是否同跑按最终方案决定。自动报告不暴露验证集失败详情；所有失败和 `review` 进入人工复核队列，队列中的验证条目只保留盲态占位。

**验收条件**：能区分分析集提升和验证集真实泛化，不把人工未确认的 `review` 算成 pass 或 fail。

### T12：最终报告与清理 `[ ]`

**最终报告**必须包含：评测边界、固定配置、语料规模、题目划分、检索和 RAG 指标、逐题样本、失败分类、人工结论、可证明改进和未改善项。

**清理顺序**：确认所有实验和人工复核完成后，才执行：

```powershell
uv run python scripts/run_rag_evaluation.py cleanup `
  --dataset enterpriserag --run-id <corpus-run-id>
```

清理后再次核验独立集合不存在、父块前缀已删除、默认 `tutorial_verify_embeddings` 行数未改变。cleanup 必须幂等，运行目录和审计报告保留。

## 当前下一步

T7 已完成，T8 自动初筛和 analysis 集人工复核已完成。当前不修改 RAG 参数、不开始 T9；下一步等待用户确认一个唯一优化变量。验证集 200 题仍保持盲态，不在自动报告或人工复核文件中展示题目级失败详情。

## 每个实验的最低产物

```text
evaluation-config.json
source corpus manifest 引用与 SHA-256
case-split 引用与 SHA-256
results.jsonl
case-review.md
summary.json
report.md
manual-review.jsonl
manual-review.md
evaluation-progress.json
```
