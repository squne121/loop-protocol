---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Japanese only fixture (no warning)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "japanese only no warning test"
change_kind: code
```

## Outcome

日本語テキストのみのセクションで警告が出ないことを確認する。

## Current Validated Scope

- ユーザー認証モジュールを改修する
- パスワードハッシュアルゴリズムを更新する
- セッション管理を改善する

## In Scope

- データベーススキーマを再設計する
- キャッシュレイヤーを実装する
- モニタリングダッシュボードを構築する

## Acceptance Criteria

- [ ] AC1: `src/auth.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/auth.py
```

## Stop Conditions

- 1
- 2
- 3
- 4
- 5
- 6

## Runtime Verification Applicability

decision: not_applicable
reason: fixture only.

## Allowed Paths

- `src/auth.py`
