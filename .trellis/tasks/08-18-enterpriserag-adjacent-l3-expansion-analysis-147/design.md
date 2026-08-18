# 技术设计：147 道 analysis 非通过题的相邻 L3 扩展评测

## 运行边界

```text
冻结 baseline-rag-010 results.jsonl
  -> 选择 case_set=analysis 且 answer_grade.verdict != pass 的 147 题
  -> 冻结 target manifest
  -> 原始 L3 候选
  -> 同文件前后相邻 L3 一次性扩展
  -> 既有 Auto-merging -> Rerank -> 最终 top-8 -> 回答/判卷
```

只读集合 `rag_eval_enterpriserag_enterpriserag_en_representative_sf_001`。不执行 prepare、insert、cleanup、drop 或 validation。

## Manifest 契约

扩展现有 `adjacent_l3_expansion` manifest，使其支持两种明确选择模式：

- `selection_mode=adjacent_leaf_gap`：保留 T10 的 11 题人工确认相邻缺口契约。
- `selection_mode=non_pass_analysis`：从冻结源 `results.jsonl` 选择 `case_set=analysis` 且 `answer_grade.verdict != "pass"` 的全部题，预期 147 题；必须记录 `source_selection_rule`、原始 verdict 分布和排除的 pass 数。

执行校验必须验证：manifest 自身 payload 哈希、文件 SHA-256、source results SHA-256、case split SHA-256、唯一 case ID、analysis-only、选择规则与源结果一致、`changed_variable` 正确。不得把动态 `expected_count=147` 写成 T10 11 题的全局硬编码。

## 运行配置

- 新 evaluation ID：`t10-adjacent-l3-expansion-analysis-147-001`
- `case_set=analysis`
- `evaluation_mode=rag`
- `changed_variable=adjacent_l3_expansion`
- `adjacent_l3_expansion_enabled=true`
- `candidate_trace_capture_enabled=true`
- 其余配置从已准备 corpus 快照继承；最终回答上下文仍为 8 段

## 审计数据

每个 `results.jsonl` 记录保存：

- 原始候选及其 L3 `chunk_id`；
- 每个相邻候选的来源块、相对位置、去重和跳过/回退原因；
- Auto-merging 后候选、Rerank 输入/输出、阈值结果和最终 top-8；
- 标准证据、召回文件、证据排名、全覆盖/平均覆盖；
- 原始 baseline verdict、B 组 verdict、回答、证据评分路由和错误字段；
- RAG、生成、判卷、端到端耗时及候选数量。

运行目录同时保存 `evaluation-config.json`、`evaluation-progress.json`、`attempt-results.jsonl`、`summary.json`、`report.md`、`case-review.md`、`candidate-audit.md`、`manual-review.jsonl` 和 `manual-review.md`。

## 重试和人工复核

第一次运行保留成功记录。只对缺失、`evaluation_error`、生成异常、判卷异常或超时 case 运行新的 retry evaluation；retry 必须验证源配置和结果哈希，成功记录不重复请求。

自动结果全部覆盖 147 题；人工优先复核所有自动 `pass`、证据覆盖变化、baseline/B 结果不一致、`review`、系统异常和候选链路显示目标材料缺失的题。系统异常不计作 RAG 优化失败。

## 结果解释

主指标是“baseline 非通过题中，人工确认被相邻材料补回且回答通过的题数/比例”。同时报告证据覆盖、回答通过、不可归因和延迟变化。由于题集经过失败筛选，不能从该运行推导 500 题总体通过率；通过分析集归因后，才由用户决定是否进入 validation 200 题。
