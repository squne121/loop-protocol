---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューするときに使う。linked issue の contract（AC / Allowed Paths / Verification Commands）と PR 本文 / diff / 検証証跡を照合し APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` で verdict を記録する（`--approve` / `--request-changes` は使わない）。LOOP_VERDICT YAML を verdict コメントに含めて impl-review-loop の自動判定に使えるようにする。
---

# PR Review Judge

PR の差分・evidence と linked issue contract を照合し、verdict を決定する手順。

## Input

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意。orchestrator から渡された場合は LOOP_VERDICT YAML に転記する）

## Procedure

### 0. Self-authored PR ガード

PR author と実行アカウントが同一の場合は **`gh pr review --comment` のみ** を使う（`--approve` / `--request-changes` は使わない）。GitHub の制約で自分の PR を自分で approve できないため。

```bash
PR_AUTHOR=$(gh pr view <PR番号> --json author --jq '.author.login')
ACTOR=$(gh api user --jq '.login')
SELF_AUTHORED=$([ "$PR_AUTHOR" = "$ACTOR" ] && echo true || echo false)
```

### 1. Linked Issue を特定

```bash
gh pr view <PR番号> --json body --jq '.body' | grep -E "(Closes|Fixes|Resolves) #[0-9]+"
```

`Closes #N` がない PR は判定せず「linked issue を `Closes #N` で明示してください」と返して終了。

linked issue の以下を取得:
```bash
gh issue view <linked_issue> --json title,body,labels
```
- `## Outcome`
- `## Acceptance Criteria`
- `## Allowed Paths`
- `## Verification Commands`

### 2. Mergeable 状態を確認

test-runner SubAgent が投稿する `<!-- TEST_VERDICT_MACHINE v1 -->` マーカー付きコメントから取得するのを優先する:

```bash
TEST_VERDICT_BODY=$(gh pr view <PR番号> --json comments --jq '
  [.comments[] | select(.body | contains("<!-- TEST_VERDICT_MACHINE v1 -->"))] | last | .body
')
MERGEABLE=$(echo "$TEST_VERDICT_BODY" | grep "mergeable:" | head -n1 | sed -E 's/.*mergeable:[[:space:]]*//; s/[[:space:]]*$//')
MERGE_STATE_STATUS=$(echo "$TEST_VERDICT_BODY" | grep "merge_state_status:" | head -n1 | sed -E 's/.*merge_state_status:[[:space:]]*//; s/[[:space:]]*$//')
```

TEST_VERDICT_MACHINE コメントが見つからない場合のみ、フォールバックで:
```bash
gh pr view <PR番号> --json mergeable,mergeStateStatus
```

判定:
- `mergeable=CONFLICTING` または `mergeStateStatus=DIRTY` → **Conflict blocker**（REQUEST_CHANGES）
- `mergeStateStatus=BLOCKED` → **Merge blocker**（review/protection 待ち等、REQUEST_CHANGES）
- `mergeable=UNKNOWN`（retry 後も） → **Unknown blocker**（REQUEST_CHANGES）
- `mergeStateStatus=BEHIND` → head ref が base branch より古いだけであり、Conflict blocker / Merge blocker に該当しない（REQUEST_CHANGES しない）。update-branch / rebase 自動化は Step 5 / #67 の責務
- `mergeable=MERGEABLE` かつ `mergeStateStatus=CLEAN|UNSTABLE|BEHIND` → 次へ進む

### 3. CI 証跡を確認

```bash
gh pr checks <PR番号>
```

判定:
- 全チェックが `pass` / `success` → CI pass
- `fail` / `failure` が存在 → CI fail（blocker として記録、Step 4 へ進む）
- CI チェックが紐づいていない、または `pending` のみ → **CI 証跡なし blocker**（REQUEST_CHANGES、CI 完了後に再レビュー）

GitHub Actions が動いていない場合のフォールバック（test-runner 出力の `verification_commands_pass/fail` 数値で代替）:
```bash
echo "$TEST_VERDICT_BODY" | grep -E "verification_commands_(pass|fail):"
```
test-runner が `verification_commands_fail: 0` を返していれば CI 証跡相当として扱う。

