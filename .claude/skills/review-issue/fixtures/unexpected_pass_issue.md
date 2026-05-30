---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: unexpected_pass テスト用フィクスチャ（VC が baseline で pass する Issue）
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "unexpected_pass テスト用フィクスチャ"
change_kind: code
```

## Parent Issue

none

## Outcome

`unexpected_pass_issue.md` が baseline_vc_preflight で unexpected_pass 判定されることを確認する。

## Background

このフィクスチャは `--mode execute` の unexpected_pass 検出テスト用。VC の `$ true` は実装前でも常に exit 0 を返すため unexpected_pass となる。

## Parent Goal Ref

- Goal: unexpected_pass テスト用
- Desired Destination: unexpected_pass が検出されること

## Current Validated Scope

- テストフィクスチャのみ

## Remaining Parent Gaps

なし

## In Scope

- テストフィクスチャのみ

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `true` コマンドが常に exit 0 を返すこと（unexpected_pass テスト用）

## Verification Commands

```bash
# AC1
$ true
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用。

## Allowed Paths

- `.claude/skills/review-issue/fixtures/unexpected_pass_issue.md`
