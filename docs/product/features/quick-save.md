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
    - 準備フェーズ制限・状態遷移定義（未起票）
    - Quick Load 実装（未起票）
    - 壊れたセーブデータ・欠損・容量超過時の処理（未起票）
    - アクセシビリティ互換の保存代替案（中断セーブ・チェックポイント等）（未起票）
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
- **プレイテスト証跡メタデータ**: `PlaytestEvidence` 等の証跡フィールドは保存しない

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

1. **準備フェーズ制限の実装**: Quick Save / Quick Load を出撃中に無効化する状態遷移定義（localStorage キー `loop-protocol.mvp.save` の読み書きタイミング制御を含む）
2. **Quick Load の実装**: セーブデータを localStorage から読み出し、ゲーム状態を復元する機能
3. **壊れたセーブデータの処理**: 欠損・破損・ブロック・容量超過時のエラーハンドリングとフォールバック
4. **アクセシビリティ互換の保存代替案**: 中断セーブ・チェックポイント等、ブラウザ localStorage に依存しない保存方式の検討
