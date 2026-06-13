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
- `mergeStateStatus=BEHIND` → head ref が base branch より古いだけであり、Conflict blocker / Merge blocker に該当しない（REQUEST_CHANGES しない）。update-branch / rebase 自動化は Step 5 / #67 の責務。TEST_VERDICT の `branch_behind_main: true` を確認し、`required_auto_actions` に `kind: update_branch` を追加する（Step 5 で決定）
- `mergeable=MERGEABLE` かつ `mergeStateStatus=CLEAN|UNSTABLE|BEHIND` → 次へ進む

## VC 証跡判定ポリシー（PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY）

`PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY: TEST_VERDICT_MACHINE > CI_CHECK_RUN_SCOPED > PR_BODY_SELF_REPORT`

VC 証跡の信頼階層は以下の順とする（上位が存在する場合は下位を単独の根拠として使わない）:

1. **TEST_VERDICT_MACHINE**（最優先）: test-runner SubAgent が投稿する `<!-- TEST_VERDICT_MACHINE v1 -->` マーカー付きコメント。機械的に生成された検証結果であり、最も信頼できる。

   【#88 pending 注記】#88（impl-review-loop の test-runner 実行明示）が未 merge の場合、
   TEST_VERDICT_MACHINE コメントが存在しないことがある。
   その場合は PR_BODY self-report で代替してはならない。
   CI_CHECK_RUN_SCOPED 条件を満たす外部証跡がなければ REQUEST_CHANGES とする。

2. **CI_CHECK_RUN_SCOPED**（補助証跡）: `CI_CHECK_RUN_SCOPED は head_sha・workflow・job・step・command・conclusion=success が対象 VC と対応する場合のみ補助証跡`。以下の条件をすべて満たす場合のみ有効:
   - `conclusion=success`（`skipped` / `neutral` は不可）
   - 対象 PR の `head_sha` で実行されたもの（stale な SHA は不可）
   - `workflow` / `job` / `step` / `command` が linked issue の対象 VC に対応している
   - runtime verification AC の場合は artifact/log が参照可能
   - TEST_VERDICT_MACHINE が存在しない理由が明示されている

   CI_CHECK_RUN_SCOPED を証跡として採用するには、以下を満たすこと:
   - `gh pr view <PR> --json headRefOid --jq .headRefOid` で取得した head SHA と check run の head_sha が一致する
   - `gh run view <RUN_ID> --json headSha,conclusion,workflowName,jobs` で workflow/job/step が対象 VC に対応していることを確認
   - `conclusion == success` かつ `skipped / neutral / cancelled / timed_out` ではない
   - `gh pr checks` の pass 表示だけでは CI_CHECK_RUN_SCOPED の条件を満たさない
3. **PR_BODY_SELF_REPORT**（補助情報のみ）: PR 本文の自己申告。単独では APPROVE の根拠にならない（`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED` 参照）。

### APPROVE 禁止条件（PR_REVIEW_JUDGE_APPROVE_PROHIBITION_SKIP_FALLBACK）

`PR_REVIEW_JUDGE_APPROVE_PROHIBITION_SKIP_FALLBACK`: 以下のいずれかが検出された場合は **APPROVE 禁止（REQUEST_CHANGES）**:

- `verification_skipped_count > 0`（TEST_VERDICT_MACHINE に記録されている場合）
- `SKIP:` または `exit 77` を返す VC が存在する
- `_*_fallback: true` フィールドが runtime_ac_results 内に存在する
- fallback 経由の成功を PASS として扱っている（fallback PASS は APPROVE 禁止）
- PR の `head_sha` が TEST_VERDICT_MACHINE の SHA と一致しない（stale head_sha）

required VC の SKIP は PASS ではない。SKIP guard / fallback 経由の成功は形骸化した検証であり APPROVE 不可。

### PR 自己申告単独禁止（PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED）

