---
status: draft
issue: "#282"
parent_issue: "#254"
doc_id: game-design
trace_links:
  - docs/product/requirements.md
  - docs/product/game-overview.md
  - docs/adr/0001-architecture-baseline.md
  - docs/adr/0002-sdd-tool-adoption.md
  - docs/dev/product-spec-lifecycle.md
---

# Game Design Document (GDD v0.1)

本書は LOOP_PROTOCOL の **GDD-level design** を保持する正本である。
プレイ体験のループ・画面・成長・報酬の輪郭を定義し、具体的な実装定数（時間・キー割当・経済率・武装数等）は下流仕様に委譲する。

## Intent

- GDD として AI 実装者が「何を体験させたいか」を最初に参照する SSOT を提供する。
- 体験設計の輪郭（loop / screens / progression / rewards / non-goals）に限定し、実装可能性や数値定数を定義しない。
- 下流仕様（game-logic.md / mvp-scope.md / playtest-protocol.md）の上位制約となる。
- playtest 結果に応じて diff-first に補正される対象であり、本書を全文再生成しない。

## Authority and Fallbacks

優先順位（上が強い）:

1. `docs/product/requirements.md` — 全体要件と global non-goals の正本
2. `docs/product/game-thesis.md`（C254-3 / #281。**未マージ時は不在として扱い fallback へ移る**）
3. 本書（`docs/product/game-design.md`） — GDD-level design
4. `docs/product/game-logic.md`（C254-5） — 実装可能性 / 状態遷移 / 衝突 / 勝敗 / 保存境界
5. `docs/product/mvp-scope.md`（C254-6） — MVP に含める / 含めない境界
6. `docs/product/game-overview.md` — 概念説明 fallback（正本ではない）

矛盾が生じた場合は上位が勝つ。`game-thesis.md` 未マージの段階では `game-overview.md` と `requirements.md` を fallback ソースとする。

## Requirements

EARS notation を **acceptance criteria レベル限定** で使用する（散文全体の EARS 化は禁止 — `product-spec-lifecycle.md` §EARS Usage 準拠）。

- **REQ-GDD-001 (Ubiquitous):** The game shall present a top-down action RTS experience structured around short, repeatable sortie loops.
- **REQ-GDD-002 (Event-driven):** When a sortie ends, the system shall transform the sortie's outcome into resources that feed back into player progression.
- **REQ-GDD-003 (Ubiquitous):** The game shall separate combat presentation (Canvas) from meta UI (DOM) per ADR 0001.
- **REQ-GDD-004 (Option):** Where playtest data invalidates a design hypothesis recorded here, the affected `REQ-GDD-*` shall be revised via a spec delta issue per `product-spec-lifecycle.md` §Product Spec Delta Flow.
- **REQ-GDD-005 (Unwanted behavior):** If implementation constants (time budgets / input bindings / economy rates / upgrade counts) are required, the system shall defer their definition to downstream specs (`game-logic.md` / `mvp-scope.md`) and shall not embed them in this document.

`REQ-GDD-*` の AC レベル実装可能性は `game-logic.md`（C254-5）が保証する。

## Core Loop

体験全体のループ（メタ進行）:

1. **Sortie に出る** — プレイヤーは現在の戦力で短い戦闘 instance に挑む。
2. **戦闘結果が resource になる** — 勝敗・残存・破壊実績などが次の強化導線に接続する materializable な結果として残る。
3. **強化と編成** — 得た resource を player 戦力（武装・機体・編成）に反映する。
4. **次の sortie に戻る** — 強化された戦力で次の挑戦に再突入する。

このループの長さ・経済率・強化粒度は本書では確定しない（Open Questions / Downstream Boundaries 参照）。

## Sortie Loop

1 戦闘 instance の内側の流れ:

1. **Brief / Deploy** — sortie の目的と前提を最小提示し、プレイヤーが戦場に投入される。
2. **Engage** — プレイヤーは Canvas 上で自機を操作し、戦場へ局所介入する。AI・味方・敵・projectile 等は固定タイムステップで進行する。
3. **Resolve** — 勝敗 / 中断 / 撤退いずれかで sortie が終了する。
4. **Debrief** — 結果が summary として提示され、Core Loop の resource 化へ接続する。

各フェーズの **具体的な時間・操作キー・勝敗判定** は本書で確定せず `game-logic.md` に委譲する。

## Screens

GDD-level で要求する画面の **役割集合**（実装上のレイアウト・寸法は委譲）:

- **Combat Screen (Canvas)** — 戦場表示。自機 / 敵 / projectile / 視覚 telemetry を描画する。状態を書き換えない（ADR 0001）。
- **HUD (DOM, overlay)** — 残量・スコア・最低限のシステム telemetry。Canvas の上に重ねる軽量 UI。
- **Brief / Debrief (DOM)** — sortie 前後の提示画面。Core Loop と Sortie Loop を接続する。
- **Loadout / Upgrade (DOM)** — sortie 間で player 戦力を編集する画面。具体的な強化メカニクスは `game-logic.md` / `mvp-scope.md` に委譲。
- **System (DOM)** — save / reset / settings 等の最小システム導線。

