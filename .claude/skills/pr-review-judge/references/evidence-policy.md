# PR Evidence Policy (PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY)

優先順位:

1. TEST_VERDICT_MACHINE（最上位）
2. CI_CHECK_RUN_SCOPED（head_sha/workflow/job/step/command 一致条件）
3. PR_BODY_SELF_REPORT（補助のみ）

### テスト証跡のルール

- PR本文の自己申告のみでは APPROVE 不可。
- TEST_VERDICT_MACHINE / CI_CHECK_RUN_SCOPED がなければ `REQUEST_CHANGES`。
- `skipped / fallback PASS / exit 77 / SKIP:` は `required pass` として扱わない。
- TEST_VERDICT の `head_sha` が PR head と不一致なら stale blocker。

### APPROVE 禁止条件（要約）

- `verification_skipped_count > 0`
- `SKIP:` / `exit 77`
- `_*_fallback: true`
- fallback 成功を PASS として扱う
- `head_sha` stale
