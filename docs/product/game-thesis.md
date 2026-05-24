---
doc_id: DOC-PRODUCT-GAME-THESIS-001
title: Game Thesis
status: draft
issue: "#281"
parent_issue: "#254"
canonical_source: docs-product
sdd_boundary: docs-ssot-wins
---

# ゲームコンセプト正本 / Game Thesis

本書は LOOP_PROTOCOL のプロダクトとしてのコアコンセプト・想定プレイヤー・設計の柱・非ゴール・設計仮説を定義する正本である。後続の `docs/product/game-design.md` / `docs/product/game-logic.md` / `docs/product/mvp-scope.md` および playtest 仕様は、本書をプロダクト判断基準として参照する。

## 状態注記 / Status Note

本書は `status: draft` の間は normative ではなく、downstream implementation の判断基準として拘束力を持たない。Human Product Review で承認され `status: accepted` に昇格して初めて normative になる。それまでの間、本書の設計仮説（design hypotheses）は確定仕様ではなく、playtest や下流 spec によって反証・更新され得る作業仮説として扱う。

また本書は `.specify/**` 配下の Spec Kit 生成物より優先される product SSOT である（`docs-ssot-wins` 境界、ADR 0002）。

## ピッチ / Pitch

LOOP_PROTOCOL は、自律 AI 同士が交戦する戦場へ、エース機としてプレイヤーが直接介入するトップビュー型のアクション RTS である。プレイヤーは大規模軍勢のマクロ運用ではなく、移動拠点を軸とした短時間の局所介入で戦況を傾け、撃破した敵機の技術を解析・取り込んで次の出撃に向けて自軍を強化していく。

## 想定プレイヤー / Target Player

緻密な全軍指揮や領土運営ではなく、瞬間ごとの行動判断と「自分の介入で局所が決まる」エース感、そして敵技術の解析を通じた強化ループ（戦闘 → データ抽出 → 武器強化）に満足を覚えるプレイヤー。WASD + マウス相当の直感的な操作と、`Zキー相当` の軽量なマクロ指示で味方 AI を誘導する程度の操作感を許容する層を主対象とする。具体キー割り当てや時間制約は下流仕様に委ねる。

## 設計の柱 / Design Pillars

### 1. 戦況可読性 / Combat Readability

敵機・味方機・弾幕・危険範囲を、色だけでなく **形状・シルエット・明度差・パターン・警告形状** で識別できるようにする（WCAG 1.4.1 準拠を念頭に置き、色のみに依存した識別は禁止）。UI や演出が戦闘判断を阻害してはならず、画面上のあらゆる情報は瞬時に意味が立ち上がる粒度で設計する。

### 2. 硬質な未完成兵器感 / Industrial and Unfinished Aesthetics

完成度の高いキャラクターアートではなく、黒・白・グレーを基調とした工業線画、設計図表現、センサー的表示、手続き的なサウンドで「無人戦争・冷徹な機械戦」の空気を構築する。豪華アセットや高品質ボイス前提の演出は採用しない。

### 3. 解析と鹵獲の快感 / Reverse Engineering Satisfaction

敵機の撃破を単なるスコア加算ではなく **未知技術のデータ抽出と解析プロセス** として再フレームする。撃破 → データ → 強化 → 次戦の体験を視覚と数値の両面で接続し、目の前の戦闘成果が次の機体強化に確かに反映される感触を提供する。

## 非ゴール / Non-Goals

- ワールドマップ・ノード進行型のキャンペーンや、複雑な拠点建設システム
- オンライン対戦・P2P マルチプレイ
- フルボイス、オーケストラ BGM、高品質アセット前提の演出
- 既存ロボット / メカ作品の固有名詞・固有用語の直接流用
- 色（Color）のみに依存した情報伝達・陣営識別

## 設計仮説 / Design Hypotheses

MDA framework（Hunicke et al. 原典）に基づき、mechanics → dynamics → aesthetics の設計者視点と、プレイヤーが aesthetics 側から体験する逆方向の視点差を意識して仮説化する。各仮説は playtest による反証可能性を持ち、`player_promise` でプレイヤー側体験を、`validation_method` で観測口を明示する。具体的な数値・キー割り当ては downstream spec に委ねる。