### 4. PR Evidence をレビュー

PR 本文の `## 受け入れ条件の達成状況` / `## 検証コマンド結果` / `## Allowed Paths 遵守` を確認:

```bash
gh pr view <PR番号> --json body --jq '.body'
gh pr diff <PR番号> --name-only
```

判定項目:

| 項目 | 確認内容 | fail 時 |
|---|---|---|
| **AC coverage** | linked issue の各 AC が PR 本文の `## 受け入れ条件の達成状況` で `[x]` / `[ ]` + 根拠記載されている | blocker |
| **Allowed Paths 遵守** | `gh pr diff --name-only` の出力がすべて linked issue の `## Allowed Paths` に含まれる | blocker |
| **検証コマンド結果** | linked issue の `## Verification Commands` 各コマンドが PR 本文で結果記録されている（`✅ 通過` 等の具体記述） | blocker（雰囲気で通さない） |
| **scope 混入** | PR diff にスコープ外の修正・refactoring が混入していない | blocker |
| **Runtime Verification Evidence（immediate のみ）** | linked issue の `decision: immediate` で、PR 本文に `## Runtime Verification Evidence` セクションが存在し、SKIP のみ・fallback PASS の証跡を含まない | blocker（APPROVE 禁止）: 証跡なし / SKIP のみ / fallback PASS は APPROVE しない |
| **動作検証証跡の添付確認（immediate のみ）** | linked issue の `decision: immediate` で、PR 本文に動作検証ログ（`worktree/artifacts/runtime-verification-*.log` 等）への参照リンクまたは証跡内容が存在する | blocker（APPROVE 禁止）: 動作検証 AC に対して証跡リンクが一切ない場合は承認不可 |
| **TEST_VERDICT_MACHINE の SKIP 検出（全 PR）** | test-runner の `TEST_VERDICT_MACHINE` コメントに `verification_skipped_count: 0`、または linked issue に `decision: deferred` / waiver が明示されている | blocker: required VC の SKIP は PASS ではない |
| **Runtime VC の fallback / 証跡不足検出（immediate のみ）** | linked issue の `decision: immediate` で、`runtime_ac_results` 内に `fallback_detected: true` / `artifact_present: false` / `human_review_required: true` が存在しない | blocker（APPROVE 禁止）: exit 77 / `SKIP:` / `_*_fallback: true` の検出時は APPROVE しない |
| **deferred 検証先確認** | linked issue の `decision: deferred` で、PR 本文に後続 Issue / 統合フェーズ / 検証条件の参照が存在する | blocker（参照がない場合） |

placeholder のままの行（例: `[x] AC1: <達成（根拠）>` の `<...>` が残存）は証跡として数えず blocker。

### 4.5. Schema Consumer Inventory Gate（schema 変更 PR の追加検査）

#### schema_change_applicability の判定

PR が schema を変更するかどうかを以下の基準で判定する。判定は fail-closed とし、疑わしい場合は `uncertain` とする。

| 値 | 判定条件 |
|---|---|
| `schema_change` | PR diff に `docs/dev/schema-governance.md` の Initial Known Schemas の before/after が含まれる、または新規 schema が追加される |
| `not_schema_change` | 変更がすべて内部ロジック・コメント・説明文のみで、consumer 境界をまたぐ contract に変更がない |
| `uncertain` | PR diff を見ただけでは consumer 境界への影響が判断できない。fail-closed として `schema_change` 相当の検査を適用する |

```bash
# PR diff からスキーマ変更候補を確認
gh pr diff <PR番号> --name-only
```

> consumer 検索は `docs/dev/schema-governance.md` の `Detection patterns` 列を正本として使う。
> 各 schema の representative fields / nested paths を含む検索パターンを schema-governance.md から取得し実行すること。

#### Schema Consumer Inventory の必須確認

`schema_change_applicability: schema_change` または `uncertain` の PR は、PR 本文に **Schema Consumer Inventory** セクションが存在することを確認する。

```bash
gh pr view <PR番号> --json body --jq '.body' | grep -A 30 "Schema Consumer Inventory"
```

