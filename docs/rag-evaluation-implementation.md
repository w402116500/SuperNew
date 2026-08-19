# RAG 评测实施记录

> 本文记录企业知识库智能问答系统的 RAG 评测实施过程。只记录已执行且可复现的事实；性能指标、优化结论和失败原因必须在实际运行后补充，不预填示例数据。

## 当前结论：先看这里

更新时间：2026-08-16。下面是给人快速阅读的状态摘要；后面的章节保留完整实施时间线和逐轮证据。

### 一句话结论

评测控制面已经实现并通过测试，英文 10 题 smoke、Representative 和 Challenge 正式语料准备，以及两套语料的 500 题 retrieval-only 已真实完成；**Representative 完整 RAG 三次运行均未形成有效正式基线，当前未做 RAG 优化**。

### 已经完成

| 项目 | 当前状态 | 说明 |
| --- | --- | --- |
| 题目划分 | 已完成 | 500 题固定为分析集 300 题、盲态验证集 200 题，十类题型配额已核验 |
| 语料与实验分离 | 已完成 | 一份语料可以复用多个实验；每个实验有独立配置和结果目录 |
| 结果可追溯 | 已完成 | 保存 manifest、哈希、逐题 JSONL、汇总、报告和人工复核队列 |
| 独立存储 | 已完成 | 评测使用 `rag_eval_enterpriserag_*`；默认业务集合未被写入 |
| 英文 smoke | 已完成 | 112 篇文档，10 题检索和完整 RAG 均已真实运行；结果仅作链路审计 |
| SiliconFlow Embedding 接入 | 已完成 | 真实探针返回 2/2 条 1024 维向量；密钥只从 `.env` 读取 |
| Representative 正式语料 | 已完成 | 7,222 篇文档、81,402 个 L3、62,987 个父块，独立集合已冻结 |
| Challenge 正式语料 | 已完成 | 8,222 篇文档、1,000 篇 hard distractor、96,592 个 L3、74,375 个父块，独立集合已冻结 |
| Representative retrieval-only | 已完成 | BM25、Dense、Hybrid 各 500 题；Rerank 497/500，按全量规则未纳入比较 |
| Challenge retrieval-only | 已完成 | BM25、Dense、Hybrid 各 500 题；Rerank 498/500，按全量规则未纳入比较 |
| Representative 完整 RAG | 中断，不能用于正式结论 | `baseline-rag-003` 保留 478/500 条逐题记录；350 条为 SiliconFlow HTTP 402 余额不足，1 条超时；已停止并标记为 `interrupted` |

### 英文 10 题 smoke 实测结果（不是正式结论）

- 入库文档：12 篇标准证据 + 100 篇普通干扰，共 112 篇。
- 检索 Recall@1：BM25 `0.7000`；Dense、Hybrid、Hybrid + Rerank 均为 `0.8000`。
- 完整 RAG：回答判卷通过率 `0.6000`，证据全覆盖率 `0.7778`，人工复核率 `0.3000`。
- 端到端耗时 P50：`30.3088 s`。
- smoke 集合已清理；逐题结果和报告仍保留用于审计。

### 目前没有做的事情

- Representative 的 500 题完整 RAG 基线尚未形成最终指标；`baseline-rag-001`、`baseline-rag-002`、`baseline-rag-003` 均仅作审计记录。
- `baseline-rag-003` 暴露出 SiliconFlow 账户余额不足导致的系统性 HTTP 402；恢复正式评测前必须先补充额度并重新做最小 chat health probe。
- 尚未根据正式失败样本提出或执行任何优化。

### 下一步顺序

1. 在额度恢复并通过最小 chat health probe 后，使用新的 evaluation ID 重跑 Representative 的 500 题完整 RAG 基线。
2. 向用户展示真实失败分类后，再讨论一次只改变一个变量的优化。
3. 最后运行 200 题盲态验证，确认结果能否泛化。

### 证据位置

- 代码：`backend/evaluation/`、`backend/indexing/embedding.py`、`scripts/run_rag_evaluation.py`
- 自动化测试：`tests/test_rag_evaluation.py`、`tests/test_embedding_service.py`
- 运行产物：`output/rag-evaluations/enterpriserag/`
- 本文后续章节：按日期记录命令、真实结果、问题和清理情况

## 1. 这次评测要回答什么

这不是为了给 51 万篇 EnterpriseRAG 文档做一个看似精确的全量排行榜，而是为了回答三个可定位的问题：

1. 检索系统能不能把正确证据找回来？
2. 找到证据后，RAG 能不能基于证据正确回答？
3. 如果失败，问题发生在召回、排序、证据组织、回答生成，还是评测链路本身？

因此，检索-only 和完整 RAG 分开跑；每道题都保存问题、标准答案、模型回答、证据排名、判定理由和耗时，而不是只看一个总通过率。

## 2. 评测方法与设计理由

### 数据怎么控制

- 只使用 EnterpriseRAG 原始英文题目和英文文档。
- 500 道题固定拆成分析集 300 道、盲态验证集 200 道；题型配额先冻结，避免每轮换题造成虚假提升。
- Representative 由标准证据文档和约 6,500 篇普通干扰组成，用来观察正常企业知识库中的基础能力。
- Challenge 在 Representative 上为每题加入最多两篇标题相近的难干扰，用来观察系统是否会被相似标题带偏。

### 系统怎么运行

```text
原始英文文档
  -> 标题 + 正文 Markdown
  -> L1/L2/L3 三级分块
  -> L3 Dense 向量 + Milvus BM25
  -> BM25 / Dense / Hybrid / 条件满足时 Rerank
  -> 完整 RAG：检索、Auto-merging、改写或子问题、回答生成
  -> 独立判卷 + 人工复核
```

语料只入库一次，多个实验复用同一个独立集合。每个实验保存自己的配置、语料 manifest 哈希、题目集合、逐题 JSONL、汇总和报告；正式优化时一轮只改变一个变量。

### 指标怎么解释

| 层次 | 主要指标 | 指标回答的问题 |
| --- | --- | --- |
| 检索 | Recall@1/3/5、MRR@10 | 标准证据有没有找回来、排得靠不靠前 |
| 证据 | 全覆盖率、平均覆盖率 | 一道题需要的证据是否找齐 |
| 回答 | 回答通过率、无答案拒答正确率 | 找到证据后，回答是否事实正确、是否会正确拒答 |
| 诊断 | 人工复核率、错误类别、P50/P95 | 失败发生在哪一层，代价和稳定性如何 |

例如：Recall 高但回答通过率低，优先检查证据组织和生成；Recall 低，才优先检查 Embedding、检索策略或 top-k；Representative 正常而 Challenge 明显下降，则说明相似标题干扰造成了排序问题。

补充口径：`high_level` 和 `info_not_found` 题在原始 EnterpriseRAG 标注中没有标准证据文件，因此 retrieval-only 报告里的 Recall/MRR `0` 只表示“没有可计算的标准证据排名”，不能当作检索失败；这两类题应在完整 RAG 中看回答和拒答指标。

## 3. 一道题到底怎样评测

每道题的可读记录固定包含六部分：问题、参考答案、模型回答、证据检查、自动判定、人工结论。原始 JSONL 保存完整字段，`case-review.md` 提供人直接阅读的版本。

下面是英文 smoke 中真实执行过的 `qst_0381` 摘要：

### qst_0381：证据找到了，但回答需要复核

**问题**

> In the planned controlled failover game day on 2026-01-15 (Hosted API us-east → eu-west), what caused the routing automation to oscillate (flip/rollback), and what follow-up ticket and target ship date were created to fix the streaming disconnect/reconnect behavior clients saw during cross-region redirects?

**参考答案要点**

> 振荡由嘈杂或误报的可达性信号，以及过于宽松的防抖和回滚保护共同造成；后续工单是 `ENG-2422`，目标日期是 `2026-02-07`。

**模型回答要点**

> 模型正确回答了振荡原因、`ENG-2422` 和 `2026-02-07`，但额外加入了参考答案没有提到的关联工单 `SUP-25322`。

**证据情况**

- 两篇标准证据都被召回；排名为第 2 和第 1；覆盖率 `1.0000`。
- 因此这不是召回失败，而是回答阶段的事实扩展风险。

**判定**

```text
自动判定：review
原因：核心事实正确，但存在参考答案未支持的额外工单，且 guardrails 的语义需要人工确认。
```

这类记录的价值在于，它不会把所有问题都粗暴归为“模型答错”：这道题的检索通过、证据覆盖通过，疑似问题集中在生成阶段，后续优化方向就不应首先去改召回。

## 4. 当前真实结果能说明什么

英文 10 题 smoke 使用 12 篇标准证据和 100 篇普通干扰，共 112 篇文档。检索结果为：BM25 Recall@1 `0.7000`，Dense、Hybrid 和 Hybrid + Rerank 均为 `0.8000`；完整 RAG 回答通过率 `0.6000`、证据全覆盖率 `0.7778`、人工复核率 `0.3000`。

这轮只证明评测链路、隔离集合、逐题记录和人工复核机制可以工作；样本量太小，不能作为 500 题正式结论，也没有据此开始 RAG 优化。中文 smoke 只保留审计价值，不参与英文正式结论。

## 5. 现在做到哪一步

已完成：评测运行器、固定题目划分、语料复用、独立集合、逐题 JSONL、可读 `case-review.md`、英文 smoke 和 75 项自动化测试。

尚未完成：Representative 的 500 题完整 RAG 基线，以及基于正式失败样本的单变量优化。

## 6. 下一步怎么走

1. 运行 Representative 的 500 题完整 RAG。
2. 根据 300 道分析题的逐题失败类型提出一个变量的改动，取得讨论后再做实验。
3. 最后使用 200 道盲态验证题确认结果是否可泛化。

## 7. 产物怎么查

- 逐题机器记录：每个实验目录的 `results.jsonl`。
- 人类可读逐题记录：同目录的 `case-review.md`。
- 人工复核队列与结论：`manual-review.jsonl` 和 `manual-review.md`。正式 split 下，队列仍保留每条 validation 候选的盲态占位记录，但不写入题目 ID、问题、答案、证据或失败理由；完整细节只在受控 `results.jsonl` 中。
- 汇总指标：`summary.json` 和 `report.md`。
- 语料和题目版本：corpus 根目录的 `manifest.json`、`corpus-documents.json`、`case-split.json`。

<details>
<summary>展开查看历史实施与原始审计记录</summary>

## 目标与边界

目标是建立一套可复现的离线评测闭环：固定数据、固定配置、保存逐题结果，分别评估检索、完整问答和文档解析对 RAG 表现的影响。

评测数据写入独立的 Milvus 集合，禁止清理或污染业务知识库。每轮仅变更一个变量，例如检索方式、是否启用 rerank 或切分参数；其余配置、数据和题目保持一致。

## 已完成：数据准备与核验

日期：2026-08-13。

数据保存在 `tmp/rag-benchmarks/`，该目录已受 `.gitignore` 忽略，不提交原始评测数据。通过 `http://127.0.0.1:7897` 代理下载，累计可用数据约 3.92 GiB。

| 数据集 | 实际内容 | 本地核验结果 | 计划用途 |
| --- | --- | --- | --- |
| MTEB EcomRetrieval | 100902 条中文商品文本、1000 条查询、1000 条标准相关商品 | Parquet 字段与数量已读取 | 首个检索基线 |
| MultiHopRAG | 609 篇英文资料、2556 题、答案与 0-4 条证据 | 问题和语料结构已读取 | 多证据与拒答评测 |
| Open RAG Benchmark | 1000 篇论文分节文本、3045 题、答案和目标章节 | JSON 结构与题目类型已读取 | Markdown 分块评测 |
| EnterpriseRAG-Bench | 511962 篇企业文档、500 题、标准文档、答案和事实点 | Parquet 字段、题型和来源分布已读取 | 企业知识库评测 |
| OHR-Bench | 8561 页标准文本及 MinerU 等解析噪声版本 | 字段和领域分布已读取 | 解析质量对检索的影响 |
| ChatRAG-Bench | 10 个多轮问答子集 | 官方 15 个数据文件已逐项核验大小 | 多轮追问与拒答 |

完整的本地数据清单与校验说明见 `tmp/rag-benchmarks/README.md`。

## 已完成：数据适用性分析

### EcomRetrieval

- 语料字段：`_id`、`text`、`title`；查询字段：`_id`、`text`；标准关联字段：`query-id`、`corpus-id`、`score`。
- 查询是平均约 6.8 个字符的中文商品搜索词；每个查询目前有 1 条标准相关商品。
- 适合独立比较 BM25、Dense、Hybrid 和 rerank 的排序效果。
- 局限：语料主要是短商品标题，不代表长篇企业文档或回答生成质量。

### MultiHopRAG

- 问题类型包括 inference、comparison、temporal 与 null；301 道 `null_query` 没有标准证据。
- 非空题需要 2 至 4 篇资料共同作答，适合检查多证据召回、回答合成和资料不足时的拒答。

### Open RAG Benchmark

- 每篇论文以标题和 `sections` 保存，问题标注为 extractive 或 abstractive，并提供目标 `doc_id`、`section_id` 和参考答案。
- 适合比较 Markdown 切分方式、层级分块和 Auto-merging 对长文检索的影响。

### EnterpriseRAG-Bench

- 文档字段为 `doc_id`、`source_type`、`title`、`content`，内容可直接转为 Markdown。
- 500 道题提供 `expected_doc_ids`、`gold_answer`、`answer_facts`；涵盖 basic、conflicting_info、info_not_found、completeness 等题型。
- 全量 51 万篇文档入库成本较高，先做适配与小规模冒烟，再进行全量正式评测。

### OHR-Bench

- 含 `gt_text` 与不同强度的 `semantic_noise_MinerU_*`、格式噪声字段，但没有问题、答案或标准相关文档。
- 不能单独评价问答正确率；需要与带标准检索关系的问答集结合，作为解析文本对照变量使用。

### ChatRAG-Bench

- 样本通常包含 `messages`、`ctxs`、`answers`，部分包含 `ground_truth_ctx`。
- 更适合后续评价会话历史承接和给定上下文后的多轮回答；不能替代从全库检索的单轮检索评测。

## 当前项目的接入点

- `backend/rag/utils.py` 中的 `retrieve_documents()` 负责完整检索流水线：召回、Auto-merging、rerank、阈值过滤，并返回 `docs` 与诊断 `meta`。
- `backend/rag/pipeline.py` 中的 `run_rag_graph()` 负责完整问答链路：复杂度判断、检索、证据评分、至多一次改写、复杂问题并行子问题和答案合成。
- `backend/indexing/milvus_writer.py` 中的 `MilvusWriter.write_documents()` 负责 L3 叶子块的 Dense 向量生成和 Milvus 写入；BM25 稀疏向量由 Milvus Function 从 `text` 自动生成。
- 现有 `scripts/run_rag_baseline.py` 已具备 JSONL 用例读取、逐题结果写入、来源排名记录和独立回答裁判能力，可作为新评测运行器的参考，但不会修改历史评测结果。

## 已完成：离线评测工具实现与验证

日期：2026-08-13。

- 新增 `scripts/run_rag_evaluation.py`，提供 `prepare`、`evaluate`、`cleanup` 三个子命令。评测集合固定命名为 `rag_eval_<dataset>_<run_id>`；Milvus 不接受连字符，因此集合名会把 `run_id` 中的 `-` 转为 `_`，运行目录和报告仍使用原始 `run_id`。工具不读取或写入默认业务集合。
- 新增 `backend/evaluation/datasets.py`：EcomRetrieval 转为“标题 + 正文”的 Markdown；MultiHopRAG 转为带标题、来源、作者、日期、正文的 Markdown。前者只生成 L3，后者保留 L1/L2/L3，父块文件名统一带 `__rag_eval__<run_id>__` 前缀。
- 新增 `backend/evaluation/metrics.py` 与 `backend/evaluation/runner.py`。每次运行保存配置、清理清单、Markdown 与原始 ID 映射、固定题目、逐题结果、汇总和 Markdown 报告。`cleanup` 可以重复执行。
- 为 `MilvusStore` 加入服务端原生 BM25 检索入口；EcomRetrieval 可对比 BM25、Dense、Hybrid。只有 rerank 服务配置完整且所有题目实际精排成功时，`hybrid_rerank` 才写入可比较指标；失败诊断仍保留在逐题结果中。
- `RetrievalRuntime` 与 `run_rag_graph(..., retrieval_runtime=...)` 已支持注入独立 Milvus、Embedding 和父块存储；线上默认调用路径不变。MultiHopRAG 会复用真实复杂度判断、检索、证据评分、一次改写、子问题和合成流程，回答判卷为独立 `GRADE_MODEL` 请求，温度固定为 0；配置或解析失败一律标记 `review`，等待人工复核。

