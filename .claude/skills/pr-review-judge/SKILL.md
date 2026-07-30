---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューし、linked issue の contract と PR diff / 証跡を照合して APPROVE / REQUEST_CHANGES を判定する。verdict の GitHub 投稿は生の `gh pr review` を呼ばず、controlled review publisher（`pr_review.publish` command id、Issue #1536 Option C）へ委譲する。self-authored PR でも常に `event: COMMENT`。
---

# PR Review Judge（PRレビュー判定）

## Input（入力）

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意）

## Procedure（最小化版）

### 0) Self-authored PR ガード

`PR author == 実行アカウント` の場合でも、投稿は常に controlled review publisher 経由・`event: COMMENT` 固定（`--approve` / `--request-changes` を意味する event は生成しない）。

### 1) Linked Issue を特定

`Closes #N` を PR 本文から抽出し、紐づく Issue の `Outcome` / `Acceptance Criteria` / `Allowed Paths` / `Verification Commands` を取得。

- `Closes #N` が無い場合は `REQUEST_CHANGES`。

### 2) Mergeability 取得

**Issue #1856（evidence authority cutover, Phase 1）**: `gh pr view --json mergeable,mergeStateStatus` を authoritative source として直接利用する（TEST_VERDICT_MACHINE の有無に依存しない）。BEHIND ルーティングは `merge_state_status == "BEHIND"` のみで判定し、`route_loop_verdict_v2()` は TEST_VERDICT の `branch_behind_main` を参照しない。

判定:

- `CONFLICTING` / `DIRTY` / `BLOCKED` / `UNKNOWN(継続)` → `REQUEST_CHANGES`
- `BEHIND` は衝突 blocker とせず、後続 `required_auto_actions.kind: update_branch` を検討
- `MERGEABLE` で `CLEAN|UNSTABLE|BEHIND` → 次ステップ

### 3) VC 証拠ポリシー（PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY）

**Issue #1856（evidence authority cutover, Phase 1）**: authoritative な証拠は以下の2系列のみ。`TEST_VERDICT_MACHINE` は advisory（non-authoritative）に降格した（詳細は `references/evidence-policy.md`）。

1. `CI_CHECK_RUN_SCOPED`（current-head, `expected_head_sha` / `check_run_id` 束縛）
2. exact head SHA + literal command SHA256 に束縛された独立実行 Issue VC
3. 補助報告は `PR_BODY_SELF_REPORT`（単独では APPROVE 不可）
4. `TEST_VERDICT_MACHINE`（advisory のみ。APPROVE の必須条件にも REQUEST_CHANGES 回避の根拠にもしない）

- APPROVE 禁止条件
  - `verification_skipped_count > 0`
  - `SKIP:` / `exit 77`
  - `_*_fallback: true` など fallback/偽装成功
  - `CI_CHECK_RUN_SCOPED` head が stale、または missing / skipped / neutral / cancelled / unknown-classification

### 4) CI 証拠

**Issue #1856（evidence authority cutover, Phase 1 / AC11・AC12）**: required
check の存在有無・pass/fail 判定の canonical evaluator は
`.claude/skills/impl-review-loop/scripts/wait_ci_checks.py`
（`gh pr checks --required --json` で live required set を取得し、CheckRun /
StatusContext 双方を評価対象に含め、no-checks/skipped を fail-closed にする）
であり、pr-review-judge・pr-reviewer-lite・impl-review-loop Step 4 の 3 経路が
これを共有する。`ci_verdict_summary.py` はこれに加えて log excerpt 抽出・
artifact 保存等の詳細 provenance 収集を行う拡張レイヤーとして使用する
（raw `gh pr checks` は原則不使用）。

```bash
HEAD_SHA=$(gh pr view <PR番号> --json headRefOid --jq .headRefOid)

# canonical required-CI evaluator（3経路共通）
uv run --locked python3 .claude/skills/impl-review-loop/scripts/wait_ci_checks.py \
  --repo <owner>/<repo> --pr <PR番号> --head-sha "$HEAD_SHA" --required \
  --interval 1 --timeout-seconds 1

# 詳細 provenance（log excerpt / artifact）
uv run --locked python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py --pr <PR番号> --repo <owner>/<repo> --expected-head-sha "$HEAD_SHA"
```

`wait_ci_checks.py` の `CI_WAIT_RESULT_V1.status` が `no_checks` / `skipped_only` /
`failed` / `cancelled` / `pending_timeout` / `head_sha_changed` のいずれかであれば
fail-closed で `REQUEST_CHANGES` blocker とする。

