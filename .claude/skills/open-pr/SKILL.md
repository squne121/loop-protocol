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

### 2. final PR body の validator 実行

`open_pr.py` は linked issue state を解決して `Closes` / `Refs` を final body に反映した後、`validate_pr_body.py` を実行する。

validator CLI:
```bash
uv run python3 .claude/skills/open-pr/scripts/validate_pr_body.py \
  --body-file <final-pr-body-file> \
  --changed-paths-file <changed-paths-file> \
  --linked-issue <N>
```

JSON schema は `loop_body_lint/v1` (`target: "pr"`)、exit code は pass=0 / fail=1 / internal=2。

`validate_pr_body.py` が担う rule:
- LP050: Schema Consumer Inventory 必須条件
- LP051: safety-sensitive PR に対する Safety Claim Matrix 必須条件
- LP052: 必須セクション欠落
- LP053: Schema Change Applicability decision 不正
- LP055: Safety Claim Matrix 列欠落
- LP056: `Not controlled` 非空時の Follow-up 欠落
- LP057: final PR body の related issue 欠落
- LP058: changed paths 未解決

validator が `fail` または `internal` を返した場合、`open_pr.py` は **`gh pr create` を呼ばず fail-closed** で停止する。

### 3. Linked Issue 状態確認 + Closes / Refs 自動判定

```bash
ISSUE_STATE=$(gh issue view <linked_issue> --json state --jq '.state')
```

- `OPEN` → `Closes #<linked_issue>` を PR 本文に追記
- `CLOSED` → `Refs #<linked_issue>` に downgrade（自動マージで誤って再 close しないため）し、WARN を出力
- 状態取得失敗 → `E_LINKED_ISSUE_STATE_UNKNOWN` を返して停止

PR 本文に既に `Closes #N` / `Refs #N` がある場合は、上記判定と一致するかを確認し、不一致なら本文側を優先（caller の意図を尊重）。

### 3.5. Parent Child Materialization（delivery-rollup parent の child PR の場合）

linked issue の parent が `parent_mode: delivery-rollup` の場合、PR 本文に `## Parent Child Materialization` セクションを追加する。LLM と人間レビュアーが parent の残り child 状態を PR 本文から把握できるようにする。

```bash
# parent issue 番号を linked issue から取得
PARENT_NUM=$(gh api repos/{owner}/{repo}/issues/<linked_issue>/parent --jq '.number // empty')

# parent が delivery-rollup かどうか確認
if [ -n "$PARENT_NUM" ]; then
  PARENT_MODE=$(gh issue view "$PARENT_NUM" --json body --jq '.body' \
    | grep -oP 'parent_mode:\s*\K[\w-]+' | head -1)
fi
```

`parent_mode: delivery-rollup` の場合のみ `plan_child_materialization.py` を実行して PR 本文に含める:

```bash
uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --repo <owner>/<repo> \
  --issue "$PARENT_NUM"
```

PR 本文に追加する `## Parent Child Materialization` セクションのテンプレート:

```markdown
## Parent Child Materialization

- parent_issue: #<parent_num>
- parent_mode: delivery-rollup
- child_materialization_plan: pass | pending | n/a
- unresolved_children: <C254-3, C254-4, ... | なし>

<CHILD_MATERIALIZATION_PLAN_V2 の summary（missing / stale_body_only エントリのみ列挙）>
```

- `child_materialization_plan: pass` — 全 child が `existing` または `no_op`
- `child_materialization_plan: pending` — `missing` / `stale_body_only` / `human_escalation` が残っている
- `child_materialization_plan: n/a` — linked issue が delivery-rollup parent の child でない

parent が存在しない、または `parent_mode` が `delivery-rollup` でない場合は本セクションを省略する（n/a 扱い）。

### 3.5. changed paths の決定論的解決

`--changed-paths` が未指定の場合、`open_pr.py` は `git merge-base main HEAD` と `git diff --name-only <merge-base>...HEAD` で changed paths を解決する。

