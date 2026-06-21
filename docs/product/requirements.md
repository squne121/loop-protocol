---
doc_id: DOC-REQ-001
title: LOOP_PROTOCOL Requirements Baseline
status: active
capability_gate: M4 Upgrade Loop (v0.4.x)
last_updated_by_issue: 1094
---

# Requirements Baseline

`LOOP_PROTOCOL` における全体要件と非ゴールの正本。
詳細な機能仕様は本文で定義した方針に従って、後続の feature spec へ分離する。

## Document Priority

- `CLAUDE.md`: repo 全体の不変原則と読む順序。
- `.claude/rules/project-constitution.md`: 実装手順、docs 更新規則、検証ルール。
- この文書: 全体要件と global non-goals。
- `docs/product/features/<feature>.md`: 個別機能の詳細仕様と受け入れ条件。
- `docs/adr/*.md`: 設計判断の理由。
- `docs/product/game-overview.md`: 体験概要。正本ではない。
- `docs/dev/current-focus.md`: 一時的な優先順位。正本ではない。

## SSOT 階層

- global scope / global non-goals の正本: この `docs/product/requirements.md`。
- milestone 間（M3 / M4 など）の境界の参照元: `docs/product/playable-roadmap.md`。境界（boundary）を表すものであり、global 要件の正本ではない。
- 個別機能の詳細仕様の正本: `docs/product/features/<feature>.md`。
- この階層を逆転させない。`playable-roadmap.md` を global 要件の正本として上書き扱いしない。

## Current Capability Gate

- M1 Foundation Gate (v0.1.x) の基盤・guardrail・workflow・最小仕様正本は確立済み。
- M3 の実装と自動検証は完了済みである。
- 現在の capability gate は `M4: Upgrade Loop (v0.4.x)` とする。formal close / milestone readback の最終判断は `#733` 側で扱う。

## Global Non-Goals

- 既存作品の直接再現
- 複雑な campaign / territory 管理
- 本格的な audio 実装
- network / multiplayer
- 高品質アセット前提の演出
- Issue や spec にない大規模機能の先行追加

## Milestone Scope: M4 Upgrade Loop (v0.4.x)

- 現行の実装対象スコープは `M4: Upgrade Loop (v0.4.x)`（global 要件の正本は本 requirements.md であり、milestone は実装対象を指す）。
- M4 では、M3 で永続化済みの resource を消費して最小 upgrade を適用し、次 sortie の挙動変化として観測できるまでをスコープとする。
- M4 は MVP Loop の「resource が次の強化導線へ接続できること」を最小成立させるフェーズであり、M3 persistence 境界と `src/data` の data-driven 定義の両立を前提にする。
- resource consumption / upgrade の詳細仕様（消費量、負値禁止、反映対象、次 sortie への適用境界など）は feature spec（`docs/product/features/<feature>.md`）で定義する。
- GitHub Milestone object の title mismatch や close 判断は本 requirements 更新のスコープ外であり、`docs/product/playable-roadmap.md` の mapping readback と `docs/dev/milestone-ops.md` に従って別途扱う。

### M4 Non-Goals

- 大規模な upgrade tree（アップグレードツリー）の構築。
- 複数武器の追加・切替。
- campaign / territory の管理。
- M4 / M5 境界の詳細は `docs/product/playable-roadmap.md` を参照する。

## Current MVP Requirements

### MVP-001 戦闘表示と UI の分離

- Status: active
- 戦闘表示は Canvas、HUD やメニュー UI は DOM で分離する。
- `src/systems` は DOM / Canvas API に依存しない。
- Related: `docs/adr/0001-architecture-baseline.md`

### MVP-002 固定タイムステップ

- Status: active
- シミュレーションは固定タイムステップ 60Hz、描画は `requestAnimationFrame` を使う。
- 時間進行の正本は system update であり、render は状態を書き換えない。
- Related: `docs/adr/0001-architecture-baseline.md`

### MVP-003 Combat MVP の最小プレイスライス

- Status: active
- 1 戦闘ごとの短い sortie を遊べることを最初の実装目標とする。
- プレイヤーは Canvas 上で自機を操作して戦場へ局所介入する。
- 戦果や成長ループの詳細は `Loop MVP` で具体化する。

### MVP-004 データ駆動と保存境界

- Status: active
- 武器、敵、ユニットなどの定義は `src/data` に寄せる。
- 永続化は `src/storage` を通じて snapshot 境界で扱う。
- localStorage は MVP 段階の最小保存手段として使ってよい。

### MVP-005 現段階で成立させる品質ゲート

- Status: active
- 少なくとも `pnpm typecheck` `pnpm lint` `pnpm test` `pnpm build` を通せること。
- 受け入れ条件と non-goals は Issue または feature spec と対応づけて扱う。

## Feature Spec Policy

- 個別機能の stable な仕様は `docs/product/features/<feature>.md` に置く。
- feature spec は YAML フロントマター付き Markdown を採用する。
- feature spec の最小項目は以下。
  - feature ID
  - status
  - related issue
  - acceptance
  - non-goals
  - related tests
- `movement + projectile` のような個別機能は、この配置規則に従って後続 Issue で追加する。

## Acceptance Ownership

- 全体要件の境界はこの文書が持つ。
- 実装単位の受け入れ条件は Issue 本文で作業契約として定義し、stable 化したら feature spec へ昇格する。
- Issue コメントだけで決まった内容は永久仕様にしない。
