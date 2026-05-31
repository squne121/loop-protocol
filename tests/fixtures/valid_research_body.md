## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: "#100"
goal_ref: "test research goal"
change_kind: research
```

## Parent Issue

#100

## Outcome

`docs/research.md` が作成され、調査結果がまとめられた状態。

## In Scope

- 調査の実施

## Out of Scope

- 実装

## Acceptance Criteria

- [ ] AC1: 調査結果がまとめられること

## Verification Commands

```bash
# AC1
test -f docs/research.md
```

## Allowed Paths

- docs/research.md

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足（permission denied）により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合（I/O エラー等）は即停止

## Handoff Contract

調査完了後、docs/research.md を PR でレビューする。