`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED`: PR 本文（`## 検証コマンド結果` 等）の自己申告のみを根拠として APPROVE してはならない。TEST_VERDICT_MACHINE または CI_CHECK_RUN_SCOPED（限定条件を満たすもの）の証跡が存在しない場合、PR body self-report のみでは APPROVE 不可。

### 複数 linked issue AC coverage 義務（MULTI_LINKED_ISSUE_AC_COVERAGE_REQUIRED）

`MULTI_LINKED_ISSUE_AC_COVERAGE_REQUIRED`: 1 つの PR が複数の Issue を close する場合（`Closes #N, Closes #M` 等）、すべての linked issue それぞれについて AC coverage が確認できない限り APPROVE 禁止。Issue ごとの AC coverage matrix（どの Issue のどの AC が満たされているか）を PR 本文で確認し、いずれか 1 つでも AC coverage が未確認の Issue がある場合は blocker とする。

### レビュー優先順位（PR_REVIEW_PRIORITY_ORDER）

`PR_REVIEW_PRIORITY_ORDER: Outcome/AC達成 -> VC妥当性 -> SKIP/fallback検出 -> CI scope/head_sha -> runtime evidence -> PR本文形式`

レビュー時はこの順序で判定を行い、上位の問題が存在する場合は下位の確認より優先して blocker として報告する。

### 3. CI 証跡を確認

CI 証跡の取得には `ci_verdict_summary.py` を使用する（`gh pr checks` の raw 出力を main context に流し込まない）。

```bash
# ci_verdict_summary.py で CI 状態を compact JSON に変換して確認
# exit 0=all_pass / 10=failed / 20=pending_or_queued / 30=stale_head_sha / 40=gh_error
HEAD_SHA=$(gh pr view <PR番号> --json headRefOid --jq .headRefOid)
uv run python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py \
  --pr <PR番号> \
  --repo <owner/repo> \
  --expected-head-sha "$HEAD_SHA"
CI_EXIT=$?
```

exit code に応じた判定:
- `exit 0`（all_pass）→ CI pass（ただし CI_CHECK_RUN_SCOPED の限定条件を満たす場合のみ補助証跡として有効）
- `exit 10`（failed）→ CI fail（blocker として記録、Step 4 へ進む）。`--include-log-excerpt` を追加すると失敗ジョブのログを `artifacts/ci-verdict/` に保存できる
- `exit 20`（pending_or_queued）→ **CI 証跡なし blocker**（REQUEST_CHANGES、CI 完了後に再レビュー）
- `exit 30`（stale_head_sha）→ **stale head SHA blocker**（PR head が変化している。`refresh_head_sha` して再レビュー）
- `exit 40`（gh_error）→ **gh API error**（認証・rate-limit・JSON parse エラー。`errors[]` を確認して人間判断）

CI_VERDICT_SUMMARY_V1 の `status` フィールドで判定し、`gh pr checks` の raw 出力は直接 main context に流さないこと。

ci_verdict_summary が使用できない環境では従来の raw `gh pr checks` フォールバックを許容するが、その旨を verdict コメントに明記する:

```bash
# フォールバック（ci_verdict_summary.py が使用できない場合のみ）
gh pr checks <PR番号>
```

判定:
- 全チェックが `pass` / `success` → CI pass（ただし CI_CHECK_RUN_SCOPED の限定条件を満たす場合のみ補助証跡として有効）
- `fail` / `failure` が存在 → CI fail（blocker として記録、Step 4 へ進む）
- CI チェックが紐づいていない、または `pending` のみ → **CI 証跡なし blocker**（REQUEST_CHANGES、CI 完了後に再レビュー）
- `skipped` / `neutral` → CI 証跡として扱わない（PR_REVIEW_JUDGE_APPROVE_PROHIBITION_SKIP_FALLBACK 適用）

