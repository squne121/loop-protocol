---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューし、linked issue の contract と PR diff / 証跡を照合して APPROVE / REQUEST_CHANGES / HUMAN_REVIEW_REQUIRED を判定する。verdict は `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention で呼び出し元へ返す（Issue #1873）。verdict コメントの GitHub 投稿は生の `gh pr review` を呼ばず、control-plane が通常の `gh pr comment --body-file` で行う。self-authored PR でも常に `event: COMMENT`。
---

# PR Review Judge（PRレビュー判定）

## Input（入力）

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意）

## Procedure（最小化版）

### 0) Self-authored PR ガード

`PR author == 実行アカウント` の場合でも、投稿は常に通常の `gh pr comment --body-file`・`event: COMMENT` 相当固定（`--approve` / `--request-changes` を意味する formal review event は生成しない。専用 semantic publisher は使用しない。詳細は「6) verdict 投稿」参照）。

### 1) Linked Issue を特定

`Closes #N` を PR 本文から抽出し、紐づく Issue の `Outcome` / `Acceptance Criteria` / `Allowed Paths` / `Verification Commands` を取得。

- `Closes #N` が無い場合は `REQUEST_CHANGES`。

### 2) Mergeability 取得

**Issue #1856（evidence authority cutover, Phase 1）**: `gh pr view --json mergeable,mergeStateStatus` を authoritative source として直接利用する（TEST_VERDICT_MACHINE の有無に依存しない）。BEHIND ルーティングは `merge_state_status == "BEHIND"` のみで判定し、`route_loop_verdict_v2()` は TEST_VERDICT の `branch_behind_main` を参照しない。

判定（review criteria 自体は変更しない。Issue #1873 で変わるのは transport のみ）:

- `mergeable == CONFLICTING` または `merge_state_status == DIRTY` → actual conflict。reviewer の verdict に
  関係なく `route_loop_verdict_v2()` が conflict hard stop する（#1860 Owner Decision）。reviewer 自身は
  この状態でも通常どおり判定してよい（reviewer の verdict は routing の入力の一つに過ぎず、conflict
  hard stop は live mergeability から独立に決まる）
- `BLOCKED` / `UNSTABLE` / `DRAFT` / `UNKNOWN` は Git conflict ではなく、reviewer が単独でこれらを理由に
  `REQUEST_CHANGES` にする必要はない（#1860 Owner Decision / PR #1871 P0-3）。required checks・branch
  protection の未充足自体は current-head required-CI evaluator（4) 参照）が独立に評価する
- `BEHIND` は衝突 blocker としない。`BEHIND` の update_branch 対応は reviewer が
  自己申告せず、control-plane が live mergeability から
  直接検出して `route_loop_verdict_v2()` で `update_branch` action を合成する（`step-5-mergeability-handling.md` 参照）
- `MERGEABLE` で `CLEAN`/`HAS_HOOKS`/`UNSTABLE`/`BLOCKED`/`BEHIND` → 次ステップ（PR review 自体の
  APPROVE/REQUEST_CHANGES 判定は AC/evidence 品質で決める。mergeability だけを理由に判定を変えない）

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
  明記し、merge-blocking な監査証跡として扱う。

### 5) verdict 決定

- blocker あり → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

Issue #1873 以降、機械的に対応可能な不備（`Closes` 不足、PR body hygiene 欠陥等）を
`required_auto_actions` という専用構造化フィールドで自己申告しない。これらは具体的な内容を
`blockers[]` に記載した上で `REQUEST_CHANGES` を返す（`references/required-auto-actions.md` 参照）。
`BEHIND` の update_branch 対応も reviewer が申告せず、control-plane が live mergeability から
直接検出する。

`Safety Claim Matrix` と `Schema Consumer Inventory` の欠落は blocker として扱う。

