# Issue #2230 相当プロンプト（Issue #2278 issue_to_impl runtime smoke fixture）

このプロンプトは `scripts/claude-gpt/runtime_smoke_test.sh --scenario issue_to_impl`
が起動する live Claude Code セッションへそのまま渡される固定 fixture である。
`gh` は fake provider（`scripts/claude-gpt/tests/fixtures/fake_gh.py`）へ
PATH shadow 済みのため、実 GitHub への到達は一切発生しない。

## 指示

1. Bash tool で次のコマンドを実行し、fixture Issue（#9100 相当、repo
   squne121/loop-protocol）を読み取る:

   ```
   gh issue view 9100 --repo squne121/loop-protocol --json title,body,labels,comments
   ```

2. 返ってきた `body` フィールドに、次の 5 セクション見出しがすべて含まれているかを
   確認する（`issue-contract-review` skill が着手前に確認する契約セクションと同じ
   集合）:

   - `## Outcome`
   - `## Acceptance Criteria`
   - `## Verification Commands`
   - `## Allowed Paths`
   - `## Stop Conditions`

3. 上記 5 セクションがすべて存在すれば `contract_complete=true`、1 つでも
   欠けていれば `contract_complete=false` とする。

4. 他の GitHub read/write 操作、git worktree 作成、PR 作成、ファイル編集は
   一切行わない（本プロンプトは fresh review の live liveness 確認のみを
   目的とする薄いプローブであり、impl-review-loop 本体の実行や実装作業は
   別スコープである）。

5. 最後に、他の文字列を一切付け足さず、次の 1 行だけを出力して終了する
   （deterministic marker）:

   ```
   ISSUE_TO_IMPL_FRESH_REVIEW_OK issue_number=9100 contract_complete=<true|false>
   ```

   `<true|false>` は手順3の判定結果に置き換えること。
