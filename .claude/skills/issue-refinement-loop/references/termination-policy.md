# Termination Policy

## Loop end conditions

| condition | termination_reason |
|---|---|
| Step 2 returns `approve` | `approved` |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` | `needs_second_pass` |
| Any step requires human review | `human_escalation` |
| `final_classification == superseded_by_decision` and close / replacement flow completed | `superseded_by_decision` |

## NEEDS_SECOND_PASS escape hatch

`iteration` は完了した review cycle 数を表す。`max_iterations=1` 既定では、最初の `needs-fix` で Step 4 を自動実行せず停止する。次パスを回すには明示的な iteration 追加または人間判断が必要。

## Additional stop rules

- anchor comment fact-check が未完了のまま stale approval を使おうとした場合
- scope change signal が新規追加された場合
- required external research が critical claim を unresolved のまま残した場合

## Must not

- `approve` 以外を success 扱いして silently finish しない
- `max_iterations` を超えて自動ループしない