```yaml
design_hypotheses:
  - id: HYP-001-ace-intervention
    aesthetic: agency, competence, leverage（介入主体感・優越感・てこ）
    player_promise: |
      プレイヤーは「自分が直接介入したから局所戦況が変わった」と
      自覚的に感じられる。味方 AI の強さの偶然ではなく、自分の判断と
      操作が劣勢局面をひっくり返した、という体験を保証する。
    mechanics: |
      プレイヤー操作機は味方 AI より機動・火力で優越し、明確に「エース機」として振る舞う。
      味方ラインが崩れかける局所的危機トリガがある。
      軽量なマクロ指示（拠点防衛 / 突出 / 退避 等）で味方の挙動を誘導できる。
      意思決定は秒単位の窓で行う（分単位の計画ではない）。
    expected_dynamics: |
      味方 AI は基礎的な防衛は維持する。
      プレイヤーが介入した局所では戦術的均衡が傾く。
      成功した介入は「味方の生存」「脅威の排除」として観測可能。
      プレイヤーが介入し損ねると、味方は退却し局所を失う。
    falsification_signal: |
      勝因が味方 AI 任せに帰着し、プレイヤー介入が結果に寄与していない。
      介入導線が複雑で、プレイヤーが何をしているか自覚できない。
      Playtest でマクロ戦略志向の好みがアクション介入を上回る。
    validation_method: |
      初期 playtest で、誘導なしにプレイヤー自身に「何が戦況を変えたと思うか」を語ってもらう
      （self-explanation test）。自発的に「自分の介入のおかげ」と言えるかを記録し、
      時間制約下のエンゲージメント（離脱・集中継続）も観察する。
    downstream_owner: game-design, game-logic, mvp-scope, playtest-protocol

  - id: HYP-002-tech-extraction-loop
    aesthetic: discovery, progression, mastery（発見・成長・習熟）
    player_promise: |
      撃破した敵から具体的に「次戦で使える新しい能力」を学べたと感じられる。
      強化は単なる数値増加やコスメではなく、リバースエンジニアリングと
      成長の物語として知覚される。
    mechanics: |
      敵撃破により解析可能な技術データが得られる。
      準備フェーズで、得たデータから強化項目を選ぶ。
      選んだ強化が次戦の挙動に明確に反映される（視認可能な接続）。
    expected_dynamics: |
      戦闘を重ねるごとに player capability が段階的に拡張する。
      序盤は守勢中心、後半は攻勢の選択肢が広がる。
      各出撃が積み上げに寄与する手応えとして知覚される。
    falsification_signal: |
      強化がコスメ的に感じられる。
      準備フェーズが戦略的選択ではなく単調作業として体感される。
      技術抽出のフレームと作品世界観が噛み合わない。
    validation_method: |
      3〜5 出撃連続のセッションを観察し、プレイヤーが強化を
      「意味のある成長」と語るか「単なる数値変化」と語るかを記録する。
      準備フェーズの滞在時間と、次戦のロードアウト選択との相関も計測する。
    downstream_owner: game-logic, mvp-scope, playtest-protocol
```

## 目的 / Intent

本書はプロダクトレベルの判断基準を提供する。後続の game-design（GDD 本体）、game-logic（ゲームロジック仕様）、mvp-scope（MVP 境界）、playtest 仕様は、本書の設計の柱と設計仮説を判断材料として参照し、提案機能がコアファンタジー（局所介入と技術駆動の成長）に資するかを評価する。

## 未解決の問い / Open Questions

- 初期 playtest で、プレイヤーは「エース機としての介入感」を実際に知覚するか。それとも味方 AI の強さが知覚を支配するか。
- 技術抽出 → 強化ループは「意味のある成長」として読まれるか、それとも単なる装飾的報酬として読まれるか。
- 出撃構造（sortie）において、プレイヤーはマクロ戦略よりも瞬時のアクション介入を好むか。
- 軽量マクロ指示システムは、どの程度のガイダンスで直感的に使えるようになるか。

## プレイテスト仮説 / Playtest Hypotheses

- HYP-001-ace-intervention — エース機としての介入感の知覚
- HYP-002-tech-extraction-loop — 技術抽出 → 強化ループの意味付け

## 受け入れ条件境界 / Acceptance Criteria Boundary

本書は implementation-level の EARS acceptance criteria を定義しない。具体的なタイミング（出撃秒数等）、入力キー割り当て、強化項目数、勝敗条件等は downstream の game-design / game-logic / mvp-scope spec が定める。設計仮説は playtest フィードバックの validation target として機能し、本書から直接 implementation task を生やしてはならない（ADR 0002 の「design hypothesis invalidated は直接 implementation issue にしない」原則）。

## トレースリンク / Trace Links

本書は次の正本・要件にトレースする:

- GitHub Issue `#281`（本 Issue）
- GitHub Issue `#254`（parent SDD rollup）
- `docs/product/requirements.md` — 全体要件と非ゴール
  - `MVP-003`（Combat MVP の短時間出撃と局所介入）
  - `MVP-004`（データ駆動定義と progression / resource 境界）
- `docs/product/game-overview.md` — 体験概要（non-normative）
- `docs/dev/product-spec-lifecycle.md` — compact spec 作成と lifecycle 規約
- `docs/adr/0002-sdd-tool-adoption.md` — SDD 採用方針と `docs-ssot-wins` 境界
