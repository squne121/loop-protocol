# docs/dev/workflows/

## 配置意図

本ディレクトリは、`LOOP_PROTOCOL` の主要ループ（`issue-refinement-loop` / `impl-review-loop`）に関する**運用単位別の詳細設計ノート**を格納する。

## 位置づけ（SSOT 階層内の役割）

```text
[SSOT 正本]
  docs/dev/workflow.md              ← 開発フロー全体の SSOT
  docs/dev/agent-skill-boundaries.md ← SubAgent / Skill 責務境界
  .claude/skills/<skill>/SKILL.md   ← 各 Skill の手順・LOOP_STATE 定義
  .claude/agents/<agent>.md         ← 各 SubAgent の定義

[派生設計ノート（derived_design_note）] ← 本ディレクトリ
  docs/dev/workflows/*.md
```

本ディレクトリの各ファイルは `ssot_classification: derived_design_note` かつ `conflict_rule: canonical_sources_win` であり、**正本と矛盾した場合は常に正本が勝つ**。

## ファイル一覧

| ファイル | 対象ループ | 説明 |
|---|---|---|
| `issue-refinement-loop-design.md` | issue-refinement-loop | planner/orchestrator 境界・anchor comment 設計・escalation 分類 |
| `impl-review-loop-design.md` | impl-review-loop | control-plane/data-plane 境界・LOOP_STATE field owner・PR conflict 方針 |

## 使い分け

| シナリオ | 参照先 |
|---|---|
| ループの日常的な実行 | `.claude/skills/<skill>/SKILL.md`（正本を直接参照） |
| architecture review / contract migration | 本ディレクトリの設計ノート |
| failure-mode update / escalation runbook | 本ディレクトリの設計ノート |
| SubAgent の責務境界確認 | `docs/dev/agent-skill-boundaries.md` |

## 更新ルール

本ディレクトリのファイルを変更する際は、`canonical_sources` に列挙された正本と矛盾しないように注意する。変更が `docs/dev/ssot-registry.md` への更新を必要とする場合は同一 PR で行う。
