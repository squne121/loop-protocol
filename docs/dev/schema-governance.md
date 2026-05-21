---
title: Schema Governance
status: active
related_issue: "#135"
---

# Schema Governance

このドキュメントは LOOP_PROTOCOL における schema 変更の governance ルール・初期 schema リスト・consumer inventory 義務を定義する SSOT である。

## Schema Definition（schema の定義）

本プロジェクトでいう **schema** は、producer と consumer の境界を越えて parse / validate / serialize される machine-readable contract を指す。

以下のいずれかに該当するものを schema として扱う:

- Markdown 内 YAML フロントマター（契約スキーマとして参照されるもの）
- JSON / YAML / NDJSON ファイルで複数ファイル間のインターフェース境界となるもの
- log artifact / PR comment YAML（例: `LOOP_VERDICT` YAML、`TEST_VERDICT_MACHINE` YAML）
- Markdown table contract（例: SKILL.md 内の入力・出力仕様テーブル）
- シェルスクリプト間の YAML 契約（例: `verify_acp_roundtrip.sh` が読む YAML 構造）

**非 schema（スコープ外）**:

- 内部変数名の変更（単一ファイル内のみ影響）
- コメント・説明文のみの変更

## Initial Known Schemas（初期 schema リスト）

| Schema ID | 定義場所 | Producer | Consumer |
|---|---|---|---|
| `issue_contract/v1` | GitHub Issue 本文（`## Machine-Readable Contract` YAML ブロック） | issue-author skill | issue-contract-review, implement-issue, pr-review-judge |
| `delegation_request_v1` | `.claude/skills/gemini-cli-headless-delegation/` | implement-issue, codebase-investigator | gemini-cli 実行 wrapper |
| `delegation_result/v1` | `.claude/skills/gemini-cli-headless-delegation/` | gemini-cli 実行 wrapper | web-researcher, codebase-investigator, impl-review-loop |
| `LOOP_VERDICT` | `.claude/skills/pr-review-judge/SKILL.md` Verdict コメントテンプレート | pr-review-judge | impl-review-loop |
| `TEST_VERDICT_MACHINE v1` | `.claude/skills/test-runner/`（または test-runner SubAgent） | test-runner SubAgent | pr-review-judge, impl-review-loop |
| `IMPLEMENT_RESULT_V1` | `.claude/skills/implement-issue/SKILL.md` | implement-issue | impl-review-loop |
| `contract_schema_version: v1` | GitHub Issue 本文（`## Machine-Readable Contract`） | issue-author skill | issue-contract-review |

## schema_change_applicability 判定基準

PR が schema を変更するか否かを判定する基準:

| 値 | 判定条件 |
|---|---|
| `schema_change` | 上記 Initial Known Schemas の before/after が PR diff に含まれる、または新規 schema が追加される |
| `not_schema_change` | Allowed Paths 内の変更がすべて内部ロジック・コメント・説明文のみで、consumer 境界をまたぐ contract に変更がない |
| `uncertain` | PR diff を見ただけでは consumer 境界への影響が判断できない場合。fail-closed として schema_change 相当の検査を適用する |

## Schema Consumer Inventory 義務

schema を変更する PR（`schema_change` または `uncertain`）では、以下の **Schema Consumer Inventory** を PR 本文に必ず記載しなければならない。

### 必須記載項目

1. **変更対象 schema の ID**（例: `delegation_result/v1`）
2. **before/after 差分**（key 名変更・フィールド追加削除・型変更 等）
3. **consumer 一覧**（`rg` コマンドで列挙した全 consumer ファイルのリスト）
4. **各 consumer の更新有無**（更新済み / 不要（理由）/ 未対応（blocker））

### consumer 列挙コマンド例

```bash
# schema ID またはキー名を rg で検索して consumer ファイルを列挙
rg -l "delegation_result" .
rg -l "LOOP_VERDICT" .
rg -l "issue_contract" .
```

### Consumer Inventory が欠落している場合の扱い

- `schema_change` または `uncertain` の PR で Schema Consumer Inventory が PR 本文に存在しない場合: **APPROVE 禁止（blocker）**
- consumer が更新されていない場合（「未対応」と記載されている場合）: **APPROVE 禁止（blocker）**
- consumer 列挙コマンドの出力結果が PR 本文に含まれていない場合: **APPROVE 禁止（blocker）**

## 参照

- `.claude/skills/pr-review-judge/SKILL.md` — schema_change_applicability 判定と Consumer Inventory 検査ルール
- `.claude/skills/open-pr/SKILL.md` — PR 本文への Schema Consumer Inventory セクション追加手順
- `.github/pull_request_template.md` — PR テンプレート（Schema Change Applicability / Schema Consumer Inventory セクション）
- `docs/dev/workflow.md` — Issue contract を作業計画の正本として扱う条件
