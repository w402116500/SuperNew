# PRD：相邻 L3 块扩展 147 道 analysis 错题评测

## 背景

T10 已在 11 道“关键事实位于命中块直接相邻位置”的题上证明相邻 L3 扩展可能有效。本任务把同一唯一变量扩大到 `baseline-rag-010` 中自动判卷未通过的全部 analysis 题，确认它对现有错题的实际修复范围。

## 目标

- 冻结 `baseline-rag-010` 中 147 道 analysis 非通过题：123 道 `fail` + 24 道 `review`。
- 只改变 `adjacent_l3_expansion`，评估证据补回、回答修复和耗时变化。
- 保存足够的逐题候选链路，使每个“变好、没变、变差、不可归因”都可以回查。
- 不重新测试 500 题，不重新入库，不访问 validation，不操作 `tutorial_verify_embeddings`。

## 非目标

- 不改变分块、Embedding、模型、提示词、Rerank、Auto-merging 或最终 top-k。
- 不把 `review` 自动当作错误或通过；系统异常、判卷超时和证据评分不可用单独统计。
- 不用 147 道 targeted 结果冒充 500 题总体结果或 validation 泛化结果。

## 验收标准

1. target manifest 恰好包含 147 个唯一 `analysis` case ID，来源结果和 case split 哈希可验证，且明确排除原本 `pass` 的 153 题和全部 validation。
2. 新 evaluation ID 使用 `changed_variable=adjacent_l3_expansion`，配置不可变，候选 trace 强制开启。
3. 每个成功或异常 case 均有原始 L3、相邻扩展、Auto-merging、Rerank、最终 top-8、证据、回答、判卷和分段耗时记录；超时/失败 case 可安全重试且不覆盖成功记录。
4. 汇总报告至少包含：非通过题修复率、证据全覆盖率、平均证据覆盖率、回答通过率、不可归因数量、候选数量变化、RAG/生成/端到端 P50/P95。
5. 人工复核记录区分：相邻材料补回并回答通过、部分补回、候选后丢失、证据到位但回答失败、原始目标未命中、系统异常和判卷不确定。
6. 全量单测、Python 编译、`git diff --check` 和 Trellis context 校验通过。
