## Summary

- PR body validator implementation plan

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change
- reason: Python validator and tests only

## Schema Consumer Inventory

N/A
reason: schema を変更しないため inventory は不要

## Safety Claim Matrix

N/A
reason: safety-sensitive path に該当しない

## Notes

- Related issue: #330

## 受け入れ条件の達成状況

- [x] AC1: 達成（fixture）

## 検証コマンド結果

```text
$ pnpm typecheck
pass
```

## Allowed Paths 遵守

- 変更ファイル: fixture のみ
- Allowed Paths 逸脱: なし
