---
doc_id: unit-operations-and-npc-behavior
feature_id: unit-operations-and-npc-behavior
title: Unit Operations and NPC Behavior
status: draft
does_not_close: "#975"
unblocks: []
accepted_scope: "research-only SSOT。runtime implementation は後続 issue へ deferred。本書は draft であり、単独で #975 を close しない。"
issue: "#975"
related_issue: "#975"
parent_issue: "none"
change_kind: research-only
primary_artifact: docs/product/features/unit-operations-and-npc-behavior.md
acceptance:
  - "AC1: artifact が YAML frontmatter 付きで作成され feature_id / status / related_issue / acceptance / non_goals / related_tests を含む"
  - "AC2: unit taxonomy と Faction / UnitRole / CommandIntent / NpcBehaviorState / TargetingPolicy 定義を含む"
  - "AC3: fixed tick 内の update order と deterministic tie-break（current runSortieSimulationStep との差分付き NPC tick schedule）を含む"
  - "AC4: 通常 UI / debug UI の情報境界（Canvas cue + DOM/focusable control）を含む"
  - "AC5: playtest measurement（勝因がプレイヤー介入に見えるかの観測方法）を含む"
  - "AC6: Roadmap Boundary（milestone #2 と repo roadmap の境界整合方針）を含む"
  - "AC10: Current Runtime Compatibility Matrix を含む"
  - "AC11: NPC tick schedule（current / target order 差分付き）を含む"
  - "AC12: assist_player MVP semantics 契約を含む"
  - "AC13: UI Surface Contract を含む"
  - "AC14: #727 rewrite/replace only の明記を含む"
  - "AC15: Playtest falsification signals が HYP-001 と接続されている"
  - "AC16: Evidence/Distribution Policy を含む"
  - "AC17: 8 段階 follow-up 分解を含む"
non_goals:
  - "実装コード変更（src/ / tests/）"
  - "pathfinding / navmesh / large squad formation / base building / territory"
  - "network / multiplayer / full behavior tree / GOAP"
  - "複数 command intent の同時実装"
  - "高品質アセット前提の演出"
related_tests: []
trace_links:
  self_issue: "#975"
  upstream_specs:
    - docs/product/requirements.md
    - docs/product/game-thesis.md
    - docs/product/game-design.md
    - docs/product/features/ui-information-architecture.md
    - docs/product/features/combat-core.md
    - docs/product/playable-roadmap.md
    - docs/dev/current-focus.md
  related_issues:
    - "#727"
    - "#785"
    - "#788"
    - "#800"
  related_prs:
    - "#803"
    - "#978"
runtime_verification_applicability:
  applicability: deferred
  decision: deferred
  reason: "research-only / docs-only artifact。artifact existence と grep checks のみ本 PR で applicable。runtime behavior verification は downstream implementation issues へ deferred。"
  deferred_until: "後続 implementation issue が runtime 状態を導入した時点"
---

# Unit Operations and NPC Behavior（運用モデルと NPC 行動契約）

本書は、RTS/Z 指示 mode の実装に先立ち、味方機体・敵機体・player-controlled entity・neutral entity の運用、command intent、targeting、AI state、deterministic update order、UI / debug surface 境界、playtest measurement を **product SSOT** として正本化する research-only artifact である。

本書は `change_kind: research-only` であり、runtime 実装（`src/` / `tests/`）を一切行わない。後続 implementation Issue が安全に起票できる粒度まで契約を固定することを目的とする。

---

## 1. Authority / SSOT Hierarchy

本書が他 docs と矛盾した場合、以下の優先順位で解決する（上が優先）。本書はこの hierarchy の下位 spec であり、上位を上書きしない。

1. `docs/product/requirements.md` — 全体要件と Global Non-Goals の正本。
2. `docs/product/game-thesis.md` — design hypothesis（HYP-001 等）と player promise の正本。
3. `docs/product/game-design.md` — pillar / 体験設計の正本。
4. `docs/product/features/ui-information-architecture.md` — Canvas / DOM / debug surface 境界と numeric/readability/evidence policy の正本（accepted, `#800`）。
5. `docs/product/features/combat-core.md` — `EnemyState` / collision / damage / deterministic order の正本（accepted, `#484`）。
6. `docs/product/playable-roadmap.md` — conceptual roadmap（M1〜M5）の正本。GitHub Milestone object の正本ではない。
7. `docs/dev/current-focus.md` — 現在フェーズと一時的優先順位（現在 `M3: Result Persistence (v0.3.x)`）。
8. Issue `#975` — 本書の contract / acceptance の起点。