GitHub Actions が動いていない場合: TEST_VERDICT_MACHINE コメントの `verification_commands_pass/fail` 数値は CI 代替として扱わない。CI チェックが紐づいていない場合は **CI 証跡なし blocker** とし、fail-closed で判定する。TEST_VERDICT_MACHINE が存在し、かつ CI_CHECK_RUN_SCOPED の全条件（head_sha・workflow・job・step・command・conclusion=success）を満たす場合のみ補助証跡として使用可能。

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
| **複数 linked issue coverage** | PR が複数 Issue を close する場合、各 Issue の AC coverage が PR 本文に issue ごとの matrix として記載されている（MULTI_LINKED_ISSUE_AC_COVERAGE_REQUIRED） | blocker |

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
| `Not controlled` 列が非空なのに、PR title / summary / docs が無限定の `safe` / `read-only` / `sandboxed` / `isolated` / `complete` を使用している | **APPROVE 禁止** (`SAFETY_CLAIM_OVERCLAIM_REQUEST_CHANGES`) |
| `Not controlled` 列が非空なのに、`Follow-up` 列に open な follow-up Issue の参照がない | **APPROVE 禁止** |
| `Evidence` 列が、linked issue の Verification Commands または PR の Verification Results と対応していない | **APPROVE 禁止** |

`SAFETY_CLAIM_OVERCLAIM_REQUEST_CHANGES`: 安全主張・read-only 主張・sandbox 主張・isolated 主張が実装の制御範囲を超える場合（`Not controlled` 列が非空なのに無限定の安全主張をしている場合）は REQUEST_CHANGES とする。bounded claim（射程が閉じた経路に限定された主張）のみ許可する。

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

#### required_auto_actions の決定

`required_auto_actions` は `mechanical: true`（人間判断不要・副作用が冪等・失敗が 422 等の検証可能なエラーで復旧可能）なアクションのみを分類する。

以下のロジックで `required_auto_actions` を決定する:

```
REQUIRED_AUTO_ACTIONS=[]

# 1. Closes #N 不足の検出
PR_BODY=$(gh pr view <PR番号> --json body --jq '.body')
if ! echo "$PR_BODY" | grep -iE "(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved) #[0-9]+" > /dev/null; then
  # ensure_closing_keyword: PR body に GitHub 公式 closing keyword が存在しない
  REQUIRED_AUTO_ACTIONS に追加: {kind: ensure_closing_keyword, executor: implementation-worker, skill: open-pr.update_pr, blocking_merge_ready: true, mechanical: true}
fi

# 2. PR body validator failure（mechanical: true のもののみ）
# mechanical: false（Safety Claim Matrix 不足・Consumer Inventory 不足・Evidence 不足等）は blockers に残す
if <PR body の機械的フォーマット不備（空セクション・placeholder 残存等）を検出>; then
  REQUIRED_AUTO_ACTIONS に追加: {kind: update_pr_body_hygiene, executor: implementation-worker, skill: open-pr.update_pr, blocking_merge_ready: true, mechanical: true}
fi

# 3. BEHIND branch の検出
BRANCH_BEHIND_MAIN=$(echo "$TEST_VERDICT_BODY" | grep "branch_behind_main:" | head -n1 | sed -E 's/.*branch_behind_main:[[:space:]]*//; s/[[:space:]]*//')
if [ "$MERGEABLE" = "MERGEABLE" ] && [ "$BRANCH_BEHIND_MAIN" = "true" ]; then
  # update_branch: head ref が base branch より古い（merge update で解消可能）
  REQUIRED_AUTO_ACTIONS に追加: {kind: update_branch, executor: implementation-worker, skill: implement-issue.update_branch, blocking_merge_ready: true, mechanical: true, expected_head_sha: <reviewed_head_sha>}
fi
```

**分類ルール（`required_auto_actions` vs `blockers` vs `follow_up_issue_requests`）:**

