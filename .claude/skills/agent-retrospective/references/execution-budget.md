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
observer_wave_concurrency_model: fan_out_fan_in_all_terminal
```

- `observer_parallelism: 3` -- 同時に起動しうる observer 数の上限（`EXPECTED_OBSERVER_MANIFEST` の固定
  3 件と一致）。Issue #2646 以降、`run_retrospective.py` の `run_observer_wave()` 自体が required
  observer 全件を fan-out（互いの completion を待たずに `ThreadPoolExecutor` へ同時 submit）し、全件が
  terminal になるまで fan-in（all-terminal barrier）する実装であり、「Python 側は sequential 参照実装で
  実際の並列起動は root Skill 側の責務」という記述はもはや正しくない（旧記述はこの節の下で置き換え済み）
- `schema_repair_retries: 1` -- `parse_agent_output_with_repair` の `max_retries` 既定値。超過すると
  `SchemaRepairExhausted` を送出し、evaluator は起動しない
- `evaluator_retries: 0` -- evaluator 呼び出しは再試行しない（`run_evaluation` は 1 回のみ `invoke_evaluator`
  を呼ぶ）
- `partial_agent_output: reject` -- 一部の observer が成功しても、全 observer 成功前は evaluator を
  起動しない。`run_observer_wave()` は required observer 全件を fan-out/fan-in（all-terminal）で回収した
  上で、1 件でも失敗があれば evaluator を起動せず fail-closed で終了する（1 件失敗時は既存の granular
  `reason_code` を維持した単一の例外を、2 件以上同時失敗時は `reason_code: observer_wave_multiple_failures`
  を持つ集約例外を送出する -- 単一の「最初の失敗理由」で他の失敗を隠さない）
- `api_error_with_partial_text: reject_as_evidence` -- `invoke_agent` は `is_error` を含む応答を
  `partial_result` として扱い、`run_observer_wave`/`run_evaluation` は non-`ok` status を常に失敗として
  扱う（`api_error_with_partial_text` の内容が finding evidence として採用されることはない）
- `timeout_status` / `interruption_status` -- `AgentInvocationResult.status` の `timeout`/`terminated`
  はいずれも typed operational failure として扱われ、プログラマバグ（`KeyError`/`AssertionError` 等）と
  混同されない（`collect_snapshot.py` の既存規約と同じ方針）
- `cleanup_required: true` -- `run_scoped_temp_dir` が success/exception/SIGINT/SIGTERM の全経路で
  private temp artifact ディレクトリ（mode `0700`）を削除する。Issue #2646 以降、SIGINT/SIGTERM ハンドラは
  cleanup（`shutil.rmtree`）の前に、起動済みの全 observer 子プロセスへ終了要求を出し（`terminate()`）、
  有限の猶予後に必要なら `kill()` へ escalate し、全ての子プロセスが実際に reap されたことを確認してから
  ``RunInterrupted`` を送出する（`terminate_all_active_child_processes()`）。順序は常に「終了要求 → 猶予
  → (必要なら) kill → reap 確認 → 例外伝播 → temp dir cleanup」であり、逆順にはならない
- `observer_wave_concurrency_model: fan_out_fan_in_all_terminal` -- `run_observer_wave()` は required
  observer 全件を、先に dispatch した observer の completion を待たずに同時 dispatch（fan-out）し、
  全件が terminal になるまで待つ（fan-in）。1 件の通常失敗（malformed/schema 不一致/nonzero exit 等）は
  他 observer の dispatch/completion を止めない。observer 自身の timeout はその observer の実
  subprocess を terminate → 有限猶予 → 必要なら kill → reap してから terminal record を確定し、他
  observer は継続する（`_terminate_and_reap_process()`）。evaluator は、required observer 全件が
  terminal かつ全件 successful + schema-valid のときにのみ、fan-in 完了後に正確に 1 回だけ起動される

`.claude/agents/retrospective-runtime-observer.md` / `.claude/agents/retrospective-evaluator.md` の
frontmatter に固定された具体値:

| SubAgent | maxTurns | tools | model |
|---|---|---|---|
| `retrospective-runtime-observer` | 6 | `[]`（no tool） | haiku |
| `retrospective-evaluator` | 8 | `[]`（no tool） | sonnet |

両者とも `mcpServers`/`hooks`/`memory` は不使用（frontmatter に宣言しない）。

## Latitude CLI の収集予算（Collection Budget、Issue #2375 の課題）

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
  `run_retrospective.execute_run()` は `session_id` を解決できない場合も CLI を起動しない
  （collector 自身が起動前に `session_id_unresolved`/`project_slug_unresolved` を返すため、
  無条件の 1 回起動ではなく「起動する場合のみ最大 1 回」）。
- timeout（10秒）・output size（64 KiB、stdout/stderr 個別）を超過した場合は raw output を
  保持せず `availability: error` / `reason_code: budget_exceeded` に正規化する。
- allowlisted metric は `trace_count`/`span_count`/`duration_ms` の 3 個で固定。CLI の応答
  （`{items: [...], nextCursor, hasMore}`）に他のフィールドが含まれていても、この 3 個以外は
  読み取り直後に破棄する。`--limit 1` で常に 1 trace 以下に絞るため、`span_count`/
  `duration_ms` は複数 trace を集約しない（該当 trace が 0 件なら
  `reason_code: no_matching_trace` で `unavailable`）。
- CLI Boundary（read-only・argv-only・no shell/stdin prompt）は
  `references/wire-contract.md`（`latitude_runtime_evidence/v1` セクション）を参照。
