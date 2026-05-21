---
name: open-pr
description: 承認済みの implementation issue の PR を起票するときに使う。publish ゲート（人間承認）/ Closes/Refs 自動判定 / idempotency チェック（同一ブランチの既存 PR detect）/ `gh pr create` 実行を担当する独立スキル。implement-issue / impl-review-loop から委譲され、PR 起票責務を一箇所に集約する。
---

# Open PR

承認済み issue の PR を起票する専用スキル。`implement-issue` / `impl-review-loop` から委譲して PR 作成ロジックを一箇所に集約する。

## Input

呼び出し元（`implement-issue` 等）から以下を受け取る。

**必須:**
- `pr_title`: 例 `feat(systems): MovementSystem に境界クランプを追加`
- `linked_issue`: linked issue 番号（PR 本文の `Closes` / `Refs` に使う）
- `publish`: `yes` が明示されていない場合は PR 作成を中断する（人間承認ゲート）
- `pr_body`: PR 本文の Markdown

**任意:**
- `dry_run`: `true` で PR 作成プレビューのみ実行（gh pr create はしない）
- `draft`: `true` で Draft PR として作成（デフォルト: true）
- `branch`: ブランチ名（省略時は現在の HEAD ブランチを使う）

## Procedure

### 1. Publish ゲート

`publish: yes` が明示されていない場合、`E_APPROVAL_MISSING` を返して停止:

```
[ERROR] E_APPROVAL_MISSING: publish: yes が指定されていません。
人間承認後、publish: yes を明示して再度呼び出してください。
```

### 2. PR 本文の Template Guard

`pr_body` に最小必須セクションが含まれていることを確認:
- `## Summary`
- `## 受け入れ条件の達成状況`
- `## 検証コマンド結果`
- `## Allowed Paths 遵守`

欠落があれば `E_PR_TEMPLATE_GUARD` を返して停止し、欠落セクション一覧を出力。

`.github/PULL_REQUEST_TEMPLATE.md` が存在する場合は、そのファイルの `## 見出し` をテンプレ正本として参照する（追加チェック）。

### 2.3. Schema Consumer Inventory Guard（手続きレベル観点）

> **注意:** 本 guard は手続きレベルの観点追加であり、`open_pr.py` への deterministic enforcement は未実装（follow-up: #170）。
> 現時点では skill を呼び出す AI エージェントが本手順を遵守する責任を持つ。

PR diff に `docs/dev/schema-governance.md` の Initial Known Schemas が含まれる、または新規 schema が追加される場合（`schema_change_applicability: schema_change` または `uncertain`）、`pr_body` に以下の両セクションが存在することを必須とする。

必須セクション:
- `## Schema Change Applicability`（decision フィールド: `schema_change` | `not_schema_change` | `uncertain`）
- `## Schema Consumer Inventory`（before/after + rg 列挙結果 + consumer 更新状況テーブル）

consumer 列挙に使う `rg` コマンド例:
```bash
rg -l "<schema-id-or-key>" .
```

欠落を検出した場合は呼び出し元に `E_SCHEMA_CONSUMER_INVENTORY_MISSING`（prose-level guard、open_pr.py 未実装）として報告し、欠落セクション一覧を出力する。

`schema_change_applicability: not_schema_change` を `## Schema Change Applicability` セクションで明示し、その根拠が diff と一致している場合は本 guard をスキップする。

### 2.5. Safety-sensitive PR の Safety Claim Matrix Guard

changed paths が以下のいずれかに部分一致する PR は **safety-sensitive** と判定し、`pr_body` に `## Safety Claim Matrix` セクションが存在することを必須とする。

Safety-sensitive 判定パターン（部分一致）:
- `*transport*`, `*permission*`, `*sandbox*`, `*auth*`, `*mcp*`
- `.claude/skills/**`
- `.github/workflows/**`

Safety Claim Matrix の必須列: `Claim` / `Implemented?` / `Not controlled` / `Evidence` / `Follow-up`

`Not controlled` 列が非空の場合、`Follow-up` 列に open Issue 番号（`#<N>` 形式）が必須。

欠落があれば `E_SAFETY_CLAIM_MATRIX_MISSING` を返して停止し、不足内容を出力。

### 3. Linked Issue 状態確認 + Closes / Refs 自動判定

```bash
ISSUE_STATE=$(gh issue view <linked_issue> --json state --jq '.state')
```

- `OPEN` → `Closes #<linked_issue>` を PR 本文に追記
- `CLOSED` → `Refs #<linked_issue>` に downgrade（自動マージで誤って再 close しないため）し、WARN を出力
- 状態取得失敗 → `E_LINKED_ISSUE_STATE_UNKNOWN` を返して停止

PR 本文に既に `Closes #N` / `Refs #N` がある場合は、上記判定と一致するかを確認し、不一致なら本文側を優先（caller の意図を尊重）。