- 解決成功 → validator に file 経由で渡す
- 解決失敗 → validator が `LP058` を返し、PR 作成を停止する

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
  --body-file "<validated-final-body-file>" \
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
ERROR=E_APPROVAL_MISSING | E_PR_BODY_VALIDATION_FAILED | E_LINKED_ISSUE_STATE_UNKNOWN | E_GH_FAILURE
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
| `E_PR_BODY_VALIDATION_FAILED` | `validate_pr_body.py` が fail / internal を返した（Schema Consumer Inventory 欠落以外の一般的な validation 失敗） | `VALIDATOR_RULE_IDS` と `ERROR_DETAIL` を確認し、該当 section / changed paths / validator 出力を修正して再実行 |
| `E_SCHEMA_CONSUMER_INVENTORY_MISSING` | `schema_change` / `uncertain` PR で Schema Consumer Inventory が欠落または placeholder（LP050 / LP052 による検出） | `## Schema Consumer Inventory` セクションを追加し、before/after、consumer 列挙、更新状況を記載する |
| `E_LINKED_ISSUE_STATE_UNKNOWN` | linked issue の state 取得失敗 | gh 認証 / linked_issue 番号を確認 |
| `E_GH_FAILURE` | `gh pr create` 失敗 | stderr の詳細を確認、リポジトリ権限 / ブランチ存在 / リモート push 済みを確認 |

## Guardrails

- `publish: yes` 未指定で PR を作成しない（人間承認 fail-closed）
- `validate_pr_body.py` が fail / internal を返した場合は PR を作成しない
- changed paths を解決できない場合は `LP058` により fail-closed する
- linked issue が CLOSED の場合は `Closes` を `Refs` に必ず downgrade（リンク済み close 連鎖防止）
- 同一ブランチに OPEN PR がある場合は重複作成せず既存 URL を返す
- `dry_run: true` でも publish ゲートと validator は実行する
- 既存 PR が見つかった場合、本文 update は必ず update_pr.py wrapper 経由で行う（validator bypass 防止）

## PR 作成・PR 更新前の必須ローカル preflight

`open_pr.py` および `update_pr.py` は `gh pr create` / `gh pr edit` の直前に以下の 2 段 preflight を実行する（fail-closed）。

### preflight ステップ 1: PR body 構造バリデーション

`validate_pr_body.py` で LP050〜LP058 を検査する。

```bash
uv run python3 .claude/skills/open-pr/scripts/validate_pr_body.py \
  --body-file <final-pr-body-file> \
  --changed-paths-file <changed-paths-file> \
  --linked-issue <N>
```

fail / internal の場合、mutation は実行されず `ERROR=E_PR_BODY_VALIDATION_FAILED` 等が出力される。

### preflight ステップ 2: 日本語比率チェック（`validate_japanese_content.py --threshold 0.1`）

`validate_japanese_content.py` で PR body から抽出した**各 prose block** の日本語文字比率が threshold（0.1）以上であることを検査する。`aggregate_ratio` は診断値であり、pass 条件ではない。いずれか 1 ブロックでも比率が threshold を下回ると fail となる。

```bash
uv run python3 .claude/skills/create-issue/scripts/validate_japanese_content.py \
  --file <final-pr-body-file> \
  --threshold 0.1 \
  --verbose
```

日本語チェック失敗時、`gh pr create` / `gh pr edit` は実行されず以下が出力される:

```
PR_BODY_PREFLIGHT_RESULT_V1={"schema": "PR_BODY_PREFLIGHT_RESULT_V1", "status": "fail", "body_sha256": "sha256:...", "failed_blocks": N, "aggregate_ratio": 0.0XX, "threshold": 0.1}
ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED
ERROR_DETAIL=<stderr from validate_japanese_content.py>
```

#### CI との SSOT 対応（AC7）

ローカル preflight（`validate_japanese_content.py --threshold 0.1`）と CI ジョブは同一スクリプトを参照する。

| 場所 | スクリプト | 閾値 | workflow/job |
|---|---|---|---|
| ローカル preflight（`open_pr.py` / `update_pr.py`） | `validate_japanese_content.py` | `0.1` | ローカル（`check-japanese.yml` 相当） |
| CI | `validate_japanese_content.py` | `0.1` | `check-japanese.yml` |

表記ゆれ解消: CI の workflow 名は `check-japanese.yml`（`validate-japanese.yml` ではない）。SSOT は `.github/workflows/check-japanese.yml` を参照すること。

## PR Body Japanese Check 失敗時の修復手順（CI 失敗後）

PR Body Japanese Check（`check-japanese.yml`）が失敗した場合は、`pr_body_japanese_repair_plan.py` を使って修復プランを生成し、`update_pr.py` 経由で適用する。

### ステップ 1: 修復プランの生成

```bash
# --body-file モード（PR body ファイルを直接指定）
uv run python3 .claude/skills/open-pr/scripts/pr_body_japanese_repair_plan.py \
  --body-file <path-to-pr-body.md> \
  --threshold 0.1
```

または PR 番号から直接取得:

