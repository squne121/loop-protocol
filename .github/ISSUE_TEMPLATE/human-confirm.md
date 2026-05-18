---
name: Human Confirm
about: 導入方針と設定境界を人間判断で確定する child issue
title: "人間確認: "
labels:
  - question
  - phase/human-confirm
  - state/needs-human
---

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: human-confirm
parent_issue: "#<parent-issue-number>|none"
goal_ref: "<人間判断で固定したい目的>"
decision_type: policy|scope|priority|go-no-go
```

## Parent Issue
- #

## Outcome
<!-- 人間判断で確定したい状態を 1 文で -->

## In Scope
-

## Out of Scope
-

## Acceptance Criteria
-

## Verification Commands
- 人間レビュー結果のコメント記録のみ

## Allowed Paths
- 読み取り専用。リポジトリ変更なし。

## Stop Conditions
- 人間の回答がないまま implementation child issue を着手可にしない