| 検出事象 | 分類先 | 理由 |
|---|---|---|
| `Closes #N` 不足（GitHub 公式 keyword なし） | `required_auto_actions` (kind: `ensure_closing_keyword`) | mechanical: true（字句解析で判定可能・冪等） |
| PR body の機械的フォーマット不備 | `required_auto_actions` (kind: `update_pr_body_hygiene`) | mechanical: true（テンプレート照合で判定可能） |
| `mergeStateStatus=BEHIND && mergeable=MERGEABLE` | `required_auto_actions` (kind: `update_branch`) | mechanical: true（REST API merge update で解消可能） |
| Safety Claim Matrix 不足 | `blockers` | mechanical: false（人間判断が必要） |
| Schema Consumer Inventory 不足 | `blockers` | mechanical: false（人間判断が必要） |
| Evidence 不足 | `blockers` | mechanical: false（人間判断が必要） |
| 現在 PR の merge readiness に不要な恒久的改善 | `follow_up_issue_requests` (blocking_merge_ready: false) | merge を blocking しない任意改善 |

**`merge_ready` 決定ルール:**

```
merge_ready = (
  verdict == APPROVE
  AND blockers == []
  AND required_auto_actions == []
  AND mergeability.mergeable == MERGEABLE
  AND mergeability.merge_state_status in [CLEAN, UNSTABLE]
)
```

- `required_auto_actions` が 1 件以上存在する場合は `merge_ready: false` を強制する（verdict が APPROVE でも同様）
- `follow_up_issue_requests` の存在は `merge_ready` に影響しない
- `follow_up_issue_requests` 内のエントリはすべて `blocking_merge_ready: false` でなければならない

**`merge_ready` と Draft PR について:**

`merge_ready: true` は `impl-review-loop` の終端条件（レビュー・修正ループが完了した状態）であり、GitHub UI 上の即時マージ可能性とは独立する。Draft PR であっても `impl-review-loop` の終端条件（verdict==APPROVE かつ blockers==[] かつ required_auto_actions==[] かつ mergeability 条件を満たす）を満たせば `merge_ready: true` を出力できる。実際のマージは人間が Draft を外して実行する。`merge_ready: true` はマージを命令するものではなく、ループの終端を示すシグナルである。

**`ensure_closing_keyword` の判定について:**
- GitHub 公式 closing keyword（`close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved`）の字句解析で判定する
- `closingIssuesReferences` API への依存は Out of Scope（`gh pr view --json closingIssuesReferences` は補助確認としてのみ使用可）

**`auto_fix_applied` フィールドについて:**
- `pr-review-judge` の初回出力では `auto_fix_applied` は常に `[]` とする
- `implementation-worker` が `required_auto_actions` を実行した後に verdict comment を mutate して埋めるフィールドであり、`pr-review-judge` 自身は空配列で出力する（verdict comment の mutate は実行しない）

### 6. verdict コメントを投稿

```bash
# self-authored
gh pr review <PR番号> --comment --body-file /tmp/pr-verdict-<PR番号>.md

# 他者の PR
gh pr review <PR番号> --approve --body-file /tmp/pr-verdict-<PR番号>.md
gh pr review <PR番号> --request-changes --body-file /tmp/pr-verdict-<PR番号>.md
```

## LOOP_VERDICT_V2 スキーマ定義

