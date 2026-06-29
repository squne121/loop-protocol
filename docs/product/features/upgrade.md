---
doc_id: feature-upgrade
status: draft
related_issue: "#1170"
parent_issue: "#1176"
acceptance:
  - AC1: 本 spec は YAML フロントマター付きで存在し、`doc_id` が `feature-upgrade` である。
  - AC2: `related_issue` と `parent_issue` が `#1170`, `#1176` である。
  - AC3: `docs/product/playable-roadmap.md` の M4: Upgrade Loop を上位権威として参照し、M4 スコープの実装契約として機能する。
  - AC4: 本 spec は `resources` のみを通貨として扱い、`state.progress.resources` を参照する。
  - AC5: upgrade 定義は `currency: resources`、`cost`、`target` を必須項目として持つ。
  - AC6: M4 最小 UpgradeDefinition は `currency: resources`、`cost`、`target: progress.weaponPower` を満たす。
  - AC7: `cost` は positive safe integer として定義・検証し、`0`、負値、NaN、Infinity、小数は禁止。
  - AC8: 申請時は `state.progress.resources >= cost` を検証し、`resourcesAfter = resourcesBefore - cost` を計算する。
  - AC9: Upgrade 適用順序は `validate → debit → apply → snapshot` を満たす。
  - AC10: `preparation` フェーズ以外では purchase / apply を行わない。
  - AC11: 保存失敗時（`QuotaExceededError` / `SecurityError`）は `no false success` とし、live state を commit せず、save 前に mutate 済みなら exact rollback を MUST とする。
  - AC12: upgrade の効果は次回 sortie 以降に反映され、実行中 runtime への retroactive 適用はしない。
  - AC13: storage key matrix、HTTP origin 前提、`file://` 非保証を保存・検証境界に記載し、`persistence.md` の schema を継承する。
non-goals:
  - 複雑な upgrade tree / branch / respec / refund。
  - reward（獲得）実装と `resource.md` 以外の reward pipeline。
  - クラウド同期、複数セーブスロット。
  - multi-tab / multi-window 間の read-modify-write transaction 保証。
  - sortie/runtime（HP、敵、projectile、cooldown）への遡及修正。
  - file:// 固有の互換性保証。
related_tests: []
---

# Weapon Upgrade (M4)

## Intent

本 spec は M4 (Upgrade Loop) の `resource` 消費型 upgrade の正本を定義する。
#1170 / #1176 の文脈で、`resource.md` の `resources` 定義と、`persistence.md` の保存境界を前提に、
最小 upgrade ループの契約（定義、消費、保存、失敗時意味論）を固定する。

## Scope

対象:
M4 の最小 upgrade（weaponPower への最小増加）を、`preparation` フェーズのみで実行できる仕様として規定する。

対象外:
- 攻撃演算の再設計
- 複合 upgrade ツリー・再振り分け（respec）
- 複数スロット / クラウド同期
- 過去の sortie / runtime への retroactive 修正

## Authority / Conflict Resolution

- `docs/product/playable-roadmap.md` の M4 章（`spec_destination`）を本機能の上位境界とする。
- `docs/product/features/resource.md` が `resources` データモデルの正本であり、upgrade は本 spec では **消費側の契約**として扱う。
- `docs/product/features/persistence.md` の storage key / origin 境界を継承し、保存実装は `persistence.md` を正本とする。
- `docs/product/features/sortie.md` / `resource.md` と衝突した場合、`SortieResult` と `resources` の責務は既存仕様を優先する。

## Upgrade Definition Schema

M4 最小 upgrade 定義は以下を満たす。

```yaml
upgrade_id: weapon_power_plus_1
upgrade_definition_schema_version: 1
currency: resources
cost: 100
target: progress.weaponPower
operation: add
value: 1
availability:
  phase: preparation
  repeatable: false
```

### Normative fields

- `upgrade_id` / `upgrade_definition_schema_version`: 一意性と追跡可能性のための識別。
- `currency`:
  - 本 spec は `resources` のみを通貨として許可する。
  - 参照元は `state.progress.resources` とする。
- `cost`:
  - positive safe integer を要求する。
  - `1 <= cost <= RESOURCE_CAP`。
  - `NaN` / `Infinity` / 小数 / 負値は無効。
- `target`:
  - `target: progress.weaponPower` と固定する（M4 最小スコープ）。
  - `operation` は現時点で `add` のみ、`value` は positive safe integer。
  - apply 後の `weaponPower` は positive safe integer を維持しなければならない。
