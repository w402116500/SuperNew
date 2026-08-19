"""运行隔离的 EcomRetrieval 和 MultiHopRAG 离线评测。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许 ``uv run python scripts/run_rag_evaluation.py`` 从脚本目录直接执行。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.runner import cleanup_run, default_run_id, evaluate_run, prepare_run
from backend.indexing.document_loader import DEFAULT_CHUNKING_STRATEGY, STRUCTURED_MARKDOWN_CHUNKING_STRATEGY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG 离线评测工具，不操作默认业务知识库。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "evaluate", "cleanup"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--dataset",
            required=True,
            choices=["ecomretrieval", "multihoprag", "enterpriserag"],
        )
        command_parser.add_argument("--run-id", default=None, help="运行标识；prepare 未提供时自动生成。")
        if command == "prepare":
            command_parser.add_argument("--profile", choices=["smoke", "full"], default="smoke")
            command_parser.add_argument("--language", choices=["en", "zh"], default="en")
            command_parser.add_argument("--corpus", choices=["representative", "challenge"], default="representative")
            command_parser.add_argument("--mode", choices=["retrieval", "rag"], default="rag")
            command_parser.add_argument(
                "--chunking-strategy",
                choices=[DEFAULT_CHUNKING_STRATEGY, STRUCTURED_MARKDOWN_CHUNKING_STRATEGY],
                default=DEFAULT_CHUNKING_STRATEGY,
                help="仅 EnterpriseRAG 评测可显式选择结构化 Markdown 分块；默认保持旧分块。",
            )
            command_parser.add_argument(
                "--rechunk-scope",
                choices=["full", "targeted"],
                default="full",
                help="结构化分块范围；targeted 只重切冻结题目的答案文档，其余文档保持旧策略。",
            )
            command_parser.add_argument(
                "--target-manifest",
                default=None,
                help="targeted scope 使用的冻结结构化分块 manifest。",
            )
            command_parser.add_argument(
                "--document-parse-workers",
                type=int,
                default=None,
                help="Markdown 文档解析并发数，默认由 ENTERPRISE_DOCUMENT_PARSE_WORKERS 控制。",
            )
        elif command == "evaluate":
            command_parser.add_argument(
                "--evaluation-id",
                default=None,
                help="实验标识；提供后在 corpus 下创建独立实验目录，不覆盖其他结果。",
            )
            command_parser.add_argument("--case-set", choices=["all", "analysis", "validation"], default="all")
            command_parser.add_argument("--mode", choices=["retrieval", "rag"], default=None)
            command_parser.add_argument(
                "--changed-variable",
                default=None,
                help="单变量实验唯一允许改变的变量名；基线留空。",
            )
            command_parser.add_argument(
                "--retry-failed-from",
                default=None,
                help="从已有 RAG evaluation 中只补跑缺失或 evaluation_error 题目，并在成功后合并。",
            )
            command_parser.add_argument(
                "--target-manifest",
                default=None,
                help="仅运行已冻结的 analysis target manifest（例如 T9/T10 小范围评测）。",
            )
            command_parser.add_argument(
                "--capture-candidate-trace",
                action="store_true",
                help="在受控 target 实验中保存原始候选和阶段快照。",
            )
            command_parser.add_argument(
                "--workers",
                type=int,
                default=1,
                help="并发评测 worker 数；仅改变执行吞吐，默认 1。",
            )
            command_parser.add_argument(
                "--subquestion-language-policy",
                choices=["legacy", "preserve_input_language_v1"],
                default="legacy",
                help="复杂题拆分后的小问题语言策略；默认保持既有行为。",
            )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_id = args.run_id or (default_run_id() if args.command == "prepare" else None)
    if not run_id:
        raise ValueError("evaluate 和 cleanup 必须提供 --run-id")
    if args.command == "prepare":
        output = prepare_run(
            dataset=args.dataset,
            run_id=run_id,
            profile=args.profile,
            language=args.language,
            corpus=args.corpus,
            evaluation_mode=args.mode,
            chunking_strategy=args.chunking_strategy,
            rechunk_scope=args.rechunk_scope,
            target_manifest_path=Path(args.target_manifest) if args.target_manifest else None,
            document_parse_workers=args.document_parse_workers,
        )
    elif args.command == "evaluate":
        output = evaluate_run(
            dataset=args.dataset,
            run_id=run_id,
            evaluation_id=args.evaluation_id,
            case_set=args.case_set,
            evaluation_mode=args.mode,
            changed_variable=args.changed_variable,
            retry_from_evaluation_id=args.retry_failed_from,
            target_manifest_path=Path(args.target_manifest) if args.target_manifest else None,
            capture_candidate_trace=bool(args.capture_candidate_trace),
            evaluation_worker_count=args.workers,
            subquestion_language_policy=args.subquestion_language_policy,
        )
    else:
        result = cleanup_run(dataset=args.dataset, run_id=run_id)
        print(result)
        return 0
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"评测失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
