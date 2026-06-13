# LOOP_VERDICT_V2 Schema（要点）

```yaml
LOOP_VERDICT_V2:
  verdict: APPROVE | REQUEST_CHANGES
  reviewed_head_sha: <SHA>
  merge_ready: true | false
  mergeability:
    mergeable: MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  blockers: []
  required_auto_actions:
    - kind: ensure_closing_keyword | update_branch | update_pr_body_hygiene
      executor: implementation-worker
      skill: open-pr.update_pr | implement-issue.update_branch
      blocking_merge_ready: true
      mechanical: true
      expected_head_sha: <sha>   # update_branch のみ
  auto_fix_applied: []
  follow_up_issue_requests: []
  allowed_paths_gate:
    status: ok | fail_closed | stale_snapshot | indeterminate
  allowed_paths_gate_source: review_subagent
  required_auto_actions: []
```

## 制約

- snake_case 専用（`mergeStateStatus`/`recommendations` など V1 フィールド禁止）
- `merge_ready` は `verdict==APPROVE` かつ blockers 空 かつ `required_auto_actions` 空 かつ mergeability が clean 条件を満たす場合のみ true
- `follow_up_issue_requests` は起票リクエスト（materialize 結果ではない）
