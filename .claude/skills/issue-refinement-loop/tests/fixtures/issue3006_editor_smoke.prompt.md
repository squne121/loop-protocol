# Issue #3006 — native local-only editor smoke

これは clean implementation HEAD での実 Claude Code 検証です。GitHub Issue や PR の変更、`gh issue edit`、controlled executor の実行は禁止します。追跡対象ファイルは変更せず、書き込みはこの worktree 内の `artifacts/` に限ります。権限の bypass / 設定やランチャーの変更は禁止です。できない操作は実行したと偽らず、阻害理由を報告してください。

あなたは親 session です。最初に Read tool で `.claude/skills/edit-issue/SKILL.md` を読み、実際の `Agent` tool で **既存の issue-editor** を 1 回呼んでください（架空の editor や自己申告では不可）。子に以下の作業をそのまま伝えます。

1. preload された実 `edit-issue` Skill の手順と `.github/ISSUE_TEMPLATE/implementation.yml` を確認する。実 `Read` tool event を残す。
2. 作業領域は `artifacts/issue3006-editor-native-<UTC時刻とランダム識別子>/` の新しい空 directory のみとする。Python のテスト fixture `.claude/skills/issue-refinement-loop/tests/test_pre_edit_validation_parity.py` の `_body(_template_stop_items()[:-1])` と同等の本文を local candidate file に保存する（実際に template を読み、標準6項目の末尾1件だけを抜き、fixture-specific Stop Condition は維持）。GitHub を参照しない。
3. 変更候補に実 Bash tool から `uv run --locked python3 .claude/skills/edit-issue/scripts/guard-issue-body.py <candidate> --format json` を実行し、exit 2 / `issue_body_validation.errors[].rule_id: LP017` を確認する。同じ候補で `contract_readiness_check.py --mode preflight-static --body-file <candidate>` が `go` になることを確認し、この go を edit-ready と混同しない。
4. LP017 の文言先頭3件ではなく current template から不足した標準項目のみを候補ファイルへ補い、独自条件を消さずに同じ full guard を再実行し exit 0 / `all_passed: true` と static readiness `go` を確認する。各手順の Bash tool events を残す。
5. 最後に検証した同じ候補 file を `ISSUE_EDIT_TXN_INPUT_V1.new_body_file` に指定した local input JSON を artifacts に作る。**実 transaction helper は呼ばない**。代わりに Python 内で `edit_issue_txn.py` の `run_transaction` を import し、`_fetch_issue` を synthetic existing body / final readback の in-process stub、`_invoke_controlled_exec` を command id 記録だけの in-process mock に置換して呼ぶ。mock が `issue_content.update` 1 回に到達し、`status: ok`、`mutation_started: true` であること、直前に guard を通した candidate file の SHA-256 と input JSON が指す file の SHA-256 が同じであり処理後も変化しないことを確認する。raw body、secret は出力しない。
6. 親に worktree-local artifact path と redacted event 順序・候補/handoff SHA・full guard/readiness の exit code・mock transaction 結果を返す。`ISSUE_AUTHOR_RESULT_COMPACT_V1` の `STATUS / SUMMARY / BODY_HASH / COMMENT_URL / ARTIFACT / NEXT_ACTION` 6 field を必ず出す。COMMENT_URL は GitHub mutation をしないため null。

親 session でも同一 agent_id の SubagentStart / SubagentStop と子 transcript path の実観測を確認してください。親の Auto mode classifier に対して安全な local `Read` と、`artifacts/` 内の無害な新規 probe file を作る mutation-capable tool call の実 permission decision をそれぞれ観測してください。deny が観測できない場合、permission_mode の表示だけから拒否を推測せず `AC7=UNVERIFIED` として報告してください。親から別の SubAgent を起動しないでください。結果は証跡に基づき簡潔に返し、未観測の PASS を宣言しないでください。
