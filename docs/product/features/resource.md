---
doc_id: feature-resource
status: draft
related_issue: "#735"
parent_issue: "#733"
trace_links:
  self_issue: "#735"
  parent_issue: "#733"
  sibling_specs:
    - docs/product/features/sortie.md
    - docs/product/features/persistence.md
    - docs/product/features/quick-save.md
  implementation_consumers:
    - "#737"
    - "#739"
  related_milestone: "M3: Result Persistence (v0.3.x)"
  existing_specs:
    - docs/product/game-design.md
    - docs/product/game-logic.md
    - docs/product/playable-roadmap.md
    - docs/adr/0001-architecture-baseline.md
acceptance:
  - AC1: docs/product/features/resource.md が YAML フロントマター付きで存在する
  - AC3: 報酬計算の入力が terminal SortieResult であることが明記されている
  - AC4: reward_formula_version 1 の M3 暫定固定値が deterministic に定義されている
  - AC5: resources の型・初期値・上限・無効値処理・overflow clamp が定義されている
  - AC6: reward が terminal SortieResult 1 件につき exactly-once で適用され reload 後に再計算しない
  - AC7: sortie.md との Authority が明記されている
  - AC13: upgrade を M4 スコープとして non-goals に記載している
non-goals:
  - upgrade（resources 消費・武器強化）の spec / 実装（M4 スコープ）
  - reward / persistence の実装コード（#737 / #739）
  - reward_formula_version 1 の暫定値に対する最終バランス調整（tuning は M4 以降）
  - SortieResult shape そのものの定義（sortie.md が正本）
related_tests: []
---

# Resource & Reward

## Intent

本 spec は M3 (Result Persistence) における **報酬計算（reward）** と **resources データモデル** の正本を定義する。
sortie の terminal な結果を、プレイヤー進行に還元される `resources` へ deterministic に変換する規約を確定し、
後続実装 #737 (RewardSystem) / #739 (save-after-reward) の停止条件を解除する実装契約として機能する。

関連する上位要件:

- REQ-GDD-002: sortie の outcome を、プレイヤー進行に還元される resources へ変換する（`docs/product/game-design.md`）
- `docs/product/playable-roadmap.md` milestone M3（`source_mvp_loop: result_resource_loop`）

## Authority / Conflict Resolution

- **`docs/product/features/sortie.md` が sortie lifecycle と `SortieResult` shape の正本**である。本 spec は `SortieResult` の型・discriminator を再定義しない。
- M2 の `sortie.md` は「`SortieResult` は transient output であり persistence / resources / upgrades / localStorage に書き込まない」と定めている。本 spec はこのルールを **M3 向けに refine** する:
  - `SortieResult` は依然として **永続化しない**（transient のまま）。
  - ただし RewardSystem が terminal な `SortieResult` を **exactly-once** で消費して reward delta を生成し、その delta を `ProgressState` に適用する。
  - 永続化境界は `ProgressState`（progression snapshot）のみであり、`SortieResult` 自体は永続化対象に含まれない。詳細な永続化境界は `docs/product/features/persistence.md` を正本とする。
- 本 spec と `sortie.md` が衝突した場合、`SortieResult` の shape・lifecycle は `sortie.md`、reward 変換・resources データモデルは本 spec を優先する。

## Reward 計算

### 入力（normative）

報酬計算の **唯一の入力は terminal な `SortieResult`** である。

- `SortieResult` は `outcome`（`'victory' | 'defeat' | 'timeout'`）を discriminator に持つ discriminated union で、`durationMs` / `kills` / `shotsFired` / `playerHpRemaining` を payload に持つ（shape の正本は `sortie.md`）。
- `outcome` と `endReason` は型レベルでペア固定されており（`sortie.md` / `src/state/GameState.ts` の正本に従う）、reward 区分はこのペアから導出する。**`timeout` は独立した outcome（neutral terminal）であり `defeat` ではない**（#732 で neutral terminal 化済み）:

  | outcome | endReason | 意味 |
  |---|---|---|
  | `'victory'` | `'all_enemies_defeated'` | 全敵撃破 |
  | `'defeat'` | `'player_hp_zero'` | 自機 HP 0 |
  | `'timeout'` | `'timeout'` | 30 秒タイムアウト（neutral terminal） |

  実装（#737）は `outcome === 'timeout'` を defeat と混同してはならない。
- RewardSystem は **terminal な（= sortie が終了して確定した）`SortieResult` のみ** を入力に取る。途中状態・非終端な runtime state を入力にしない。
- reward は `SortieResult` の field のみから決定論的に計算され、wall-clock / 乱数 / 外部 I/O に依存しない（同一 `SortieResult` からは常に同一 reward delta が得られる）。

### reward_formula_version: 1（M3 暫定固定値）