衝突時の扱い:

- 本書の taxonomy / enum と `combat-core.md` の `EnemyState` が衝突する場合、`combat-core.md` を優先し、本書は既存契約を **上書きせず拡張提案**として記述する。
- 本書の UI 記述と `ui-information-architecture.md` が衝突する場合、`ui-information-architecture.md` を優先する。
- 本書はいずれの上位 SSOT も runtime mutation で書き換えない。

---

## 2. Roadmap Boundary

- **This issue does not create or mutate GitHub Milestone objects.**
- "M2" refers only to the conceptual roadmap section in `docs/product/playable-roadmap.md`（`### M2: Gameplay Core (v0.2.x)`）。これは conceptual roadmap であり、GitHub Milestone object（GitHub API による milestone 作成・更新）ではない。
- Runtime implementation belongs to follow-up issues after this SSOT is accepted.
- 現在フェーズは `docs/dev/current-focus.md` の `M3: Result Persistence (v0.3.x)` である。本書の taxonomy / NPC 行動契約は M2 conceptual scope（gameplay core）に属する設計であり、本 Issue では SSOT 確定のみを行い、runtime 着手の優先順位は current-focus に従う。
- GitHub Milestone object が必要になった場合は、別 Issue（`change_kind: github-metadata`）として分離する。本 `research-only` contract に混ぜない。

---

## 3. RTS Boundary

- "Z指示" is a lightweight macro intent, not a full RTS command system.
- No multi-select, command queue, squad formation, pathfinding, navmesh, base building, or territory control.
- MVP supports at most one player command intent: `assist_player`.
- The default key binding may be Z-equivalent, but product SSOT defines `CommandIntent`, not a physical key（binding-agnostic）。

この境界は `game-thesis.md` HYP-001 と整合する。HYP-001 は、プレイヤーを「大規模軍勢のマクロ運用」ではなく **局所介入するエース機**として定義し、`Zキー相当` を「拠点防衛 / 突出 / 退避 等」で味方挙動を誘導する程度の **軽量なマクロ指示**に留めている。本書はこの制約を破る full RTS 拡張を product 非ゴールとして固定する。

---

## 4. Unit Taxonomy

ゲーム内 entity を以下に分類する。

| 分類 | 説明 | commandability | MVP での扱い |
| --- | --- | --- | --- |
| player-controlled entity | プレイヤーが直接操作するエース機。`GameState.player`。 | 直接操作（move / aim / fire） | 既存 runtime。command の発信源であり NPC ではない。 |
| ally（味方機体） | 味方 NPC。半自律で行動する。 | semi-autonomous（直接命令しない） | MVP で 1 種 `ally_basic` を導入予定（runtime は後続 Issue）。 |
| enemy（敵機体） | 敵 NPC。 | pure NPC | 既存 `enemy_chaser` のみ。taxonomy へ非破壊移行。 |
| neutral | 中立 entity（障害物・無所属）。 | pure NPC（非交戦） | MVP では objective / obstacle として最小限。 |
| objective / obstacle | 防衛/破壊対象、または移動を妨げる静的 entity。 | 非操作 | MVP は省略可。taxonomy 上のみ予約。 |

**MVP 既定方針**:

- 味方は **semi-autonomous ally NPC** とする。プレイヤーは個別ユニットへ直接移動命令を出さない。
- プレイヤーが発信できる command intent は **`CommandIntent.assist_player` の 1 種のみ**とする。
- 別案（commandable 直接操作 / 複数 intent）を採る場合は、本書を改訂し、downstream impact（pathfinding / formation / collision avoidance へのスコープ爆発リスク）を明記してから移行する。MVP では採用しない。

---

## 5. Type / Enum Contract

以下は **prose artifact としての型定義案**であり、runtime には追加しない。runtime 追加は follow-up `state/types` Issue（§13 / §14）の責務とする。各 enum は normal UI に raw 文字列を出してはならず、raw enum は debug panel 限定とする（§9）。

