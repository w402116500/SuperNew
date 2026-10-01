# 脚本索引

这里保留项目运行、文档构建和可重复评测所需的脚本。根目录的旧流程图生成器和一次性调试文件已移入仓库外的本地归档。

## 开发环境

| 脚本 | 用途 |
| --- | --- |
| `supermew.ps1` | 启动、停止、重启、查看状态和日志；远程模型模式使用 `-NoOllama`。 |

根目录的 `start.bat`、`stop.bat` 是 Windows 入口；`start.sh`、`stop.sh` 与 `server-start.sh`、`server-stop.sh` 是服务器入口。

## 可视化文档

| 脚本或目录 | 用途 |
| --- | --- |
| `build_supermew_visualization.py` | 构建根目录的 `supermew_visualization.html`。 |
| `viz_builder/` | 上述可视化文档所需的 HTML、样式与交互源码。 |

## 评测与分析

| 脚本 | 用途 |
| --- | --- |
| `run_rag_baseline.py` | 使用明确指定的题集和评测账号运行问答基线。 |
| `run_rag_evaluation.py` | 离线评测 CLI 入口，管理语料准备、执行和结果。 |
| `init_evaluation_parent_store.py` | 初始化隔离的评测父块存储。 |
| `analyze_rag_failures.py` | 分析逐题结果和失败类别。 |
| `shadow_compare_rerank_model.py` | 对候选证据做 Rerank 模型对照。 |
| `audit_*chunking.py` | 审计结构化或语义分块。 |
| `audit_raw_candidate_facts.py` | 检查原始候选中的事实证据。 |
| `create_*manifest.py` | 生成固定实验或人工审查清单。 |
| `finalize_*manual_review.py` | 汇总人工审查记录。 |
| `summarize_*.py` | 汇总分块、邻接扩展等实验结果。 |

这些脚本属于离线研究与评测流程，应用启动时无需全部执行。评测结果保留在 `output/rag-evaluations/`，不要把语料、报告和数据库备份当成临时缓存清理。

## 文件放置

- 可重复使用的运维、构建和评测脚本放在 `scripts/`。
- 一次性调试、烟测、下载样本和运行日志放在 `tmp/`。
- 浏览器采样文件放在已忽略的 `.playwright-cli/` 或 `.playwright-mcp/`。
- 清理已有本地文件时先核对引用和运行状态，采用保留原路径与校验清单的归档方式。
