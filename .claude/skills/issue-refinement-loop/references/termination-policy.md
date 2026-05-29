# Termination Policy

## Loop end conditions

| condition | termination_reason |
|---|---|
| Step 2 returns `approve` | `approved` |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` and `--no-approval` not set | `needs_second_pass` |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` and `--no-approval` set and `blocker_class: requires_human` | `needs_second_pass` |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` and `--no-approval` set and `blocker_class: auto_fixable_structural` | auto-continue to second iteration |
| Any step requires human review | `human_escalation` |
| `final_classification == superseded_by_decision` and close / replacement flow completed | `superseded_by_decision` |

## NEEDS_SECOND_PASS escape hatch

`iteration` は完了した review cycle 数を表す。`max_iterations=1` 既定では、最初の `needs-fix` で Step 4 を自動実行せず停止する。次パスを回すには明示的な iteration 追加または人間判断が必要。

## --no-approval auto-continuation（blocker_class: auto_fixable_structural）

`--no-approval` フラグが設定されており、かつ `blocker_class` が `auto_fixable_structural` のみの場合は、`max_iterations` を超えていても第2イテレーションへ自動継続する。

### auto_fixable_structural とみなす blocker の種類

| blocker_id | 説明 |
|---|---|
| `missing_machine_readable_contract` | `## Machine-Readable Contract` セクション欠落（YAML フロントマター生成可能） |
| `missing_stop_conditions` | `## Stop Conditions` セクション欠落（issue_kind / change_kind から定型生成可能） |
| `vc_missing_prefix` | `## Verification Commands` の `$` / `- ` プレフィックス欠落（形式変換のみ） |

### requires_human とみなす blocker の種類

以下のいずれかが含まれる場合は `blocker_class: requires_human` とし、`needs_second_pass` で停止する。

| blocker_id | 説明 |
|---|---|
| `new_scope_area` | AC の追加・削除・変更が必要 |
| `ac_removed_or_weakened` | Acceptance Criteria の弱体化や削除 |
| `allowed_paths_expanded` | Allowed Paths の縮小・拡大 |
| `outcome_rewrite_needed` | Outcome のリライトが必要 |

### 分類ルール

1. `blocker_class` の分類は `REFINEMENT_LOOP_PLAN_V1.decisions.auto_fixable_structural_blocker_list` を参照する。
2. `auto_fixable_structural_blocker_list` が空でなく、かつ `requires_human` blocker が0件の場合は `blocker_class: auto_fixable_structural`。
3. `requires_human` blocker が1件でも含まれる場合は `blocker_class: requires_human`。
4. `--no-approval` 未指定の場合は `blocker_class` に関わらず `needs_second_pass` で停止する。

## Additional stop rules

- anchor comment fact-check が未完了のまま stale approval を使おうとした場合
- scope change signal が新規追加された場合
- required external research が critical claim を unresolved のまま残した場合

## Must not

- `approve` 以外を success 扱いして silently finish しない
- `max_iterations` を超えて自動ループしない（ただし `--no-approval` + `blocker_class: auto_fixable_structural` のみの場合は例外）
- `requires_human` blocker を自動処理しない
- `blocker_class: auto_fixable_structural` で auto-continue する際も `--no-approval` フラグの確認を省略しない

## Termination Comment（全 termination reason 共通）

すべての termination reason（`approved` / `needs_second_pass` / `human_escalation` / `superseded_by_decision`）で、終了コメントに `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含める。follow-up が存在しない場合も空配列で出力する（`follow_up_issues: []` / `note_only_observations: []`）。

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues: []   # 起票済み / reuse / skip 結果。空の場合も省略しない
  note_only_observations: []  # 起票せず記録のみ。空の場合も省略しない
```

詳細 schema は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を参照。`issue-refinement-loop` は thin orchestrator として raw context を保持せず、materialization 結果のみを報告する（`docs/dev/agent-skill-boundaries.md` の `ORCHESTRATOR_IO_BOUNDARY_V1` 参照）。
