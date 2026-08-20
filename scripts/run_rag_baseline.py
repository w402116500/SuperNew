"""Run an isolated-session RAG baseline and preserve raw responses for review."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AnswerJudgeConfig:
    """Connection settings for the independent answer grader."""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: int


def parse_cases(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL evaluation set and reject malformed case records early."""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not case.get("id") or not case.get("question"):
            raise ValueError(f"Case on line {line_number} must include id and question")
        cases.append(case)
    if not cases:
        raise ValueError(f"No evaluation cases found in {path}")
    return cases


def source_filenames(trace: dict[str, Any] | None) -> list[str]:
    """Return retrieved file names in trace order without duplicate entries."""
    filenames: list[str] = []
    for chunk in (trace or {}).get("retrieved_chunks") or []:
        filename = chunk.get("filename") if isinstance(chunk, dict) else None
        if filename and filename not in filenames:
            filenames.append(filename)
    return filenames


def expected_doc_names(case: dict[str, Any]) -> list[str]:
    """Normalize fixture-relative source paths to the document names returned by the API."""
    return [Path(value).name for value in case.get("expected_docs") or []]


def expected_doc_ranks(case: dict[str, Any], trace: dict[str, Any] | None) -> dict[str, int | None]:
    """Report the 1-based retrieved rank for every expected document."""
    retrieved = source_filenames(trace)
    return {
        name: retrieved.index(name) + 1 if name in retrieved else None
        for name in expected_doc_names(case)
    }


def _first_json_object(text: str) -> dict[str, Any]:
    """Read the first JSON object from a model response, including fenced JSON."""
    normalized = text.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1] if len(lines) > 1 and lines[-1].strip().startswith("```") else lines[1:])

    start = normalized.find("{")
    if start < 0:
        raise ValueError("grader response does not contain a JSON object")
    payload, _ = json.JSONDecoder().raw_decode(normalized[start:])
    if not isinstance(payload, dict):
        raise ValueError("grader response JSON must be an object")
    return payload


def _message_content(payload: dict[str, Any]) -> str:
    """Extract text content from an OpenAI-compatible chat completion response."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("grader response does not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, dict))
        )
    raise ValueError("grader response has no text content")


def _boolean(value: Any) -> bool:
    """Accept the common JSON and text forms of a boolean from compatible models."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "是", "有"}
    return False


