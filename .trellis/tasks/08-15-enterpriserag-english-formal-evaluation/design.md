# 技术设计：英文 EnterpriseRAG 正式评测

## 设计目标

将“构造一次评测语料”和“在该语料上运行一次实验”分开。前者最耗时，后者需要反复比较检索或 RAG 参数；二者分离后，单变量实验不必重复向量化数万至十余万个 L3 分块，也不会改写基线配置。

## 数据边界

```text
EnterpriseRAG 原始 Parquet（英文）
  -> 代表性 / 挑战语料选择 + Markdown
  -> L1/L2 写入父块存储，L3 写入独立 Milvus 集合
  -> corpus manifest（文档角色、哈希、分块数、cleanup 清单）
  -> 固定 500 题与 300/200 分层 manifest
  -> 多个实验运行（检索-only 或完整 RAG）
  -> 逐题 JSONL、汇总 JSON、人工可读报告与复核记录
```

正式语料的 Markdown 为标题和原始英文正文，不经过翻译。Representative 预计为 `722 + 6,500 = 7,222` 篇；Challenge 的上限是在其上附加 1,000 篇题级难干扰，实际数量必须写入 manifest，不能事先假定没有去重。

L3 是唯一写入 Milvus 的向量；L1/L2 以 `__rag_eval__<corpus_run_id>__` 文件名前缀写入现有 PostgreSQL/Redis 父块存储。每个实验只能从自己的 `collection_name` 和该前缀读取，线上默认集合不参与。

## 需要补齐的运行器能力

1. 在 `backend/evaluation/datasets.py` 以固定种子按十类题型生成 `analysis` 300 题与 `validation` 200 题；写入不可变 `case-split.json`，包含题目 ID、题型、配额和全文件 SHA-256。
2. 在 `backend/evaluation/runner.py` 将 corpus 准备与实验输出拆分。语料 run 保存集合和文档 manifest；实验 run 保存 `evaluation-config.json`、来源语料 run、来源 manifest 哈希、所用 case set、环境中可公开的模型标识及代码版本。
3. 为 `evaluate` 增加 `--evaluation-id` 和 `--case-set {all,analysis,validation}`。默认兼容现有运行；新正式流程按实验 ID 写入独立目录，禁止覆盖既有 `results.jsonl`。
4. 对完整 500 题基线仅在报告中显示整体及按题型聚合，不在自动报告中列出验证集失败题；验证集的盲态依赖评测纪律，不把项目所有者无法物理访问本地文件伪装成安全隔离。
5. 汇总报告增加每题型指标、人工复核清单、Rerank 生效统计、输入/输出 manifest 哈希。原始逐题记录继续使用 JSONL，便于题级 checkpoint、断点续跑和人工筛选。

## 实验与模型边界

检索-only 复用同一隔离集合，依次执行 BM25、Dense、Hybrid、条件成立时 Hybrid + Rerank；它不调用主回答模型或 GRADE_MODEL。完整 RAG 复用 `run_rag_graph()` 和独立 `GRADE_MODEL` 请求。判卷失败或结构非法一律为 `review`，不得猜测判定。

首轮冻结：L1/L2/L3 `2400/1600/800` 字符、重叠 `400/200/100`、本地 `BAAI/bge-m3`、`top_k=8`、候选池 `30`、Auto-merging 阈值 `2`、SiliconFlow 上的 `deepseek-ai/DeepSeek-V4-Flash` 与现有 Rerank 配置。后续实验的差异必须写在 `changed_variable` 字段中且只能有一项。

## 恢复、清理和报告

准备完成前 `prepare_completed=false`，评测拒绝使用未完成语料。逐题结果立即 checkpoint；单题超时、模型异常和判卷解析错误保留为可复核记录。所有正式运行的真实命令、耗时、集合名、配置、失败样本、人工结论和“保留/回退”决定追加到 `docs/rag-evaluation-implementation.md`。

最终 cleanup 先删除 Milvus 集合，再按 manifest 删除父块与 Redis 缓存，并写回 cleanup 结果；命令必须幂等。语料 run 在其依赖的实验全部结束前不得 cleanup。
