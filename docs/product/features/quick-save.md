---
doc_id: feature-quick-save
status: draft
related_issue: "#572"
parent_issue: "#483"
trace_links:
  self_issue: "#572"
  parent_issue: "#483"
  background_pr: "#570"
  future_issues:
    - "#619: Quick Save を準備フェーズ限定に制限し Quick Load を実装する"
    - "#620: Quick Save ボタンの有効/無効表示と最小限のフィードバックを実装する"
    - "#621: LocalGameStorage の例外処理を実装する（QuotaExceededError / parse 失敗）"
    - "#622: Quick Save のアクセシビリティ互換代替案（中断セーブ・チェックポイント等）を調査する"
acceptance:
  - AC1: docs/product/features/quick-save.md が存在する
  - AC2: progression-only スナップショットであると明記されている
  - AC3: 保存対象フィールド（resources / weaponPower / playerMaxHp）が明記されている
  - AC4: 非保存フィールド（current HP・enemies・projectiles 等）が明記されている
  - AC5: ゲームデザイン方針（準備フェーズ制限・Quick Load 必要性）が明記されている
  - AC6: ストレージ仕様（loop-protocol.mvp.save・browser-local・origin-scoped）が明記されている
  - AC7: UI 自己説明・保存の意味方針が明記されている
  - AC8: 将来参照が少なくとも 3 件記載されている
non-goals:
  - Quick Save/Quick Load の UI 実装・コード変更（別 Issue）
  - 準備フェーズの状態遷移実装（将来 Issue）
  - ボタン UI 名の変更判定（Quick Save vs Progress Save）（スコープ外）
related_tests: []
---

# Quick Save

## Intent

本文書は Quick Save の現行実装スコープ・ゲームデザイン上の役割・ストレージ仕様・将来方針の SSOT である。Quick Save/Quick Load の UI 実装・コード変更・準備フェーズ状態遷移の具体的定義は別 Issue で扱う。

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

## 現行 Load 動作

- 起動時のみ `storage.load()` を呼び、`createInitialGameState(snapshot)` に渡す
- 手動 Quick Load UI/action は現行実装に存在しない
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

## ゲームデザイン方針

### 出撃中の Quick Save / Quick Load 禁止

出撃中（sortie フェーズ中）の Quick Save および Quick Load は **禁止**する。

セーブ・ロード操作は準備フェーズ（sortie 開始前の状態）にのみ許可する。これによりゲームバランスへの悪影響（戦闘中の save-scum 等）を防ぐ。

### 準備フェーズ制限

現行実装では `main.ts` が初期化後に直接 `startSortie(...)` を呼び出すため、明確な UI 準備フェーズが存在しない。Quick Save / Quick Load の有効化に伴い、具体的な状態遷移（準備フェーズ → 出撃フェーズ → 結果フェーズ）は将来の実装 Issue で定義する。

### Quick Load の必要性

Quick Save に対する対の操作として **Quick Load** が必要である。Quick Load の実装は別 Issue で扱う。

## UI 方針

### UI 自己説明・保存の意味

Quick Save の役割や保存の意味を説明するコピー（テキスト）をボタン近辺に表示する方針は **採用しない**。

ボタンの有効/無効表示（準備フェーズ以外では無効化）および最小限の成功/失敗フィードバックは将来の実装で必要であるが、これは「保存の意味を説明するコピー」の不採用方針とは独立した実装タスクとして扱う。

## 将来の実装 Issue への参照

以下の項目は現行スコープ外であり、それぞれ別 Issue で実装・定義する:

1. **準備フェーズ制限の実装** (#619): Quick Save / Quick Load を出撃中に無効化する状態遷移定義（localStorage キー `loop-protocol.mvp.save` の読み書きタイミング制御を含む）
2. **UI フィードバック** (#620): Quick Save ボタンの有効/無効表示（準備フェーズ外では無効化）および最小限の成功/失敗フィードバック
3. **壊れたセーブデータの処理** (#621): 欠損・破損・ブロック・容量超過時のエラーハンドリングとフォールバック
4. **アクセシビリティ互換の保存代替案** (#622): 中断セーブ・チェックポイント等、ブラウザ localStorage に依存しない保存方式の検討
