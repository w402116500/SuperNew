# EnterpriseRAG 分块与证据链问题分析稿

更新时间：2026-08-19。

本文是后续复核用的工作底稿，记录本项目已经做过的评测、候选审计和分块方案讨论。它刻意区分三种内容：

1. **已真实运行并保存产物的结果**：可以复查对应 `evaluation_id`、JSONL、汇总和人工复核记录；
2. **根据这些结果作出的判断**：说明判断依据和不能推出的结论；
3. **尚未实施的方案**：只是一份待确认的具体实施设计，不能写成已经取得的优化效果。

本文不展示 validation 200 题的题目、答案、证据或失败详情。它们继续保持盲态。

---

## 文档索引

### 章节索引

| 章节 | 内容 |
| --- | --- |
| [第 1 节](#1-本次评测的边界测的是什么没有测什么) | 评测范围、语料、analysis/validation 边界与正式基线 |
| [第 2 节](#2-一道题实际经过了哪些步骤) | 一道题从检索到判卷的完整链路 |
| [第 3 节](#3-指标是怎么算出来的) | 回答、证据、拒答和延迟指标口径 |
| [第 4 节](#4-500-题基线结果哪些题更难) | 500 题基线与题型差异 |
| [第 5 节](#5-t8-300-道-analysis-的自动失败初筛) | T8 自动失败分类及其局限 |
| [第 6 节](#6-97-道证据链候选审计分块问题到底占多少) | 97 道证据链审计和分块问题边界 |
| [第 7 节](#7-已试方案一t9-改写候选融合) | T9 改写候选融合结果 |
| [第 8 节](#8-已试方案二t10-相邻-l3-扩展) | T10 相邻块扩展结果 |
| [第 9 节](#9-当前项目的实际分块方式) | 旧分块链路和问题 |
| [第 10 节](#10-讨论过的外部分块方案及其适用性) | 外部方案比较 |
| [第 11 节](#11-目前推荐的具体改造不是抽象口号) | 新分块的具体设计 |
| [第 12 节](#12-为什么首轮不选-semanticchunker-或-adaptive-chunking) | 方案取舍 |
| [第 13 节](#13-下一轮如何验证而不是直接说优化成功) | 验证计划 |
| [第 14 节](#14-审计产物复查位置和不可丢失的信息) | 审计要求和产物 |
| [第 15 节](#15-不能得出的结论) | 不能夸大的结论 |
| [第 16 节](#16-术语解释) | 术语对照表 |
| [第 17 节](#17-当前决策与待确认事项) | 实施前的历史决策记录 |
| [第 18 节](#18-2026-08-18-实施与离线审计记录) | 实施和离线审计历史记录 |
| [第 19 节](#19-最新讨论先定向替换错题答案文档再验证新分块) | 定向实验方案及执行过程 |
| [第 20 节](#20-新分块真实成果与逐题问题复核) | 本轮真实结果、严格候选统计和逐题复核结论 |
| [第 20.8 节](#208-更换-qwenqwen35-35b-a3b-后的-30-题测试) | Qwen 模型对照测试 |
| [第 20.9 节](#209-更换-qwenqwen3-vl-reranker-8b-后的-30-题对照) | Rerank 模型对照测试 |
| [第 21 节](#21-下一轮提议1022-篇语料上的结构保护语义切分对照) | 结构保护加长段落按内容断开方案与验证计划 |
| [第 22 节](#22-1022-篇资料500-道开发题的实际对照结果) | Markdown 结构化切分与语义切分的完整开发对照 |

### 关键文件索引

| 用途 | 文件 |
| --- | --- |
| 本分析稿 | `docs/rag-chunking-analysis.md` |
| Trellis 实施记录 | `.trellis/tasks/08-18-enterpriserag-structured-chunking-isolation/implement.md` |
| 旧版 500 题基线汇总 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/baseline-rag-010/summary.json` |
| 新分块语料 manifest | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/manifest.json` |
| 新分块完整逐题链路 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/structured-chunking-analysis-30-retry-005/structured-chunking-merged-results.jsonl` |
| 新分块影响汇总 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/structured-chunking-analysis-30-retry-005/structured-chunking-impact-summary.md` |
| 新分块人工复核 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/structured-chunking-analysis-30-retry-005/structured-chunking-manual-review.md` |
| 新分块逐题候选报告 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/structured-chunking-analysis-30-001/candidate-audit.md` |
| 97 道原始候选事实审计 | `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/t9-rewrite-fusion-evidence-audit-001/raw-candidate-fact-audit-summary.json`、`raw-candidate-fact-audit.md` |
| `qst_0295` 标准来源原文 | `tmp/rag-benchmarks/enterprise-rag-bench/derived/v1/en-control/representative/full/documents/enterprise__dsid_4b6a0f839f514ca794314398409fc883.md` |

> 注：上表只列可复查的文件位置。`structured-chunking-merged-results.jsonl` 是 retry 链的离线合并审计输入；它不是一次新的模型运行，也不改变原始 retry 的中断状态。

---

## 1. 本次评测的边界：测的是什么，没有测什么

### 1.1 正式语料和隔离集合

正式结论只来自 EnterpriseRAG 原始英文语料，不使用中文 smoke 的结果。Representative 语料由以下文件组成：

| 文档角色 | 数量 | 具体作用 |
| --- | ---: | --- |
| 标准证据文档 | 722 | 出题数据指定的正确来源文件。一道题可能需要其中一篇，也可能需要多篇共同回答。 |
| 普通干扰文档 | 6,500 | 不是本题答案，但内容、术语或主题可能相近，用来模拟企业知识库里真正会出现的干扰。 |
| 合计 | 7,222 | 本轮 Representative 语料。 |

语料已冻结在独立集合：

```text
rag_eval_enterpriserag_enterpriserag_en_representative_sf_001
```

它不是业务默认集合 `tutorial_verify_embeddings`。本系列评测没有向默认集合写入、删除或清理数据；已经完成的 T9、T10 也都复用该只读评测集合，没有重新入库。

### 1.2 为什么 500 道题分为 300 + 200

500 道题先完整跑了一次，得到优化前的共同基线。之后固定拆为两个互不重叠的集合：

| 集合 | 题数 | 大白话用途 | 可以看到什么 |
| --- | ---: | --- | --- |
| `analysis` | 300 | 医生拿来查病因的病例。可以逐题看哪里没找到、哪里材料到了却没答全，从而决定只改哪一个东西。 | 可以审计逐题问题、证据、回答和失败原因。 |
| `validation` | 200 | 最后考试题。不能先拿它的错题来改系统。 | 只保留聚合结果，题目、答案、证据和失败详情保持隐藏。 |

这不是训练集和模型训练。RAG 没有用这 300 道题更新模型参数。区别在于：如果开发者看了某道题的标准答案和失败原因，再专门调整切分、提示词、检索规则让它答对，那么这道题已经不适合再证明“新方案能处理没见过的问题”。

因此，正确流程是：

```text
先对全部 500 道题跑优化前基线
  -> 用 300 道 analysis 找出问题和选定唯一变量
  -> 在 analysis 做小范围、可审计的对照实验
  -> 锁定方案后才使用从未参与找方向的 200 道 validation 验证
```

这也解释了两个常见问题：

- **“为什么 200 道也要先跑？”** 因为要有同一时间、同一语料、同一配置下的优化前对照分数。优化后仍可把它和自己的基线逐题/汇总比较；只是开发阶段不看它的内容和错因。
- **“为什么不能只拿优化后的 500 道和优化前的 500 道对比？”** 可以比较，但如果反复根据这 500 道的细节调方案，最终分数会混入“记住这些题怎么修”的成分。把 200 道内容锁住，是为了在最后保留一次相对公平的检查。

### 1.3 已完成的正式基线

正式 Representative 完整 RAG 基线：

| 项目 | 值 |
| --- | --- |
| `evaluation_id` | `baseline-rag-010` |
| 完成数 | 500 / 500 |
| 保留的旧成功题 | 497 |
| 只重试的剩余题 | 3 |
| 题级 `evaluation_error` | 0 |
| 本轮唯一运行时改动 | 外层单题总时限由 300 秒改为 600 秒；单次模型请求仍为 90 秒 |

模型、Embedding、提示词、`top-k`、Rerank、Auto-merging、分块策略、语料和 Milvus 集合均未改变。`baseline-rag-010` 的原始汇总在：

`output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/baseline-rag-010/summary.json`

---

## 2. 一道题实际经过了哪些步骤

下面的链路描述的是已运行的完整 RAG，不是只做“搜文件”的 retrieval-only 测试。

```text
英文问题
  -> 判断是否需要改写问题或拆成子问题
  -> Dense 语义检索 + BM25 关键词检索
  -> 合并候选
  -> Auto-merging 尝试恢复较完整的父级上下文
  -> Rerank 对候选再排序
  -> 选出最终 top-8 上下文片段
  -> 主模型根据这 8 段材料生成答案
  -> 独立判卷模型把答案与题目、参考答案对照
  -> 写入逐题 JSONL、可读报告、汇总指标和人工复核队列
```

### 2.1 几个容易混淆的位置

| 位置 | 它拿到什么 | 不能保证什么 |
| --- | --- | --- |
| 原始候选 | 第一次检索到的许多 L3 小片段 | 不保证所有标准文件或所有关键事实都已进入。 |
| 相邻扩展候选 | 命中 L3 的前后 L3 块也加入候选池 | 不保证它们会通过 Rerank，也不保证最终进入 top-8。 |
| Auto-merging 后候选 | 可能用父块替代若干叶子块，给模型更完整上下文 | 不保证父块刚好覆盖答案所需的每句话。 |
| Rerank 输入/输出 | Rerank 输入是待比较材料；输出是按问题相关性重新排序的材料 | 不保证“同一文件”或“关键事实”一定排在前 8。 |
| 最终上下文 | 实际送给回答模型的 8 段材料 | 文件被找回过，不等于关键句真的在这 8 段里。 |

举一个不依赖具体题目的例子：一份事故报告被切成 A、B、C 三段。A 说“服务在 10:03 出现异常”，B 说“根因是路由规则回滚”，C 说“修复工单是 ENG-2422，目标日期为 2 月 7 日”。问题要求根因、工单和日期。如果搜索只命中 A，文件名虽然可能是对的，但答题需要的 B、C 并没有在模型眼前；如果 B、C 被放进候选池但 Rerank 后没有进入最终 8 段，结果仍然一样。

### 2.2 Rerank 到底在做什么

检索的第一轮相当于先从几万段材料里挑出一批“看上去相关”的候选。Rerank 是第二次排序：它同时看“问题”和“候选片段”，把更像能直接回答问题的片段排前面。

例如问题问“工单编号和目标上线日期”，第一轮可能找出十段都包含“故障”或“路由”的材料；Rerank 理想情况下应把同时包含编号和日期的那段推到前面。它不会创造原文没有的事实，也不会自动把距离很远的同文件段落拼在一起。

项目已经具备并使用 Rerank。当前问题不是“系统没有 Rerank 代码”，而是要逐题审计：关键事实有没有进入 Rerank 输入、有没有从输入排到最终 top-8、以及最后模型有没有使用它。

### 2.3 召回是什么意思

“召回”就是在资料库里把本题需要的材料先找回来。它只回答“东西有没有被找到”，不回答“最后是否排在前面”或“模型是否答对”。

如果正确资料在原始候选中，就表示它被召回；如果在最终 top-8 中，就表示它不仅被召回，还通过了后续筛选；如果模型回答用了它且信息完整，才算问答最终成功。

---

## 3. 指标是怎么算出来的

实现位置：`backend/evaluation/metrics.py` 的 `multihop_metrics()`。以下口径与 `baseline-rag-010/summary.json` 一致。

### 3.1 回答通过率：52.40%

独立判卷模型对每题输出：

| 判定 | 数量 | 解释 |
| --- | ---: | --- |
| `pass` | 262 | 回答满足题目和参考答案。 |
| `fail` | 203 | 回答错误、不完整、答偏或缺少关键限制。 |
| `review` | 35 | 自动判卷不能可靠判断，需要人工确认。 |

计算是：

```text
回答通过率 = pass 数 / (pass + fail + review)
           = 262 / 500
           = 52.40%
```

`review` 放在分母中但不算通过，所以这是偏保守的自动指标。它不是“52.4% 的文件被搜到”，而是“自动判卷认为答案完整正确的题占 52.4%”。

### 3.2 证据全覆盖率：70.00%

对每道有标准证据的可回答题，先看题目要求的全部标准文件是否都出现在最终证据文件列表里：

```text
单题全覆盖 = 该题所有 expected_evidence_filenames 都被最终证据命中
证据全覆盖率 = 全覆盖的可回答题数 / 可回答题总数
             = 336 / 480
             = 70.00%
```

20 道 `info_not_found` 题是故意设计成“知识库没有可靠答案”的拒答题，没有应该被找到的标准证据，因此不在全覆盖率分母里。

这个指标检查的是“标准文件是否出现”，不检查关键句是否真的被保留到给模型的上下文，更不检查模型有没有把这句话写进答案。所以它比回答通过率高是合理现象。

### 3.3 平均证据覆盖率：74.66%

每道题先算：

```text
单题证据覆盖率 = 命中的标准证据文件数 / 该题要求的标准证据文件数
```

例如：一题要求 4 个标准文件，最终命中其中 3 个，则该题为 75%。全部 500 题按代码中的规则汇总：

```text
所有题覆盖率之和 = 373.3095
平均证据覆盖率 = 373.3095 / 500
                 = 74.66%
```

`info_not_found` 题按评测定义处理为覆盖完整，因此这个值不能误读成“回答中有 74.66% 内容正确”。它只是文件层面的平均命中情况。

### 3.4 正确拒答率：95.00%

只看 20 道 `info_not_found` 题：

```text
正确拒答率 = 正确说“没有可靠资料，不能回答”的题数 / 无答案题数
             = 19 / 20
             = 95.00%
```

这说明系统在没有可靠资料时大多数时候没有硬编答案。它不说明系统在有资料时一定答得好。

### 3.5 人工复核率和系统异常

```text
manual_review_rate = 自动 verdict 为 review 的题数 / 全部题数
                   = 35 / 500
                   = 7.00%
```

`review` 是“答案有歧义或判卷拿不准，需要人看”，不是系统崩溃。API 失败、回答生成超时、判卷请求超时或解析失败属于 `system_error`；它们必须与回答质量问题分开记录。

### 3.6 P50 延迟

P50 是中位数，不是平均值：一半样本比它快，一半样本比它慢。

| 阶段 | P50 |
| --- | ---: |
| 端到端 | 72.19 秒 |
| RAG 阶段 | 35.88 秒 |
| 回答生成 | 15.51 秒 |

---

## 4. 500 题基线结果：哪些题更难

`baseline-rag-010` 的聚合结果如下。按题型表只给汇总数，不泄露 validation 的逐题内容。

| 题型 | 题数 | 证据全覆盖率 | 平均证据覆盖率 | 自动回答通过率 | 人工复核率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `basic` | 175 | 81.71% | 81.71% | 68.00% | 2.29% |
| `completeness` | 20 | 25.00% | 42.64% | 25.00% | 5.00% |
| `conflicting_info` | 20 | 80.00% | 87.50% | 75.00% | 0.00% |
| `constrained` | 30 | 90.00% | 91.67% | 36.67% | 13.33% |
| `high_level` | 10 | 0.00% | 0.00% | 10.00% | 30.00% |
| `info_not_found` | 20 | 不适用 | 100.00% | 95.00% | 5.00% |
| `intra_document_reasoning` | 40 | 82.50% | 82.50% | 62.50% | 0.00% |
| `miscellaneous` | 20 | 90.00% | 90.00% | 80.00% | 5.00% |
| `project_related` | 40 | 50.00% | 79.45% | 5.00% | 32.50% |
| `semantic` | 125 | 59.20% | 59.20% | 39.20% | 6.40% |

可以从表里看到两类不同问题：

1. **材料没有完整找回或没进入上下文**：例如 `completeness`、`semantic`、`high_level` 的覆盖较低；
2. **材料文件找回了，答案仍然不行**：例如 `constrained` 的证据全覆盖 90%，但回答通过只有 36.67%。这更像回答漏限制条件、混入相似方案、没有按问题范围收束，而不只是检索问题。

所以不能只看到 70% 的文件级全覆盖就说“检索没问题”，也不能只看到 52.4% 就一口咬定“分块是全部根因”。后续审计正是为了把这些位置拆开。

---

## 5. T8：300 道 analysis 的自动失败初筛

T8 只分析 `analysis` 的 300 道题，不等于总评测只跑了 300 道。500 题总基线已经完成；300 题只是为了公开查找根因。

自动判卷分布：

```text
153 pass
123 fail
 24 review
```

自动分类结果：

| 自动分类 | 数量 | 它表示什么 |
| --- | ---: | --- |
| `retrieval_miss` | 63 | 预期证据文件没有在最终结果中完整出现。 |
| `retrieval_late` | 19 | 标准材料出现得太靠后，存在最终上下文被截掉的风险。 |
| `evidence_incomplete` | 19 | 标准证据未完整覆盖。 |
| `answer_failure` | 122 | 自动判卷认为答案不符合参考答案。 |
| `human_review` | 23 | 自动判卷是 `review`，且没有检测到 API/超时异常。 |
| `system_error` | 2 | API、生成、证据评分、判卷或超时异常。 |
| `hard_distractor` | 0 | 没有被自动规则识别为难干扰文件导致的失败。 |
| `refusal_failure` | 0 | 没有发现无答案题错误拒答。 |

这些类别可以重叠，不能相加。例如同一道题可能既是 `retrieval_miss`，也因为缺证据而被判成 `answer_failure`。`human_review` 不是“系统异常”；`system_error` 才是运行/服务问题。当前自动初筛源文件为：

`output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/baseline-rag-010/analysis-failure-classification.json`

### 5.1 这一步不能直接叫“最终根因”

自动规则只知道标准文件名、排名、覆盖率、模型回答和错误字段。它通常不知道：

- 同一标准文件里，真正的答案句是不是落在另一个 L3；
- 候选材料进入 Rerank 前后在哪一步丢失；
- 答案里额外的一句话是否真的有害，还是自动判卷过严；
- 生成或判卷服务异常时，题目本身是否其实很容易。

所以 T8 的作用是把人工精力集中到最值得看的题，而不是把类别数字当作最终诊断。

### 5.2 analysis 中的具体例子

下面只使用 analysis 题，不包含 validation 细节。

| case ID | 观察到的现象 | 应归到哪里 |
| --- | --- | --- |
| `qst_0208` | 标准文件排名是第 1，但回答生成超时，答案为空。 | `system_error`；不能说它是分块或模型知识失败。 |
| `qst_0089` | 三个 precision mode 基本答出，但 `aggressive` 漏了“近期 validator 成功窗口”的限定。 | `human_review` / 回答完整性问题；标准文件已在最终证据。 |
| `qst_0151` | 核心因果链正确，但回答写出的 sudo 错误消息与参考答案细节不一致。 | `human_review`；不能直接归为检索漏召回。 |
| `qst_0152` | 正确来源文件命中，但回答把配置推送当成根因，漏掉题目要求的直接原因 “LACP uplink flap”。 | 证据到位而回答整合失败，或选取了相似材料。 |

---

## 6. 97 道证据链候选审计：分块问题到底占多少

为进一步追问“材料究竟在哪里丢了”，从 T8 的 `retrieval_miss`、`retrieval_late`、`evidence_incomplete` 三类取去重并集：

```text
63 + 19 + 19 = 101 个类别命中
存在 4 个类别重叠
去重后 = 97 道 analysis 题
```

这 97 道不是随机抽样，而是专门挑出的“证据链有风险”的题。因此比例只能用来解释这批题，不能直接外推为全体 500 题比例。

人工候选链路复核结果：

| 候选材料状态 | 题数 | 大白话解释 |
| --- | ---: | --- |
| 标准来源没有完整进入原始候选 | 30 | 系统第一轮找材料时，就没有把这题所需的完整标准来源找齐。 |
| 标准文件进入了，但关键事实在该文件的其他 L3 | 41 | 文件名看起来“找到了”，但模型看到的那一小段没有关键句。 |
| 其中：关键事实就在直接相邻 L3 | 11 | 前后紧挨的一段就能补上；这是相邻 L3 扩展能处理的窄场景。 |
| 其中：关键事实在同文件更远 L3 | 30 | 前后各补一段也不够，说明不是简单边界问题。 |
| 自动事实缺口判断错误 | 4 | 自动规则把没有问题的题误判成事实缺口。 |
| 原始候选材料已完整 | 19 | 问题主要在后续 Rerank、最终 top-8 或回答整合，而不是“原始材料没来”。 |
| 无法判断 | 3 | 现有证据不能可靠定位具体丢失点。 |

### 6.1 “原始候选不完整”与“最终 8 段不完整”不是一回事

这两个问题必须分开：

```text
原始候选不完整
  = 第一轮检索就没把足够材料带回来

原始候选完整，最终 8 段不完整
  = 材料来过，但在 Auto-merging、Rerank、阈值或 top-8 截断时被筛掉

最终 8 段完整，答案仍失败
  = 回答模型遗漏、混淆、答得太宽或太窄，或者判卷需要人工确认
```

这 97 道审计最重要的发现是：**当前问题不是简单的“候选太少”。** 有 41 道题属于“文件命中了但关键句落在别的 L3”，其中 30 道甚至不在直接相邻位置。增加一圈相邻块只能触及其中的一部分。

### 6.2 为什么这可以怀疑分块，但不能把所有失败都叫分块问题

“标准文件进来了，但关键事实落在别的 L3”说明当前 800 字符左右的叶子块把同一语义单元切开了，或者切分时没有尊重 Markdown 的标题、列表、表格和段落结构。这是分块/上下文组织问题的强信号。

但以下情况不该草率写成分块问题：

- 30 道原始候选完全没有完整标准来源：可能是向量检索、BM25、查询表达、候选召回量或难干扰造成；
- 19 道原始候选已完整：更可能是 Rerank、最终 top-8 或回答阶段；
- 9 道 T10 运行期系统异常：是服务可靠性问题；
- `review`：答案和标准答案可能只是在措辞、额外信息或细节上有歧义。

结论应表述为：**结构性分块是值得优先验证的主要假设，不是未经验证的“全部根因”。**

---

## 7. 已试方案一：T9 改写候选融合

### 7.1 想解决什么

复杂问题有时会被系统改写成一个更具体的新问题。原先的担心是：系统用改写问题重新搜索后，只保留第二次候选，丢掉第一次搜索已经找到的材料。

T9 的实现思路是：

```text
原问题第一次候选
  + 改写问题第二次候选
  -> 以稳定 chunk_id 去重
  -> 对融合后的材料继续既有 Auto-merging、Rerank、阈值和最终 top-8
```

唯一变量是 `rewrite_candidate_fusion`。模型、Embedding、`top-k`、Rerank、分块和语料不变。

### 7.2 15 道定向结果

运行：`t9-rewrite-fusion-targeted-003`，只读 Representative 集合。

| 指标 | 结果 |
| --- | --- |
| 定向 analysis 题数 | 15 |
| 实际进入融合分支 | 7 / 15 |
| 触发融合且证据覆盖提升 | 0 / 7 |
| 触发融合且答案变为通过 | 1 / 7 |
| 新增系统错误 | 0 |

唯一“答案变通过”的 `qst_0458` 在基线时证据本来已经完整，因此无法证明是融合把缺失证据补回。其余 8 道没有进入改写分支，不能归因给融合变量。

### 7.3 97 道审计后的结论

在 97 道证据链审计里，实际进入融合分支的只有 8 道。对绝大多数题，这个变量根本没有被执行，自然不能解释主要失败。

因此没有把 T9 扩大到 300 道 analysis，也没有进入 validation。该实验保留记录，作为“候选融合没有显示稳定、可归因的证据收益”的证据，不删除、不覆盖。

---

## 8. 已试方案二：T10 相邻 L3 扩展

### 8.1 做法到底是什么

对第一次检索命中的每个 L3 小块，按**同一个文件、同一个 L3 序号**读出它前一个和后一个 L3：

```text
命中 L3 #12
  -> 加入同文件 L3 #11 和 L3 #13
  -> 稳定去重
  -> 仍走原来的 Auto-merging、Rerank、阈值、最终 top-8
  -> 给回答模型的最终段数仍是 8
```

它不是扩大最终回答上下文，也不是再做一次向量检索，更没有调用新的 Embedding。它只是在候选池里补上“当前命中块的紧邻上下文”。

### 8.2 11 道直接相邻缺口的定向验证

运行：`t10-adjacent-l3-expansion-targeted-001`。这 11 道题是前述 97 道审计中人工确认“关键事实就在命中 L3 前后”的题。

自动结果：

| 指标 | 结果 |
| --- | ---: |
| 题数 | 11 |
| 证据全覆盖率 | 72.73% |
| 平均证据覆盖率 | 72.73% |
| 自动回答通过率 | 45.45% |
| 端到端 P50 | 134.07 秒 |
| RAG P50 | 39.80 秒 |
| 生成 P50 | 17.03 秒 |

人工能确认“直接相邻材料补回关键事实，并且最后回答完整正确”的题有 5 道：

| case ID | 关键事实如何补回 |
| --- | --- |
| `qst_0039` | 命中 L3 #12 后，L3 #13 补回 Performance/Compliance 门。 |
| `qst_0068` | 命中 L3 #15 后，L3 #14 补回 Hosted 5 ms。 |
| `qst_0082` | 命中 L3 #1 后，L3 #2 补回网络根因与 KV 预热事实。 |
| `qst_0158` | 命中 L3 #0 后，L3 #1 补回三个 preset 名称。 |
| `qst_0449` | 六份来源均从命中块的直接相邻块补回，支持完整的计数比较。 |

这个结果证明相邻扩展对“答案刚好被边界切到前后块”的题有效，但它本来就是按照这种现象筛选出的 11 道题，不能当成全体错题的成功率。

### 8.3 扩大到 147 道 analysis 非通过题

运行：`t10-adjacent-l3-expansion-analysis-147-002`。

输入是 `baseline-rag-010` 的全部 147 道 analysis 非通过题：123 `fail` + 24 `review`。不包含原本 153 道 `pass`，不包含任何 validation。之前已成功的 75 道结果被保留，以 10 并发只补跑缺失的 72 道；补跑题级 `evaluation_error=0`。

自动汇总：

| 指标 | 冻结基线（这 147 道） | 相邻扩展后 | 自动变化 |
| --- | ---: | ---: | ---: |
| 证据全覆盖率 | 42.86%（63 题） | 55.78%（82 题） | +12.93 个百分点 |
| 平均证据覆盖率 | 48.38% | 62.70% | +14.32 个百分点 |
| 自动回答通过率 | 0.00% | 27.89%（41 / 147） | +27.89 个百分点 |
| 零证据覆盖题 | 68 | 46 | -22 |

候选与耗时：

| 项目 | 值 |
| --- | ---: |
| 实际执行相邻扩展 | 147 / 147 |
| 每题新增相邻候选 P50 | 48 |
| 每题新增相邻候选 P95 | 57 |
| 每题新增相邻候选平均数 | 47.08 |
| 端到端 P50 | 87.28 秒 |

本轮使用 10 个 worker 是为了缩短批量评测等待时间，不是 RAG 逻辑变化。实际运行时按服务方给出的上限控制并发：

| 服务 | 记录的限制 |
| --- | --- |
| BGE-M3 Embedding | 最高 2,000 RPM；最高 500,000 TPM。 |
| DeepSeek 回答模型 | 最高 500 RPM；最高 2,000,000 TPM。 |
| Rerank | 最高 2,000 RPM；最高 1,000,000 TPM。 |

单题模型请求仍是 90 秒上限，`baseline-rag-010` 的外层单题总时限是 600 秒。并发只影响同一时间发多少道题，不能改变单题的候选内容；因此重试时可以改变 worker 数，但必须在 `evaluation-config.json` 中单独记录，不能把它写成算法提升。

此前讨论的“60 篇合并候选”和“最终 8 段”也不是同一个集合：前者是某道题在中间链路中的候选池（具体数量随题目和分支变化），后者是经过 Auto-merging、Rerank、阈值和 top-k 截断后真正送给回答模型的上下文。只有候选 trace 能证明某个标准材料从候选池一路保留到最终 8 段，才能说它对答案有直接证据作用。

不要把 `87.28 - 72.19 = 15.09 秒` 写成“相邻扩展让每题慢了 15.09 秒”。前者来自专门挑出的 147 道历史难题，后者来自完整 500 题，题目难度与复杂度分布不同。要准确估计成本，需要同一批题在同一服务状态下只开关一个变量并对照 P50/P95。

### 8.4 为什么自动显示 41 道通过，人工只确认 5 道收益

自动“从非通过变通过”只是结果变了，不足以证明变因就是相邻块。人工归因要求同时成立：

1. 关键事实确实在原命中 L3 的直接相邻块；
2. 该相邻块被扩展加入；
3. 它穿过后续筛选，进入最终上下文；
4. 答案使用了这条材料，并完整正确。

人工结论：

| 现象 | 数量 | 为什么不能都算相邻扩展收益 |
| --- | ---: | --- |
| 自动通过且证据覆盖提升 | 20 | 只有其中 5 道能事实级证明为“直接相邻补回”。另 15 道可能是重跑波动、已有材料、或缺少完整链路证明。 |
| 自动通过但覆盖不变 | 21 | 没有证据说明相邻扩展真正发挥作用。 |
| 覆盖提升但答案仍未通过 | 15 | 材料进来不代表模型会把所有限制条件答出来。 |
| 覆盖下降 | 19 | 不能直接认定为扩展导致退化；可能在 Auto-merging、Rerank 或最终 top-8 丢失。 |
| 系统异常 | 9 | 2 回答超时、3 证据评分不可用、4 判卷超时，不能算 RAG 或分块失败。 |

因此保守、可归因的收益是：

```text
5 / 147 = 3.40%
```

这不是 500 题总体提升，也不是 validation 泛化结果。它证明“补一格前后文”有用，但只能解决一小部分边界型缺口。

### 8.5 相邻扩展不能解决的例子

- `qst_0359`：相邻块进了扩展候选池，但在最终上下文前被丢掉，说明候选扩展本身不能保证材料留存；
- `qst_0382`：证据覆盖有改善，回答仍不完整，说明材料补齐与回答合成是两件事；
- `qst_0152`：存在相关材料，答案仍漏掉直接原因 “LACP uplink flap”；
- `qst_0211`：把相似的轮询补救误当成本题要求的 SSE passthrough / 60 秒 keepalive 方案。

这就是为什么下一步不应继续把候选池无上限扩大：它会增加精排负担，却不能自动解决“远距离事实”“最终筛掉”“回答漏写”三种主要问题。

---

## 9. 当前项目的实际分块方式

实现文件：`backend/indexing/document_loader.py`。

### 9.1 当前已经在用 LangChain RecursiveCharacterTextSplitter

项目当前导入的是：

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter
```

所以“改成 LangChain RecursiveCharacterTextSplitter”不是新优化，项目现在就是它。当前策略是把一整页或整篇 Markdown/TXT 文本递归切成三层：

```text
整页 / 整篇文本
  -> L1：目标 2400 字符，重叠 400
  -> 在每个 L1 内再切 L2：目标 1600 字符，重叠 200
  -> 在每个 L2 内再切 L3：目标 800 字符，重叠 100
```

L3 是最细的叶子检索单位；L1、L2 保留父子关系，供 Auto-merging 向上恢复较大上下文。

目前使用的优先分隔符为：

```python
["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""]
```

它会优先从段落、中文句末、换行、逗号和空格处找断点，实在找不到才按字符截断。这在一般纯文本上是合理的“尽量别把句子腰斩”的策略。

### 9.2 当前策略具体缺什么

对于 Markdown，当前加载逻辑把整篇正文交给递归切分器，没有先按 `#`、`##`、`###` 解析标题层级。因此：

- 一个块虽然可能包含标题那一行文字，但系统不知道它属于哪条标题路径；
- 表格、列表、标题下的定义和后面的例外说明，都只是普通字符流；
- 当一个章节超过 800 字符，切分器只能按段落/句子/字符找边界，不能表达“这几条列表是同一个配置项”；
- 同一份文档里相距较远但同属某个标题章节的事实，无法因为共享标题自然聚在一起；
- 切分器启用了 `add_start_index=True`，但当前生成的块字典只提取正文和基础文件 metadata，没有把 `start_index` 作为最终块字段写出。后续审计只能依赖块序号和文本对照，而不是稳定的原文起止范围。

这不是说现有切分“完全没用”。它已经避免了大量粗暴的固定长度硬切，也有 L1/L2/L3 父子层级。问题在于企业 Markdown 中常见的“标题 -> 条款 -> 列表 -> 表格 -> 例外”结构没有成为可检索、可审计的 metadata。

### 9.3 什么叫“分块把答案切断”

假设一个 Markdown 章节是：

```markdown
## Failover behavior

- Trigger: noisy reachability signal.
- Guardrail: require three consecutive failures before rollback.
- Follow-up: ENG-2422, ship by 2026-02-07.
```

如果切分恰好把 `Trigger` 留在 L3 #12、把 `Guardrail` 和 `Follow-up` 放到 L3 #13，用户问“原因、保护条件、工单和日期”时，L3 #12 很容易因为问题中有 “failover” 被命中，但它本身不能完成回答。当前的 100 字符重叠可能不足以把后面两条完整带进来。

相邻扩展可以临时补 L3 #13；但如果 `Follow-up` 在同节更远的 L3 #16，或者表格被切成好几段，前后各一块仍然无法解决。

---

## 10. 讨论过的外部分块方案及其适用性

下面的链接是讨论时查阅或引用的资料。外部项目/论文的结果只能说明“该方案在它自己的数据集上发生过什么”，不能直接当成本项目的效果承诺。

用户提供的选型文章：

- [工业级的 RAG 优化选型（用户提供）](https://sealearn.yuque.com/org-wiki-sealearn-syaeae/oi5uvb/sy2eibydtutm1g13#7863fc01)

### 10.1 LangChain RecursiveCharacterTextSplitter

资料：

- [LangChain Text Splitters 总览](https://docs.langchain.com/oss/python/integrations/splitters)
- [Recursive Text Splitter](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter)

它的做法是按一组分隔符由大到小递归尝试，尽可能不在词/句中间切断。优点是便宜、快、长度可控；缺点是它本身不知道 Markdown 的标题树，也不知道一组列表或一张表是否应作为一个整体。

本项目已在使用它，因此保留它作为“章节内部控制最大长度”的第二层工具是合理的，单独换名或微调几个分隔符不应被包装成一次全新方案。

### 10.2 LangChain MarkdownHeaderTextSplitter + Recursive

资料：

- [LangChain Markdown splitter 源码](https://github.com/langchain-ai/langchain/blob/8fed1dd641f49b96d69ad3bfbb922ba6e45c2878/libs/text-splitters/langchain_text_splitters/markdown.py)
- [LangChain 结构化分块文章](https://www.langchain.com/blog/a-chunk-by-any-other-name)
- [MarkdownHeaderTextSplitter 的段落格式问题记录](https://github.com/langchain-ai/langchain/issues/22256)

它先用 Markdown 标题拆出章节，并把标题文本写进 metadata，再对每个章节内部做长度切分。一个可实现的基本调用形态是：

```python
header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[
        ("#", "h1"),
        ("##", "h2"),
        ("###", "h3"),
        ("####", "h4"),
    ],
    strip_headers=False,
)
sections = header_splitter.split_text(markdown_text)

leaf_splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=100,
    separators=["\n\n", "\n", ". ", " ", ""],
    add_start_index=True,
)
leaf_docs = leaf_splitter.split_documents(sections)
```

这与当前实现的区别不是“都能按长度切”，而是先把 `文档 > 一级标题 > 二级标题 > 三级标题` 作为结构上下文保存下来。后续每个叶子块可以带上标题路径前缀，例如：

Context7 当前文档还特别说明：应使用 `split_documents()` 保留标题 metadata；`chunk_overlap` 只在同一章节超过长度、需要继续细分时生效，不会跨不同标题强行重叠。因此，标题路径和章节内的结构保护必须先设计好，不能把“设置了 overlap”误解为所有相邻章节都会自动拼接。

```text
Document: Failover handbook
Section: Operations > Failover behavior > Guardrails

Trigger: noisy reachability signal.
Guardrail: require three consecutive failures before rollback.
```

查询 “rollback guardrail” 时，标题本身会成为可检索文本的一部分；模型拿到块时，也能判断这段话属于哪个章节，而不是只看到一截正文。

要注意开源 issue 中记录的格式/段落行为差异。项目落地时必须用 EnterpriseRAG 的真实 Markdown 做单元测试，尤其检查列表、表格、空行和标题下的短段落，不能只照抄示例。

### 10.3 LangChain SemanticChunker（语义切分）

资料：

- [LangChain SemanticChunker 文档入口](https://python.langchain.com/docs/how_to/semantic-chunker/)
- [Is Semantic Chunking Worth the Computational Cost?](https://aclanthology.org/2025.findings-naacl.114/)

语义切分大意是：

```text
先按句子切开
  -> 为每句或句群额外生成 Embedding
  -> 比较相邻句子的语义距离
  -> 距离突变的地方切成新块
```

它与 Recursive 的区别是：Recursive 主要看字符长度和分隔符；SemanticChunker 试图在“话题明显变了”的地方切。

它的限制也很直接：

- 入库时需要额外 Embedding，7,222 篇语料成本和耗时会上升；
- 输出块长度不稳定，仍要二次限制最大长度；
- 它并不天然理解 Markdown 标题、列表和表格；
- 如果两条关键事实在同一章节但相隔很远，语义距离规则也不会自动把它们拼回来；
- 相邻句子看似语义变化，可能恰好把“条件”和“例外”切开。

结论：可以以后在小样本做对照，但当前不适合直接替换全量 7,222 篇语料的分块。它增加的是离线入库成本，不是免费得到“更完整答案”。

### 10.4 Adaptive Chunking（自适应分块）

资料：

- [Adaptive Chunking GitHub](https://github.com/ekimetrics/adaptive-chunking)
- [Adaptive Chunking 论文](https://arxiv.org/abs/2603.25333)

Adaptive Chunking 不是一个单一切分器。它会为同一文档生成多套候选分块，例如小递归块、大递归块、按标题的结构块、页面块、语义块，然后用一些质量规则挑更适合的块。常见评价项包括：长度是否合规、块内语义是否一致、上下文是否连贯、表格/列表等结构是否完整、引用是否还在原块内。

该项目公开报告的一个 99 问题实验中：

| 方案 | Retrieval Completeness | Answer Correctness | 成功回答数 |
| --- | ---: | ---: | ---: |
| LangChain Recursive | 58.1 | 70.1 | 49 / 99 |
| Adaptive Chunking | 67.7 | 78.0 | 65 / 99 |

这是该外部项目自己的评测，数据、模型、文档和指标都不同，不能预言 EnterpriseRAG 的提升。它的实际代价是每篇文档多生成候选块、多做评估，通常还伴随额外 Embedding，工程复杂度和入库成本都更高。

当前决策是：**不把 Adaptive Chunking 作为第一轮方案。** 先验证更低成本、可解释的结构化标题分块；只有它仍无法改善“同一章节被拆碎”的问题，再讨论是否值得做一个有限样本的 Adaptive 对照。

### 10.5 标题路径前缀和结构感知分块

资料：

- [Structure-Aware Semantic Chunking with Title-Chain Prefixes](https://arxiv.org/html/2608.00824v1)
- [Hermes Agent 的 Markdown 原子块、合并与超长拆分实现](https://github.com/NousResearch/hermes-agent/blob/6680afba4a5580d1fcc39e1e85fcb1ac5ae9ca4c/gateway/platforms/helpers.py)
- [NVIDIA: Finding the Best Chunking Strategy for Accurate AI Responses](https://developer.nvidia.com/blog/finding-the-best-chunking-strategy-for-accurate-ai-responses/)
- [Weaviate: Chunking Strategies for RAG](https://weaviate.io/blog/chunking-strategies-for-rag)

标题路径前缀的关键点不是“把标题做得漂亮”，而是让每个小块在离开原文后仍带着自己的语境。外部论文报告在一个 Markdown 知识库上，把标题切分、相邻语义合并和标题路径前缀组合后，MRR@5 从 0.374 到 0.463（23.8% 相对提升）。它没有额外 LLM 调用，但只测了一个知识库，因此本项目只能把它当成具体的设计参考。

Hermes Agent 的实现可作为代码形态参考：先按 Markdown 结构形成原子块，合并过短块，再对超长块进行受控拆分。它比“整篇字符串直接递归切”更适合保护标题、段落、列表和代码/表格等边界。

---

## 11. 目前推荐的具体改造，不是抽象口号

推荐的第一版唯一变量是：

```text
document_chunking_strategy = markdown_header_recursive_v1
```

它不是把 Rerank、Embedding、top-k、Prompt 或 Auto-merging 一起改掉。第一版只修改“文档进入 L1/L2/L3 前，怎样按照结构形成块”。

### 11.1 新链路

```text
原始 Markdown / 已解析 Markdown
  -> 清洗文本，但保留 # 标题、列表、表格和代码块边界
  -> MarkdownHeaderTextSplitter 识别 # / ## / ### / ####
  -> 得到带 h1/h2/h3/h4 metadata 的章节
  -> 同标题路径内，把短段落、连续列表项、连续表格行作为候选原子单元
  -> 只在同一标题路径内合并过短单元
  -> 章节超长时，再用 RecursiveCharacterTextSplitter 限制长度
  -> 为每个 L1/L2/L3 写入标题路径、原文范围、前后块 ID 和结构类型
  -> 沿用原来的 L1 -> L2 -> L3 父子关系
  -> L3 向量化并写入新的独立评测集合
  -> 现有 Dense/BM25/Hybrid/Rerank/Auto-merging/top-8 不变
```

### 11.2 具体规则

| 部分 | 第一版规则 | 要解决什么 |
| --- | --- | --- |
| 标题 | 识别 `#` 到 `####`，保存完整标题路径。 | 让块知道自己属于哪个章节。 |
| 标题文本 | 保留在正文，且以简短前缀加入每个子块。 | 标题也参与向量和关键词匹配，模型读块时不丢语境。 |
| 列表 | 连续列表优先视为一个原子单元；超长再从完整条目边界拆。 | 避免“条件在前一块、例外在下一块”。 |
| 表格 | 连续表格行优先同块；超长时重复表头并按完整行拆。 | 避免列名与数值、条件分离。 |
| 段落 | 连续段落优先在同一标题路径下合并；不要跨标题合并。 | 防止把两个主题无意义拼在一起。 |
| 超长章节 | 才调用 Recursive，优先 `\n\n`、`\n`、英文句末、空格，最后才字符截断。 | 保持 800 字符左右的叶子长度可控。 |
| 短块 | 在同标题路径内向后合并短块，达到合理长度后停止。 | 避免只有标题或一行残句的低信息块。 |
| L1/L2/L3 | 初版仍使用 2400/1600/800，重叠仍使用 400/200/100。 | 将“结构策略”作为唯一变化，避免分不清是标题结构还是块大小带来的结果。 |

### 11.3 每块必须新增或保留的审计字段

| 字段 | 含义 |
| --- | --- |
| `chunk_id` | 稳定唯一 ID。 |
| `parent_chunk_id` / `root_chunk_id` | 维持 L1 -> L2 -> L3 的父子关系。 |
| `chunk_level` | 1、2、3。 |
| `chunk_index` | 同文件顺序，用来恢复前后关系。 |
| `heading_path` | 例如 `Operations > Failover behavior > Guardrails`。 |
| `heading_level` | 当前最深标题层级。 |
| `source_start_index` / `source_end_index` | 块在原文中的稳定范围，便于回查。 |
| `previous_chunk_id` / `next_chunk_id` | 同标题路径内的相邻块，而不是猜块编号。 |
| `content_kind` | `paragraph`、`list`、`table`、`code`、`mixed` 等。 |
| `chunking_strategy` | `markdown_header_recursive_v1`。 |
| `chunking_config_hash` | 标题级别、长度、重叠、分隔符规则的配置哈希。 |

这些字段要同时存在于父块存储和 L3 检索记录，不能只写在日志里。以后再次出现“文件找到了，关键句却不在最终上下文”的题，才能精确回答：它是在第几节、第几个块、哪一步被丢掉。

### 11.4 一个新旧对照例子

原文：

```markdown
## API redirect behavior

### Current limitation
- Clients disconnect during cross-region redirects.

### Cause and guardrails
- Noisy reachability signals cause routing to flip and roll back.
- Require three failures before rollback.

### Follow-up
- ENG-2422 will fix reconnect behavior by 2026-02-07.
```

旧链路可能按长度切成：

```text
L3 #12: Current limitation + Cause 的前半句
L3 #13: Cause 的后半句 + Guardrails
L3 #14: Follow-up
```

问题若带有 “redirect” 和 “clients disconnect”，很容易只找到 L3 #12；回答所需的防抖条件、工单、日期分散在 #13、#14。

新链路会先得到三个有名称的章节，再在章节内部控长：

```text
heading_path = API redirect behavior > Cause and guardrails
L3: 标题前缀 + 两条 Cause/Guardrail 列表

heading_path = API redirect behavior > Follow-up
L3: 标题前缀 + ENG-2422 和日期
```

它不保证一个检索块就装下所有事实，但至少不会把 “Cause and guardrails” 这两条同属一个逻辑单元的列表按普通字符流切断；即使 `Follow-up` 独立，也带有标题语境，候选审计能明确解释少了哪个章节。

---

## 12. 为什么首轮不选 SemanticChunker 或 Adaptive Chunking

| 方案 | 是否额外模型/Embedding 调用 | 块长度是否稳定 | 是否保留 Markdown 标题语境 | 当前是否建议直接全量入库 |
| --- | --- | --- | --- | --- |
| 当前 Recursive | 否 | 稳定 | 否 | 已使用；不是新实验变量。 |
| 标题 + Recursive | 否 | 稳定 | 是 | 是，先小范围离线验证。 |
| SemanticChunker | 是，通常每句/句群要额外 Embedding | 不稳定，仍要二次截长 | 否 | 否，成本高且不直击标题/列表结构问题。 |
| Adaptive Chunking | 通常是，多策略候选与质量评估 | 因策略而异 | 取决于实现 | 否，工程和入库成本最高。 |

目前的证据说“同一标准文件里，关键事实在其他 L3”的问题很多。这更贴近“结构信息丢失”，而不是“必须用更贵的句子语义距离才能切”。先用标题、列表和表格边界恢复结构，成本最低、因果最清晰，也便于解释有没有有效。

---

## 13. 下一轮如何验证，而不是直接说优化成功

以下是建议的实验计划，尚未获得“开始重新分块/重新入库”的授权。

### 13.1 阶段 A：离线分块对照，不调用模型、不写 Milvus

目标是先验证新分块是否真的减少“关键事实落在不该分开的 L3”这种现象。

固定样本：从 97 道审计中的 30 道“标准文件已进入，但关键事实在同文件更远 L3”的题开始。它们比 11 道直接相邻题更能检验新结构策略是否解决远距离结构问题。

具体操作：

1. 冻结这 30 道 `analysis` case ID、题目文件哈希和原始文档哈希；
2. 对每份对应原文分别运行旧 `recursive_l1_l2_l3` 和新 `markdown_header_recursive_v1`；
3. 对每题人工标出参考答案每个事实位于旧/新块的哪个 `chunk_id`、标题路径、起止范围；
4. 统计：一个关键事实是否还被断在列表/表格中间、与同一逻辑单元是否仍分离、标题路径是否完整、块是否只有残句；
5. 生成新旧并排的 `chunk-audit.md` 和结构化 JSONL；
6. 不生成 Embedding，不调用 Rerank/回答/判卷，不写 Milvus。

离线阶段的通过条件不能只看“新块看起来更漂亮”。至少应由人工确认：相同题目需要的事实在新块中更完整，或能够以明确标题路径被更稳定地定位；同时不能出现大规模表格破碎、重复内容激增或块长度失控。

### 13.2 阶段 B：建立新的独立评测集合

只有阶段 A 通过，并且用户明确批准后才做：

1. 使用新 corpus run ID，例如 `enterpriserag-en-representative-md-header-recursive-001`；
2. 使用新的 Milvus 集合，例如 `rag_eval_enterpriserag_enterpriserag_en_representative_md_header_recursive_001`；
3. 保存新的 `manifest.json`、文档清单、分块配置、配置哈希、每层块数量和每文件块映射；
4. 不写、不清理、不替换原 Representative 集合，也不触碰 `tutorial_verify_embeddings`；
5. 新实验 `evaluation_id` 必须不同于现有基线，`changed_variable=document_chunking_strategy`；
6. L1/L2/L3 长度、重叠、Embedding、模型、Prompt、top-k、Rerank、Auto-merging 均保持原值。

由于真正改变了文档块及其向量，必须新建独立集合；不能拿旧集合直接假装使用了新分块。

### 13.3 阶段 C：先跑 30 道有明确分块缺口的 analysis 题

不是直接重跑 500 题，也不是一开始就跑全部 147 道历史非通过题。先跑上述 30 道，逐题记录：

| 审计项 | 必须保留的内容 |
| --- | --- |
| 新旧块对照 | 标题路径、块 ID、原文范围、结构类型、相邻块。 |
| 原始候选 | 原始 L3、排名、文件名、文本/必要摘要。 |
| 每个关键事实 | 首次出现在哪一阶段、在哪个块、是否进入最终上下文。 |
| Auto-merging | 子块被哪个父块替换，替换前后内容和排名。 |
| Rerank | 输入候选、输出候选、最终 top-8、被丢弃原因。 |
| 回答与判卷 | 原回答、参考答案、自动 verdict、人工复核理由。 |
| 性能 | RAG、生成、判卷、端到端耗时；候选数、最终上下文数。 |
| 系统异常 | 独立记录，不能合并到回答失败。 |

成功判定应至少分三层报告：

```text
材料层：新分块是否让关键事实进入原始候选/最终上下文
回答层：材料完整后，回答是否真正变为正确
归因层：这次变好能否明确由新分块带来，而不是服务波动或其他路径
```

### 13.4 并发执行方案

并发只用于同时处理多道题或多个互不相关的文档；单道题内部仍然按顺序执行：

```text
单题：检索 -> Auto-merging -> Rerank -> top-8 -> 生成 -> 判卷
      必须保持顺序

多题：case-001 / case-002 / case-003 / ...
      可以由多个 worker 同时处理
```

具体安排：

| 阶段 | 并发方式 | 约束 |
| --- | --- | --- |
| 离线新旧分块 | 按文档并行解析 | 只写各自临时结果，最后由主进程合并；不访问 PostgreSQL、Milvus、Redis。 |
| 新语料准备 | 文档解析和向量批处理可并发 | PostgreSQL 父块采用批量单写入或受控连接池；不能让多个 worker 同时更新同一 `chunk_id`。 |
| 30 道真实评测 | 10 个 worker | 每个 worker 一次只处理一道题；主进程统一 checkpoint、回收异常 worker 和合并结果。 |
| 异常重试 | 只并发缺失/异常题 | 成功题不重复请求；重试必须使用新的 attempt 记录并保留原结果。 |

10 并发是执行吞吐配置，不是 RAG 变量。运行时记录 `evaluation_worker_count=10`，同时遵守已确认的服务限制：BGE-M3 最高 2,000 RPM / 500,000 TPM，DeepSeek 最高 500 RPM / 2,000,000 TPM，Rerank 最高 2,000 RPM / 1,000,000 TPM。单次模型请求仍是 90 秒，外层单题评测时限仍是 600 秒。

并发安全规则：

1. 每道题完成后立即写入 `results.jsonl` 或 attempt checkpoint，进程中断不会丢掉已完成题；
2. 某个 worker 超时或返回 `evaluation_error` 时，只回收该 worker，不影响其他题；
3. 同一个 evaluation ID 不允许被两个运行器同时写入；
4. PostgreSQL、Milvus 和 Redis 的写入范围仍由新 corpus run ID 和新数据库/集合/命名空间限制；
5. 报告同时记录 worker 数、请求失败、重试题数、RAG/生成/判卷/端到端 P50/P95，避免把并发带来的吞吐变化误认为算法收益。

### 13.5 阶段 D：再决定是否扩大范围

如果 30 道题显示明确的材料层改善，而且没有明显的回答退化或成本失控，才由用户决定是否：

1. 扩大到冻结的 147 道 analysis 非通过题；
2. 加一个少量 baseline pass 回归检查，确认新分块没有把原本易答题大面积破坏；
3. 在方案冻结后运行 validation 200 题，报告聚合泛化结果。

即使 30 道局部题提升明显，也不能直接宣布“500 题通过率提升了多少”。它只说明这个分块假设在已定位的结构缺口题上值得继续验证。

---

## 14. 审计产物、复查位置和不可丢失的信息

### 14.1 当前已存在的产物

| 位置 | 内容 |
| --- | --- |
| `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations/baseline-rag-010/results.jsonl` | 500 题完整机器记录，受控保存 validation 详情。 |
| `.../baseline-rag-010/summary.json` | 500 题正式汇总指标。 |
| `.../baseline-rag-010/analysis-failure-classification.json` | 300 道 analysis 自动失败初筛。 |
| `.../baseline-rag-010/manual-review.jsonl` / `manual-review.md` | 基线人工复核队列和记录。 |
| `.../baseline-rag-010/evidence-candidate-audit-target-manifest.json` | 97 道证据链审计清单。 |
| `.../t9-rewrite-fusion-targeted-003/` | 改写候选融合 15 题试跑产物。 |
| `.../t10-adjacent-l3-expansion-targeted-001/` | 11 道直接相邻缺口定向实验产物。 |
| `.../t10-adjacent-l3-expansion-analysis-147-002/results.jsonl` | 147 道 analysis 非通过题的候选链路与逐题结果。 |
| `.../t10-adjacent-l3-expansion-analysis-147-002/adjacent-l3-impact-summary.json` | 147 题自动对比汇总。 |
| `.../t10-adjacent-l3-expansion-analysis-147-002/manual-review-conclusions.jsonl` / `.md` | 5 道可归因收益、不能归因结果与系统异常的人工结论。 |

其中 `...` 均指：

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/evaluations
```

### 14.2 以后每轮实验的最低记录要求

每轮只改变一个变量，并至少保存：

```text
新的 evaluation-config.json
新的 evaluation_id
changed_variable
语料 manifest 与哈希
冻结题目清单与哈希
逐题 results.jsonl
候选 trace
summary.json 和 report.md
case-review.md
manual-review.jsonl 和 manual-review.md
异常/超时重试记录
```

不能只保存“通过率提高了多少”。一旦没有原始候选、最终 top-8、块血缘和人工复核，就无法判断一次变化是分块收益、Rerank 波动、判卷波动还是服务异常。

---

## 15. 不能得出的结论

为了防止后续汇报夸大，以下说法都不成立：

- “70% 证据全覆盖，所以检索没问题。”文件命中不等于关键句进入最终上下文。
- “52.4% 回答通过，所以 47.6% 全是分块问题。”其中包含生成、Rerank、范围控制、判卷歧义和系统异常。
- “相邻扩展后 41 / 147 变通过，所以它修复了 27.89% 的错题。”人工能确认的直接相邻因果收益只有 5 / 147。
- “11 道定向题有效，所以全库相邻扩展都值得长期打开。”11 道题就是按直接相邻缺口挑出来的，代表性很窄。
- “T9 没效果，就说明改写没有价值。”它只证明本项目当时的候选融合变量没有呈现稳定收益；它没有评价所有可能的查询改写策略。
- “使用 LangChain 就已经有 Markdown 结构分块。”项目目前只使用 RecursiveCharacterTextSplitter，没有标题树解析和标题路径 metadata。
- “SemanticChunker 或 Adaptive Chunking 的论文效果好，所以本项目一定会提升。”外部数据、模型、文档类型和指标不同，必须在本项目冻结数据上再验证。
- “analysis 提升就是 validation 泛化。”analysis 已参与找问题，只能用来解释和筛选方案。

---

## 16. 术语解释

| 术语 | 大白话解释 |
| --- | --- |
| RAG | 先去知识库找资料，再让模型依据资料回答。 |
| 语料库 / corpus | 系统允许查询的所有文档。 |
| 标准证据 | 出题时指定的正确来源文件。 |
| 干扰文档 | 看起来相关，但不是这道题正确答案来源的文件。 |
| 分块 / chunking | 把长文拆成可检索的小段。 |
| L1 / L2 / L3 | 三层大小不同的块。L3 最小，主要用来检索；L1/L2 是它的父级大上下文。 |
| 递归切分 | 先按段落、再按句子、再按更小分隔符寻找切点的长度控制方法。 |
| Markdown 标题切分 | 先识别 `#`、`##` 等章节结构，再在章节内部继续切。 |
| 标题路径 | 一个块在文档中的位置，例如“运行手册 > 故障转移 > 回滚保护”。 |
| Embedding | 把文本变成数字向量，便于按语义相近程度搜索。 |
| Dense 检索 | 按语义相似度找材料。 |
| BM25 | 按关键词出现和稀有程度找材料。 |
| Hybrid | Dense 和 BM25 一起用。 |
| 召回 | 在资料库的早期搜索阶段，把可能需要的材料找回来。 |
| 候选 | 搜索出来、还没决定是否最终使用的材料。 |
| Rerank | 对候选再排序，把更可能直接回答问题的材料推前。 |
| Auto-merging | 发现多个小块来自同一父块时，尝试换成更完整的父级上下文。 |
| top-8 | 最终只把排序靠前的 8 段材料交给回答模型。 |
| 证据覆盖 | 标准证据文件被找中的比例。 |
| 全覆盖 | 一道题要求的标准文件全部都被命中。 |
| `pass` / `fail` / `review` | 自动判卷的通过、失败、待人工确认。 |
| `system_error` | API、超时、生成、判卷等服务异常，不代表答案质量。 |
| P50 | 中位数，一半题更快，一半题更慢。 |
| `evaluation_id` | 每次实验独立编号，避免覆盖旧结果。 |
| `changed_variable` | 该实验唯一允许变化的东西，例如 `adjacent_l3_expansion`。 |
| 单变量实验 | 每轮只改一个因素，才能知道结果变化来自哪里。 |
| analysis | 可查看详情、用于找根因的 300 道诊断题。 |
| validation | 内容保持隐藏、用于最后检查泛化的 200 道题。 |

---

## 17. 当前决策与待确认事项

> 历史说明：本节记录实施前的决策状态。结构化分块已经按第 19 节方案完成真实定向实验，最新结果和复核结论以第 20 节为准。

已经有证据支持的判断：

1. 当前 Recursive 分块在部分企业 Markdown 中把关键事实分散到了不同 L3；
2. 相邻 L3 扩展能修复“关键事实就在前后块”的少量题，但不是通用解；
3. 改写候选融合没有显示稳定、可归因的证据收益；
4. 分块/结构组织是下一轮最值得优先验证的变量，但仍需要小范围实测证明；
5. 高成本的 SemanticChunker 和 Adaptive Chunking 不适合作为第一步。

在本节形成时尚未授权、当时不能直接开始的动作：

1. 修改 `backend/indexing/document_loader.py` 实现 `markdown_header_recursive_v1`；
2. 创建新的 Representative 评测集合和重新入库；
3. 运行 30 道分块缺口题的真实 RAG 对照；
4. 扩大至 147 道 analysis 或运行 validation；
5. 改动模型、Embedding、Rerank、Prompt、top-k 或默认业务集合。

本节形成时，下一步应由用户确认的唯一优化变量是：**是否按第 11 节实施 Markdown 标题 + 结构保护 + 章节内递归分块，并先做第 13 节的离线 30 题分块审计。** 该离线审计和后续真实对照已经完成；不要把本节的旧待确认状态当成当前状态。

---

## 18. 2026-08-18 实施与离线审计记录

> 历史说明：本节先记录离线阶段和实施前门禁；真实 P6 结果已在后续完成，汇总见第 20 节。

本节覆盖第 17 节中“尚未实施”的旧计划状态。已实施的唯一 RAG 输入变量为
`document_chunking_strategy=markdown_header_recursive_v1`；默认业务分块仍是
`recursive_l1_l2_l3`，没有被替换。

### 18.1 已实现的边界

1. `DocumentLoader` 新增显式的 `markdown_header_recursive_v1` 路径：先识别 `#` 到 `####` 标题，再在标题内处理段落、连续列表、连续表格和围栏代码；长普通段落才继续按原有 `2400/1600/800` 和 `400/200/100` 规则递归切分。
2. 新块保存标题路径、标题层级、原文起止位置、结构类型、同路径前后块、策略名和配置哈希。默认路径不生成这些字段，原有业务行为保持不变。
3. 新评测父块使用独立 `EvaluationParentChunkStore`。它要求显式 `EVALUATION_DATABASE_URL`，发现数据库名与业务 `DATABASE_URL` 相同、URL 缺失或不可连接时会在创建 Milvus 集合和写块之前停止；Redis 键使用 `rag_eval_chunking:<corpus_run_id>:parent_chunk:<chunk_id>`。
4. 新脚本只会在一个**已经由运维创建好的**评测数据库中初始化 `evaluation_parent_chunks` 表；它不会执行 `CREATE DATABASE`，不会调用业务 `init_db()`，也不会写入 `langchain_app`。
5. 结构 metadata 已进入新 Milvus 写入数据、检索返回和候选审计快照。它只在新策略显式启用时出现，不改变旧集合中的旧块。

### 18.2 冻结的离线 30 题审计

产物位于：

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-sf-001/
  evaluations/structured-chunking-offline-audit-002/
```

其中 `chunking-target-manifest.json` 冻结了 30 道 `analysis` 题。它们全部来自人工确认的 `same_source_far_leaf_gap`：标准文件已进入原始候选，但完整关键材料位于同一文件中较远的 L3。清单校验了 case split、原始审计、基线结果和每份 Markdown 的 SHA-256；拒绝 validation、重复 case ID 和输入哈希漂移。最新可用于运行器门禁的清单为 `structured-chunking-offline-audit-002`；早期 `001` 产物保留作历史记录，不作为后续真实评测输入。

离线对照只读取本地 Markdown，输出 `chunk-audit.jsonl`、`chunk-audit.md` 和 `chunk-audit-summary.json`。没有调用模型、Embedding、Milvus、PostgreSQL、Redis、Rerank、生成或判卷。

| 离线观察 | 数值 | 能说明什么 | 不能说明什么 |
| --- | ---: | --- | --- |
| 冻结 analysis 题 | 30 | 范围固定且可复核 | 不代表 300、500 或 validation。 |
| 旧 L3 总数 | 500 | 旧分块的对照数量 | 不是检索命中数。 |
| 新 L3 总数 | 462 | 新策略改变了这批文档的边界和合并方式 | 块变少不等于检索或回答变好。 |
| 关键旧 L3 记录 | 61 | 30 题中需要核查的远距离事实块数 | 不是 61 道题。 |
| 新旧边界完全相同 | 17 / 61 | 部分关键材料的边界没有变化 | 不表示该题无收益。 |
| 边界发生变化 | 44 / 61 | 新策略确实改变了多数关键材料周围的块边界 | 不表示关键块会被召回或进入 top-8。 |
| 关键材料处于二级及以上标题 | 3 / 30 | 只有少数目标文档能直接从内部标题层级获益 | 不能把“标题分块”当作 30 题的通用强信号。 |
| 旧关键块可定位到新块 | 30 / 30 | 原文映射完整，可继续做逐题人工复核 | 只说明同一原文可追踪，不是效果指标。 |

一个典型正向结构信号是 `qst_0023`：旧 L3#21 把评审名单的一部分和前一节内容放在一起；新分块将完整名单放入 `Review, launch gating, and update cadence` 标题下的 L3#22。相反，许多 Slack、邮件或事件流文档只有一个 `#` 标题，例如 `qst_0092` 的 `eng-platform`；新块仍有标题前缀和结构类型，但没有额外小标题可用。因此当前最诚实的结论是：**新实现可审计且确实改变了 44 个关键材料块的边界，但离线审计尚未证明召回、最终上下文或回答质量提升。**

### 18.3 当前门禁

在本节形成时，本轮没有创建 `enterprise_rag_evaluation`、没有初始化评测表、没有重建 7,222 篇语料、没有创建新 Milvus collection、没有操作 `tutorial_verify_embeddings`、没有运行真实 30 题 RAG，也没有触及 validation。后续 P6 已完成，不能再把这段当作当前状态。

当时的下一步是先人工查看 `chunk-audit.md` 中 30 条并排记录，再决定是否执行 P6。P6 后续已经按该门禁完成；每题仍保持检索 -> Auto-merging -> Rerank -> top-8 -> 生成 -> 判卷的顺序。

## 19. 最新讨论：先定向替换错题答案文档，再验证新分块

> 历史说明：本节先记录定向实验方案和执行边界。方案已经执行，最终结果、严格候选统计和逐题例外见第 20 节。

本节记录 2026-08-18 对实施范围的讨论。开头的范围说明属于当时的待确认状态；后文的 P6 执行记录已经补充了真实运行事实。

### 19.1 147 道题、30 道题和 43 篇文档分别是什么

之前所说的“一百多道错题”是 `baseline-rag-010` 的 300 道 `analysis` 非通过题池：

```text
123 fail + 24 review = 147 道非通过题
```

其中 `review` 只是自动判卷无法可靠判断，不等于已经确认是系统错误。147 道题包含检索没找到、证据不完整、回答生成失败、答案有歧义和系统异常等多种原因，不能全部归因于分块。

当前结构化分块离线审计只选择了 30 道高置信度分块候选，筛选条件是：

```text
标准答案文件已经进入原始候选
  + 关键事实在同一文件的更远 L3 块
  + 怀疑旧分块把一个完整语义单元拆散
```

这 30 道题中有 28 道来自上述 147 道非通过题；另外 `qst_0115` 和 `qst_0265` 虽然基线回答为 `pass`，但证据完整性有疑点，因此被保留作结构审计样本。

这 30 道题的主要答案来源是 30 篇文档，但多文档题还需要补充来源。按
`structured-chunking-offline-audit-002/chunking-target-manifest.json` 中的
`baseline_expected_evidence_filenames` 去重后，共有 43 篇答案相关文档。因此：

| 数字 | 准确含义 |
| ---: | --- |
| 147 | analysis 中所有 `fail` 和 `review` 组成的非通过题池，不是 147 道已确认的分块问题。 |
| 30 | 当前离线结构化分块审计和第一轮真实验证的高置信度分块题。 |
| 43 | 这 30 道题所需的唯一答案相关文档数。 |
| 269 | 如果把全部 147 道题的标准答案文件全部去重，约涉及 269 篇文档；其中很多题并非分块问题。 |

因此，不能因为有 147 道非通过题，就直接把 269 篇文档全部改成新分块。那会把检索问题、回答问题和系统问题混在同一个实验里。

### 19.2 推荐的第一轮：30 道题、43 篇答案文档的定向实验

推荐先做一个成本更低、问题更明确的实验：只替换这 30 道题涉及的 43 篇答案文档，验证新分块能否把已经定位的结构性证据缺口补回来。

测试语料仍保留完整的 7,222 篇文档：

| 文档范围 | 第一轮处理方式 |
| --- | --- |
| 43 篇答案相关文档 | 删除测试副本中的旧父块、旧 L3 向量和缓存，使用 `markdown_header_recursive_v1` 重新分块。 |
| 其余文档 | 保持 `recursive_l1_l2_l3`，作为不变对照。 |
| 6,500 篇普通干扰文档 | 继续保留，不能因为它们不是答案就删掉；否则检索难度会被人为降低。 |

这不是宣称“全库已经采用新策略”，而是一个定向问题：

> 只修复错题答案文档的分块，其他文档和检索环境保持不变，能不能把这些分块类错题救回来？

### 19.3 存储隔离和删除范围

不能只复制 PostgreSQL 就开始测试。RAG 的旧数据同时存在三个位置，三处必须一起隔离：

```text
旧环境（只读保留）
  PostgreSQL + Milvus collection + Redis 旧缓存
        |
        | 复制/迁移到新评测环境
        v
新环境
  独立 PostgreSQL + 新 Milvus collection + 新 Redis namespace
```

在新环境中处理 43 篇文档时，删除范围必须按稳定的 `source_ref` / 文档 ID 确定，不能靠模糊文件名：

1. 删除新 PostgreSQL 中对应的旧父块记录；
2. 删除新 Milvus 中对应文档的全部旧 L3 向量；
3. 删除对应的旧 Redis 父块缓存；
4. 写入新分块、父块、向量和结构 metadata；
5. 用 manifest 记录实际删除和写入的文档哈希，供复核。

原始 `baseline-rag-010` 环境不做任何删除。`tutorial_verify_embeddings`、线上业务 PostgreSQL 和业务 Redis 前缀也不在操作范围内。

Milvus 不应被假定为“会随着 PostgreSQL 自动复制”。如果无法可靠迁移其余 7,179 篇文档的向量、metadata 和索引状态，就不能只复制 SQL 后声称环境完整；此时应按现有 P6 方案重新构建完整 7,222 篇评测集合。

### 19.4 这轮真实测试固定不变的内容

除目标文档的 `document_chunking_strategy` 外，以下内容全部保持与 `baseline-rag-010` 一致：

- 模型和 Embedding provider；
- Prompt；
- Dense + BM25 检索；
- 候选数量、Auto-merging、Rerank 和最终 top-8；
- 语料文件、题目、参考答案和判卷规则；
- 单题超时和单次模型请求时限；
- 10 个 worker 的并发方式。

配置必须保存：

```text
changed_variable=document_chunking_strategy
target_case_set=analysis
target_case_count=30
target_document_count=43
evaluation_worker_count=10
```

同时要明确记录：43 篇目标文档使用新策略，其余文档仍是旧策略。这是“定向替换实验”，不能把它写成全库统一分块实验。

### 19.5 每道题需要留下的审计链

每道题都要保存新旧对照，而不是只保存最终答案：

```text
旧 baseline 结果
  -> 新原始候选
  -> 新合并候选
  -> 新 Rerank 输入/输出
  -> 新 Auto-merging 结果
  -> 实际送给模型的 top-8
  -> 新答案和独立判卷
  -> 人工逐题结论
```

至少记录：

- 新旧 chunk ID、标题路径、结构类型和原文范围；
- 43 篇目标文档是否被检索到；
- 关键事实是否在原始候选、Rerank 后候选和最终 top-8 中；
- Auto-merging 是否补回了完整上下文；
- 新旧证据全覆盖率和平均覆盖率；
- 新旧答案是否通过；
- 端到端、RAG、生成和判卷耗时；
- API、生成、判卷或超时异常；
- 人工归因：分块修复、检索仍失败、回答失败、人工无法判断或发生退化。

### 19.6 结果怎样才算有意义

第一轮只回答这 30 道题的定向问题，结果按以下方式解释：

| 结果 | 可以说什么 | 不能说什么 |
| --- | --- | --- |
| 关键事实因新分块进入最终 top-8，答案由错变对 | 新分块对该题产生了直接收益 | 不能外推到 500 道题或 validation。 |
| 证据变完整，但答案仍错 | 分块问题可能修复，剩下是回答/整合问题 | 不能说新分块无效。 |
| 候选和最终上下文都没有变化 | 该题没有体现结构化分块收益 | 不能说所有分块都无效。 |
| 原来通过、现在失败 | 发生了退化，必须人工复核并停止扩大范围 | 不能用总体平均数掩盖退化题。 |
| 出现 API/超时/判卷错误 | 属于 `system_error` | 不能归为分块失败。 |

只有当 30 道题中出现可逐题解释的“证据补回并带来答案改善”，且没有明显退化和成本失控，才考虑从剩余 117 道题中再筛选同类分块问题。即使扩大到更多题，也不能把 analysis 的定向结果冒充 validation 泛化结果。

### 19.7 当前执行状态与 30 题真实结果

截至本文更新时间，已完成 147 道非通过题池的定义、30 道高置信度分块题的离线审计、43 篇目标文档范围确定、新隔离环境语料准备和 30 题真实 RAG 评测。评测只使用冻结的 `analysis` 题，不含 validation。

- 当前运行：`enterpriserag-en-representative-structured-targeted-004`；
- 当前阶段：旧 Representative collection 的非目标文档 L3 向量复制已完成；checkpoint 最终为 `source_page_offset=81402`、`source_page_count=82`、`vector_copy_count=80621`。目标 43 篇文档未复制旧向量，已使用新分块生成 734 个目标 L3；
- 已完成：独立 PostgreSQL 初始化、目标 collection 创建/校验、并发准备代码和运行 manifest；
- 已完成：向量复制、目标文档新分块/Embedding、父块和 L3 写入，manifest 已为 `prepare_completed=true`；
- 已完成：真实评测主运行 `structured-chunking-analysis-30-001` 及 `retry-002`、`retry-003`、`retry-004`、`retry-005` 链式补跑，冻结 30 道 `analysis` 题，10 worker；最终 24 道可判题，6 道系统异常，整体 `evaluation_status=interrupted`。此前误启动的 `structured-chunking-targeted-004` 只留下 0 / 30 的配置与进度文件，已终止，不作为结果。
- 不会做：删除旧 Representative 集合、重跑 500 题、运行 validation 200 题、修改 RAG 参数。

整体 30 题结果：结构化分块证据全覆盖率 `50.00%`（15 / 30），平均证据覆盖率 `53.98%`，回答通过率 `26.67%`（8 / 30），系统异常 6 题。只保留 baseline 和结构化结果都没有系统错误的 24 道同题对照，证据全覆盖率为 `8.33% -> 54.17%`（`+45.83` 个百分点），平均证据覆盖率为 `14.70% -> 59.14%`（`+44.44` 个百分点），回答通过率为 `4.17% -> 33.33%`（`+29.17` 个百分点）。这些数值只能说明该 targeted analysis 子集上的变化，不能外推到 500 题总体或 validation。

自动逐题影响分类为：7 道“证据补回且回答通过候选”、5 道“证据覆盖提升但回答仍失败”、1 道“证据覆盖退化”、11 道“没有可测证据收益”、6 道系统异常。7 道仍需人工确认关键事实是否确实因新分块进入最终上下文；5 道说明材料可能补回并不等于回答整合正确；1 道出现退化；6 道系统错误不计入优化收益。

候选链路中目标答案文档出现数为：初始候选 `14 / 30`、原始候选 `21 / 30`、合并后候选 `21 / 30`、Rerank 输入 `21 / 30`、最终上下文 `20 / 30`。这些是文件级命中，不代表关键事实完整进入了最终上下文。

推荐决策是先执行“30 道 analysis + 43 篇答案文档”的定向实验。若需要回答“新分块对全部 147 道非通过题的总体影响”，则必须另立实验范围，先完成 147 道题的根因筛选，不能直接把所有 269 篇答案文档一起替换。

### 19.8 补充的并发方案：分块不再逐文档串行执行

之前的“按文档处理”只是说明数据边界，不表示必须一个文档处理完后才开始下一个文档。本次实施应使用有界并发流水线；并发只提高准备速度，不改变题目内部的 RAG 顺序，也不改变 `changed_variable`。

#### 文档准备流水线

```text
43 篇目标文档 manifest
        |
        v
本地 Markdown 解析池（8 个 worker）
        |
        v
结构化分块校验队列
        |
        +--> 父块批量写入队列 --> PostgreSQL 单写入者
        |
        +--> L3 批量 Embedding 队列 --> 受限 Embedding worker（最多 10）
                                      |
                                      v
                              Milvus 批量写入者
        |
        v
逐文档 manifest 校验和完成 checkpoint
```

具体规则：

1. **本地解析并发**：8 个 worker 同时读取不同 Markdown，完成标题解析、列表/表格/代码识别、递归分块和 metadata 生成。解析阶段不访问外部模型服务，可以根据 CPU 调整到 8--16，但同一 `source_ref` 只能由一个 worker 处理。
2. **Embedding 并发**：分块结果进入有界队列，最多 10 个 Embedding 请求同时运行；每个请求使用批量文本，不为每个 L3 单独发一次请求。RPM/TPM 限流器必须优先于 worker 数，遵守 BGE-M3 的 2,000 RPM 和 500,000 TPM 限制。
3. **PostgreSQL 写入**：解析 worker 不直接并发写父块表；由单写入者按批次提交，避免同一 `chunk_id` 重复写入和事务互相等待。批次提交成功后才写 Redis 父块缓存。
4. **Milvus 写入**：Embedding 完成后由受控批量写入者写入新 collection，并在写入成功后记录批次 checkpoint。不能把 PostgreSQL 已成功、Milvus 未成功的批次标记为完成。
5. **旧数据替换顺序**：新环境中先按 `source_ref` 清理 43 篇目标文档的旧父块、旧向量和旧缓存，再允许对应的新批次写入；原始 baseline 环境完全不参与删除。
6. **其余 7,179 篇文档**：如果采用迁移旧向量的定向方案，只做完整性校验，不重新分块；如果迁移工具不能保证向量和 metadata 一致，则切换到全量 7,222 篇重建流水线，不能半复制、半重建后直接评测。

#### 评测题目并发

语料准备完成后，30 道题使用 10 个 worker 并发执行。单题内部仍严格保持：

```text
检索 -> 候选合并 -> Auto-merging -> Rerank -> top-8 -> 生成 -> 判卷
```

题目并发与文档并发分开限流：Embedding 使用 Embedding 限流器，Rerank 使用 Rerank 限流器，DeepSeek 生成和判卷使用模型限流器；不能因为文档阶段开了 10 个 worker，就让所有下游请求无限叠加。

#### 失败恢复和审计

每篇文档、每个 Embedding 批次和每道题都必须有独立 checkpoint：

- 成功文档不重复解析、不重复写入；
- 某个文档或批次失败时，只重试缺失/失败项；
- 重试使用新的 attempt 记录，保留原错误和响应时间；
- 失败 worker 被回收，不影响已经完成的其他文档或题目；
- 最终 manifest 记录文档完成数、分块数、Embedding 批次数、失败/重试数和总耗时。

这样可以把“一个文档一个文档串行分块”的等待，改成“多个文档同时解析、Embedding 受限并发、数据库和向量库批量落盘”，同时保留可恢复、可审计和不重复入库的条件。

截至 2026-08-18，代码层已落实以下准备能力：`prepare --rechunk-scope targeted --target-manifest ...` 会校验 analysis-only 的 30 题冻结清单；优先读取 manifest 的 `target_document_ids`，对旧版清单则从 30 题的 `expected_doc_ids` 推导完整答案文档集合（当前为 43 篇）。解析默认最多 8 个 worker，每篇文档有独立 checkpoint；Embedding 最多 10 个批次并行且受 RPM/TPM 限制，Milvus 由单写者按批次顺序提交，评测父块使用独立 PostgreSQL 的单写者分批提交，Embedding/Milvus 失败只重试对应批次。targeted 模式还会只读分页复制 baseline collection 中非目标文档的旧 L3 向量，按 source filename 排除 43 篇目标文档；复制 checkpoint 保存页偏移和 chunk ID，恢复时去重，缺旧集合或向量字段直接失败，不会退回全量重嵌入。未完成的同一 targeted run 可在 manifest、target hash 和 collection 不变时从 checkpoint 恢复；已完成 run 或配置漂移会拒绝覆盖。默认线上分块路径、旧集合和旧数据库没有被改写。

代码已通过全量单测、Python 编译、`git diff --check` 和 Trellis task validate。独立 PostgreSQL 已由显式运维步骤创建并初始化。2026-08-18 已完成新的 targeted 语料准备 run：

- `corpus_run_id=enterpriserag-en-representative-structured-targeted-004`；
- 新 collection 为 `rag_eval_enterpriserag_enterpriserag_en_representative_structured_targeted_004`；
- `evaluation_database_name=enterprise_rag_evaluation`，不使用业务库 `langchain_app`；
- `rechunk_scope=targeted`，43 篇目标答案文档使用 `markdown_header_recursive_v1`；
- 文档解析使用最多 8 个 worker，Embedding 使用最多 10 个受 RPM/TPM 限制的批次并发，PostgreSQL 和 Milvus 仍由单写入者按批次提交；
- 旧 baseline collection 只读，分页复制 checkpoint 位于 `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/preparation-checkpoints/vector-copy.json`；最终状态为 `completed`，目标 43 篇文档已按 source filename 排除。

语料准备已完成，`manifest.json` 为 `prepare_completed=true`，并通过新 collection、独立父块库和 Redis namespace 校验；30 道 `analysis` 题真实评测及链式重试也已完成逐题记录。不会重跑 500 题、运行 validation、删除旧 Representative 集合或操作 `tutorial_verify_embeddings`。最后 3 道整题超时和 3 道回答请求超时均保留为系统异常，不再把它们算作分块失败。

---

## 20. 新分块真实成果与逐题问题复核

更新时间：2026-08-18。

本节是本轮结构化分块实验的最终整理。范围固定为 30 道 `analysis` 题和 43 篇目标答案文档。新策略只替换这 43 篇文档，其他 7,179 篇文档仍使用旧策略；本轮唯一 RAG 变量是：

```text
changed_variable=document_chunking_strategy
document_chunking_strategy=markdown_header_recursive_v1
```

模型、Embedding、Prompt、候选数量、top-k、Rerank、Auto-merging、语料范围和旧 Representative 集合均未改变。6 道系统异常不计入分块失败或回答失败，validation 200 题没有运行，以下结果不能外推到 500 题总体。

### 20.1 结果先看

30 道题的整体记录如下。由于有 6 道系统异常，整体数字只用于说明运行完整性，不能直接作为优化收益。

| 指标 | 结构化分块结果 | 说明 |
| --- | ---: | --- |
| 证据全覆盖率 | 15 / 30 = 50.00% | 含系统异常记录，不作为最终对照收益 |
| 平均证据覆盖率 | 53.98% | 含系统异常记录 |
| 回答通过率 | 8 / 30 = 26.67% | 含系统异常记录 |
| 系统异常 | 6 道 | 3 道整题超过 600 秒，3 道单次回答或判卷请求异常 |

只保留 baseline 和新分块都没有系统异常的 24 道同题，才是公平对照：

| 指标 | 旧分块 | 新分块 | 变化 |
| --- | ---: | ---: | ---: |
| 证据全覆盖率 | 2 / 24 = 8.33% | 13 / 24 = 54.17% | +45.83 个百分点 |
| 平均证据覆盖率 | 14.70% | 59.14% | +44.44 个百分点 |
| 回答通过率 | 1 / 24 = 4.17% | 8 / 24 = 33.33% | +29.17 个百分点 |

因此，在这 24 道可比较题中，**答对题目增加了 7 道，旧分块答对 1 道，新分块答对 8 道**。这 7 道首先是自动归因候选，只有逐题确认“关键事实确实因新分块进入最终上下文”，才能把它们写成最终的分块直接收益。

### 20.2 逐题影响分类

自动对照结果分为以下几类：

| 类别 | 数量 | Case ID | 解释 |
| --- | ---: | --- | --- |
| 证据补回且回答通过候选 | 7 | `qst_0023`、`qst_0142`、`qst_0189`、`qst_0312`、`qst_0331`、`qst_0335`、`qst_0423` | 材料和答案同时改善，待人工确认因果 |
| 证据覆盖提升但回答仍失败 | 5 | `qst_0037`、`qst_0136`、`qst_0227`、`qst_0308`、`qst_0356` | 分块可能修复了材料问题，回答组织仍失败 |
| 证据覆盖退化 | 1 | `qst_0366` | 必须单独复核，不能被平均数掩盖 |
| 没有可测分块收益 | 11 | `qst_0092`、`qst_0193`、`qst_0231`、`qst_0233`、`qst_0241`、`qst_0247`、`qst_0265`、`qst_0275`、`qst_0295`、`qst_0300`、`qst_0320` | 候选没有变化、事实仍缺或属于其他问题 |
| 系统异常 | 6 | `qst_0100`、`qst_0115`、`qst_0120`、`qst_0133`、`qst_0221`、`qst_0345` | 超时、生成或判卷异常，不算分块失败 |

### 20.3 原始候选和最终 8 段的严格文件审计

之前汇总中的 `raw_candidates_contains_expected_document=21/30` 只表示“至少有一份标准证据文件出现”，不能解释成“这道题所需的所有证据文件都出现”。本节用逐题 JSONL 重新按以下严格规则核对：

```text
expected_evidence_filenames ⊆ 原始候选中的 filename
```

结果是：

| 口径 | 数量 |
| --- | ---: |
| 原始候选已经包含全部标准证据文件 | 20 / 30 = 66.67% |
| 排除 6 道系统异常后，原始候选包含全部标准证据文件 | 18 / 24 = 75.00% |
| 原始候选全齐且最终 8 段仍保留全部标准证据文件 | 19 / 20 = 95.00% |
| 原始候选全齐但最终 8 段丢失至少一份标准证据文件 | 1 / 20 = 5.00% |

唯一一题是 `qst_0295`。因此，正常可比较题里，原始候选全齐的 18 道中有 17 道把全部标准文件保留到最终上下文，只有 1 道在后续筛选阶段丢失。这里统计的是**文件级**命中，不能代替关键事实级审计；一个文件出现，不代表文件中包含答案的那一段也出现。

### 20.4 `qst_0295`：原始候选有正确文件，但 Rerank 选错片段

题目问：

```text
在 2026 年 3 月 payments rollout 的欧洲 canary 故障中，最后回退到了哪个版本？
```

正确文件进入原始候选的片段是 `...dsid_4b6a0f...::p0::l3::0`，内容主要是：

```text
payments-hotpatch canary in eu-west1 出现大量 pod restarts
日志显示 SIGTERM、preStop handler 和 shutdown hook 异常
Canary health degraded，正在讨论是否暂停 promotion
```

这段没有包含题目真正需要的版本号。标准来源后面的其他内容才写着：

```text
helm rollback payments-service 3 --namespace prod
rollback completed — helm rolled back to v2.4.1 (revision 3)
```

但这段后半内容没有进入原始候选。随后 Rerank 从 30 个输入片段中返回 8 个，排在前三的相似干扰片段分别写着：

```text
rollback succeeded; canary restored to v2.2.7
safe move is rollback to v2.2.7
rollout rolled back to v2.2.7 ... revision=3
```

这 3 段来自另一篇相似的 payments/eu canary 文档，不是本题标准来源。它们的 Rerank 分数分别为 `0.999578`、`0.989452`、`0.986751`；正确文件的片段在 `rerank_not_returned_candidates` 中，没有进入最终 8 段。Rerank 本身没有报错，阈值也没有额外拒绝，Auto-merging 也没有执行。

这道题的准确归因是：

```text
直接问题：Rerank 没有选出正确的候选片段
前置原因：正确文件中真正包含答案的后半分块没有进入原始候选
干扰因素：另一篇文档明确写了 rollback、revision 和版本号，表面相关性更高
```

因此 `qst_0295` 是一个“语义区分和 Rerank 选择”例外题，不能拿它证明新分块整体无效。标准来源本身明确写的是 `v2.4.1 (revision 3)`，分歧只发生在候选池同时出现了另一场相似事故时。

### 20.5 本轮成果能说明什么

本轮已经证明，在这批定向 analysis 题中，结构化 Markdown 分块能够改善一部分“同一文档的关键内容被切散”的问题，证据覆盖和回答通过都出现了明显提升。最直接的可观察结果是：24 道无系统异常同题中，回答通过从 1 道增加到 8 道，增加 7 道。

同时，本轮也明确暴露出三类不能靠分块单独解决的问题：

1. 正确文件根本没有进入原始候选，这是检索或查询表达问题；
2. 文件进入了，但真正的事实片段没有进入候选或被 Rerank/top-8 丢掉，这是候选组织和排序问题；
3. 最终证据已经完整，模型仍然没有正确组织答案，这是回答阶段问题。

后续汇报必须使用“30 道 targeted analysis 结果”这个完整限定，不能写成“全库通过率提升”或“validation 已验证”。在用户确认下一唯一变量前，不开始新的 RAG 优化，不运行 validation，不删除旧集合，也不操作 `tutorial_verify_embeddings`。

### 20.6 七道收益候选人工最终确认（已完成）

2026-08-19 对 `structured-chunking-analysis-30-retry-005` 中自动标记为
`evidence_recovered_answer_pass_candidate` 的 7 道题进行了人工确认。确认只读取已有逐题 JSONL、候选 trace、最终 8 段和原始英文 Markdown，没有重跑模型、重新入库或查看 validation。详细记录见：

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/
  evaluations/structured-chunking-analysis-30-retry-005/structured-chunking-human-confirmation.md
```

确认标准是：基线没有完整标准证据；新分块运行没有系统异常；关键事实进入原始候选和最终 8 段；回答覆盖问题要求的核心事实且没有与原文冲突。

| 题目 | 文件 / 事实 / 最终 8 段 | 回答与异常 | 人工结论 |
| --- | --- | --- | --- |
| `qst_0023` | Reviewers required 段完整列出五类审查角色及人员 | `fail -> pass`，无系统异常 | 确认直接收益 |
| `qst_0142` | 最终块同时给出 Hosted `99.9%`、P95 `80-180ms`、Dedicated `99.95%`、P99 `<250ms` | `fail -> pass`，无系统异常 | 确认直接收益；额外说明不单独计收益 |
| `qst_0189` | 最终块链路包含 `origins`、构建来源、origin-mapper、cosign 和 `--enforce-origin-verification` | `fail -> pass`，无 API、生成或判卷错误 | 确认直接收益；`retrieval_status=partial` 不等于系统异常 |
| `qst_0312` | 最终块包含 `2026-08-16` 和 `internal/runbooks/oncall` | `fail -> pass`，无系统异常 | 确认直接收益 |
| `qst_0331` | 最终块包含 `redwood-open-gpt-3.5-v1` 和 Meridian Analytics 的 Dedicated contract | `fail -> pass`，无系统异常 | 确认直接收益 |
| `qst_0335` | 最终块包含 Tanya Bennett、她的可用窗口和最终确认的 Tue 7/7 3:00-3:45pm PDT | `fail -> pass`，无系统异常 | 确认核心问题收益；CPO 职务不是题目必需字段 |
| `qst_0423` | 最终块同时包含旧的 18 个月建议和更新后的 12 个月初始核查要求 | `fail -> pass`，无系统异常 | 确认收益，但同时属于新旧事实冲突消解 |

因此，7/7 道候选均可保留为“关键事实进入最终 8 段且回答通过”的人工确认样本。它们仍然只是 24 道无系统异常同题对照中新增的 7 道答对，不能外推为 500 题总体提升，也不能替代 validation 泛化结果。

`qst_0295` 不在确认名单中。它的正确文件进入了原始候选，但包含版本号的后半片段没有进入候选，Rerank 选中了相似事故文档；因此仍保留为“候选材料不完整 + 相似干扰 + Rerank 误选”的例外。

### 20.7 排除 8 道正确题后的剩余题目：逐题错误原因

本节回答“剩余题目为什么没有达到效果”。范围仍然是 30 道 targeted `analysis` 题，依据是逐题 JSONL、candidate trace、`expected_fact_audit`、回答和判卷结果；没有重跑评测。

30 道题中有 8 道通过，因此剩余 22 道可分成：

| 结果 | 数量 | 含义 |
| --- | ---: | --- |
| 可分析的质量问题 | 16 | 有稳定的检索、证据或回答结果，可以定位失败层次 |
| 系统异常 | 6 | 超时或 API 请求失败，没有稳定的可判答案，不能算分块或回答质量失败 |

#### A. 证据补回了，但回答仍然错：5 道

这 5 道的共同点是：目标材料已经进入最终上下文，证据覆盖比 baseline 提高，但回答没有把材料准确、完整地组织出来。因此不能说“分块没有找到资料”，更准确的说法是“资料到了，回答阶段仍然失败”。

| 题目 | 证据覆盖 | 错误原因 | 链路判断 |
| --- | ---: | --- | --- |
| `qst_0037` | `0 -> 1` | 回答只说 API gateway backpressure 和线程耗尽，漏掉 `hedged retries`、`prefetch.window_size` 增大以及 `pthread/SIGABRT` 结果。 | 文件和关键主因已找到并进入最终 8 段；失败在回答不完整。 |
| `qst_0136` | `0 -> 1` | 正确措施是把 Slateware 流量固定到更稳定的 edge pool，回答却扩写成 routing adjustment、gateway rollback、client timeout/retry guidance 等没有被题目证据支持的细节。 | 证据已到；模型混入不受支持的信息，且没有准确复述核心措施。 |
| `qst_0227` | `0 -> 1` | 正确根因是 KV cache 内存压力导致背压、延迟 token 发射；回答错误归因于边缘代理发送 `RST/GOAWAY`，把现象当成根因。 | 证据已到；失败在因果关系选择错误。 |
| `qst_0308` | `0 -> 1` | 混合联网方案的方向基本对，但把 `scoped inference access` 说成 `short inference bursts`，漏掉 `low-latency`，把 redundancy/backfill 改成 backup/archive uploads，并漏掉年份 `2026`。 | 文件和大意已找到；失败在术语、限定条件和日期精度不足。 |
| `qst_0356` | `0.25 -> 0.75` | 回答答对 incident type 和 primary owner，但没有给出客户更新必须使用的 TTM `45 分钟`、TTF `3 个工作日`。 | 新分块只补回了部分证据；剩余缺口加上回答未补齐，不能算通过。 |

#### B. 证据覆盖发生退化：1 道

| 题目 | 证据覆盖 | 错误原因 | 链路判断 |
| --- | ---: | --- | --- |
| `qst_0366` | `0.7778 -> 0.4444` | 回答把应为 `SLO_GATED` 的拒绝原因代码写成 `BURST_NEIGHBOR_SLO_GUARDRAIL`，并且没有完整说明“先降 burst 压力、再进入 SLO gate、严重或持续时打开 circuit breaker”的触发顺序。 | 这是新分块后的回归候选：不能只看平均收益，需要继续检查候选合并、Rerank 和最终 top-8 哪一步丢了事实。现有记录能证明发生了退化，但不能仅凭这一题确定唯一责任组件。 |

#### C. 没有产生可测的分块收益：10 道

这里不包括 `qst_0265`，因为它 baseline 和新分块都已经答对；“没有新增收益”不等于“错误”。其余 10 道再按证据链路细分。

**1）正确材料没有进入原始候选：3 道**

| 题目 | 错误原因 | 链路判断 |
| --- | --- | --- |
| `qst_0092` | 目标 rollout 文档没有进入新运行的原始候选，系统因此拒答。 | 这是召回/查询表达问题；分块只有在文件先被找到后才有机会发挥作用。 |
| `qst_0231` | 目标文档没有进入原始候选，回答缺少审计 trace 的 12 个月保留和默认每月导出信息。 | 同样是召回缺口，不是最终回答模型单独造成的。 |
| `qst_0275` | 没有找到题目所指的那份性能历史系统文档，候选被大量相似材料占据，回答拼接了多个不相干来源。 | 这是主题识别/召回问题；“找到了很多文件”不代表找到了正确文件。 |

**2）材料曾进入早期候选，但在后续链路丢失：3 道**

| 题目 | 错误原因 | 链路判断 |
| --- | --- | --- |
| `qst_0233` | 目标材料只出现在 initial candidates，随后在 raw/final 阶段消失，回答因此拒答。 | 文件曾被召回，但候选合并、重排或 top-8 筛选没有保留它。 |
| `qst_0295` | 正确文件进入原始候选的片段只写了故障现象；真正写 `v2.4.1 (revision 3)` 的后半片段没有进入候选。Rerank 反而选中了另一篇相似事故中的 `v2.2.7`。 | 这是“关键片段未入候选 + 语义相似干扰 + Rerank 误选”的例外，不能用来否定结构化分块对整体的收益。 |
| `qst_0300` | 目标文档只出现在初始候选，后续没有保留包含中等长度请求最低成本及具体价格的片段。 | 表格/结构识别本身没有转化成最终 8 段中的答案事实，问题在后续候选组织。 |

**3）目标文件进了最终上下文，但关键事实仍然没有进来：3 道**

| 题目 | 错误原因 | 链路判断 |
| --- | --- | --- |
| `qst_0193` | 最终上下文出现相关 onboarding 文档，但没有出现 hands-on 期间负责 standby 的关键人员 Monica Patel，系统遂拒答。 | “文件出现”不等于“答案段落出现”；是事实级证据缺失。 |
| `qst_0247` | 相关 partner/ISV 文档进入最终上下文，但题目要求的双方具体截止日期没有被带入可用证据。 | 文件级命中，事实级不完整；分块没有解决远距离日期信息。 |
| `qst_0320` | 相关商业材料进入最终上下文，但缺少 24 个月预付费 `20%` 折扣和 Q1 `5%` onboarding credit。 | 文件已找到，关键商业条款仍未进入证据覆盖。 |

**4）证据完整，但回答模型仍然答错：1 道**

| 题目 | 证据覆盖 | 错误原因 | 链路判断 |
| --- | ---: | --- | --- |
| `qst_0241` | baseline 和新分块均为 `1.0` | 回答只泛泛描述导出、签名和时间戳能力，漏掉 controlled re-export、redaction rule lookup timeout、salted HMAC、SHA256 checksum manifest、platform signing key、RFC3161 timestamp 等明确步骤。 | 证据已经完整，失败主要在回答的步骤完整性和精确复述，不是分块问题。 |

#### D. 6 道系统异常：不应算作错误答案

| 题目 | 异常 | 为什么不能归为质量失败 |
| --- | --- | --- |
| `qst_0100` | 单题超过 600 秒总时限 | 没有形成稳定的回答/判卷结果，无法判断分块是否有效。 |
| `qst_0115` | 单题超过 600 秒总时限 | baseline 曾通过，但本次没有可比较的结构化结果，不能算回归或回答失败。 |
| `qst_0345` | 单题超过 600 秒总时限 | 评测链路未完成，不能对证据或答案下质量结论。 |
| `qst_0120` | 独立判卷请求超时 | 已生成回答，但没有稳定判卷结果；不能把 review 当成 fail。 |
| `qst_0133` | 独立判卷请求超时 | 同上，属于 judge/API 异常。 |
| `qst_0221` | 独立判卷请求超时 | 同上，不能用不完整的运行记录判断答案质量。 |

#### 这一组题说明了什么

剩余题目的主要问题不是一个“分块失败”可以概括的，而是分布在不同层：

1. **召回层**：`qst_0092`、`qst_0231`、`qst_0275` 中，正确文件根本没有进入原始候选；
2. **候选组织/排序层**：`qst_0233`、`qst_0295`、`qst_0300` 中，文件或片段在初始候选之后被丢弃，或被相似干扰片段挤掉；
3. **事实级证据层**：`qst_0193`、`qst_0247`、`qst_0320` 中，文件出现了，但题目真正需要的那一段事实没有进入最终 8 段；
4. **回答层**：`qst_0037`、`qst_0136`、`qst_0227`、`qst_0308`、`qst_0356`、`qst_0241` 中，证据已部分或全部到位，但模型漏答、混淆因果、添加无依据细节或缺少精确限定；
5. **运行层**：6 道 timeout/API 异常没有稳定答案，不能混入质量指标。

这里必须区分四件事：文件被找到、关键事实被找到、事实进入最终 8 段、模型答对。文件出现只能证明“找到了这份文档”，不能直接证明答案证据完整；证据覆盖上升也只能说明材料变好，不能保证回答一定正确。

#### 后续验证建议（本轮不执行）

- 对召回缺口题，单独审计原始候选和查询改写，验证正确文件是否能稳定进入候选；
- 对 `qst_0233`、`qst_0295`、`qst_0300`，固定同一候选池，逐层比较 initial、raw、Rerank 输入、最终 8 段，确认是合并、Rerank 还是 top-8 截断造成丢失；
- 对回答层题，固定已经确认的最终证据，单独重放生成/判卷，验证是否仍漏事实或混淆因果；
- 对 `qst_0366`，先做单题链路回放，确认覆盖退化发生在哪一层，再决定是否需要下一轮唯一变量实验；
- 对 6 道异常题，增加阶段耗时和超时原因记录后再重试，不能直接把异常记录并入质量失败。

本节仍然只说明 30 道 targeted `analysis` 题的原因分布，不能外推到 500 题，也不能替代 validation 200 题的盲态泛化验证。

### 20.8 更换 `Qwen/Qwen3.5-35B-A3B` 后的 30 题测试

2026-08-19 使用 `.env` 中的 `Qwen/Qwen3.5-35B-A3B` 重新测试同一批 30 道 targeted `analysis` 题，并发数为 10。唯一改变的是主回答模型、快速模型和独立判定模型；Embedding、Rerank、分块、候选数量、最终 8 段、Auto-merging、提示词、语料和独立 Milvus 集合都没有改变。

本轮没有运行 validation，也没有重跑 500 题。运行仍使用 `markdown_header_recursive_v1` 的结构化分块语料，未写入 `tutorial_verify_embeddings`。

#### 真实结果

| 项目 | 结果 |
| --- | ---: |
| 发起测试 | 30 题 |
| 拿到稳定回答和判定 | 27 题 |
| 判定服务超时 | 3 题：`qst_0100`、`qst_0247`、`qst_0366` |
| 27 道中答对 | 11 题 |
| 27 道中证据文件全部到位 | 17 题 |
| 27 道平均证据覆盖 | 65.74% |

3 道超时不是答错。它们没有稳定的独立判定结果，所以不能把它们计入“答案失败”，也不能用 27 道结果冒充完整的 30 道通过率。前几轮重试还出现过回答请求超时；最终一轮留下的是判定请求超时，说明主要问题在外部模型服务响应时间，不是本地检索库或分块程序报错。

#### 和原来的 DeepSeek 模型怎么比

原结构化分块测试使用的是 `deepseek-ai/DeepSeek-V4-Flash`。为了不把两边的超时混成答错，只比较两边都拿到稳定结果的 22 道同题：

| 指标 | DeepSeek | Qwen | Qwen 多答对/多覆盖 |
| --- | ---: | ---: | ---: |
| 答案通过 | 8 / 22（36.36%） | 9 / 22（40.91%） | 多 1 题 |
| 证据文件全部到位 | 13 / 22（59.09%） | 14 / 22（63.64%） | 多 1 题 |
| 平均证据覆盖 | 62.50% | 67.05% | 高 4.55 个百分点 |

这个对照说明：在这 22 道可公平比较的题上，Qwen 结果略好一些；但只多答对 1 道，样本很小，而且两边都有服务超时，不能据此断言 Qwen 在整个 500 题上一定更好。它也不能说明 Qwen 解决了所有资料查找问题，因为仍有题目是正确文件没找到、文件找到了但关键句没进入最终 8 段，或者材料齐全但回答仍然漏信息。

#### 产物位置

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/
  evaluations/model-qwen35-35b-a3b-analysis-30-retry-007/attempt-results.jsonl
  evaluations/model-qwen35-35b-a3b-analysis-30-retry-007/summary.json
  evaluations/model-qwen35-35b-a3b-analysis-30-retry-007/evaluation-config.json
  evaluations/model-qwen35-35b-a3b-analysis-30-retry-007/case-review.md
  model-comparison-30-qwen35-target.json
```

由于还有 3 道未拿到稳定判定，运行器按规则没有写出一个虚假的 30 道合并 `results.jsonl`；旧运行和每次补跑的 `attempt-results.jsonl` 均保留，可继续复查。下一步如果要形成严格的完整 30 题模型对照，需要在判定服务稳定后只重试这 3 道，或者明确接受它们作为系统异常；在用户确认前不运行 validation、不重跑 500 题、不改变其他 RAG 变量。

### 英文子问题保持英文：30 道 DeepSeek 对照（完成）

这轮只改了一个地方：英文题被拆成多个小问题后，小问题必须继续用英文找资料。模型、Embedding、Rerank、top-k、Auto-merging、结构化分块、语料、集合和 10 并发都没有改变；只读取已有的结构化英文 collection，没有重新入库，也没有触碰 validation 或 `tutorial_verify_embeddings`。

首次执行时，评测数据库密码与容器保存的旧密码不一致，30 个 worker 都在返回结果前退出。数据库恢复后发现恢复逻辑把这些异常记录误当成“已完成”；已修复为“成功题复用，接口、模型、生成或判卷异常题必须重跑”，并把旧异常单独留在 `results-retry-history.jsonl`。这次 30 / 30 真正完成，系统异常为 0；不是把旧异常当作新结果。

语言检查显示 30 道英文题中有 16 道实际走了复杂题拆分路径；这 16 道的所有小问题和实际检索文字均不含中文。因此可以确认“英文小问题检索”确实发生了，不能再把它当作只改了提示词而没有进入实际链路。

主比较只保留旧结构化运行和本轮都没有系统异常的 27 道同题：标准证据文件全覆盖 `55.56% -> 62.96%`，平均文件覆盖 `59.98% -> 68.21%`，但自动回答通过 `8 / 27 -> 7 / 27`。这里的“覆盖”只表示标准文件是否进入最终结果，不等于答案所需的每个关键事实都在最后给模型看的内容里，更不等于模型一定会正确使用。

更直接地看，旧链路确实使用中文小问题的 10 道题，本轮表现是文件全覆盖 `60.00% -> 50.00%`、平均覆盖 `71.94% -> 64.17%`、通过 `3 / 10 -> 1 / 10`。其中 `qst_0227` 本轮被判为简单题，不实际使用该变量；去掉它的 9 道复杂题对照同样没有改善。也就是说，当前结果没有证明“强制英文小问题”能带来稳定收益，不能默认开启，更不能外推到 500 题或 validation。

变化题的人工预复核也说明不能只看文件命中：`qst_0233`、`qst_0300`、`qst_0320` 虽然从未覆盖变为标准文件覆盖完整，但仍拒答、选错结论或漏掉数值；`qst_0189`、`qst_0331` 的标准文件一直完整，回答却从通过变成失败或待复核。它们分别是“文件或材料阶段改善但回答没用好”和“材料在、回答组织变化”的例子，不能算英文检索收益。

完整产物位于 `output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/evaluations/english-subquestion-language-deepseek-analysis-30-001/`：`evaluation-config.json`、`results.jsonl`、`candidate-audit.md`、`case-review.md`、`manual-review.jsonl/.md`、`subquestion-language-compliance.json/.md`、`english-subquestion-language-impact-summary.md` 和 `english-subquestion-language-manual-review.md`。这些都是 analysis 记录，不能当成 validation 泛化结果。

### 20.9 更换 `Qwen/Qwen3-VL-Reranker-8B` 后的 30 题对照

这轮只改 Rerank 模型：从 `Qwen/Qwen3-Reranker-4B` 改为 `Qwen/Qwen3-VL-Reranker-8B`。回答模型和判卷模型仍是 `deepseek-ai/DeepSeek-V4-Flash`；Embedding、提示词、候选数 30、最终 8 段、Auto-merging、结构化分块语料、Milvus collection 和 analysis 30 题清单均没有改变。没有运行 validation、没有重跑 500 题、没有重新入库，也没有写入 `tutorial_verify_embeddings`。

这次必须把两个测试分开看：一个测试“在同一批资料里，8B 会不会把正确资料挑出来”；另一个测试“整条链路最后能不能答对”。前者能直接看 Rerank，后者还会受到第一次找资料、复杂题拆分、回答和判卷服务波动影响。

#### A. 固定同一批资料后，8B 有一处真实改善

固定旧 4B 当时收到的问题和候选资料，让 8B 在完全相同输入上重新挑最后 8 段；不调用回答模型和判卷模型。30 道题里有 5 道缺少可用的旧候选记录，因此可比较的是 25 道。5 并发下 48 / 48 次挑选请求成功，8B 自身没有超时。

| 指标 | 旧 4B | 新 8B |
| --- | ---: | ---: |
| 正确资料全部保留到最后 8 段 | 76.00% | 80.00% |
| 正确资料平均保留程度 | 78.22% | 82.22% |
| 资料保留改善的题 | 0 | `qst_0295` |
| 资料保留变差的题 | 0 | 0 |

`qst_0295` 是旧 4B 把正确资料从最终 8 段排掉、8B 保住正确资料的一处明确例子。因此可以说：**在候选资料已经一样时，8B 对这道相似事故干扰题更会挑资料。** 但它只改善了 1 道，不能据此说 8B 整体明显更强，也不能解决“正确资料第一次根本没找进来”的题。

#### B. 放回完整链路后，没有看到整体收益

完整 30 题首先以 5 并发运行，随后只补跑服务异常题：11 道以 2 并发补跑，剩余 4 道以 1 并发补跑。最终 30 道中有 27 道拿到可用记录，仍有 3 道服务异常：`qst_0275`、`qst_0345` 是单题超过 600 秒总时限，`qst_0335` 是判卷服务超过 90 秒。它们不是 8B 挑错资料，不能算作模型答错。

27 道可用记录本身的结果是：最终标准文件全部到位 15 / 27（55.56%），平均文件覆盖 57.20%，回答通过 8 / 27（29.63%）。这只是本次运行的记录，不可直接和旧 4B 的不同异常题相减。

为公平比较，只保留旧 4B 和新 8B 都没有服务异常的同 22 道题：

| 指标 | 旧 4B | 新 8B |
| --- | ---: | ---: |
| 最终标准文件全部到位 | 54.55% | 50.00% |
| 最终标准文件平均覆盖 | 59.97% | 52.02% |
| 回答通过 | 31.82% | 27.27% |

8B 在 `qst_0320` 把标准文件补到最终结果，但 `qst_0037`、`qst_0189`、`qst_0356` 的文件覆盖变差；没有新增答对题，`qst_0189` 从旧 4B 的通过变为失败。完整链路每次第一次找出的资料池和分支会有波动，因此这些下降不能全部归罪于 8B；但它们足以说明目前**没有证据证明换成 8B 会带来整条链路的净收益**。

#### C. 运行稳定性和当前决策

8B 在 10 并发、5 秒单次超时下不稳定；固定资料池测试中 48 次请求有 30 次超时。降到 5 并发后 48 / 48 成功，说明 5 是当前可用的 Rerank 并发上限。后续回答和判卷服务的超时发生在 DeepSeek 链路，不应误记为 8B 的能力或稳定性问题。

当前结论是：**不把 8B 当作 4B 的默认替代方案，也不把这次对照写成优化成功。** 8B 在固定资料池里有一处可复核改善，但在真实完整链路中没有形成整体提升。当前 `.env` 的临时配置不在本节自动回退；任何默认配置变更都应在用户确认后单独处理。

本轮可复查产物如下：

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-structured-targeted-004/
  evaluations/rerank-qwen3-vl-8b-shadow-30-retry-003/
    evaluation-config.json
    results.jsonl
    summary.json
    report.md
    case-summary.json
  evaluations/rerank-qwen3-vl-8b-analysis-30-workers5-004/
    evaluation-config.json
    results.jsonl
    candidate-audit.md
  evaluations/rerank-qwen3-vl-8b-analysis-30-workers2-retry-005/
    evaluation-config.json
    retry-manifest.json
    attempt-results.jsonl
  evaluations/rerank-qwen3-vl-8b-analysis-30-workers1-retry-006/
    evaluation-config.json
    retry-manifest.json
    attempt-results.jsonl
```

下一步若要继续验证，应扩大“固定同一批资料池”的 Rerank 对照范围，只看正确资料是否被保留到最后 8 段，不混入回答和判卷服务波动。它仍然只能在 analysis 范围内找原因，不能替代 validation 泛化验证。

## 21. 下一轮提议：1,022 篇语料上的结构保护语义切分对照

### 21.1 先说结论：这还是计划，不是实验结果

下一轮不再做“旧递归切分 vs Markdown 切分”。旧递归切分已经明显不如当前的 Markdown 结构化切分，继续拿它做主对照不能回答现在真正关心的问题。

下一轮只比较两种方式：

| 组别 | 分块方式 | 说明 |
| --- | --- | --- |
| 对照组 | `markdown_header_recursive_v1` | 保留当前已经验证过的 Markdown 标题、列表、表格、代码块和父子块关系。长普通段落仍按现有安全切法处理。 |
| 实验组 | `markdown_header_semantic_fallback_v1` | 先保留同样的 Markdown 结构；只有超过长度上限的普通段落，才根据段落内部内容变化选择更自然的断点。 |

本节写的是尚未实施的方案。当前没有新建语料、没有重新入库、没有运行 500 题，也没有任何可以称为“语义切分有效”的新结果。

### 21.2 新一轮固定的资料范围

为了先把成本降下来，两组都使用完全相同的轻量资料库：

```text
标准证据文档：722 篇
普通干扰文档：300 篇
合计：1,022 篇
```

300 篇干扰文档必须先固定下来，不能每组临时随机抽。建议使用固定随机种子 `20260813`，并保存每个干扰文档的文档 ID、来源、文件哈希和选择理由。全部 722 篇标准证据文档必须排除在干扰候选之外；两组使用同一份干扰清单、同一份题目清单、同一份模型和同一份评测参数。

这 1,022 篇是为了做开发阶段的 500 题对照，不是旧正式基线的替代品。旧基线使用 7,222 篇资料，因此新旧分数不能直接当成同一个实验的前后提升。

### 21.3 “按内容意思切”具体怎么落到代码

这不是把整篇英文资料交给一个黑盒工具，也不是让模型凭感觉改写原文。具体链路如下：

1. `backend/indexing/document_loader.py` 仍然先识别标题、段落、连续列表、表格和代码块，并保留标题路径、原文起止位置、块层级和父子关系。
2. 只挑出长度超过 800 字符的 `paragraph`。列表、表格、代码块和短段落完全沿用当前 Markdown 行为，不参与这一步。
3. 在准备语料的阶段集中处理这些长段落，不在 8 个 `DocumentLoader` 线程里各自调用 Embedding。入口放在 `backend/evaluation/runner.py` 的 `_prepare_enterprise_documents()` 附近，先生成一份冻结的断点计划，再交给并发加载器执行。
4. 先按英文句号、问号、感叹号和换行把长段落拆成完整句子。句子太短、标点异常或无法稳定拆句时，不伪装成语义切分，记录 `semantic_not_applicable`，再使用当前安全递归切法。
5. 将相邻 3 句组成一个滑动窗口，用现有 `backend/indexing/embedding.py` 的批量 BGE-M3 向量一次计算这些窗口。向量已经归一化，因此相邻窗口的点积可以直接用来判断“前后内容是否仍在讲同一件事”。
6. 如果相邻窗口的接近程度明显下降，就把这里作为候选断点；从候选断点中选择能让块长度落在现有 L1/L2/L3 上限内的位置。初始固定参数为：`sentence_window=3`、`min_l3_chars=300`、`breakpoint_percentile=20`，块大小仍是 L1/L2/L3 的 `2400/1600/800` 字符，重叠仍是 `400/200/100` 字符。
7. 最终切分仍然只在完整句子之间进行，不在句子中间截断。标题、列表、表格和代码块的原文不能被这一步拆开；每个新块必须继续带上原来的标题路径和原文范围。
8. 断点计划要写成 `semantic-boundaries.jsonl`，至少包含文档名、段落原文范围、句子范围、候选断点、最终断点、使用的参数、向量批次标识和是否回退。入库 manifest 记录断点文件哈希、语料哈希和策略名。

实现上新增策略名建议为：

```text
markdown_header_semantic_fallback_v1
```

不直接使用 LangChain 实验性 `SemanticChunker` 作为本轮实现。原因不是它一定不能用，而是本轮需要保留本项目已有的标题、列表、表格、代码块、原文范围和父子块审计信息；直接套一个实验性黑盒切分器，很难证明到底切了哪里，也容易把结构化内容拆坏。LangChain 的结构化切分文章和实验性组件资料只作为设计参考，不作为本轮已验证能力。

### 21.4 必须有的失败处理

- 语义断点计算失败、Embedding 服务失败或断点文件写不完整时，准备语料直接失败；不能悄悄改用旧切法后还把结果标成语义切分。
- 只有明确记录 `semantic_not_applicable` 的段落，才允许按当前安全递归方式回退。
- 任何列表、表格或代码块被拆开，离线审计直接判失败，不能进入 500 题评测。
- 新策略必须是显式配置，默认仍是当前旧路径；不能改变默认业务集合，也不能覆盖旧评测集合。

### 21.5 先做离线检查，再做完整评测

正式请求模型前，先不写 Milvus、不写 PostgreSQL、不跑回答和判卷模型，只对分块结果做离线审计。审计对象包括原来 30 道定向难题对应的答案文档，再加一份固定随机样本。

离线审计至少检查：

| 检查项 | 要回答的问题 |
| --- | --- |
| 结构不被破坏 | 列表、表格、代码块是否仍是完整的一块？标题路径是否保留？ |
| 事实是否被切散 | 同一段里相邻的条件、数字、版本、日期是否被不必要地分开？ |
| 块长是否合规 | 是否遵守 L1/L2/L3 上限、最小长度和重叠要求？ |
| 断点是否可复查 | 每个语义断点能否回到原文句子和向量相似度？ |
| 重复是否异常 | 是否因为重叠或回退产生大量重复块？ |
| 回退是否透明 | 哪些段落没有使用语义断点，原因是否写清楚？ |

只有离线审计通过，才允许建立两个全量索引。两组不能复用当前“43 篇目标文档使用新切法、其他文档使用旧切法”的混合索引，因为那样无法回答两种完整分块方式谁更好。

### 21.6 500 题对照的运行方式

在你确认计划后，按下面的口径执行：

1. 建两个新的、完全隔离的 corpus run 和 Milvus collection：一个是 Markdown 对照组，一个是语义增强组；均使用同一份 1,022 篇资料。
2. 两组各跑全部 500 题。为了便于开发检查，本轮允许查看 500 题的题目、答案和逐题结果；但报告必须明确标注为“开发对照”，不能冒充旧正式 baseline 或 validation 泛化结果。
3. 保留现有内部 `analysis`/`validation` 分类用于统计，但新增一个仅本轮使用的可见 500 题运行类型，例如 `development_all_500_v1`。旧的 300/200 盲态规则和历史产物不改写。
4. 两组评测配置分别记录 `evaluation-config.json`、`evaluation_id`、`changed_variable`、模型快照、Embedding、Rerank、语料哈希、题目清单哈希和并发数。每题保存 JSONL、candidate trace、汇总、人工复核记录和异常重试记录。
5. 文档准备阶段建议 8 并发；评测阶段先用 5 并发，服务异常题只补跑且降到 2 并发。异常题不能算成答案错误，也不能用缺题结果计算主指标。

### 21.7 结果必须分四层说

两组比较时，不能只报“答对了多少”。每道题至少拆成：

1. 正确文件有没有进入第一次找出的资料池；
2. 题目真正需要的关键事实有没有出现在资料池；
3. 这些事实有没有进入最后交给回答模型看的 8 段；
4. 回答模型有没有据此答对；另外单独列系统异常。

这样才能知道新切法到底改善了哪一层。比如文件找到了但关键句仍不在最后 8 段，说明问题不在“有没有找到文件”；证据已经齐全但答案仍错，说明问题也不在分块本身。

### 21.8 这轮能证明什么，不能证明什么

如果实验组在同一批 1,022 篇资料、同一批 500 题、同一模型和同一后续链路下，稳定提高了“关键事实进入最终 8 段”的比例，同时没有明显增加结构破坏、异常和块数量，才能说它对长普通段落有实际帮助。

它能说明：在这份轻量英文资料和这 500 道开发题上，长段落按内容变化选断点是否比当前安全切法更容易把相关事实放在一起。

它不能说明：

- 语义切分一定适用于中文、表格密集文档或其他资料库；
- 1,022 篇轻量语料的结果可以直接替代 7,222 篇正式 baseline；
- 所有错误都来自分块；
- 只要文件命中率提升，答案就一定提升；
- 可以跳过之后的 validation 盲态验证。

### 21.9 参考资料

本方案参考了以下公开资料，但没有把它们的结论当成本项目实测结果：

- [LangChain：A Chunk by Any Other Name](https://www.langchain.com/blog/a-chunk-by-any-other-name)：说明按文档结构逐层切分比一上来把所有文本混在一起更容易保留上下文。
- [LightRAG Paragraph Semantic Chunking](https://github.com/HKUDS/LightRAG/blob/main/docs/ParagraphSemanticChunking.md)：说明可以只对普通段落计算相邻内容变化，而不是破坏所有 Markdown 结构。
- [Unstructured：Semantic Chunking for RAG](https://unstructured.io/insights/semantic-chunking-for-rag)：说明语义断点适合处理长段落，但仍要结合文档结构和长度约束。
- [LangChain 迁移讨论](https://github.com/langchain-ai/langchain/issues/35553)：说明 `SemanticChunker` 仍属于实验性组件路径，本轮不把它直接当作稳定主链路。

### 21.10 当前下一步

当前只需要你确认这份计划。确认后再按以下顺序推进：

```text
建立新 Trellis 任务和三份计划文档
  -> 固定 300 篇干扰文档清单
  -> 实现语义断点策略和单元测试
  -> 做不入库的离线分块审计
  -> 两组各建 1,022 篇完整索引
  -> 两组各跑 500 题开发对照
  -> 做逐题四层链路比较和人工复核
```

在你确认前，不启动上述任何实验动作，不运行 validation，不重跑旧 500 题，不删除 Milvus 集合，也不操作 `tutorial_verify_embeddings`。

## 22. 1,022 篇资料、500 道开发题的实际对照结果

### 22.1 先说结论

这轮已经完成，不再是计划。当前 Markdown 结构化切分仍然更适合作为默认方案；“只对过长普通段落按内容变化切开”的语义切分，没有在这 500 道开发题上带来整体回答提升。

语义切分在“先找资料”和“最后交给回答模型的 8 段资料”两个环节有很小的正向变化，但这些变化没有转成更多正确回答，反而少答对了 11 道题。因此不能把它写成优化成功，也不应替换当前的 `markdown_header_recursive_v1`。

这不是 validation，也不能替代 7,222 篇资料上的旧正式基线。它只能说明：在同一批 1,022 篇英文资料和同一批 500 道可见开发题里，这个具体的语义切分做法没有显示出净收益。

### 22.2 两组到底有什么相同和不同

两组都使用：722 篇标准证据文档、同一份固定的 300 篇普通干扰文档、同一份冻结 500 题清单（SHA-256 都是 `02954cc10082b735596ff8883f924935ef26073ff6d762df72307228153ebb53`）、`deepseek-ai/DeepSeek-V4-Flash`、`BAAI/bge-m3`、`Qwen/Qwen3-Reranker-4B`、候选资料数 30、最后 8 段、自动合并和相同提示词。

| 组别 | 唯一资料处理差异 | corpus run | 最终评测 |
| --- | --- | --- | --- |
| Markdown 对照组 | `markdown_header_recursive_v1` | `enterpriserag-en-representative-markdown-dev500-300-005` | `markdown-dev500-300-eval-011` |
| 语义组 | `markdown_header_semantic_fallback_v1` | `enterpriserag-en-representative-semantic-dev500-300-003` | `semantic-dev500-300-eval-009` |

语义组实际只对 674 个过长普通段落计算了内容断点，形成 1,458 个断点；标题、列表、表格、代码块和短段落仍按 Markdown 结构处理。

语义组最后有 3 道题曾遇到服务等待问题，因此只补跑这些异常题。补跑把整题最长等待从 600 秒提高到 1,200 秒；其中最后 1 道还把单次模型请求等待从 90 秒提高到 180 秒。这个改动只是不提前掐断服务请求，不改变资料、检索、回答模型或提示词；每次变化都记录在 `semantic-dev500-300-eval-008` 和 `semantic-dev500-300-eval-009` 的 `evaluation-config.json`。最终两组都是 500/500，系统异常都是 0。

### 22.3 总体数字

| 指标 | Markdown 结构化切分 | 语义切分 | 变化 |
| --- | ---: | ---: | ---: |
| 标准证据文件全部到位 | 83.33% | 83.54% | +0.21 个百分点 |
| 平均标准证据文件覆盖 | 87.92% | 87.65% | -0.27 个百分点 |
| 回答通过 | 69.40%（347 / 500） | 67.20%（336 / 500） | -2.20 个百分点，少 11 道 |
| 人工复核 | 6.60% | 6.80% | +0.20 个百分点 |
| 系统异常 | 0 | 0 | 无变化 |

发现了什么：语义切分的“标准文件全部到位”只多了 1 道，但回答通过少了 11 道。

为什么这样判断：以上是两组各自完整 500/500 的 `summary.json`，使用同一题目清单和同一非分块配置，不是从缺题结果里估算出来的。

这能说明什么：这套语义切法没有证明整体更好；当前结果更支持继续使用 Markdown 结构化切分。

这不能说明什么：它不能证明“按内容变化切开永远没用”，也不能证明那 11 道下降全部由切分造成。回答和独立判分都调用远程模型，同一模型重复运行仍可能有输出波动；其中 3 道语义题还经过了只针对异常的补跑。

### 22.4 分开看资料在哪一步变化

下面只统计 470 道有明确标准证据文件的题；20 道本来就应该拒答，10 道没有标准文件，不能混进“资料是否找齐”的分母。

| 资料位置 | Markdown | 语义 | 语义相对变化 |
| --- | ---: | ---: | ---: |
| 第一次找出的资料池里，标准文件全部齐全 | 426 / 470（90.64%） | 430 / 470（91.49%） | 净多 4 道 |
| 最后真正交给回答模型的 8 段资料里，标准文件全部齐全 | 415 / 470（88.30%） | 421 / 470（89.57%） | 净多 6 道 |
| 一开始文件齐全、后来在筛选中丢文件 | 11 道 | 9 道 | 少 2 道 |

逐题来回变化也要保留：语义组让 14 道题在第一次资料池里从“不全”变“全”，同时让 10 道从“全”变“不全”；在最后 8 段里，改善 17 道、变差 11 道。也就是说，语义切分不是单向提升，只是最后净多保住了 6 道的标准文件。

回答层面则相反：32 道题从 Markdown 的不通过变为语义组通过，但有 43 道从 Markdown 通过变为语义组不通过，净少 11 道；两组都通过 304 道、两组都不通过 121 道。

这能说明什么：新切法可能帮助了少量“资料被分得不方便一起找到”的题，也略减少了“第一次找到、后面又被筛掉”的情况。

这不能说明什么：文件出现不等于答案所需的每个关键句都在里面，更不等于回答模型正确使用了它。当前 500 道记录里的 `expected_fact_audit` 都还是 `pending_manual_review`，因此没有自动化的“关键句已经找到”结论；本节严格只报告可核对的文件位置、最后 8 段和回答结果。

### 22.5 哪些题型变化最大

- 数据集标为 `semantic` 的 125 道题：语义组标准文件全部到位从 99 道到 100 道，第一次资料池文件齐全从 111 道到 115 道，最后 8 段文件齐全从 109 道到 111 道；但回答通过两组都是 81 道。也就是说，即使在这类题里，资料位置略有改善，也没有增加答对数。
- `constrained` 题：回答从 19 / 30 降到 15 / 30，是回答下降最多的一类；标准文件全部到位只少 1 道，不能仅凭文件数量解释这 4 道下降。
- `completeness` 和 `conflicting_info` 各少答对 2 道；它们的标准文件全部到位数量没有变化。这里更需要逐题看回答是否漏条件、选错冲突版本或受到不同上下文排列影响。
- `project_related` 两组都只答对 5 / 40，资料文件到位也几乎不变。它仍是当前最难的题型，但本轮不能据此说语义切分让它更难或更容易。

### 22.6 三道超时题补完后，实际说明了什么

- `qst_0361`：此前已经检索和回答完成，只是最后判分等满 90 秒。提高等待时间后完整跑完，资料文件全到位、回答通过。这说明此前是系统等待问题，不能把旧超时记成答错。
- `qst_0378`：资料文件全到位，最终也完整判完，但回答失败。它说明“资料文件齐全”仍不足以保证回答正确；这不是分块失败的直接证据。
- `qst_0436`：完整跑完后只找到一半标准文件，回答少了一个要求的支持票号，因此失败。这次失败是资料不完整导致回答不完整，不是系统异常。

### 22.7 当前决定和下一步验证

当前决定：不把 `markdown_header_semantic_fallback_v1` 设为默认，不继续把它当作“已验证有效”的优化；保留代码、隔离 collection 和全部产物，方便以后复查。

下一步若要继续研究，不能直接调语义切分阈值来追着这 500 道题刷分。更合理的验证是先人工抽查回答变化的 75 道题（32 道变好、43 道变差），逐题分开确认：

1. 正确文件有没有进入第一次资料池；
2. 题目需要的关键句是否真的在资料里；
3. 关键句是否进入最后 8 段；
4. 回答是否漏写、添写或选错；
5. 是否存在系统异常。

只有确认回答下降确实反复来自某一种“长普通段落被切开”的情况，才值得提出一个新的、单变量的语义切分方案。若主要是“资料已经在、回答仍然没用好”，下一轮就不该继续改分块。

完整同题对照报告位于：

```text
output/rag-evaluations/enterpriserag/enterpriserag-en-representative-semantic-dev500-300-003/evaluations/semantic-dev500-300-eval-009/markdown-semantic-dev500-comparison.md
```
