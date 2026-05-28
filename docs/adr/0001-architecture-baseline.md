---
adr_id: "0001"
title: "Architecture Baseline"
status: accepted
decision_date: null # 元の決定日は記録なし。将来判明したら更新する
confirmed_date: null
metadata_migrated_at: "2026-05-28" # PR #441 で frontmatter を導入した日付
related_issues:
  - "#2"
supersedes: []
superseded_by: null
---

# ADR 0001: Architecture Baseline

ECS 風の責務分離、固定タイムステップ、データ駆動設計をこのプロジェクトの初期方針とする。

- `state` は純粋データとして保つ。
- `systems` は 60Hz 前提の更新ロジックだけを担う。
- `render` は Canvas 描画専用とし、状態更新を禁止する。
- `ui` は DOM で構築し、Canvas 内 UI と混在させない。
- `storage` は snapshot を介した永続化境界として分離する。
