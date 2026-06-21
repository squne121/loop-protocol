---
doc_id: DOC-FOCUS-001
title: Current Focus
status: active
milestone: M4 Upgrade Loop (v0.4.x)
last_updated_by_issue: 1094
---

# Current Focus

## Current Phase

- 現在は `M4: Upgrade Loop (v0.4.x)` を進行中。
- 目的は、M3 で成立した resource 永続化の上に resource consumption と最小 upgrade を載せ、次 sortie へ反映される MVP Loop を閉じること。
- M3 の実装と自動検証は完了済みだが、parent close / milestone readback の最終判断は `#733` 側で扱う。

## Current Milestone

- `M4: Upgrade Loop (v0.4.x)`
- 完了条件は、sortie → resource 獲得 → upgrade → 次 sortie での挙動変化までの最小 Loop が成立し、既存品質ゲートを安定して通せること。
- M3 / M4 / M5 の境界の参照元は `docs/product/playable-roadmap.md`、global scope / non-goals の正本は `docs/product/requirements.md`。

## Priority Order

1. M4 の resource consumption / upgrade boundary を issue contract と product spec に同期する
2. sortie → resource → upgrade → 次 sortie 反映の最小 Loop を実装・検証順へ分解する

## Carry-Forward Notes

- `#733` は M3 parent close / readback の最終判断を保持しており、M4 着手と同時に自動 close されたものとして扱わない。
- `#690` の人間動画採取・waiver 解消は M2 / M3 系の carry-forward note として残るが、M4 current phase の primary outcome ではない。

## Do Now

- resource 消費の制約、最小 upgrade 定義、次 sortie への反映点を feature spec / issue contract に固定する。
- `src/storage` で永続化済みの resource を、`src/data` 駆動の upgrade 定義へ安全に接続する準備を進める。

## Do Not Do in M4

- 複雑な upgrade tree や大規模な複数武器導線を M4 に持ち込まない。
- campaign / territory / network / audio / 高品質アセット前提の作業へ広げない。
- GitHub milestone object の rename / create / close を、この current-focus 更新だけで解決したものとして扱わない。

## Decision Notes

- NotebookLM は運用の主役ではなく、必要時のレビュー支援として使う。
- `docs/product/game-overview.md` は概要文書であり、全体要件の正本ではない。
- 個別機能 spec の標準配置は `docs/product/features/<feature>.md` とする。
- global scope / global non-goals の正本は `docs/product/requirements.md`、M3 / M4 / M5 milestone 境界の参照元は `docs/product/playable-roadmap.md`。
