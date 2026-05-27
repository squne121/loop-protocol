---
doc_id: movement-projectile
status: draft
issue: "#2"
parent_issue: "#1"
trace_links:
  - "#1"
  - "#2"
  - docs/adr/0001-architecture-baseline.md
  - docs/product/game-logic.md
---

# Movement + Projectile 最小仕様

## intent

player 移動・aim・fire・projectile の最小ライフサイクルを一箇所に固定し、`#1` 実装者が着手判断できる最小の規約を提供する。

実装ブレを防ぐため、値が `TBD` の定数は `open_questions` セクションへ移し、識別子のみここで明記する。コード実装は `#1` のスコープとする。

## requirements

### 座標系

- `coordinate_space`: arena logical pixels（論理ピクセル単位でゲームロジックを計算する）
- Canvas への描画時は `devicePixelRatio` を `CanvasRenderer` 内で処理し、System 層は logical pixels のみを扱う
- pointer 座標は `PointerEvent` の `.clientX / .clientY` から `getBoundingClientRect()` を使って canvas logical coordinate に変換する（`InputMapper` の責務）

### 固定タイムステップ

- シミュレーションは 60Hz（約 16.67ms / tick）固定タイムステップのアキュムレータで進める
- `requestAnimationFrame` ループ内でアキュムレータを更新し、120Hz / 144Hz 等の高リフレッシュ環境でも tick 速度が変わらないようにする
- background 復帰時の panic clamp（`maxFrameSkip`）後の残余時間は次フレームへ持ち越す（破棄しない）
- `cooldown_authority`: simulation time（wall clock ではなく simulation tick 時間で管理する）

### 自機移動

- 入力: `KeyboardEvent.code` で WASD を取得し `InputState` → `InputCommand` へ正規化
- `diagonal_normalization`: 斜め入力時はベクトルを正規化する（8 方向移動速度均等化）
- `boundary_clamp`: 移動後の自機中心座標を `[player_radius, arena_width - player_radius]` / `[player_radius, arena_height - player_radius]` にクランプする
- 移動速度識別子: `speed_px_per_sec`（値は `open_questions` 参照）

### aim・fire

- 入力: `PointerEvent`（mouse / pen / touch 統合。pointer capture を使い canvas 外 drag 中も aim 継続）
- aim は logical arena 座標で保持する（角度ではなくターゲット座標を推奨するが `#1` 実装者が選択）
- aim と自機が同座標の場合のフォールバック方針は `open_questions` 参照
- fire: `pointerdown` 検知 → hold-to-repeat（クールダウン完了時に pointerdown が継続していれば次弾発射）
- `cooldown_authority` は simulation time（tick 単位）で管理

### projectile

- 状態格納先: `GameState.projectiles`（`Projectile[]`）
- ID 採番: `monotonic counter`（deterministic、生成順連番）
- spawn 座標: 自機中心（muzzle offset は `open_questions`）
- 進行方向: 自機からターゲット座標への正規化ベクトル
- 速度識別子: `speed_px_per_sec`（値は `open_questions` 参照）
- 寿命識別子: `lifetime_ms`（値は `open_questions` 参照）
- 削除条件:
  1. `lifetime_ms` 経過
  2. arena 境界外へ出た（オプションの margin を含む）

### System 責務境界

| System | 責務 |
|---|---|
| `MovementSystem` | `InputCommand` + `dt` から自機座標を更新し `boundary_clamp` を適用 |
| `CombatSystem` | fire intent + cooldown 管理、発射可能なら `GameState.projectiles` に追加 |
| `ProjectileSystem` | 全 projectile を `speed_px_per_sec` + `dt` で移動し、削除条件を判定して除去 |
| `CanvasRenderer` | `GameState` を読み取り描画のみ。System 状態更新に関与しない |

`GameState` は純粋データのみを保持し、DOM / Canvas API への依存を持たない。

## acceptance_criteria

- AC1: `docs/product/features/movement-projectile.md` が存在し、YAML frontmatter に `status: draft`、`issue: "#2"`、`parent_issue: "#1"`、`doc_id: movement-projectile`、`trace_links` が含まれる
- AC2: spec 本文に必須セクション `intent` / `requirements` / `acceptance_criteria` / `non_goals` / `open_questions` / `playtest_hypotheses` / `related_tests` が存在する
- AC3: spec 本文に以下の固定定数識別子が明記されている: `coordinate_space: arena logical pixels`、`diagonal_normalization`、`boundary_clamp`、`PointerEvent`、`cooldown_authority`、`GameState.projectiles`、`monotonic counter`、`speed_px_per_sec`、`lifetime_ms`
- AC4: rAF と 60Hz fixed timestep の境界が明文化され、`requestAnimationFrame` / `60Hz` / `120Hz` / `devicePixelRatio` への言及がある
- AC5: `related_tests` に 4 テストファイルが列挙されている
- AC6: `non_goals` に collision / damage / enemy AI / visual effects のキーワードが含まれる
- AC7: `docs/dev/ssot-registry.md` に `movement-projectile` エントリが追加されている
- AC8: `pnpm typecheck` / `pnpm lint` / `pnpm test` / `pnpm build` が PASS する

## non_goals

- collision 判定（弾と敵の衝突検知）
- damage 処理（ヒット時の HP 演算）
- enemy AI（敵の行動ロジック）
- visual effects（ヒットストップ・パーティクル・画面振動等）
- Combat 全体の包括仕様化
- `#1` のコード実装（`src/` への変更）
- テスト追加（`tests/` への変更は `#1` のスコープ）

## open_questions

以下の具体的な値は `#1` の実装 Issue またはバランス調整 Issue で決定する:

| 識別子 | 内容 | 現行 GameState 値 |
|---|---|---|
| `speed_px_per_sec` (player) | 自機移動速度 | 210 (要確認) |
| `speed_px_per_sec` (projectile) | 弾の移動速度 | 未設定 |
| `lifetime_ms` | 弾の寿命 | 未設定 |
| `cooldown_ms` | 発射クールダウン | 280ms (要確認) |
| player_radius | 自機半径 | 14 (要確認) |
| arena_width / arena_height | arena サイズ | 未設定 |
| spawn origin offset | muzzle offset の有無 | 未設定（自機中心を暫定） |
| aim at same position | 自機とターゲットが同座標のフォールバック | 未設定 |
| aim storage format | 座標 or 角度 | 未設定 |

## playtest_hypotheses

- PH-1: 60Hz 固定タイムステップは 120Hz / 144Hz 環境でも視覚的に一定速度の移動・弾道として体感できる
- PH-2: `diagonal_normalization` により斜め移動が直線移動と同速に感じられる
- PH-3: hold-to-repeat の連射挙動が「連射感」を適切に提供する（クールダウン値が確定次第検証）
- PH-4: pointer capture により canvas 外 drag 中も aim が途切れない体験を提供できる

## related_tests

実装 `#1` で追加・拡充するテストファイル:

- `tests/movement-system.test.ts` — diagonal 正規化・boundary clamp のユニットテスト
- `tests/combat-system.test.ts` — fire 時の projectile 生成・クールダウン中の抑止・simulation time 管理のテスト
- `tests/projectile-system.test.ts` — 移動・lifetime 削除・arena 境界外削除・削除順序 determinism のテスト
- `tests/input-mapper.test.ts` — PointerEvent → arena 座標変換・fire command 生成のテスト
