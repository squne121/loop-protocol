# Termination Policy

## Loop end conditions

| condition | termination_reason |
|---|---|
| Step 2 returns `approve` | `approved` |
| Step 2 returns `needs-fix` and `iteration + 1 < max_iterations` | continue to next iteration |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` | `human_escalation` (with full blocker summary) |
| Any step requires human review | `human_escalation` |
| `final_classification == superseded_by_decision` and close / replacement flow completed | `superseded_by_decision` |

## Human Escalation on max_iterations

`iteration + 1 >= max_iterations` かつ approve なしの場合は `human_escalation` で停止し、全 iteration 分の blocker summary を終了コメントに添付する。`max_iterations=3` 既定では、3 回目の `needs-fix` で停止する。

## Additional stop rules

- anchor comment fact-check が未完了のまま stale approval を使おうとした場合
- scope change signal が新規追加された場合
- required external research が critical claim を unresolved のまま残した場合

## Must not

- `approve` 以外を success 扱いして silently finish しない
- `max_iterations` を超えて自動ループしない
- hard stop 条件（`state/needs-human`、scope change 等）をスキップしない

## Termination Result Schema（LOOP_TERMINATION_RESULT_V1）

`human_escalation` 終了時は以下の構造で終了コメントを出力する:

```yaml
LOOP_TERMINATION_RESULT_V1:
  termination_reason: human_escalation
  max_iterations: 3
  blockers_history:
    - iteration: 0
      blockers: []
    - iteration: 1
      blockers: []
    - iteration: 2
      blockers: []
```

## Termination Comment（全 termination reason 共通）

すべての termination reason（`approved` / `human_escalation` / `superseded_by_decision`）で、終了コメントに `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含める。follow-up が存在しない場合も空配列で出力する（`follow_up_issues: []` / `note_only_observations: []`）。

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues: []   # 起票済み / reuse / skip 結果。空の場合も省略しない
  note_only_observations: []  # 起票せず記録のみ。空の場合も省略しない
```

詳細 schema は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を参照。`issue-refinement-loop` は thin orchestrator として raw context を保持せず、materialization 結果のみを報告する（`docs/dev/agent-skill-boundaries.md` の `ORCHESTRATOR_IO_BOUNDARY_V1` 参照）。

## Loop Policy（LOOP_POLICY_V1）

```yaml
LOOP_POLICY_V1:
  max_iterations_default: 3
  loop_iteration_approval_gate:
    default_required: false
    scope: repo_loop_iteration_only
    does_not_control:
      - Claude Code permissions.defaultMode
      - bypassPermissions
      - --dangerously-skip-permissions
      - --allow-dangerously-skip-permissions
      - --permission-mode
      - hooks PermissionRequest auto-approval
  routes:
    - when: hard_stop_triggered
      action: human_escalation
    - when: verdict == "approve"
      action: done
    - when: "verdict == 'needs-fix' and iteration_plus_one < max_iterations"
      action: continue
    - when: "verdict == 'needs-fix' and iteration_plus_one >= max_iterations"
      action: human_escalation
  hard_stops:
    - state/needs-human
    - state/done
    - scope_change_signal
    - contract_malformation
    - required_external_research_unresolved
    - unsafe_mutation
```