`ci_verdict_summary.py` の exit code:

- `exit 0`: 補助証拠可
- `exit 10`: blocker（required checks が 0 件の場合の `no_required_evidence` を含む。
  Issue #1856 AC11 により 0 件を `all_pass` として通過させない）
- `exit 20`: CI 未確定 blocker
- `exit 30`: stale_head_sha blocker
- `exit 40`: gh error blocker

いずれのスクリプトも不可用時は `gh pr checks --required` fallback を明記した上で停止判断。

### 5) PR Evidence / AC の一致

- AC coverage: linked issue の各 AC が PR本文の `## 受け入れ条件の達成状況` に `[x]/[ ] + 根拠`
- Allowed Paths 遵守: 変更ファイルが contract の Allowed Paths 内
- 検証コマンド結果: 証拠付きで結果記載
- scope 混入（無関係修正）: blocker
- immediate runtime AC: `## Runtime Verification Evidence` と artifact/log 証跡

### 4.5) `Schema Consumer Inventory` の有無と妥当性を判定

PR が schema を変更しうると判断される場合:

- `Schema Change Applicability`（`schema_change/not_schema_change`）を確認
- `Schema Consumer Inventory` の存在・consumer 差分・compatibility 決定を確認

`schema_change` / `uncertain` だが表記不足なら `REQUEST_CHANGES`。

### 4.6) Safety Claim Gate（Safety Claim の判定ゲート）

`.claude/skills/**`, 権限・サンドボックス系差分、または安全ワード/本文条件を満たすと safety-sensitive。

`Safety-sensitive` 判定になった場合は `Safety Claim Matrix` を必須とする。

`Not controlled` 列が非空の際は bounded な主張であること、証跡一致、必要 follow-up があることを確認。

### 4.7) Clean-Room Review（grounded_research / 認証 surface 変更）

grounded_research 関連（`.claude/skills/gemini-cli-headless-delegation/**` の
provider 呼び出し・fan-out・grounding evidence 検証を含む差分）または AGY / OAuth
等の認証 surface に関する変更の PR は、**security-boundary reviewer** と
**experimental-validity reviewer** の 2 名独立 parallel review を要求する
（Issue #1776; #1494 敵対的再監査 follow-up）。

- **security-boundary reviewer**: 認証境界・権限境界・secret 取り扱いの安全性を
  判定する。security-boundary reviewer には実装者（implementation-worker）の
  raw transcript を渡さない。渡してよいのは Issue contract、PR diff、
  test result manifest、experiment manifest、public-safe artifact hash の
  みに限定する（clean-room 制約）。実装者の推論過程・自己申告コメントを
  そのまま信頼材料にしない。
- **experimental-validity reviewer**: 実験手順・evidence binding・causal claim
  の妥当性を判定する。`validate_agy_grounding_evidence.py` の
  `AGY_GROUNDING_EVIDENCE_VERDICT_V1`（`unsupported_claims[]` を含む）を
  一次情報源として使う。
- 2 名の判定が食い違う場合は `REQUEST_CHANGES` を優先する（fail-closed）。
- clean-room 制約（raw transcript 非共有）の遵守は verdict コメント内に
  明記し、`producer_role` / `allowed_paths_gate` ブロックと同様に
  merge-blocking な監査証跡として扱う。

### 5) verdict 決定

- blocker あり → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

`required_auto_actions` を機械的に決定（`mechanical: true` のみ）。
意味論的不足（`mechanical: false`）は常に `blockers` 側へ残す。

- `Closes` 不足時は `ensure_closing_keyword`
- PR body hygiene 欠陥時は `update_pr_body_hygiene`
- `BEHIND` + `MERGEABLE` 時は `update_branch`
- `Safety Claim Matrix` と `Schema Consumer Inventory` の欠落は `mechanical: false` の blocker として扱う。

`merge_ready` は `verdict == APPROVE` かつ blockers なし かつ required_auto_actions 空 かつ mergeability が CLEAN/UNSTABLE であるときのみ true。

- `Draft PR` 自体は blocker ではない。
ただし `DRAFT` 状態では `merge_ready` が成立していても impl-review-loop は `LOOP_VERDICT_V2.merge_ready` を見て次工程へ進行する。
merge_ready は impl-review-loop の終端条件。

`required_auto_actions` がある場合は `merge_ready` false。

### 6) verdict 投稿

