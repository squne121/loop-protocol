---
doc_id: feature-quick-save
status: draft
related_issue: "#572"
parent_issue: "#483"
updated_by: "#619"
trace_links:
  self_issue: "#572"
  parent_issue: "#483"
  background_pr: "#570"
  implementation_issue: "#619"
  future_issues:
    - "#620: Save ボタンの有効/無効表示と最小限のフィードバックを実装する"
    - "#621: LocalGameStorage の例外処理を実装する（QuotaExceededError / parse 失敗）"
    - "#622: Save のアクセシビリティ互換代替案（中断セーブ・チェックポイント等）を調査する"
    - "#740: HTTP origin 上の runtime verification（M3 playtest 後）"
acceptance:
  - AC1: docs/product/features/quick-save.md が存在する
  - AC2: progression-only スナップショットであると明記されている
  - AC3: 保存対象フィールド（resources / weaponPower / playerMaxHp）が明記されている
  - AC4: 非保存フィールド（current HP・enemies・projectiles 等）が明記されている
  - AC5: ゲームデザイン方針（準備フェーズ制限・Load Game ポリシー）が明記されている
  - AC6: ストレージ仕様（loop-protocol.mvp.save・browser-local・origin-scoped）が明記されている
  - AC7: UI 自己説明・保存の意味方針が明記されている
  - AC8: 将来参照が少なくとも 3 件記載されている
non-goals:
  - Save UI 実装（#619 でポリシーを確定; UI は別 Issue）
  - 準備フェーズの状態遷移実装（#619 で実装済み）
  - ボタン UI 名の変更判定（Quick Save → Save に変更済み; #619）
related_tests:
  - tests/storage.test.ts
  - tests/sortie-system.test.ts
---

# Quick Save

## Intent

本文書は Quick Save の現行実装スコープ・ゲームデザイン上の役割・ストレージ仕様・将来方針の SSOT である。Save / Load Game の UI 実装・コード変更・準備フェーズ状態遷移の具体的定義は別 Issue で扱う。

## 現行実装の実態

Quick Save は **progression-only スナップショット**（進行データのみの保存）であり、出撃状態を含む完全なセーブではない。

UI ラベルとして "Quick Save" を使用しているが、実装上は `LocalGameStorage` が `resources / weaponPower / playerMaxHp` の 3 フィールドのみを localStorage に書き出す。

## 保存対象フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `resources` | number | プレイヤーの所持リソース量 |
| `weaponPower` | number | 武器パワー値 |
| `playerMaxHp` | number | プレイヤーの最大 HP |

## 非保存フィールド

以下のフィールドは Quick Save の対象外である:

- **current HP**: 現在の HP は保存しない（ロード時は playerMaxHp から復元）
- **出撃の状態と進行**: sortie フェーズ・経過タイムスタンプ・進行カウントは保存しない
- **enemies**: 敵の位置・HP・状態は保存しない
- **projectiles**: 飛翔体（弾）の状態は保存しない
- **戦闘中の状態**: その他の in-combat runtime state は保存しない
- **プレイテスト証跡メタデータ**: `PlaytestEvidence` 等の証跡フィールドは保存しない（Issue #571 参照）

### Issue #571 との境界

プレイテスト証跡メタデータ（`PlaytestEvidence`）は Quick Save の対象外。証跡エクスポートは Issue #571 で実装済みの export panel が担う（game state / Quick Save との統合は禁止）。

## 現行 Load 動作（#619 更新）

- **起動時の auto-load は廃止**。起動直後の state は `title_menu` フェーズであり、`storage.load()` の snapshot を自動適用しない
- 起動時に storage の probe（snapshot 有無の確認）は行う（Load Game ボタンの enabled/disabled 制御のため）
- **Load Game は `title_menu` / `load_menu` フェーズからのみ実行可能**:
  - `title_menu` で Load Game ボタンを押すと `load_menu` に遷移する
  - `load_menu` で Load slot-1 ボタンを押すと `storage.load()` を呼び、成功時に `preparation` フェーズへ遷移する
- 復元対象: `resources`, `weaponPower`, `playerMaxHp` のみ
- `player.hp` は保存時 HP ではなく `playerMaxHp` で初期化される
- sortie / enemies / projectiles / cooldown / result / runtime は復元されない
- JSON parse 失敗または required number field 欠落時は `null` 扱いとなり、default initial state へ fallback する

## ストレージ仕様

