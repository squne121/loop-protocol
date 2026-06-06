---
doc_id: feature-sortie
status: draft
parent_issue: "#483"
trace_links:
  self_issue: "#485"
  parent_issue: "#483"
  sibling_specs:
    - "#484"
  implementation_consumers:
    - "#489"
  upstream_implementation:
    - "#486"
    - "#487"
    - "#488"
  validation:
    - "#490"
  existing_specs:
    - docs/product/features/movement-projectile.md
    - docs/product/features/combat-core.md
    - docs/product/game-logic.md
    - docs/adr/0001-architecture-baseline.md
---

# Sortie Lifecycle & Combat End Conditions

## Intent

`docs/product/features/sortie.md` は、1 回の戦闘（Sortie）の開始から終了（敵機全滅による勝利・自機HP0による敗北・30秒タイムアウトによる敗北）までの状態遷移、固定タイムステップタイマーの管理、および Transient Result Object の生成ルールを定義する正本である。永続化・UIレイアウト・upgrade・campaign への定義は持たない。

---

## Authority / Conflict Resolution

本 spec は M2 Combat MVP ゲートのために `docs/product/game-logic.md` の該当範囲を **refine and override** する。

> For M2 Combat MVP, this document refines and overrides `docs/product/game-logic.md`
> for sortie lifecycle, enemy-elimination victory, timer-based timeout, and SortieResult persistence semantics.

具体的な override 範囲：

- 敵機全滅（`allEnemiesDefeated`）が M2 における victory 条件。
  30秒タイムアウトは defeat（timeout）扱い。
  `REQ-LOGIC-VICTORY-001` の "outpost destruction" は M2 スコープ外に据え置き。
- SortieResult は transient output であり MUST NOT be persisted in M2.
  `REQ-LOGIC-PERSISTENCE-001` の "snapshot at debrief" / "campaign state" は M2 スコープ外に据え置き。
- Resource conversion、debrief rewards、progression、upgrade persistence は後続 Issue に委譲。

これにより、#489 実装者は `game-logic.md` の BattleState（`combat -> debrief` 遷移、outpost victory、storage snapshot）を M2 では実装しないことを明示的に確認できる。

---

## Sortie State Machine (FSM)

### States

| State     | Role                                                      |
|-----------|-----------------------------------------------------------|
| `idle`    | 戦闘未開始（初期状態）                                    |
| `running` | 戦闘進行中。elapsedTicks が加算される                     |
| `victory` | Result-latched state（敵機全滅）                          |
| `defeat`  | Result-latched state（自機HP0 または 30秒タイムアウト）   |
| `ended`   | Post-result acknowledgement state（結果確認後）           |

**重要**: `victory` と `defeat` は **result-latched states であり、final FSM states ではない**。
これらの状態は戦闘シミュレーションを停止させ、immutable な SortieResult を 1 度だけ生成する。
`ended` が final state であり、`ACK_RESULT` イベントにより到達する。

```
`victory` and `defeat` are result-latched states, not final FSM states.
They stop combat simulation and expose exactly one immutable SortieResult.
`ended` is the acknowledged post-result state and is the true terminal state.
```

### Normative Transition Table

| From          | Event / Guard                                                       | To        | Notes                                                          |
|---------------|---------------------------------------------------------------------|-----------|----------------------------------------------------------------|
| `idle`        | `START_SORTIE`                                                      | `running` | 戦闘開始。elapsedTicks=0 にリセット                            |
| `running`     | `FIXED_TICK [playerHp <= 0]`                                        | `defeat`  | defeat が最優先（同一 tick で他条件と重なっても defeat が勝つ）|
| `running`     | `FIXED_TICK [allEnemiesDefeated && playerHp > 0]`                   | `victory` | 全敵撃破。`enemies.length > 0` ガードで vacuous truth を防ぐ   |
| `running`     | `FIXED_TICK [elapsedTicks >= targetTicks && !allEnemiesDefeated]`   | `defeat`  | 30秒タイムアウト → defeat 扱い。victory より低優先             |
| `victory`     | `ACK_RESULT`                                                        | `ended`   | 結果確認。result は変更されない                                |
| `defeat`      | `ACK_RESULT`                                                        | `ended`   | 結果確認。result は変更されない                                |

---

