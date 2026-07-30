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