mergeability（`mergeable` / `merge_state_status`）は verdict に含めない。control-plane が
`gh pr view` で直接取得し、`route_loop_verdict_v2()` の判定に使う。`verdict == APPROVE` は
reviewer 側の判定であり、実際にループを終了できるかどうか（旧 `merge_ready` 相当）は
`route_loop_verdict_v2()` が live mergeability から決定する終端条件である。

- `Draft PR` 自体は blocker ではない。`DRAFT` 状態は `route_loop_verdict_v2()` が
  `fail_closed`（defer to current-head CI evaluator。人間 escalation は自動で発生しない）として扱い、
  impl-review-loop はループを終了しない（#1860 Owner Decision / #1873 Delivery Rule: PR は Draft の
  まま人間の最終マージ判断へ残す設計であり、Draft 自体を human escalation の理由にしない）。

### 6) verdict 投稿

pr-reviewer（本 SubAgent）は `Edit`/`Write`/`MultiEdit` を持たず、Bash 経由のファイル書き込みも禁止されている（`disallowedTools`）。

- pr-reviewer は verdict 本文（人間可読 Markdown + 最小 YAML ブロック）と `verdict` / `reviewed_head_sha` / `blockers` / `warnings` を構造化出力として **呼び出し元（impl-review-loop control-plane）に返すのみ**。
- 呼び出し元（Write ツールを持つ trusted orchestrator）が、pr-reviewer の返した本文テキストをそのまま通常の `gh pr comment --body-file` で投稿する（専用 semantic publisher は使用しない）:

```bash
gh pr comment <PR番号> --repo <owner>/<repo> --body-file <本文テキストのパス>
```

- 投稿前後に `gh pr view --json headRefOid` で head をリードバックする。投稿後に head が変化していた場合は
  その投稿を stale note として扱い、fresh review を実行する（Safety Invariants）。
- self-authored でも常に `event: COMMENT` 相当の通常コメント投稿（`gh pr review --approve` / `--request-changes` は使わない）
- 生の `gh pr review` を直接呼び出してはならない（root checkout からは `local_main_branch_guard.sh` が `gh_mutation_denied` として拒否する）

## Output Contract（出力契約、Issue #1873 最小 convention）

最小に必要な fields:

- verdict 値: `verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED`
- レビュー対象 head: `reviewed_head_sha`
- blocker 一覧: `blockers[]`
- 任意の補足: `warnings[]`

`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` は出力に含めない
（新規の同等 schema field も追加しない。AC13）。`recommendations`（camelCase）も出さない。

### consumer_inventory（消費先一覧）

- `impl-review-loop` が本 `pr-review-judge` の出力（`reviewer_verdict`）を受け取り、
  control-plane が別途取得した `live_mergeability` と合わせて `route_loop_verdict_v2()` に渡す。
  `route: approved` の場合にループを終了する。
- Allowed Paths / contract 監査結果は `LOOP_VERDICT_V2.allowed_paths_gate` という専用フィールド
  ではなく、`status != ok` の場合に具体的な違反内容を `blockers[]` へ記載する形で受け渡す
  （`references/allowed-paths-gate.md` 参照）。
- production consumer（`route_loop_verdict_v2()`）の挙動は、待機条件（stale な issue 参照）ではなく
  次の 8 fixture で固定する（AC11。各 fixture は `impl-review-loop/tests/` の production consumer test
  が参照する現行挙動のスナップショット）:

  1. `APPROVE+CLEAN+no actions` — `verdict: APPROVE` かつ `merge_state_status: CLEAN` かつ
     action 不要 → `route: approved`
  2. `APPROVE+BEHIND+valid update_branch` — `verdict: APPROVE` かつ `BEHIND` かつ有効な
     `expected_head_sha` → `route_loop_verdict_v2()` が `update_branch` action を合成
  3. `APPROVE+BEHIND+missing action` — `verdict: APPROVE` かつ `BEHIND` だが action 合成に
     必要な情報が欠落 → fail-closed で human escalation
  4. `REQUEST_CHANGES` — blocker が非空 → 次イテレーション（`implementation-worker` へ fix_delta 委譲）
  5. `stale expected_head_sha` — `reviewed_head_sha` が live head と不一致 → fresh review 要求
  6. `multiple actions` — 複数 action 候補が合成された場合 → 単一 action への収束または human escalation
  7. `body-only action while BEHIND` — action が PR body 更新のみで `BEHIND` の update_branch を
     伴わない場合 → 両方を並行に扱う（`rerun_required` の整合を維持）
  8. `unknown action-executor-skill` — action の routing 先 skill が routing table に存在しない場合
     → `status: blocked` で人間判断へ差し戻す