pr-reviewer（本 SubAgent）は `Edit`/`Write`/`MultiEdit` を持たず、Bash 経由のファイル書き込みも禁止されている（`disallowedTools`）。そのため `PR_REVIEW_PUBLISH_REQUEST_V1` の JSON（`body_sha256` / `idempotency_key` / `producer_role` を含む）を自ら組み立てて `--input-file` に渡すことはできない（Issue #1539 fix_delta Blocker 1）。

- pr-reviewer は verdict 本文（`LOOP_VERDICT_V2` フェンス YAML を含む Markdown）と `verdict` / `merge_ready` / `reviewed_head_sha` を構造化出力として **呼び出し元（impl-review-loop control-plane）に返すのみ**。JSON の組み立て・ハッシュ計算・`producer_role` の付与は行わない。
- 呼び出し元（Write ツールを持つ trusted orchestrator）が、pr-reviewer の返した本文テキストをそのまま `artifacts/<紐づく Issue番号>/issue-metadata/pr_review.publish/<name>.md` に書き込み（本文のみ。ハッシュや schema は含まない）、controlled review publisher を **render mode** で起動する。呼び出しコマンド例:

```bash
uv run --locked python3 scripts/agent-guards/controlled_skill_mutation_exec.py \
  --command-id pr_review.publish --issue-number <紐づく Issue 番号> \
  --pr-number <PR番号> --repo <owner>/<repo> \
  --render-body-file <本文テキストのパス> \
  --verdict <APPROVE または REQUEST_CHANGES または COMMENT のいずれか> \
  --reviewed-head-sha <SHA> --expected-head-sha <SHA> \
  [--merge-ready] --json
```

`--issue-number`（紐づく Issue）と `--pr-number`（レビュー対象の PR）は独立した識別子であり、必ずしも一致しない（例: Issue #1688 に対する PR #1818）。artifact subtree のパス（`artifacts/<Issue番号>/issue-metadata/pr_review.publish/`）は常に `--issue-number` を基準にし、GitHub への投稿先・idempotency key（`repo:pr_number:expected_head_sha:body_sha256`）は常に `--pr-number` を基準にする（Issue #1822）。

- render mode の executor（trusted bridge）は `body_sha256` / `idempotency_key` を自前で再計算し、`producer_role: pr-reviewer` と `event: COMMENT` を自ら固定する（入力からは受け取らない）。本文中の `LOOP_VERDICT_V2.verdict` / `merge_ready` が CLI で宣言した値と一致しない場合は投稿前に fail-closed で拒否する。
- publisher が `expected_head_sha` を GitHub REST API の `commit_id` へ拘束し、投稿前 stale 検出・投稿後 readback・idempotency marker 判定・投稿後の current-head 再検証（TOCTOU close-out）を行う（詳細: `scripts/agent-guards/controlled_skill_mutation_policy.py` / `_exec.py`）
- self-authored でも常に `event: COMMENT`（`gh pr review --approve` / `--request-changes` は使わない）
- 生の `gh pr review` を直接呼び出してはならない（root checkout からは `local_main_branch_guard.sh` が `gh_mutation_denied` として拒否する）
- 従来の `--input-file`（事前構築済み JSON を渡す形）は dry-run/テスト用途として引き続き存在するが、本番の pr-reviewer 経路では使わない。

## Output Contract（出力契約）

最小に必要な fields:

- verdict 値: `verdict: APPROVE | REQUEST_CHANGES`
- レビュー対象 head: `reviewed_head_sha`
- merge 可否: `merge_ready`
- merge 状態: `mergeability.mergeable / merge_state_status`
- blocker 一覧: `blockers[]`
- 自動対応一覧: `required_auto_actions[]`（object）
- 自動修正結果: `auto_fix_applied`
- follow-up 要求: `follow_up_issue_requests[]`

`LOOP_VERDICT_V2` は `snake_case` を厳守し、`recommendations`（camelCase）を出さない。

### required_auto_actions 結果 schema（要点）

- 種別 `kind`: `ensure_closing_keyword` | `update_pr_body_hygiene` | `update_branch`
- 実行者 `executor`: `implementation-worker`
- 使用 skill `skill`: `open-pr.update_pr` | `implement-issue.update_branch`
- `mechanical`: `true` 固定（false の場合は `blockers`）
- `blocking_merge_ready: true`
- `expected_head_sha`（`update_branch` の場合のみ必須）

### consumer_inventory（消費先一覧）

- `impl-review-loop` が本 `pr-review-judge` の出力を受け取り、`merge_ready` が true かつ blocker が無い場合にループを終了する。
- `pr-reviewer` は `allowed_paths` / `contract` 監査結果を `LOOP_VERDICT_V2.allowed_paths_gate` として受け渡す。
- `consumer_inventory` の完全置換は #631/#632 のランタイム挙動完了まで行わない（既存消費者の振る舞いを維持）。