```bash
uv run python3 .claude/skills/open-pr/scripts/pr_body_japanese_repair_plan.py \
  --pr <PR_NUMBER> \
  --repo <owner>/<repo> \
  --threshold 0.1
```

stdout は `PR_BODY_JAPANESE_REPAIR_PLAN_V1` の compact JSON:

```json
{
  "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
  "status": "pass | repairable | human_review_required | invalid_body | gh_error",
  "threshold": 0.1,
  "failed_blocks": [...],
  "safe_rewrite_plan": [...],
  "body_file_out": null,
  "preserved_tokens": ["Closes #N", "Refs #N", ...],
  "next_action": "none | apply_safe_rewrite_plan | human_review_required"
}
```

exit code: `0 pass / 10 repairable / 20 human_review_required / 30 invalid_body / 40 gh_error`

日本語判定の SSOT:
- `validate_japanese_content.py` の `validate_text()` / `split_markdown_blocks()`
- `prose_boundary_policy.py` の `iter_markdown_blocks()` / `lookup_heading_policy()`

### ステップ 2: status に応じた対処

| status | 対処 |
|---|---|
| `pass` | Japanese Check は通過済み。再チェック不要 |
| `repairable` | `safe_rewrite_plan` の `action: append_japanese_note` に従い、各ブロックに日本語注記を追記し、`update_pr.py` 経由で更新する |
| `human_review_required` | 任意英語の意味変換が必要。人間が日本語翻訳を行い `update_pr.py` 経由で更新する |
| `invalid_body` | PR body が空または読み込み不能。body を確認する |
| `gh_error` | gh CLI エラー。認証 / PR 番号 / ネットワークを確認する |

### ステップ 3: 修正後の PR body を update_pr.py 経由で適用

修正した body ファイルを `update_pr.py` 経由で更新する（AC2 準拠: validator bypass 防止）:

```bash
uv run python3 .claude/skills/open-pr/scripts/update_pr.py \
  --pr-number <N> \
  --body-file <path-to-repaired-body.md> \
  --linked-issue <linked-issue-num>
```

`update_pr.py` は内部で `validate_japanese_content.py` と `validate_pr_body.py` の両方を実行して
整合性を確認してから `gh pr edit` を呼ぶ。

### 保護トークン（preserved_tokens）

以下のトークンは修復プラン生成時に `preserved_tokens` に記録され、書き換えから保護される:
- GitHub closing keyword 全 variant: `close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved` + colon variant
- cross-repo reference: `owner/repo#N`
- 複数 issue 列挙: `Closes #1, #2, #3`
- `Refs #N` / `Refs owner/repo#N`
- HTML comment: `<!-- ... -->`

## PR Body Update（既存 PR への本文反映）

PR の本文を更新する場合（既存 PR 発見時など）は、必ず以下の wrapper 経由で行う（validator pre-write hook を強制）:

```bash
uv run python3 .claude/skills/open-pr/scripts/update_pr.py \
  --pr-number <N> \
  --body-file <path-to-new-body.md> \
  --linked-issue <linked-issue-num> \
  --changed-paths-file <changed-paths-file>
```

direct `gh pr edit --body-file` は使用禁止（validator が bypass されるため）。

update_pr.py は以下を実行:
1. 新しい body を読み込む
2. validator pre-write hook 実行（fail-closed）
3. validator pass 後、本検証済み body を temp file に書き出す
4. gh pr edit に temp file の path を渡す（TOCTOU 安全）
5. temp file を削除

KEY=VALUE stdout contract:
```
PR_NUMBER=<N>
REPO=<owner>/<repo>
UPDATED=true
```

エラー時:
```
ERROR=E_VALIDATION_FAILED | E_UPDATE_FAILURE | E_FILE_NOT_FOUND
ERROR_DETAIL=<詳細>
VALIDATOR_RULE_IDS=<rule_ids>  # validator fail 時
```

## Related

- `.claude/skills/implement-issue/SKILL.md` — 本 skill の主な呼び出し元
- `.claude/skills/impl-review-loop/SKILL.md` — オーケストレーター（差し戻し時の再呼び出し含む）
- `.github/pull_request_template.md` — テンプレート正本（あれば）
- `docs/dev/schema-governance.md` — schema 定義・Initial Known Schemas・Consumer Inventory 義務の SSOT
- `scripts/open_pr.py` — PR 작성手順を実装する Python wrapper
- `scripts/update_pr.py` — PR body 更新 wrapper with validator pre-write hook
- `docs/dev/agent-run-report.md` — PR open 後のレポート posting handoff 規約

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
