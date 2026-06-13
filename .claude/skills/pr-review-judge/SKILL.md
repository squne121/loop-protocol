---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューし、linked issue の contract と PR diff / 証跡を照合して APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` のみを使う。
---

# PR Review Judge

## Input

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意）

## Procedure（最小化版）

### 0) Self-authored PR ガード

`PR author == 実行アカウント` の場合、`gh pr review --comment` でのみ投稿。

### 1) Linked Issue を特定

`Closes #N` を PR 本文から抽出し、紐づく Issue の `Outcome` / `Acceptance Criteria` / `Allowed Paths` / `Verification Commands` を取得。

- `Closes #N` が無い場合は `REQUEST_CHANGES`。

### 2) Mergeability 取得

優先: TEST_VERDICT_MACHINE のコメント。

見つからない場合のみ `gh pr view --json mergeable,mergeStateStatus` を利用。

判定:

- `CONFLICTING` / `DIRTY` / `BLOCKED` / `UNKNOWN(継続)` → `REQUEST_CHANGES`
- `BEHIND` は衝突 blocker とせず、後続 `required_auto_actions.kind: update_branch` を検討
- `MERGEABLE` で `CLEAN|UNSTABLE|BEHIND` → 次ステップ

### 3) VC 証拠ポリシー（PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY）

優先順位:

1. `TEST_VERDICT_MACHINE`
2. `CI_CHECK_RUN_SCOPED`
3. `PR_BODY_SELF_REPORT`（単独では APPROVE 不可）

- APPROVE 禁止条件
  - `verification_skipped_count > 0`
  - `SKIP:` / `exit 77`
  - `_*_fallback: true` など fallback/偽装成功
  - TEST_VERDICT head が stale

### 4) CI 証拠

`ci_verdict_summary.py` を使用（raw `gh pr checks` は原則不使用）。

```bash
HEAD_SHA=$(gh pr view <PR番号> --json headRefOid --jq .headRefOid)
uv run python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py --pr <PR番号> --repo <owner>/<repo> --expected-head-sha "$HEAD_SHA"
```

- `exit 0`: 補助証拠可
- `exit 10`: blocker
- `exit 20`: CI 未確定 blocker
- `exit 30`: stale_head_sha blocker
- `exit 40`: gh error blocker

`ci_verdict_summary.py` 不可用時は `gh pr checks` fallback を明記した上で停止判断。

### 5) PR Evidence / AC の一致

- AC coverage: linked issue の各 AC が PR本文の `## 受け入れ条件の達成状況` に `[x]/[ ] + 根拠`
- Allowed Paths 遵守: 変更ファイルが contract の Allowed Paths 内
- 検証コマンド結果: 証跡付きで結果記載
- scope 混入（無関係修正）: blocker
- immediate runtime AC: `## Runtime Verification Evidence` と artifact/log 証跡

### 4.5) Schema Consumer Inventory Gate

PR が schema を変更しうると判断される場合:

- `Schema Change Applicability`（`schema_change/not_schema_change`）を確認
- `Schema Consumer Inventory` の存在・consumer 差分・compatibility 決定を確認

`schema_change` / `uncertain` だが表記不足なら `REQUEST_CHANGES`。

### 4.6) Safety Claim Gate

`.claude/skills/**`, 権限・サンドボックス系差分、または安全ワード/本文条件を満たすと safety-sensitive。

`Safety-sensitive` は `Safety Claim Matrix` 必須。

`Not controlled` 列が非空の際は bounded な主張であること、証跡一致、必要 follow-up があることを確認。

### 5) verdict 決定

- blocker あり → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

`required_auto_actions` を機械的に決定。

- `Closes` 不足: `ensure_closing_keyword`
- PR body hygiene 欠陥: `update_pr_body_hygiene`
- `BEHIND` + `MERGEABLE`: `update_branch`

`merge_ready` は `verdict == APPROVE` かつ blockers なし かつ required_auto_actions 空 かつ mergeability が CLEAN/UNSTABLE であるときのみ true。

`required_auto_actions` がある場合は `merge_ready` false。

### 6) verdict 投稿

- `LOOP_VERDICT_V2` を `gh pr review --comment` 等で投稿
- self-authored でも常に `--comment`

## Output Contract

最小に必要な fields:

- `verdict: APPROVE | REQUEST_CHANGES`
- `reviewed_head_sha`
- `merge_ready`
- `mergeability.mergeable / merge_state_status`
- `blockers[]`
- `required_auto_actions[]`（object）
- `auto_fix_applied`
- `follow_up_issue_requests[]`

`LOOP_VERDICT_V2` は `snake_case` を厳守し、`recommendations`（camelCase）を出さない。

### required_auto_actions 結果 schema（要点）

- `kind`: `ensure_closing_keyword` | `update_pr_body_hygiene` | `update_branch`
- `executor`: `implementation-worker`
- `skill`: `open-pr.update_pr` | `implement-issue.update_branch`
- `blocking_merge_ready: true`
- `expected_head_sha`（update_branch の場合のみ必須）

### ALLOWED_PATHS_GATE_RESULT_V1

PR review 後に `allowed_paths_review_gate.py` を使って changed files の契約違反を再計算。

`status` は `ok | fail_closed | stale_snapshot | indeterminate`。

`indeterminate/fail_closed` は merge-blocking。

## Output Constraint (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` を遵守。

`LOOP_VERDICT_V2` の全フィールドは維持。

## Reference Loading Map（読取条件）

- `references/evidence-policy.md`: 証拠優先度、`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED`、APPROVE 禁止条件。
- `references/ci-verdict-summary.md`: `ci_verdict_summary.py` の判定規則。
- `references/ac-evidence-checks.md`: AC coverage / Allowed Paths / runtime evidence / placeholder 判定。
- `references/schema-consumer-gate.md`: schema_change_applicability と `Schema Consumer Inventory` 判定。
- `references/safety-claim-gate.md`: safety-sensitive 判定と `Safety Claim Matrix` 要件。
- `references/loop-verdict-v2-schema.md`: `LOOP_VERDICT_V2` 必須フィールド。
- `references/allowed-paths-gate.md`: `ALLOWED_PATHS_GATE_RESULT_V1` 再計算手順。
- `references/required-auto-actions.md`: required_auto_actions の object schema と merge_ready への反映。
- `references/verdict-output-template.md`: コメントテンプレート。
- `references/deterministic-gates.md`: G1–G5 重要 gate。

## Related

- `.claude/skills/implement-issue/SKILL.md`
- `.claude/skills/impl-review-loop/SKILL.md`
- `.claude/agents/pr-reviewer.md`
- `.claude/agents/test-runner.md`
- `.github/pull_request_template.md`
- `docs/dev/schema-governance.md`
