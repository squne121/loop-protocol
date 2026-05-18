---
name: ssot-discovery
description: LOOP_PROTOCOL の docs/ 配下を Single Source of Truth (SSOT) として扱い、Issue / PR / タスクのキーワードから関連 SSOT ドキュメントを再現可能に発見するスキル。実装エージェント・レビューエージェントがタスク着手前のコンテクスト収集に必ず使う。「関連ドキュメント探して」「該当する仕様書は」「ssot 探索」「SSOT 確認」「docs 検索」などのトリガーで使用。.agents/rules/ に分散していた SSOT 参照ルールを単一エージェントスキルに集約する。
---

# ssot-discovery — SSOT ドキュメント探索

LOOP_PROTOCOL の `docs/` 配下は **プロジェクトの単一の真実の情報源（SSOT）** である。
本スキルは、任意のタスク（Issue 番号 / PR 番号 / 自然言語クエリ / 変更対象ファイルパス）から関連 SSOT を発見し、それらの場所と要点をメインセッション / SubAgent へ返す。

## Use When

- 実装エージェント / レビューエージェント / orchestrator skill がタスク着手前に SSOT を収集したい時
- 「該当する仕様書は？」「関連 ADR は？」「workflow ルールはどこに書いてある？」
- skill / agent が `required_rules:` 相当の参照を行いたい時（旧 `.agents/rules/index.md` の代替）

## Do Not Use When

- 即時に既知のファイル 1 つだけ読むなら直接 `Read` で済む
- `src/` 配下のコード探索は本スキルの対象外（`docs/` 限定）

## Input

以下のいずれかを受ける（複数可）:

- `task_keywords`（自然言語キーワードのリスト）— 例: `["worktree", "issue contract"]`
- `target_paths`（変更対象のファイル/ディレクトリ）— 例: `["src/systems/MovementSystem.ts"]`
- `issue_number` / `pr_number`（gh CLI 経由で本文を取得して keyword 抽出）

## Output

`SSOT_DISCOVERY_RESULT_V1` YAML 形式で返す（[references/output-contract.md](references/output-contract.md) 参照）:

```yaml
SSOT_DISCOVERY_RESULT_V1:
  status: ok | partial | failed
  matched_documents:
    - path: docs/dev/workflow.md
      relevance: high | medium | low
      reason: "worktree 運用フローの正本"
      sections:
        - "## Worktree 配置"
  unmatched_keywords: []
  notes: []
```

## Procedure

### Step 1: SSOT カタログを把握する

LOOP_PROTOCOL の SSOT カタログは [references/ssot-catalog.md](references/ssot-catalog.md) で固定している。本スキル内で `docs/` 配下をスキャンする際は本カタログを最初に読む。

カタログは以下を持つ（例）:
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体（最も汎用的）
- `docs/dev/directory-structure.md` — ディレクトリ責務
- `docs/dev/current-focus.md` — 現在のフェーズ・優先項目
- `docs/dev/imported-harness-triage.md` — 流用 agent/skill の判定表
- `docs/adr/*.md` — アーキテクチャ決定記録
- `docs/product/*.md` — プロダクト仕様

### Step 2: 入力からキーワードを抽出する

- `task_keywords` をそのまま使う
- `target_paths` から「ディレクトリ名」「ファイル名語幹」をキーワード化
- `issue_number` / `pr_number` がある場合は `gh issue view <番号> --json title,body --jq '.title + "\n" + .body'` で取得し、見出し・コード参照・固有名詞を抽出

### Step 3: マッチ判定

[scripts/match-ssot.sh](scripts/match-ssot.sh) を呼んで、キーワードと SSOT カタログのマッチを取る。

```bash
.claude/skills/ssot-discovery/scripts/match-ssot.sh \
  --keywords "worktree,issue contract" \
  --paths "src/systems/"
```

スクリプトは以下を返す:
- `docs/` 配下で本文に該当キーワードを含むファイル
- ディレクトリ → SSOT のマッピング（例: `src/systems/` → `docs/adr/0001-architecture-baseline.md`）
- relevance スコア（high: タイトル/見出しに含む、medium: 本文一致、low: 関連推定）

### Step 4: 結果整形

スクリプト出力を `SSOT_DISCOVERY_RESULT_V1` YAML に整形して返す。
不一致キーワードは `unmatched_keywords` に列挙し、SSOT 未整備の論点を可視化する。

## Guard Rails

- `docs/` 配下のみを対象（`src/` のコード探索は別タスク）
- マッチしないキーワードがあっても fail せず `partial` で返す（SSOT 未整備のヒントとして残す）
- 大量出力にならないよう、relevance high のみ詳細表示、medium/low は path 列挙のみ

## ディレクトリごとの CLAUDE.md との関係

- 本スキルは **横断的な SSOT 探索** を担う
- **特定ディレクトリ配下の不変条件** は各 `<dir>/CLAUDE.md`（Claude Code が自動ロード）に集約されている
- 重複する内容は CLAUDE.md を正本とし、本スキルは SSOT 探索手順のみを定義する

## 関連

- ルート `CLAUDE.md`
- `docs/CLAUDE.md`
- [references/ssot-catalog.md](references/ssot-catalog.md)
- [references/output-contract.md](references/output-contract.md)
- [scripts/match-ssot.sh](scripts/match-ssot.sh)