## End Conditions

### Victory

- Guard: `state.enemies.length > 0 && state.enemies.every(e => e.defeated)`（かつ `playerHp > 0`）
- 意味: スポーン済みの敵機をすべて撃破した
- `state.enemies.length > 0` ガードにより、敵がスポーンしていないティックでの vacuous truth（空配列 `every()` が true になる問題）を防ぐ

### Defeat

- Guard 1: `playerHp <= 0`
  - 意味: 自機の HP が 0 以下になった
- Guard 2: `elapsedTicks >= targetTicks`（敵機が残存している場合）
  - 意味: 30秒のタイムリミットに達したが勝利条件を満たさなかった（timeout defeat）

### 優先順位（同一 tick で複数条件成立時）

```
1. player.hp <= 0       → defeat  （最優先）
2. allEnemiesDefeated   → victory （次優先）
3. elapsedTicks >= targetTicks → defeat（timeout・最低優先）
```

同一 `FIXED_TICK` で複数条件が成立した場合、上記の順序で先に成立した条件が採用される。

### Normative Per-Tick Evaluation Order

sortie.status == "running" の各 `FIXED_TICK` において、以下の順序で処理する:

```
For each FIXED_TICK while sortie.status == "running":

1. Run movement / projectile / enemy AI / collision / combat systems for this tick.
2. Apply all HP mutations and enemy defeat mutations produced by step 1.
3. Increment elapsedTicks by 1.
4. Evaluate terminal guards in this priority:
   a. if player.hp <= 0 -> latch defeat (generate SortieResult, transition to defeat)
   b. else if enemies.length > 0 && enemies.every(e => e.defeated) -> latch victory (generate SortieResult, transition to victory)
   c. else if elapsedTicks >= targetTicks -> latch defeat/timeout (generate SortieResult, transition to defeat)
5. Once a result is latched (sortie.status != "running"):
   - combat, projectile, enemy AI, spawn, and collision systems MUST NOT mutate state
     on subsequent ticks.
```

この順序は一意に定義される。実装者は上記以外の順序でステップを実行しない。

---

## Timer Authority

### elapsedTicks の加算規則

- `elapsedTicks` は `running` 状態の fixed simulation steps 内でのみ加算される（Per-Tick Evaluation Order の step 3）。
- `idle`、`victory`、`defeat`、`ended` の各状態では加算されない。

### targetTicks の算出

```
targetTicks = ceil(30_000 / fixedDeltaMs)
```

- `fixedDeltaMs`: 固定タイムステップの 1 ステップ当たりのミリ秒数（アーキテクチャ定数、ADR 0001）
- 30_000 ms = 30秒（MVP 戦闘時間）

### durationMs の算出

```
durationMs = elapsedTicks * fixedDeltaMs
```

`durationMs` は表示・互換用の導出フィールドである。勝利条件の主キーは `elapsedTicks` であり、`durationMs` は `elapsedTicks * fixedDeltaMs` から算出する。

### wall-clock / rAF の利用範囲

**Outer render/infrastructure loop での利用（MAY）:**

```
requestAnimationFrame, Date.now(), and performance.now() MAY be used by the
outer render/infrastructure loop to produce frame deltas for the SimulationLoop accumulator.
```

これは既存の `SimulationLoop` 設計（`fixedDeltaMs` ごとに step を実行）と整合する。

**SortieResult / sortie duration 計測での利用（MUST NOT）:**

```
They MUST NOT be used to:
- increment sortie elapsed time (elapsedTicks),
- decide victory or defeat,
- populate SortieResult.durationMs,
- persist or compare sortie outcome duration.

The only authoritative duration source is the number of executed fixed ticks (elapsedTicks).
```

理由: これらはシミュレーション外の時間経過（pause、フレームスキップ、パニック処理、背景タブによる rAF 停止）を含むため、再現性と決定論性が保証されない。

### SimulationLoop の panic / maxFrameSkip 破棄時間

SimulationLoop の panic や maxFrameSkip により破棄されたフレームの時間は `durationMs` に含めない。`elapsedTicks` が `running` 状態の固定ステップ内でのみ加算されることにより、この要件は自動的に満たされる。

---

## SortieResult

### Shape

`SortieResult` は discriminated union として定義される。`outcome` と `endReason` の組み合わせは型レベルで制約される。