```yaml
# LOOP_VERDICT_V2 スキーマ（snake_case のみ使用。camelCase は V2 では禁止）
# V2 禁止フィールド: mergeStateStatus（camelCase）、recommendations（V2 では required_auto_actions に昇格）
LOOP_VERDICT_V2:
  verdict: APPROVE | REQUEST_CHANGES
  reviewed_head_sha: <SHA>
  merge_ready: true | false  # required_auto_actions==[] && blockers==[] && verdict==APPROVE && mergeability.mergeable==MERGEABLE && mergeability.merge_state_status in [CLEAN, UNSTABLE] の場合のみ true
  mergeability:
    mergeable: MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  blockers: []  # mechanical: false な問題（人間判断が必要なブロッカー）
  required_auto_actions:
    - kind: ensure_closing_keyword | update_branch | update_pr_body_hygiene
      executor: implementation-worker
      skill: open-pr.update_pr | implement-issue.update_branch  # 実行 skill を明示（executor が委譲する skill）
      blocking_merge_ready: true  # required_auto_actions のエントリは常に true
      mechanical: true  # required_auto_actions に分類できるのは mechanical: true のみ
      # update_branch の場合のみ追加フィールド:
      expected_head_sha: <reviewed_head_sha>  # update_branch 時のみ（race guard 用）
  auto_fix_applied: []  # pr-review-judge 初回出力では常に []。implementation-worker が実行後に mutate して埋める
  follow_up_issue_requests:
    - title: "<follow-up タイトル>"
      issue_kind: implementation | research | parent
      severity: mandatory_follow_up | optional_follow_up | note_only
      blocking_merge_ready: false  # follow_up_issue_requests のエントリは必ず false（merge をブロックしない）
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
      initial_label_profile: <string>
      materialization: <string>
```

### LOOP_VERDICT_V2 スキーマ制約

- **snake_case 専用**: V2 フィールドは snake_case のみ（`merge_state_status`, `required_auto_actions` 等）。`mergeStateStatus`（camelCase）は V2 では禁止。
- **recommendations 廃止**: V2 では `recommendations` フィールドを出力しない（`required_auto_actions` に昇格済み）。新規出力では emit しないこと。旧 consumer（V1 読み込みを実装しているもの）はカットオーバー（#631/#632 完了）まで旧 LOOP_VERDICT を読み続けることができるが、pr-review-judge は V2 出力では `recommendations` を含めない。
- **merge_ready 充足条件**: `verdict==APPROVE && blockers==[] && required_auto_actions==[] && mergeability.mergeable==MERGEABLE && mergeability.merge_state_status in [CLEAN, UNSTABLE]` の場合のみ `merge_ready: true`。
- **required_auto_actions 強制**: `required_auto_actions` が 1 件以上存在する場合は `merge_ready: false` を強制する。
- **follow_up_issue_requests 制約**: 全エントリに `blocking_merge_ready: false` を必須とする（merge を blocking する問題は `blockers` または `required_auto_actions` に分類）。
- **auto_fix_applied 初期値**: `pr-review-judge` の初回出力では `auto_fix_applied: []`。`implementation-worker` が mutate して埋める（`pr-review-judge` は verdict comment を mutate しない）。

### Schema Consumer Inventory

```yaml
consumer_inventory:
  schema: LOOP_VERDICT_V2
  consumers:
    - id: impl-review-loop
      path: .claude/skills/impl-review-loop/SKILL.md
      usage: verdict routing（step-5 の APPROVE/REQUEST_CHANGES 分岐・required_auto_actions dispatch）
      update_status: pending（#631/child-5 で対応）
    - id: pr-reviewer-agent
      path: .claude/agents/pr-reviewer.md
      usage: LOOP_VERDICT_V2 出力の参照先スキーマ
      update_status: updated（本 Issue #630 で対応）
    - id: schema-governance
      path: docs/dev/schema-governance.md
      usage: Initial Known Schemas への登録
      update_status: pending（#631 で対応）
    - id: test-loop-verdict-v2
      path: .claude/skills/pr-review-judge/scripts/tests/test_loop_verdict_v2.py
      usage: スキーマ検証・分類ルールのユニットテスト
      update_status: updated（本 Issue #630 で対応）
  cutover_note: |
    #630 マージ後もカットオーバー（#631/#632 完了）まで consumer は旧 LOOP_VERDICT 互換読み込みを維持する。
    #631（impl-review-loop step-5 消費ロジック更新）が完了するまで runtime behavior は変わらない。
```

## Verdict コメントテンプレート

````markdown
## Verdict: APPROVE | REQUEST_CHANGES

### Mergeability
- mergeable=<MERGEABLE|CONFLICTING|UNKNOWN>, merge_state_status=<CLEAN|UNSTABLE|BEHIND|DIRTY|BLOCKED|UNKNOWN>

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

