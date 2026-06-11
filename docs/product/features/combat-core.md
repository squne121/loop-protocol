---
doc_id: combat-core
status: accepted
issue: "#484"
parent_issue: "#483"
trace_links:
  - "#483"
  - "#484"
  - "#485"
  - "#486"
  - "#488"
  - "#489"
  - "docs/product/features/movement-projectile.md"
  - "docs/product/game-logic.md"
  - "docs/adr/0001-architecture-baseline.md"
  - "docs/product/playable-roadmap.md"
related_tests:
  - tests/collision-system.test.ts
  - tests/combat-system.test.ts
---

# Combat Core Feature Spec

## 目的 / Intent

本ドキュメントは M2 Combat MVP Gate の前提仕様として、以下の責務境界・型・ポリシーを正本として定義する。

- `CollisionSystem` と `CombatSystem` の責務境界
- `CollisionPair` 型定義
- `EnemyState` フィールド定義
- projectile hit semantics（single-hit / non-piercing）
- defeat policy（hp clamp、`defeated` / `defeatedAtTick` フラグ）
- deterministic order（同一 tick 内の処理順序）

本ドキュメントは **docs-only spec** であり、`src/` への実装変更は含まない。
実装は後続の child issues（#486〜#489 等）で行う。

---

## Aim Indicator — Visual-Only Invariants（Issue #580）

aim indicator は **visual-only** な描画要素であり、以下の不変条件が成立する:

### Tracking Target

aim indicator は `PlayerState.aimX` / `aimY` をマウスカーソル位置として追跡する。
`InputBindings.ts` の `pointermove` および `pointerdown` ハンドラが `activePointerId` 依存なしに常時 `pointerX` / `pointerY` を更新し、
`pointerKnown = true` を設定する。
`CanvasRenderer` は **毎フレーム `aimX/aimY` から方向ベクトルを計算する**（`dist > AIM_EPSILON_PX` の場合）。
`lastAimDirectionX/Y`（`CombatSystem` が fire 時に正規化して保持）は、ポインターがプレイヤー位置に重なる場合（`dist <= AIM_EPSILON_PX`）の **fallback にのみ** 使用する。

### Fixed Length

aim indicator の描画長は `AIM_INDICATOR_LENGTH_PX`（= 60 px）定数で固定する。
`lineTo(aimX, aimY)` のような可変長描画は禁止。
方向ベクトルを正規化して `AIM_INDICATOR_LENGTH_PX` を乗じた端点へ `lineTo` する。

### Draw Layer

描画レイヤー順序は以下の通り:

1. background（fill）
2. grid
3. **aim indicator**（← この層）
4. player
5. enemies
6. projectiles
7. HUD text
8. Victory / Defeat overlay

aim indicator は background / grid より上、actors / projectiles より下に描画する。
`context.save()` / `context.restore()` で描画状態を隔離し、周辺レイヤーに strokeStyle / lineWidth の副作用を及ぼさない。

### No Hitbox

aim indicator はヒットボックスを持たない。
- `CollisionSystem` の衝突判定対象ではない
- `CombatSystem` の damage 処理対象ではない
- `GameState` へのエンティティ追加なし

### Fallback Behavior

`pointerKnown === false`（ポインターがまだ canvas に入っていない）の間は:
- `InputMapper` が aim command を送出しない（`CombatSystem` に aim 座標が伝達されない）
- aim indicator は `lastAimDirectionX/Y` の初期値（`0, 0`）に基づき右方向フォールバックで描画される

`pointerKnown` は `InputState` に保持されるフラグであり、最初の `pointermove` で `true` になる。

---

## M2 範囲

M2（v0.2.x）の Combat 最小仕様は以下を含む:

- enemy の spawning、circle hitbox、hp/damage、defeat
- `projectile-enemy` 衝突と `player-enemy` 接触ダメージ
- 1 sortie を開始→操作→戦闘結果（kills カウント）まで通す

---

## Non-Goals

以下は本 spec および M2 の scope 外とする:

- campaign / story progression
- upgrade system / skill tree
- persistence（save/load、sortie result の永続化）
- audio（SE / BGM）
- network / multiplayer
- VFX（爆発エフェクト、パーティクル等）
- broad-phase collision（空間分割、BVH 等）— 後続 Issue に defer
- CCD（Continuous Collision Detection）— 後続 Issue に defer
- balance tuning（HP 値・ダメージ値の最終調整）
- DOM / Canvas の直接操作（systems 層からは禁止）

