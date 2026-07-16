# Step 4: PR Review

Step 2 が `PASS` / `PARTIAL` で完了したら、`pr-reviewer` SubAgent に PR レビューを委譲する。Step 2 が `FAIL` の場合は本ステップをスキップして Step 5 に直行（REQUEST_CHANGES 確定）。

Codex CLI: spawn the custom agent named pr-reviewer for this step; the root thread must not edit files, run tests, commit, push, or make the review judgment directly.
（Codex CLI ではこのステップ用に pr-reviewer という名前のカスタムエージェントを spawn する。root thread はファイル編集・テスト実行・commit・push・レビュー判定のいずれも直接行ってはならない。）

## 委譲呼び出し

```
subagent_type: pr-reviewer
inputs:
  pr_number: <Step 1 で取得した PR 番号>
  reviewed_head_sha: <現在の HEAD SHA>
```

SubAgent 側は `.claude/skills/pr-review-judge/SKILL.md` の手順を実行し、verdict コメントを PR に投稿する。

## PR レビュー前の CI 待機ルート

Step 4 では verdict 判定前に `wait_ci_checks.sh` を使って required checks の head-scoped 完了を待つ。

- `--required` は必須
- expected head SHA は Step 4 入力の `reviewed_head_sha`
- helper は全終了経路で `CI_WAIT_RESULT_V1_JSON=...` を 1 行だけ出力する
- exit code は `0=passed` / `1=CI negative or incomplete` / `2=auth, gh, malformed, invalid args`

```bash
.claude/skills/impl-review-loop/scripts/wait_ci_checks.sh \
  --repo "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  --pr <pr_number> \
  --head-sha <reviewed_head_sha> \
  --required \
  --interval 15 \
  --timeout-seconds 1800
```

### CI_WAIT_RESULT_V1 status routing（ステータス別ルーティング）

| status | routing |
|---|---|
| `passed` | PR review を継続 |
| `failed` | `get_ci_failed_log.sh` を呼び出して failed log summary を取得 |
| `cancelled` | `get_ci_failed_log.sh` を呼び出して cancelled / interrupted context を取得 |
| `pending_timeout` | fail-closed。`CI_PENDING_TIMEOUT` blocker で REQUEST_CHANGES |
| `no_checks` | fail-closed。required checks 未解決として REQUEST_CHANGES |
| `skipped_only` | fail-closed。required checks が skipped のみとして REQUEST_CHANGES |
| `head_sha_changed` | stale review。最新 head に対して Step 4 を再実行 |
| `auth_error` | fail-closed。認証/権限問題として REQUEST_CHANGES |
| `gh_error` | fail-closed。CLI/runtime 問題として REQUEST_CHANGES |
| `malformed_gh_response` | fail-closed。machine-readable parse 不能として REQUEST_CHANGES |

`bucket=skipping` は成功扱いにしてはならない。required-only 集合に skipped entry が残る場合は incomplete とみなし、少なくとも `skipped_only` は fail-closed とする。

## 期待する出力

pr-reviewer は判定結果（verdict 本文 + `verdict`/`merge_ready`/`reviewed_head_sha`）を呼び出し元（control-plane）へ返す。pr-reviewer は Write/Edit を持たず JSON を自ら組み立てられないため、実際の投稿は control-plane が本文を artifact パスへ書き込み、controlled review publisher を render mode （`--render-body-file` / `--verdict` / `--reviewed-head-sha` / `--expected-head-sha`、`pr_review.publish` command id、Issue #1536 Option C / Issue #1539 fix_delta Blocker 1）で起動して委譲する。pr-reviewer 自身は生の `gh pr review` を呼ばない。publisher が投稿する verdict コメントには `LOOP_VERDICT_V2` fenced YAML を含める。

```yaml
LOOP_VERDICT_V2:
  verdict: APPROVE | REQUEST_CHANGES
  reviewed_head_sha: <SHA>
  merge_ready: true | false
  mergeability:
    mergeable: MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  blockers: []
  required_auto_actions: []
  auto_fix_applied: []
  follow_up_issue_requests: []
```

LOOP_VERDICT_V2 の解析は `step-5-mergeability-handling.md` を canonical とする。

## reviewed_head_sha 整合チェック

```bash
CURRENT_HEAD=$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)
```

`reviewed_head_sha != CURRENT_HEAD` の場合は stale review とみなし、Step 4 を現在 head で再実行する。

## CI 失敗ログの取得（get_ci_failed_log helper）

`wait_ci_checks.sh` が `failed` または `cancelled` を返した場合のみ呼び出す。pending 中は呼び出さない。

```bash
REVIEWED_HEAD_SHA=$(gh pr view <pr_number> --repo <repo> --json headRefOid --jq .headRefOid)

.claude/skills/impl-review-loop/scripts/get_ci_failed_log.sh \
  --repo <owner/repo> \
  --pr <pr_number> \
  --head-sha "$REVIEWED_HEAD_SHA" \
  --max-bytes 60000
```

`reviewed_head_sha` には branch 名ではなく現在の PR head SHA を渡す。

### CI_FAILED_LOG_RESULT_V1_JSON の解釈

helper は出力末尾に `CI_FAILED_LOG_RESULT_V1_JSON: {...}` を出す。主要フィールドは以下。

```yaml
CI_FAILED_LOG_RESULT_V1:
  status: ci_failed | ci_passed | ci_pending | no_matching_run | log_unavailable
  run_id: <int>
  attempt: <int>
  head_sha: <sha>
  workflow_name: <str>
  failed_jobs: ["job-name", ...]
  retrieval_method: gh_log_failed | rest_job_logs | none
  redaction_applied: true | false
  truncated: true | false
```

| status | routing |
|---|---|
| `ci_failed` | log summary を `LOOP_VERDICT_V2.blockers` に反映 |
| `ci_passed` | CI pass とみなしログ取得をスキップ |
| `ci_pending` | wait helper を再実行、または `CI_PENDING` blocker |
| `no_matching_run` | `CI_LOG_UNAVAILABLE` blocker |
| `log_unavailable` | `CI_LOG_UNAVAILABLE` blocker |
