---
status: draft
issue: "#284"
parent_issue: "#254"
doc_id: playtest-protocol
canonical_source: docs/product/playtest-protocol.md
sdd_boundary: procedure
trace_links:
  - docs/product/mvp-scope.md
  - docs/adr/0002-sdd-tool-adoption.md
---

# Playtest Protocol

## Intent
この文書は、LOOP_PROTOCOL におけるプレイテストの実施手順、フィードバック分類、および仕様変更ゲート（Spec Delta Gate）を定義する。
Spec-Driven Development (SDD) において、プレイテストは設計仮説を検証し、必要に応じて仕様を補正するための重要なフィードバックループである。

## Session Planning
- **対象ビルド**: プレイテスト実施時の commit SHA または PR 番号を特定する。
- **検証仮説**: `docs/product/mvp-scope.md` で定義された `MVP-HYP-NNN` から今回の検証対象を選択する。
- **タスク設計**: プレイヤーが遂行すべき具体的なシナリオ（Task Script）を用意する。

## Participant / Tester Handling
- **テスター選定**: ターゲットプレイヤー層、または開発メンバーから選定する。
- **匿名化**: 公開リポジトリにはテスターの本名や連絡先を記録せず、`tester_profile` または匿名 ID（例: `P1`）を使用する。
- **同意**: テスト内容の記録（録画・メモ）に関する合意を事前に得る。
- **AI/Human の役割分担**:
    - **Human**: 感情的反応、UX、直感的な面白さ、意図しない複雑さの検知。
    - **AI**: 数値的なバランス、機械的な不整合、特定条件下での境界挙動の網羅的確認。

## AI-driven Playtest Automation
- **自動化の範囲**: 
    - 大量のプレイ回数を必要とする確率的要素の検証（ドロップ率、エンカウント率等）。
    - 異なるパラメータセットでの性能比較。
- **結果の統合**: AI による自動プレイテスト結果も `playtest-log.md` の形式に従って記録し、人間による評価（`developer_interpretation`）を必須とする。
- **制限**: AI プレイテストは「面白いかどうか」の最終判断を代替するものではなく、人間が面白さに集中するための「前提条件（バグや不整合の排除）」を整えるために使用する。

## Task Script
- **明確な目標**: プレイヤーに「何を達成してほしいか」を明確に伝える。
- **誘導の禁止**: 「ここをクリックしてください」といった具体的な操作指示は避け、プレイヤーの自然な行動を観察する。

## Observation Rules
- **発話思考法 (Think Aloud)**: プレイヤーが考えていること、感じていることを口に出してもらいながら観察する。
- **観察の集中**: ファシリテーターはプレイヤーの行動、迷い、感情的な反応（混乱・興奮・退屈）を記録する。

## Feedback Classification
プレイテストの結果は以下の 4 カテゴリに分類する。

| カテゴリ | 内容 | 対応アクション |
|---|---|---|
| `bug` | 意図した仕様通りに動作していない | Implementation Task Issue 起票 |
| `balance/tuning` | 数値調整や微細な感触の改善 | Spec Delta Issue 推奨（軽微なら実装時に理由付記） |
| `design hypothesis invalidated` | 設計の前提や仮説が誤っていた | **Spec Delta Issue 必須**（実装前に仕様修正） |
| `unclear/needs-more-data` | 判断材料が不足している | 次回プレイテストでの継続検証 |

## Spec Delta Gate
- **禁止事項**: `design hypothesis invalidated` と判定されたフィードバックを、仕様（docs/product/**）の更新なしに直接コード変更してはならない。
- **フロー**: playtest-log → Spec Delta Issue (doc update) → Implementation Issue (code change) → PR → Next Playtest。

## Decision Meeting
- プレイテスト終了後、開発チームでログを振り返り、各項目の `classification` と `decision`（対応方針）を確定する。
- 決定事項は `docs/product/playtest-log.md` の `decision` フィールドに記録する。

## Privacy / PII Handling
- **PII 禁止**: 公開リポジトリには個人名、顔写真、連絡先、特定の個人を識別できる発話全文を保存しない。
- **証跡管理**: 録画データや生のメモはリポジトリ外の安全な場所に保管し、GitHub には要約と匿名化された引用のみを記載する。
- **引用**: 引用は短く、文脈を維持しつつ個人を特定できないよう加工する。
