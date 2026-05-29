## Machine-Readable Contract

```yaml
contract_schema_version: "v1"
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用 fixture（Stop Conditions 不完全）"
change_kind: chore
```

## Parent Issue

なし

## Parent Goal Ref

- Goal: テスト用 fixture（Stop Conditions が 3 条件のみ）
- Desired Destination: N/A

## Current Validated Scope

- `example_script.py` の実装

## Remaining Parent Gaps

なし

## Outcome

`example_script.py` が `--dry-run` フラグを受け付け、副作用なしで実行結果を出力する。

## In Scope

- `example_script.py` の実装

## Out of Scope

- テストの追加

## Acceptance Criteria

- [ ] AC1: `example_script.py` が存在し、`--dry-run` フラグを受け付けること

## Verification Commands

```bash
# AC1
test -f example_script.py
```

## Allowed Paths

- `example_script.py`

## Stop Conditions

実装中にこれらの状況が発生したら直ちに作業を停止し、Issue comment に状況を記録して人間の判断を待つ。

- Allowed Paths 外の変更が必要と判明した場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- nested SubAgent delegation が必要になった場合

## Required Skills

なし
