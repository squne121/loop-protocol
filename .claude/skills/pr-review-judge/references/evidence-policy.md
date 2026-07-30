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

### テスト証跡のルール

- PR本文の自己申告のみでは APPROVE 不可（変更なし）。
- `CI_CHECK_RUN_SCOPED` または「exact head SHA + literal command SHA256 に束縛された独立実行 Issue VC」のいずれもなければ `REQUEST_CHANGES`。
- `CI_CHECK_RUN_SCOPED` の missing / skipped / neutral / cancelled / stale-head / unknown-classification は、Phase 1 変更後も引き続き fail-closed（`REQUEST_CHANGES`）である（`ci_verdict_summary_v2.py` の既存実装を参照）。
- `skipped / fallback PASS / exit 77 / SKIP:` は `required pass` として扱わない（変更なし）。
- `head_sha` が PR head と不一致（stale）なら fail-closed blocker（変更なし）。
- `TEST_VERDICT_MACHINE` は advisory として参照してよいが、APPROVE の必須条件にも REQUEST_CHANGES 回避の根拠にもしない。

### APPROVE 禁止条件（要約）

- `verification_skipped_count > 0`
- `SKIP:` / `exit 77`
- `_*_fallback: true`
- fallback 成功を PASS として扱う
- `head_sha` stale
- `CI_CHECK_RUN_SCOPED` が missing / skipped / neutral / cancelled / stale-head / unknown-classification
- authoritative evidence（`CI_CHECK_RUN_SCOPED` または束縛済み独立実行 Issue VC）が一つも無い
