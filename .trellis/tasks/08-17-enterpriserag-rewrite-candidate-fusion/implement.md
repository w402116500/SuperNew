# 实施计划：改写候选融合单变量评测

## 实施前提

- 本计划只在用户确认本子任务最终计划后启动。
- 复用父任务已冻结的 EnterpriseRAG 英文 Representative 语料和独立集合。
- 不执行 prepare、cleanup、Milvus 删除或默认业务集合操作。

## 步骤

### T9.1：固定小范围清单

1. 从 `baseline-rag-010` 的 analysis JSONL 提取实际存在 `rag_trace.rewrite_method` 的题目。
2. 按 case ID 稳定排序，写入 15 题 target manifest 和 SHA-256。
3. 记录基线来源、case split 哈希和每题基线证据覆盖；禁止读取或写入 validation 细节。
4. 为非法、重复或 validation case-ID 清单添加拒绝测试。

### T9.2：实现候选融合开关

1. 复用现有候选获取、Auto-merging、Rerank 和诊断构造逻辑，避免复制 Hybrid/Dense/BM25 分支。
2. 在 `RetrievalRuntime` 加入默认关闭的融合开关，并从评测运行器显式传入。
3. 初始节点保存本轮内部候选；改写节点在开关开启时合并两次候选、按稳定 ID 去重、以原始问题统一筛选最终 8 段。
4. 保留关闭开关时的既有覆盖行为，确保基线和线上默认调用无变化。
5. 记录融合数量、去重数量、最终来源及降级原因，并在 Schema 白名单中定义字段。

### T9.3：单元与契约验证

1. 覆盖默认关闭、只在改写路径启用、按 `chunk_id` 去重、原问题 Rerank、最终 top-k 不变和改写失败回退。
2. 覆盖 15 题清单的来源/哈希/analysis-only 校验，以及 evaluation-config 不可覆盖。
3. 检查 validation 不进入小范围报告、人工复核和清单。
4. 运行：

```powershell
uv run python -m unittest discover -s tests
uv run python -m py_compile backend/evaluation/*.py backend/rag/pipeline.py backend/rag/utils.py scripts/run_rag_evaluation.py
git diff --check
```

### T9.4：15 题小范围真实评测

1. 创建新的 T9 target evaluation ID 和 `changed_variable=rewrite_candidate_fusion` 配置快照。
2. 只运行冻结的 15 道 analysis 题，保存逐题 JSONL、报告、trace 和人工复核。
3. 以基线逐题比较证据覆盖、最终 8 段、回答判定、系统错误/超时和延迟。
4. 人工核查所有变好、变差或系统异常的题；不报告 validation 详情。
5. 仅当至少 3 道证据提升、没有新增系统错误/超时、且下降不超过 1 道并有理由时，记录允许进入完整 analysis 的决定。

### T9.5：完整 analysis 与后续闸门

1. 只有 T9.4 门槛通过后，运行新的 300 道 analysis evaluation ID。
2. 将前后指标、逐题变化、延迟和人工复核追加到 `docs/rag-evaluation-implementation.md`。
3. 向用户呈现 analysis 结论；不开始 T10/T11，等待用户决定后续压力测试或盲态 validation。

## 高风险文件与回退点

- `backend/rag/utils.py`：候选取得和最终筛选必须保持原有错误语义。
- `backend/rag/pipeline.py`：图状态和改写预算不能形成第二次改写循环。
- `backend/schemas/chat.py`：trace 字段为严格白名单，新增字段必须同步类型。
- `backend/evaluation/runner.py`：target manifest 不能扩大 case set、覆盖已有运行或暴露 validation。
- 回退方式：关闭 T9 runtime flag，保留评测产物；不回写语料和集合。

## T9.4 实际执行记录

- 已完成 `t9-rewrite-fusion-targeted-003`，只运行冻结的 15 道 analysis 题。
- Milvus、PostgreSQL、Redis、etcd 和 MinIO 均使用既有服务；没有重新入库、清理集合或访问 `tutorial_verify_embeddings`。
- 实际改写候选融合触发 7/15 题；触发融合的 7 题中证据覆盖提升 0 题，回答变通过 1 题（`qst_0458`），无新增系统错误或超时。
- 全体 15 题的证据覆盖变化不能用于证明融合收益：6 道未触发融合的重跑题出现覆盖提升，无法归因于本变量。
- 人工复核已写入试跑目录的 `manual-review.jsonl` 和 `manual-review.md`；自动报告补充说明自动队列与人工复核的区别。
- 按门槛规则，本轮未通过，不启动 300 道 analysis，不运行 validation。后续需要先固定改写分支的可复现输入/输出，再由用户确认新的唯一试验变量。
