# Quality Guidelines

> Code quality standards for backend development.

## Overview

Evaluation code is an offline backend boundary: it may write only an explicitly
named evaluation collection and run directory. It must never use the default
business collection as a fallback.

Failure-classification reports must keep model-quality uncertainty separate from
evaluation-chain failures. A `review` verdict without an API, generation, or
grader error is `human_review`; `evaluation_error`, answer-generation errors,
and grader request/parse errors are `system_error`. These labels are automatic
triage only and require human confirmation before an optimization decision.

Evidence grading can fail closed before the independent grader returns an error:
the RAG trace records this as `rag_trace.evidence_reason ==
"evidence_grading_unavailable"`. Classification must treat that trace reason as
`system_error` and must not also add `answer_failure`, because the answer was
never evaluated under usable evidence-grading conditions.

## Forbidden Patterns

Do not overwrite a prepared corpus or an experiment configuration. A repeated
`evaluation_id` may resume its checkpoint only when its immutable source hashes,
case set, mode, and `changed_variable` match the existing
`evaluation-config.json`.

## Required Patterns

Formal EnterpriseRAG runs must use the frozen `case-split.json` and its SHA-256
reference. `analysis` and `validation` are disjoint; automatic reports suppress
validation case details, and the manual-review artifacts retain only redacted
validation queue markers (no IDs, questions, answers, evidence, or failure
reasons). The controlled `results.jsonl` remains the complete machine audit
source.
Rerank is a comparison strategy only when every case in that experiment succeeds.

### Common Mistake: Double-counting evidence-grading failures

**Symptom**: A case with `evidence_grading_unavailable` appears in both
`system_error` and `answer_failure`, inflating the apparent answer-quality loss.

**Cause**: The classifier checks only `evaluation_error`, answer-generation
errors, and `grader_error`, while the fail-closed evidence route reports its
failure through `rag_trace.evidence_reason`.

**Correct handling**:

```python
evidence_grading_error = (
    (record.get("rag_trace") or {}).get("evidence_reason")
    == "evidence_grading_unavailable"
)
has_system_error = bool(evaluation_error or answer_error or grader_error or evidence_grading_error)
if verdict == "fail" and not has_system_error:
    categories.add("answer_failure")
```

Keep these categories mutually exclusive for classification purposes; human
review may still record the case separately when the underlying answer is
otherwise inspectable.

## Testing Requirements

For changes to evaluation contracts, add mock tests for split quotas, hash
validation, experiment reuse, config immutability, validation detail suppression,
Rerank all-or-nothing inclusion, and idempotent cleanup. Run:

```powershell
uv run python -m unittest discover -s tests
uv run python -m py_compile backend/evaluation/*.py scripts/run_rag_evaluation.py
git diff --check
```

## Code Review Checklist

### 1. Scope / Trigger

This contract applies when changing `backend/evaluation/runner.py`,
`backend/evaluation/datasets.py`, or `scripts/run_rag_evaluation.py`, especially
when a new experiment mode or persistent artifact is added.

### 2. Signatures

```python
evaluate_run(
    *, dataset, run_id, evaluation_id=None,
    case_set="all", evaluation_mode=None, changed_variable=None
) -> Path
```

CLI evaluation accepts `--evaluation-id`, `--case-set {all,analysis,validation}`,
`--mode {retrieval,rag}`, and `--changed-variable`.

### 3. Contracts

An experiment snapshot records `source_corpus_manifest_sha256`,
`source_case_split_sha256`, `case_set`, `evaluation_mode`, `changed_variable`,
model identifiers, retrieval settings, and `code_version`. Corpus artifacts stay
at the corpus run root; experiment artifacts are under `evaluations/<id>/`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| corpus not prepared or already cleaned | reject evaluation |
| analysis/validation requested without a frozen split | reject evaluation |
| split hash differs from corpus manifest | reject evaluation |
| existing evaluation ID has immutable config differences | reject overwrite |
| Rerank fails for any case | omit Rerank from comparison metrics, retain diagnostics |

