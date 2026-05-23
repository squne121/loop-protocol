---
name: ssot-discovery
description: LOOP_PROTOCOL の docs/ 配下を Single Source of Truth として扱い、Issue / PR / タスク記述から関連 SSOT を機械的に発見する。実装やレビュー前に「関連ドキュメントを探す」「該当 ADR を確認する」「workflow ルールはどこ」「テンプレ仕様は」など SSOT 参照が必要なあらゆる場面で使うこと。AI が docs/ を都度全文 grep して見落とすパターンを防ぐため、人間が「ssot」「SSOT」と明示しなくても、Issue 番号や変更対象パスが言及される作業着手フェーズでは積極的にトリガーする。
---

# ssot-discovery

LOOP_PROTOCOL の `docs/` 配下は **プロジェクトの単一の真実の情報源（SSOT）** である。
本スキルは任意の入力（Issue / PR 番号、キーワード、変更対象パス）から関連 SSOT を機械的に発見し、その場所と関連度を構造化して返す。

## Why this skill exists

各エージェントが独自に「関連しそうな doc を grep する」と、見落としと冗長な探索が積み重なる。SSOT 探索を 1 つのスキルに集約することで、

- カタログ更新時に各エージェント側を直さなくていい（`docs/dev/ssot-registry.md` を 1 箇所修正）
- 結果フォーマットが固定（[references/output-contract.md](references/output-contract.md)）で、呼び出し側が確実にパースできる
- マッチ判定スクリプトを通すので、人間が再現できる（同入力 → 同出力）

ことを保証する。各層特有の不変条件は各ディレクトリの `CLAUDE.md` に集約されているため、本スキルでそれらを再説明しない。

> **SSOT エントリの正本**: SSOT エントリは `docs/dev/ssot-registry.md`（docs 層）を参照すること。
> `references/` 配下の手動キャッシュファイルは廃止済みである。新規エントリ追加時は `docs/dev/ssot-registry.md` のみを手編集すること。フォールバックは行わない。

## Inputs

以下のいずれか（複数可）:

| 入力 | 例 |
|---|---|
| `task_keywords` | `["worktree", "issue contract"]` |
| `target_paths` | `["src/systems/MovementSystem.ts"]` |
| `issue_number` / `pr_number` | `42`（gh CLI で本文取得 → キーワード化） |

## Output

`SSOT_DISCOVERY_RESULT_V1` YAML（詳細は [references/output-contract.md](references/output-contract.md)）。
`matched_documents` は relevance 順（high → medium → low）、`unmatched_keywords` は SSOT 未整備の示唆として返す。

## Procedure

1. `docs/dev/ssot-registry.md` を正本として参照する。registry が読めない場合は `match-ssot.sh` が `status: failed` を返す（手動キャッシュへのフォールバックは行わない）。`docs/` 直下スキャンより先にエントリを読むことで、ディレクトリ → SSOT の事前定義マッピングを活かせる
2. 入力からキーワードを抽出する：
   - `task_keywords` はそのまま
   - `target_paths` はディレクトリ名・ファイル名語幹を切り出す
   - `issue_number` / `pr_number` は `gh issue view <N> --json title,body --jq '.title+"\n"+.body'` で取得して見出し・固有名詞を拾う
3. [scripts/match-ssot.sh](scripts/match-ssot.sh) を呼ぶ：
   ```bash
   .claude/skills/ssot-discovery/scripts/match-ssot.sh \
     --keywords "<comma,separated>" \
     --paths "<comma,separated>"
   ```
4. 出力 YAML をそのまま呼び出し側に返す（散文での再要約はしない — 出力契約が崩れる）

## Examples

**Example 1 — 変更対象パスから SSOT を引く**

呼び出し：
```bash
.claude/skills/ssot-discovery/scripts/match-ssot.sh --paths "src/systems/MovementSystem.ts"
```
返却（抜粋）：
```yaml
SSOT_DISCOVERY_RESULT_V1:
  status: ok
  matched_documents:
    - path: "docs/adr/0001-architecture-baseline.md"
      relevance: "low"
      reason: "directory mapping from src/systems"
```
呼び出し側はこの ADR を Read してから systems 変更に着手する。

**Example 2 — キーワードから運用ルールを引く**

呼び出し：
```bash
.claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "worktree,1 issue 1 pr"
```
返却（抜粋）：
```yaml
SSOT_DISCOVERY_RESULT_V1:
  status: partial
  matched_documents:
    - path: "docs/dev/workflow.md"
      relevance: "medium"
      reason: "body match for 'worktree'"
  unmatched_keywords: ["1 issue 1 pr"]
```
`unmatched_keywords` は SSOT 未整備のサインとして人間レビューに残す（勝手に新 SSOT を作らない）。

**Example 3 — Issue 番号から探索**

```bash
KW=$(gh issue view 42 --json title,body --jq '.title + " " + .body' | tr -s '[:punct:][:space:]' ',' | head -c 200)
.claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "$KW"
```

## Guard Rails

- `docs/` 配下のみが探索対象。`src/` のコード探索は本スキルの対象外
- マッチしないキーワードがあっても fail にせず `partial` で返す（SSOT 未整備のヒントを潰さない）
- 出力は YAML 構造のまま渡す（散文サマリで上書きしない）

## Registry 更新

`docs/` に新規 SSOT を追加・削除したら、以下を同 PR で更新する。これを忘れると本スキルが古い世界観で動き続ける。

1. `docs/dev/ssot-registry.md` のみを手編集してエントリを追加・削除する
2. `match-ssot.sh` は `docs/dev/ssot-registry.md` を動的に読むため、エントリ追加で自動反映される

registry が読めない場合は `match-ssot.sh` が `status: failed` を返す。手動キャッシュへのフォールバックは行わない。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
