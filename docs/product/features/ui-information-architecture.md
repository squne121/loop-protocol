---
doc_id: ui-information-architecture
status: accepted
issue: "#800"
trace_links:
  - "#785"
  - "#727"
  - "#788"
  - "docs/product/requirements.md"
  - "docs/product/game-design.md"
  - "docs/product/game-thesis.md"
  - "docs/product/features/combat-core.md"
  - "docs/adr/0001-architecture-baseline.md"
related_tests: []
---

# UI Information Architecture

## Intent

本書は通常プレイ UI と debug surface の境界、phase-aware 情報提示、1 画面内 HUD 方針、surface taxonomy、numeric/readability/evidence policy を定義する product SSOT である。`#785` の combat UI research、`#727` の deferred implementation、`#788` の narrow numeric-policy implementation は、本書を前提として再開または継続する。

## Scope Boundary

- 対象 phase は `preparation` / `combat` / `debrief` / `menu` / `debug` とする。
- 「1 画面 UI」は、主要な player-facing 情報を単一の play surface に収める方針を指す。Canvas と DOM の責務分離を破棄して「全部 Canvas に描く」ことは意味しない。
- `Canvas combat layer` は world 表示と visual cue に限定し、主要なテキスト制御面を担わない。
- `DOM HUD overlay` は player-facing status と操作 affordance を担う。
- exact internal state、telemetry、raw reward state、開発用 controls は `debug panel` に隔離する。
- 本書は docs-only architecture artifact であり、runtime 実装を直接変更しない。

## title / preparation / running / result / pause Mapping

- title screen: `title_menu` phase を `menu overlay` として扱い、`LoopPhase` は表示しない。開始導線（例: `出撃開始`、`編成確認`）は可読語彙で提示する。
- preparation: mission briefing、loadout、`ready` gating を `menu overlay + DOM HUD overlay` で統合する。`combat` 以外の生データ（LoopPhase 等）は非表示。
- running: combat 中の状態把握を `DOM HUD overlay` + `Canvas combat layer` で分担する。数値系は integer-only / non-fractional policy を維持し、`Claim reward` 等の内部語彙は非表示。
- result: `debrief` を結果確認（勝敗ラベル、報酬差分、次行動導線）として `menu overlay` に一本化する。`combat` 生データは overlay summary へ要約して差し替える。
- pause: `pause` は `productPause.isPaused` 起点の interruption overlay として定義し、Menu layer 上で `resume` / `settings` / `cancel` の最低操作を提供する。`LoopPhase` は pause 中の説明に露出しない。

## Surface Taxonomy

| Surface | Role | Allowed | Forbidden |
|---|---|---|---|
| Canvas combat layer | world / danger / lock / damage の視覚提示 | actor silhouette、projectile、warning shape、near-diegetic indicator | raw debug state、primary text-control surface、internal state name |
| DOM HUD overlay | 通常プレイの readable status と command | player HP/HULL summary、ammo summary、ally command、minimal minimap、pause affordance | telemetry dump、LoopPhase、raw reward state、developer-only toggle |
| Menu overlay | flow transition / interruption | preparation、pause、settings、debrief、loadout | live combat telemetry、developer inspection |
| Debug panel | development / evidence / exact inspection | LoopPhase、telemetry、exact HP、raw reward state、evidence metadata | default-on exposure、normal-play 必須情報の代替 |

## Interaction Accessibility Boundary

- Canvas MUST NOT be the only representation of an interactive control.
- player command、menu action、debug action は DOM control で実装するか、少なくとも 1 対 1 の focusable fallback を持たなければならない。
- Canvas text は non-authoritative な visual decoration または world cue に限定し、唯一の command surface として使ってはならない。

## Normal / Debug Vocabulary Boundary

### Forbidden raw player-facing labels

- `LoopPhase`
- `Telemetry`
- `Claim reward`
- `STATUS HULL`
- `reward pending`
- raw internal state name
- exact internal HP/HULL
- fractional HP/HULL
- `<1`

通常プレイでは上記を直接表示しない。必要な情報は world-facing な語彙、warning、icon、gauge、silhouette、damage cue、integer-only summary に変換する。

### Potential player-facing affordances

- `Continue`
- `Confirm result`
- `Return to preparation`

### Debug surface only

- exact combat values
- LoopPhase and lifecycle state
- telemetry counters
- raw reward pipeline state
- evidence capture metadata

## Numeric Display Policy