### 5. Good/Base/Bad Cases

- Good: prepare once, run retrieval and RAG with distinct IDs against the same collection.
- Base: legacy `evaluate --run-id` continues to read the corpus-root artifacts.
- Bad: create a second collection or rewrite `config.json` for each parameter trial.

### 6. Tests Required

Assert the exact `300/200` counts and ten type quotas; assert experiment config
hashes and collection reuse; assert validation IDs are not passed to an analysis
evaluator; assert validation failures produce redacted queue markers (without
validation IDs or details) and are absent from the automatic report.

### 7. Wrong vs Correct

#### Wrong

```python
_write_json(run_directory(dataset, run_id) / "config.json", changed_config)
```

#### Correct

```python
evaluation_directory(dataset, corpus_run_id, evaluation_id)
# Save evaluation-config.json once; resume only when immutable fields match.
```

## Scenario: Per-Case Reports and Remote Embeddings

### 1. Scope / Trigger

This contract applies when an evaluation needs human-readable question-level
evidence or when the dense embedding provider is changed through `.env`.

### 2. Signatures

```python
_write_case_review_report(
    output_dir, records, *, dataset, evaluation_mode,
    suppress_validation_details=False
) -> Path
```

### 3. Contracts

- Every evaluation writes `results.jsonl` as the complete machine-readable
  checkpoint and `case-review.md` as the human-readable question-level view.
- RAG case views include question, reference answer, model answer, expected and
  retrieved evidence, evidence ranks, coverage, automatic grade, reason, human
  review status, and timing.
- Retrieval case views group all strategies under one question and show evidence
  ranks and retrieved filenames per strategy.
- When a frozen formal split is used, validation details are omitted from
  `report.md` and `case-review.md`; the complete audit data remains in JSONL.
- `EMBEDDING_PROVIDER` accepts `huggingface` (default) or `siliconflow`.
  SiliconFlow reads `EMBEDDING_API_KEY` or `SILICONFLOW_API_KEY`,
  `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_BATCH_SIZE`, and
  `EMBEDDING_TIMEOUT_SECONDS`. API keys must never enter config snapshots.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Unsupported embedding provider | Raise a configuration error before creating the embedder |
| SiliconFlow provider without an API key | Raise a clear configuration error; do not silently fall back to local model |
| Validation case in a frozen automatic or manual-review artifact | Hide question/answer/failure details; retain a redacted queue marker and the full details only in controlled `results.jsonl` |
| Missing answer, evidence, or grade field | Render an explicit empty marker; do not crash report generation |

### 5. Good/Base/Bad Cases

- Good: read `case-review.md` to inspect an analysis question, then follow its
  case ID to `manual-review.md` or `results.jsonl` for the full trace. Validation
  queue entries remain redacted until an authorized blind reviewer handles them.
- Base: smoke runs expose all question details because they have no blind split.
- Bad: replace `results.jsonl` with a shortened Markdown-only record or write an
  API key into `evaluation-config.json`.

### 6. Tests Required

- Assert a RAG case report contains the question, reference answer, model
  answer, evidence filename, grade, and reason.
- Assert retrieval strategies are grouped under one question.
- Assert validation case IDs are absent from the automatic case report.
- Mock SiliconFlow construction and assert provider, endpoint, batch size,
  timeout, API-key requirement, and secret omission.

### 7. Wrong vs Correct

#### Wrong

```python
# Only write aggregate metrics; the user cannot inspect a failed question.
_write_json(output_dir / "summary.json", summary)
```

#### Correct

```python
_write_results(output_dir / "results.jsonl", results)
_write_case_review_report(output_dir, results, dataset=dataset, evaluation_mode=mode)
```

## Scenario: Timeout Recovery for RAG Evaluation Workers

### 1. Scope / Trigger

This contract applies to `backend/evaluation/runner.py` when a formal RAG
evaluation uses a spawned process to enforce the total per-case timeout.

### 2. Signatures

