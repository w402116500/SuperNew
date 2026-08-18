# 实施计划：相邻 L3 块扩展 147 道 analysis 错题评测

## 阶段 1：冻结输入

1. 从 `baseline-rag-010/results.jsonl` 和冻结 `case-split.json` 选择 `analysis` 且自动 verdict 非 `pass` 的 147 道题。
2. 生成 `adjacent-l3-expansion-analysis-147-target-manifest.json`，保存选择规则、verdict 分布、源文件哈希、case split 哈希、稳定排序和 manifest 哈希。
3. 验证 147 个唯一 ID，确认 0 个 validation，确认 153 个 baseline pass 未被选入。

## 阶段 2：扩展 manifest 和测试

1. 让相邻 L3 manifest 同时支持 T10 的 11 题人工缺口模式和本任务的 147 题 non-pass analysis 模式。
2. 增加 manifest 选择规则、源结果一致性、动态题数和 verdict 分布校验。
3. 增加/更新单测，覆盖 147 题生成、pass 排除、validation 排除、哈希漂移、重复 ID、错误选择规则和 target trace 强制开启。

## 阶段 3：质量门

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q backend scripts/create_adjacent_l3_expansion_manifest.py scripts/run_rag_evaluation.py
git diff --check
python ./.trellis/scripts/task.py validate 08-18-enterpriserag-adjacent-l3-expansion-analysis-147
```

## 阶段 4：运行 147 题

1. 只读复用现有集合，使用新 evaluation ID 运行完整 RAG 和 candidate trace。
2. 本次恢复使用 `evaluation_worker_count=10`；这是执行吞吐参数，不改变 `adjacent_l3_expansion` 之外的任何 RAG 输入。
3. 依据当前服务限额记录运行边界：bge-m3 为 2,000 RPM / 500,000 TPM，DeepSeek 为 500 RPM / 2,000,000 TPM，Rerank 为 2,000 RPM / 1,000,000 TPM。
4. 保存逐题 JSONL、候选审计、汇总报告、人工复核队列和延迟拆分。
5. 如有缺失/异常，只通过新的 retry evaluation 保留成功题并补跑异常题；不从第 1 题重跑。

## 阶段 5：复核和记录

1. 对自动通过、证据改善、证据/回答不一致、review、system_error 和目标材料状态变化题完成人工复核。
2. 在 `docs/rag-evaluation-implementation.md` 追加本轮范围、指标、修复率、不可归因项、延迟和限制。
3. 在本文件实施记录中写入 manifest 哈希、evaluation ID、完成度、人工结论和最终质量门。
4. 评测完成后等待用户决定是否做小型 pass 回归 guard 或 validation 200 泛化测试。

## 风险和回退

| 风险 | 防护 |
| --- | --- |
| 147 题运行时间长 | 分批 checkpoint；只重试缺失/异常题；不覆盖成功结果 |
| 候选增加导致精排变慢 | 记录原始/扩展候选数和 Rerank、生成、端到端 P50/P95；最终 top-8 不变 |
| 失败筛选造成选择偏差 | 报告明确标记 targeted analysis；不冒充 500 题总体结果 |
| 系统异常被算成优化失败 | `system_error`、判卷不可用和超时单独分类，人工复核后再归因 |
| 新 manifest 误含 validation/pass | 运行前执行 split、verdict、哈希和唯一 ID 全量校验 |

## 实施记录

| 时间 | 状态 | 记录 |
| --- | --- | --- |
| 2026-08-18 | 已批准启动 | 用户确认将相邻 L3 扩展从 11 道 targeted 题扩大到 baseline 的 147 道 analysis 非通过题；不重跑 500 题、不运行 validation。 |
| 2026-08-18 | 实现完成 | 扩展相邻 L3 manifest 契约，新增 `selection_mode=non_pass_analysis`；保留原 11 题 `adjacent_leaf_gap` 模式，新增源结果 verdict、pass 排除数和选择规则校验。 |
| 2026-08-18 | 输入冻结 | 生成 `baseline-rag-010/adjacent-l3-expansion-analysis-147-target-manifest.json`：147 个唯一 analysis ID，`fail=123`、`review=24`，排除 pass=153；文件 SHA-256 为 `0EFC6E7F5F186E82B83D383948E10012C233A85FDD29A948BD1236CF382FA494`，payload 哈希为 `a047744fa08bdd63742321ec89d0518f503e28c04ba3ca5b9203addf9dc851b9`。 |
| 2026-08-18 | 校验修复 | 首次运行前发现 manifest 验证只统计非通过子集而清单记录全体 verdict 分布，已修正为按全部 analysis 结果校验；未发生模型调用或结果写入。 |
| 2026-08-18 | 并发恢复实现 | 评测器新增受控 worker pool；每个 worker 一次只执行一道题，主进程统一 checkpoint，异常 worker 单独回收。新增 `evaluation_worker_count` 配置和 `--workers` 参数；retry 允许只调整该执行参数，不允许改变 RAG 输入。115 项单测通过，编译和 `git diff --check` 通过。 |
| 2026-08-18 | 并发恢复待运行 | 用户确认使用 10 并发。源 evaluation `t10-adjacent-l3-expansion-analysis-147-001` 已保存 75/147 道且无题级 `evaluation_error`；新 retry 将保留这 75 道，只补跑缺失 72 道，使用新的 evaluation ID，不重新入库、不运行 validation。 |
| 2026-08-18 | 运行完成 | retry evaluation `t10-adjacent-l3-expansion-analysis-147-002` 完成 147/147：保留 75 道，10 worker 补跑 72 道，补跑题级 `evaluation_error=0`。全量自动指标为证据全覆盖 55.78%、平均覆盖 62.70%、自动回答通过 27.89%（41/147）；相对冻结 147 题基线分别变化 +12.93、+14.32、+27.89 个百分点。相邻扩展实际执行 147/147，每题新增候选 P50 为 48、P95 为 57；端到端 P50 为 87.28 秒。 |
| 2026-08-18 | 人工归因完成 | 复核候选链路、参考答案和模型答案后，仅 `qst_0039`、`qst_0068`、`qst_0082`、`qst_0158`、`qst_0449` 五题可确认是“直接相邻材料补回并回答通过”。其余 36 道自动通过不具备事实级直接相邻证明或覆盖未变，不能算作变量收益。9 道系统异常（2 回答超时、3 证据评分不可用、4 判卷超时）单独排除。结论写入运行目录 `manual-review-conclusions.jsonl` 和 `manual-review-conclusions.md`；未读取 validation。 |
