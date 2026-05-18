# Rule: subagent-design-policy

SubAgent 設計の共通指針。

## 1. 単一責任

- 1 SubAgent = 1 つの明確な役割
- 「実装と PR レビューを両方やる」のような兼務はしない
- 例:
  - `implementation-worker`: 実装のみ
  - `pr-reviewer`: PR レビューのみ
  - `test-runner`: 検証コマンド実行のみ

## 2. 権限最小化

- `tools:` フロントマターで使用ツールを限定
- 書き込みが不要な SubAgent は `disallowedTools: [Edit, Write, MultiEdit]` を必ず指定
- `Bash` を許す場合も、コマンドの種類を SKILL.md 本文で明示する

## 3. コンテキスト隔離

- SubAgent はメインセッションの会話履歴を引き継がない
- 必要な情報は **プロンプト引数** または **ファイルシステム経由** で渡す
- 推奨: `.claude/plans/<task>-handoff.md` を SubAgent に Read させる

## 4. 出力契約

- 構造化フォーマット（YAML / JSON）で結果を返す
- 例: `POST_MERGE_CLEANUP_REPORT_V1`、`ISSUE_AUTHOR_COVERAGE_V1`
- メインセッションは出力を parse して次アクションを決める

## 5. モデル選択

LOOP_PROTOCOL では Claude モデル系を前提：

| タスク特性 | 推奨 model |
|---|---|
| 大規模調査・複雑判定（コードレビュー等） | `sonnet`（Sonnet 4.6） |
| 決定論的な集計・形式変換（cleanup レポート等） | `haiku`（Haiku 4.5） |
| 計画立案・難しい設計議論 | `opus`（Opus 4.7） |

`gpt-5.x` 等の他モデル指定は LOOP_PROTOCOL では使わない。

## 6. ネスト委譲の禁止

- SubAgent から更に SubAgent を呼ぶ（Agent ツールを使う）ことは原則禁止
- 必要な場合は `disallowedTools: [Agent]` で明示的に禁止する
- 多段委譲が必要なフローは orchestrator skill 側で設計する

## 関連

- [`orchestrator-skill-policy`](orchestrator-skill-policy.md)
- [`skill-rule-boundary`](skill-rule-boundary.md)