### ALLOWED_PATHS_GATE_RESULT_V1（Allowed Paths 判定結果）

PR review 後に `allowed_paths_review_gate.py` を使って changed files の契約違反を再計算。
snapshot freshness 用の `contract_fingerprint.base_sha_at_snapshot` と、local fallback changed files 算出用の
`diff_base_sha` は別物として扱う。local fallback の `changed_files_source` は
`git_diff_current_merge_base_head` で、snapshot base を changed files diff には使わない。

`status` は判定状態として `ok | fail_closed | stale_snapshot | indeterminate` を取る。

`indeterminate/fail_closed` は merge-blocking 状態として扱う。

changed files source hierarchy は `github_pull_request_files_api_with_previous_filename` を preferred oracle、
`git_diff_name_status_find_renames_z` を deterministic local fallback とする。
`gh_pr_diff_name_only` / `git_diff_current_merge_base_head_name_only` は rename provenance では
insufficient_for_rename_provenance であり、`git_diff_snapshot_base_head` は禁止経路である。
local fallback は `current_base_sha` と `head_sha` から evaluator 内で `git merge-base` を検証できた場合だけ
`git_diff_name_status_find_renames_z` を名乗る。`LOOP_VERDICT_V2.allowed_paths_gate` consumer への保証は script output の
provenance に限り、verdict schema 自体が詳細 provenance を直接 carry するとまでは主張しない。

### リネーム元 provenance（`previous_filename`）監査（Issue #1300）

canonical な判定 input は `audited_paths[]`（`changed_file_records[]` から派生）であり、`changed_files[]` は
post-image filename のみの backward-compatible alias に過ぎない。`status: renamed` の record は rename 元
（`previous_filename` ロール）と rename 先（`filename` ロール）の両方を `audited_paths` に含め、双方を
Allowed Paths 判定対象とする。rename 元・先のどちらかが Allowed Paths 外なら `fail_closed`（false green 禁止）。
`status: renamed` なのに `previous_filename` を取得できない場合は `indeterminate` とし、filename-only fallback
で `ok` に倒してはならない。詳細は `references/allowed-paths-gate.md` を参照する。

## Output Constraint（OUTPUT_BUDGET_V1 出力制約）

出力上限は `docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` を遵守。

`LOOP_VERDICT_V2` の全フィールドは維持。

## Reference Loading Map（読取条件）

- `references/evidence-policy.md`: 証拠優先度、`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED`、APPROVE 禁止条件。
- `references/ci-verdict-summary.md`: `ci_verdict_summary.py` の判定規則。
- `references/ac-evidence-checks.md`: AC coverage、Allowed Paths、runtime evidence、placeholder 判定。
- `references/schema-consumer-gate.md`: schema_change_applicability と `Schema Consumer Inventory` 判定。
- `references/safety-claim-gate.md`: safety-sensitive 判定と `Safety Claim Matrix` 要件。
- `references/loop-verdict-v2-schema.md`: `LOOP_VERDICT_V2` の必須フィールド。
- `references/allowed-paths-gate.md`: `ALLOWED_PATHS_GATE_RESULT_V1` の再計算手順。
- `references/required-auto-actions.md`: required_auto_actions の object schema と merge_ready への反映。
- `references/verdict-output-template.md`: コメントテンプレート。
- `references/deterministic-gates.md`: G1–G5 の重要 gate。

## Verdict コメントテンプレート

````markdown
## LOOP_VERDICT_V2
verdict: REQUEST_CHANGES
reviewed_head_sha: "<HEAD_SHA>"
merge_ready: false
mergeability:
  mergeable: "<MERGEABLE>"
  merge_state_status: "<MERGE_STATE_STATUS>"
blockers:
  - "<issue summary>"
required_auto_actions:
  - kind: ensure_closing_keyword
    executor: implementation-worker
    skill: open-pr.update_pr
    blocking_merge_ready: true
    mechanical: true
auto_fix_applied: []
follow_up_issue_requests: []
````

## Related（関連資料）

- 実装フロー: `.claude/skills/implement-issue/SKILL.md`
- 反復フロー: `.claude/skills/impl-review-loop/SKILL.md`
- reviewer agent: `.claude/agents/pr-reviewer.md`
- test runner agent: `.claude/agents/test-runner.md`
- PR template: `.github/pull_request_template.md`
- schema governance: `docs/dev/schema-governance.md`
