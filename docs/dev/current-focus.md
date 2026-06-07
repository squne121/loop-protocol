---
doc_id: DOC-FOCUS-001
title: Current Focus
status: active
milestone: M3 Result Persistence (v0.3.x)
last_updated_by_issue: 734
---

# Current Focus

## Current Phase

- 現在は `M3: Result Persistence (v0.3.x)` を進行中。
- 目的は、sortie 結果から報酬を獲得し resource として localStorage へ永続化し、reload 後に進行を復元する MVP Loop を閉じること。
- M1 Foundation Gate の基盤・運用ルール・guardrail は確立済みであり、その上に M3 の永続化スライスを載せる。

## Current Milestone

- `M3: Result Persistence (v0.3.x)`
- 完了条件は、報酬 → resource → localStorage 永続化 → reload 復元までの最小 Loop が成立し、既存品質ゲートを安定して通せること。
- M3 / M4 の境界の参照元は `docs/product/playable-roadmap.md`、global scope / non-goals の正本は `docs/product/requirements.md`。

## Priority Order

1. `#735` M3 feature spec（resource / persistence）を作成する
2. `#736` 以降で reward / persistence の実装を進める

## Do Now

- `#735` で resource / persistence の詳細仕様（初期値・上限・負値禁止・reward 計算式）を feature spec に固定する。
- 報酬 → resource → localStorage → reload 復元の最小 Loop を `src/storage` の snapshot 境界経由で実装する準備を整える。

## Do Not Do in M3

- weapon upgrade（武器強化）を M3 に持ち込まない。武器1種のパラメータ強化（resource 消費）は `M4: Upgrade Loop` のスコープ。
- resource consumption（resource 消費）による強化導線を M3 で実装しない（M4）。
- upgrade UI / upgrade tree（アップグレードツリー）を M3 に追加しない。
- 複数武器の追加を M3 で行わない。
- campaign / territory / network / audio / 高品質アセット前提の作業へ広げない。

## Decision Notes

- NotebookLM は運用の主役ではなく、必要時のレビュー支援として使う。
- `docs/product/game-overview.md` は概要文書であり、全体要件の正本ではない。
- 個別機能 spec の標準配置は `docs/product/features/<feature>.md` とする。
- global scope / global non-goals の正本は `docs/product/requirements.md`、M3 / M4 milestone 境界の参照元は `docs/product/playable-roadmap.md`。