---

## EnemyDefinition と EnemyState

### EnemyDefinition（テンプレート）

```typescript
// EnemyDefinition（src/data/enemies.ts で定義）
type EnemyDefinition = {
  definitionId: string;     // archetype id（例: "enemy-basic"）
  maxHp: number;            // 最大 HP
  radius: number;           // circle hitbox 半径（px）
  speedPxPerSec: number;    // 移動速度（px/sec）
  contactDamage: number;    // 接触ダメージ（1 collision tick あたりの HP 減少量）
};
```

### EnemyState（ランタイム状態）

```typescript
// EnemyState（runtime instance）
type EnemyState = {
  id: number;               // monotonic spawn counter
  definitionId: string;     // 対応する EnemyDefinition.definitionId
  hp: number;               // 現在 HP（0 以上、maxHp 以下）
  maxHp: number;            // 最大 HP
  x: number;                // arena 座標 X（px）
  y: number;                // arena 座標 Y（px）
  radius: number;           // circle hitbox 半径（px）
  speedPxPerSec: number;    // 移動速度（px/sec）
  contactDamage: number;    // 接触ダメージ（1 collision tick あたりの HP 減少量、固定 60Hz タイムステップ前提）
  defeated: boolean;        // defeat 済みフラグ。true の場合、collision / damage 対象外
  defeatedAtTick: number | null; // defeat した tick 番号。未 defeat 時は null
};
```

### contactDamage の単位

`contactDamage` は **1 collision tick あたりの HP 減少量（HP decrease per tick）**（固定 60Hz タイムステップ前提）として定義する。

- 60Hz = 1 tick / 16.67ms
- `contactDamage = 1` は毎 tick 1 HP 減少（= 1 秒間に 60 HP 減少）
- DPS 換算: `dps = contactDamage * 60`

---

## Hitbox Model

M2 では **circle hitbox のみ** を使用する。

衝突条件:
```
distanceSq(a, b) <= (a.radius + b.radius)^2
```

ここで `distanceSq(a, b) = (a.x - b.x)^2 + (a.y - b.y)^2`

broad-phase（空間分割、BVH 等）および CCD（Continuous Collision Detection）は M2 の **non-goals** であり、後続 Issue に defer する。

M2 の衝突検出は narrow-phase のみ（`O(projectiles × enemies)` の circle 判定）で実装する。

---

## CollisionPair 型

```typescript
type CollisionKind = "projectile-enemy" | "player-enemy";

type CollisionPair = {
  kind: CollisionKind;
  tick: number;
  projectileId?: number;   // kind === "projectile-enemy" の場合に存在
  playerId?: string;       // kind === "player-enemy" の場合に存在
  enemyId: number;
  priorityKey: string;
  // Opaque dedupe/debug key. MUST NOT be parsed. MUST NOT be used for sorting.
  // For debug display and snapshot stability only; not a reversible serialization format.
  // Canonical format:
  // - projectile-enemy: `projectile-enemy-${projectileId}-${enemyId}`
  // - player-enemy: `player-enemy-${playerId}-${enemyId}`
};
```

---

## CollisionSystem Contract

> **M2 Migration Note**: 現行の `CollisionSystem.ts` は player boundary clamp / telemetry を担う void system である。M2 実装（Issue #488）では、boundary clamp 責務を `MovementSystem` または `BoundaryClampSystem` へ移管し、本仕様の pure CollisionPair[] producer として再実装する。現行 `CombatSystem.ts` は aim/fire/cooldown を担う system であり、M2 では collision damage 処理を担う `CombatResolutionSystem` を別途追加するか、既存 `CombatSystem` をリネーム・分割して対応する（#488 で実装判断を行う）。

### 責務

`CollisionSystem` は **circle hitbox 判定のみ** を行い、`CollisionPair[]` を返す。

入力: `GameState`
出力: sorted `CollisionPair[]`（後述 `compareCollisionPair` comparator 順）

### 禁止事項

`CollisionSystem` は以下を **一切変更してはならない**:

- HP / ダメージ計算
- projectile の削除
- defeat 判定・`defeated` / `defeatedAtTick` の設定
- sortie result / resource の変更
- persistence（save/load）の操作

`CollisionSystem` は **純粋な判定関数**として、入力に対して同じ出力を返す（副作用なし）。

### 実装イメージ