- Normal play UI は fractional HP/HULL と `<1` を表示しない。
- Normal play UI で numeric HP/HULL を出す場合、integer-only bucket に丸める。
- 優先表現は numeric exactness ではなく visual cue である。gauge、damage cue、warning shape、silhouette change、destruction state を優先する。
- exact values は debug panel にのみ表示してよい。
- `#788` はこの上位方針を通常 UI の formatter / tests / docs に適用する narrow implementation issue であり、本書の代替ではない。

## Compatibility Note

- 既存 runtime / docs では `formatCombatNumber` により `<1` が露出する箇所が残りうる。
- `#788` が完了するまで、この方針は runtime-enforced ではなく design-enforced として扱う。
- 通常 UI の implementation PR は、新たな `<1` / fractional HP/HULL の露出を追加してはならない。

## Phase Matrix

| phase | player intent | always visible | conditional | debug-only | hidden | surface | priority | follow-up issue | implementation state | blocked by |
|---|---|---|---|---|---|---|---|---|---|---|
| preparation | 出撃前に loadout と目的を確認する | mission objective、primary command affordance | resource summary、loadout delta | telemetry bootstrap、spec evidence metadata | enemy exact HP、LoopPhase | menu overlay + DOM HUD overlay | high | #785 | partial | none |
| combat | 戦況を読み局所介入する | player HP/HULL summary、damage warning、lock warning | ally command、minimap、enemy HP summary、ammo / magazine summary | LoopPhase、telemetry、exact HP/HULL | Claim reward、raw reward state | Canvas combat layer + DOM HUD overlay | highest | #727 | mixed | ammo/magazine state, #727 rewrite |
| debrief | 結果を理解し次の判断へ進む | outcome summary、resource summary、next action | equipment delta、performance highlight | raw extraction state、telemetry snapshot | live combat warnings | menu overlay | high | #785 | partial | none |
| menu | 設定や一時停止を操作する | pause / settings / resume affordance | evidence link、control reminder | exact system state | live combat-only cue | menu overlay | medium | pause-resume follow-up | blocked | pause/resume follow-up |
| debug | 開発・検証のため内部状態を読む | none | developer toggle が有効なときのみ panel 表示 | player HP/HULL exact、enemy HP exact、telemetry、LoopPhase、pause/debug evidence、raw reward state | player-facing simplified wording | debug panel | medium | pause-resume follow-up | blocked | debug panel policy implementation |

## Information Classification

| information | always visible | conditional | debug-only | hidden | default surface | implementation state | blocked by |
|---|---|---|---|---|---|---|---|
| player HP/HULL | combat | preparation / debrief summarized | exact numeric detail | none | DOM HUD overlay | existing but policy drift | #788 |
| enemy HP | none | combat only when target context requires summary | exact numeric detail | preparation / menu | Canvas cue + optional DOM summary | existing policy, needs alignment | #785 |
| ammo / magazine | none | combat when runtime state exists | exact reload timers | debrief | DOM HUD overlay | not yet backed by state | follow-up issue |
| damage warning | combat | none | source telemetry | menu / debrief | Canvas combat layer | partial | implementation follow-up |
| lock warning | combat | none | lock-state detail | menu / debrief | Canvas combat layer | partial | implementation follow-up |
| ally command | combat when commandable | preparation command preview | AI state trace | debrief | DOM HUD overlay | partial | implementation follow-up |
| minimap | none | combat when space/readability permits | pathing trace | debug-off menu | DOM HUD overlay | undecided | follow-up issue |
| resource | debrief / preparation | combat only if immediately actionable | raw reward pipeline | hidden during core combat | menu overlay | partial | implementation follow-up |
| telemetry | none | none | debug only | normal play | debug panel | debug-only by policy | debug panel implementation |
| Claim reward | none | none | raw action label only | normal play | debug panel | hidden in normal UI | reward follow-up |
| LoopPhase | none | none | debug only | normal play | debug panel | debug-only by policy | debug panel implementation |
| pause/debug evidence | none | pause or debug enabled | full metadata | combat default | menu overlay + debug panel | blocked | pause/resume follow-up |

## Routing Map

| Target | Routing |
|---|---|
| `#785` | 本書を前提に combat UI information architecture と HP/HULL display policy の research を再開する。phase matrix と vocabulary boundary を下流具体化する。 |
| `#727` | そのまま resume しない。本書は元の「Canvas aggregated player HP/HULL」方向を supersede するため、実装前に `Canvas visual cues + DOM HUD player status integration` へ rewrite または replace する。 |
| `#788` | integer-only / no-fractional / no-`<1` の通常 UI policy を formatter / tests / docs に適用する narrow implementation として継続する。 |
| pause-resume follow-up | pause / resume surface、Esc affordance、simulation freeze boundary、evidence label、paused 中の debug interaction を定義・実装する。 |

## Minimum Readability Targets

