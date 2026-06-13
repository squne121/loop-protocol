# Verdict コメントテンプレート（要点）

```markdown
## Verdict: APPROVE | REQUEST_CHANGES

### Mergeability
- mergeable=<...>, merge_state_status=<...>

### Evidence Check
- AC coverage: ...
- Allowed Paths: ...
- CI Verification: ...
- 検証コマンド結果: ...

### Blockers
- なし / ...

### Non-blockers
- なし / ...

## LOOP_VERDICT_V2
```yaml
verdict: APPROVE | REQUEST_CHANGES
reviewed_head_sha: <sha>
merge_ready: false
mergeability:
  mergeable: ...
  merge_state_status: ...
blockers: []
required_auto_actions: []
auto_fix_applied: []
follow_up_issue_requests:
  - ...
```
```
