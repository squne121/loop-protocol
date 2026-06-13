---
doc_id: feature-accessibility-save-policy
status: draft
related_issue: "#622"
parent_issue: "#733"
trace_links:
  self_issue: "#622"
  parent_issue: "#733"
  sibling_specs:
    - docs/product/features/quick-save.md
    - docs/product/features/persistence.md
    - docs/product/features/sortie.md
  background_comment: "https://github.com/squne121/loop-protocol/issues/622#issuecomment-4698764657"
  related_issues:
    - "#619: Quick Save preparation-only 制限の実装"
    - "#739: autosave / reload wiring（#622 の merge order 前提）"
acceptance:
  - "AC1: docs/product/features/accessibility-save-policy.md が存在する"
  - "AC2: Save は preparation のみ、Load は title_menu / load_menu のみという LoopPhase 契約が明記されている"
  - "AC3: GameSnapshot は schemaVersion / resources / weaponPower / playerMaxHp のまま維持し runtime state を混入しないことが明記されている"
  - "AC4: product Pause の採否と受け入れ条件（HUD・Keyboard・simulation 停止・入力 reset・タイマー停止）が明記されている"
  - "AC5: Assist Suspend の採否と制約（別 key・別 schema・one-shot・delete-on-resume）が明記されている"
  - "AC6: visibilitychange は auto-pause / best-effort flush まで、beforeunload 依存を禁止する方針が明記されている"
  - "AC7: storage failure は no crash / no false success / existing snapshot preservation を維持することが明記されている"
  - "AC8: HTTP origin 上の playtest 検証計画（playtest evidence 記録場所）が明記されている"
non_goals:
  - 出撃中 runtime state を通常 Save として永続化すること
  - localStorage → IndexedDB への移行（MVP 範囲外）
  - beforeunload ベースの中断保存の採用
  - GameSnapshot への runtime state フィールド追加（schemaVersion / resources / weaponPower / playerMaxHp 以外のフィールド混入）
  - cloud save / ネットワーク同期
  - 複数セーブスロット
  - 戦闘中の任意ロード
---

# Save/Load アクセシビリティ互換方針

## Intent

本文書は **#622 の設計決定の正本**である。出撃中 Quick Save / Quick Load 禁止を維持したまま、アクセシビリティ互換の中断・時間制限緩和・復帰手段をどう確保するかの採否判断と根拠をまとめる。

`docs/product/features/persistence.md` および `docs/product/features/quick-save.md` の永続化境界・LoopPhase 契約は本文書より優先する。本文書はこれらに矛盾する仕様変更を行わず、代替案評価として整理する。

## Authority / Conflict Resolution

- **`docs/product/features/quick-save.md`** が progression snapshot の保存挙動（保存対象・storage key・origin scope）の正本。本文書はその方針を継承する。
- **`docs/product/features/persistence.md`** が localStorage 失敗モデル・永続化境界の正本。本文書はこれに従う。
- **`docs/product/features/sortie.md`** が `SortieResult` の transient lifecycle の正本。
- 本文書が主権を持つのは「アクセシビリティ代替案の採否判断」のみである。

## 背景と問題設定

Issue #572 の SSOT spec（`docs/product/features/quick-save.md`）で `preparation`-only Save / `title_menu・load_menu`-only Load が確定した（#619 / PR #868）。

**アクセシビリティ上の主問題は保存媒体（localStorage か否か）ではない。**
リアルタイム戦闘中に「中断できない」「時間制限を調整できない」「離脱で進行が失われる」ことが本質的な問題である。

WCAG 2.2 "Enough Time" 基準では、時間制限について「オフにできる」「調整できる」「延長できる」などの手段、または real-time 例外が問題になる。また自動的に動く・更新される情報には pause/stop/hide の機構が求められる（WCAG 2.2 2.2.2 Pause, Stop, Hide）。

したがって、本調査は「IndexedDB にするか」ではなく、**Pause / timer adjustment / フェーズ境界 Checkpoint / one-shot Assist Suspend の採否比較**を成果物とする。

