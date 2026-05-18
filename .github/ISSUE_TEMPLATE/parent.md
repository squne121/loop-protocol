---
name: Parent
about: 親 Issue（複数 child issue を束ねるトラッカー）を起票する
title: "導入: "
labels:
  - tracking
  - state/in-progress
---

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "<親トラッカーが維持する目的>"
change_kind: workflow
parent_mode: "<required: delivery-rollup | quality-gate | routing-map | decision-log>"
closure_mode: "<required: child-complete | measurement-ready | quality-validated | routing-complete | decision-recorded>"
```

`parent_mode` と `closure_mode` の対応:

- `delivery-rollup` → `child-complete`
- `quality-gate` → `measurement-ready | quality-validated`
- `routing-map` → `routing-complete`
- `decision-log` → `decision-recorded`

## Summary

## Goal

## Desired Destination
-

## Current Validated Scope
-

## Decisions Fixed On YYYY-MM-DD
-

## Quality Decision Record
- `Status`: `<quality-gate parent の場合は measurement-ready / quality-unvalidated など。不要なら N/A>`
- `Decision Date`: `<YYYY-MM-DD または未記録>`
- `Does this prove the parent goal complete?`: `<Yes / No / N/A>`
- `Reason`: `<判定根拠>`
- `Evidence`: `<benchmark / review / routing evidence / N/A>`
- `Next Action`: `<次に必要な child issue または decision>`

## Parent Closure Rule
- 本テンプレートは close 契約の文面を固定する first-step contract である
- `delivery-rollup`: `<child issue rollup 完了で close する条件>`
- `quality-gate`: `<Quality Decision Record が確定するまで close しない条件>`
- `routing-map`: `<routing / destination mapping 完了で close する条件>`
- `decision-log`: `<decision record と next action 固定で close する条件>`
- 非 `quality-gate` parent の `Quality Decision Record` は `N/A` 扱いでよく、close 判定の必須材料にしない
- `<required: ...>` placeholder や enum 外値は invalid とみなし、close 判定へ進めない

## Child Issues
- [ ]

## Dependency Order
1.

## Remaining Parent Gaps
- [ ]

## Phase Handoff Contract
<!-- child issue 起票時に implementation / research に引き継ぐ構造 -->
- `Desired Destination`
- `Current Validated Scope`
- `Remaining Parent Gaps`
- `Current Objective`
- `Bounded Current Context`
- `Normative References`
- `Allowed Paths`
- `Required Skills`
- `Validation Commands`
- `Stop Conditions`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
- `Handoff Prompt Draft`
- `Linked PR` (implementation のみ)

## Acceptance Criteria
-

## Notes
-
