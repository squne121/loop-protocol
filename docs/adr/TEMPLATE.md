---
adr_id: "NNNN"
title: "<ADR のタイトル>"
status: proposed # allowed: proposed | accepted | superseded | deprecated
decision_date: "YYYY-MM-DD"
confirmed_date: null
related_issues:
  - "#NNN"
supersedes: []
superseded_by: null
---

# ADR NNNN: <タイトル>

<!--
frontmatter キーの運用規約:
- adr_id: ADR 番号を 4 桁の文字列で表現（leading zero を保つため必ず quote する）
- title: frontmatter と H1 で重複させる場合は frontmatter を正本とする
- status: proposed → accepted → (superseded | deprecated) の lifecycle で管理
- decision_date: accepted に遷移した日（proposed 段階では null 可）
- confirmed_date: 別 Issue / PR 等での再確認が完了した日。未確認なら null
- related_issues: 関連 GitHub Issue / PR を配列で列挙（最低 1 件）
- supersedes / superseded_by: ADR 間の代替関係を ADR ID 配列で表現
-->

## Context

<!-- なぜこの決定が必要になったか。背景、制約、判断に影響した要求を記述する。 -->

## Considered Options

<!-- 検討した選択肢を列挙する。単一案しかない場合も、なぜ代替を捨てたかを残す。 -->

## Decision

<!-- 選んだ選択肢と理由を記述する。 -->

## Consequences

<!-- 肯定的影響、否定的影響、トレードオフ、後続 Issue / ADR への引き継ぎ事項を記述する。 -->

## References

<!-- 関連 Issue、PR、外部一次情報、検証ログを列挙する。 -->
