# PR Evidence Policy (PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY)

## Authority（正本の情報源を定義する。Issue #1856, Phase 1: evidence authority cutover）

通常レビュー（pr-review-judge / impl-review-loop Step 2）の APPROVE/REQUEST_CHANGES 判定は、
以下の2系列のみを authoritative（正本）として扱う。TEST_VERDICT lane の有無に依存しない。

1. **CI_CHECK_RUN_SCOPED**（`ci_verdict_summary_v2` 相当、current-head の GitHub Check Run。
   `expected_head_sha` / `check_run_id` に束縛される。missing / skipped / neutral /
   cancelled / stale-head / unknown-classification のいずれも fail-closed 扱い）
2. **独立実行 Issue VC**（exact PR head SHA + literal command SHA256 に束縛された、
   Issue Verification Commands の独立再実行結果）

`TEST_VERDICT_MACHINE`（producer/publisher/materializer/schema は現存するが）は、
通常レビュー判定においては **non-authoritative（advisory）** に降格する。
TEST_VERDICT comment/artifact の有無・内容は APPROVE/REQUEST_CHANGES の必須条件にしない。
producer/publisher/materializer/schema の物理削除は Phase 3（別 Issue、未起票）に委ねる。

**Issue #88**: `TEST_VERDICT_MACHINE` は diagnostics-only（診断専用）出力であり、通常 routing（`impl-review-loop` Step 4 の pr-reviewer 起動可否）の入力にはならない。Step 4 起動可否は Step 2（test-runner）が完了させた current-head の `VC_ADJUDICATION_RESULT_V1`（`.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py`）が `blocking: false` を返した場合にのみ許可される（`step-4-pr-review.md` 参照）。

### テスト証跡のルール

- PR本文の自己申告のみでは APPROVE 不可（変更なし）。
- `CI_CHECK_RUN_SCOPED` または「exact head SHA + literal command SHA256 に束縛された独立実行 Issue VC」のいずれもなければ `REQUEST_CHANGES`。
- `CI_CHECK_RUN_SCOPED` の missing / skipped / neutral / cancelled / stale-head / unknown-classification は、Phase 1 変更後も引き続き fail-closed（`REQUEST_CHANGES`）である（`ci_verdict_summary_v2.py` の既存実装を参照）。
- `skipped / fallback PASS / exit 77 / SKIP:` は `required pass` として扱わない（変更なし）。
- `head_sha` が PR head と不一致（stale）なら fail-closed blocker（変更なし）。
- `TEST_VERDICT_MACHINE` は advisory として参照してよいが、APPROVE の必須条件にも REQUEST_CHANGES 回避の根拠にもしない。

### APPROVE 禁止条件（要約）

- 独立実行 Issue VC（`independent_issue_vc`）の `verification_skipped_count > 0`
- `SKIP:` / `exit 77`
- `_*_fallback: true`
- fallback 成功を PASS として扱う
- `head_sha` stale
- `CI_CHECK_RUN_SCOPED` が missing / skipped / neutral / cancelled / stale-head / unknown-classification
- authoritative evidence（`CI_CHECK_RUN_SCOPED` または束縛済み独立実行 Issue VC）が一つも無い

上記の `verification_skipped_count > 0` は、独立実行 Issue VC 自体の
skipped 件数を指し、advisory な `TEST_VERDICT_MACHINE` コメントの
`verification_skipped_count` フィールドを指さない（TEST_VERDICT は
`may_block_approval: false` のため拒否根拠にしない。下記判定表を参照）。

### 判定表（EVIDENCE_AUTHORITY_TABLE_V1、Issue #1856 Round 2 で明文化）

以下は各 evidence source の authority を一意に定める判定表であり、
本ドキュメント内の他記述と矛盾する場合はこの表を優先する。

| evidence source | required | role | may_grant_approval | may_block_approval | may_change_routing |
|---|---|---|---|---|---|
| `current_head_required_ci`（`CI_CHECK_RUN_SCOPED` 相当） | always | authoritative | true | true | false |
| `independent_issue_vc`（exact head SHA + literal command SHA256 束縛） | linked Issue に対象 VC がある場合 | authoritative | true | true | false |
| `test_verdict`（`TEST_VERDICT_MACHINE`） | never | diagnostics_only | false | false | false |

- `current_head_required_ci` と `independent_issue_vc` は「CI/VC のいずれも無い場合」に
  fail-closed（`REQUEST_CHANGES`）の根拠になる。`current_head_required_ci` は常に
  required、`independent_issue_vc` は linked Issue に対象 Verification Command が
  存在する場合にのみ required（対象 VC が無い Issue では独立実行 VC 欠落を理由に
  拒否しない）。
- `test_verdict` は APPROVE の付与にも REQUEST_CHANGES の判断にも routing の変更にも
  一切使わない（diagnostics 表示専用）。TEST_VERDICT コメントの有無・内容・
  stale/SKIP 状態は、他の authoritative evidence が揃っていれば APPROVE を妨げず、
  他の authoritative evidence が揃っていなければ APPROVE を与えない。

## Canonical Runtime Acceptance vs Fixture-Only Evidence（Issue #2807）

AC が **actual / canonical / default runtime selection**（例: current-head production launcher の通常 binary resolution で選択される proxy identity）を要求する場合、レビュアーは AC の evidence-source requirement（actual/canonical vs fixture）と、割り当てられた VC / test implementation が実際に生成する evidence source を照合する。両者が一致しない場合（fixture-only VC が actual-runtime AC に割り当てられている場合）は `REQUEST_CHANGES` とする。

**PR body の `[x]` チェック、Safety Claim、self-report、および fixture-only test の PASS だけでは、actual/canonical runtime を要求する AC を APPROVE する根拠にならない。** fixture PASS + real smoke SKIP / environment_blocked の組み合わせは、actual-runtime AC の充足として不十分である（#2801 / PR #2802 で観測した failure class: actual-runtime AC + fixture-only PASS + real smoke SKIP のまま AC を `[x]` にして APPROVE した旧 iteration 1 相当の判断は、本ポリシー適用後は根拠を持たない）。

**canonical runtime acceptance evidence の必須フィールド**（本ポリシーの evidence 要件。`.claude/skills/create-issue/references/body-authoring.md` の同名ガイダンスと同一）:

- `run_head_sha`
- `git_dirty`
- 実行 command identity（実際に起動したコマンド文字列 / invocation）
- launcher hash
- selected proxy の absolute path / version / hash

fixture proxy の path/version のみの evidence は、この evidence 要件を **充足しない**（fixture proxy の path/version のみを記載した evidence はこの AC の充足として明示的に不十分と扱う）。

canonical smoke（current-head production launcher を fake proxy override なしで external process 起動した結果。例: `scripts/claude-gpt/launch.sh --check-only`）が `cause: proxy_model_catalog_incompatible` または non-zero exit を返した場合、同一 head の fixture compatibility tests（例: `scripts/claude-gpt/tests/test_proxy_model_compatibility.py`）が全て PASS であっても、actual-runtime AC を PASS / ready-for-merge に **昇格させない**。

既存の fixture tests（`scripts/claude-gpt/tests/test_proxy_model_compatibility.py` 等）は hermetic implementation-semantics coverage としてそのまま維持し、廃止・改変しない。通常 CI は real ChatGPT account / network を必須にしない。

canonical runtime evidence の取得には既存の `scripts/claude-gpt/launch.sh --check-only` および `scripts/claude-gpt/runtime_smoke_test.sh` を再利用する。本ポリシーは新しい permanent daemon、generic runtime harness、network-required merge gate の追加を要求しない（既存 runtime verification assets への参照のみ）。