已执行的验证命令：

```powershell
uv add pyarrow
uv run python -m unittest discover -s tests
uv run python -m py_compile backend/evaluation/datasets.py backend/evaluation/metrics.py backend/evaluation/runner.py scripts/run_rag_evaluation.py
git diff --check
```

结果：测试集共 35 项全部通过，运行约 0.04 秒；编译和空白检查通过。本地适配器再次实际读取到 EcomRetrieval 100,902 篇语料、1,000 题与 100 题固定冒烟集，MultiHopRAG 609 篇语料、2,556 题与 120 题固定冒烟集。全部为 mock 或本地数据读取验证，尚未调用 Milvus、Embedding、Rerank、主回答模型或判卷模型，因此本文没有填写任何性能指标或优化结论。

### 真实运行记录：集合名兼容性修复

日期：2026-08-13。首次执行 `prepare --dataset ecomretrieval --profile smoke --run-id ecom-smoke-001` 时，Milvus 在创建集合前拒绝了 `rag_eval_ecomretrieval_ecom-smoke-001`：集合名不能含连字符。该次未产生向量写入、模型调用或默认集合变更；运行目录及其 `manifest.json` 保留用于追溯。

根因是运行编号允许连字符，而 Milvus collection 只允许字母、数字和下划线。已将集合内部名称改为把连字符转换为下划线，例如 `ecom-smoke-002` 对应 `rag_eval_ecomretrieval_ecom_smoke_002`；运行目录、报告和样本编号不变。新增单元测试覆盖此规则。修复后将使用新的运行编号重新执行真实冒烟，真实指标另行记录。

### 真实运行记录：进程中断保护

同日，修复集合名后开始 `ecom-smoke-002`，首批 50 条向量已经写入独立集合，但 Windows 记录到虚拟内存不足后终止了评测进程，未完成入库。原因是该进程加载 CPU BGE-M3 约占 5.3 GB，运行中的后端也持有一份模型，加上 Milvus 的 WSL 虚拟机约占 17.2 GB。该集合已通过 `cleanup` 删除，未调用检索、rerank 或回答模型。

为防止中断后的部分数据被误评测，`manifest.json` 新增 `prepare_completed`：入库成功后才标记为 `true`，`evaluate` 会拒绝未完成运行。`cleanup` 继续可重复执行；对修复前无法被 Milvus 创建的非法集合名会明确记录为 `skipped_invalid_legacy_name`。后续真实运行将先停止项目后端释放模型内存，保留 Docker/Milvus 与前端，完成后再恢复后端。

### 真实运行记录：EcomRetrieval 全量语料入库与检索基线（已完成）

日期：2026-08-13。当前运行编号为 `ecom-smoke-003`，运行命令为：

```powershell
uv run python scripts/run_rag_evaluation.py prepare --dataset ecomretrieval --profile smoke --run-id ecom-smoke-003
```

- 独立集合：`rag_eval_ecomretrieval_ecom_smoke_003`（连接 `127.0.0.1:19531`），未触碰默认业务集合。
- 语料范围：EcomRetrieval 全量 100,902 篇文档；评测范围为固定种子 `20260813` 的 100 道 smoke 题。完整语料必须入库，避免相关文档位于未写入的部分而使召回指标失真。
- 实际配置：`BAAI/bge-m3`、CPU、1024 维 Dense 向量；Milvus 服务端同时根据 `text` 生成 BM25 稀疏向量。为释放内存，已暂时停止项目后端和 MinerU 容器，保留项目 Docker 的 Milvus、PostgreSQL、Redis、MinIO、etcd 与 Attu。
- 运行观察：19:56（UTC+8）已生成 32,250 份 Markdown、Milvus 已确认 32,000 行；评测 Python 工作集约 2.29 GB，系统可用物理内存约 5.46 GB，未出现新的虚拟内存耗尽事件。20:43 完成 100,902 份 Markdown，`manifest.json` 已置 `prepare_completed=true`；主动 flush 后 Milvus 实际行数为 100,902。

检索命令如下，实际产生 `results.jsonl`（400 条逐题记录）、`summary.json` 和 `report.md`：

```powershell
uv run python scripts/run_rag_evaluation.py evaluate --dataset ecomretrieval --run-id ecom-smoke-003
```

| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.2500 | 0.3700 | 0.4100 | 0.3263 | 3.2 ms | 3.8 ms |
| Dense | 0.3900 | 0.5400 | 0.5700 | 0.4793 | 83.2 ms | 101.3 ms |
| Hybrid (RRF) | 0.3500 | 0.4800 | 0.5600 | 0.4332 | 84.2 ms | 98.9 ms |
| Hybrid + Rerank | 0.5000 | 0.6000 | 0.6600 | 0.5632 | 681.2 ms | 1847.0 ms |

- Rerank 真实调用：100/100 成功，`rerank_strategy_status=effective`；四种策略均没有检索异常或 Dense 降级。
- 发现的问题：当前 RRF Hybrid 的 Recall@5 为 0.56，略低于 Dense 的 0.57，因此“加入 BM25 就一定提升”的假设不成立；而即使精排后仍有 34/100 题未在前 5 命中，说明该问题更多是候选集未召回或商品文本本身相近，不能靠重排单独解决。
- 证据样本：`case_id=200050`（"蒙迪欧车模"）Dense 未命中前 10，Hybrid + Rerank 将标准文档排到第 5；`case_id=200011`（"毛衣绿色拼接"）在 Hybrid + Rerank 前 5 仍无标准文档。前者说明精排能纠正候选排序，后者说明需要检查召回候选数、分块内容或查询改写。
- 单变量结论：本轮没有改动代码或参数，只比较检索模式。保留 Rerank 作为可配置优化项，因为 Recall@5 相比 Dense 提升 9 个百分点、MRR@10 提升约 8.4 个百分点；但不能默认强制开启，因为 P50 增加约 598 ms、P95 增加约 1.75 s。下一轮应固定该 smoke 集，仅提高 rerank 候选数或调整 RRF 参数之一，验证能否降低 34 个未命中题而不明显放大延迟。

### 真实运行记录：MultiHopRAG 首次准备失败

日期：2026-08-13。执行 `prepare --dataset multihoprag --profile smoke --run-id multihop-smoke-001` 时，首份 Markdown 在 Windows 创建失败，错误为路径不存在，实际原因是评测前缀、URL 和标题共同生成的文件名过长，超过 Windows 路径限制。失败发生在 Markdown 写入阶段，尚未写入 PostgreSQL 父块、Milvus 向量或调用任何模型。

修复：`stable_filename()` 改为在摘要保持不变的前提下限制文件名长度；写入 MultiHopRAG 时会把 `__rag_eval__<run_id>__` 前缀纳入 120 字符预算。新增单元测试覆盖长 URL、长标题与评测前缀。下一次真实运行使用新的 `multihop-smoke-002` 编号，避免覆盖失败痕迹。

### 真实运行记录：MultiHopRAG 入库完成，完整链路受外部模型额度阻断

日期：2026-08-13。运行 `prepare --dataset multihoprag --profile smoke --run-id multihop-smoke-002` 已完成：609 篇原始资料转换为 Markdown，生成 10,002 个 L1/L2 父块并写入评测前缀隔离的 PostgreSQL/Redis，13,556 个 L3 叶子块写入独立集合 `rag_eval_multihoprag_multihop_smoke_002`。`manifest.json` 已标记 `prepare_completed=true`；Milvus 的 `get_collection_stats()` 曾延迟显示 11,000 行，但实际 `query` 已返回完整 13,556 行，因此没有缺失入库数据。固定 smoke 题共 120 道，comparison、inference、null、temporal 各 30 道。

随后执行：

```powershell
uv run python scripts/run_rag_evaluation.py evaluate --dataset multihoprag --run-id multihop-smoke-002
```

该命令真实启动了 BGE-M3、Milvus、父块读取、Ollama 云模型和独立判卷链路，但在首个需要模型结构化输出的样本前后异常退出，没有产生可用的 `results.jsonl`、`summary.json` 或 `report.md`，所以本次没有任何 MultiHopRAG 指标可以填写。为定位原因，对同一模型服务执行最小真实请求，返回 HTTP 429：`weekly usage limit`，即 `minimax-m3:cloud` 的云模型周额度已耗尽。补充检查点后用同一命令再次真实执行，`evaluation-progress.json` 明确记录为 `interrupted`，失败题号为 `1001`、已完成题数为 `0`，错误仍为 HTTP 429。该限制来自外部模型配额，不是检索、切分、Auto-merging 或回答正确率的评测结果，不能通过重试伪装成正常指标。

针对这次失败，评测器改为逐题同步写入 `results.jsonl`，并写入不含密钥的 `evaluation-progress.json`。在集合尚未清理时，恢复同一 `run_id` 会跳过已完成样本并重试失败题；错误会保留失败题号与错误摘要。本轮完成取证后已执行 `cleanup`，因此额度恢复或切换到有可用额度的等价配置后，必须使用新的运行编号重新 `prepare`。只有得到完整逐题结果后，才能记录覆盖率、回答通过率、拒答率和时延。

首次真实冒烟运行的固定命令：

```powershell
uv run python scripts/run_rag_evaluation.py prepare --dataset ecomretrieval --profile smoke --run-id ecom-smoke-001
uv run python scripts/run_rag_evaluation.py evaluate --dataset ecomretrieval --run-id ecom-smoke-001
uv run python scripts/run_rag_evaluation.py cleanup --dataset ecomretrieval --run-id ecom-smoke-001
```

MultiHopRAG 的首次真实冒烟命令：

```powershell
uv run python scripts/run_rag_evaluation.py prepare --dataset multihoprag --profile smoke --run-id multihop-smoke-001
uv run python scripts/run_rag_evaluation.py evaluate --dataset multihoprag --run-id multihop-smoke-001
uv run python scripts/run_rag_evaluation.py cleanup --dataset multihoprag --run-id multihop-smoke-001
```

### 真实运行记录：SiliconFlow DeepSeek-V4-Flash MultiHopRAG 烟测

日期：2026-08-14。切换到 SiliconFlow OpenAI 兼容接口后，使用 `deepseek-ai/DeepSeek-V4-Flash` 作为主模型、复杂度规划模型和独立判卷模型；Embedding 保持 `BAAI/bge-m3`，Rerank 为 `Qwen/Qwen3-Reranker-4B`。密钥未记录在本文件或运行配置中。

本轮先执行：

```powershell
uv run python scripts/run_rag_evaluation.py prepare --dataset multihoprag --profile smoke --run-id multihop-siliconflow-smoke-001
uv run python scripts/run_rag_evaluation.py evaluate --dataset multihoprag --run-id multihop-siliconflow-smoke-001
```

- 独立集合：`rag_eval_multihoprag_multihop_siliconflow_smoke_001`，与默认业务集合隔离。
- 实际入库：609 篇 Markdown、10,002 个 L1/L2 父块、13,556 个 L3 叶子向量。
- 固定题目：120 道，comparison、inference、null、temporal 各 30 道。
- 真实链路：Hybrid 检索、Milvus 原生 BM25、RRF、SiliconFlow 的 `Qwen/Qwen3-Reranker-4B` 精排、三级分块 Auto-merging、复杂度判断、并行子问题、证据评分、主模型回答和独立 GRADE_MODEL 判卷均已调用。Rerank trace 显示实际生效；所有模型与判卷调用均为 DeepSeek-V4-Flash，判卷温度为 0。

运行产物位于被 Git 忽略的 `output/rag-evaluations/multihoprag/multihop-siliconflow-smoke-001/`，其中 `evaluation-progress.json` 为 `completed`，120 题均已落盘。

| 指标 | 真实结果 |
| --- | ---: |
| 标准证据全覆盖率（仅 90 道非 null 题） | 63.33% (57/90) |
| 平均证据覆盖率 | 84.93% |
| 回答通过率（含需复核题） | 72.50% (87/120) |
| 回答通过率（仅独立判卷成功的 113 题） | 76.99% (87/113) |
| 无答案题正确拒答率 | 93.33% (28/30) |
| 人工复核率 | 5.83% (7/120) |
| 端到端 P50 / P95 | 69.30 s / 212.28 s |
| RAG 阶段 P50 / P95 | 40.40 s / 113.84 s |
| 回答生成 P50 / P95 | 16.08 s / 69.02 s |
| 判卷 P95 | 15.87 s |
| 查询改写触发率 | 0.83% (1/120) |
| 子问题拆分触发率 | 81.67% (98/120) |
| Auto-merging 触发率 | 12.50% (15/120) |

按题型拆分：comparison 为 21/30 通过，inference 为 27/30，null 为 28/30，temporal 为 11/30 通过、6/30 需复核。temporal 题同时也是证据全覆盖最弱的一类（13/30），因此不能把其失败简单归因于生成模型。

本轮的可观察结论如下：

1. 证据覆盖与回答正确性存在明显关联：在 90 道有标准证据的题中，证据齐全的 57 题有 45 题通过；部分证据的 26 题仅 14 题通过。样本 `1086` 预期三篇证据只召回两篇，系统保守拒答，独立判卷因参考答案为 `no` 判为失败。这是“召回覆盖不足导致答案失败”的直接证据。
2. 证据齐全也不保证答案正确：12 道已经召回全部标准资料的非 null 题仍失败。例如 `1014` 的资料齐全，但模型把应回答 `no` 的跨文档比较回答成了“是的”；这类问题应单独研究合成提示、比较/时间关系判定和判卷人工抽检，而不是继续盲目扩大候选集。
3. 无答案保护基本有效，但不是完美：题目 `113` 在资料未覆盖目标事实时仍依据外部常识给出答案，导致 null 类唯一的正常失败。后续可针对“问题要求的推导不在证据中”增强回答阶段的拒答约束。
4. 复杂链路的主要成本在 RAG 编排与子问题模型调用，而不是独立判卷。98/120 题触发子问题，端到端 P50 为 69.30 秒，说明下一轮需单独比较更严格的复杂度门槛或子问题数量，而不能仅通过替换回答模型解释延迟。

本轮存在 7 个必须人工复核的运行异常，不能算作模型错误：`1148` 超过 300 秒单题总时限；其后 `1087` 超时，`1088`、`1090`、`1094`、`1099`、`1103` 的新工作进程在返回结果前退出。该保护机制保证了整轮评测最终完成且保留前序结果，但 Windows 上重建工作进程的失败原因需要单独复现和修复。下一轮正式对比前，应先以这 7 题建立小回归集，验证超时后的工作进程重启与续跑；在修复前，本轮可作为真实烟测和问题定位依据，不能作为无误差的正式模型排行榜。

本轮没有在指标后直接修改 RAG 策略，因此不存在“优化后提升”的结论。下一次只改一个变量，建议优先固定这 120 题，把 temporal 类的证据候选数或来源过滤策略作为单变量，分别观察证据全覆盖率、回答通过率和 P95 延迟是否同步改善。

完成取证后已执行 `cleanup --dataset multihoprag --run-id multihop-siliconflow-smoke-001`：独立集合已删除，10,002 个带评测前缀的 L1/L2 父块已删除；默认业务集合未被操作。运行目录仍保留结果、配置与清理清单，供后续人工复核。

#### 全部问题题目清单

下面列出本轮所有非通过题。`coverage` 是标准证据覆盖比例；`review` 是运行异常，不纳入模型答错统计。

**证据未找全或覆盖不足（13 题）**

