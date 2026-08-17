# 技术设计：改写候选融合单变量评测

## 设计原则

本任务不是新增一次查询改写。当前系统已经有“一次初始检索、证据评分、一次 Step-back 或 HyDE 改写、一次改写检索”的机制。唯一行为变化是：改写检索后不再覆盖首次候选，而是把两次的叶子候选合并后统一完成父块合并和精排。

```text
当前：原问题候选 -> 合并/精排 -> 评分不足 -> 改写候选 -> 合并/精排 -> 覆盖首次结果

T9：原问题候选 -> 合并/精排 -> 评分不足 -> 改写候选
    -> 两批叶子候选按 chunk_id 去重 -> Auto-merging -> 原问题 Rerank -> 最终 8 段
```

首次评分仍使用当前最终上下文，决定是否进入改写。融合后的上下文再次经过当前证据评分；改写预算仍为一次。

## 边界与数据流

1. `backend/rag/utils.py` 将“取得某个查询的原始候选”与“对候选做 Auto-merging、Rerank、阈值过滤”划分为内部可复用步骤。
2. `backend/rag/pipeline.py` 在初始检索节点保留仅供本次图执行使用的原始候选；无改写时继续现有结果，不走融合。
3. 改写检索节点取得第二批原始候选；开关开启时按 `chunk_id` 合并去重，并以原始问题完成一次统一筛选。关闭时严格沿用当前“改写结果覆盖首次结果”的行为。
4. `RetrievalRuntime` 增加默认关闭的融合开关。离线 T9 运行显式打开；在线运行和既有基线不传该开关，因此输出不变。
5. RAG trace 与评测记录增加融合诊断字段；API Schema 只允许经白名单验证后的字段。
6. 评测运行器允许加载经过哈希校验的 analysis case-ID 清单，只对这 15 道题运行；完整 analysis 不传该清单。

## 候选与排序契约

- 每一次检索的 candidate_k 保持冻结值；融合后的候选最多是两批候选的并集。
- 先去重，再 Auto-merging，再 Rerank；不能分别精排后将两批 top-k 直接相加。
- Rerank 的查询必须是原始用户问题。HyDE 的假设性文本仅用于第二次召回，不能成为最终排序依据或回答事实。
- 最终 Rerank 和阈值过滤后仍返回既有 `RETRIEVAL_TOP_K=8`，回答模型的输入规模不扩张。
- 稳定去重键优先使用非空 `chunk_id`；缺失时保守使用 Milvus `id`，并记录诊断，避免依赖正文文本相等。

## 错误与回退

- 初次检索失败：沿用现有空检索/保守拒答行为。
- 改写模型或第二次检索失败：融合开关分支不能清空首次可用候选；恢复首次最终结果，trace 写入 `rewrite_candidate_fusion_fallback_reason`。
- 融合、父块读取或 Rerank 失败：沿用已有失败语义，并保留可诊断错误；不得把系统异常当作证据不足或回答错误。

## 小范围评测契约

- 清单从 `baseline-rag-010/results.jsonl` 的 `case_set=analysis` 且实际具有 `rag_trace.rewrite_method` 的 15 条生成。
- 清单记录源 evaluation ID、源结果 SHA-256、case ID 数量、排序和全文件 SHA-256。运行器拒绝重复 ID、非 analysis ID、与冻结 split 不一致的 ID 或哈希不匹配清单。
- 首个运行使用新的 evaluation ID，例如 `t9-rewrite-fusion-targeted-001`；case_set 仍为 `analysis`，额外记录 target-manifest 哈希。
- 通过人工门槛后，第二个运行使用不同 evaluation ID，例如 `t9-rewrite-fusion-analysis-001`，并运行 300 道 analysis。

## 回滚

- 开关默认 `false`，因此移除 T9 运行配置即可回退线上行为，无需重新入库或删除任何数据。
- 若 15 题门槛不通过，只保留独立运行产物和人工审计，不进入完整 analysis，也不改线上配置。