## LOOP_VERDICT_V2
```yaml
verdict: APPROVE | REQUEST_CHANGES
reviewed_head_sha: <SHA>
merge_ready: false
mergeability:
  mergeable: MERGEABLE | CONFLICTING | UNKNOWN
  merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
blockers: []
required_auto_actions: []
auto_fix_applied: []
follow_up_issue_requests:
  - title: "<follow-up タイトル>"
    issue_kind: implementation | research | parent
    severity: mandatory_follow_up | optional_follow_up | note_only
    blocking_merge_ready: false
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
    initial_label_profile: <string>
    materialization: <string>
```
````

### LOOP_VERDICT_V2 YAML の制約

1. `reviewed_head_sha` は YAML ブロック **内** に記載する（外側は禁止）
2. コメント本文全体で `reviewed_head_sha:` 行は 1 つだけ（複数だと parse が最初の行のみ採用）
3. コードフェンス（` ``` `）は `\` でエスケープしない（heredoc 内でもそのまま書く）
4. `follow_up_issue_requests` は non-blocker observations を構造化したフィールド。pr-review-judge は **起票を実行しない**。起票責務は impl-review-loop Step 5 等の main thread が担う（詳細は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_ISSUE_REQUEST_V1` を参照）。
5. **negative rule**: pr-review-judge は `follow_up_issues`（materialize 結果フィールド）を出力してはならない。`LOOP_VERDICT_V2` に出力するのは `follow_up_issue_requests`（起票前候補）のみ。`follow_up_issues` は Issue 起票後の materialize 結果であり、起票を行わない pr-review-judge が正しく埋めることはできない。

   ```yaml
   # INVALID — LOOP_VERDICT_V2 に follow_up_issues を出してはならない
   follow_up_issues:
     - issue_number: 123

   # VALID — LOOP_VERDICT_V2 には follow_up_issue_requests のみを出す
   follow_up_issue_requests:
     - title: "..."
       severity: optional_follow_up
       blocking_merge_ready: false
   ```

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

verdict コメントには `LOOP_VERDICT_V2` ブロックを含める（`LOOP_VERDICT` (V1) は deprecated）。

## Allowed Paths Gate（ALLOWED_PATHS_GATE_RESULT_V1）

PR の実 changed files（`changed_files_source: git_diff_base_head`）と linked issue 契約スナップショットの Allowed Paths から、PR boundary audit を決定論的に再計算する gate。

### 手順

```bash
git diff --name-only <base_sha>...<head_sha>  # merge-base..head (triple-dot)
```

の実 changed files を取得し、issue contract 内の `## Allowed Paths` リストに照合する。worker の transcript / report は入力にしない（`worker_report_used_as_canonical: false`）。

### ALLOWED_PATHS_GATE_RESULT_V1 スキーマ

```yaml
ALLOWED_PATHS_GATE_RESULT_V1:
  status: ok | fail_closed | stale_snapshot | indeterminate
  produced_at: <ISO 8601>
  produced_by: allowed_paths_review_gate.py
  producer_role: review_subagent  # Gate を生成するのは review_subagent（pr-review-judge）
  worker_report_used_as_canonical: false  # Worker self-report は使用しない
  
  # Input binding
  pr_number: <int>
  base_ref: <string>
  base_sha: <string>
  head_sha: <string>
  reviewed_head_sha: <string>  # Review 時点の head SHA（race guard）
  changed_files_source: git_diff_base_head
  
  # Result details
  allowed_paths_source: linked_issue_contract_snapshot
  changed_files_count: <int>
  changed_files: [<files>]
  allowed_paths_list: [<paths>]
  violations: [{ file: <string>, reason: <string> }]
  
  # Snapshot freshness
  contract_fingerprint: { issue_number, contract_source_kind, contract_body_sha256, allowed_paths_normalized_sha256, base_ref, base_sha_at_snapshot }
  execution_context: { worktree_root, generated_at, tool_version }  # Audit log only (not used for freshness)
  
  reason: <string>
  errors: []
```