```typescript
function runCollisionSystem(state: GameState): CollisionPair[] {
  const pairs: CollisionPair[] = [];

  // projectile-enemy 衝突判定
  for (const projectile of state.projectiles) {
    for (const enemy of state.enemies) {
      if (enemy.defeated) continue;
      if (circleOverlap(projectile, enemy)) {
        pairs.push({
          kind: "projectile-enemy",
          tick: state.tick,
          projectileId: projectile.id,
          enemyId: enemy.id,
          priorityKey: `projectile-enemy-${projectile.id}-${enemy.id}`,
        });
      }
    }
  }

  // player-enemy 衝突判定
  for (const enemy of state.enemies) {
    if (enemy.defeated) continue;
    if (circleOverlap(state.player, enemy)) {
      pairs.push({
        kind: "player-enemy",
        tick: state.tick,
        playerId: state.player.id,
        enemyId: enemy.id,
        priorityKey: `player-enemy-${state.player.id}-${enemy.id}`,
      });
    }
  }

  return pairs.sort(compareCollisionPair);
}
```

---

## CombatSystem Contract

### 責務

`CombatSystem` は `CollisionPair[]` を消費し、以下の処理を担当する:

- enemy damage（projectile からのダメージ適用）
- player damage（enemy contact damage 適用）
- enemy defeat marker（`defeated = true`、`defeatedAtTick = <tick>`）
- projectile deletion（命中した projectile の削除）

入力: `GameState` + sorted `CollisionPair[]`
出力: 更新された `GameState`（イミュータブル更新 or 直接 mutate — 実装判断は実装 Issue に委譲）

### 禁止事項

`CombatSystem` は以下を **直接変更してはならない**:

- sortie result（`result.kills` 等）— `SortieSystem` の責務
- resource（スコア・currency 等）— 後続システムの責務
- persistence（save/load）— persistence 層の責務
- DOM / Canvas — render 層の責務

---

## Deterministic Order（同一 tick 処理順序）

同一 tick 内の衝突処理順序を以下のように定義する:

1. **`projectile-enemy` を先に処理**
2. 次に **`player-enemy` を処理**
3. 同種内は **id 昇順**:
   - `projectile-enemy`: `projectileId ASC`（数値比較）、同一 projectileId では `enemyId ASC`（数値比較）
   - `player-enemy`: `playerId ASC`（文字列比較）、同一 playerId では `enemyId ASC`（数値比較）

この順序により、同一 tick 内での処理結果が deterministic になる。

正規ソート順の **SSOT は以下の `compareCollisionPair` comparator** とする。`priorityKey` フィールドは重複排除用途に残すが、ソート順の決定には使用しない。

```typescript
function compareCollisionPair(a: CollisionPair, b: CollisionPair): number {
  // projectile-enemy を player-enemy より先に処理
  const kindRank = (p: CollisionPair) => (p.kind === "projectile-enemy" ? 0 : 1);
  const ak = kindRank(a);
  const bk = kindRank(b);
  if (ak !== bk) return ak - bk;

  if (a.kind === "projectile-enemy" && b.kind === "projectile-enemy") {
    if (a.projectileId !== b.projectileId) return (a.projectileId ?? 0) - (b.projectileId ?? 0);
    return a.enemyId - b.enemyId;
  }
  if (a.kind === "player-enemy" && b.kind === "player-enemy") {
    const playerCmp = (a.playerId ?? "").localeCompare(b.playerId ?? "");
    if (playerCmp !== 0) return playerCmp;
    return a.enemyId - b.enemyId;
  }
  return 0;
}
```

---

## Projectile Deletion Policy

projectile の衝突処理は **single-hit / non-piercing** として定義する:

- projectile は 1 体の enemy に命中した時点で即削除する
- 同一 tick に複数の enemy と衝突判定が成立した場合、**正規順序（projectileId ASC, enemyId ASC）の先頭 1 体のみ**にダメージを適用し、projectile を削除する
- piercing（貫通）は M2 の non-goal

---

## Defeat Policy

enemy の defeat 処理:

1. `hp <= 0` になった時点で `hp = 0` に clamp する
2. `defeated = true` を設定する
3. `defeatedAtTick = <現在の tick 番号>` を設定する
4. 以後、当該 enemy は collision / damage の対象外となる

`CombatSystem` は result / resource を **直接 mutate しない**。defeat のマーキング（`defeated = true`、`defeatedAtTick`）のみを担当する。