```ts
type Faction =
  | 'player'
  | 'ally'
  | 'enemy'
  | 'neutral'

type UnitRole =
  | 'ace_player'
  | 'ally_basic'
  | 'enemy_chaser'
  | 'objective'
  | 'neutral_obstacle'

type CommandIntent =
  | 'none'
  | 'assist_player'

type NpcBehaviorState =
  | 'inactive'
  | 'acquire_target'
  | 'move_to_engage'
  | 'attack'
  | 'retreat'
  | 'destroyed'

type TargetingPolicy =
  | 'focus_player'
  | 'assist_player_threat'
  | 'nearest_hostile'
  | 'ignore'
```

### 5.1 `Faction`

- **allowed values**: `player` / `ally` / `enemy` / `neutral`
- **semantics**: 交戦関係を決める所属。`enemy` は `player` / `ally` に敵対する。`neutral` は非交戦。
- **examples**: 自機 = `player`、味方僚機 = `ally`、追尾敵 = `enemy`、障害物 = `neutral`。
- **anti-examples**: 「弱い敵」を `neutral` にする（強さは faction ではない）。プレイヤーを `ally` にする。
- **non-goals**: 多陣営外交、陣営切替、寝返り。
- **UI**: normal UI で `Faction.enemy` のような raw enum を出さない。debug panel では出してよい。

### 5.2 `UnitRole`

- **allowed values**: `ace_player` / `ally_basic` / `enemy_chaser` / `objective` / `neutral_obstacle`
- **semantics**: 行動テンプレートを決める役割。`ace_player` は唯一の player-controlled、`enemy_chaser` は現行の直線追尾敵。
- **examples**: 現行 `enemy-basic` 定義 → `UnitRole.enemy_chaser`。MVP 味方 → `ally_basic`。
- **anti-examples**: 役割に HP 値を埋め込む（数値は `EnemyState` 側）。1 entity に複数 role を持たせる。
- **non-goals**: クラス分岐の階層化、role ごとの skill tree。
- **UI**: raw role 名は debug 限定。

### 5.3 `CommandIntent`

- **allowed values**: `none` / `assist_player`
- **semantics**: プレイヤーが発信する軽量マクロ意図。物理キーではなく意図を表す（binding-agnostic）。
- **examples**: Z-equivalent 押下 → `assist_player` を TTL 付きで buffer に積む。未入力 → `none`。
- **anti-examples**: `move_to(x,y)` / `select_unit(id)` / `queue([...])` のような RTS 命令を `CommandIntent` に足す。
- **non-goals**: 命令キュー、複数 intent 同時、個別ユニット選択。
- **UI**: normal UI は `Assist` 等の player-facing wording。raw `assist_player` は debug 限定。

### 5.4 `NpcBehaviorState`

- **allowed values**: `inactive` / `acquire_target` / `move_to_engage` / `attack` / `retreat` / `destroyed`
- **semantics**: NPC（ally / enemy）の行動状態機械。`destroyed` は `EnemyState.defeated` に対応。
- **examples**: spawn 直後 `inactive` → target 取得で `acquire_target` → 接近 `move_to_engage` → 射程内 `attack`。
- **anti-examples**: behavior tree / GOAP ノードを state に混ぜる。state に座標を持たせる。
- **non-goals**: full behavior tree、感情/士気モデル。
- **UI**: `NpcBehaviorState: acquire_target` を HUD に出すのは禁止。debug panel 限定。

### 5.5 `TargetingPolicy`

- **allowed values**: `focus_player` / `assist_player_threat` / `nearest_hostile` / `ignore`
- **semantics**: target 候補の scoring 方針。`enemy_chaser` は `focus_player`、`assist_player` 適用中の ally は `assist_player_threat`。
- **examples**: 通常 ally = `nearest_hostile`、`assist_player` TTL 中 = `assist_player_threat`、neutral = `ignore`。
- **anti-examples**: policy に距離しきい値の数値を直接埋める（scoring tuple 側で扱う）。
- **non-goals**: 確率的 target ばらつき（再現性を壊す）。
- **UI**: raw policy 名は debug 限定。

---

## 6. Current Runtime Compatibility Matrix

