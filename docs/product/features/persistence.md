---
doc_id: feature-persistence
status: draft
related_issue: "#735"
parent_issue: "#733"
trace_links:
  self_issue: "#735"
  parent_issue: "#733"
  sibling_specs:
    - docs/product/features/quick-save.md
    - docs/product/features/resource.md
    - docs/product/features/sortie.md
  implementation_consumers:
    - "#736"
    - "#739"
    - "#621"
  related_milestone: "M3: Result Persistence (v0.3.x)"
  existing_specs:
    - docs/product/game-logic.md
    - docs/product/playable-roadmap.md
    - docs/adr/0001-architecture-baseline.md
acceptance:
  - AC2: docs/product/features/persistence.md が YAML フロントマター付きで存在する
  - AC8: 永続化対象が progression snapshot のみで SortieResult は transient（永続化しない）である
  - AC9: canonical M3 snapshot fields と loop-protocol.mvp.save が quick-save.md 継承として明記され playerMaxHp compatibility note がある
  - AC10: localStorage の失敗モデルが normative に定義されている
  - AC11: HTTP origin 前提と file:// 非保証、schemaVersion 参照が明記されている
  - AC12: localStorage が最小手段でクラウド同期・複数スロットが非ゴールである
  - AC13: upgrade を M4 スコープとして non-goals に記載している
non-goals:
  - クラウド同期・ネットワーク越しの永続化
  - 複数スロット（複数セーブ）管理
  - upgrade（resources 消費・武器強化）の永続化（M4 スコープ）
  - GameSnapshot schemaVersion の実装本体（#736。本 spec では参照のみ）
  - quick-save.md 本体の改訂（canonical fields は継承・参照のみ）
related_tests: []
---

# Result Persistence

## Intent

本 spec は M3 (Result Persistence) における **progression snapshot の永続化境界** の正本を定義する。
sortie の結果から獲得した進行（`resources` 等）を localStorage へ best-effort で保存し、Load Game で復元できる境界を確定し、
後続実装 #739 (save-after-reward) / #736 (GameSnapshot schemaVersion) の停止条件を解除する実装契約として機能する。

関連する上位要件:

- REQ-LOGIC-PERSISTENCE-001: debrief entry 時点で snapshot を保存する（schema version 付き、`src/storage` 経由 / `docs/product/game-logic.md`）
- `docs/product/playable-roadmap.md` milestone M3（`source_mvp_loop: result_resource_loop`）

## Authority / Conflict Resolution

- `docs/product/features/quick-save.md` が現行 progression snapshot の保存挙動（保存対象・storage key・origin scope）の正本である。本 spec はその canonical fields / storage key を **継承・参照** し、本体を改訂しない。
- reward 計算・resources データモデルは `docs/product/features/resource.md` を正本とする。本 spec は「reward 適用後の `ProgressState` をどう永続化するか」の境界のみを定義する。
- `SortieResult` の shape / lifecycle は `docs/product/features/sortie.md` を正本とする。

## 永続化境界（normative）

- **永続化対象は progression snapshot のみ**である。具体的には reward 適用後の進行状態（`ProgressState` に対応する canonical snapshot fields）を保存する。
- **`SortieResult` は transient であり永続化しない**。sortie の terminal result は reward 変換の入力としてのみ消費され（`resource.md` の exactly-once 規約）、localStorage には書き込まない。
- したがって Load Game によって復元されるのは適用済みの progression snapshot のみであり、`SortieResult` を再構築・再計算しない。
- 非保存対象（current HP・enemies・projectiles・cooldown・sortie runtime 等）の扱いは `quick-save.md` を継承する。

## Canonical M3 snapshot fields

canonical M3 snapshot fields は `quick-save.md` を継承し、以下の 3 フィールドとする:

| フィールド | 型 | 説明 |
|---|---|---|
| `resources` | number（非負整数。`resource.md` 参照） | プレイヤーの所持リソース量 |
| `weaponPower` | number | 武器パワー値 |
| `playerMaxHp` | number | プレイヤーの最大 HP |

- storage key は namespaced かつ stable な `loop-protocol.mvp.save` を `quick-save.md` から継承する。
- **compatibility note（`playerMaxHp`）**: `playerMaxHp` は `ProgressState`（`stageLabel` / `resources` / `weaponPower`）のフィールドではなく、`GameSnapshot`（`resources` / `weaponPower` / `playerMaxHp`）のフィールドである。progression snapshot を `ProgressState` と読み替える際、`playerMaxHp` は `ProgressState` に存在しない点に注意する。canonical snapshot fields の正本は `GameSnapshot` 形状（= `quick-save.md` 記載の 3 フィールド）に従う。

## schemaVersion

- progression snapshot は将来の互換性のため `schemaVersion` を持つことを前提とする（保存データのバージョン識別子）。
- ただし `GameSnapshot` への `schemaVersion` フィールド追加の **実装本体は #736 のスコープ**であり、本 spec では `schemaVersion` を持つこと自体の参照に留める。

## localStorage 失敗モデル / Trust Model（normative）