---

## SortieSystem Integration

`SortieSystem` は `defeatedAtTick !== null` のフィールドをもとに `result.kills` を集計する。

```typescript
// SortieSystem での kills 集計（概念イメージ）
const kills = state.enemies.filter(e => e.defeatedAtTick !== null).length;
result.kills = kills;
```

`CombatSystem` は `result` / `resource` を直接 mutate しない。`SortieSystem` が defeat marker を読み取り、sortie result を更新する責務を持つ。

---

## M2 Scope Exception（game-logic.md からの一時的逸脱）

上位仕様 `docs/product/game-logic.md` は broad-phase / CCD / swept circle を collision 要件に含む。
M2 ではこれらを **non-goals（後続 Issue に defer）** として一時的に除外する。

**No-tunneling envelope（M2 制約）:**
- projectile speed: 最大 ~520 px/s（60Hz では 1 tick あたり ~8.7 px 移動）
- projectile radius: 最小 4 px
- enemy radius: 最小 16 px

1 tick の移動量（~8.7px）は enemy radius（16px）を超えないため、
M2 で実装する enemy サイズの範囲では tunneling は発生しない。
この制約を超える high-speed projectile または small enemy を追加する場合は、
CCD（swept circle / segment-circle 判定）を先行 Issue として起票すること。

broad-phase / CCD は後続 Issue（#483 またはその sub-issue）で対応する。

---

## Acceptance Criteria（spec 内参照）

| AC | 内容 |
|----|------|
| AC1 | 本ファイルが存在する |
| AC2 | YAML frontmatter に `doc_id`、`status: accepted`、`issue: "#484"`、`parent_issue`、`trace_links` がある |
| AC3 | `trace_links` に `#483`、`#486`、`#488`、`#489`、`movement-projectile.md`、`game-logic.md`、`0001-architecture-baseline.md` が含まれる |
| AC4 | EnemyState の最小フィールドが定義されている（`id: number`、`definitionId: string`、`hp`、`maxHp`、`x`、`y`、`radius`、`speedPxPerSec`、`contactDamage`、`defeated`、`defeatedAtTick` を含む） |
| AC5 | CollisionSystem が circle hitbox 判定のみを行い、CollisionPair[] を返し、HP/削除/defeat/result/resource/persistence を変更しないことが明記されている |
| AC6 | CombatSystem が CollisionPair[] を消費し、enemy damage / player damage / defeat marker / projectile deletion を担当し、sortie result / resource / persistence を直接変更しないことが明記されている |
| AC7 | projectile-enemy 衝突が single-hit / non-piercing / `projectileId ASC, enemyId ASC` 優先順序で定義されている |
| AC8 | `contactDamage` が 1 collision tick あたりの HP 減少量（固定 60Hz タイムステップ前提）として明記されている |
| AC9 | defeat 時に hp=0 clamp + defeated=true + defeatedAtTick=<tick> が設定され、CombatSystem が result/resource を直接 mutate しないことが明記されている |
| AC10 | SortieSystem が `defeatedAtTick !== null` をもとに result.kills を集計することが明記されている |
| AC11 | M2 では narrow-phase のみ（O(projectiles × enemies) circle 判定）とし、broad-phase / CCD を non-goals または後続 Issue に defer することが明記されている |
| AC12 | 同一 tick 処理順序（projectile-enemy 先処理、次に player-enemy；同種内は id 昇順）が明記されている |
| AC13 | non-goals に campaign / upgrade / persistence / audio / network / VFX / broad-phase / CCD が含まれる |
| AC14 | `related_tests` に `tests/collision-system.test.ts` / `tests/combat-system.test.ts` が列挙されている |


---

## 戦闘数値表示ポリシー / Combat Numeric Display Policy (Issue #581, #726 で更新)

SSOT identifiers（正本識別子）: `number_display_policy`, `formatCombatNumber`（実装は `src/render/renderUtils.ts` を参照）。本セクションは HUD(DOM) と Canvas 敵 HP ラベルが共有する数値表示規則を定める。通常プレイ UI と debug surface の語彙境界、および player-facing numeric policy の上位正本は `docs/product/features/ui-information-architecture.md` とする。

### `number_display_policy`