現行 runtime（`src/state/GameState.ts`, `src/systems/EnemyAISystem.ts`, `src/systems/EnemySpawnSystem.ts`, `src/systems/SortieSystem.ts`）には `Faction` / `UnitRole` / `NpcBehaviorState` / `TargetingPolicy` / ally collection / command intent buffer が **存在しない**。現行 `EnemyState` は `id / definitionId / hp / maxHp / x / y / radius / speedPxPerSec / contactDamage / defeated / defeatedAtTick` のみ（`combat-core.md` SSOT と一致）。現行 `EnemyAISystem` は非撃破 enemy を player 中心へ直線接近させる chaser AI のみ。`runEnemySpawnSystem` は enemies が空のとき `enemy-basic` を 1 体 spawn するのみ。`InputCommand` は `move` / `aim` / `fire` のみで CommandIntent は無い。

| Current runtime concept | Current source | Proposed taxonomy | Runtime gap | Downstream issue |
| --- | --- | --- | --- | --- |
| player | `GameState.player` | `Faction.player` / `UnitRole.ace_player` | command source only; not NPC | none |
| enemy-basic | `EnemyState` / `enemyDefinitions` | `Faction.enemy` / `UnitRole.enemy_chaser` | no `behaviorState` / `faction` / `role` field yet | enemy policy migration |
| enemy chase AI | `runEnemyAISystem`（直線追尾） | `TargetingPolicy.focus_player` + `NpcBehaviorState.move_to_engage/attack` | no behavior state machine; no targeting policy | targeting policy / enemy policy migration |
| allies | absent | `Faction.ally` / `UnitRole.ally_basic` | no runtime ally state exists | ally NPC MVP |
| command intent | absent（`InputCommand` = move/aim/fire のみ） | `CommandIntent.assist_player` | no input buffer / TTL | command intent input |
| targeting policy | absent（chaser のみ） | `TargetingPolicy` enum + scoring fn | no deterministic target scoring | targeting policy |
| tick loop | `runSortieSimulationStep` | §7 target NPC tick schedule | no CommandIntent sampling / no snapshot boundary | targeting policy / command intent input |

この表により、後続 implementation は現行 `GameState` を broad に壊さず、`EnemyState` 拡張 / `AllyState` 追加 / command buffer 追加を段階的に行える。

---

## 7. NPC Tick Schedule（current / target order 差分付き）

### 7.1 Current order（`src/systems/SortieSystem.ts` `runSortieSimulationStep`）

固定 60Hz の各ステップで以下を順に実行する（逐語順）:

1. `runMovementSystem(state, commands, fixedDeltaMs)`
2. `runEnemySpawnSystem(state)`
3. `runEnemyAISystem(state, fixedDeltaMs)`
4. `runCombatSystem(state, commands, fixedDeltaMs)`
5. `runProjectileSystem(state, commands, fixedDeltaMs)`
6. `runCollisionSystem(state)` → `pairs`
7. `resolveCombatCollisions(state, pairs)`
8. `runSortieSystem(state, fixedDeltaMs)`
9. `state.tick += 1` / `state.elapsedMs += fixedDeltaMs`

現行には command intent の sampling 段も、NPC target acquisition の snapshot boundary も存在しない。

### 7.2 Target order（NPC tick schedule）

NPC 行動と command intent を導入する際の目標順序:

1. **sample input commands into CommandIntent buffer**（入力を CommandIntent buffer へサンプリング）
2. update player movement / aim / fire command
3. spawn/despawn gate
4. expire or apply CommandIntent TTL
5. acquire NPC targets from a declared **snapshot boundary**（同一 tick 内で全 NPC が同じ state snapshot を読む）
6. update NPC movement / attack intent
7. update projectile movement
8. collision detection
9. combat resolution
10. sortie terminal evaluation
11. **increment tick**
12. render reads state only（描画は state を読むのみ。書き込まない）

### 7.3 Delta（current → target）

| 項目 | current | target | delta |
| --- | --- | --- | --- |
| command sampling | なし | step 1 で CommandIntent buffer 化 | 新規段を追加 |
| TTL 処理 | なし | step 4 で expire/apply | 新規 |
| target acquisition | chaser が毎回 player 直読 | step 5 で snapshot boundary 経由 | snapshot 境界を明示 |
| NPC movement | step 3（enemy のみ） | step 6（ally + enemy 統合） | ally 統合 |
| 順序 | movement→spawn→enemyAI→combat→projectile→collision | 上記 12 段 | 段の追加と再配置 |

