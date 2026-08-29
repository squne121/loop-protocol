# 実行 budget・retry・cancel・partial-result policy（Issue #2237 P1-2）

```yaml
observer_parallelism: 3
schema_repair_retries: 1
evaluator_retries: 0
partial_agent_output: reject
api_error_with_partial_text: reject_as_evidence
timeout_status: typed operational failure
interruption_status: aborted
cleanup_required: true
```

- `observer_parallelism: 3` -- root Skill が同時起動してよい observer 数の上限（`run_retrospective.py`
  自体は sequential reference 実装であり、実際の並列起動は root Skill の `Agent` tool 呼び出し側の責務）
- `schema_repair_retries: 1` -- `parse_agent_output_with_repair` の `max_retries` 既定値。超過すると
  `SchemaRepairExhausted` を送出し、evaluator は起動しない
- `evaluator_retries: 0` -- evaluator 呼び出しは再試行しない（`run_evaluation` は 1 回のみ `invoke_evaluator`
  を呼ぶ）
- `partial_agent_output: reject` -- 一部の observer が成功しても、全 observer 成功前は evaluator を
  起動しない（`run_observer_wave` は最初の失敗で `ObserverWaveFailed` を送出し即座に停止する）
- `api_error_with_partial_text: reject_as_evidence` -- `invoke_agent` は `is_error` を含む応答を
  `partial_result` として扱い、`run_observer_wave`/`run_evaluation` は non-`ok` status を常に失敗として
  扱う（`api_error_with_partial_text` の内容が finding evidence として採用されることはない）
- `timeout_status` / `interruption_status` -- `AgentInvocationResult.status` の `timeout`/`terminated`
  はいずれも typed operational failure として扱われ、プログラマバグ（`KeyError`/`AssertionError` 等）と
  混同されない（`collect_snapshot.py` の既存規約と同じ方針）
- `cleanup_required: true` -- `run_scoped_temp_dir` が success/exception/SIGINT/SIGTERM の全経路で
  private temp artifact ディレクトリ（mode `0700`）を削除する

`.claude/agents/retrospective-runtime-observer.md` / `.claude/agents/retrospective-evaluator.md` の
frontmatter に固定された具体値:

| SubAgent | maxTurns | tools | model |
|---|---|---|---|
| `retrospective-runtime-observer` | 6 | `[]`（no tool） | haiku |
| `retrospective-evaluator` | 8 | `[]`（no tool） | sonnet |

両者とも `mcpServers`/`hooks`/`memory` は不使用（frontmatter に宣言しない）。

## Latitude CLI Collection Budget（Issue #2375）

```yaml
latitude_max_launches_per_run: 1
latitude_timeout_seconds: 10
latitude_max_output_bytes: 65536   # 64 KiB, stdout/stderr 個別に適用
latitude_max_allowlisted_metrics: 3   # trace_count / span_count / duration_ms
latitude_pagination: prohibited
latitude_retry_loop: prohibited
latitude_background_polling: prohibited
```

- `collect_snapshot.collect_latitude_runtime_evidence()` は 1 回の呼び出しにつき `latitude` CLI
  を最大 1 回だけ起動する（内部に retry loop を持たない -- 予算超過は呼び出し側の責務）。
- timeout（10秒）・output size（64 KiB、stdout/stderr 個別）を超過した場合は raw output を
  保持せず `availability: error` / `reason_code: budget_exceeded` に正規化する。
- allowlisted metric は `trace_count`/`span_count`/`duration_ms` の 3 個で固定。CLI の応答に
  他のフィールドが含まれていても、この 3 個以外は読み取り直後に破棄する。
- CLI Boundary（read-only・argv-only・no shell/stdin prompt）は
  `references/wire-contract.md`（`latitude_runtime_evidence/v1` セクション）を参照。