## 既存の LoopPhase 契約（変更なし）

以下の契約は本 Issue で変更しない。これは設計採否判断の前提条件である。

```
[*] → title_menu
title_menu → preparation: New Game
title_menu → load_menu: Load Game (メニュー選択)
load_menu → preparation: Load slot-1 (storage.load())
load_menu → title_menu: Back
preparation → preparation: Save (storage.save())
preparation → running: Start Sortie
running → result: Victory / Defeat / Timeout
result → preparation: Confirm result
```

**Save は `preparation` フェーズのみ許可**。`running` / `result` / `title_menu` / `load_menu` では `storage.save()` を実行しない。
**Load Game は `title_menu` / `load_menu` フェーズからのみ実行可能**。`running` / `result` フェーズ中は Load Game を実行しない。

## GameSnapshot の不変条件（変更なし）

`GameSnapshot` は以下の 3 フィールドのまま維持する。runtime state フィールドを混入しない。

```ts
type GameSnapshot = {
  schemaVersion: number  // #736 で追加予定
  resources: number
  weaponPower: number
  playerMaxHp: number
}
```

保存しない（combat runtime state）:

- active enemies / bullets・projectiles / current player position
- active `SortieResult` / pending input / frame・tick counter
- DOM・HUD state / Canvas・rendering state
- current HP（ロード時は `playerMaxHp` から初期化）

これは `docs/product/features/persistence.md` の「Snapshot Responsibility Boundary」を継承する。

## 代替案の比較評価

### 案 A：product Pause（採用）

**概要**: 現行の `debugPause` 由来の pause 機能をアクセシビリティ機能として正式昇格する。`running` フェーズ中にキーボード操作（Escape / P キー等）または HUD ボタンで pause できる。

**評価**:

| 観点 | 評価 |
|---|---|
| save-scum リスク | なし（runtime state を保存しない） |
| ゲームバランス | 維持（pause 中は simulation 停止） |
| 実装コスト | 低（既存 debug pause の昇格） |
| WCAG 2.2 対応 | 2.2.2 Pause, Stop, Hide を満たす方向 |
| 中断復帰 | Pause 中の状態は画面に残る（save 不要） |

**採否: M3 採用**。HUD ボタン・Keyboard 操作・pause 中 simulation 停止・入力 reset・状態表示・タイマー停止を受け入れ条件とする。

**受け入れ条件（product Pause）**:

- HUD 上に Pause ボタンを表示する
- Escape / P キー等でキーボードから pause できる
- pause 中は simulation ループを停止する
- pause 直前の入力状態を reset し、resume 時の入力 bleed を防ぐ
- HUD に "Paused" 状態を表示する
- pause 中は戦闘タイマーを停止する

> **実装 Issue**: product Pause の具体的実装は #622 スコープ外（follow-up Issue に分離）。本文書はアクセシビリティ機能としての採用判断のみを記録する。

### 案 B：フェーズ境界 Checkpoint（採用）

**概要**: `preparation` での progression snapshot（現行実装）がフェーズ境界 Checkpoint として機能する。sortie 終了 → debrief → preparation 遷移後に snapshot を保存するため、各 sortie 完了時点が Checkpoint になる。

**評価**:

| 観点 | 評価 |
|---|---|
| save-scum リスク | なし（preparation のみ保存） |
| ゲームバランス | 維持（sortie 結果は確定後のみ保存） |
| 実装コスト | 現行実装で既に実現（追加変更なし） |
| 中断復帰 | sortie 完了済みの進行は reload 後に復元 |
| アクセシビリティ | sortie 内の中断には対応しない |

**採否: 既存実装として M3 採用済み**。各 sortie の結果確定後に progression が保存される。出撃中（`running` フェーズ）の Checkpoint は追加しない。

### 案 C：one-shot Assist Suspend（M4 以降・研究候補）

**概要**: 出撃中に専用の「中断保存」を一度だけ行える機能。アクセシビリティ用途に限定し、通常 Save とは別 key・別 schema として管理する。resume 時に削除（one-shot）。