**snapshot boundary**: step 5 では、その tick の player 位置 / aim / 既存 enemy 配置を固定 snapshot として読む。`assist_player` が入力された tick に味方 target が「現在 aim」を読むか「前 tick snapshot」を読むかで結果が変わるため、**step 5 で読む値はその tick の step 2 完了後の player state とし、NPC 同士は互いの同 tick 移動結果を読まない（全 NPC が同一 snapshot）**ことを契約とする。

- **migration strategy**: 一括変更ではなく段階移行。`command intent input` Issue で step 1/4 を追加 → `targeting policy` Issue で step 5 の snapshot boundary と scoring を追加 → `ally NPC MVP` で step 6 に ally を統合 → `enemy policy migration` で現 `runEnemyAISystem` を新 policy へ非破壊接続。
- **implementation issue owner**: §13 の `targeting policy` / `command intent input` / `ally NPC MVP` / `enemy policy migration`。

---

## 8. Targeting Policy / Deterministic Scoring

target 選定は pure function とし、同一入力に対し同一結果（再現可能）でなければならない。

### 8.1 candidate filter（候補フィルタ）

scoring 前に以下を除外する:

- defeated / destroyed units excluded（`EnemyState.defeated === true` を除外）
- same faction excluded unless explicitly protected objective（同 faction は除外。ただし保護対象 objective は例外）
- outside active arena excluded（active arena 外の entity を除外）
- **stale target** IDs cleared before scoring（既に存在しない entity を指す stale target ID は scoring 前にクリア）

### 8.2 tie-break

- 最終 tie-break は **monotonic `entityId ASC`** とする。
- これは `combat-core.md` の `compareCollisionPair`（projectile-enemy first, then player-enemy; `projectileId ASC` then `enemyId ASC`; player-enemy は `enemyId ASC`）の deterministic comparator 方針と揃える。

### 8.3 `assist_player` target score tuple（ally, `assist_player` 適用中）

降順/昇順を順に評価し、最初に差が付いた要素で決定する:

1. `commandIntentMatch` DESC（command intent に合致する候補を優先）
2. `threatToPlayer` DESC（player への脅威が高い候補を優先）
3. `distanceToPlayer` ASC（player に近い候補を優先）
4. `distanceToAlly` ASC（その ally に近い候補を優先）
5. `targetEntityId` ASC（最終 tie-break）

### 8.4 `enemy_chaser` target score tuple

1. `isPlayer` DESC（player を最優先 = 現行挙動の保存）
2. `distanceToPlayer` ASC
3. `targetEntityId` ASC（最終 tie-break）

`enemy_chaser` の上記は現行 `runEnemyAISystem` の「player 中心へ直線接近」と挙動互換である（migration で挙動を変えない）。

### 8.5 score 入力特徴量の定義（deterministic feature contract）

score tuple の比較順が deterministic でも、入力特徴量の算出が曖昧だと実装が分岐する。本書は各特徴量を以下に拘束する（具体式の選択は後続 issue で確定するが、性質はここで固定する）。

- **`threatToPlayer`**:
  - pure deterministic scalar でなければならない。
  - randomness / wall-clock time / render state / DOM state / frame-rate 依存の floating tolerance を使用してはならない。
  - MVP は次のいずれか 1 つを選び、`targeting policy` issue で runtime merge 前に採用式を明記する:
    1. inverse distance to player（player までの距離の逆数）
    2. projected time-to-contact（接触までの予測時間）
    3. fixed binary hostile-near-player flag（player 近傍の敵 = 1 の二値）
- **`commandIntentMatch`**: `assist_player` TTL がアクティブな間のみ 1、それ以外 0 の二値。確率や連続値にしない。
- **`distanceToPlayer` / `distanceToAlly`**: arena 座標上の Euclidean 距離（`Math.hypot`）。同一 fixedDeltaMs で再現可能。
- **`player's current engagement vector`**: §7 snapshot boundary の step 2 完了後 player state（aim / 速度）から導出する固定 snapshot とし、同 tick の NPC 移動結果を混ぜない。
- **`valid target`**: §8.1 candidate filter を通過し、かつ stale でない target ID を指すもの。

---

## 9. `assist_player` MVP Semantics