```typescript
export type SortieEndReason =
  | 'all_enemies_defeated'
  | 'player_hp_zero'
  | 'timeout'

type SortieResultBase = Readonly<{
  durationMs: number
  kills: number
  shotsFired: number
  playerHpRemaining: number
}>

export type SortieResult =
  | (SortieResultBase & {
      readonly outcome: 'victory'
      readonly endReason: 'all_enemies_defeated'
    })
  | (SortieResultBase & {
      readonly outcome: 'defeat'
      readonly endReason: 'player_hp_zero' | 'timeout'
    })
```

`SortieEndReason` は M2 で発生しうる 3 値のみを持つ。`survival_timer` は #542 で MVP から除外されたため含まない。

### endReason マッピング

| 終了条件 | outcome | endReason |
|----------|---------|-----------|
| 全敵撃破（`allEnemiesDefeated && playerHp > 0`） | `'victory'` | `'all_enemies_defeated'` |
| HP ゼロ（`player.hp <= 0`） | `'defeat'` | `'player_hp_zero'` |
| タイムアウト（`elapsedTicks >= targetTicks`、敵残存） | `'defeat'` | `'timeout'` |

同一 tick で複数条件が成立した場合は「優先順位」セクションの順で先に成立した条件の `endReason` が採用される。

### 不変条件

| 不変条件 | 規則 |
|----------|------|
| 生成回数 | result は `running` → `victory` または `running` → `defeat` への初回遷移で一度だけ生成される |
| immutability | result は生成後 MUST NOT be mutated（再生成・上書き禁止） |
| `outcome` / `endReason` 整合 | `outcome: 'victory'` は `endReason: 'all_enemies_defeated'` のみ。`outcome: 'defeat'` は `endReason: 'player_hp_zero' \| 'timeout'` のみ（型で保証） |
| `kills` 算出元 | `defeatedAtTick <= terminalTick` の敵から導出される（terminal tick 以前に defeated 状態になった敵） |
| `shotsFired` | terminal tick 時点の snapshot（遷移後の変更を反映しない） |
| `playerHpRemaining` clamp | `[0, player.maxHp]` にクランプされる |
| HP defeat 時の HP | `player.hp <= 0` による defeat 時: `playerHpRemaining === 0` |
| timeout defeat 時の HP | `elapsedTicks >= targetTicks` による defeat 時: `playerHpRemaining === clamp(player.hp)` |
| 永続化禁止 | result を persistence / resources / upgrades / localStorage / campaign への書き込みに使用しない |

### Transient Output

SortieResult は transient output として定義される。永続化は本 spec のスコープ外。

---

## Non-Goals

本 feature spec が定義しないもの:

- **Persistence**: SortieResult の localStorage への保存、セッションをまたぐ結果の保持
- **Upgrade / Progression**: 戦闘結果によるリソース獲得、アップグレードロジック
- **Campaign**: キャンペーンモード、ステージ進行、マップ遷移
- **Briefing UI**: 戦闘前ブリーフィング、結果表示 UI のレイアウト定義
- **Outpost destruction**: M2 スコープ外（game-logic.md 経由で後続 Issue へ）

---

## Related Tests

Planned:

```yaml
- path: tests/sortie-system.test.ts
  covers:
    - transition: idle -> running
    - victory at target tick
    - defeat when player HP reaches 0
    - same-tick defeat precedence (defeat wins)
    - SortieResult generated exactly once
    - no combat mutation after terminal result
    - timer authority uses executed fixed ticks, not wall clock
```

---

## Related

- `docs/product/features/movement-projectile.md`: player 移動・発射の最小仕様
- `docs/product/features/combat-core.md`: CollisionSystem / CombatSystem contract（#484）
- `docs/product/game-logic.md`: ゲームロジック全体仕様（本 spec が M2 範囲を override）
- `docs/adr/0001-architecture-baseline.md`: 60Hz 固定タイムステップ・state/systems 分離
- Issue #485: この仕様の起票 Issue
- Issue #483: parent（M2 Combat MVP Gate）
- Issue #484: sibling spec（combat-core）
- Issue #486, #487, #488: upstream implementation
- Issue #489: implementation consumer（sortie-system 実装・テスト）
- Issue #490: validation
