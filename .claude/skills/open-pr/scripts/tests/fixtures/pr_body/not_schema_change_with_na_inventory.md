## Summary

- Valid not schema change fixture（スキーマ変更を伴わない正常系 fixture）

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change
- reason: PR body parser とテストのみを変更する

## Schema Consumer Inventory

N/A
reason: producer-consumer 境界の machine-readable schema を変更しない

## Safety Claim Matrix

N/A
reason: safety-sensitive path に該当しない

## Notes

- Related issue: #244
- 上記は関連する Issue 番号です

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