Schema Consumer Inventory の必須項目:
- 変更対象 schema の ID
- before/after 差分（key 名変更・フィールド追加削除・型変更 等）
- `rg` コマンドで列挙した consumer ファイルのリスト
- 各 consumer の更新有無（更新済み / 不要（理由）/ 未対応）

#### Schema Consumer Inventory の判定ルール（APPROVE 禁止条件）

以下のいずれかに該当する場合は **APPROVE 禁止（REQUEST_CHANGES）**:

| 条件 | 判定 |
|---|---|
| `schema_change` または `uncertain` の PR なのに `## Schema Consumer Inventory` セクションが存在しない | **APPROVE 禁止** |
| consumer 列挙コマンド（`rg` 等）の出力結果が PR 本文に含まれていない | **APPROVE 禁止** |
| consumer が「未対応」と記載されている（更新漏れが明示されている） | **APPROVE 禁止** |
| `## Schema Change Applicability` セクションが存在しない | **APPROVE 禁止**（`not_schema_change` の明示がない限り） |

`schema_change_applicability: not_schema_change` を明示し、その根拠が diff と一致している場合は Schema Consumer Inventory の提出を不要とする。

#### Compatibility Decision の判定ルール（APPROVE 禁止条件）

Schema Consumer Inventory 内の `### Compatibility Decision` セクションを確認する:

| 条件 | 判定 |
|---|---|
| `compatibility: breaking` または `uncertain` なのに `migration_or_followup` が `N/A` または空欄 | **APPROVE 禁止** |
| `compatibility: breaking` なのに consumer 更新状況テーブルに「未対応」が残存している | **APPROVE 禁止** |

#### Schema Consumer Inventory の確認コマンド例

```bash
PR_BODY=$(gh pr view <PR番号> --json body --jq '.body')

# Schema Change Applicability セクションの存在確認
echo "$PR_BODY" | grep -c "Schema Change Applicability"

# Schema Consumer Inventory セクションの存在確認
echo "$PR_BODY" | grep -c "Schema Consumer Inventory"

# consumer 未対応の記載がないか確認
echo "$PR_BODY" | grep -i "未対応\|not updated\|TODO"
```

### 4.6. Safety Claim Gate（安全境界 PR の追加検査）

#### Safety-sensitive PR の判定（fail-closed）

以下のいずれかに該当する PR は safety-sensitive と判定し、Safety Claim Matrix の検査を必須とする。判定は PR 本文キーワードだけでなく、changed paths / diff keywords / linked issue text に基づく fail-closed 条件で行う。

```
Safety-sensitive PR if any of:

1. changed path matches（部分一致）:
   - *transport*, *permission*, *sandbox*, *auth*, *mcp*, *tool*
   - .github/workflows/**
   - .claude/skills/**
   - docs/dev/runtime-verification-policy.md

2. diff または PR 本文に以下のキーワードが含まれる:
   safe, safety, read-only, sandbox, isolated, permission, approvalMode,
   MCP, tool registry, native tool, capability, auth, trust, execute

3. linked issue の labels または本文に以下が含まれる:
   safety boundary, permission, sandbox, transport, workflow, runtime verification
```

判定が疑わしい場合は safety-sensitive と判定する（fail-closed）。

#### Safety Claim Matrix の必須確認

safety-sensitive と判定された PR は、PR 本文に **Safety Claim Matrix** セクションが存在することを確認する。

```bash
gh pr view <PR番号> --json body --jq '.body' | grep -A 20 "Safety Claim Matrix"
```

Safety Claim Matrix の必須列: `Claim` / `Implemented?` / `Not controlled` / `Evidence` / `Follow-up`

#### Safety Claim Matrix の判定ルール（APPROVE 禁止条件）

以下のいずれかに該当する場合は **APPROVE 禁止（REQUEST_CHANGES）**:

| 条件 | 判定 |
|---|---|
| safety-sensitive PR なのに Safety Claim Matrix セクションが存在しない | **APPROVE 禁止** |
| `Not controlled` 列が非空なのに、PR title / summary / docs が無限定の `safe` / `read-only` / `sandboxed` / `isolated` / `complete` を使用している | **APPROVE 禁止** |
| `Not controlled` 列が非空なのに、`Follow-up` 列に open な follow-up Issue の参照がない | **APPROVE 禁止** |
| `Evidence` 列が、linked issue の Verification Commands または PR の Verification Results と対応していない | **APPROVE 禁止** |