Baseline:
- 1920x1080 CSS viewport at 100% browser zoom.
- Major HUD text: 18 CSS px 以上 at 1080p baseline.
- Critical warning text/icon は 200% browser zoom でも可視であること。
- HUD safe margin の初期目標は 16 CSS px とし、実装 PR で調整理由を明示する。

Required verification cases for implementation PRs:
- viewport: 1920x1080, 1366x768, 1280x720
- browser zoom: 100%, 125%, 150%, 200%
- devicePixelRatio: 1, 1.25, 2

## Readability and Viewport Policy

- Supported minimum viewport を下回る場合は、情報の追加表示ではなく conditional HUD を先に縮退させる。
- 1080p baseline readability を維持し、major HUD text は collision なく読めることを優先する。
- DPR 固定前提で設計しない。page zoom、browser zoom、OS scaling を含む実測 viewport / DPR で evidence を残す。
- HUD safe margin を持ち、画面端 clamp により text / icon が arena 外へはみ出さないようにする。
- Overlay collision policy は次の順序で縮退する: `minimap` -> `resource secondary info` -> `enemy numeric summary`。`player HP/HULL` と critical warning は最後まで残す。
- Space 不足時に normal UI が exact numeric detail や raw internal labels へ退避してはならない。
- safe margin px、collapse threshold、font fallback は実装 PR の verification で最終確定するが、本書の最低検証条件を下回ってはならない。

## Evidence Policy

- UI review と playtest evidence は PR preview の一時 URL のみへ依存しない。
- stable artifact URL または安定した GitHub Pages URL を残す。
- evidence には full commit SHA、GitHub Actions run ID、page_url または stable artifact URL、artifact names、artifact digests、retention-days、live asset check result を含める。
- さらに viewport、DPR、browser zoom、userAgent、timezone、build metadata、paused/running state を含める。
- screenshot または video reference を issue / PR から辿れる形で残す。
- pause surface がある場合、evidence capture は pause 状態での可読性確認を優先する。

## Pause / Resume Requirement

- pause/resume は evidence capture の都合ではなく、single-player/local gameplay におけるアクセシビリティ要件として扱う。
- UI implementation が paused screenshot に依存する前に、pause input、simulation freeze behavior、overlay surface、paused 中の debug interaction、evidence state label を定義する follow-up issue を作成する。

## Handoff

Current Objective:
- `#727` / `#788` / pause-resume follow-up の再開前に、本書を product-level UI IA SSOT として使う。

Bounded Current Context:
- This artifact is docs-only.
- No runtime HUD, CanvasRenderer, HudController, pause/resume, or formatter implementation is changed in this PR.
- Existing runtime behavior may still diverge until `#727` rewrite and `#788` implementation are completed.

Open Questions:
- Supported minimum viewport の最終値。
- Minimum readable HUD font size の platform variance。
- Exact safe margin constants。
- Overflow/collapse thresholds。
- `#727` を rewrite するか replace するか。
- pause/resume follow-up の owner と scope。
- ammo/magazine state source と migration path。

Next Action:
- `#727` を `Canvas visual cues + DOM HUD responsibility split` に沿って rewrite または replace する。
- `#788` を通して normal/debug numeric policy を runtime formatter tests へ適用する。
- pause/resume follow-up issue を作成し、accessibility requirement と evidence capture prerequisite を定義する。

Artifact Refs:
- `docs/product/features/ui-information-architecture.md`
- `docs/product/game-design.md`
- `docs/product/features/combat-core.md`
- `#785`
- `#727`
- `#788`
- PR `#782`
- PR `#803`

## Runtime Verification Applicability

- decision: not_applicable
- reason: この artifact は docs-only architecture SSOT であり、runtime HUD / Canvas / formatter 実装を直接変更しないため。
- downstream_applicability:
  - `#727` rewrite/replacement implementation: applicable
  - `#788` numeric policy implementation: applicable
  - pause/resume follow-up implementation: applicable

## Downstream Notes

- `docs/product/game-design.md` は画面役割集合を保持し、本書を UI 情報提示の下流正本として参照する。
- `docs/product/features/combat-core.md` の combat numeric display policy は、通常 UI の語彙境界について本書に従う。
- 実装定数、font px、exact safe margin 値、具体レイアウト座標は後続 implementation issue で確定する。

## Acceptance

- primary artifact として本書が存在する。
- normal play UI と debug surface の境界が明文化されている。
- phase matrix、surface taxonomy、routing map が存在する。
- viewport / DPR / readability / evidence policy が normal/debug boundary と整合している。
- handoff と runtime verification applicability が docs-only artifact として明示されている。