```python
def _stop_multihop_worker(worker: _MultiHopWorker | None) -> None: ...
def _run_case_in_worker(worker: _MultiHopWorker, case: dict[str, Any], timeout_seconds: float) -> dict[str, Any]: ...
```

### 3. Contracts

- A timed-out or failed worker must release both request/result queue feeder
  threads and the process handle before the next worker starts.
- A worker that exits before writing a result raises an error containing its
  `exitcode`; the resulting JSONL record keeps that error in
  `evaluation_error` and remains eligible for manual review.
- A case that returns `evaluation_error` also retires its worker before the
  next case, because an in-process API client can remain broken even though
  the worker itself has not exited.
- Resource-recovery changes do not alter the corpus manifest, retrieval
  configuration, model settings, or the default business collection.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Worker finishes normally | Send sentinel, join, close queues and process handle. |
| Worker exceeds per-case deadline | Terminate, join, close/join both queues, then checkpoint a review record. |
| Worker exits before result | Include `exitcode` in the raised error and checkpoint it as `evaluation_error`. |
| Case returns `evaluation_error` | Checkpoint that case once, retire the worker, and start a clean worker for the next case. |
| Queue cleanup throws or is skipped | Do not start another worker until the cleanup path completes; otherwise repeated restarts can turn one timeout into broad false failures. |

### 5. Good/Base/Bad Cases

- Good: after one case timeout, the next case can use a newly spawned worker
  and its record contains real RAG timings.
- Base: a single model timeout remains a `review` record with its case ID and
  error preserved.
- Bad: continue recording empty answers after the worker has exited, then
  report the resulting zero-second records as retrieval or answer metrics.

### 6. Tests Required

- Mock a dead worker and assert `_run_case_in_worker` includes its exact
  `exitcode` in the exception.
- Mock a stopped worker and assert both queues call `close()` and
  `join_thread()`, while the process calls `close()`.
- Mock two consecutive `evaluation_error` records and assert each is executed
  by a separately started worker.
- Keep the existing single-case-error continuation test to ensure a recoverable
  case error does not discard later checkpoints.

### 7. Wrong vs Correct

#### Wrong

```python
worker.request_queue.close()
worker.result_queue.close()
# Feeder threads and process handles can remain alive after a timeout.
```

#### Correct

```python
for worker_queue in (worker.request_queue, worker.result_queue):
    worker_queue.close()
    worker_queue.join_thread()
if not worker.process.is_alive():
    worker.process.close()
```

#### Wrong

```python
record = _run_case_in_worker(worker, case, timeout_seconds)
# A returned APIConnectionError leaves the unhealthy worker in service.
```

#### Correct

```python
record = _run_case_in_worker(worker, case, timeout_seconds)
if record.get("evaluation_error"):
    _stop_multihop_worker(worker)
    worker = None
```

## Scenario: Retry Evaluation Completion State

### 1. Scope / Trigger

This contract applies when `evaluate_run(..., retry_from_evaluation_id=...)`
combines preserved records with a new `attempt-results.jsonl`.

### 2. Signatures

```python
evaluate_run(
    *, dataset, run_id, evaluation_id,
    case_set="all", evaluation_mode="rag",
    retry_from_evaluation_id,
) -> Path
```

### 3. Contracts

- `attempt_summary["evaluation_status"]` describes only the selected retry
  attempt; it is not the status of the logical 500-case evaluation.
- Experiment snapshots record both `model_timeout_seconds` and
  `evaluation_case_timeout_seconds`.
- A retry may change only `evaluation_case_timeout_seconds` when
  `changed_variable` is exactly `"evaluation_case_timeout_seconds"`; model,
  retrieval, corpus, and case-set inputs must still match the source.
- If any formal case is missing or has `evaluation_error`, both
  `summary.json["evaluation_status"]` and
  `evaluation-progress.json["status"]` are `"interrupted"`.