| case_id | 类型 | coverage | 题目 | 结果 |
| --- | --- | ---: | --- | --- |
| 1001 | comparison | 0.50 | Does 'The New York Times' article suggest that Lamar Jackson's effectiveness is diminished when regulated to pocket passing, while 'Sporting News' indicates that Arthur Smith has found success with Bijan Robinson's playing style for the Atlanta Falcons? | 缺少 NYT 证据，未确认 Yes |
| 1065 | comparison | 0.50 | Does the 'Sporting News' article about Manchester United's victory over Bayern indicate the same outcome for Manchester United's European competitions as the 'Sporting News' article about Alvaro Barreal's goal implies for Inter Miami's postseason running? | 缺少指定来源，未给出 no |
| 1012 | inference | 0.50 | Which company, featured in a TechCrunch article for reducing its workforce by 870 employees and depicted as an underdog in a legal battle against Google according to The Verge, is involved in both scenarios? | 未找全两篇来源，未答出 Epic Games |
| 1021 | inference | 0.67 | Who is the player that, according to articles from both 'Sporting News' and 'CBSSports.com', suffered an oblique injury affecting his ability to play in Week 14 and provided a chance for a rookie to shine in his potential absence during Week 12? | 缺少 CBSSports 证据，未答出 Kenneth Walker III |
| 1061 | inference | 0.00 | What are the entities associated with the Sporting News that not only modify betting lines based on the amount of money wagered and collected information but also offer promotional incentives and have the authority to return stakes in certain weather-affected events? | 没有召回证据，未答出 Sportsbooks |
| 100 | temporal | 0.50 | Has the approach of Sportsbooks in adjusting betting lines and odds, as reported by Sporting News after October 4, 2023, and before November 1, 2023, remained consistent? | 证据不足，未给出 no |
| 1006 | temporal | 0.50 | Between the report from The Independent - Life and Style on Travis Kelce's absence at a Taylor Swift concert published on November 25, 2023, and the Yardbarker report on Travis Kelce's potential performance against the Raiders published on December 24, 2023, was there a change in the type of events and activities involving Travis Kelce covered by the news sources? | 缺少一篇指定报道，未给出 no |
| 1016 | temporal | 0.67 | Between the TechCrunch report on OpenAI's launch of GPT-4 with vision published on September 28, 2023, and the TechCrunch report on OpenAI's development platform advancements published on November 30, 2023, was there a change in OpenAI's prioritization of ChatGPT as a development platform? | 证据不完整，回答 Yes 而标准答案为 No |
| 1038 | temporal | 0.67 | Between the report by The Age on the fairness of Google Search published on October 22, 2023, and the TechCrunch article discussing the class action antitrust suit against Google published later, was there a change in the portrayal of Google's competitive practices according to these news sources? | 证据不完整，回答发生变化而标准答案为 No |
| 1044 | temporal | 0.33 | Was there inconsistency in the portrayal of Sam Bankman-Fried's legal situation according to the 'Fortune' article on October 4, 2023, discussing his actions and the state of FTX, or the 'TechCrunch' article mentioning the prosecution's allegations against him? | 只找到部分证据，未给出 no |
| 1080 | temporal | 0.50 | Has the approach to presenting prop bets to bettors by Sporting News remained consistent between the report on NBA prop bets published on October 2, 2023, and the report on NCAAF bowl season prop bets published later? | 缺少 NCAAF 报道，未确认 Yes |
| 1082 | temporal | 0.50 | After Jerome Powell's aggressive interest rate hikes mentioned by 'Fortune' on October 6th, 2023, did 'Business Line' report on October 14th, 2023, suggest that central bankers' stance on interest rates was consistent or inconsistent with Powell's approach as reported by 'Fortune'? | 缺少 Business Line 报道，未答出 Consistent |
| 1086 | temporal | 0.67 | Was the narrative concerning Caroline Ellison's role and actions as the CEO of Alameda Research consistent between the report from Fortune published on October 4, 2023, which discussed Mark Cohen's claims about her, and the subsequent reports from The Verge regarding statements made by her? | 三篇证据只找到两篇，未给出 no |

**证据基本齐全但归纳或回答错误（12 题）**

| case_id | 类型 | coverage | 题目 | 结果 |
| --- | --- | ---: | --- | --- |
| 1014 | comparison | 1.00 | Did the Sporting News article that discusses Michigan's performance against Penn State with Jim Harbaugh suggest the same involvement of Harbaugh during the game compared to the Sporting News article regarding the sign-stealing scandal and his suspension? | 资料齐全，回答 Yes，标准答案 no |
| 1020 | comparison | 1.00 | Did the TechCrunch article imply that Sam Bankman-Fried's use of wealth was primarily for personal gain, while The Verge article focuses on his challenges in managing FTX and Alameda Research, and the second TechCrunch article alleges that his actions were driven by a desire for personal gain? | 资料齐全，回答 Yes，标准答案 no |
| 1023 | comparison | 1.00 | Does "The Independent - Life and Style" article on Jada Pinkett Smith and Will Smith's marriage suggest a different level of commitment to avoiding divorce compared to the stance on divorce expressed in a separate article? | 资料齐全，回答 Yes，标准答案 No |
| 1026 | comparison | 1.00 | Does the TechCrunch article on the antitrust suit against Google claim that Google's behavior towards news publishers is supportive, while the other TechCrunch article suggests that Google has no plans to implement additional measures on YouTube, indicating a difference in Google's approach? | 资料齐全，但回答无法确认，标准答案 no |
| 1052 | comparison | 1.00 | Does the article from The Verge suggest that Pokémon has successfully expanded its reach beyond its core audience and multimedia properties, while The Guardian indicates that Shayda has managed to connect with a universal audience? | 资料齐全，回答 Yes，标准答案 no |
| 1060 | comparison | 1.00 | Do 'Music Business Worldwide' and 'The Verge' both report that TikTok users engage in similar activities on the platform, with one describing music discovery and sharing and the other vlogging their lives? | 资料齐全，但回答无法确认，标准答案 Yes |
| 1066 | comparison | 1.00 | Do both articles from Sporting News suggest that bettors can find value in NBA prop bets by considering team performance, with one discussing player and team props and the other detailing team options? | 资料齐全，但认为只有一篇支持，标准答案 Yes |
| 1011 | temporal | 1.00 | Did the reporting style on players achieving first downs in Sporting News articles change between the article featuring Anthony Hankerson on October 7, 2023, and the one highlighting A.J. Dillon on December 3, 2023? | 资料齐全，回答发生变化，标准答案 no |
| 102 | temporal | 1.00 | Between the TechCrunch report on Meta's moderation bias problem suppressing Palestinian voices and the report on Meta's compliance with COPPA, was there a change in the nature of issues reported concerning Meta's platform practices? | 资料齐全，回答发生变化，标准答案 no |
| 1027 | temporal | 1.00 | Between the TechCrunch report on the situation at OpenAI involving Sam Altman and the subsequent report on his plans after departure, was there a change in the narrative regarding his professional intentions? | 资料齐全，回答发生变化，标准答案 No |
| 1039 | temporal | 1.00 | Between the report from 'The Independent - Life and Style' on September 26, 2023, regarding Taylor Swift and Travis Kelce, and the subsequent report on December 6, 2023, did the narrative about their relationship change? | 资料齐全，回答发生变化，标准答案 no |
| 1074 | temporal | 1.00 | Has the description of ChatGPT's capabilities by TechCrunch changed between the article published on September 28 and the subsequent article on November 30? | 资料齐全，回答与标准答案 no 相反 |

**资料未覆盖但没有正确拒答（1 题）**

| case_id | 类型 | coverage | 题目 | 结果 |
| --- | --- | ---: | --- | --- |
| 113 | null | 1.00* | Considering an article from The New York Times discussing Ron DeSantis's education policy and another from The Washington Post detailing his 2024 presidential strategies, what is the first letter of the state that DeSantis governs? | 题目要求的事实不在资料中，但模型依据外部常识回答了 Florida 的 F；应拒答 |

`null` 题的 `coverage=1.00*` 仅表示该题不要求标准证据，不代表检索到了可用证据。

**运行异常（7 题，需复跑，不算模型失败）**

| case_id | 类型 | 题目 | 异常 |
| --- | --- | --- | --- |
| 1148 | null | Considering information from The Verge and CNET on Watch Series 9, which feature is represented by a single letter commonly associated with a vital sign? | 超过 300 秒单题时限 |
| 1087 | temporal | Between Sporting News reports on the Michigan sign-stealing scandal and Michigan's game against Penn State, was reporting on Jim Harbaugh's absence consistent? | 超过 300 秒单题时限 |
| 1088 | temporal | After Fortune's report on the Gaza blockade and TechCrunch's report on its impact, was the portrayal by international aid groups consistent? | 超时后新工作进程退出 |
| 1090 | temporal | After The Age report on Google Search fairness, did TechCrunch remain consistent in reporting Google's competitive practices? | 工作进程在返回结果前退出 |
| 1094 | temporal | Between The Verge and TechCrunch reports on Epic v. Google, was there agreement on Epic Games' arguments? | 工作进程在返回结果前退出 |
| 1099 | temporal | Between two Engadget reports on the Steam Deck OLED published on November 9, was reporting on the release date inconsistent? | 工作进程在返回结果前退出 |
| 1103 | temporal | After Sporting News reported NBA MVP odds, did the later report on sportsbooks' use of data and analytics remain consistent? | 工作进程在返回结果前退出 |

## 实施计划与记录模板

### 阶段 1：EcomRetrieval 检索基线

状态：已完成真实 smoke 运行，运行编号 `ecom-smoke-003`。

目标：在独立评测集合中比较 BM25、Dense、Hybrid、Hybrid + rerank，输出 Recall@1/3/5、MRR、平均耗时和逐题结果。

实际集合、配置、指标、失败样本与结论见“EcomRetrieval 全量语料入库与检索基线”记录。评测结束后保留集合供本轮复核，完成 MultiHopRAG 后统一执行 `cleanup`。

### 阶段 2：MultiHopRAG 完整问答基线

状态：已完成 SiliconFlow 真实 smoke 运行；7 个运行异常样本待人工复核及进程重启回归验证。

目标：调用真实 `run_rag_graph()`，记录多证据召回覆盖率、答案正确率、无答案题拒答率、每阶段耗时和 RAG trace。

实际集合、模型配置、真实指标、典型失败样本与运行异常见“SiliconFlow DeepSeek-V4-Flash MultiHopRAG 烟测”记录。正式参数对比前先复跑 7 个 `review` 样本，避免将运行时故障混入模型质量结论。

### 阶段 3：长文与解析质量评测

状态：待实施。

目标：使用 Open RAG Benchmark 比较切分与父块合并；使用 OHR-Bench 做标准文本与 MinerU 噪声文本的对照实验。

### 阶段 4：企业级与多轮评测

状态：待实施。

目标：以 EnterpriseRAG-Bench 验证企业文档场景，以 ChatRAG-Bench 验证会话上下文、追问和不可回答问题。

## 每轮优化的固定记录项

每次执行或优化必须追加以下内容，保证面试时可以说明完整因果链：

1. 发现的问题：具体指标和失败样本，而非主观描述。
2. 假设：判断问题位于召回、排序、分块、解析、回答或拒答的理由。
3. 单项改动：代码、环境变量或配置的具体差异。
4. 验证方法：固定的数据集、样本范围、运行命令和指标定义。
5. 结果：优化前后真实数据、耗时和副作用。
6. 结论：保留、回退或继续实验，以及原因。

## 结果目录约定

每次评测写入 `output/rag-evaluations/<数据集>/<运行编号>/`：

```text
config.json       # 数据版本、集合、模型、切分与检索配置
results.jsonl     # 每题的标准答案、召回结果、回答、trace 与耗时
report.md         # 汇总指标、失败案例和结论
```

原始数据、评测结果和系统配置共同构成可复现证据；报告中不使用未实际运行得到的指标。

## 2026-08-14：EnterpriseRAG 评测实施启动

### 目标

在不触碰默认业务知识库的前提下，把 EnterpriseRAG 接入现有隔离评测工具。第一阶段先完成英文控制组的 10 题冒烟，确认数据适配、Markdown、三级分块、独立 Milvus 集合和评测结果落盘链路；中文翻译语料在英文链路稳定后再显式启用。

### 已确认事实

- EnterpriseRAG 文档位于 `tmp/rag-benchmarks/enterprise-rag-bench/data/documents/test.parquet`，字段为 `doc_id`、`source_type`、`title`、`content`。
- EnterpriseRAG 问题位于 `tmp/rag-benchmarks/enterprise-rag-bench/data/questions/test.parquet`，共 500 题，包含 `expected_doc_ids`、`gold_answer`、`answer_facts`。
- 无答案题的实际类型是 `info_not_found`，不是 MultiHopRAG 使用的 `null`。
- 当前环境候选池为 30，最终召回数量为 8；三级分块目标大小为 L1/L2/L3 `2400/1600/800` 字符。

### 本轮已实施改动

1. `backend/evaluation/metrics.py`：拒答指标兼容 `null`、`null_query` 和 `info_not_found`。
2. `backend/evaluation/runner.py`：EnterpriseRAG 支持独立语料准备、来源分层抽样、标题 BM25 难干扰选择、英文/中文语言分支、检索-only 和完整 RAG 模式。
3. `backend/evaluation/datasets.py`：增加 EnterpriseRAG Parquet 流式读取、Markdown 适配、标准证据保留、6,500 篇普通干扰配额、每题两篇难干扰和 SQLite FTS5 标题索引。
4. `backend/evaluation/translation.py`：增加显式启用的中文翻译缓存、温度 0、最多三次重试和数字/代码/URL 保真校验。
5. `scripts/run_rag_evaluation.py`：增加 `enterpriserag`、`--language`、`--corpus` 和 `--mode` 参数。
6. `tests/test_rag_evaluation.py`：增加 EnterpriseRAG 字段映射、来源抽样、标题 BM25 排除证据、翻译缓存和 `info_not_found` 指标测试。

### 当前验证状态

- Python 编译检查：已执行并通过：

  ```powershell
  uv run python -m py_compile backend\evaluation\datasets.py backend\evaluation\translation.py backend\evaluation\runner.py scripts\run_rag_evaluation.py
  ```

- 受影响单元测试：真实评测后重新执行，`tests/test_rag_evaluation.py` 共 30 项通过，耗时 0.173 秒。
- Milvus、Embedding、Rerank、回答模型和独立判卷模型：英文控制组已真实调用；中文翻译 API 本轮未调用。
- 性能指标和模型结论：已在下方“英文控制组真实完整 RAG 冒烟”中记录，指标仅代表 10 题 smoke 集。

### 当前风险

- SQLite FTS5 必须可用；如果当前 Python 构建没有 FTS5，难干扰选择会明确失败，不退化成未经说明的字符串匹配。
- 完整 EnterpriseRAG 首次准备仍需扫描 511,962 篇标题并生成约 7,222 篇代表性文档，不能与 10 题英文冒烟混为一轮。
- 中文翻译尚未执行；只有显式使用 `--language zh` 且翻译质量门通过后，才会生成中文主语料。

## 2026-08-14：EnterpriseRAG 英文控制组真实完整 RAG 冒烟

### 1. 评测目的与边界

本轮要验证的是“EnterpriseRAG 数据适配为 Markdown后，现有系统能否完成独立入库、三级分块、检索、Auto-merging、回答和独立判卷”的完整链路。先使用 10 道固定 smoke 题，是为了先确认链路可运行和结果可追溯；它不是 500 题正式排行榜，也没有在本轮修改线上 RAG 策略。

### 2. 实际配置

| 项目 | 实际值 |
| --- | --- |
| 数据集 | EnterpriseRAG-Bench，源文档 511,962 篇，问题 500 道 |
| 运行编号 | `enterpriserag-en-smoke-001` |
| 语言 / 语料 | 英文控制组 / representative / smoke |
| 独立 Milvus 集合 | `rag_eval_enterpriserag_enterpriserag_en_smoke_001` |
| Embedding | `BAAI/bge-m3`，1024 维 |
| 主模型、FAST_MODEL、GRADE_MODEL | `deepseek-ai/DeepSeek-V4-Flash` |
| Rerank | `Qwen/Qwen3-Reranker-4B`，实际开启 |
| 分块目标 | L1/L2/L3 = 2400/1600/800 字符 |
| 评测题 | 10 道，覆盖 10 类题型中的 smoke 样本 |

