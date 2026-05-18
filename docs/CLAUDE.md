# docs — プロジェクト SSOT（Single Source of Truth）

## 位置づけ

`docs/` 配下は **プロジェクトの単一の真実の情報源（SSOT）** である。
仕様・運用ルール・アーキテクチャ決定は本ディレクトリの Markdown を正本とする。

## 構成

| サブディレクトリ | 内容 |
|---|---|
| `docs/adr/` | アーキテクチャ決定記録（Architecture Decision Records） |
| `docs/dev/` | 開発運用ドキュメント（workflow、directory-structure、current-focus、imported-harness-triage 等） |
| `docs/product/` | プロダクト仕様（game-overview、requirements 等） |

## AI エージェントによる参照手順

実装エージェント・レビューエージェントは、タスク着手前に `docs/` 配下から関連 SSOT ドキュメントを探索する。
**探索フローは `.claude/skills/ssot-discovery` スキルに集約** されている。skill 経由で再現可能に行うこと。

## 編集規約

- 大きな運用変更は ADR として `docs/adr/NNNN-<topic>.md` を追加
- 既存 SSOT を変更する PR は、影響を受ける skill / agent / コードを同 PR で更新する
- 単純なタイポ修正・表現改善は任意の PR で可

## 関連

- ルート `CLAUDE.md`
- `.claude/skills/ssot-discovery/SKILL.md`（SSOT 探索フロー）
- `docs/dev/workflow.md`（運用フロー全体）
