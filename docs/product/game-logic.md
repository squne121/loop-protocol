---
status: draft
issue: "#287"
parent_issue: "#254"
doc_id: game-logic
trace_links:
  - docs/product/requirements.md
  - docs/product/game-thesis.md
  - docs/product/game-design.md
  - docs/adr/0001-architecture-baseline.md
  - docs/adr/0002-sdd-tool-adoption.md
  - docs/dev/product-spec-lifecycle.md
---

# ゲームロジック仕様 / Game Logic Specification

本書は LOOP_PROTOCOL のゲームロジックの正本である。状態遷移・入力・時間モデル・衝突判定・勝敗・保存境界を規定し、`src/state` / `src/systems` / `src/storage` 実装の参照仕様として機能する。具体的な実装定数（武器パラメータ・キーバインド・敵機数・報酬量）は Open Questions に退避する。

## 目的 / Intent

AI 実装者が state system / system update / collision detection / persistence の各層を実装する際の参照仕様を提供する。本書はプレイ体験（game-design.md）から実装可能性（architectural constraint ADR 0001）への橋渡しとなる。

## 正本階層 / Authority and Fallbacks

優先順位（上が強い）:

1. `docs/product/requirements.md` — 全体要件
2. `docs/product/game-thesis.md` — 受け入れ済み設計コンセプト（上位正本）
3. `docs/adr/0001-architecture-baseline.md` — 技術制約（state/render 分離・60Hz 固定タイムステップ）
4. 本書（`docs/product/game-logic.md`） — ゲームロジック仕様
5. `docs/product/game-design.md` — GDD-level design（補助参照のみ、`status: draft`）

## 要求 / Requirements

- **REQ-LOGIC-TIME-001:** Fixed timestep 60Hz (accumulator + panic clamp per ADR 0001).
- **REQ-LOGIC-TIME-002:** Catch-up clamp to max step count for determinism.
- **REQ-LOGIC-INPUT-001:** DOM events → semantic `InputCommand`. Concrete bindings are downstream.
- **REQ-LOGIC-COLLISION-001:** Broad-phase partition + narrow-phase overlaps. CCD for fast projectiles.
- **REQ-LOGIC-COLLISION-002:** Deterministic resolution order (seeded for replay).
- **REQ-LOGIC-VICTORY-001:** Sortie terminates on: player HP ≤ 0 (defeat), timer 120s (normal), enemy units destroyed, or outpost destroyed (victory).
- **REQ-LOGIC-PERSISTENCE-001:** Snapshot at debrief entry with schema version, tick count, campaign state via `src/storage`.

## 状態遷移 / State Transitions

### BattleState

```
pre-combat -> combat
combat -> defeat   [player_hp <= 0]
combat -> debrief  [sortie_timer_expired | enemy_outpost_destroyed | all_enemy_units_destroyed]
debrief -> pre-combat [next sortie]
defeat -> pre-combat  [retry or campaign continuation]
```

- **pre-combat**: 初期配置完了、シミュレーション未開始。player 入力受け入れ開始。
- **combat**: タイマー ≥ 0、敵機 > 0、player HP > 0。毎フレーム simulation step 実行。
- **debrief**: 戦闘終了条件達成。報酬計算・UI 更新。次出撃への導線。
- **defeat**: player HP ≤ 0 で即座に遷移。敗北終端状態。

### CampaignState

campaign 層の持続状態：unlocked_resources, upgraded_units, sortie_count等。各 sortie 終了時（debrief）に snapshot。

## 入力 / Input

DOM event (KeyboardEvent, PointerEvent) は input layer で capture し、以下に正規化：

```typescript
type InputCommand = 
  | { type: 'MoveIntent'; direction: Vec2; }   // normalized (-1..1)
  | { type: 'AimIntent'; angle: number; }      // radians
  | { type: 'FireIntent'; }
  | { type: 'IssueAllyCommandIntent'; command: string; }
  | { type: 'PauseIntent'; }
```

input layer は DOM 依存。`src/systems` は InputCommand を受け取り、simulation state update に反映。

## 時間モデル / Time Model

### Fixed Timestep (60Hz, 16.67ms)

システム更新は固定タイムステップ 60Hz。`requestAnimationFrame` はフレーム時間のみ供給し、simulation dt を駆動しない。

### Accumulator Pattern

```
accumulated_time += (current_frame_time - last_frame_time)
while (accumulated_time >= dt) {
  simulate(dt)
  accumulated_time -= dt
}
render()
```

### Panic Clamp