### ALLOWED_PATHS_GATE_RESULT_V1（Allowed Paths 判定結果、正本移譲先: references/allowed-paths-gate.md）

PR review 後に `allowed_paths_review_gate.py` を使って changed files の契約違反を再計算する。
canonical source（live linked issue 本文の扱い）・`status`（`ok | fail_closed | indeterminate`）の
意味・changed files source hierarchy・rename/copy provenance（`previous_filename`、Issue #1300）の
判定手順は `references/allowed-paths-gate.md` を正本とし、本節では複製しない。

## Output Constraint（OUTPUT_BUDGET_V1 出力制約）

出力上限は `docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` を遵守。

最小 convention のフィールド（`verdict` / `reviewed_head_sha` / `blockers` / `warnings`）は全て維持。

## Deterministic Processing Script の禁止事項（AC6）

`.claude/skills/pr-review-judge/scripts/` 配下の deterministic processing script（request
builder／validator／classifier／executor／renderer）は以下を行わない:

- semantic findings（コード品質・設計判断等の主観的知見）の自動生成
- `gh pr review` / `gh issue edit` 等の GitHub mutation の直接実行
- publisher（`open-pr` skill）が所有する hash／identity／TOCTOU gate の再実装
- test 専用の shadow implementation（pytest 検出による分岐等）

script が計算するのは G1–G5（`references/deterministic-gates.md`）等の決定論的 gate 判定のみであり、
`verdict` 自体（APPROVE/REQUEST_CHANGES の意味判断）は reviewer_verdict（本 SKILL.md の Procedure）が
決める。

## Reference Loading Map（読取条件）

- `references/evidence-policy.md`: 証拠優先度、`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED`、APPROVE 禁止条件。
- `references/ci-verdict-summary.md`: `ci_verdict_summary.py` の判定規則。
- `references/ac-evidence-checks.md`: AC coverage、Allowed Paths、runtime evidence、placeholder 判定。
- `references/schema-consumer-gate.md`: schema_change_applicability と `Schema Consumer Inventory` 判定。
- `references/safety-claim-gate.md`: safety-sensitive 判定と `Safety Claim Matrix` 要件。
- `references/loop-verdict-v2-schema.md`: 最小 convention の必須フィールド。
- `references/allowed-paths-gate.md`: `ALLOWED_PATHS_GATE_RESULT_V1` の再計算手順。
- `references/required-auto-actions.md`: 機械的に対応可能な不備の blockers への反映方針。
- `references/verdict-output-template.md`: コメントテンプレート。
- `references/deterministic-gates.md`: G1–G5 の重要 gate。

## Verdict コメントテンプレート

````markdown
## Verdict: REQUEST_CHANGES

### Mergeability
- mergeable=<...>, merge_state_status=<...>

### Blockers
- "<issue summary>"

```yaml
verdict: REQUEST_CHANGES
reviewed_head_sha: "<HEAD_SHA>"
blockers:
  - "<issue summary>"
warnings: []
```
````

## Related（関連資料）

- 実装フロー: `.claude/skills/implement-issue/SKILL.md`
- 反復フロー: `.claude/skills/impl-review-loop/SKILL.md`
- reviewer agent: `.claude/agents/pr-reviewer.md`
- test runner agent: `.claude/agents/test-runner.md`
- PR template: `.github/pull_request_template.md`
- schema governance: `docs/dev/schema-governance.md`
