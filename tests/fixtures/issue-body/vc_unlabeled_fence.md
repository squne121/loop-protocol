---
TITLE: 実装: unlabeled fence VC format test fixture
LABELS: phase/implementation
---
## Goal

unlabeled fence VC format のテスト用 fixture（非 canonical）。

## Acceptance Criteria

- [ ] AC1: コマンドが実行される

## Verification Commands

```
# AC1
$ uv run pytest tests/ -x -q
```

## Allowed Paths

- tests/fixtures/issue-body/

## Stop Conditions

- In Scope 外の変更が必要と判明した場合
- Allowed Paths 外の変更が必要と判明した場合
- 依存サービスが利用不可の場合
- テストが 3 回以上失敗し続ける場合
- データ整合性の問題が発生した場合
- セキュリティ上の懸念が発生した場合

## Runtime Verification Applicability

decision: not_applicable
