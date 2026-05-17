---
doc_id: DOC-FOCUS-001
title: Current Focus
status: active
milestone: M1 Foundation Gate (v0.1.x)
last_updated_by_issue: 3
---

# Current Focus

## Current Phase

- 現在は開発基盤整備フェーズ。
- 目的は「動く戦闘を増やすこと」ではなく、「AI と人間が壊しにくい土台を閉じること」。

## Current Milestone

- `M1: Foundation Gate (v0.1.x)`
- 完了条件は、基盤、運用ルール、guardrail、最小仕様正本が揃い、既存品質ゲートを安定して通せること。

## Priority Order

1. `#3` AI運用憲法と最小仕様正本を整備する
2. `#4` Issue駆動実装 skill と workflow 文書を整備する
3. `#5` Claude Code ガードレールを `.claude` に実装する
4. `#2` movement + projectile の最小仕様を固定する
5. `#1` 自機移動と Projectile 射撃システムを実装する

## Do Now

- `#3` で docs の正本と優先順位を確定する。
- `#4` で Issue から Plan、実装、PR までの標準手順を固定する。
- `#5` で `.claude` の実効ガードレールを強化する。

## Do Not Do Yet

- `#1` の Combat 実装を先に広げない。
- 敵 AI、当たり判定、ダメージ、勝敗処理を追加しない。
- campaign / territory / network / audio / 高品質アセット前提の作業へ広げない。
- hooks や skill の実装を、対応 Issue 以外へ押し込まない。

## Decision Notes

- NotebookLM は運用の主役ではなく、必要時のレビュー支援として使う。
- `docs/product/game-overview.md` は概要文書であり、全体要件の正本ではない。
- 個別機能 spec の標準配置は `docs/product/features/<feature>.md` とする。
- `movement + projectile` は `#2` の仕様固定を経てから `#1` へ入る。