def normalize_answer_grade(payload: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Normalize untrusted grader JSON into a complete, predictable result record."""
    verdict = str(payload.get("verdict", "review")).strip().lower()
    if verdict not in {"pass", "fail", "review"}:
        verdict = "review"

    raw_fact_results = payload.get("fact_results")
    facts_by_index: dict[int, dict[str, Any]] = {}
    if isinstance(raw_fact_results, list):
        for result in raw_fact_results:
            if not isinstance(result, dict):
                continue
            try:
                index = int(result.get("index"))
            except (TypeError, ValueError):
                continue
            facts_by_index[index] = result

    fact_results: list[dict[str, Any]] = []
    for index, fact in enumerate(case.get("expected_facts") or [], start=1):
        raw_result = facts_by_index.get(index, {})
        status = str(raw_result.get("status", "review")).strip().lower()
        if status not in {"met", "missing", "contradicted", "review"}:
            status = "review"
        fact_results.append({
            "index": index,
            "fact": fact,
            "status": status,
            "reason": str(raw_result.get("reason", "")).strip(),
        })

    raw_forbidden_results = payload.get("forbidden_claim_results")
    forbidden_by_index: dict[int, dict[str, Any]] = {}
    if isinstance(raw_forbidden_results, list):
        for result in raw_forbidden_results:
            if not isinstance(result, dict):
                continue
            try:
                index = int(result.get("index"))
            except (TypeError, ValueError):
                continue
            forbidden_by_index[index] = result

    forbidden_claim_results: list[dict[str, Any]] = []
    for index, claim in enumerate(case.get("must_not_claim") or [], start=1):
        raw_result = forbidden_by_index.get(index, {})
        forbidden_claim_results.append({
            "index": index,
            "claim": claim,
            "mentioned": _boolean(raw_result.get("mentioned")),
            "reason": str(raw_result.get("reason", "")).strip(),
        })

    failed_fact_check = any(result["status"] in {"missing", "contradicted"} for result in fact_results)
    forbidden_claim_present = any(result["mentioned"] for result in forbidden_claim_results)
    reason = str(payload.get("reason", "")).strip()
    if verdict == "pass" and (failed_fact_check or forbidden_claim_present):
        verdict = "fail"
        reason = "阅卷结果与明细冲突，已按缺失事实或禁答项修正为不通过。"

    return {
        "verdict": verdict,
        "fact_results": fact_results,
        "forbidden_claim_results": forbidden_claim_results,
        "reason": reason,
        "grader_error": "",
    }


def _answer_judge_prompt(case: dict[str, Any], answer: str) -> str:
    """Build a constrained prompt so the judge grades against the fixture, not world knowledge."""
    rubric = {
        "question": case.get("question", ""),
        "expected_facts": case.get("expected_facts") or [],
        "reference_answer": case.get("reference_answer", ""),
        "must_not_claim": case.get("must_not_claim") or [],
        "answer_type": case.get("answer_type", ""),
        "assistant_answer": answer,
    }
    return (
        "你是知识库问答的严格阅卷器，不是回答用户的助手。只根据给出的评分标准判卷，"
        "不要引入外部知识。语义等价的表述应视为满足事实；缺少、矛盾或擅自断言均不通过。"
        "对于资料未覆盖的问题，只有明确说明无法从现有资料确认才算通过。\n\n"
        "只输出一个 JSON 对象，字段必须完整：\n"
        "{\"verdict\":\"pass|fail|review\",\"fact_results\":[{\"index\":1,\"status\":\"met|missing|contradicted|review\",\"reason\":\"...\"}],"
        "\"forbidden_claim_results\":[{\"index\":1,\"mentioned\":false,\"reason\":\"...\"}],\"reason\":\"...\"}\n"
        "fact_results 和 forbidden_claim_results 必须分别覆盖输入列表中的每一项，index 从 1 开始。\n\n"
        "评分材料：\n"
        + json.dumps(rubric, ensure_ascii=False)
    )


def grade_answer(
    judge_client: requests.Session | None,
    judge_config: AnswerJudgeConfig | None,
    case: dict[str, Any],
    answer: str,
) -> dict[str, Any]:
    """Ask the configured judge to score one answer and degrade to manual review on errors."""
    if not judge_client or not judge_config:
        return {
            "verdict": "not_run",
            "fact_results": [],
            "forbidden_claim_results": [],
            "reason": "未启用回答判卷。",
            "grader_error": "",
        }

    try:
        response = judge_client.post(
            f"{judge_config.base_url.rstrip('/')}/chat/completions",
            json={
                "model": judge_config.model,
                "temperature": 0,
                "messages": [{"role": "user", "content": _answer_judge_prompt(case, answer)}],
            },
            timeout=judge_config.timeout_seconds,
        )
        response.raise_for_status()
        grade = normalize_answer_grade(_first_json_object(_message_content(response.json())), case)
        grade["grader_model"] = judge_config.model
        return grade
    except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "verdict": "review",
            "fact_results": [],
            "forbidden_claim_results": [],
            "reason": "阅卷器未能给出可解析结果，需要人工复核。",
            "grader_error": str(exc),
            "grader_model": judge_config.model,
        }


def run_case(
    client: requests.Session,
    base_url: str,
    case: dict[str, Any],
    timeout_seconds: int,
    knowledge_filenames: list[str] | None = None,
) -> dict[str, Any]:
    """Submit one case using a fresh session id and keep response diagnostics verbatim."""
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    session_id = f"rag-eval-{case['id']}-{uuid4().hex[:8]}"
    try:
        request_body = {"message": case["question"], "session_id": session_id}
        if knowledge_filenames:
            request_body["knowledge_filenames"] = knowledge_filenames
        response = client.post(
            f"{base_url}/chat",
            json=request_body,
            timeout=timeout_seconds,
        )
        elapsed_seconds = round(time.perf_counter() - started, 3)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {
            "case": case,
            "session_id": session_id,
            "started_at": started_at,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "http_status": None,
            "response": "",
            "rag_trace": None,
            "error": str(exc),
            "retrieved_filenames": [],
            "expected_doc_names": expected_doc_names(case),
            "all_expected_docs_recalled": False,
        }

    trace = payload.get("rag_trace") if isinstance(payload, dict) else None
    retrieved = source_filenames(trace)
    expected = expected_doc_names(case)
    expected_ranks = expected_doc_ranks(case, trace)
    return {
        "case": case,
        "session_id": session_id,
        "started_at": started_at,
        "elapsed_seconds": elapsed_seconds,
        "http_status": response.status_code,
        "response": payload.get("response", "") if isinstance(payload, dict) else "",
        "rag_trace": trace,
        "error": payload.get("detail") if response.status_code >= 400 and isinstance(payload, dict) else None,
        "retrieved_filenames": retrieved,
        "expected_doc_names": expected,
        "expected_doc_ranks": expected_ranks,
        "all_expected_docs_recalled": bool(expected) and all(name in retrieved for name in expected),
    }


def authenticate(client: requests.Session, base_url: str, username: str, password: str, timeout_seconds: int) -> None:
    """Authenticate once and attach the bearer token to the session headers."""
    response = client.post(
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Login succeeded but no access token was returned")
    client.headers.update({"Authorization": f"Bearer {token}"})


def build_summary(records: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
    """Calculate retrieval, answer, and latency metrics from one fixed exam run."""
    successful = [record for record in records if record["http_status"] == 200]
    recalled = [record for record in successful if record["all_expected_docs_recalled"]]
    first_expected_at_one = [
        record
        for record in successful
        if any(rank == 1 for rank in record.get("expected_doc_ranks", {}).values())
    ]
    all_expected_at_two = [
        record
        for record in successful
        if record.get("expected_doc_ranks")
        and all(rank is not None and rank <= 2 for rank in record["expected_doc_ranks"].values())
    ]
    all_expected_at_three = [
        record
        for record in successful
        if record.get("expected_doc_ranks")
        and all(rank is not None and rank <= 3 for rank in record["expected_doc_ranks"].values())
    ]
    judged = [
        record for record in successful
        if record.get("answer_grade", {}).get("verdict") in {"pass", "fail", "review"}
    ]
    answer_passed = [record for record in judged if record["answer_grade"]["verdict"] == "pass"]
    answer_review = [record for record in judged if record["answer_grade"]["verdict"] == "review"]
    fact_results = [
        fact
        for record in judged
        for fact in record.get("answer_grade", {}).get("fact_results", [])
    ]
    forbidden_claim_results = [
        claim
        for record in judged
        for claim in record.get("answer_grade", {}).get("forbidden_claim_results", [])
    ]
    return {
        "cases": len(records),
        "successful_cases": len(successful),
        "failed_cases": len(records) - len(successful),
        "all_expected_docs_recalled": len(recalled),
        "all_expected_docs_recall_rate": round(len(recalled) / len(successful), 4) if successful else 0,
        "first_expected_doc_at_1": len(first_expected_at_one),
        "all_expected_docs_at_2": len(all_expected_at_two),
        "all_expected_docs_at_3": len(all_expected_at_three),
        "answer_judged_cases": len(judged),
        "answer_passed_cases": len(answer_passed),
        "answer_pass_rate": round(len(answer_passed) / len(judged), 4) if judged else None,
        "answer_review_cases": len(answer_review),
        "expected_facts_total": len(fact_results),
        "expected_facts_met": sum(fact["status"] == "met" for fact in fact_results),
        "forbidden_claims_total": len(forbidden_claim_results),
        "forbidden_claim_violations": sum(claim["mentioned"] for claim in forbidden_claim_results),
        "average_elapsed_seconds": round(
            sum(record["elapsed_seconds"] for record in records) / len(records), 3
        ) if records else 0,
        "output": str(output_path),
    }


def _report_cell(value: Any, limit: int = 140) -> str:
    """Keep generated Markdown tables one row per case and easy to scan."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text[:limit] + ("..." if len(text) > limit else "")


def _rank_summary(record: dict[str, Any]) -> str:
    return "；".join(
        f"{name}：#{rank}" if rank is not None else f"{name}：未找到"
        for name, rank in record.get("expected_doc_ranks", {}).items()
    ) or "无"


def build_markdown_report(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    """Create the human-readable scorecard used to review and discuss bad cases."""
    lines = [
        "# 企业知识库智能问答系统 RAG 评测成绩单",
        "",
        "## 汇总",
        "",
        f"- 试题数：{summary['cases']}；接口成功：{summary['successful_cases']}；失败：{summary['failed_cases']}",
        f"- 正确资料全部找到：{summary['all_expected_docs_recalled']}/{summary['successful_cases']}（{summary['all_expected_docs_recall_rate']:.1%}）",
        f"- 回答通过：{summary['answer_passed_cases']}/{summary['answer_judged_cases']}"
        + (f"（{summary['answer_pass_rate']:.1%}）" if summary['answer_pass_rate'] is not None else "（未启用自动阅卷）"),
        f"- 需要人工复核：{summary['answer_review_cases']}；禁答项误答：{summary['forbidden_claim_violations']}/{summary['forbidden_claims_total']}",
        f"- 平均用时：{summary['average_elapsed_seconds']} 秒",
        "",
        "## 逐题结果",
        "",
        "| 题目 | 回答判定 | 正确资料 | 正确资料排名 | 禁答误答 | 用时 | AI 回答摘要 | 判定原因 |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for record in records:
        grade = record.get("answer_grade") or {}
        violations = [
            result["claim"]
            for result in grade.get("forbidden_claim_results", [])
            if result.get("mentioned")
        ]
        lines.append(
            "| "
            + " | ".join([
                _report_cell(record.get("case", {}).get("question")),
                _report_cell(grade.get("verdict", "not_run")),
                "是" if record.get("all_expected_docs_recalled") else "否",
                _report_cell(_rank_summary(record)),
                _report_cell("；".join(violations) or "否"),
                str(record.get("elapsed_seconds", "")),
                _report_cell(record.get("response")),
                _report_cell(grade.get("reason") or record.get("error") or grade.get("grader_error")),
            ])
            + " |"
        )
    return "\n".join(lines) + "\n"


def resolve_answer_judge_config(args: argparse.Namespace) -> AnswerJudgeConfig | None:
    """Load the existing project model settings only when answer grading is requested."""
    if args.answer_grading == "off":
        return None

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv(args.judge_api_key_env, "").strip()
    base_url = (args.judge_base_url or os.getenv("BASE_URL", "")).strip()
    model = (args.judge_model or os.getenv("GRADE_MODEL", "")).strip()
    missing = [name for name, value in ((args.judge_api_key_env, api_key), ("BASE_URL", base_url), ("GRADE_MODEL", model)) if not value]
    if missing:
        raise RuntimeError("回答判卷缺少配置：" + "、".join(missing) + "。可用 --answer-grading off 仅运行检索评测。")
    return AnswerJudgeConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=args.judge_timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local RAG evaluation set")
    parser.add_argument("--cases", type=Path, required=True, help="JSONL evaluation set path")
    parser.add_argument("--base-url", default="http://127.0.0.1:8050")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-env", default="EVAL_PASSWORD")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--knowledge-filenames", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--answer-grading", choices=("llm", "off"), default="llm")
    parser.add_argument("--judge-base-url")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-api-key-env", default="ARK_API_KEY")
    parser.add_argument("--judge-timeout-seconds", type=int, default=60)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cases_path = args.cases.resolve()
    cases = parse_cases(cases_path)
    password = os.getenv(args.password_env) or getpass.getpass(f"Password for {args.username}: ")
    if not password:
        raise ValueError("A non-empty password is required")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = (args.output or PROJECT_ROOT / "output" / "rag-evaluations" / f"baseline-{timestamp}.jsonl").resolve()
    base_url = args.base_url.rstrip("/")

    client = requests.Session()
    authenticate(client, base_url, args.username, password, args.timeout_seconds)
    judge_config = resolve_answer_judge_config(args)
    judge_client = requests.Session() if judge_config else None
    if judge_client and judge_config:
        judge_client.headers.update({"Authorization": f"Bearer {judge_config.api_key}"})
    results: list[dict[str, Any]] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        for index, case in enumerate(cases, start=1):
            result = run_case(
                client,
                base_url,
                case,
                args.timeout_seconds,
                args.knowledge_filenames,
            )
            result["answer_grade"] = grade_answer(
                judge_client,
                judge_config,
                case,
                result["response"],
            )
            results.append(result)
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(
                f"[{index}/{len(cases)}] {case['id']}: {result['http_status']}"
                f" | 回答：{result['answer_grade']['verdict']}",
                flush=True,
            )

    summary = build_summary(results, output_path)
    summary_path = output_path.with_suffix(".summary.json")
    report_path = output_path.with_suffix(".report.md")
    summary["summary_output"] = str(summary_path)
    summary["report_output"] = str(report_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_markdown_report(results, summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not summary["failed_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
