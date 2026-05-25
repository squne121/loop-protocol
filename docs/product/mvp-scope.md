---
status: draft
issue: "#285"
parent_issue: "#254"
doc_id: mvp-scope
trace_links:
  - docs/product/requirements.md
  - docs/product/game-thesis.md
  - docs/product/game-design.md
  - docs/product/game-logic.md
  - docs/dev/product-spec-lifecycle.md
  - docs/adr/0002-sdd-tool-adoption.md
  - "#254"
  - "#285"
---

# MVP スコープ定義 / MVP Scope Definition

本書は LOOP_PROTOCOL の MVP 境界を定義する product SSOT である。MVP を「完成版の縮小版」ではなく、ゲームデザイン仮説を最小コストで検証するための検査装置として扱う。

## 状態注記 / Status Note

本書は `status: draft` の間は normative ではなく、downstream implementation の判断基準として拘束力を持たない。Human Product Review で承認され `status: accepted` に昇格して初めて normative になる。draft 段階では MVP 境界の作業仮説として扱い、playtest と後続 spec delta により更新し得る。

## 目的 / Intent

後続の implementation issue と playtest protocol が、MVP に何を含め、何を含めず、何を success / failure / pivot の signal として観測するかを共有できるようにする。本書は feature 数やバランス定数を確定する文書ではなく、M1 Foundation Gate における「何を検証対象にするか」の境界を定める。

## 正本階層 / Authority and Fallbacks

### Normativity Guard

`status: draft` の間、本書は discovery / review / playtest planning のための作業仮説であり、implementation issue の acceptance source としては使用しない。

以下の Authority and Fallbacks は `status: accepted` に昇格した後にのみ適用する。draft 段階で実装判断が必要な場合は、`docs/product/requirements.md`、accepted `docs/product/game-thesis.md`、対象 Issue contract を優先し、本書は非拘束の参考情報として扱う。

優先順位（上が強い）:

1. `docs/product/requirements.md` — 全体要件と global non-goals の正本
2. `docs/product/game-thesis.md` — accepted な上位コンセプト正本
3. 本書（`docs/product/mvp-scope.md`） — `status: accepted` 後にのみ、MVP に含める / 含めない境界の正本
4. `docs/product/game-logic.md` — implementation-facing reference（main 上に存在するが `status: draft`）
5. `docs/product/game-design.md` — GDD-level design。`status: draft` のため **auxiliary_only_until_reconciled**
6. `docs/product/game-overview.md` — 概念説明 fallback（正本ではない）

`game-design.md` は Human Product Review による reconciliation 前の draft であり、本書はそれを無条件の上位正本として扱わない。`docs/dev/product-spec-lifecycle.md` と ADR 0002 に従い、generated workbench artifacts は正本化せず、`docs/product/**` を優先する。

## MVP Hypotheses

- **HYP-MVP-001:** プレイヤーは短時間 sortie の中で、自分の直接介入が局所戦況を変えたと説明できる。Trace: `HYP-001-ace-intervention`
- **HYP-MVP-002:** 撃破・解析・強化の流れが、単なる数値加算ではなく「敵技術を取り込んだ成長」として読まれる。Trace: `HYP-002-tech-extraction-loop`
- **HYP-MVP-003:** 色だけに依存せず、形状・シルエット・明度差・警告形状で戦況と危険を把握できる。Trace: `docs/product/game-thesis.md` の Design Pillars / Combat Readability

## Included

- 1 sortie 単位の `pre-combat -> combat -> debrief / defeat` ループ
- Canvas 戦闘表示と DOM HUD / debrief の分離
- player-controlled entity による局所介入
- 最小限の ally / enemy / projectile を使った短時間戦闘
- `requirements.md` と `game-logic.md` に整合する 60Hz fixed timestep と snapshot 境界
- 戦果が解析 / 強化選択に接続する最小 progression 導線
- playtest で success / failure / pivot を判断するための観測軸

## Excluded

- network / multiplayer
- territory / world map / node campaign / base building
- 高品質アセット前提の演出、本格 audio、フルボイス
- 複雑な economy / upgrade tree / release candidate 基準
- generated workbench artifacts を authoritative source にする運用
- `tasks.md` を direct implementation source にする運用

## Success Criteria

- プレイヤーが「自分の介入で局所戦況が変わった」と自発的に説明できる
- 報酬や成長が「敵技術の解析・取り込み」として読まれ、単なる数値上昇に見えない
- 戦況把握が色依存にならず、危険・敵味方・報酬導線を区別できる
- 3〜5 sortie の試行で、次の sortie を試す理由が強化選択または学習に結びつく

## Failure Criteria