catch-up step 数に上限を設定。例：最大 5 step の catch-up のみ許可。6 step 以上必要な場合は頭を切る（遅延は避け、determinism と safety を優先）。

### Tick Authority

- Simulation time is integer `tick` count (authoritative).
- Render may interpolate using `alpha = accumulator / dt` (no state mutation).

## 衝突 / Collision

### Broad-phase

空間分割（grid または quadtree）で candidate pair を抽出。

### Narrow-phase

overlapping AABB の各 pair について circle-circle または circle-polygon overlap test。

### Continuous Collision Detection (CCD)

projectile が 1 tick で移動距離 > collider radius の場合、swept circle collision。高速弾の誤り抜けを防止。

### Deterministic Resolution Order

Collision pairs は canonical sort（entity id / collision_kind priority / projectile_id）で正規化。Broad-phase enumeration order は最終順を影響しない。Replay 用に snapshot 内に `rng_seed`, `rng_algorithm`, `tick`, `input_log`, `outcome_cause` を保持。

## 勝敗 / Victory, Defeat, Draw

### Defeat 条件

player-controlled entity の combat HP が 0 以下 → 即座に defeat state へ遷移。

### 戦闘終了トリガ（いずれか先に成立）

1. **120 秒の sortie timer 満了** → 通常戦闘終了（sortie timer ≥ 120s）
2. **敵拠点破壊** → 敵陣営の designated outpost destroyed → 戦闘終了（victory 扱い）
3. **敵機殲滅** → enemy unit count = 0 → 戦闘終了（victory 扱い）

### Draw

MVP では定義しない。timer 満了は通常終了として扱い、敗北ではない。

### 報酬差分

victory / normal-end / defeat による報酬差は Open Questions に退避。

### Termination Resolution

同 tick で複数原因成立時の outcome は優先順位で決定: 1) player_hp_zero→defeat, 2) enemy_outpost_destroyed→victory, 3) all_enemy_units_destroyed→victory, 4) sortie_timer_expired→normal_end。`causes[]` には全成立原因を deterministic order で保持。

## 保存境界 / Persistence Boundary

### Snapshot Point

sortie 終了時（debrief entry）に game state を snapshot。

### Snapshot Content

```typescript
type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

type BattleOutcome = 'victory' | 'defeat' | 'normal_end';

type BattleOutcomeCause =
  | 'player_hp_zero'
  | 'sortie_timer_expired'
  | 'enemy_outpost_destroyed'
  | 'all_enemy_units_destroyed';

interface SnapshotV1 {
  schema_version: 1;
  created_at_tick: number;
  campaign_state: CampaignStateSnapshot;
  battle_result: {
    outcome: BattleOutcome;
    primary_cause: BattleOutcomeCause;
    causes: BattleOutcomeCause[];
    sortie_duration_tick: number;
  };
  entity_states: EntitySnapshot[];
  extensions?: Record<string, JsonValue>;
}
```

### Entity ID-based Persistence

各 entity は stable id を持ち、snapshot/restore 時に identity を保証。

### Constraints

- JSON-serializable only (no functions / DOM / Map).
- Async interface for future IndexedDB migration.
- Debrief entry only (no per-tick saves).

## 非ゴール / Non-Goals

- 外部 physics engine の導入（独自実装）
- Z 軸立体物理（top-down 2D）
- hitscan 兵器（projectile ベース）
- 高精度 CCD（swept sphere; 円形で十分）
- ネットワーク同期

## 下流境界 / Downstream Boundaries

### `src/state`

Pure data model (no side effects).

### `src/input`

Keyboard / Pointer events → `InputCommand`. Simulation does not consume DOM.

### `src/data`

Weapon, unit, projectile, reward params. Concrete tunables are downstream.

### `src/render`

Canvas 描画（read-only state）。

### `src/systems`

Simulation update（state mutation）。

### `src/storage`

Persistence boundary（snapshot）。

### Key Invariant

- state mutation は `src/systems` のみ
- render は state read-only
- 描画フレーム率と simulation tick rate は独立

## 未解決の問い / Open Questions

- 具体的な武器パラメータ（fire rate / projectile speed / damage / ammo）
- キーバインド（WASD / space / Z など）
- 敵拠点 HP（複数ダメージで破壊されるか、即破壊か）
- 敵機数（初期配置）
- 報酬量（victory / normal-end / defeat での経験値・リソース差）
- sortie timer の正確な秒数（120s は試案）
- 味方 AI 行動ツリーの詳細（別 Issue / spec）

## 検証 / Verification Notes

記述正確性（AC1〜AC9）のみ本 Issue scope。実装 Issue で state transition / collision / persistence test を実施。