**評価**:

| 観点 | 評価 |
|---|---|
| save-scum リスク | 設計制約で軽減（one-shot・delete-on-resume） |
| ゲームバランス | 要注意（runtime state を一時保存するため） |
| 実装コスト | 高（別 schema・TTL・cleanup・UI 分離が必要） |
| アクセシビリティ | 長時間セッション中断に有効 |
| M3 適合性 | M3 では採用しない（GameSnapshot の不変条件を壊し得る） |

**採否: M3 では採用しない。M4 以降の研究候補。**

採用する場合の必須制約:

- 通常 Save と別 key（例: `loop-protocol.mvp.assist-suspend.v1`）
- 通常 Save と別 schema（`schemaVersion` と distinct な型識別子）
- one-shot: 1 回 resume したら削除
- `SortieResult` / pending reward は保存しない
- reward claim 済み状態を再現しない
- UI 上は "Save" ではなく "Suspend Sortie" と表示
- 失敗時は現在の progression snapshot に fallback
- アクセシビリティ用途の明示的モード指定を必須とする

### 案 D：beforeunload 中断保存（不採用）

**採否: 不採用。**

`beforeunload` イベントは以下の理由で主軸にできない:

- モバイルブラウザでは信頼性が低い（ページが完全に unload される前に呼ばれない場合がある）
- Firefox の bfcache（ページナビゲーションキャッシュ）に悪影響を与える
- 表示するダイアログ文字列はブラウザが決定し、カスタマイズ不可
- `beforeunload` の途中で localStorage 書き込みが完了しない可能性がある

参照: [MDN - beforeunload event](https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeunload_event)

### 案 E：visibilitychange auto-pause / best-effort flush（採用・限定的）

**採否: auto-pause / best-effort flush に限定して採用。出撃中の runtime save には使用しない。**

`visibilitychange` の `hidden` を「最後に比較的信頼できるセッション終了シグナル」として使い、
以下の目的に限定する:

- `running` フェーズ中の **auto-pause**（ウィンドウ非表示時に simulation を一時停止）
- best-effort なテレメトリ flush

**`beforeunload` 依存を方針文書として禁止する。** `visibilitychange` を runtime save（出撃中 GameSnapshot への中断保存）に使用しない。

