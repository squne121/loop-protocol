---
adr_id: "0001"
title: "Architecture Baseline"
status: accepted
date: "2025-01-01"
related_issue: "#2"
---

# ADR 0001: Architecture Baseline

ECS 風の責務分離、固定タイムステップ、データ駆動設計をこのプロジェクトの初期方針とする。

- `state` は純粋データとして保つ。
- `systems` は 60Hz 前提の更新ロジックだけを担う。
- `render` は Canvas 描画専用とし、状態更新を禁止する。
- `ui` は DOM で構築し、Canvas 内 UI と混在させない。
- `storage` は snapshot を介した永続化境界として分離する。
