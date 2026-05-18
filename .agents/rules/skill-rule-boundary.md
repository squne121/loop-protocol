# Rule: skill-rule-boundary

skill 本文と rule の責務境界。

## 1. rule（不変条件）

- LOOP_PROTOCOL 全体に適用される **長期不変** の原則を記述
- 配置: `.agents/rules/<id>.md`
- skill / SubAgent が `required_rules` で参照
- 例: 「assets/ を編集しない」「1 Issue = 1 PR」「`--no-verify` 禁止」

## 2. skill（手順）

- 特定の作業フローを **再現可能な手順** として記述
- 配置: `.claude/skills/<name>/SKILL.md`（必要に応じて `references/`、`steps/`、`scripts/` を併設）
- skill は rule に従う前提で書かれる（rule を再説明しない）
- 例: 「Issue 起票の手順」「PR レビューの判定フロー」

## 3. SubAgent（実行担当）

- 特定の役割に特化した実行エージェント
- 配置: `.claude/agents/<name>.md`
- 権限を最小化し（disallowedTools 等）、コンテクストを隔離する
- skill から `Task` ツール経由で呼び出されることが多い

## 4. 重複の禁止

- 同じ内容を rule と skill の両方に書かない
- skill は rule を参照する形（`[file-edit-protocol](.agents/rules/file-edit-protocol.md)` 等）
- rule の更新は skill 群全体に波及するため慎重に（[`skill-sync-policy`](skill-sync-policy.md) で整合確認）

## 5. CLAUDE.md との関係

- `CLAUDE.md` は **プロジェクト憲法**（state/render/systems 分離、固定タイムステップ等）
- 毎セッション自動ロードされる
- rule は CLAUDE.md の補完として、より細かい運用ルールを定義する
- 内容が重複する場合は CLAUDE.md を優先（rule からは参照のみ）

## 関連

- [`skill-sync-policy`](skill-sync-policy.md)
- [`subagent-design-policy`](subagent-design-policy.md)