参照: [MDN - visibilitychange event](https://developer.mozilla.org/en-US/docs/Web/API/Document/visibilitychange_event)

### 案 F：localStorage → IndexedDB 移行（不採用）

**採否: M3 では不採用。localStorage は MVP の最小保存手段として継続。**

アクセシビリティ問題の本質は保存媒体ではなく「中断できない / 時間制限を調整できない」ことであり、IndexedDB への移行は問題解決にならない。`docs/product/features/persistence.md` の localStorage 方針を継承する。

## 採否まとめ

| 代替案 | 採否 | 適用フェーズ | 主な理由 |
|---|---|---|---|
| 案 A: product Pause | **採用** | M3 | save-scum なし。WCAG 2.2.2 対応。既存 debug pause の昇格 |
| 案 B: フェーズ境界 Checkpoint | **採用（実装済み）** | M3 | preparation 後保存は現行実装で実現済み |
| 案 C: one-shot Assist Suspend | **M4 以降研究候補** | M4〜 | runtime state 保存のため M3 には含めない |
| 案 D: beforeunload 中断保存 | **不採用** | - | モバイル非信頼・bfcache 干渉 |
| 案 E: visibilitychange auto-pause | **限定採用** | M3 | auto-pause のみ。runtime save には不使用 |
| 案 F: IndexedDB 移行 | **不採用** | - | 問題の本質でない。localStorage 継続 |

## storage failure への準拠

本方針は `docs/product/features/persistence.md` の localStorage 失敗モデルを継承する:

- storage failure（`SecurityError` / `QuotaExceededError` / corrupt JSON / localStorage 利用不可）でゲームをクラッシュさせない
- 保存失敗時に「保存成功」フィードバックを表示しない（no false success）
- storage failure 時は既存のロード可能な snapshot を維持する（existing snapshot preservation）
- load したデータは untrusted として validate してから state に反映する

## GitHub Pages / PR preview 同一 origin 衝突への対応

`docs/product/features/persistence.md` が指摘する通り、GitHub Pages 本番と PR preview は同一 origin の localStorage を共有し得る。アクセシビリティ機能の playtest では以下を遵守する:

- E2E / preview 環境では test 実行前に `loop-protocol.mvp.save` を clear する
- Assist Suspend を将来実装する場合は `loop-protocol.mvp.assist-suspend.v1` 等、本番 save と別 key を使用する

## Playtest 計画

**playtest evidence の記録場所**: `docs/product/playtest-log.md`

HTTP origin 上の playtest で以下を検証する（#622 完了条件の AC8 に対応）:

| 検証項目 | 検証方法 |
|---|---|
| Save/Load が preparation / title_menu / load_menu のみ動作する | 手動フェーズ遷移確認 |
| running フェーズ中に Save が no-op になる | HUD ボタン disabled 確認 |
| Pause（案 A 実装後）で simulation が停止する | 戦闘中 Pause 操作確認 |
| visibilitychange hidden で auto-pause が発火する | タブ切り替え確認 |
| storage blocked 環境（incognito 等）でクラッシュしない | storage ブロック確認 |
| corrupt snapshot が安全に fallback する | DevTools で JSON 破損注入 |
| PR preview key が production save を汚染しない | 別 key 分離確認 |

> playtest は M3 全フェーズ統合後（#740 以降）に実施し、`docs/product/playtest-log.md` に証跡を記録する。

## Non-Goals

- 出撃中 runtime state を通常 Save として永続化すること（save-scum 合法化・ゲームバランス破壊のため不採用）
- localStorage → IndexedDB への移行（アクセシビリティ問題の本質でないため M3 では不採用）
- `beforeunload` ベースの中断保存（モバイル非信頼・bfcache 干渉のため不採用）
- `GameSnapshot` に runtime state フィールドを追加すること
- cloud save / ネットワーク同期（`persistence.md` の non-goals を継承）
- 複数セーブスロット（`persistence.md` の non-goals を継承）
- 戦闘中の任意ロード

## 最終推奨案（M3）

> M3 では progression-only Save/Load を維持し、出撃中 runtime save は導入しない。アクセシビリティ互換は product Pause とフェーズ境界 Checkpoint（実装済み）で満たす。Assist Suspend は M4 以降の明示的な accessibility mode として別 schema / one-shot / delete-on-resume で研究する。

## References

- [WCAG 2.2 - Enough Time](https://www.w3.org/TR/WCAG22/) — 2.2.1 Timing Adjustable / 2.2.2 Pause, Stop, Hide
- [MDN - Window: beforeunload event](https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeunload_event) — beforeunload の制限・bfcache 干渉
- [MDN - Document: visibilitychange event](https://developer.mozilla.org/en-US/docs/Web/API/Document/visibilitychange_event) — auto-pause シグナルとしての利用
- [MDN - Window: localStorage property](https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage) — origin-scoped / SecurityError / eviction
- [MDN - Storage quotas and eviction criteria](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria) — QuotaExceededError / IndexedDB の用途
- `docs/product/features/quick-save.md` — progression snapshot SSOT
- `docs/product/features/persistence.md` — localStorage 失敗モデル / 永続化境界 SSOT
- `docs/product/features/sortie.md` — SortieResult transient lifecycle SSOT
- Issue #622 アンカーコメント: https://github.com/squne121/loop-protocol/issues/622#issuecomment-4698764657

## Related

- `docs/product/features/quick-save.md`（LoopPhase 契約・progression snapshot の正本）
- `docs/product/features/persistence.md`（localStorage 失敗モデル・永続化境界の正本）
- `docs/product/features/sortie.md`（SortieResult transient の正本）
- `docs/product/playtest-log.md`（playtest evidence の記録場所）
- Issue #622 / #733 / #739 / #619