```yaml
reward_formula_version: 1
# M3 暫定固定値（deterministic）。最終バランス調整（tuning）は M4 以降の non-goal。
base_reward:
  victory: 100   # outcome == 'victory'（all enemies defeated）
  defeat: 10     # outcome == 'defeat'（player hp 0）
  timeout: 30    # outcome == 'timeout'
kill_bonus_per_kill: 5         # kills * 5（全 outcome 共通、kills は非負整数前提）
hp_bonus:
  victory_only: true           # victory 時のみ playerHpRemaining を加算、それ以外は 0
  per_remaining_hp: 1          # floor(playerHpRemaining) * 1（victory 時のみ）
per_sortie_cap: 500            # 1 sortie あたりの reward delta 上限（clamp）
```

reward delta の算出（normative, deterministic）:

1. `base = base_reward[outcome]` を選択する。
2. `killBonus = max(0, floor(kills)) * kill_bonus_per_kill`。
3. `hpBonus = outcome == 'victory' ? max(0, floor(playerHpRemaining)) * 1 : 0`。
4. `rawDelta = base + killBonus + hpBonus`。
5. `rewardDelta = clamp(rawDelta, 0, per_sortie_cap)`（per-sortie cap への clamp）。

`reward_formula_version` は formula 改訂時にインクリメントする。M3 では `1` 固定とし、上記の固定値が deterministic な実装の単一正本である。

## resources データモデル（normative）

| 項目 | 定義 |
|---|---|
| 型 | **非負整数**（non-negative safe integer。`Number.isSafeInteger` かつ `>= 0`） |
| 単位 | 抽象的な進行リソース量（無次元） |
| 初期値 | `0` |
| 上限 | `RESOURCE_CAP`（M3 暫定値 = `9_999_999`） |

### 無効値処理と overflow clamp

- 加算（reward delta 適用）の結果が `RESOURCE_CAP` を超える場合は `RESOURCE_CAP` へ **clamp** する（overflow clamp）。
- 保存 / 復元 / 加算のいずれかで finite な非負整数でない値（**負値 / `NaN` / `Infinity` / 小数**）を観測した場合、その値は **invalid** とみなし、**invalid fallback として `0`** を採用する（M3 ではこの 1 つの規則に固定する。floor 整数化や clamp ではなく `0` へ落とす）。
  - 例: 復元した `resources` が `NaN` / `Infinity` / 負値 / 小数 → `0` として扱う。
  - reward delta 適用の最終結果も `clamp(value, 0, RESOURCE_CAP)` の範囲に必ず収まる非負整数である。

## Reward application（exactly-once 規約）

- terminal な `SortieResult` 1 件につき、reward delta は **最大 1 回（exactly-once）** だけ `ProgressState.resources` に適用される。
- 以下のいずれの再実行でも **二重加算しない**:
  - debrief 画面の再描画 / 再レンダリング
  - reload（ページ再読み込み）
  - debrief ボタンの連打（button repeat）
  - state update の再実行・冪等再評価
- `SortieResult` は **永続化しないため、reload 後に同一 result から reward を再計算しない**。reload 後に復元されるのは適用済みの `ProgressState`（resources を含む）のみであり、terminal result の再消費は発生しない。
- **exactly-once の単位は `SortieResult` の値同一性（value identity）ではなく、1 sortie lifecycle の terminal transition event とする**。同一 payload（同じ `outcome` / `kills` 等）を持つ別 sortie の結果は、**別個の reward application** として扱い加算する。実装（#737）は `JSON.stringify(result)` や shallow equality による値ベースの重複排除を行ってはならない（別 sortie の同一 payload を誤って二重適用扱いで握り潰す危険があるため）。
- exactly-once を保証する具体的な実装機構の選択は #737 の実装範囲とするが、**同一 sortie lifecycle 内の再適用防止は `rewardApplied` latch または consumed transition token で行う**ことを本 spec が正本として要求する（上記の不変条件: 最大 1 回適用・reload 後再計算しない・lifecycle 単位）。これは debrief / preparation 遷移（#738）と整合する。

## Non-Goals

- **upgrade（resources の消費・武器強化）の spec / 実装は M4 スコープ**であり本 spec の対象外。reward で獲得した resources をどう消費するかは M4 以降で別 spec として定義する。
- reward / persistence の実装コード（#737 / #739）。
- `reward_formula_version: 1` の暫定値に対する最終バランス調整（tuning は M4 以降）。
- `SortieResult` shape そのものの定義（`sortie.md` が正本）。
- クラウド同期・複数スロット等の永続化拡張（`persistence.md` の non-goal）。

## Related Tests

- （未作成。#737 RewardSystem 実装時に reward 計算・exactly-once・clamp の決定論テストを追加予定）

## Related

- `docs/product/features/sortie.md`（`SortieResult` shape / sortie lifecycle の正本）
- `docs/product/features/persistence.md`（progression snapshot 永続化境界の正本）
- `docs/product/features/quick-save.md`（canonical snapshot fields の継承元）
- `docs/product/game-design.md`（REQ-GDD-002）
- `docs/product/playable-roadmap.md`（milestone M3）