- `availability`:
  - `phase` は `preparation` のみ許可。
  - `running` / `result` では purchase 不可。
  - M4 最小スコープでは `repeatable: false` を `state.progress.weaponPower > 1` で充足済みと判定する。
  - M4 では `purchasedUpgradeIds` のような永続 ledger を導入しない。
  - 将来の複数 upgrade tree は、schema version 付き ownership ledger を別 issue で導入する。

## Apply Contract

### Preconditions

1. `state.progress.resources` が non-negative safe integer であること。
2. `upgrade.definition` が本節の Schema を満たすこと。
3. `state.progress.resources` は resources is greater than or equal to cost を満たすこと。
4. `state.progress.resources >= cost` を満たすこと。
5. `availability.phase == preparation` を満たすこと。

### Steps（deterministic）

1. **validate**
   - `state.progress.resources` を non-negative safe integer として検証し、違反なら通らない。
   - `definition` の `currency` / `target` / `cost` 形式を検証する。
2. **debit**
   - `resourcesBefore = state.progress.resources`
   - `resourcesAfter = resourcesBefore - cost`
   - `resourcesAfter` が 0 未満なら失敗し、何も mutate しない。
3. **apply**
   - candidate progression state に対して `state.progress.weaponPower` を effect に従って更新する。
   - 変更値の有効性を検証し、次の sortie から観測される候補状態として確定する。
4. **snapshot**
   - candidate progression state から snapshot を作成し、persistence 層で保存する。
   - 保存成功後にのみ live `state.progress` を commit し、upgrade success 扱いとする。

### Atomicity / visible success

- M4 upgrade purchase は **strong atomic visible success** を採用する。
- 実装は candidate progression state を先に組み立て、candidate snapshot の保存に成功した後にのみ live `state.progress` を commit する。
- save 前に live state を mutate する実装を選ぶ場合でも、save failure 時には pre-upgrade progression state へ **exact rollback** を MUST とする。
- `applied in memory but not persisted` は M4 upgrade purchase の許容される post-return state ではない。
- `state.progress.resources` の debit と `progress.weaponPower` の apply は、呼び出し元へ success を返す時点で snapshot 保存成功と不可分でなければならない。

### 次 sortie 反映 / 非 retroactive

- upgrade 適用は **次の sortie**で効力を持つ。
- running / result 中の projectile / enemy / runtime への遡及適用（retroactive）は行わない。
- 実行済み projectile の damage を再計算しない。

## Failure Rules

- 不足 resources (`state.progress.resources < cost`):
  - 何も mutate しない。
  - エラーを返す（成功扱いしない）。
- 無効定義（schema / currency / target / cost）:
  - 何も mutate しない。
  - 仕様違反として失敗を返す。
- 無効 state:
  - `state.progress.resources` が invalid の場合、`resource.md` の invalid fallback（`0`）を通しても validate 通過できなければ失敗。
- 保存失敗（`QuotaExceededError`、`SecurityError`、保存例外）:
  - candidate snapshot が保存できなければ live `state.progress` を commit しない。
  - save 前に live state を mutate していた場合は exact rollback を実行し、**no false success** を維持する。
  - 保存成功前に upgrade success を返してはならない。

保存契約は `persistence.md` による保存境界を従う。`state.progress.resources` の減算および
`progress.weaponPower` への反映は、snapshot 保存が成功した場合のみ commit とする。

### persistence boundary（storage key / origin）

- 保存実装は `persistence.md` の storage key matrix に従い分離キーを使う。要約:
  - production: `loop-protocol.mvp.save`
  - PR preview: `loop-protocol.preview.pr-<number>.mvp.save`
  - E2E: `loop-protocol.e2e.<run-id>.mvp.save`
- 検証と再現条件は HTTP origin を前提とする。
- `file://` については localStorage 振る舞いが保証されないため、保存契約の検証対象外とする。
- `VITE_LOOP_STORAGE_NAMESPACE` は build-time namespace として扱い、PR preview では `loop-protocol.preview.pr-<number>.mvp.save` へ解決される非秘密値のみを許可する。

### Single-runtime scope / cross-tab non-goal

- M4 purchase atomicity は **単一の active game runtime** 内でのみ定義する。
- multi-tab / multi-window 間の localStorage read-modify-write transaction 保証は本 spec の非ゴールである。
- cross-tab conflict detection や revision token の導入は将来の別 issue とし、M4 では扱わない。

## Related

- `docs/product/features/resource.md`
- `docs/product/features/persistence.md`
- `docs/product/features/sortie.md`
- `docs/product/playable-roadmap.md`