- **Source**: player command surface（プレイヤーの command affordance）。Z-equivalent は default mapping 候補であり product contract ではない（binding-agnostic）。
- **Duration / TTL contract**（単位・範囲・変換規則・禁止事項を本書で固定。MVP default 値のみ follow-up で確定）:
  - Runtime parameter name: `assistPlayerTtlTicks`
  - Unit: fixed ticks only。ms 入力は sampling 時に `ceil(ms / fixedDeltaMs)` で tick へ変換する。
  - Allowed range: `1 <= assistPlayerTtlTicks <= 180`
  - MVP default: `command intent input` issue で確定する。本 SSOT は per-frame wall-clock expiry を禁止する。
  - Expiry comparison: `currentTick < expiresAtTick` の間アクティブ（deterministic に expire）。
  - 具体 default 値のみ `command intent input` / `targeting policy` follow-up Issue で確定する（placeholder 値は埋めない）。
- **Effect**: allied NPC の target selection を、player 周辺の threat または player の engagement vector へ **bias** する（`TargetingPolicy.assist_player_threat`、§8.3）。これは "命令" ではなく "意図による target scoring bias" である。
- **It does not**:
  - directly move units（直接移動命令を出さない）
  - create formations（編隊を作らない）
  - enqueue commands（命令キューを作らない）
  - select individual allies（個別 ally を選択しない）
  - introduce pathfinding / navmesh
- **no-op conditions**（以下のいずれかで command は no-op）:
  - allied NPC が存在しない
  - valid target がない
  - player が combat phase でない
  - command buffer が expired（TTL 切れ）
- **DOM focusable affordance**: normal UI には player-facing wording を出す。raw `assist_player` は出さない。
- **Canvas visual cue**: non-authoritative cue のみ（target line / ally intent icon 等）。Canvas だけを command state の唯一表現にしない。
- **debug panel**: raw `CommandIntent`, TTL, candidate score, selected target id を出してよい。
- **normal UI wording**: `Assist` / `Covering you` / `No ally available` 等の player-facing 表現。raw enum 禁止。

---

## 10. UI Surface Contract

本節は `ui-information-architecture.md`（accepted, `#800`）の surface 境界を継承する。

- Canvas may show **non-authoritative visual cues**: target line, ally intent icon, danger/warning shape.
- DOM HUD must provide the **authoritative, focusable** player command affordance for `assist_player`.
- Canvas text or icon must not be the only representation of command state.
- Debug panel may expose raw `CommandIntent` / `NpcBehaviorState` / target score（debug raw 表示）。
- Normal UI must not expose raw enum labels（`ui-information-architecture.md` の forbidden raw player-facing labels に整合: raw internal state name 等は禁止）。
- Canvas-only interactive control 禁止（`ui-information-architecture.md`: "Canvas MUST NOT be the only representation of an interactive control"。player command は DOM control または 1:1 の focusable fallback を持つ）。
- 後続 UI implementation issue は `ui-information-architecture.md` の viewport / DPR / browser zoom / readability cases（viewport 1920x1080 / 1366x768 / 1280x720、browser zoom 100/125/150/200%、DPR 1/1.25/2）を継承する。

### 10.1 DOM command affordance requirements（HUD/Canvas integration stage AC）

`<canvas>` は単なる bitmap であり描画オブジェクトの semantic information を accessibility tools へ露出しない（MDN）。したがって command affordance は以下を満たす:

- `assist_player` must have a real DOM control or one-to-one focusable fallback.
- It must be operable by keyboard and pointer.
- It must expose a player-facing accessible name, not raw `assist_player`.
- No-op state must be communicated in DOM text or ARIA status, not only Canvas cue.
- Canvas cue may duplicate state visually but must not be the sole state representation.

### 10.2 UI implementation verification cases（downstream pass/fail gate）

後続 UI/visual cue 実装 issue は、継承するだけでなく次を pass/fail として検証する:

- viewport: 1920x1080, 1366x768, 1280x720
- browser zoom: 100%, 125%, 150%, 200%
- devicePixelRatio: 1, 1.25, 2（`devicePixelRatio` は page zoom で変化するため DPR と browser zoom を同時記録する）
- major DOM HUD text: >= 18 CSS px at 1080p baseline（WCAG 2.2 Resize Text / Xbox Accessibility Guideline 101 整合）
- command affordance must remain reachable and understandable at 200% zoom（text scaling 200% で content/functionality を失わない）
- Canvas backing store must be tested against observed DPR, not assumed DPR.