戦闘演出と meta UI の分離は ADR 0001 を遵守する（Canvas は描画専用 / DOM は UI / `systems` は 60Hz 更新ロジック）。

## Progression

- player の戦力は sortie の結果に応じて段階的に強化される。
- 進行は **resource → choice → permanence** の流れを持つ：sortie で得た resource を、プレイヤーの選択を通じて戦力に固定化する。
- 進行の粒度（強化単位の数・1 sortie あたりの取得量・最大強化階層）は本書で確定しない。`mvp-scope.md`（C254-6）が MVP 境界を定義する。
- 失敗時に何を保持し / 何を失うかの roguelite/persistence 軸も本書では確定しない（Open Questions）。

## Rewards

- 報酬 surface は **sortie outcome → resource → strengthening choice** の連鎖で発火する。
- 報酬は player の意思決定を生む単位で粒度を切ること（純粋な numeric inflation を主目的としない）。
- 経済率・還元方式・upgrade 単価は **本書で確定しない**。playtest 仮説として Open Questions に置き、`mvp-scope.md` / `game-logic.md` に委譲する。

## Non-Goals

本書は `docs/product/requirements.md` の Global Non-Goals を反転せず継承する。GDD-level での非ゴール:

- 既存作品の固有名詞 / 画像 / 音声 / キャラクター / テキストの流用、および直接再現
- network / multiplayer / オンライン対戦・協力プレイ
- territory / 領地拡大 / base building / 施設建設のメタレイヤー
- campaign 構造（複雑な分岐シナリオ・長期キャンペーン管理）
- 高品質アセット前提の演出を体験設計の前提に置くこと
- 本格的な audio 実装（最小 SE 程度を超える音声設計）
- Issue や spec にない大規模機能の先行追加

これらは Global Non-Goals の鏡像であり、本書から削除・反転してはならない。

## Downstream Boundaries

本書では確定せず下流仕様に委譲する事項:

| 委譲先 | 委譲内容 |
|---|---|
| `docs/product/game-logic.md`（C254-5） | 状態遷移 / 入力バインディング / 1 sortie の具体的な時間進行 / 衝突 / 勝敗判定 / 保存境界 / 数値ルール |
| `docs/product/mvp-scope.md`（C254-6） | MVP に含める/含めない feature 集合・武装/敵/ユニット数の上限・成功/失敗/ピボット基準 |
| `docs/product/playtest-protocol.md`（C254-7） | playtest 仮説の検証手順・playtest-log template・design hypothesis invalidated の取扱い |
| `docs/product/features/<feature>.md` | 個別 feature の詳細仕様（feature spec 標準配置） |
| `docs/adr/0001-architecture-baseline.md` | Canvas / DOM / `systems` / `state` / `storage` の責務分離（GDD は本 ADR に整合させる） |

本書は上位制約を提供するが、上記の **具体的な数値・キー名・実装定数を本文に書かない**。下流が確定する。

## Open Questions

playtest によって検証または棄却される候補。各項目は `playtest-protocol.md` の playtest-log を通じて補正される（仮説のまま放置しない）。

- **OQ-GDD-01:** 1 sortie の体感長は「短い」をどの程度に解像するか（time budget の design 仮説 — 確定は `game-logic.md`）。
- **OQ-GDD-02:** 戦果 → resource の還元方式（全量還元か段階還元か）— 経済率は仮説段階。
- **OQ-GDD-03:** 強化粒度（1 sortie あたりの upgrade 選択回数・upgrade tree の幅と深さ）。
- **OQ-GDD-04:** 失敗時の roguelite/persistence 軸（失敗時に戦力を保持するか / 一部を失うか / 完全リセットか）。
- **OQ-GDD-05:** 操作モデル（自機の直接操作と RTS 的な指示の比率）— 操作キー割当は `game-logic.md` 領域。
- **OQ-GDD-06:** 報酬の判断粒度（プレイヤーが意思決定を行う最小単位）。
- **OQ-GDD-07:** sortie のシナリオ多様性（mission archetype の数・procedural 要素の有無）。

これらは playtest による補正対象であり、本書を全文再生成せず diff-first で `REQ-GDD-*` または Open Questions の項目を更新する。

## Verification

```bash
# File exists
test -f docs/product/game-design.md

# Line budget
awk 'END { exit (NR > 250) }' docs/product/game-design.md

# Frontmatter
rg -q '^issue: "#282"' docs/product/game-design.md
rg -q '^parent_issue: "#254"' docs/product/game-design.md
rg -q '^doc_id: game-design$' docs/product/game-design.md
rg -q '^trace_links:' docs/product/game-design.md

# Trace links
rg -q "docs/product/requirements.md" docs/product/game-design.md
rg -q "docs/product/game-overview.md" docs/product/game-design.md

# No implementation constants — see Issue #282 AC4 / AC7 for forbidden token list
# (Verification Commands authoritative source: Issue #282 §Verification Commands Block 2)
```
