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
`docs/product/mvp-scope.md` で定義された Measurement Contract を実運用するための手順書である。

## Session Planning
- **対象ビルド**: プレイテスト実施時の commit SHA または PR 番号を特定する。
- **検証仮説**: `docs/product/mvp-scope.md` で定義された `HYP-MVP-001/002/003` から今回の検証対象を選択する。
- **タスク設計**: プレイヤーが遂行すべき具体的なシナリオ（Task Script）を用意する。

## Participant / Tester Handling
- **基本方針**: 本プロジェクトは個人趣味開発であり、外部テスターを募集してのプレイテストは実施しない。
- **主担当**: プレイテストは開発者によるセルフプレイテストを中心に実施する。
- **人間が扱う領域**: 面白さ、違和感、ストレス、迷い、納得感、操作感などの感情的・UX的な判断は、人間の開発者プレイテストで扱う。
- **匿名化**: 公開リポジトリには個人名や連絡先を記録せず、必要に応じて `developer-self` または匿名 ID を使用する。
- **同意**: 外部参加者を前提としないため、第三者の録画・発話記録は原則発生しない。例外的に第三者の協力を得る場合は、記録範囲について事前合意を得る。

## AI-driven Playtest Automation
- **目的**: AI/自動化プレイテストは、仕組みの正しさ、到達可能性、ルール破綻、数値バランス、回帰、定量メトリクスの収集を目的とする。
- **evidence 境界**: deterministic event log と qualitative self-explanation は分離して記録する。自由記述は `deterministic_events` に混ぜない。
- **metadata 最低要件**: commit SHA、GitHub Actions run ID/run attempt、page URL または artifact URL、artifact 名、retention-days、viewport、DPR、declared browser zoom、userAgent、timezone、paused/running state、screenshot path を export 可能にする。
- **runtime event の最小集合**: `command_use`、`command_noop`、`target_switch`、`local_threat_sample`、`ally_survival` を replay 再現可能な順序 (`tick ASC`, `event_type_order ASC`, `command_seq ASC`, `entity_id ASC`) で保持する。
- **Playtest Mode**:

| mode | 用途 | 許可する根拠 | 禁止する主張 |
|---|---|---|---|
| `human_internal` | 開発者による UX/違和感確認 | 観察メモ、redacted quote、仮説違反 | 一般プレイヤー代表性 |
| `ai_simulation` | 仕組み・バランス・探索量の検査 | seed、agent profile、metric、replay/log | 楽しさ・感情の代替 |
| `browser_automation` | UI/Canvas/DOM 統合、リグレッション | Playwright trace、Vitest Browser Mode、CI result | UX 妥当性の代替 |
| `human_external` | 外部テスター | 現時点 out of scope | 募集・録画・PII 保存 |

- **人間確認ゲート**: AI/自動化で検出された問題が UX や設計仮説に影響する場合は、開発者による human playtest で影響を確認してから `Spec Delta Issue` または `Implementation Issue` に分類する。

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
- **基本原則**: 公開リポジトリには個人名、連絡先、顔・声の録画 URL、識別可能な発話全文を保存しない。
- **PII 禁止**:
  - `raw_audio_video_allowed: false`
  - `raw_transcript_committed: false`
  - `pii_reviewed_before_commit: true`
- **引用の方針**: 
  - `player_quote` の代わりに `player_quote_redacted` または `player_observation_summary` を使用する。
  - 引用は短く、文脈を維持しつつ個人を特定できないよう加工する。
- **証跡管理**: 録画データや生のメモはリポジトリ外の安全な場所に保管し、GitHub には要約と匿名化された引用のみを記載する。
