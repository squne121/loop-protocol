---
name: issue-contract-review
description: 人間承認後・実装着手直前に、Issue contract（作業計画・コンテクスト）が指定通りで開発フローに沿って AI Agent が安全に着手できるかを **決定論的**に preflight する skill。VC が baseline で fail することと AC が決定論的に検証可能であることを確認する。Issue 内容・文脈レビューは review-issue / issue-refinement-loop の責務で、本 skill では扱わない。
---

# Issue Contract Review

実装開始前の **preflight skill**。Issue 本文・コメント・VC・AC を読んで `implement-issue` へ渡せるかを判定する。

## Input

- `Issue番号` または `Issue URL`（必須）

## Procedure（最小化版）

### 1) Issue contract の取得

```bash
# issue 本文・コメント・ラベルを取得
gh issue view <番号> --json title,body,labels,comments
```

### 2) 開発フロー適合性チェック

`State Label`、template 準拠、`Allowed Paths`、`Verification Commands`、Stop Conditions（implementation の場合）を確認する。

- `state/needs-human` がある場合のみ BLOCKED（他の state ラベルは判定に直接使用しない）
- Allowed Paths/VC が空のままなら BLOCKED
- Stop Conditions は implementation のみ必須

詳細は `references/contract-compliance.md`

### 3) blockers / dependency を決定論的検証

```bash
bash .claude/skills/issue-contract-review/scripts/check_blockers.sh <issue_number> <owner>/<repo>
```

- native dependency API が主情報源
- `Depends on #N` fallback は native 取得不可時のみ
- native と fallback 不一致は human escalation

### 3.5) Product Spec Preflight（必要時のみ）

トリガー条件（docs/product / tasks.md / .specify / generated task / `Product Spec Context`）で `check_product_spec_contract.py` を実行。

### 4) VC preflight

`baseline_vc_preflight.py` で AC 別 VC を AC1..N に抽出・実行し、scope/カテゴリ/判定を記録。

`pnpm build` regression gate は `shell=False` のまま runner-side fixed env delta `{CI:"true"}` で実行する。Issue body 側で `CI=true pnpm build` や `env CI=true pnpm build` のような shell/env prefix workaround を許可しない。

`## Verification Commands` では ` ```bash ... ``` ` の **fenced bash** を **canonical VC format** とし、各コマンドを
`$ <command>` + `# ACn` 形式で記載する。

VC 実行前に静的に弾くカテゴリ:

- `unsupported_shell_syntax` : `$(...)` / backtick / `${...}` を含む場合
- `unsafe_command` : `rm` / `git push` / `curl` など危険コマンド
- `command_not_allowed` : allowlist 外コマンド
- `package_manager_no_tty_prompt` : pnpm no-TTY prompt 由来の tooling/env blocker。`body_author_fixable=false` / downstream bucket `env_or_runtime`

`unsupported_shell_syntax` は `run_command()` を呼ばない前提の必須カテゴリ。

- コマンドは「`$ <command>` + `# ACn`」形式（verbatim）
- baseline fail が期待される項目は `go` 可
- `preflight-scope` marker は `baseline_fail_expected / pr_review_only / runtime_only` を制御

### 4.5) 動作検証 AC 前提チェック

`Runtime Verification Applicability` の有無と実装前提（対象 AC、環境要件、SKIP 規約、証跡要件）を確認。

### 5) AC 検証可能性チェック

- AC はチェックボックス `- [ ]`
- AC 番号と VC 番号の一致
- VC は決定論的（exit code / 数値比較）
- `AC <ACN>` と VC 内 `# ACn` 一致

### 6) Worktree / Branch preflight

`git worktree list` と branch 競合をチェック。

### 7) 実行結果の出力

`CONTRACT_REVIEW_RESULT_V1` を `go | blocked` で返却し、`go` の場合のみ `implement-issue` へ handoff。

## Output Contract（最小）

### status: `go`

- Contract Snapshot（issue body 参照情報）をコメント投稿
- `implement-issue` へ handoff 可能

### status: `blocked`

- 不足理由・不足項目を列挙してコメント
- `issue-refinement-loop` へ委譲

`contract_snapshot` には少なくとも次を含める。

- `Outcome`
- `Acceptance Criteria`
- `Verification Commands`
- `Allowed Paths`
- `Worktree / Branch`
- `VC preflight` 集約

## Programmatic Entry Points (scripts)

`run_contract_review_once.py`（#817 追加）

- `--mode static|execute`
- `CONTRACT_REVIEW_ONCE_RESULT_V1` を返す

`contract_review_result_parser.py`（#817 追加）

- `CONTRACT_REVIEW_RESULT_V1` をコメントから解析（issue-comment / issue-body）

## Output Contract 実体

`CONTRACT_REVIEW_RESULT_V1` の全フィールドは残す。

`checks` には少なくとも以下を含める。

- `template_compliance`
- `state_label`
- `allowed_paths_present`
- `vc_present`
- `stop_conditions_complete`
- `vc_preflight`
- `ac_verifiability`
- `product_spec_check`
- `worktree_branch_collision`
- `next_action`
- `blocked_reasons`

`vc_preflight` は各 classification を保持し、`scope_class`（baseline_fail_expected / regression_gate / pr_review_only / runtime_only）を含める。

## Handoff to implement-issue（go 時）

必須項目（最小）:

- Issue 番号
- `contract_snapshot_url`
- Outcome / Acceptance Criteria / Verification Commands / Allowed Paths / Required Skills
- Worktree/Branch 命名（preflight で確定）

## Guardrails

- Issue 本文の品質評価をしない（review-issue が担当）
- code edit は行わない（preflight 専用）
- branch / PR / worktree を本 skill で作らない
- `pnpm test` を含む回帰失敗は blocked
- `.claude/skills/**` や `.codex` を触る場合は Stop 条件で一度停止

## Reference Loading Map（読取条件）

- `references/contract-compliance.md`: テンプレ準拠 / state label / blocker / stop conditions の判定詳細。
- `references/product-spec-preflight.md`: PS001–PS006 テーブルと停止ルール。
- `references/vc-preflight.md`: baseline preflight スコープ、分類カテゴリ、分類ロジック。
- `references/runtime-verification.md`: immediate/deferred/not_applicable の判定と必須項目。
- `references/ac-verifiability.md`: AC/VC 検証可能性の詳細。
- `references/output-schema.md`: `CONTRACT_REVIEW_RESULT_V1` フィールド定義。

## Related

- `.claude/skills/issue-contract-review/scripts/*`
- `.claude/skills/review-issue/SKILL.md`
- `.claude/skills/implement-issue/SKILL.md`
- `.claude/skills/issue-refinement-loop/SKILL.md`
- `.github/ISSUE_TEMPLATE/implementation.yml`

## Output Constraint (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約を遵守。
