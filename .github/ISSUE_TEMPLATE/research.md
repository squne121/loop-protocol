---
name: Research
about: 参照元資産の棚卸し / 仕様調査 / 比較検討用の child issue
title: "調査: "
labels:
  - phase/research
  - state/queued
  - agent/research
---

<!-- AC に実装変更（src/ 改修・tests/ 追加）を含む場合は implementation.md を使用する -->

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: "#<parent-issue-number>|none"
goal_ref: "<調査が前進させる目的>"
change_kind: research-only
```

## Parent Issue
- #

## Outcome
<!-- 調査結果として確定したい状態。1 文で -->

## In Scope
-

## Out of Scope
-

## Acceptance Criteria
<!-- 「結論ドキュメントが <パス> に書かれている」「比較表が出力される」など、検証可能な形で -->
-

## Verification Commands
<!-- 成果物の存在確認・形式チェックなど。実装変更を伴わない検証コマンドを列挙 -->
-

## Allowed Paths
- 読み取り専用。リポジトリ変更なし。
- 成果物の書き込み先がある場合のみ、具体的なパスを列挙する（例: `docs/research/<topic>.md`）

## Stop Conditions
- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足（permission denied）により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合（I/O エラー等）は即停止

## Handoff Contract
<!-- research 完了時に implementation child issue へ引き継ぐ場合の構造 -->
- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