- An interrupted retry writes its attempt, review queue, and incomplete report,
  but must not write a merged `results.jsonl`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Every logical case has a non-error record | Write merged `results.jsonl`; overall status is `completed`. |
| Retry attempt reaches its end but has a timeout/API error | Overall status remains `interrupted`; retain the error for the next retry. |
| Retry attempt stops before all selected cases | Overall status is `interrupted`; missing IDs remain eligible for retry. |
| Retry changes a non-timeout input | Reject the retry as incompatible; do not mix records. |

### 5. Good/Base/Bad Cases

- Good: a 48-case repair attempt has 48 successful records and produces one
  500-case merged result.
- Base: a repair attempt finishes all selected calls but one has a timeout; its
  own status may be complete, while the logical evaluation remains interrupted.
- Bad: spread `**attempt_summary` after an explicit overall status and silently
  overwrite `interrupted` with `completed`.

### 6. Tests Required

Mock a retry whose selected case still has `evaluation_error`, then assert the
summary and progress statuses are both `interrupted`, the unresolved count is
nonzero, and `results.jsonl` does not exist.

### 7. Wrong vs Correct

#### Wrong

```python
summary = {"evaluation_status": "interrupted", **attempt_summary}
```

#### Correct

```python
summary = {**attempt_summary, "evaluation_status": "interrupted"}
```

## Scenario: Rewrite Candidate Fusion Attribution

### 1. Scope / Trigger

This contract applies to the opt-in T9 rewrite-candidate fusion path in
`backend/rag/pipeline.py`, `backend/rag/utils.py`, and
`backend/evaluation/runner.py`. It prevents a targeted rerun from treating
ordinary model or routing variance as a retrieval optimization gain.

### 2. Signatures

```python
RetrievalRuntime(enable_rewrite_candidate_fusion: bool = False)
fuse_rewrite_candidate_results(
    *, original_query, initial_retrieval, rewritten_retrieval,
    top_k, runtime, fallback_reason=None
) -> dict
evaluate_run(
    *, target_manifest_path=None, changed_variable="rewrite_candidate_fusion"
) -> Path
```

### 3. Contracts

- Fusion is disabled by default and is only evaluated when the existing
  rewrite branch actually runs.
- Raw candidates are merged by stable `chunk_id` (Milvus `id` is the fallback);
  Auto-merging, Rerank, and final top-k are applied once to the union.
- Final Rerank uses the original user question, not HyDE text.
- Trace fields record whether fusion was applied, candidate counts, source
  labels, and fallback reason.
- A targeted result may claim fusion evidence gain only for cases whose trace
  contains `rewrite_candidate_fusion_applied=true`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Target manifest includes validation or duplicate IDs | Reject before evaluation |
| Target manifest hash or source result hash differs | Reject before evaluation |
| Target manifest used without `changed_variable=rewrite_candidate_fusion` | Reject |
| Rerun does not enter the rewrite branch | Mark the case not attributable to fusion |
| Rewrite/fusion retrieval fails | Keep first finalized evidence and record fallback |

### 5. Good/Base/Bad Cases

- Good: a case triggers fusion, evidence coverage changes, and the report
  attributes the change only to that case.
- Base: a targeted rerun takes a different route and improves coverage; record
  the change but exclude it from the fusion gain count.
- Bad: compare aggregate targeted metrics without checking the per-case fusion
  trace and start a 300-case experiment based on unrelated rerun variance.

### 6. Tests Required

- Assert the default runtime flag is false.
- Assert candidate deduplication, original-query Rerank, stable top-k, and
  fallback preservation.
- Assert target manifests reject validation IDs, duplicate IDs, bad hashes, and
  the wrong changed variable.
- Assert reports distinguish actual fusion cases from reruns where fusion did
  not execute.

### 7. Wrong vs Correct

#### Wrong

```python
if t9_coverage > baseline_coverage:
    fusion_gain += 1
```

#### Correct

```python
if trace.get("rewrite_candidate_fusion_applied") and t9_coverage > baseline_coverage:
    fusion_gain += 1
```