本轮实际选出 112 篇 Markdown：12 篇标准证据 + 100 篇普通干扰；生成 1,253 个 L3 叶子块和 989 个 L1/L2 父块。需要特别说明：本次 `manifest.json` 的 `hard_document_count` 为 `0`，因此本轮没有形成标题 BM25 难干扰对照，不能把本轮结果解释为“已验证难干扰场景”。

### 3. 实际执行命令

```powershell
uv run python scripts/run_rag_evaluation.py prepare `
  --dataset enterpriserag `
  --profile smoke `
  --run-id enterpriserag-en-smoke-001

uv run python scripts/run_rag_evaluation.py evaluate `
  --dataset enterpriserag `
  --run-id enterpriserag-en-smoke-001

uv run python scripts/run_rag_evaluation.py cleanup `
  --dataset enterpriserag `
  --run-id enterpriserag-en-smoke-001
```

### 4. 汇总结果

| 指标 | 真实结果 |
| --- | ---: |
| 题目数 | 10 |
| 标准证据全覆盖率 | 100% |
| 平均证据覆盖率 | 100% |
| 回答通过率 | 60%（6/10） |
| 无答案题正确拒答率 | 100%（1/1） |
| 人工复核率 | 20%（2/10） |
| 端到端 P50 | 41.00 秒 |
| RAG 阶段 P50 | 24.08 秒 |
| 回答生成 P50 | 12.61 秒 |
| 查询改写触发率 | 10% |
| 子问题拆分触发率 | 30% |
| Auto-merging 触发率 | 70% |

这些数据说明本轮链路确实跑通，但样本量只有 10，严格来说不能据此宣称模型质量稳定；尤其是 20% 的人工复核率会明显影响小样本通过率的解释。

### 5. 逐题成绩

`coverage` 是标准证据覆盖比例；`rank` 是标准证据在最终来源列表中的排名；`review` 不直接计为答错。

| case_id | 题型 | coverage | 标准证据排名 | 判定 | 端到端耗时 |
| --- | --- | ---: | --- | --- | ---: |
| `qst_0001` | basic | 1.00 | 1 | pass | 33.522 s |
| `qst_0431` | completeness | 1.00 | 1, 2 | review：判卷请求超时 | 191.909 s |
| `qst_0411` | conflicting_info | 1.00 | 2, 1 | pass | 28.530 s |
| `qst_0381` | constrained | 1.00 | 1, 2 | fail：事实极性表达错误 | 63.604 s |
| `qst_0471` | high_level | 1.00* | 无 | fail：误拒答 | 23.745 s |
| `qst_0481` | info_not_found | 1.00* | 无 | pass：正确拒答 | 58.106 s |
| `qst_0301` | intra_document_reasoning | 1.00 | 1 | pass | 40.997 s |
| `qst_0451` | miscellaneous | 1.00 | 1 | pass | 37.227 s |
| `qst_0341` | project_related | 1.00 | 1, 9 | review：判卷认为验证指标不完整 | 121.769 s |
| `qst_0176` | semantic | 1.00 | 1 | pass | 44.545 s |

`qst_0471` 的 `1.00*` 是指标实现对“无标准证据列表”题目的默认值，不代表实际召回到了证据；该题的运行轨迹是 `no_knowledge`，实际召回 0 篇。这个案例暴露出数据集的 `expected_doc_ids` 与“参考答案存在但标准证据为空”的口径不一致，后续需要单独修正评测指标或数据适配。

### 6. 失败与复核样本分析

- `qst_0381`：两篇标准证据均在第 1、2 位，说明召回和排序没有丢失关键资料。回答把参考答案中的“过于宽松的 hold-down/anti-flap 和 rollback 护栏”表达成“不足的护栏”，语义方向相反；这是回答合成阶段的事实极性问题，不是召回问题。
- `qst_0471`：系统因为没有检索到资料而拒答，但标准答案要求回答 Redwood Inference 的使命。这是“误拒答”问题，且当前 coverage 指标无法识别它，不能只看证据覆盖率判断回答质量。
- `qst_0431`：回答已经生成，证据排名为 1、2，但独立 `GRADE_MODEL` 请求因 SiliconFlow 读取超时（90 秒）而无法解析，结果必须人工复核，不能算模型答错。
- `qst_0341`：回答正确覆盖了限流原因和临时策略，但独立判卷认为 SLO 验证指标没有完整覆盖 error-budget、availability、p95/p99、5xx、429 和 shed_rate；需人工确认是“部分正确”还是“判卷标准过严”。

### 7. 清理与隔离验证

`cleanup` 返回：独立集合 `dropped`，删除评测父块 `989` 个，且 `manifest.json` 已记录 `cleanup_completed=true`。清理后重新查询默认业务集合 `tutorial_verify_embeddings`：集合存在，`row_count=0`。因此本轮没有把 EnterpriseRAG 数据写入默认业务知识库。

### 8. 本轮结论

本轮保留为英文控制组基线，不做优化结论。可以确认的是：Markdown 适配、独立集合、三级分块、真实 RAG 链路、独立判卷、逐题结果和幂等清理均可运行；暂时不能确认的是难干扰场景效果、中文语料效果和 500 题稳定指标。下一步应先人工复核 `qst_0431`、`qst_0341`，修正 `qst_0471` 的“参考答案存在但标准证据为空”口径，再单独运行 hard 干扰配置或中文翻译控制组。

## 2026-08-14：EnterpriseRAG 中文主语料准备首次失败与修复

### 发现的问题

按照英文控制组的同一题目、同一代表性语料范围，首次执行中文准备：

```powershell
uv run python scripts/run_rag_evaluation.py prepare `
  --dataset enterpriserag `
  --profile smoke `
  --language zh `
  --corpus representative `
  --mode rag `
  --run-id enterpriserag-zh-smoke-001
```

首篇文档在翻译质量门失败。原始错误为：`missing_protected_token:30 minutes`、`missing_protected_token:24h`、`missing_protected_token:60 minutes`。翻译接口已正确返回中文内容，但把英文时间单位自然地译为“分钟”“小时”；旧校验把所有“数字 + 单位”当成必须逐字符保留，因此误将正常中文时间表达识别为数据损坏。

### 根因、修复与验证

根因是 `validate_translation()` 只有单一的文本包含判断，无法区分必须原样保留的代码、URL、技术单位（如 `MiB`、`ms`、`%）与可以自然翻译的时间单位。

修复后，翻译质量门保留以下不变量：

1. 代码、URL、数字、技术单位仍要求原样保留。
2. 时间表达允许 `30 minutes -> 30 分钟`、`24h -> 24 小时`，但数值与时间量级必须匹配。
3. 翻译提示版本由 `enterprise-zh-v1` 升为 `enterprise-zh-v2`，缓存键随之变化，避免旧规则下的缓存复用。

新增回归用例覆盖中文时间单位等价和缺失时间单位两种结果。实际验证：翻译相关 3 项测试通过；全套单元测试 53 项通过；`py_compile backend/evaluation/translation.py` 通过。

### 数据隔离结论

失败发生在第一篇 Markdown 的翻译与质量校验阶段。译文只会在质量门通过后才写入翻译缓存，向量写入位于全部 Markdown 生成之后，因此本次失败没有写入 Milvus、PostgreSQL 父块或默认业务集合。后续重跑使用新的编号 `enterpriserag-zh-smoke-002`，保留首次失败编号供审计。

### 后续真实重试与当前状态

首次修复后继续真实执行中文准备，发现问题并不只来自时间单位校验：部分企业长文在整篇请求或 2,000/1,000 字符请求下会触发 SiliconFlow 的读取超时；另有文档会遗漏 Linear URL 或把 HTTP `429s` 当作可翻译描述。为避免把异常译文入库，所有问题均由质量门阻断，没有采用“翻译失败后直接保留英文”之类的隐蔽降级。

已实施的可恢复性改动如下：

1. 时间单位允许语义等价翻译，技术单位、代码、URL 和数字仍保持硬校验。
2. 文档按换行优先分段，最终分段上限收紧为 500 字符；每段独立调用、校验和重试。
3. 对代码、URL、技术单位和数字使用 `[[RAG_KEEP_n]]` 占位符保护；模型返回后恢复原值，再做最终校验。URL 尾部中英文句号不计入 URL 本体。
4. 写入整篇缓存前，先把已通过的分段写入 `translation-cache/segments/`；重试不会重做已成功分段。
5. 翻译使用独立的 `TRANSLATION_TIMEOUT_SECONDS`（默认 150 秒），不改变线上回答和判卷的超时；翻译并发降为“1 篇文档 x 2 个分段”，避免 API 排队放大超时。

实际探针调用证明 `deepseek-ai/DeepSeek-V4-Flash` 的小文本翻译可用，因此问题定位为长文分段吞吐与服务端慢响应，而不是密钥、模型名称或 API 整体不可用。

截至暂停时，中文派生语料已完成 17/112 篇整篇缓存、102 个分段缓存。所有缓存都已经通过保真校验，但尚未完成全部 112 篇，因此没有进入问题/参考答案翻译、Markdown 分块、Embedding、Milvus 入库或完整 RAG 评测。默认业务知识库没有受到本轮写入影响。

## 2026-08-14：整篇翻译策略的真实探针与修正

### 发现的问题

前一轮把长文按最多 500 字符拆分，虽然便于单段重试，但一篇文档会产生 2～17 次模型请求。当前已完成样本的文档长度为 821～6380 字符，平均约 3888 字符；因此提出过“利用模型 1M 上下文整篇发送”的方案。

### 决策与理由

不能仅凭上下文上限做决定。对当前语料中最长的约 6380 字符文档进行真实整篇请求，等待超过 3 分钟仍无响应；该请求随后被人工中止，没有计入成功。这个结果说明当前瓶颈是服务端读取/生成延迟，而不是“文档放不进上下文”。

因此采用“短文整篇、长文分段”的自适应策略：默认不超过 1000 字符的文档整篇发送，超过阈值的文档继续按最多 500 字符分段。1000 字符是当前服务条件下的保守工程阈值，不宣称是模型理论上限，后续可通过真实探针重新调整。

### 实际改动

- `TranslationClient.translate()` 根据文档长度选择 `whole_document` 或 `segmented`，实际策略写入缓存结果。
- 保留已有整篇缓存和分段缓存，不删除历史产物；已有通过校验的整篇缓存继续复用。
- 评测 metadata 记录 `translation_strategy=adaptive_whole_document` 和阈值，便于人工核查。
- 单元测试验证超过 500 字符但不超过阈值的文档只发起一次请求。

### 当前边界

模型支持 1M 上下文不等于无限输出或请求一定不超时。后续继续翻译时，长文走可恢复分段；若要提高整篇阈值，必须先做单篇真实探针并记录耗时、重试次数和质量校验结果，不能把模型规格直接当作服务性能结论。

本阶段的回归验证结果：翻译相关 12 项测试、全套测试待本次改动后重新执行；整篇最长文档真实探针超过 3 分钟未返回并已中止。本阶段没有可报告的中文 RAG 指标，不能把“已有缓存翻译成功”写成检索或问答性能提升。

## 2026-08-14：旧分段译文的整篇重译

### 发现的问题

旧中文缓存虽然通过了数字、URL、代码和技术单位等硬事实校验，但文档曾按最多 500 字符独立翻译。该方式无法保证邮件线程中的指代、术语和前后行动项保持一致，因此旧译文不能继续作为中文评测语料。

### 处理方式

1. 保留旧 `translation-cache`，仅用于审计，不删除也不再作为入库来源。
2. 将旧缓存中 18 篇 `kind=document` 的 `source_hash` 与英文控制组 Markdown 的 SHA-256 匹配，得到明确的重译范围。
3. 两个子智能体按完整文档进行翻译，分别输出到独立目录；执行过程中外部子智能体服务返回 `403 Forbidden`，原因为额度不足。已产生的候选译文保留，不将额度错误伪装为翻译成功。
4. 对缺失文档补齐整篇译文，并把 18 个唯一文件汇总到 `manual-codex-retranslation/canonical/documents/`。存在重复候选的 5 篇文档保留在子智能体目录供人工对照，canonical 版本优先选择 `agent-a`，缺失时选择 `agent-b`。

### 核验结果

- Canonical 文档数：18。
- 原文-译文数字缺失检查：18/18 通过。
- 原文-译文 URL 缺失检查：18/18 通过。
- 邮件地址校验脚本在原始资料的文本 `\\n` 换行上产生 `nxxx@example.com` 假阳性，需在后续修复解析器后再做一次自动核验；这不是发现译文丢失邮箱的证据。

### 当前结论

中文语料的整篇翻译可行，但这 18 篇只是“重译质量与链路”样本，不是完整中文测试集。尚未进行问题/参考答案中文化、三级分块、Embedding、独立 Milvus 集合写入或 RAG 评测，因此不能报告中文检索和问答性能，也不能与英文控制组做性能结论。

## 2026-08-14：继续补充中文整篇译文（第 2 批）

### 范围与方式

在旧缓存中的 18 篇完成重译后，继续从英文控制组的 112 篇代表性 Markdown 中补充未覆盖文档。本批完成 5 篇，写入 `manual-codex-retranslation/next-batch/documents/`；校验通过后复制到 `canonical/documents/`。原始文件名保持不变，便于从译文回溯至英文控制组。

本批文档为：

1. `enterprise__dsid_c91a34ed8dcb44aeacf7713c5aab8dea.md`
2. `enterprise__dsid_b2566b591e7f4a499a0b3dac2104891a.md`
3. `enterprise__dsid_80fbf16579e74fe48e1e5bd328acd066.md`
4. `enterprise__dsid_178fc2a568c24f9d913b74c25ca28077.md`
5. `enterprise__dsid_a2b925345cd149baa2c41c84eacde8e1.md`

每篇先通读全文，再进行整篇翻译；不使用外部翻译 API，也不复用旧分段译文。URL、邮箱、命令、代码、状态码、组织 ID、产品名和数字必须保留。自然语言的时间单位允许翻译，例如 `30min` 可表达为“30 分钟”，但数值和量级不能改变。

### 核验结果

对 5 篇译文逐一执行原文-译文对照：文件名对应、URL、邮箱、行内代码、有效数字和 Markdown 标题结构均通过，结果为 5/5。URL 对照去除原文句末的 `.`、`,`、`;`、`:` 后比较，避免把语法标点误判为 URL 内容。首次宽松数字正则把 `a11y` 的后半段误识别为 `1y`，已在人工核查中确认这不是事实值；因此本批没有因该正则误报修改文本。

### 当前状态与边界

- canonical 译文累计：23/112 篇；仍有 89 篇尚未成为规范语料。
- 本批新增内容只在 `tmp/rag-benchmarks/` 下保存，未写入 Milvus、PostgreSQL、Redis 或默认业务知识库。
- 并行子智能体的候选输出与 canonical 隔离；每篇仍需由主流程执行相同校验后才可合并。
- 尚未翻译题目和参考答案，尚未执行中文分块、Embedding、入库或 RAG 评测。因此本批仅证明语料准备继续推进，不能报告任何中文性能指标。

## 2026-08-14：并行候选验收与结构修复

### 候选对照

为继续补充 112 篇代表性语料，三个子智能体被分配到相互隔离的候选目录。`agent-c` 和 `agent-e` 因任务分配重叠，分别产出了同一组 5 篇候选译文；未将两组文件同时复制入 canonical。两组均通过 URL、邮箱、行内代码、有效数字和标题数量校验后，人工抽样核查其中一篇 CRM 记录，并对 5 篇统计中文字符和拉丁字符数量。

`agent-e` 在 5 篇中均保留了相同的事实和 Markdown 结构，且自然语言中文化更完整，因此选择 `agent-e/documents/` 的版本进入 canonical；`agent-c/documents/` 保留为审计对照。该选择仅说明译文可读性取舍，不是检索或回答效果比较。

### 结构问题与修复

`agent-d` 的 5 篇候选中，`enterprise__dsid_1980f45c3fb4455f943a2b030892ae90.md` 首次检查发现原文有 12 个 Markdown 标题、译文有 14 个。该文件没有进入 canonical；已要求只修正标题层级。修复后重新执行全量对照，5 篇均满足 URL、邮箱、行内代码、有效数字和标题数量校验，其中该文件恢复为 12/12，随后才合并。

### 当前状态