- 勝因が味方 AI / passive stat / 偶然に見え、プレイヤー介入の寄与を説明できない
- 報酬が cosmetic または "just numbers" として解釈される
- 戦況が読めず、介入判断より混乱や視認負荷が支配する
- 本書と `game-thesis.md` / `game-logic.md` の前提矛盾が implementation 前に顕在化する

## Pivot Criteria

- 介入感が成立しない場合、コンテンツ追加より先に player control / ally crisis design / telemetry framing を見直す
- 技術抽出ループが成立しない場合、economy 拡張より先に reward framing と debrief 導線を見直す
- 可読性が成立しない場合、asset 品質向上より先に shape / silhouette / warning grammar を見直す
- sortie timer や敵数が不適切な場合、コード変更へ直行せず spec delta issue を経由して調整する

## Measurement Contract

| Hypothesis | Observable Signal | Collection Method | Success | Failure |
|---|---|---|---|---|
| `HYP-MVP-001` | プレイヤーが局所戦況変化の主因を自機介入として説明する | 3〜5 sortie の internal playtest 後に self-explanation prompt を記録する | 主要因として player intervention が語られる | 主要因が allied AI / passive stat / randomness に帰属される |
| `HYP-MVP-002` | 報酬が enemy technology / analysis / capability growth として読まれる | debrief 後の自由記述または probe を記録する | reward が敵技術の解析・取り込みとして説明される | reward が cosmetic / generic currency / just numbers と説明される |
| `HYP-MVP-003` | 色以外の cue で敵味方・危険・報酬導線を判別できる | observation log と misread notes を残す | shape / silhouette / brightness / warning grammar で判別できる | color-only cue 依存または誤認が支配的 |

本書は測定契約の最小境界のみを定め、playtest 手順・記録フォーマット・判定ログの詳細は `docs/product/playtest-protocol.md` に委譲する。

## Non-Goals

- Combat 全機能の固定仕様化
- 実装定数（キー割り当て、敵数、報酬量、upgrade 回数）の最終決定
- playtest 実施手順そのものの定義
- Vertical Slice / Release Candidate / 配布ポリシーの定義

## Downstream Boundaries

| 委譲先 | 委譲内容 |
|---|---|
| `docs/product/game-logic.md` | 状態遷移、勝敗条件、保存境界、timer 初期値など implementation-facing rule |
| `docs/product/game-design.md` | 画面、ループ、報酬体験の輪郭 |
| `docs/product/playtest-protocol.md` | 仮説の観測手順、playtest-log、spec delta 連携 |
| implementation issue | 具体コード変更、テスト、PR |

## MVP Tunable Parameters

- `sortie_timer_initial`: `120s` は `docs/product/game-logic.md` 由来の draft test parameter として扱う。`status: accepted` までは fixed normative constant ではない
- timer を扱う implementation は hard-code ではなく configurable value とし、後続 playtest の spec delta 対象にできること
- enemy count / outpost HP / reward amount / upgrade count は本書では固定しない
- 軽量マクロ指示は MVP test parameter として最大 1 種類まで含め得る。具体キー・UI・command taxonomy は `game-logic.md` と `playtest-protocol.md` に委譲する

## Open Questions

- sortie timer の初期値を 120s 以外へ spec delta すべき signal を何にするか
- enemy count / outpost HP / reward amount / upgrade count の初期仮置きをどの issue で固定するか
- 軽量マクロ指示 1 種を採る場合、どの command intent を最優先にするか
- Human Product Review で `status: accepted` に上げるための最低観測証拠を何にするか

## Playtest Handoff

playtest では少なくとも以下を観測対象にする:

- HYP-MVP-001: プレイヤーが介入の因果を言語化できるか
- HYP-MVP-002: 成長ループを「敵技術の取り込み」と読めるか
- HYP-MVP-003: 色以外の視覚要素で戦況を把握できるか
- timer / enemy count / reward framing が spec delta を要するほど不適切か

本書は playtest 結果を直接実装へ流さず、`product-spec-lifecycle.md` の spec delta flow を経由して更新する。

## Trace Links

- `docs/product/requirements.md`
  - `MVP-003` — Combat MVP の短時間 sortie と局所介入
  - `MVP-004` — データ駆動定義と保存境界
- `docs/product/game-thesis.md`
  - `HYP-001-ace-intervention`
  - `HYP-002-tech-extraction-loop`
- `docs/product/game-design.md` — draft の補助参照
- `docs/product/game-logic.md` — draft の implementation-facing reference
- `docs/dev/product-spec-lifecycle.md`
- `docs/adr/0002-sdd-tool-adoption.md`
- GitHub Issue `#254`
- GitHub Issue `#285`