- localStorage は **best-effort な browser-local persistence** であり、durable storage ではない。保存の成功・永続は保証されない。
- 永続化の可用性は、各 API 呼び出しを個別の `try`/`catch` で保護する lazy failure detection によって判定する（明示的な probe write は行わない）。
- 以下はすべて **failure boundary** であり、各呼び出しを `try`/`catch` で保護する: `getItem` / `setItem` / `removeItem` / `JSON.parse`。
- 次の失敗はいずれも **ゲームをクラッシュさせない**:
  - `SecurityError`（localStorage アクセス自体が拒否される。例: privacy 設定 / sandbox）
  - `QuotaExceededError`（保存容量超過）
  - corrupt JSON（不正な JSON。`JSON.parse` 失敗）
  - invalid schema（必須フィールド欠落・型不一致）
- 保存に失敗しても runtime state は **playable のまま** とし、default initial state へ安全に fallback する。
- load したデータは **untrusted** として扱い、復元前に validate する。ユーザーは DevTools から localStorage を直接改変可能であるため、load データを信頼しない。検証規約は下記「Snapshot validation」を正本とする。
- `LocalGameStorage.save()` / `load()` における `QuotaExceededError` / parse 失敗 / `SecurityError` の具体的な try/catch 実装は **#621 のスコープ**であり、本 spec は failure model の正本として #621 を implementation consumer に含める。

### Snapshot validation（per-field, normative）

canonical snapshot fields（`resources` / `weaponPower` / `playerMaxHp`）の検証規約を **一意に固定**する。`null fallback`（snapshot 全体破棄）と field 単位 fallback の混在でテストが割れないよう、以下の 2 段階規則とする:

1. **snapshot 全体 null fallback（最優先）**: corrupt JSON（`JSON.parse` 失敗）・required field 欠落・最上位が object でない場合は、snapshot 全体を `null` 扱いとし default initial state（`createInitialGameState()`）へ fallback する（`quick-save.md` の現行挙動を継承）。
2. **field 単位 fallback**: required field は揃っているが値が invalid（`NaN` / `Infinity` / 型不一致 / 範囲外）な場合のみ、当該 field を default 値へ fallback する。default は現行 `createInitialGameState` の既定に従う:

   | field | 妥当値 | invalid 時の field default |
   |---|---|---|
   | `resources` | non-negative safe integer（`>= 0`） | `0`（`resource.md` の無効値処理に準拠） |
   | `weaponPower` | positive safe integer（`>= 1`） | `1` |
   | `playerMaxHp` | positive safe integer（`>= 1`） | `8` |

`weaponPower < 1` / `NaN` / `Infinity` / 破損 JSON 等の regression test 要件は #736 と整合する。

## storage / origin（normative）

- storage key は namespaced かつ stable: `loop-protocol.mvp.save`。
- acceptance test は `file://` ではなく **HTTP origin** 上で実行する前提とする。`file://` では localStorage の挙動が origin 仕様上保証されない（`file://` は保証対象外）。
- same-origin deployment は path 間で localStorage を共有するため、`loop-protocol.` の project prefix によって project 間の key collision を回避する。
- ただし project prefix は **同一 project の本番 / PR preview / dev preview 間の衝突には効かない**。localStorage は origin 単位で分離され path 単位では分離されないため、GitHub Pages の `/<repo>/` 本番と `/<repo>/pr-<n>/` preview は同一 origin の保存領域（同一 key `loop-protocol.mvp.save`）を共有し得る。
  - 旧運用として `loop-protocol.mvp.save` を E2E / preview 前に clear する案があったが、GitHub Pages 同一 origin 衝突により本番セーブデータを消す危険があるため廃止する。
  - E2E / preview は `loop-protocol.e2e.*` / `loop-protocol.preview.pr-*` の分離 key を使い、production key は clear しない。

### Storage key matrix（production / preview / e2e / assist-suspend）

| 環境 | 利用 key |
|---|---|
| production（本番配信） | `loop-protocol.mvp.save` |
| PR preview | `loop-protocol.preview.pr-<pr-number>.mvp.save` |
| E2E（per-test / per-worker） | `loop-protocol.e2e.<run-id>.mvp.save` |
| Assist Suspend（要件化なし・将来） | `loop-protocol.mvp.assist-suspend.v1` |

PR preview と E2E は production key を clear せず、上記の分離 key を使用して互換性テスト・回帰検証を行う。

```yaml
storage_key_matrix:
  production: loop-protocol.mvp.save
  preview: loop-protocol.preview.pr-<number>.mvp.save
  e2e: loop-protocol.e2e.<run-id>.mvp.save
  assist_suspend: loop-protocol.mvp.assist-suspend.v1
```

## Non-Goals

- **クラウド同期・ネットワーク越しの永続化**は非ゴール（localStorage は MVP の最小保存手段である）。
- **複数スロット（複数セーブ）管理**は非ゴール。
- **upgrade（resources 消費・武器強化）の永続化は M4 スコープ**であり本 spec の対象外。
- `GameSnapshot` `schemaVersion` の実装本体（#736）。
- `quick-save.md` 本体の改訂（canonical fields は継承・参照のみ）。

## Related Tests

- （未作成。#739 save-after-reward 実装時に保存→reload 復元・失敗モード非クラッシュの決定論テストを追加予定）

## Related

- `docs/product/features/quick-save.md`（canonical snapshot fields / storage key の継承元）
- `docs/product/features/resource.md`（resources データモデル / reward exactly-once の正本）
- `docs/product/features/sortie.md`（`SortieResult` transient の正本）
- `docs/product/game-logic.md`（REQ-LOGIC-PERSISTENCE-001）
- `docs/product/playable-roadmap.md`（milestone M3）
