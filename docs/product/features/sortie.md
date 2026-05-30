---
doc_id: feature-sortie
status: draft
parent_issue: "#483"
trace_links:
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
    - docs/product/adr/0002-product-architecture-baseline.md
---

# Sortie Lifecycle & Combat End Conditions

## Intent

`docs/product/features/sortie.md` は、1 回の戦闘（Sortie）の開始から終了（120秒生存または自機HP0）までの状態遷移、固定タイムステップタイマーの管理、および Transient Result Object の生成ルールを定義する正本である。永続化・UIレイアウト・upgrade・campaign への定義は持たない。

---

## Sortie State Machine (FSM)

### States

| State     | Role                                         |
|-----------|----------------------------------------------|
| `idle`    | 戦闘未開始（初期状態）                        |
| `running` | 戦闘進行中。elapsedTicks が加算される         |
| `victory` | Terminal result-latched state（生存達成）     |
| `defeat`  | Terminal result-latched state（自機HP0）      |
| `ended`   | Post-result acknowledgement state（結果確認後）|

- `victory` と `defeat` は terminal result-latched states: 一度遷移したら状態は変化しない。result は変更されない。
- `ended` は post-result acknowledgement state: result を変更しない。戦闘後の後処理フェーズを示す。

### Normative Transition Table

| From          | Event / Guard                                   | To        | Notes                                     |
|---------------|-------------------------------------------------|-----------|-------------------------------------------|
| `idle`        | `START_SORTIE`                                  | `running` | 戦闘開始。elapsedTicks=0 にリセット       |
| `running`     | `FIXED_TICK [playerHp <= 0]`                    | `defeat`  | 同一 tick で両条件成立時も defeat が優先  |
| `running`     | `FIXED_TICK [elapsedTicks >= targetTicks]`      | `victory` | playerHp > 0 の場合のみ（defeat 優先規則による） |
| `victory`     | `ACK_RESULT`                                    | `ended`   | 結果確認。result は変更されない           |
| `defeat`      | `ACK_RESULT`                                    | `ended`   | 結果確認。result は変更されない           |

---

## End Conditions

### Victory

- Guard: `elapsedTicks >= targetTicks`
- 意味: 120秒相当の固定ティック数を playerHp > 0 の状態で生存した

### Defeat

- Guard: `playerHp <= 0`
- 意味: 自機の HP が 0 以下になった

### 同一 tick での両条件成立時の裁定

同一 `FIXED_TICK` で `playerHp <= 0` と `elapsedTicks >= targetTicks` が同時に成立した場合、**defeat が優先**される。

理由: 最終ティックで自機が被弾して HP が 0 になった場合、プレイヤーは生存しておらず victory 条件を充足していない（"player did not survive terminal tick"）。

---

## Timer Authority

### elapsedTicks の加算規則

- `elapsedTicks` は `running` 状態の fixed simulation steps 内でのみ加算される。
- `idle`、`victory`、`defeat`、`ended` の各状態では加算されない。

### targetTicks の算出

```
targetTicks = ceil(120_000 / fixedDeltaMs)
```

- `fixedDeltaMs`: 固定タイムステップの 1 ステップ当たりのミリ秒数（アーキテクチャ定数）
- 120_000 ms = 120秒

### durationMs の算出

```
durationMs = elapsedTicks * fixedDeltaMs
```

### 禁止事項（MUST NOT）

以下の時間計測手段を sortie の duration 計測に使用してはならない:

- `wall-clock`（壁時計）
- `requestAnimationFrame` (rAF)
- `Date.now()`
- `performance.now()`

理由: これらはシミュレーション外の時間経過（pause、フレームスキップ、パニック処理）を含むため、再現性と決定論性が保証されない。

### SimulationLoop の panic / maxFrameSkip 破棄時間

SimulationLoop の panic や maxFrameSkip により破棄されたフレームの時間は `durationMs` に含めない。`elapsedTicks` が `running` 状態の固定ステップ内でのみ加算されることにより、この要件は自動的に満たされる。

---

## SortieResult

### Shape

```typescript
interface SortieResult {
  outcome: "victory" | "defeat";
  durationMs: number;
  kills: number;
  shotsFired: number;
  playerHpRemaining: number;
}
```

### 不変条件

| 不変条件 | 規則 |
|----------|------|
| 生成回数 | result は `running` → `victory` または `running` → `defeat` への初回遷移で一度だけ生成される |
| immutability | result は生成後 immutable（再生成・上書き禁止） |
| `kills` 算出元 | terminal tick 以前に `defeated` 状態になった敵から導出される |
| `shotsFired` | terminal tick 時点の snapshot（遷移後の変更を反映しない） |
| `playerHpRemaining` clamp | `[0, player.maxHp]` にクランプされる |
| defeat 時の HP | `defeat` 時: `playerHpRemaining === 0` |
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

---

## planned_consumer_tests

```yaml
planned_consumer_tests:
  - src/systems/__tests__/sortie-system.test.ts
  - covers:
      - victory at target tick
      - defeat at HP zero
      - same-tick victory/defeat precedence (defeat wins)
      - result generated once
      - timer authority ignores wall-clock/rAF pause
      - combat mutation does not continue after terminal state
```

---

## Related

- `docs/product/features/movement-projectile.md`: player 移動・発射の最小仕様
- `docs/adr/0001-architecture-baseline.md`: 60Hz 固定タイムステップ・state/systems 分離
- Issue #483: parent（M2 Combat MVP Gate）
- Issue #484: sibling spec（combat-core）
- Issue #486, #487, #488: upstream implementation
- Issue #489: implementation consumer（sortie-system テスト）
- Issue #490: validation