以下の場合は APPROVE 禁止しない（bounded claim として許可）:

- `Not controlled` が非空でも、claim の射程が閉じた経路に限定されている（例: 「ACP client-side の fs/terminal proxy を提供しない」は許可。「read-only ACP transport」は禁止）
- `Not controlled` が空で、Evidence がすべての閉じた経路と対応している

#### Safety Claim Matrix の確認コマンド例

```bash
PR_BODY=$(gh pr view <PR番号> --json body --jq '.body')

# Safety Claim Matrix の存在確認
echo "$PR_BODY" | grep -c "Safety Claim Matrix"

# Not controlled が非空かつ無限定安全主張の確認
# （not_controlled 列に値があり、かつ無限定 safe/read-only 等が PR title や本文にないかチェック）
gh pr view <PR番号> --json title --jq '.title' | grep -iE "\bsafe\b|\bread-only\b|\bsandboxed\b|\bisolated\b|\bcomplete\b"
```

### 5. verdict 決定

- Step 2-4 のいずれかで blocker → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

self-authored PR の場合は **verdict 値に関わらず `--comment` で投稿**（GitHub 制約）。

### 6. verdict コメントを投稿

```bash
# self-authored
gh pr review <PR番号> --comment --body-file /tmp/pr-verdict-<PR番号>.md

# 他者の PR
gh pr review <PR番号> --approve --body-file /tmp/pr-verdict-<PR番号>.md
gh pr review <PR番号> --request-changes --body-file /tmp/pr-verdict-<PR番号>.md
```

## Verdict コメントテンプレート

````markdown
## Verdict: APPROVE | REQUEST_CHANGES

### Mergeability
- mergeable=<MERGEABLE|CONFLICTING|UNKNOWN>, mergeStateStatus=<CLEAN|UNSTABLE|BEHIND|DIRTY|BLOCKED|UNKNOWN>

### Evidence Check
- AC coverage: <○/△/×、根拠>
- Allowed Paths: <遵守 / 逸脱 + 該当ファイル>
- CI Verification: <gh pr checks の結果サマリ>
- 検証コマンド結果: <PR 本文の `## 検証コマンド結果` セクション要約>

### Blockers
<!-- 0 件なら「なし」と書く -->
- なし / <blocker 詳細>

### Non-blockers（任意改善）
- なし / <改善提案>

## LOOP_VERDICT
```yaml
verdict: APPROVE | REQUEST_CHANGES
blockers: []
mergeable: MERGEABLE | CONFLICTING | UNKNOWN
mergeStateStatus: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
reviewed_head_sha: <SHA>
follow_up_issue_requests:
  - title: "<follow-up タイトル>"
    issue_kind: implementation | research | parent
    severity: mandatory_follow_up | optional_follow_up | note_only
    source:
      kind: pr_body | pr_review | issue_comment | post_merge_cleanup | refinement
      url: "<PR コメント URL または PR URL>"
      note_id: "<Non-blockers セクション内の通し番号（1-indexed）>"
    dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
    desired_destination: "<この Issue を解決したあとの状態（Outcome 1文）>"
    validated_scope_delta: "<create-issue に渡す In Scope の概要>"
    origin_skill: pr-review-judge
    labels:
      - triage-required
```
````

### LOOP_VERDICT YAML の制約

1. `reviewed_head_sha` は YAML ブロック **内** に記載する（外側は禁止）
2. コメント本文全体で `reviewed_head_sha:` 行は 1 つだけ（複数だと parse が最初の行のみ採用）
3. コードフェンス（` ``` `）は `\` でエスケープしない（heredoc 内でもそのまま書く）
4. `follow_up_issue_requests` は non-blocker observations を構造化したフィールド。pr-review-judge は **起票を実行しない**。起票責務は impl-review-loop Step 5 等の main thread が担う（詳細は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_ISSUE_REQUEST_V1` を参照）。