---

## 11. #727 Handling

- **#727 must not be resumed as-is.**（#727 をそのまま resume してはならない）
- This issue may unblock a **replacement / rewrite** issue only.
- Replacement direction: **Canvas visual cues + DOM HUD** player status / command affordance integration.
- No Canvas aggregated player HP/HULL implementation is authorized by this issue.

#727 はもともと HP/HULL を Canvas に集約表示する方向だったが、`ui-information-architecture.md` がこの方向を supersede している。本書は #727 の rewrite/replacement のみを unblock し、Canvas 集約 HP/HULL 実装を authorize しない。

---

## 12. Playtest Measurement

本節は `game-thesis.md` の HYP-001（プレイヤーが「自分の介入で局所戦況が変わった」と感じられるか）と接続する **観測方法**である。

### 12.1 Playtest falsification signals（HYP-001 接続）

以下が観測された場合、HYP-001 は反証側に傾く:

- (a) Player **cannot explain** what `assist_player` did.（プレイヤーが assist_player が何をしたか説明できない）
- (b) Player attributes victory primarily to **allied AI randomness** rather than own intervention.（勝因を自分の介入でなく味方 AI 任せに帰着する）
- (c) Command use does not correlate with local threat removal or ally survival.（command 使用が局所脅威の排除や味方生存と相関しない）
- (d) Command feedback is noticed only as **debug text**, not as world/visual cue.（feedback が debug text としてしか認識されない）
- (e) Player asks for full RTS controls, indicating lightweight macro intent is underspecified.（プレイヤーが full RTS 操作を要求する = 軽量マクロ意図が未仕様）

### 12.2 Required evidence（measurement の観測項目）

- self-explanation prompt after sortie（sortie 後に誘導なしで「何が戦況を変えたと思うか」を語ってもらう。HYP-001 validation_method の self-explanation test）
- **command use count**（command 使用回数）
- **no-op count**（no-op 回数。before.*after.*local threat の文脈で測る）
- target switch count caused by command intent（command intent による target 切替回数）
- before / after local threat count（介入前後の局所脅威数）
- ally survival or protected-zone stability（味方生存 / 保護区域の安定）
- screenshot / video with viewport, DPR, browser zoom, userAgent, timezone, paused/running state

### 12.3 Playtest event schema（記録方法。playtest/evidence stage AC）

観測項目を解釈一致のもとで記録するため、最低限の event schema を定義する（具体 field 値は playtest/evidence issue で確定するが、形は本書で固定する）。

- `command_use`: `{ tick, intent: CommandIntent, accepted: boolean }`（command 発行ごとに 1 event。`command use count` は本 event の総数）
- `command_noop`: `{ tick, reason: 'no_ally' | 'no_target' | 'not_combat' | 'expired' }`（§9 no-op conditions のいずれか。`no-op count` は本 event の総数）
- `target_switch`: `{ tick, allyId, fromTargetId, toTargetId, causedByCommandIntent: boolean }`（`target switch count caused by command intent` は `causedByCommandIntent === true` の event 数）
- `local_threat_sample`: `{ tick, phase: 'before' | 'after', threatCount }`（`before / after local threat count` を同一 command の前後で記録）
- `ally_survival`: `{ sortieId, alliesSpawned, alliesSurvived, protectedZoneStable: boolean }`
- 各 event は deterministic（同一 replay で再現）であり、wall-clock を識別子に使わない。

---

## 13. Evidence / Distribution Policy

UI / playtest follow-up PR は、一時的な PR preview URL のみを証跡にしてはならない（`ui-information-architecture.md` Evidence Policy 継承）。

- **Do not use PR preview URL as the only evidence.**
- Required for UI / playtest follow-up PRs:
  - full **commit SHA**
  - **GitHub Actions run ID**
  - deployed **page_url** or stable **artifact** URL
  - artifact names
  - artifact digests or attestations when available
  - **retention-days**
  - live asset check result for JS/CSS/images
  - **viewport** / **DPR** / **browser zoom** / **userAgent** / **timezone**
  - **paused/running** state
  - screenshot or video path

### 13.1 docs-only PR の証跡境界

本 SSOT を作成する docs-only PR 自体には、上記 UI evidence は適用しない（矛盾回避）。

