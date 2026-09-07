---
doc_id: DOC-FOCUS-001
title: Current Focus
status: active
milestone: M5 Playable Slice Hardening (v0.5.x)
last_updated_by_issue: 2573
---

# Current Focus

## Current Phase

- `M4: Upgrade Loop (v0.4.x)` は完了済み。parent tracker `#1176` は `CLOSED / COMPLETED`（2026-09-08 live readback で確認）。
- 現在は `M5: Playable Slice Hardening (v0.5.x)` へ phase handoff済みで、これを進行中とする。
- 目的は、既存 M2〜M4 slice（sortie → result/resource → upgrade → next sortie）を、新規 core mechanic を増やさず player-facing / persistence / readability / balance / runtime evidence の各境界で硬化する baseline acceptance である。

## Current Milestone

- `M5: Playable Slice Hardening (v0.5.x)`（delivery-rollup parent tracker: `#2572`）
- 完了条件は `docs/product/playable-roadmap.md` の M5 セクション `close_conditions` を正本とする。
- M4 / M5 / M6〜M8 の境界の参照元は `docs/product/playable-roadmap.md`、global scope / non-goals の正本は `docs/product/requirements.md`。

## Priority Order

1. M5 baseline-first 方針で、既存 M2〜M4 slice の player-facing normal flow / persistence continuity / combat readability を現行 current-main で評価する（`#2572` Workstream 2）。
2. baseline で実際に観測された M5 blocker のみを narrow child Issue として分解する（speculative hardening を先行させない）。

## Carry-Forward Notes

- `#733` は M3 parent close / readback の最終判断を保持しており、M5 着手と同時に自動 close されたものとして扱わない。
- `#690` の人間動画採取・waiver 解消は M2 / M3 系の carry-forward note として残るが、M5 current phase の primary outcome ではない。

## Do Now

- M5 baseline acceptance（fresh state flow / saved・reload flow / developer-self 3〜5 sortie playtest / architecture invariant / canonical quality gates）を現行 current-main で実行する。
- baseline で観測された blocker のみを narrow implementation child Issue として起票する。

## Do Not Do in M5

- 複雑な upgrade tree や大規模な複数武器導線を M5 に持ち込まない（引き続き非ゴール）。
- campaign / territory / network / audio / 高品質アセット前提の作業へ広げない。
- M6 の ally NPC / assist-player runtime 実装を M5 に持ち込まない。
- M7 の technology-choice expansion（tech extraction runtime）を M5 に持ち込まない。
- GitHub milestone object の close を、この current-focus 更新だけで解決したものとして扱わない。

## Decision Notes

- NotebookLM は運用の主役ではなく、必要時のレビュー支援として使う。
- `docs/product/game-overview.md` は概要文書であり、全体要件の正本ではない。
- 個別機能 spec の標準配置は `docs/product/features/<feature>.md` とする。
- global scope / global non-goals の正本は `docs/product/requirements.md`、M4 / M5 / M6〜M8 milestone 境界の参照元は `docs/product/playable-roadmap.md`。