### Status 判定ルール

判定は上から順に評価し、最初に該当した status を返す（先に該当した merge-blocking 条件で short-circuit し、git diff を参照しない）:

| 順 | 条件 | status |
|---|---|---|
| 1 | `head_sha != reviewed_head_sha` | `indeterminate`（merge-blocking） |
| 2 | `Allowed Paths` snapshot が欠落 / 空 | `indeterminate`（merge-blocking） |
| 3 | contract_fingerprint 算出失敗 / git diff 失敗 | `indeterminate`（merge-blocking） |
| 4 | snapshot 時の `expected_contract_fingerprint` と現在 fingerprint が不一致（contract_body_sha256 / base_sha / allowed_paths / base_ref / issue のいずれかが変化） | `stale_snapshot`（merge-blocking） |
| 5 | changed files が `Allowed Paths` 外に存在 | `fail_closed`（merge-blocking） |
| 6 | `head_sha == reviewed_head_sha` かつ changed files すべて `Allowed Paths` 内 | `ok` |

`--expected-contract-fingerprint` / `--contract-source-kind` / `--contract-source-id` は review 実行の必須 binding とする。いずれかが欠落した場合は stale を見逃さず `indeterminate`（merge-blocking）に倒す。

### Contract Fingerprint vs Execution Context

- **contract_fingerprint** (freshness 判定に使用): issue_number / contract_source_kind / contract_source_id / contract_body_sha256 / allowed_paths_normalized_sha256 / base_ref / base_sha_at_snapshot の正規化 JSON
  - `generated_at` / `worktree_root` を含めない（これらが変化しても stale と判定しない）
  
- **execution_context** (監査ログのみ): worktree_root / generated_at / tool_version
  - Freshness 判定に使わない（記録用）

### Allowed Paths Matcher（POSIX 正規化規則）

Repo-relative パスの正規化後、以下の規則で照合:

- **exact file path**: 完全一致 （`src/main.ts` は `src/main.ts` のみ）
- **trailing `/**`**: recursive subdirectory match （`src/**` は `src/` 配下すべてを match）
- **`*` (single segment)**: 単一 path segment のみ match、`/` を跨がない （`docs/*` は `docs/README.md` は match するが `docs/guides/README.md` は match しない）
- **invalid paths**: `..` / absolute path / backslash は fail_closed 扱い（backslash は POSIX へ変換せず reject）
- **invalid allowed patterns**: unsupported `**` 位置や invalid path は `indeterminate`（merge-blocking）

### スクリプト

`.claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py`: PR number / base / head SHA / allowed paths / contract body SHA を入力とし `ALLOWED_PATHS_GATE_RESULT_V1` を出力する CLI。pure 関数として unit test 可能。

```bash
uv run python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py \
  --pr-number <PR> \
  --base-ref <branch> \
  --base-sha <sha> \
  --head-sha <sha> \
  --reviewed-head-sha <sha> \
  --allowed-paths '<json array>' \
  --contract-body-sha256 <sha256> \
  --contract-source-kind <issue_body|issue_comment> \
  --contract-source-id <comment_id|issue_body> \
  --expected-contract-fingerprint '<json object>' \
  [--issue-number <issue>] \
  [--format json|yaml]
```

### integration (LOOP_VERDICT_V2.allowed_paths_gate)

impl-review-loop の Step 4（PR Review）内で、review_subagent（pr-review-judge）は `ALLOWED_PATHS_GATE_RESULT_V1` を生成し、`LOOP_VERDICT_V2.allowed_paths_gate`（`required: true`）に埋め込む。

Status routing:
- `ok` → merge-blocking でない（continue）
- `fail_closed` / `stale_snapshot` / `indeterminate` → merge-blocking（REQUEST_CHANGES）

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
`LOOP_VERDICT_V2` の全フィールドは必ず含める（routing 必須フィールド）。
