## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#100"
goal_ref: "test goal"
change_kind: workflow
```

## Parent Issue

#100

## Parent Goal Ref

- Goal: test

## Current Validated Scope

- test scope

## Remaining Parent Gaps

- none

## Outcome

`foo.py` が更新され、bar が動作する状態。

## In Scope

- foo の実装

## Out of Scope

- baz の実装

## Acceptance Criteria

- [ ] AC1: foo が動作すること

## Verification Commands

```bash
# AC1
uv run pytest tests/ -v
```

## Allowed Paths

- foo.py

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Required Skills

- Python 3

## Runtime Verification Applicability

decision: not_applicable
rationale: static checks only
