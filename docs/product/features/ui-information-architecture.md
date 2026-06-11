---
doc_id: ui-information-architecture
status: draft
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

## Surface Taxonomy

| Surface | Role | Allowed | Forbidden |
|---|---|---|---|
| Canvas combat layer | world / danger / lock / damage の視覚提示 | actor silhouette、projectile、warning shape、near-diegetic indicator | raw debug state、primary text-control surface、internal state name |
| DOM HUD overlay | 通常プレイの readable status と command | player HP/HULL summary、ammo summary、ally command、minimal minimap、pause affordance | telemetry dump、LoopPhase、raw reward state、developer-only toggle |
| Menu overlay | flow transition / interruption | preparation、pause、settings、debrief、loadout | live combat telemetry、developer inspection |
| Debug panel | development / evidence / exact inspection | LoopPhase、telemetry、exact HP、raw reward state、evidence metadata | default-on exposure、normal-play 必須情報の代替 |

## Normal / Debug Vocabulary Boundary

### Normal play UI

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

## Phase Matrix

| phase | player intent | always visible | conditional | debug-only | hidden | surface | priority | follow-up issue |
|---|---|---|---|---|---|---|---|---|
| preparation | 出撃前に loadout と目的を確認する | mission objective、primary command affordance | resource summary、loadout delta | telemetry bootstrap、spec evidence metadata | enemy exact HP、LoopPhase | menu overlay + DOM HUD overlay | high | #785 |
| combat | 戦況を読み局所介入する | player HP/HULL summary、ammo / magazine summary、damage warning、lock warning | ally command、minimap、enemy HP summary | LoopPhase、telemetry、exact HP/HULL | Claim reward、raw reward state | Canvas combat layer + DOM HUD overlay | highest | #727 |
| debrief | 結果を理解し次の判断へ進む | outcome summary、resource summary、next action | equipment delta、performance highlight | raw extraction state、telemetry snapshot | live combat warnings | menu overlay | high | #785 |
| menu | 設定や一時停止を操作する | pause / settings / resume affordance | evidence link、control reminder | exact system state | live combat-only cue | menu overlay | medium | pause-resume follow-up |
| debug | 開発・検証のため内部状態を読む | none | developer toggle が有効なときのみ panel 表示 | player HP/HULL exact、enemy HP exact、telemetry、LoopPhase、pause/debug evidence、raw reward state | player-facing simplified wording | debug panel | medium | pause-resume follow-up |

## Information Classification

| information | always visible | conditional | debug-only | hidden | default surface |
|---|---|---|---|---|---|
| player HP/HULL | combat | preparation / debrief summarized | exact numeric detail | none | DOM HUD overlay |
| enemy HP | none | combat only when target context requires summary | exact numeric detail | preparation / menu | Canvas cue + optional DOM summary |
| ammo / magazine | combat | preparation summary | exact reload timers | debrief | DOM HUD overlay |
| damage warning | combat | none | source telemetry | menu / debrief | Canvas combat layer |
| lock warning | combat | none | lock-state detail | menu / debrief | Canvas combat layer |
| ally command | combat when commandable | preparation command preview | AI state trace | debrief | DOM HUD overlay |
| minimap | none | combat when space/readability permits | pathing trace | debug-off menu | DOM HUD overlay |
| resource | debrief / preparation | combat only if immediately actionable | raw reward pipeline | hidden during core combat | menu overlay |
| telemetry | none | none | debug only | normal play | debug panel |
| Claim reward | none | none | raw action label only | normal play | debug panel |
| LoopPhase | none | none | debug only | normal play | debug panel |
| pause/debug evidence | none | pause or debug enabled | full metadata | combat default | menu overlay + debug panel |

## Readability and Viewport Policy

- Supported minimum viewport を下回る場合は、情報の追加表示ではなく conditional HUD を先に縮退させる。
- 1080p baseline readability を維持し、major HUD text は collision なく読めることを優先する。
- DPR 固定前提で設計しない。page zoom、browser zoom、OS scaling を含む実測 viewport / DPR で evidence を残す。
- HUD safe margin を持ち、画面端 clamp により text / icon が arena 外へはみ出さないようにする。
- Overlay collision policy は次の順序で縮退する: `minimap` -> `resource secondary info` -> `enemy numeric summary`。`player HP/HULL` と critical warning は最後まで残す。
- Space 不足時に normal UI が exact numeric detail や raw internal labels へ退避してはならない。

## Evidence Policy

- UI review と playtest evidence は PR preview の一時 URL のみへ依存しない。
- stable artifact URL または安定した GitHub Pages URL を残す。
- evidence には full commit SHA、viewport、DPR、userAgent、timezone、build metadata、paused/running state を含める。
- screenshot または video reference を issue / PR から辿れる形で残す。
- pause surface がある場合、evidence capture は pause 状態での可読性確認を優先する。

## Routing Map

| Target | Routing |
|---|---|
| `#785` | 本書を前提に combat UI information architecture と HP/HULL display policy の research を再開する。phase matrix と vocabulary boundary を下流具体化する。 |
| `#727` | Canvas / DOM / menu / debug の責務境界、viewport / DPR / readability policy が本書で固定された後に deferred implementation を再開する。 |
| `#788` | integer-only / no-fractional / no-`<1` の通常 UI policy を formatter / tests / docs に適用する narrow implementation として継続する。 |
| pause-resume follow-up | pause / resume surface、Esc affordance、evidence panel、simulation stop boundary を別 follow-up で定義・実装する。 |

## Downstream Notes

- `docs/product/game-design.md` は画面役割集合を保持し、本書を UI 情報提示の下流正本として参照する。
- `docs/product/features/combat-core.md` の combat numeric display policy は、通常 UI の語彙境界について本書に従う。
- 実装定数、font px、exact safe margin 値、具体レイアウト座標は後続 implementation issue で確定する。

## Acceptance

- primary artifact として本書が存在する。
- normal play UI と debug surface の境界が明文化されている。
- phase matrix、surface taxonomy、routing map が存在する。
- viewport / DPR / readability / evidence policy が normal/debug boundary と整合している。