- 已验收的 canonical 译文：43/112 篇，剩余 69 篇未成为规范语料。
- `agent-f/documents/` 正在处理后续 5 篇候选；在主流程验收前不计入上述数字，也不参与入库。
- 全部操作仅创建或复制 `tmp/rag-benchmarks/` 下的 Markdown；未写入 Milvus、PostgreSQL、Redis 或业务知识库。

## 2026-08-14：后续两批候选验收

### 验收范围

`agent-c/batch-2/documents/` 与 `agent-e/batch-2/documents/` 各提供 5 篇互不重叠的整篇译文，共 10 篇。每篇再次对照英文控制组，检查 URL、邮箱、行内代码、有效数字和 Markdown 标题数量。`agent-c` 的 5 篇全部一次通过；`agent-e` 的 4 篇一次通过，另 1 篇经过 URL 边界复核后通过。

### 原始文本的 URL 假阳性

`enterprise__dsid_2978d0a683aa494b9e72ec6b3d8142de.md` 的原始内容把换行写成字面量 `\\n`。简单 URL 正则把 `https://app.redwood.ai/harness/recipes/streaming-probe\\n\\nNext` 误识别为一个 URL，导致出现“URL 缺失”报警。将字面量 `\\n` 视为 URL 边界后，原文的两个 URL 为 `https://hubspot.com/deals/12345` 和 `https://app.redwood.ai/harness/recipes/streaming-probe`，译文均完整保留。

这是评测数据格式造成的核验脚本假阳性，不是译文漏掉 URL，也没有在本轮修改项目的翻译或入库代码。以后自动化验收需要在 URL 提取前处理文本形式的换行，避免重复误报。

### 合并结论

10 篇均通过后已复制到 `canonical/documents/`，规范译文累计为 43/112。候选目录仍保留，不删除审计材料。所有数据继续处于本地临时目录，尚未进入分块、向量化、Milvus 入库或中文 RAG 评测阶段。

## 2026-08-14：中文代表性语料全部完成

### 完成结果

后续批次继续使用并行子智能体分工翻译，主流程逐批执行事实和 Markdown 结构核验。最终英文控制组与中文 canonical 的文件名集合均为 112 个：缺失 0 个，额外 0 个，覆盖率 112/112。所有中文译文仍位于 `tmp/rag-benchmarks/`，尚未进入任何向量库。

最终核验项目包括：URL（忽略句末语法标点）、邮箱、行内代码、数字、正文 Markdown 标题数量；包含 fenced code 的文档还比较了规范化文本换行后的围栏数量和围栏内容。

### 实际发现并修复的问题

1. `enterprise__dsid_3fc6af945ae64d4ca6c193ad2ecc42c0.md` 将两个 `redwood.ai` 邮箱误写成 `redwood.com`，在合并前已恢复原域名。
2. `enterprise__dsid_1980f45c3fb4455f943a2b030892ae90.md` 首次标题数量为 14，而原文为 12；修复后按 12/12 通过才合并。
3. `enterprise__dsid_c4af7e58ecf94f38aa38330244d2d07e.md` 的代码围栏内部曾保留字面量 `\\n`，修复为真实换行后，3 个围栏逐段与原文规范化结果一致。
4. `enterprise__dsid_f2c2361ee1584f11a261e8e5f88e326c.md` 曾在正确内容后追加原文不存在的手册，已删除追加内容并恢复标题 1/1。
5. `enterprise__dsid_faa1e9f5ce8e4c1e978fb00a50b8bb0c.md` 曾生成占位文本，已整篇重译；随后补回原文要求的 `logan_wright@redwood.com`，并保留 `logan.wright@redwood.com` 的点号版本。

这些样本说明“数字和 URL 校验通过”不能代替完整人工复核：邮箱域名、Markdown 结构、代码围栏和译文是否仍对应原文，都可能单独出错。所有失败候选均在 canonical 外修复，只有复核通过后才复制进入规范目录。

### 当前边界

中文文档语料已经准备完成，但题目和参考答案尚未中文化，三级分块、Embedding、独立 Milvus 入库和中文 RAG 评测尚未开始。本次完成的是语料准备，不是中文检索或回答性能结果；默认业务知识库仍未写入。

## 2026-08-14：中文 canonical 接入评测准备器

### 发现的问题

`prepare --language zh` 原先会把选中的英文文档再次发送给翻译 API。这样会绕过已经人工整篇翻译并复核的 canonical 版本，也会让同一个 run 的文档来源随 API 返回结果变化。中文代表性语料已经达到 112/112 后，继续重复翻译没有评测价值，反而增加成本和不确定性。

### 实施改动

1. `backend/evaluation/datasets.py` 增加 `load_enterprise_canonical_markdown()`，按 `doc_id` 对应的稳定文件名读取 `canonical/documents/`。
2. canonical 文档缺失或为空时立即失败，并列出最多 5 个问题 ID；不允许静默回退到英文正文或旧分段缓存。
3. `backend/evaluation/runner.py` 的中文准备流程改为直接使用 canonical Markdown。文档翻译策略记录为 `canonical_manual_retranslation`，同时记录英文源哈希、实际 Markdown 哈希和来源目录。
4. 翻译客户端只负责题目和参考答案的中文化，并在 cases 中保留 `source_question`、`source_reference_answer` 与 `translation_source`。客户端在准备结束时显式关闭；翻译结果仍使用现有缓存机制。

### 离线验证

使用本地 EnterpriseRAG 数据执行适配探针，没有调用模型、Embedding 或数据库：

| 检查项 | 结果 |
| --- | ---: |
| 原始问题总数 | 500 |
| smoke 题数 | 10 |
| 标准证据文档 | 12 |
| 普通干扰文档 | 100 |
| 选中文档总数 | 112 |
| canonical 中文文档 | 112 |
| 缺失或空文件 | 0 |

按来源抽样配额与实际数量完全一致：`confluence=1`、`fireflies=2`、`github=1`、`gmail=24`、`google_drive=5`、`hubspot=3`、`jira=1`、`linear=7`、`slack=56`。新增评测模块测试 42 项全部通过，耗时约 1.1 秒。

### 当前边界

本轮只完成中文文档来源接入和离线一致性验证；尚未翻译并落盘中文题目/参考答案，尚未分块、Embedding、Milvus 入库或执行中文 RAG。下一步需要先用可缓存翻译完成 10 道 smoke 题，再人工核查题目、答案和证据映射，之后才进入独立集合的真实准备。

## 2026-08-14：中文 EnterpriseRAG smoke 真实准备与评测

### 准备命令与配置

```powershell
uv run python scripts/run_rag_evaluation.py prepare `
  --dataset enterpriserag `
  --profile smoke `
  --language zh `
  --corpus representative `
  --mode rag `
  --run-id enterpriserag-zh-smoke-canonical-001
```

本轮使用 `deepseek-ai/DeepSeek-V4-Flash` 作为主模型、FAST_MODEL 和 GRADE_MODEL，使用本地 `BAAI/bge-m3` 生成 1024 维 Dense 向量，Rerank 配置为 `Qwen/Qwen3-Reranker-4B`。配置文件只记录模型名和地址，不记录密钥。

### 准备结果

- 题目翻译：10/10；参考答案翻译：10/10；结果均进入可恢复缓存，并在 cases 中保留英文原文。
- 中文 canonical 文档：112 篇；L3 叶子块：886；写入 Milvus 的实际查询行数：886。
- PostgreSQL 父块：701，其中 L1=263、L2=438；Redis 使用同一 run 前缀的父块缓存。
- 独立集合：`rag_eval_enterpriserag_enterpriserag_zh_smoke_canonical_001`。
- 默认业务集合 `tutorial_verify_embeddings` 查询行数仍为 0；本轮没有污染默认知识库。

### 评测命令与汇总结果

```powershell
uv run python scripts/run_rag_evaluation.py evaluate `
  --dataset enterpriserag `
  --run-id enterpriserag-zh-smoke-canonical-001
```

| 指标 | 结果 |
| --- | ---: |
| 题目数 | 10 |
| 证据全覆盖率 | 0.8889 |
| 平均证据覆盖率 | 0.9000 |
| 回答判卷通过率 | 0.7000 |
| 无答案题拒答正确率 | 1.0000 |
| 人工复核率 | 0.1000 |
| 端到端耗时 P50 | 48.53 s |
| RAG 耗时 P50 | 26.75 s |
| 生成耗时 P50 | 7.25 s |
| 改写触发率 | 0.0000 |
| 子问题触发率 | 0.3000 |
| Auto-merging 触发率 | 0.7000 |

Rerank 在 8/10 条主链路记录中实际生效；3 道复杂题走了并行子问题分支，trace 字段不在主记录上重复汇总，不能简单把空字段当成 Rerank 失败。

### 逐题问题记录

| 题目 | 判定 | 证据 | 结论 |
| --- | --- | --- | --- |
| `qst_0381` | `review` | 1.00 | 回答额外写出 `SUP-25322`，并把“由 2026-02-07”表达成“2026-02-07 前”，需要人工确认是否属于事实扩展和日期语义变化。 |
| `qst_0471` | `fail` | 0.00 | 题目有参考答案但数据没有标准证据 ID，系统正确表现为 `no_knowledge` 后拒答，属于数据集证据标注缺失暴露出的误拒答。 |
| `qst_0341` | `fail` | 1.00 | 召回到证据但回答引入未被参考答案支持的 RPS、自动回滚条件，遗漏 `PROXIMA-ENT-014`、Retry-After、p95 和 error-budget 等要求，属于合成阶段事实取舍问题。 |

其余 7 道题被独立 `GRADE_MODEL` 判为 `pass`。所有判卷请求均使用温度 0；`review` 不计入通过，必须人工复核。

### 评测口径修正

发现原实现把“标准证据为空”的所有题目都算作满覆盖，导致 `qst_0471` 虚高。现在只有 `null`、`null_query`、`info_not_found` 拒答题允许空证据按无需证据处理；其他可回答题没有标准证据时记为 0 覆盖，并支持从既有 checkpoint 重算报告，不需要重复模型调用。

本轮结果保留在 `output/rag-evaluations/enterpriserag/enterpriserag-zh-smoke-canonical-001/`，尚未执行 cleanup，便于人工核查；该集合和对应父块不属于默认业务知识库。

## 2026-08-15：英文正式评测运行器 Phase 0

### 实际离线结果

- 使用原始英文问题 Parquet 执行固定划分探针：500 题拆分为分析集 300 题、盲态验证集 200 题。十类题型分别满足 PRD 中的 `105/70`、`75/50`、`24/16`、`24/16`、`18/12`、四组 `12/8` 和 `6/4` 配额。
- 新运行器在 `case-split.json` 中固定题目 ID 与按题型清单，并把文件 SHA-256 写入 corpus manifest。分析或验证实验只能引用该冻结清单。
- corpus 准备与实验执行现已分离：`evaluate --evaluation-id` 在同一 corpus 目录下创建独立实验产物，保存 `evaluation-config.json`、来源 manifest 哈希、case set、模型与检索配置快照、代码版本和唯一 `changed_variable`。既有 ID 的配置不同会拒绝覆盖。
- 自动报告增加按题型聚合；验证集失败题号不出现在自动报告或人工复核文件，完整机器审计只保留在受控逐题 JSONL。Rerank 只有全题实际成功时才进入策略比较。
- 离线验证真实执行：`uv run python -m unittest discover -s tests` 共 70 项通过（约 1.3 秒）；`py_compile` 与 `git diff --check` 通过。

### 当前边界

本节不包含 Milvus 入库、检索或回答性能指标。上述结果只证明正式评测控制面和 mock 契约可运行；下一步才是使用新的独立集合运行 10 题英文 smoke。

## 2026-08-15：英文 10 题 smoke（改造后真实运行）

### 实际命令与产物

```powershell
uv run python scripts/run_rag_evaluation.py prepare `
  --dataset enterpriserag --run-id enterpriserag-en-smoke-formal-001 `
  --profile smoke --language en --corpus representative --mode rag

uv run python scripts/run_rag_evaluation.py evaluate `
  --dataset enterpriserag --run-id enterpriserag-en-smoke-formal-001 `
  --evaluation-id enterpriserag-en-smoke-formal-001-retrieval `
  --case-set all --mode retrieval

uv run python scripts/run_rag_evaluation.py evaluate `
  --dataset enterpriserag --run-id enterpriserag-en-smoke-formal-001 `
  --evaluation-id enterpriserag-en-smoke-formal-001-rag `
  --case-set all --mode rag
```

- 语料准备完成时间约 01:37（本地时间），未重复向量化的两轮实验分别写入独立 `evaluations/` 子目录。
- 原始英文题目 10 道、标准证据 12 篇、普通干扰 100 篇；L3 叶子块 1,253，L1/L2 父块 989。
- corpus manifest：`output/rag-evaluations/enterpriserag/enterpriserag-en-smoke-formal-001/manifest.json`；来源 manifest SHA-256 为 `37c343fb3e51ba954b1e80df8b4906150b9678e6619195a4422a451588be4851`。
- 独立集合：`rag_eval_enterpriserag_enterpriserag_en_smoke_formal_001`。最终用 Milvus `query(count(*))` 核验 1,253 行；默认业务集合 `tutorial_verify_embeddings` 同时为 0 行。`get_collection_stats` 的 Bounded 统计短暂显示 0，不能代替一致性查询。

### 检索 smoke 真实结果

结果目录：`output/rag-evaluations/enterpriserag/enterpriserag-en-smoke-formal-001/evaluations/enterpriserag-en-smoke-formal-001-retrieval/`。

| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50(s) | P95(s) | 失败 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.7000 | 0.7000 | 0.8000 | 0.7250 | 0.0054 | 0.0341 | 0 |
| Dense | 0.8000 | 0.8000 | 0.8000 | 0.8000 | 0.1247 | 5.5104 | 0 |
| Hybrid | 0.8000 | 0.8000 | 0.8000 | 0.8000 | 0.1292 | 0.1575 | 0 |
| Hybrid + Rerank | 0.8000 | 0.8000 | 0.8000 | 0.8000 | 0.9856 | 4.3743 | 0 |

Rerank 配置为 `Qwen/Qwen3-Reranker-4B`，10/10 题实际成功，因此作为有效比较组保留。该表只是 10 题 smoke 观察，不是正式基线。

### 完整 RAG smoke 真实结果

结果目录：`output/rag-evaluations/enterpriserag/enterpriserag-en-smoke-formal-001/evaluations/enterpriserag-en-smoke-formal-001-rag/`。

| 指标 | 真实结果 |
| --- | ---: |
| 题目数 | 10 |
| 证据全覆盖率 | 0.7778 |
| 平均证据覆盖率 | 0.8000 |
| 回答判卷通过率 | 0.6000 |
| 无答案题拒答正确率 | 1.0000 |
| 人工复核率 | 0.3000 |
| 端到端耗时 P50 | 30.3088 s |
| RAG 耗时 P50 | 17.2779 s |
| 生成耗时 P50 | 8.0783 s |
| 改写触发率 | 0.0000 |
| 子问题触发率 | 0.5000 |
| Auto-merging 触发率 | 0.4000 |

人工复核队列真实包含：`qst_0381`（判卷 `review`）、`qst_0341`（判卷 `review`）、`qst_0176`（判卷 `review` 且 `APIConnectionError`）。完整理由和逐题 trace 保存在 `manual-review.jsonl` 与 `results.jsonl`；没有把 `review` 猜测成通过或失败。

### 本轮意味着什么

这轮证明改造后的英文 smoke 可以在一次入库上复用多个实验、结果不互相覆盖、Rerank 成功条件有明确统计、题型汇总和人工复核可回溯，并且默认业务集合没有被写入。它没有证明 500 题正式指标，也没有触发任何 RAG 优化；中文 smoke 仍只作为历史审计记录。

### Smoke cleanup

```powershell
uv run python scripts/run_rag_evaluation.py cleanup `
  --dataset enterpriserag --run-id enterpriserag-en-smoke-formal-001