### 4. Idempotency チェック（既存 PR 検出）

```bash
EXISTING_PR=$(gh pr list --head <branch> --state open --json number,url --jq '.[0]')
```

- 既存 PR あり → 重複作成せず、既存 PR URL を返す（必要なら本文 update を提案）
- 既存 PR なし → 次のステップへ

### 5. PR 作成

dry_run 時はここでプレビューを表示して終了（gh pr create は実行しない）。

```bash
PR_URL=$(gh pr create \
  --title "<pr_title>" \
  --body "<pr_body（Closes/Refs 追記後）>" \
  $([ "$draft" = "true" ] && echo "--draft") \
  --head "<branch>" \
  --base main)
```

### 6. Output（KEY=VALUE stdout contract）

```
PR_URL=https://github.com/<owner>/<repo>/pull/<number>
PR_NUMBER=<number>
LINKED_ISSUE=<linked_issue>
LINK_KIND=Closes | Refs
EXISTING=true | false
DRY_RUN=true | false
```

dry_run 時:
```
DRY_RUN=true
PR_URL=
PR_TITLE_PREVIEW=...
PR_BODY_PREVIEW_FIRST_LINES=...
```

エラー時:
```
ERROR=E_APPROVAL_MISSING | E_PR_TEMPLATE_GUARD | E_LINKED_ISSUE_STATE_UNKNOWN | E_GH_FAILURE
ERROR_DETAIL=<エラー詳細>
```

## Implementation: Python wrapper

本手順は `scripts/open_pr.py` に集約されている。skill 呼び出し側は以下のように起動:

```bash
uv run python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" \
  --linked-issue <N> \
  --publish yes \
  --pr-body-file /tmp/pr-body.md \
  --draft true
```

dry_run:
```bash
uv run python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" \
  --linked-issue <N> \
  --publish yes \
  --pr-body-file /tmp/pr-body.md \
  --dry-run
```

## Error Codes

| code | 意味 | 復旧手順 |
|---|---|---|
| `E_APPROVAL_MISSING` | `publish: yes` 未指定 | 人間承認を得て `publish: yes` で再実行 |
| `E_PR_TEMPLATE_GUARD` | 必須セクション欠落 | 不足セクションを `pr_body` に追記して再実行 |
| `E_SCHEMA_CONSUMER_INVENTORY_MISSING` | schema 変更 PR の `## Schema Change Applicability` または `## Schema Consumer Inventory` セクション欠落、または consumer 更新状況の記載欠落（prose-level guard、open_pr.py 未実装 → #170） | 両セクションを追加し、rg コマンドで consumer を列挙して更新状況を記載して再実行 |
| `E_SAFETY_CLAIM_MATRIX_MISSING` | safety-sensitive PR の `## Safety Claim Matrix` セクション欠落、または必須列・Follow-up 記載欠落 | Safety Claim Matrix セクションを追加して再実行。`Not controlled` 非空の場合は `Follow-up` に open Issue 番号を記載する |
| `E_LINKED_ISSUE_STATE_UNKNOWN` | linked issue の state 取得失敗 | gh 認証 / linked_issue 番号を確認 |
| `E_GH_FAILURE` | `gh pr create` 失敗 | stderr の詳細を確認、リポジトリ権限 / ブランチ存在 / リモート push 済みを確認 |

## Guardrails

- `publish: yes` 未指定で PR を作成しない（人間承認 fail-closed）
- Template Guard 失敗時は PR を作成せず欠落を caller に返す
- schema 変更 PR で Schema Consumer Inventory Guard（手続きレベル）失敗を検出した場合は欠落を caller に報告する（open_pr.py への deterministic 実装は #170 で追跡）
- safety-sensitive PR で Safety Claim Matrix Guard 失敗時は PR を作成せず欠落を caller に返す（fail-closed）
- linked issue が CLOSED の場合は `Closes` を `Refs` に必ず downgrade（リンク済み close 連鎖防止）
- 同一ブランチに OPEN PR がある場合は重複作成せず既存 URL を返す
- `dry_run: true` でも publish ゲートと Template Guard と Safety Claim Matrix Guard は実行する
- 既存 PR が見つかった場合、本文 update は本 skill では行わない（呼び出し元が `gh pr edit` で対応）

## Related

- `.claude/skills/implement-issue/SKILL.md` — 本 skill の主な呼び出し元
- `.claude/skills/impl-review-loop/SKILL.md` — オーケストレーター（差し戻し時の再呼び出し含む）
- `.github/PULL_REQUEST_TEMPLATE.md` — テンプレート正本（あれば）
- `docs/dev/schema-governance.md` — schema 定義・Initial Known Schemas・Consumer Inventory 義務の SSOT
- `scripts/open_pr.py` — 本手順を実装する Python wrapper