- This PR is docs-only. The PR preview URL is not acceptance evidence.
- docs-only PR の acceptance evidence は次に限定する: commit SHA / GitHub Actions run ID / docs lint / markdownlint / test command results。
- `actions/deploy-pages` の PR preview input は alpha で public 利用不可、`upload-pages-artifact` / `upload-artifact` の retention-days は 1〜90 日。これらは downstream UI/playtest PR で適用する。
- For downstream UI/playtest PRs, §13 Evidence / Distribution Policy is mandatory（artifact attestations で build provenance を検証可能にする）。

---

## 14. 8-Stage Follow-up Decomposition

後続 implementation Issue は以下の順序で起票する。runtime mutation は本 Issue の out of scope を維持する。

1. **docs-only**: `unit-operations-and-npc-behavior.md` SSOT accepted（本書）。
2. **state/types**: `Faction` / `UnitRole` / `CommandIntent` 型追加。ally runtime はまだ追加しない。
3. **command intent input**: binding-agnostic command intent buffer + Z-equivalent mapping。DOM/focusable fallback 必須。
4. **targeting policy**: deterministic target scoring pure function + unit tests。
5. **ally NPC MVP**: 1 種 `ally_basic` state と semi-autonomous behavior。
6. **enemy policy migration**: existing `enemy_chaser` を new taxonomy に対応。挙動は変えない。
7. **HUD/Canvas integration**: DOM command affordance + Canvas visual cue。#727 は rewrite/replacement として扱う。
8. **playtest/evidence**: self-explanation test + artifact/Pages evidence policy。

各 stage の Issue draft 本文（state/types / command intent input / targeting policy / ally NPC MVP / enemy policy migration / HUD/Canvas integration / playtest/evidence）は §13 evidence policy と本書の各 contract を Allowed Paths / AC の根拠として参照する。

### 14.1 enemy policy migration compatibility gate

stage 6（enemy policy migration）は既存 M2 runtime の terminal/collision timing を壊さないことを必須 gate とする:

- With no allies and `CommandIntent.none`, `enemy_chaser` movement must produce the same tick-by-tick enemy positions as current `runEnemyAISystem` for the same `fixedDeltaMs`.
- Existing sortie terminal timing must not change when no ally / no command intent is present.
- Add golden tick-trace tests for:
  - enemy position after N ticks
  - collision pair ordering（`compareCollisionPair` 順序の不変）
  - victory / defeat / timeout terminal tick
  - no-op command intent path
- 上記 golden test が緑である限りにおいてのみ tick schedule の §7 target order 再配置を merge できる。

---

## 15. Handoff Contract

- **Current Objective**: unit operation / NPC behavior / command intent の product SSOT を確定し、後続 implementation Issue 群が起票可能な状態にする。
- **Bounded Current Context**: 現 runtime は単一 chaser 敵 AI のみ（`runEnemyAISystem`）・ally / CommandIntent / TargetingPolicy 不在。`GameState` に ally collection / command intent buffer なし。現在フェーズは M3 Result Persistence。本書 taxonomy は M2 conceptual scope。
- **Open Questions**: `assist_player` の TTL 具体値（tick / ms）、ally_basic の HP / 速度パラメータ、command affordance の DOM 配置詳細 → いずれも後続 Issue で確定。
- **Next Action**: 本 PR は draft SSOT であり単独で #975 を close しない（frontmatter `does_not_close: "#975"`）。merge 後に §14 の 8-stage に沿って implementation Issue 群を起票し本 Issue（#975）に link、AC7 充足を確認したうえで #975 を close する。
- **Additional research requirement（AC7 escape hatch）**: follow-up issue の draft/link は本 PR 時点で未作成。merge 後の起票・link を残課題として明示する。
- **Artifact Refs**: `docs/product/features/unit-operations-and-npc-behavior.md`, `docs/product/playable-roadmap.md`（M2 conceptual）, #727, #785, #788, #800, `docs/product/requirements.md`, `docs/dev/current-focus.md`, `docs/product/game-thesis.md`。

---

## 16. Runtime Verification Applicability

- 本 Issue は docs-only / research-only のため runtime verification は **deferred / not_applicable**。
- artifact existence / grep checks（`test -f`, `rg`）はこの PR で applicable。
- runtime behavior verification（NPC 挙動 / targeting / command intent の動作）は downstream implementation issues へ deferred。