```

首次 cleanup 删除独立集合和 989 个父块；重复执行成功且删除父块数为 0。清理后再次核验默认集合为 0 行、`rag_eval_enterpriserag_enterpriserag_en_smoke_formal_001` 不存在。运行目录及两轮 evaluation 结果保留用于审计，未清理中文历史运行。

## 2026-08-15：SiliconFlow Embedding 真实探针

### 目的与边界

在正式语料入库前验证 `.env` 的 SiliconFlow Embedding 配置。本次只发送 2 条英文文本，不写入 Milvus、不创建评测集合、不产生正式指标。

### 实际配置（不含密钥）

- provider：`siliconflow`
- model：`BAAI/bge-m3`
- base URL：`https://api.siliconflow.cn/v1`
- API key：已从 `.env` 读取，未打印、未写入任何报告

### 实际结果

| 检查项 | 结果 |
| --- | ---: |
| 输入文本数 | 2 |
| 返回向量数 | 2 |
| 向量维度 | 1024 |
| 非零向量数 | 2 |
| 请求耗时 | 0.629 s |

探针通过，说明远程 Embedding 接口、模型名和 Milvus 需要的 1024 维配置可以进入正式准备阶段。该结果不是检索或 RAG 性能指标。

## 2026-08-15：Representative 正式语料准备完成

### 实际运行

- run ID：`enterpriserag-en-representative-sf-001`
- 独立集合：`rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`
- 使用 SiliconFlow `BAAI/bge-m3`，1024 维；本轮未运行检索或 RAG。

### 核验结果

| 检查项 | 结果 |
| --- | ---: |
| 标准证据文档 | 722 |
| 普通干扰文档 | 6,500 |
| 文档总数 | 7,222 |
| L3 叶子块 | 81,402 |
| L1/L2 父块 | 62,987 |
| 固定题目 | 500 |
| 分析集 / 验证集 | 300 / 200 |
| case-split SHA-256 | 与 manifest 匹配 |
| Milvus 实际行数 | 81,402 |
| 父块实际行数 | 62,987 |
| 默认集合 `tutorial_verify_embeddings` | 0 行 |

Representative 已冻结，运行目录、manifest、case split 和集合保留给后续实验复用；本结果不代表检索或回答性能。

## 2026-08-15：Representative 500 题检索正式基线

### 实际运行

- corpus run：`enterpriserag-en-representative-sf-001`
- evaluation ID：`baseline-retrieval-001`
- 语料：Representative；题集：500 题（分析 300、验证 200）
- 模式：retrieval-only，不调用回答模型或 GRADE_MODEL
- 逐题结果：`results.jsonl` 共 2,000 条（四种策略各 500 条）；验证集详情在 `case-review.md` 和自动报告中隐藏

### 汇总结果

| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50(s) | P95(s) | 失败 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.6440 | 0.7540 | 0.7840 | 0.7032 | 0.0038 | 0.0051 | 0 |
| Dense | 0.5960 | 0.6740 | 0.7140 | 0.6420 | 0.1199 | 0.1478 | 0 |
| Hybrid | 0.6560 | 0.7640 | 0.8040 | 0.7163 | 0.1225 | 0.1388 | 0 |

Rerank 共尝试 500 题，成功 497 题；`qst_0072`、`qst_0157`、`qst_0160` 的 SiliconFlow 请求发生 5 秒读取超时。因为正式口径要求 Rerank 全题成功才进入横向指标，`hybrid_rerank` 未纳入汇总比较，但失败诊断仍保存在 `results.jsonl`。

这轮首先说明：在 Representative 语料上，Hybrid 的 Recall@5 和 MRR@10 高于 BM25 与 Dense；同时 Rerank 服务稳定性尚不足以形成可比较的全题组。它只回答“证据能否被召回和排前”，不等同于完整 RAG 回答正确率，也不触发优化。

## 2026-08-15：Challenge 正式语料准备完成

### 实际运行

- run ID：`enterpriserag-en-challenge-sf-001`
- 独立集合：`rag_eval_enterpriserag_enterpriserag_en_challenge_sf_001`
- 使用与 Representative 相同的英文配置和 500 题固定 split；本轮未运行检索或 RAG。

### 核验结果

| 检查项 | 结果 |
| --- | ---: |
| 标准证据文档 | 722 |
| 普通干扰文档 | 6,500 |
| hard distractor 文档 | 1,000 |
| 文档总数 | 8,222 |
| L3 叶子块 | 96,592 |
| L1/L2 父块 | 74,375 |
| 固定题目 | 500 |
| 分析集 / 验证集 | 300 / 200 |
| 每题 hard distractor | 2（500 题共 1,000 条映射） |
| hard 与标准证据重叠 | 0 |
| case-split SHA-256 | 与 Representative 和 manifest 匹配 |
| Milvus 实际行数（flush 后） | 96,592 |
| 父块实际行数 | 74,375 |
| 默认集合 `tutorial_verify_embeddings` | 0 行 |

Challenge 已冻结，运行目录、manifest、题级 hard 映射和集合保留给后续检索与 RAG 实验；本结果不代表检索或回答性能。

## 2026-08-15：Challenge 500 题检索正式基线

### 实际运行

- corpus run：`enterpriserag-en-challenge-sf-001`
- evaluation ID：`baseline-retrieval-001`
- 语料：Challenge；题集：500 题（分析 300、验证 200）
- 模式：retrieval-only，不调用回答模型或 GRADE_MODEL
- 逐题结果：`results.jsonl` 共 2,000 条（四种策略各 500 条）；验证集详情在 `case-review.md` 和自动报告中隐藏

### 汇总结果

| 策略 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | P50(s) | P95(s) | 失败 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.5940 | 0.7220 | 0.7640 | 0.6635 | 0.0036 | 0.0047 | 0 |
| Dense | 0.5340 | 0.6560 | 0.6980 | 0.6036 | 0.1227 | 0.1512 | 0 |
| Hybrid | 0.5880 | 0.7360 | 0.7680 | 0.6647 | 0.1248 | 0.1494 | 0 |

Rerank 共尝试 500 题，成功 498 题；`qst_0164`、`qst_0207` 的 SiliconFlow 请求发生 5 秒读取超时，因此 `hybrid_rerank` 未纳入汇总比较。

### 与 Representative 的受控比较

在配置、题目和标准证据不变的前提下，只增加标题相近 hard distractor 后：

| 策略 | Recall@5 变化 | MRR@10 变化 |
| --- | ---: | ---: |
| BM25 | -0.0200 | -0.0398 |
| Dense | -0.0160 | -0.0384 |
| Hybrid | -0.0360 | -0.0516 |

这说明相似标题干扰会使正确证据更容易排到后面，Hybrid 在本轮也没有消除该退化。它是 T8 失败分类的输入，不是已经批准的优化结论；下一步先运行 Representative 完整 RAG，观察召回退化是否传导为回答错误。

## 2026-08-15：Representative 500 题完整 RAG 基线（运行中）

### 当前进度快照

- evaluation ID：`baseline-rag-001`
- 语料：Representative 固定英文语料；集合：`rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`
- 题集：固定 500 题（分析集 300、盲态验证集 200）
- checkpoint 时间：2026-08-15 13:51:44 +08:00
- 已完成：30 / 500 题；`results.jsonl` 有 30 条题目 ID 唯一的逐题记录，最新为 `qst_0030`
- 运行状态：`running`；进度文件没有记录错误或失败题，当前 30 条记录没有评测/生成错误。

### 冻结的评测方法

本轮复用已经入库的 Representative 集合，不重新入库。每道题都经过固定的提问改写、子问题、Hybrid 检索、Rerank、Auto-merging、回答生成和自动判卷链路；运行器逐题保存检索与生成 trace、模型回答、标准答案、判卷明细和耗时。Embedding 为 SiliconFlow `BAAI/bge-m3`（1024 维），主回答模型、判卷模型均为 `deepseek-ai/DeepSeek-V4-Flash`。这些配置在 `evaluation-config.json` 中冻结，且本轮 `changed_variable` 为空。

### 如何解读当前状态

这只是可恢复运行的进度快照，不产生通过率、事实覆盖率或失败类型结论。完成 500 题、生成汇总报告并按题型检查后，才能进入 T8 的 300 题分析集失败分类。盲态验证集的题目级失败详情继续保持隐藏，直到参数冻结后的最终验证与人工复核。

### 进度更新（真实 checkpoint）

截至 2026-08-15 14:24:57 +08:00，已完成 `51 / 500` 题，最新为 `qst_0051`；`results.jsonl` 共 51 行且题目 ID 唯一，进度文件仍为 `running`，没有记录失败题或错误。该快照仍不代表最终 RAG 指标。

### 首轮完成后的有效性判断

`baseline-rag-001` 在 2026-08-15 16:14:09 +08:00 写完 500 条 checkpoint，并生成了 `summary.json`、逐题报告和人工复核队列；记录数、题目 ID 唯一性和 `300 / 200` split 均正确。但该轮**不是有效的 500 题 RAG 性能基线**：130 题真实完成端到端 RAG，3 题达到 300 秒总时限，另有 367 题在 worker 返回前退出。后 370 条异常记录均已保留为 `review`，没有被静默丢弃。

因此自动汇总中的全题证据覆盖率 `0.2271`、回答通过率 `0.1700` 和 P50 耗时 `1.0175s` 不可解读为模型效果，其中大量 0 秒字段来自未执行的异常记录。只看分析集时，300 题中有 226 题是系统错误（2 次超时、224 次 worker 退出），剩余 74 题全部为 basic；尚不能比较十类题型，也不能据此提出 RAG 优化变量。

已将仅含分析集详情的失败分类写入 `baseline-rag-001/analysis-failure-classification.md`。它保留了 `qst_0037`（超时）、`qst_0135`（worker 退出）、`qst_0017`（证据完整但错误回答）、`qst_0036`（证据不完整且错误回答）和 `qst_0022`（判卷超时）等可复核样本；验证集题目详情继续隐藏。

排查发现，前两次题级超时后 worker 可以恢复，但第三次超时后新 worker 立即退出。运行器现在会等待 request/result queue feeder 线程释放并关闭子进程句柄，同时在将来记录 worker 退出时保存 exit code。该修复只提高评测运行可靠性，不改变语料、Embedding、检索、Rerank、提示词、模型或任何 RAG 参数；相关单元测试和全量 77 项测试均已通过。旧运行保留审计，下一步在同一 Representative 集合建立新的完整基线运行。

### 重跑启动（真实 checkpoint）

2026-08-15 16:53:11 +08:00，已启动 `baseline-rag-002`。它复用 `rag_eval_enterpriserag_enterpriserag_en_representative_sf_001` 和相同的 corpus/case-split 哈希，`changed_variable` 为 `null`；唯一变化是上述运行器资源回收修复。启动后已完成 `2 / 500`，两条结果均有正常题级记录且无错误。正式指标仍等待 500 题完成后生成。

### 第二次重跑中断与进一步修复

`baseline-rag-002` 于 2026-08-15 20:20 +08:00 在 `305 / 500` 时人工中断，目录、305 条 JSONL 和错误日志均保留。该轮有 133 条真实完成记录、3 条 300 秒超时和 169 条 `APIConnectionError`；从 `qst_0137` 开始出现连续连接错误，因此同样不能作为正式基线。进度文件已明确标为 `interrupted`，避免把已停止的审计运行显示为仍在执行。

为排除“SiliconFlow 整体不可用”的误判，单独发送了不含业务语料的最小 chat health probe，得到 HTTP 200。问题在于 worker 内某次 RAG 调用返回 `evaluation_error` 后，运行器仍复用该进程，令后续题目的 API 客户端错误持续扩散。运行器现改为：只要一题返回 `evaluation_error`，即回收该 worker，下一题在干净进程中启动。该改动不重试或覆盖失败题，不改变 RAG 配置，只隔离故障进程；新增回归测试后全量 78 项测试、编译和 diff 检查均通过。下一次完整基线会使用新的 evaluation ID，前两次运行都只作审计证据。

### 第三次完整基线启动（真实 checkpoint）

2026-08-15 20:23:36 +08:00，已启动 `baseline-rag-003`。它仍引用同一 Representative corpus manifest、case split、独立集合与所有冻结模型/检索配置，`changed_variable=null`；唯一运行器差异是题级 `evaluation_error` 后重建 worker。启动检查已完成 `2 / 500`，没有题级错误或 stderr 输出；正式指标仍等待全题完成。

</details>

## 2026-08-16：Representative 完整 RAG 基线恢复（已完成）

### `baseline-rag-004`：从余额中断后的首次补跑

`baseline-rag-004` 从 `baseline-rag-003` 恢复，复用同一份 Representative 英文语料、冻结配置和独立集合，只选择源运行中缺失或带 `evaluation_error` 的 373 题。源运行中保留的 127 条有效记录没有被重新调用。

该轮写入 358 条 attempt 记录，其中 325 条有效、31 条达到 300 秒单题总时限、1 条为连接错误。运行至 `qst_0485` 时 SiliconFlow 返回 HTTP 402（余额不足），运行器立即停止；`qst_0486` 至 `qst_0500` 共 15 题未开始。链路累计得到 452 个有效题级结果，仍有 48 题未解决，因此没有生成 `results.jsonl` 或正式质量汇总。

### `baseline-rag-005`：恢复余额后的第二次补跑

恢复额度后，`baseline-rag-005` 从 `baseline-rag-004` 继续，只选择上述 48 个未解决题。48 题均已尝试：38 条有效，9 条达到 300 秒总时限，1 条为 `APITimeoutError`。本轮没有新的余额错误或连接错误。

这意味着当前链路已有 `452 + 38 = 490` 个有效的唯一题级结果；剩余 10 条仍属于运行时异常，不能进入正式质量统计，也不能开始 T8 失败分类。

### 运行器状态一致性修复

在本轮产物检查中发现：retry attempt 已跑完时，`attempt_summary` 中的 `evaluation_status=completed` 会覆盖整体评测的 `interrupted` 状态，即使仍有未解决题。该问题只影响汇总元数据，不会改变任何逐题结果、语料、模型或检索配置。

修复后，当存在缺失题或 `evaluation_error` 时，`summary.json` 与 `evaluation-progress.json` 均明确为 `interrupted`，且不会生成 `results.jsonl`。新增回归测试覆盖该场景；目标测试、完整测试套件（82 项）、Python 编译和 diff 检查均已通过。`baseline-rag-005` 已用已有 checkpoint 刷新元数据，不重新调用模型。

### 下一步

`baseline-rag-006` 已完成对 10 个异常题的尝试，其中 5 条有效、5 条再次达到 300 秒单题总时限；没有新的余额或连接错误。当前累计有效结果为 `495/500`，仍未生成合并 `results.jsonl`，因此不能开始 T8。

`baseline-rag-007` 已完成对这 5 个超时题的尝试，其中 1 条有效、4 条再次达到 300 秒单题总时限；当前累计有效结果为 `496/500`，仍未生成合并 `results.jsonl`。

`baseline-rag-008` 已完成对这 4 个超时题的尝试，其中 1 条有效、3 条再次达到 300 秒单题总时限；当前累计有效结果为 `497/500`，仍未生成合并 `results.jsonl`。

经用户确认后，`baseline-rag-009` 曾启动并在旧的 300 秒总时限下写入 `qst_0197` 的超时记录；为避免继续消耗旧配置，运行器随后停止，已有 checkpoint 保留不变。

随后将唯一运行变量改为 `EVALUATION_CASE_TIMEOUT_SECONDS=600`，单次模型请求仍为 `90s`，模型、提示词、检索、语料和集合均不变。`baseline-rag-010` 从 `baseline-rag-009` 保留 497 个有效结果，只重试恢复清单中的 3 个题；配置快照记录 `changed_variable=evaluation_case_timeout_seconds`、总时限 `600s`。三题补跑成功后，形成 500/500 条唯一结果，且没有 `evaluation_error`。

### `baseline-rag-010`：完整质量基线（真实结果）

本轮是当前 Representative 英文正式 RAG 基线。它复用已经入库的
`enterpriserag-en-representative-sf-001` 语料和独立集合
`rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`，没有重新入库，也没有调用默认业务集合。唯一运行变量是外层单题评测总时限从 300 秒调整为 600 秒；模型单次请求仍为 90 秒，其他模型、提示词、Embedding、top-k、Rerank、分块和语料均保持冻结。

完整产物位于 `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/baseline-rag-010/`，包括配置快照、逐题 `results.jsonl`、人类可读 `case-review.md`、汇总 `summary.json`、自动报告、人工复核队列和恢复审计链。