```yaml
number_display_policy:
  hp:
    input_domain: finite non-negative number (integers or floats)
    policy:
      - condition: NaN / Infinity / negative
        output: "0"
      - condition: value === 0
        output: "0"
      - condition: 0 < value < 1
        output: "<1"
        rationale: "Avoid showing living unit as 0; critical for gameplay clarity"
      - condition: value >= 1
        output: "displayValue = Math.ceil(value); compact boundary is evaluated on displayValue"
        sub_rules:
          - "displayValue < 10000  -> String(displayValue)"
          - "displayValue >= 10000 -> existing compact (floor-based k/M)"
        rationale: "Ceil first absorbs float artifact (7.9999999 -> 8) AND keeps 9999.1 -> ceil 10000 -> 10k (not the 5-digit '10000')"
    examples:
      0: "0"
      0.5: "<1"
      1.5: "2"
      7.9999: "8"
      999: "999"
      9999: "9999"
      9999.1: "10k"
      10000: "10k"
      999999: "999k"
      1000000: "1M"
enemy_hp_label:
  anchor: "enemy center x, enemy top y - 8px"
  text_align: center
  text_baseline: middle
  x_clamp: "[padding + textWidth/2, arenaWidth - padding - textWidth/2]"
  y_clamp: "[fontSize/2 + padding, arenaHeight - fontSize/2 - padding]"
  font: "10px sans-serif"
  color: "#ffffff"
  padding: 2
```

### 丸め戦略 / Rounding Strategy

- **0 < value < 1**: 生存中ユニットの意味を保つため `"<1"` と表示する（`"0"` は撃破済みを示すため使わない）。
- **value >= 1**: まず `Math.ceil()` で整数 `displayValue` に切り上げ、**その `displayValue` で compact 境界を判定する**。これにより浮動小数点アーティファクト（例: `7.9999999` は `8`）を吸収する。
  - `displayValue < 10000`: そのまま整数文字列（例: `9999` は `"9999"`）。
  - `displayValue >= 10000`: 既存の compact 短縮ルール（floor ベースの k / M 表記）を適用する。
  - **境界注意**: 判定は raw value ではなく ceil 後の値で行う。したがって `9999.1` は `ceil` で `10000` となり `"10k"` と表示される（`"10000"` ではない）。これは #581 の `compact_from: 10000` と整合する。

### 短縮表記ルール / Abbreviation Rules (10000 以上)

ceil 適用後、compact 短縮は既存の floor ベース規則に従う。

| Input Range | Format | Example |
|---|---|---|
| 0 to 9999 | ceil(value) as string | "0", "<1", "1", "999", "9999" |
| 10000 to 999999 | floor(ceil(value) / 1000) + "k" | "10k", "999k" |
| 1000000+ | floor(ceil(value) / 1000000) + "M" | "1M" |

compact 表記で floor を使うのは、k / M 帯で表示 HP が実 HP より大きく見えるのを避けるためである。

### 表示セマンティクス / Display semantics (#726)

- 本表示は **exact HP ではなく、戦闘中に人間が即時認識するための display bucket（残存段階表示）** である。`0 < value < 1` を `"<1"`、`value >= 1` を `ceil` で表示するため、表示値は実 HP 以上になり得る（意図的）。
- この関数（`formatCombatNumber`）は **damage log / balance calculation / persistence / score 表示には流用しない**。それらは raw な数値モデルを直接使うこと。表示丸めはレンダリング層に閉じる（state / systems から参照しない）。
- `0 < value < 1` を `"0"`（撃破済み）と表示しないのは、`CombatSystem` が `hp === 0` を defeated 判定とし `Math.max(0, ...)` でクランプするため、`0 < hp < 1` が生存値だからである。
- **maxHp について**: `maxHp` は通常 integer の設計値（invariant）である。HUD では分子・分母に同一フォーマッタを一様適用するが、これは万一 `maxHp` が小数化しても過剰精度を露出させないための防御であり、`maxHp` を小数設計値として正当化するものではない。

### Canvas Bounds Clamp Policy

The HP label bounding box must remain within arena bounds regardless of enemy position.

Render pipeline: format -> measureText -> bounds clamp -> fillText.
`maxWidth` is NOT used (browser-dependent compression; no readability guarantee).

### Canvas State Protection

`drawEnemyHpLabel` uses `ctx.save()` / `ctx.restore()` to isolate font, fillStyle, textAlign, textBaseline.
No canvas state leaks to the caller.

### Future Numeric Labels

Score, timer, and other future numeric labels should also use `formatCombatNumber`.
Bulk unification is out of scope for this Issue (policy definition only).