| 項目 | 値 |
|---|---|
| localStorage キー | `loop-protocol.mvp.save` |
| スコープ | browser-local（ブラウザローカル） |
| オリジンスコープ | origin-scoped（オリジン分離） |
| 機密情報 | 含まない（non-sensitive） |
| ユーザー改変可能性 | ユーザーは DevTools から直接改変可能 |

### 将来課題（ストレージ）

欠損・破損・ブロック・容量超過時の処理（エラーハンドリング・フォールバック）は将来の実装 Issue で定義する。

## Storage 失敗モード / Trust Model

| 失敗・脅威 | 現行挙動 | 将来対応 |
|---|---|---|
| localStorage 利用不可 | load は null fallback。save は storage 未取得なら no-op | UI feedback を将来 Issue で定義する |
| SecurityError | browser policy / invalid origin で発生し得る | save/load 初期化を crash させない |
| QuotaExceededError | `setItem` で発生し得る。現行 save は未捕捉 | try/catch + user-visible failure feedback（#621 の対象） |
| private/incognito | 永続保存を保証できない | "durable save" と誤記しない |
| same-origin sharing | path 単位で隔離されない | key namespace と schemaVersion を維持する |
| ユーザー / XSS による改変 | save data は信頼できない | parse/validate/clamp してから state に反映する |

## ゲームデザイン方針（#619 更新）

### Save 操作の準備フェーズ制限

Save 操作（`LocalGameStorage.save()` の実際の呼び出し）は `preparation` フェーズのみ許可する。

`running` / `result` / `title_menu` / `load_menu` フェーズでは `storage.save()` を実行しない。これによりゲームバランスへの悪影響（戦闘中の save-scum 等）を防ぐ。

**`result` フェーズの autosave は廃止（#619 B2/B3 更新）**: 以前は `onClaimReward()` が `result` フェーズ中に `storage.save()` を呼んでいたが、この動作は廃止した。現在の実装では `confirmResult()` が pending reward を自動 claim したうえで `preparation` フェーズへ遷移し、遷移後に `storage.save()` を呼ぶ。これにより save タイミングが `preparation` フェーズ内に統一される。

### Load Game ポリシー

ロード操作は **Load Game** として、ゲーム開始前メニュー（`title_menu` / `load_menu` フェーズ）からのみ実行可能とする（旧ロード名称は廃止）。

Load Game を実行すると、スナップショットを復元して `preparation` フェーズに入る。`running` / `result` フェーズ中は Load Game を実行しない（ゲームバランス保護）。

この方針は 2026-06-13 の人間デザインコメント（Issue #619#issuecomment-4697787543）で確定した。

### フェーズ状態機械（#619 実装）

`LoopPhase` は以下の遷移を定義する（#619 で実装済み）:

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

### Runtime Verification（deferred）

HTTP origin 上の実動作確認（フェーズ遷移・ボタン状態・Save/Load 実挙動）は M3 playtest（全フェーズ遷移統合後）に deferred とする。
参照: Issue #740 または後続 Issue で明示的に対応する。

## UI 方針

### UI 自己説明・保存の意味

Quick Save の役割や保存の意味を説明するコピー（テキスト）をボタン近辺に表示する方針は **採用しない**。

ボタンの有効/無効表示（準備フェーズ以外では無効化）および最小限の成功/失敗フィードバックは将来の実装で必要であるが、これは「保存の意味を説明するコピー」の不採用方針とは独立した実装タスクとして扱う。

## 将来の実装 Issue への参照

以下の項目は現行スコープ外であり、それぞれ別 Issue で実装・定義する:

1. **準備フェーズ制限の実装** (#619, 実装済み): Save / Load Game をフェーズ状態機械から導出し、`preparation` / `title_menu` / `load_menu` 制限を実装した。
2. **UI フィードバック** (#620): Save ボタンの有効/無効表示（準備フェーズ外では無効化）および最小限の成功/失敗フィードバック
3. **壊れたセーブデータの処理** (#621): 欠損・破損・ブロック・容量超過時のエラーハンドリングとフォールバック
4. **アクセシビリティ互換の保存代替案** (#622): 採否判断と設計方針を `docs/product/features/accessibility-save-policy.md` に記録済み。product Pause・フェーズ境界 Checkpoint を M3 採用、Assist Suspend を M4 以降研究候補とした。
5. **HTTP origin 上の runtime verification** (#740, deferred): M3 playtest（全フェーズ遷移統合後）に実施予定。