| 指标 | 结果 | 解释 |
| --- | ---: | --- |
| 唯一题目 | 500 / 500 | 分析集 300，验证集 200；没有缺题或评测错误 |
| 证据全覆盖率 | 70.00% | 标准证据全部出现在最终证据集合中的题目比例 |
| 平均证据覆盖率 | 74.66% | 按题计算标准证据覆盖比例后取平均 |
| 自动回答通过率 | 52.40% | 独立判卷为 `pass` 的比例；`review` 不计为通过 |
| 正确拒答率 | 95.00% | `info_not_found` 题中正确拒答的比例 |
| 自动判卷人工复核率 | 7.00% | 35 / 500 题的独立判卷为 `review`；这不是完整人工候选队列规模 |
| 端到端延迟 P50 | 72.19 秒 | 从单题开始到回答和独立判卷完成 |
| RAG 阶段延迟 P50 | 35.88 秒 | 检索、改写、子问题和证据组织阶段 |
| 回答生成延迟 P50 | 15.51 秒 | 主回答模型阶段 |

过程触发率为：问题改写 4.20%、子问题 43.00%、Auto-merging 38.60%。按题型看，`basic` 通过率最高（68.00%），`project_related`（5.00%）、`high_level`（10.00%）、`completeness`（25.00%）和 `semantic`（39.20%）明显偏低；这说明当前主要问题不只是“有没有召回”，还包括多约束证据组织和回答完整性。完整按题型表见同目录的 `report.md` 和 `summary.json`。

### T8 自动初筛与 analysis 集人工复核（已完成）

脚本 `scripts/analyze_rag_failures.py` 只读取分析集 300 题，验证集不展示题目、答案或失败详情。结果写入同一实验目录的 `analysis-failure-classification.json` 和 `analysis-failure-classification.md`。运行器生成的人工队列会为 validation 候选保留不含 ID、问题、答案、证据或失败理由的盲态占位；本轮人工结论文件只处理 analysis 候选，validation 细节不进入可读复核文件。

分析集自动判卷为 `153 pass / 123 fail / 24 review`。自动分类如下；一题可以同时属于多个类别，因此分类计数不能相加为失败总数：

| 自动类别 | 题数 | 当前含义 |
| --- | ---: | --- |
| 未召回标准证据 | 63 | 标准证据存在，但最终证据排名为空 |
| 证据召回但排序靠后 | 19 | 标准证据已召回，但最前排名大于 3 |
| 标题相近干扰误排 | 0 | Representative 不含题级 hard distractor，不能据此评价 Challenge |
| 证据不完整或上下文组织失败 | 19 | 有证据但覆盖率在 0 和 1 之间 |
| 回答事实遗漏、扩展或编造 | 122 | 证据链无错误且独立判卷为 fail |
| 正确拒答失败 | 0 | 本轮 `info_not_found` 题没有自动归入该类 |
| 自动判卷不确定，待人工复核 | 23 | `review` 且没有 API、生成或判卷请求错误 |
| 判卷异常、API 异常或超时 | 2 | 真实生成或判卷链路错误 |

自动分类只是“优先检查哪些题、可能属于哪一层”的分诊，不是人工最终结论。人工复核范围为 71 条 analysis 候选：`review`、系统异常、证据完整但回答失败，以及回答通过但证据不完整。

人工结论写入 `baseline-rag-010/manual-review.jsonl` 和 `baseline-rag-010/manual-review.md`。本轮 analysis 人工候选共 71 条；其中 24 条来自自动 `review`，其余为证据完整但回答失败、回答通过但证据不足或系统异常。validation 候选只保留盲态占位，不计入可读人工结论。摘要如下：

| 人工结论 | 题数 | 含义 |
| --- | ---: | --- |
| 自动判定改为通过 | 21 | 核心回答已满足问题，自动判卷对额外信息或次要差异过严。 |
| 保持通过但证据不足 | 4 | 答案正确不等于已由完整标准证据支持。 |
| 确认回答合成失败 | 37 | 遗漏、范围漂移、冲突值/步骤或相邻资料污染。 |
| 确认证据上下文失败 | 7 | 文件级召回存在，但最终上下文没有提供决定性内容。 |
| 答案失败且有系统异常 | 1 | `qst_0471` 判卷 API 返回 402；人工仍确认拒答与参考答案不符。 |
| 系统异常，质量不可判 | 1 | `qst_0208` 生成请求超时且答案为空，不计入模型质量根因。 |

这批人工结论说明：当前失败不应简单归结为“没有召回”。至少 7 题是标准文档已被记录为召回、但关键细节没有进入最终上下文；37 题则是回答阶段遗漏、冲突或相邻资料污染。另一方面，21 条自动 `fail/review` 经人工核对后核心回答成立，应作为判卷校准样本，而不是直接驱动 RAG 调参。4 条“回答通过但证据不完整”不能反过来证明检索充分。

T8 仍只描述 analysis 集，不能代表 200 题 validation 泛化结果。

### T9 改写候选融合：15 题 targeted 试跑（未通过归因门槛）

本轮唯一行为变量是 `rewrite_candidate_fusion`：仅当现有流程实际触发一次问题改写时，才把首次检索和改写检索的候选按 `chunk_id` 去重，再用原问题统一 Auto-merging、Rerank 和最终 8 段筛选。默认线上开关仍关闭；没有重新入库、没有清理 Milvus，也没有操作 `tutorial_verify_embeddings`。

运行产物：

- evaluation ID：`t9-rewrite-fusion-targeted-003`
- 题集：固定的 15 道 analysis 题，来源为 `baseline-rag-010` 的 target manifest
- 配置：`changed_variable=rewrite_candidate_fusion`，`rewrite_candidate_fusion_enabled=true`
- 结果目录：`output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/t9-rewrite-fusion-targeted-003/`
- 人工复核：同目录的 `manual-review.jsonl` 和 `manual-review.md`

| 指标 | baseline-rag-010 同题 | T9 试跑 | 说明 |
| --- | ---: | ---: | --- |
| 题数 | 15 | 15 | 均为 analysis |
| 证据全覆盖题数 | 2 / 15 | 7 / 15 | 全体重跑多出的覆盖不能直接归因；其中 6 道覆盖变好的题没有实际触发融合 |
| 平均证据覆盖率 | 0.1333 | 0.5222 | 全体重跑差异，不能视为融合效果 |
| 回答通过题数 | 1 / 15 | 1 / 15 | 总体不变 |
| 实际触发融合 | 不适用 | 7 / 15 | 8 题在重跑时未进入改写分支 |
| 触发融合且证据覆盖提升 | 不适用 | 0 / 7 | 未达到本任务的证据收益门槛 |
| 触发融合且回答变通过 | 不适用 | 1 / 7 | `qst_0458`；基线证据已完整，仍需排除生成波动 |
| 新增系统错误或超时 | 不适用 | 0 | 没有发现运行稳定性退化 |

人工复核后的判断是：真正执行融合的 7 道题没有任何证据覆盖提升；全体指标中看起来变好的 6 道题是在未触发融合的重跑中出现的，不能算作该变量的收益。因此 T9.4 门槛未通过，本轮不启动 300 道 analysis，也不运行 validation。该结果只能说明当前 targeted 试验无法对融合收益作出可靠归因，不能说明融合一定无效。

下一步必须先重新设计可归因的 targeted 试验（例如固定改写分支的输入/输出，并确保对照题在同一检索条件下实际执行融合），再由用户确认唯一变量后继续。确认前不修改 RAG 参数、不扩大题集、不接触 validation。

### T9 证据链候选审计：97 道 analysis 题（已完成，不是新的正式基线）

为定位“标准文件找到了但关键事实没有进回答”和“改写是否真的补回候选”两类问题，新增独立运行
`t9-rewrite-fusion-evidence-audit-001`。清单固定为 `baseline-rag-010` 的 analysis 集中
`retrieval_miss`、`retrieval_late`、`evidence_incomplete` 的去重并集：97 道，来源哈希、题目 split 和类别成员关系均在
`evidence-candidate-audit-target-manifest.json` 中冻结。没有重跑其余 analysis、没有运行 validation、没有重新入库或触碰默认业务集合。

本次唯一行为变量仍是默认关闭的 `rewrite_candidate_fusion`；候选全文、父子块映射、Rerank 输入/输出和最终上下文只作为评测审计信息写入该独立运行的 `results.jsonl`，不暴露到线上 API 或前端。

运行完整性如下：

| 检查项 | 结果 |
| --- | --- |
| 清单完成度 | 97 / 97 个唯一 case ID |
| 数据边界 | 97 / 97 均为 analysis；0 个 validation |
| 有候选审计的题 | 96 / 97 |
| 无候选审计的题 | `qst_0240`，外层单题 600 秒时限超时 |
| 实际执行候选融合 | 8 / 97 |
| 不执行融合 | 89 / 97，未进入既有改写分支，不能把其重跑差异归因给融合 |

自动结果的 21.65% 回答通过率、31.96% 文件级证据全覆盖率和 42.50% 平均文件级覆盖率只描述这 97 道预先筛出的证据链问题，**不能**与 500 题正式基线的 52.40% 通过率并列解读，也不能作为 validation 泛化结论。

#### 人工复核结论

自动队列原有 28 条；审计又发现 6 条 `evidence_grading_unavailable`，它们此前未进入队列，却在取到候选后因证据评分请求不可用而按 fail-closed 策略拒答。最终人工复核 34 条、157 个标准事实点，记录在本运行目录的 `manual-review.jsonl` 和 `manual-review.md`。

| 人工结论 | 数量 | 含义 |
| --- | ---: | --- |
| 人工确认通过 | 2 | 独立判卷请求超时，但回答核心事实正确。 |
| 回答通过、标准证据不完整 | 5 | 文本答案正确，不能反向证明最终上下文已覆盖全部标准事实。 |
| 人工确认回答失败 | 11 | 关键事实没有完整进入最终上下文，或回答使用相邻/无关材料补成了错误结论。 |
| 系统异常、质量不可判 | 16 | 7 条证据评分不可用、8 条回答生成超时、1 条外层 case 超时；不计为分块或 RAG 质量失败。 |

其中 `qst_0359` 的自动 `pass` 被人工改为回答失败：它虽然给出了 409 和主 `error.code`，却错误声称 `subcode` 必填，并遗漏缺失 subcode、受限重试、流式终止事件和兼容映射等问题所要求的关键行为。这说明自动通过也必须接受事实级审计。

#### 候选链路发现

候选审计支持以下结论，但这些类别可能在同一题重叠，不能相加为失败总数：

| 观察 | 题数/例子 | 含义 |
| --- | --- | --- |
| 标准文件从原始 L3 候选中完全缺失 | 17 / 97 | 是真实的原始召回不足；例如 `qst_0043`、`qst_0058`。`qst_0240` 没有返回候选审计是外层超时，属于无法判断，不能算作文件未召回。 |
| 标准文件进入原始候选、却未被 Rerank 返回 | 2 / 97：`qst_0136`、`qst_0142` | Rerank 是可见的筛除点；本次没有发现标准文件在 Auto-merging 阶段被整份丢弃。 |
| 找到标准文件但没有找到完整事实 | 多例 | 文件级覆盖不能等于事实级覆盖。`qst_0039` 已有 Quality 和 Performance 所在块，但 Compliance 落在相邻未选中的块；`qst_0120` 只有客户标题；`qst_0221` 只有员工手册标题。 |
| 证据评分路由拒答/要求互动 | 36 / 97 为 `no_knowledge`、`needs_scope_selection` 或 `needs_clarification` | 其中 7 条是评分请求不可用；其余包含“候选有相关块但系统认为范围冲突或证据不足”的情况。`qst_0052` 的正确父块已在最终检索上下文，仍被评分路由要求范围选择。 |
| 文件级全覆盖仍回答失败 | 9 条可判定样本 | 例如 `qst_0068` 缺 Hosted 5 ms、`qst_0158` 缺三个预设名称、`qst_0249` 缺多项 AWS 准备步骤。它们同时表现为事实上下文缺口和回答合成时的相邻资料污染。 |

#### 原始候选材料人工复核

自动事实审计的用途是初筛，不足以直接把“事实未出现”归因为分块。因此，继续对“标准文件已进入原始 L3 候选、但自动初筛认为事实不完整”的 45 道题，使用冻结的原始 Markdown 和同一套 L1/L2/L3 切分规则逐题复核。该过程只读本地语料和已有审计结果，不查询 Milvus、不重跑 RAG，也不查看 validation。

逐题结论写入本运行目录的 `manual-raw-candidate-fact-review.jsonl` 与 `manual-raw-candidate-fact-review.md`；每条 JSONL 明确标记 `case_set=analysis`。

| 互斥材料状态 | 题数 | 人工判断 |
| --- | ---: | --- |
| 标准来源文件未全部进入原始候选 | 30 | 其中 17 道一个标准来源文件也没有出现，13 道只出现部分标准来源文件；这是文件级原始召回不足。 |
| 标准来源文件已出现，但关键事实在其他 L3 叶子块 | 41 | 这是块级材料缺口；其中 11 道在已命中叶子块的相邻叶子块，30 道在同一来源文件的更远叶子块。 |
| 自动初筛的事实缺口不构成分块问题 | 4 | 原始候选已足以支持问题，自动审计把计数、否定条件或答案合成要求误判为“需要额外事实”。 |
| 原始候选材料完整 | 19 | 标准文件和所需事实都已进入原始 L3 候选。 |
| 无法判断 | 3 | `qst_0240` 没有候选审计；`qst_0365`、`qst_0379` 的事实级初筛请求超时。 |

这五类合计为 97 道。它们描述的是预先筛出的 analysis 证据链可疑题，不能外推为 500 题总体比例，更不能作为 validation 结论。

典型例子说明了两个不同层面的块级问题：`qst_0039` 已命中 L3#12，但 Performance 和 Compliance 的完整表述在紧随其后的 L3#13（#14 是重叠块）；这类题可以用于验证相邻叶子块扩展。`qst_0023` 已命中文档的 L3#0/#1/#7，完整评审名单却在 L3#21；`qst_0300` 命中 L3#0，而价格表在 L3#4。这类远距离缺口不能靠只补一个相邻块解决，仍属于块级召回排序问题。

因此，“分块”确实是当前 97 道证据链审计中的主要材料缺口：41 道文件已经找到但正确叶子块没有进入候选。不过它不等同于“把 chunk 调大就能解决”。相邻块扩展理论上只直接覆盖其中 11 道；剩余 30 道还需要解决同一文件内正确叶子块的召回和排序。证据评分的 fail-closed 路由、生成/评分请求稳定性、Rerank 筛除和回答合成污染也仍是独立问题，不能一并归因给分块。

#### 改写候选融合的可归因结果

只有 8 道题实际执行了融合。与 `baseline-rag-010` 的同题对照中，6 道文件级覆盖率仍为 0；两道提高到全覆盖：

| case ID | 基线覆盖率 | 本轮覆盖率 | 回答结果 | 审计判断 |
| --- | ---: | ---: | --- | --- |
| `qst_0147` | 0.00 | 1.00 | pass | 标准块来源标记为 `rewritten`，是本轮唯一“融合后回答也变通过”的正向信号。 |
| `qst_0227` | 0.00 | 1.00 | fail | 改写候选补进了即时 workaround，但根因事实仍不完整，回答改用无关的 kernel/batching 解释。 |

这 8 道中只有 1 道同时获得证据和回答改善，样本过小且回答/判卷存在模型波动；不能据此宣称候选融合已成功，也不能将 89 道未触发改写的重跑结果算作融合收益。候选融合继续保持默认关闭。

#### 下一步闸门

本轮先不启动下一轮优化。后续只能由用户确认一个唯一变量，并使用新的 evaluation ID、不可变配置、逐题 JSONL、汇总报告和人工复核记录。当前审计支持两个互斥的候选方向：

1. 针对“同文件但不同叶子块”的事实缺口，试验一个分块/父块上下文变量；
2. 针对“已找到相关块仍拒答”的样本，试验一个证据评分路由变量，并把评分请求不可用与普通无知识明确分开。

二者不能在同一轮同时修改；validation 仍保持盲态，直到 analysis 根因和唯一变量得到确认。