## Stop Conditions

- linked issue が `Closes #N` で特定できない → 判定せず「`Closes #N` を PR 本文に追加してください」と返す
- PR 本文の `## 受け入れ条件の達成状況` / `## 検証コマンド結果` / `## Allowed Paths 遵守` が空欄 → `REQUEST_CHANGES`（雰囲気で通さない）
- linked issue の `## Allowed Paths` が空欄 → 「linked issue 側で Allowed Paths を明示してください」と返す

## Guardrails

- linked issue が不明な PR は判定しない（Stop Conditions）
- Issue contract にない完了条件を勝手に追加しない
- Evidence 不足を「雰囲気」で通さない
- self-authored PR では `gh pr review --approve` / `--request-changes` を使わない（必ず `--comment`）
- 曖昧な場合は APPROVE せず REQUEST_CHANGES（fail-closed）

### mandatory_follow_up_gate

```yaml
mandatory_follow_up_gate:
  rule: |
    LOOP_VERDICT.follow_up_issue_requests に severity: mandatory_follow_up が含まれ、
    かつ materialization.status: missing の場合は APPROVE を出力しない。
    代わりに REQUEST_CHANGES を出力し、blocker として記録する。
  action: REQUEST_CHANGES
  blocker_message: |
    mandatory_follow_up Issue が未 materialize です。
    APPROVE 確定前に該当 Issue を create または reuse してください（impl-review-loop Step 5 が担当）。
```

pr-review-judge 自身は **Issue 起票を実行しない**。`follow_up_issue_requests` を `LOOP_VERDICT` に出力し、起票責務は impl-review-loop Step 5 等の main thread が担う（詳細は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_ISSUE_REQUEST_V1` を参照）。

## Output Contract

GitHub surface:
- self-authored: `gh pr review <番号> --comment --body-file <verdict.md>`
- 他者: `gh pr review <番号> --approve --body-file <verdict.md>` または `--request-changes`

stdout: 実行ログと verdict サマリ。verdict の正本は GitHub コメント側。

## Deterministic Gates (G1-G5)

PR review miss-type（見落とし・誤判断）を構造的に防ぐ 5 つの deterministic gate:

- **G1** ci_test_selection: CI artifact の uncovered files を検出（fail-closed）
- **G2** evidence_binding: self_report 単独 APPROVE 禁止（per-finding structure）
- **G3** implementation_oracle: Python AST call + grep fallback で oracle 検証
- **G4** head_sha_consistency: PR SHA ≠ local SHA を detect（push 漏れ防止）
- **G5** fixture_guard_path_coverage: fixture_path_coverage/v1 trace を要求

Checker: `.claude/skills/pr-review-judge/scripts/check_pr_review_gates.py` （単一実装、`--rule g1|g2|g3|g4|g5`）
Output: `PR_REVIEW_GATE_RESULT_V1` JSON/YAML（fail≥1 で verdict=REQUEST_CHANGES）
Tests: pytest unit tests `.claude/skills/pr-review-judge/scripts/tests/`

## Related

- `.claude/skills/implement-issue/SKILL.md` — PR 起票元の手順
- `.claude/skills/impl-review-loop/SKILL.md` — LOOP_VERDICT を読んで自動判定するオーケストレーター
- `.claude/agents/pr-reviewer.md` — 本 skill を使う SubAgent
- `.claude/agents/test-runner.md` — TEST_VERDICT_MACHINE を投稿する SubAgent
- `.github/pull_request_template.md` — PR 本文テンプレ
- `docs/dev/schema-governance.md` — schema 定義・Initial Known Schemas・Consumer Inventory 義務の SSOT
- [`references/best-practices.md`](references/best-practices.md) — PR レビュー全般のベストプラクティス
- [`references/review-output-contract.md`](references/review-output-contract.md) — Verdict 出力契約の詳細
- [`references/pr-review-gate-result-schema.yml`](references/pr-review-gate-result-schema.yml) — PR_REVIEW_GATE_RESULT_V1 schema 定義

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`LOOP_VERDICT` の全フィールドは必ず含める（routing 必須フィールド）。
