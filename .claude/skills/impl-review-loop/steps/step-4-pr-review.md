# Step 4: PR Review

Step 2 が `PASS` / `PARTIAL` で完了したら、`pr-reviewer` SubAgent に PR レビューを委譲する。Step 2 が `FAIL` の場合は本ステップをスキップして Step 5 に直行（REQUEST_CHANGES 確定）。

Codex CLI: spawn the custom agent named pr-reviewer for this step; the root thread must not edit files, run tests, commit, push, or make the review judgment directly.

## 委譲呼び出し

```
subagent_type: pr-reviewer
inputs:
  pr_number: <Step 1 で取得した PR 番号>
  reviewed_head_sha: <現在の HEAD SHA>
```

SubAgent 側は `.claude/skills/pr-review-judge/SKILL.md` の手順を実行し、verdict コメントを PR に投稿する。

## 期待する出力

pr-reviewer が `gh pr review --comment` で投稿する verdict コメントに含まれる `LOOP_VERDICT` YAML:

```yaml
LOOP_VERDICT:
  verdict: APPROVE | REQUEST_CHANGES
  blockers: []
  mergeable: MERGEABLE | CONFLICTING | UNKNOWN
  mergeStateStatus: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  reviewed_head_sha: <SHA>
```

## 判定

orchestrator は LOOP_VERDICT YAML を読み取り、次ステップを決定する:

| verdict | 次アクション |
|---|---|
| `APPROVE` | LOOP_STATE.termination_reason = "approved" を立て、Step 5 で終了処理 |
| `REQUEST_CHANGES` | blockers を LOOP_STATE.blockers_history に追加、Step 5 で iteration 判定 |

LOOP_VERDICT の YAML 解析方法は `step-5-mergeability-handling.md` を参照（最新コメントの抽出ルール含む）。

## reviewed_head_sha 整合チェック

LOOP_VERDICT に含まれる `reviewed_head_sha` が現在の PR head SHA と一致しない場合、pr-reviewer は古い head をレビューしている可能性がある:

```bash
CURRENT_HEAD=$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)
```

不一致 → orchestrator は `LOOP_STATE.blockers_history` に "stale review on $REVIEWED_SHA vs current $CURRENT_HEAD" を記録し、Step 4 を再委譲（最新 head での再レビュー）。

## 出力

LOOP_STATE.last_step = "pr_review" に更新、LOOP_STATE.last_loop_verdict に APPROVE / REQUEST_CHANGES を記録、Step 5 へ進む。

## CI 失敗ログの取得（get_ci_failed_log helper）

### 呼び出し条件

PR の CI checks が失敗（conclusion: failure / timed_out / cancelled）の場合のみ呼び出す。
pending の場合は #844 wait helper に委譲し、ログ取得を行わない。
CI が pass の場合は呼び出さない。

### reviewed_head_sha の渡し方

```bash
REVIEWED_HEAD_SHA=$(gh pr view <pr_number> --repo <repo> --json headRefOid --jq .headRefOid)

.claude/skills/impl-review-loop/scripts/get_ci_failed_log.sh \
  --repo <owner/repo> \
  --pr <pr_number> \
  --head-sha "$REVIEWED_HEAD_SHA" \
  --max-bytes 60000
```

`reviewed_head_sha` は必ず PR の現在の head SHA を使う。branch 名を渡してはならない。

### #844 wait helper との関係

- CI が `ci_pending` を返した場合は #844 の wait helper（`wait_ci_checks.sh`）に委譲し、完了後に再度 get_ci_failed_log を呼ぶ。
- `ci_pending` のまま wait helper も利用不可の場合は、`LOOP_VERDICT_V2.blockers` に `CI_PENDING` を記録して REQUEST_CHANGES を返す。

### LOOP_VERDICT_V2 への取り込み方

helper が `status=log_unavailable` を返した場合は blocker type を `CI_LOG_UNAVAILABLE` にする。
ログ全文は `blockers` に含めず、failed_jobs + log summary（先頭 / 末尾 500 行程度）のみを記録する。

### CI_FAILED_LOG_RESULT_V1 の解釈

`get_ci_failed_log` は出力末尾に以下の YAML marker を常に出力する:

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

orchestrator はこの marker を parse し、`status` に応じて routing する:

| status | routing |
|---|---|
| `ci_failed` | ログを `LOOP_VERDICT_V2.blockers` に CI_FAILED_LOG として記録 |
| `ci_passed` | CI は pass — ログ取得スキップ |
| `ci_pending` | #844 wait helper へ委譲（またはCI_PENDING blocker） |
| `no_matching_run` | CI_LOG_UNAVAILABLE blocker |
| `log_unavailable` | CI_LOG_UNAVAILABLE blocker |