### T10 相邻 L3 块扩展定向评测（已完成，11 道 analysis 题）

#### 这轮到底改了什么

这轮只打开一个开关：`adjacent_l3_expansion`。系统先按原来的方式找到一批 L3 小块；对每个已经命中的小块，再从**同一份文件**中补读它前面和后面紧挨着的 L3 小块。补读只发生一次，之后仍沿用原来的 Auto-merging、Rerank、top-8 和回答流程。

可以把它理解为：原来只拿到一页纸的中间一句话，这轮额外把同一页纸前后相邻的内容一起带上，看看关键句是不是就在旁边。它不会主动搜索同一文件很远位置的块，也不会改变分块、Embedding、模型、提示词或最终 top-k。

#### 实验边界和可复核产物

- evaluation ID：`t10-adjacent-l3-expansion-targeted-001`
- 题集：冻结的 11 道 `analysis` 题；不是 97、300 或 500 题，也不是 validation 泛化测试
- 语料：已入库的 7,222 篇英文 Representative 语料，Milvus 集合为 `rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`
- 配置：`changed_variable=adjacent_l3_expansion`，`adjacent_l3_expansion_enabled=true`，`retrieval_top_k=8`，`rewrite_candidate_fusion_enabled=false`
- 清单文件：`baseline-rag-010/adjacent-l3-expansion-target-manifest.json`
- 清单文件 SHA-256：`8a344afbeacbb14b3619d7fc4df5caffb485956b4494819c38a5412e4ec54451`
- 清单内部 payload 哈希：`ec582628d35bd8f30fa72fe3761bea0553c546ef6a1626eca4cffe6e4390c35b`
- 逐题候选和阶段快照：B 组目录的 `results.jsonl`、`candidate-audit.md`、`case-review.md`
- 人工复核：B 组目录的 `manual-review.jsonl`、`manual-review.md`

本轮只读取现有 Milvus 集合，没有重新切分或入库，没有删除集合，也没有触碰 `tutorial_verify_embeddings`。每道题仍使用单次模型请求 90 秒、单题外层 600 秒时限。

#### 结果

| 指标 | 结果 | 白话解释 |
| --- | ---: | --- |
| 完成题数 | 11 / 11 | 没有评测错误 |
| 证据全覆盖率 | 72.73% | 11 题中有 8 题的标准证据文件全部进入最终证据集合 |
| 平均证据覆盖率 | 72.73% | 这 11 题的标准证据覆盖比例平均为 72.73% |
| 自动回答通过率 | 45.45% | 自动判卷判为通过的有 5 题；不把 `review` 当作通过 |
| 自动人工复核率 | 18.18% | 2 题被独立判卷标记为需要人工复核 |
| 端到端 P50 | 134.07 秒 | 从开始一题到回答和判卷结束的中位耗时 |
| RAG P50 | 39.80 秒 | 检索、相邻扩展、合并和精排的中位耗时 |
| 生成 P50 | 17.03 秒 | 主回答模型的中位耗时 |

原始候选通常约 30 个；加入相邻块后约为 72--82 个；最终交给回答模型的上下文仍固定为 8 段。因此这轮主要增加了精排输入量，可能增加 RAG 耗时，但没有扩大回答模型的上下文长度。

#### 人工复核后的逐题结论

| 题目 | 人工结论 | 这说明什么 |
| --- | --- | --- |
| `qst_0039`、`qst_0068`、`qst_0082`、`qst_0158`、`qst_0449` | 相邻材料补回，答案通过 | 这是本轮最明确的正向收益：关键事实在命中块旁边，扩展后进入最终上下文并帮助回答完整作答 |
| `qst_0255` | 材料和答案疑似正确，但独立判卷超时 | 不能计入自动通过率，也不能把它当成失败；属于判卷系统异常 |
| `qst_0249` | 只补回部分材料，答案仍不完整 | 相邻扩展能补近处内容，但远处的安全和节点配置细节仍缺失 |
| `qst_0359` | 相邻块进入扩展候选，但最终上下文丢失 | 候选找到了不代表最终会留下，后面的合并/精排仍可能丢证据 |
| `qst_0382` | 证据进入最终上下文，但回答遗漏细节 | 材料已经给到模型，问题转为回答整合，不是分块或召回问题 |
| `qst_0110` | 证据进入最终上下文，但证据评分不可用 | 评分请求异常导致 fail-closed，不能归因给这次优化 |
| `qst_0285` | 本次没有再次命中目标材料，且生成超时 | 没有可供扩展的目标块，同时存在生成异常，不能评价扩展效果 |

#### 这轮能得出的结论

相邻 L3 扩展**确实有效，但只对一类问题有效**：已经命中正确文件和附近块，只差前后相邻的一小段事实时，它能把这段材料补回来。本轮 11 题中有 5 题得到人工确认的“材料补回且答案通过”。

它不能解决以下问题：

1. 关键事实在同一文件更远的位置（例如相隔多个 L3 块）；
2. 材料进入扩展候选，却在 Rerank 或最终 top-8 前被筛掉；
3. 证据已经进入最终上下文，但回答仍遗漏、混淆或没有整合；
4. 证据评分、生成或判卷请求超时/不可用。

因此，这轮不能宣称“整体回答率已经提升”，也不能把 11 题结果外推到 97 / 300 / 500 题或 validation。下一步必须由用户确认唯一变量；在确认前保持当前开关仅用于该 targeted 评测，不扩大题集、不重新入库、不启动下一轮优化。

### T10 相邻 L3 扩展扩大评测：147 道历史 analysis 非通过题（运行与人工归因完成）

这不是新的 500 题基线。题集固定为 `baseline-rag-010` 中 147 道 `analysis` 非通过题（123 道自动 `fail`、24 道 `review`），明确排除了原本通过的 153 道 analysis 题和全部 validation。唯一 RAG 行为变量仍是 `adjacent_l3_expansion`；10 worker 只改变执行吞吐，不改变模型、Embedding、提示词、Rerank、Auto-merging、top-8、语料或集合。

首次运行保留了 75 道已经完成的题，新的 evaluation `t10-adjacent-l3-expansion-analysis-147-002` 只补跑缺失的 72 道。最终完成 147/147；没有重新入库、没有清理 Milvus、没有操作 `tutorial_verify_embeddings`，也没有运行 validation。

| 指标 | 冻结 147 题基线 | 本轮自动结果 | 变化 |
| --- | ---: | ---: | ---: |
| 证据全覆盖率 | 42.86% | 55.78% | +12.93 个百分点 |
| 平均证据覆盖率 | 48.38% | 62.70% | +14.32 个百分点 |
| 自动回答通过率 | 0.00% | 27.89%（41/147） | +27.89 个百分点 |
| 证据完全未命中 | 68 | 46 | -22 |

相邻扩展实际在 147/147 题执行；每题新增相邻候选 P50 为 48、P95 为 57。端到端 P50 为 87.28 秒，其中 RAG P50 为 34.89 秒、回答生成 P50 为 22.20 秒。这个延迟来自完整 RAG、生成和独立判卷，不能简单理解成“相邻块本身耗时”。

#### 人工归因结论

自动通过不是自动收益。人工逐题核对候选阶段、参考答案和模型答案后，只有 5 道可以确认“关键事实在命中块的直接相邻块，扩展后进入最终上下文，且答案完整正确”：`qst_0039`、`qst_0068`、`qst_0082`、`qst_0158`、`qst_0449`。这是 `5 / 147 = 3.40%` 的保守、可归因下界。

另外 15 道虽然“覆盖提升且自动通过”，但标准来源已在新运行的原始候选中，或缺少事实级直接相邻的证明；可能受益，但不能诚实地归到本变量。21 道自动通过但覆盖不变，可能是回答/判卷重跑波动，也不能归因。19 道覆盖下降不能直接称作变量退化，因为最终 top-8 还会受 Auto-merging 和 Rerank 的筛选影响；例如 `qst_0359` 的目标材料进入了扩展候选，但没有保留到最终上下文。

有 9 道系统异常必须排除：`qst_0023`、`qst_0100` 的回答生成超时；`qst_0287`、`qst_0298`、`qst_0405` 的证据评分不可用；`qst_0343`、`qst_0361`、`qst_0407`、`qst_0443` 的独立判卷超时。它们不属于分块、检索或回答质量失败。

完整人工归因记录位于 `t10-adjacent-l3-expansion-analysis-147-002/manual-review-conclusions.jsonl` 和 `manual-review-conclusions.md`；原始自动队列仍保留在 `manual-review.jsonl`/`.md`。这些全都是 analysis 记录，不能外推为 500 题总体结果，更不能作为 validation 泛化结果。

### 结构化 Markdown 分块与评测存储隔离：30 道 analysis 真实评测完成

本轮的唯一 RAG 输入变量已固定为 `document_chunking_strategy=markdown_header_recursive_v1`。实现保留旧的 `recursive_l1_l2_l3` 默认路径；新路径仅在 EnterpriseRAG 评测显式传参时启用。L1/L2/L3 的目标长度、重叠、Embedding、模型、Prompt、top-k、Rerank 和 Auto-merging 均未改变。

新路径按 Markdown 标题后，在标题范围内保护完整列表、表格和围栏代码块，并保存 `heading_path`、原文范围、结构类型、前后块、策略和配置 hash。评测 L1/L2 的新 store 使用专用 `EVALUATION_DATABASE_URL`，拒绝缺失 URL 或业务库回退；它只操作独立表和 run-scoped Redis key。物理 PostgreSQL 数据库不由应用或 runner 自动创建。

离线审计已冻结 30 道 `analysis` 的 `same_source_far_leaf_gap` 题，产物为 `structured-chunking-offline-audit-002/chunking-target-manifest.json`、`chunk-audit.jsonl`、`chunk-audit.md` 和 `chunk-audit-summary.json`。审计结果为旧 L3 `500`、新 L3 `462`；61 条关键旧 L3 中 17 条边界完全相同、44 条边界变化；只有 3 / 30 题的关键材料位于二级或更深标题。全部 30 条旧关键材料可映射到新块。

这些是结构和可审计性结果，**不是**通过率、证据覆盖率或召回提升。离线过程未连接 PostgreSQL、Redis、Milvus，也没有调用 Embedding、Rerank、回答或判卷模型；没有访问 validation，也没有改动或清理 `tutorial_verify_embeddings` 与已冻结 Representative 集合。30 条并排块记录已经人工复核，随后才进入独立库、新集合和 30 题真实 RAG 对照的受控阶段。

本轮实施记录：结构化 target manifest 的最新版本为 `structured-chunking-offline-audit-002/chunking-target-manifest.json`，文件 SHA-256 为 `7565e1f478f81956224d9cab601883053a8fd2719e84e8c4626906bf7ae5bfe1`。独立 PostgreSQL `enterprise_rag_evaluation`、新 Milvus collection `rag_eval_enterpriserag_enterpriserag_en_representative_structured_targeted_004` 和 run-scoped Redis 已实际用于语料准备；旧业务库、默认业务集合和旧 Representative 集合未被写入或清理。早期 `001` 产物保留作历史记录，不作为结果。

真实评测固定使用 `corpus_run_id=enterpriserag-en-representative-structured-targeted-004`、10 个 worker、单题总时限 600 秒，冻结 30 道 `analysis` 题，只改变 `document_chunking_strategy=markdown_header_recursive_v1`。主运行 `structured-chunking-analysis-30-001` 之后，按缺失/异常题链式使用新 evaluation ID 重试：`retry-002`、`retry-003`、`retry-004`、`retry-005`。retry-005 仍有 3 道整题超时，另有 3 道回答请求超过 90 秒，因此最终 6 道必须归为 `system_error`，不能当作分块失败或优化收益；整体状态保持 `interrupted`。

| 指标 | baseline 对应 30 题 | 结构化分块最终链 30 题 | 变化 |
| --- | ---: | ---: | ---: |
| 证据全覆盖率 | 10.00% (3/30) | 50.00% (15/30) | +40.00 个百分点 |
| 平均证据覆盖率 | 16.76% | 53.98% | +37.22 个百分点 |
| 回答通过率 | 6.67% (2/30) | 26.67% (8/30) | +20.00 个百分点 |

只保留 baseline 和结构化结果都没有系统错误的 24 道同题对照：证据全覆盖率 `8.33% -> 54.17%`（`+45.83` 个百分点），平均证据覆盖率 `14.70% -> 59.14%`（`+44.44` 个百分点），回答通过率 `4.17% -> 33.33%`（`+29.17` 个百分点）。这仍然是 targeted analysis 结果，不是 500 题总体结果。

自动影响审计把 30 题分为：7 道“证据补回且回答通过候选”、5 道“证据覆盖提升但回答仍失败”、1 道“证据覆盖退化”、11 道“没有可测证据收益”、6 道系统异常。7 道只能作为待人工确认的因果候选；5 道说明材料可能补回但回答仍失败；1 道出现退化，不能计入收益；11 道说明结构化分块没有在本次链路中产生可测证据收益。目标答案文档在初始候选、原始候选、合并后候选、Rerank 输入、最终上下文中的出现数分别为 `14/30`、`21/30`、`21/30`、`21/30`、`20/30`，文件出现不等于关键事实完整进入最终上下文。

真实评测各次 attempt 与最终链式审计位于 `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/` 下的 `structured-chunking-analysis-30-001`、`retry-002`、`retry-003`、`retry-004`、`retry-005` 目录。retry-005 的 `attempt-results.jsonl` 保留最后 3 道原始重试结果；`structured-chunking-merged-results.jsonl` 是从完整 retry 链离线合并出的 30 道审计输入，不是新的模型运行。该目录的 `structured-chunking-impact-summary.json/.md` 是新旧对照，`structured-chunking-manual-review.jsonl/.md` 是逐题人工预复核，`manual-review.jsonl/.md` 是最后 attempt 的运行器人工队列。

当前不能据此启动 T9、运行 validation 或重跑 500 题。最后 3 道整题超时已经重试仍未完成；3 道回答请求超时也只保留为系统异常。下一步应由用户确认是否继续人工确认 7 道收益候选；后续每轮仍只能改变一个变量。

离线块审计的 30 道题人工复核已完成，产物位于 `structured-chunking-offline-audit-002` 的 `manual-review.jsonl` 和 `manual-review.md`。这不是新旧 RAG 结果的最终人工因果结论；真实 RAG 结果的 `structured-chunking-manual-review.jsonl/.md` 仍标记为待人工最终确认。离线复核结论为：8 题出现明确的标题/列表/代码/表格结构信号，7 题关键边界基本不变，14 题只是普通文本边界重新分配，1 题旧块定位有重复歧义。复核期间发现 `qst_0300` 的无首尾管道符表格未被识别为表格原子，已修复 `_MARKDOWN_TABLE_SEPARATOR_RE` 并加回归测试；修复后重新运行离线审计。

## 英文子问题语言控制评测（2026-08-19）

新建 evaluation ID `english-subquestion-language-deepseek-analysis-30-001`，复用结构化英文 corpus run `enterpriserag-en-representative-structured-targeted-004` 和 collection `rag_eval_enterpriserag_enterpriserag_en_representative_structured_targeted_004`。唯一变量为 `subquestion_language_policy=preserve_input_language_v1`；30 道 frozen analysis、10 worker、DeepSeek-V4-Flash 三个模型角色、Embedding、Rerank、top-k、Auto-merging 和分块均固定。没有执行 prepare、cleanup、入库、validation 或 500 题。

首次运行暴露出两项运行问题：隔离 PostgreSQL 的认证配置需要恢复；恢复逻辑还把历史 `evaluation_error` 当作完成记录。现已将隔离库连接恢复，并修复 checkpoint 恢复：成功记录复用，`evaluation_error`、回答生成错误和判卷错误重新进入待跑队列；旧异常写入 `results-retry-history.jsonl` 供审计，不混入新的 `results.jsonl`。相应单元测试覆盖该情形。

恢复后，30 / 30 完成，`evaluation_error=0`。16 道实际复杂题的小问题和实际检索文字均已检查为英文，语言控制合规。完整配置、逐题 JSONL、候选 trace、自动人工队列、语言检查、前后对照和人工预复核均在该 evaluation 目录；结论只限 30 道 analysis，当前没有证据支持默认启用该策略。
