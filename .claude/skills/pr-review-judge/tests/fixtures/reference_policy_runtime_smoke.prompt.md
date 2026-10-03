# reference policy runtime smoke 固定試験 prompt

これは Issue #2878 の AC9（`pr-review-judge` の Step 1 が `Closes` 不在だけで `REQUEST_CHANGES` とせず、`Refs` から linked Issue を特定する）を実 runtime で確認するための、公開用の固定試験入力です。入力はすべて合成データであり、GitHub への読み取り・書き込み（`gh` の実行を含む）、ファイル編集、外部通信は一切行いません。次の手順を、記載した順序のまま実行してください。

1. `Agent` tool を 1 回だけ呼び出します。`subagent_type` は `pr-reviewer`、`run_in_background` は `false` を指定します。子 SubAgent への依頼は、下の「子 SubAgent への依頼」節の本文をそのまま渡します。
2. 子 SubAgent の完了結果が親 session に届くまで待ちます。結果が届く前に最終回答を出してはいけません。
3. 最終回答は、次の 3 行を、この順序で、それぞれ単独の行として出力し、続けて ```json フェンス付きコードブロックを 1 個だけ出力します。コードブロックの中身は子 SubAgent が返した JSON object をそのまま転記したものにします。
   - `REFERENCE_POLICY_STEP1_LINKED_ISSUE_RESOLVED_FROM_REFS`
   - `REFERENCE_POLICY_STEP_AC_EVIDENCE_EVALUATED`
   - `REFERENCE_POLICY_STEP_VERDICT_EMITTED`

## 子 SubAgent への依頼

あなたは `pr-review-judge` skill の Procedure（Step 1 の linked Issue 特定 → AC / evidence の評価 → verdict 出力）を、下の合成入力に対して宣言順に実行します。`Bash` / `gh` / `Read` による外部取得は行わず、下の合成入力だけを使ってください。GitHub への書き込みは行いません。

合成入力:

- PR 番号 900001、reviewed_head_sha は `1111111111111111111111111111111111111111`
- PR 本文の reference 行は `Refs #900002` のみで、`Closes` 系の closing keyword はありません。
- linked Issue #900002 の Runtime Verification Applicability は `decision: deferred`、`destination_type: phase`、`destination_ref: post-merge-live-evidence` です（post-merge の live evidence 待ちで、merge で close してはならない Issue です）。
- Issue の Acceptance Criteria は AC1「reference policy を単一 evaluator に集約する」の 1 件で、PR 本文の `## 受け入れ条件の達成状況` に `[x] AC1: 達成（合成 fixture の根拠あり）` と記載されています。Allowed Paths 逸脱はなく、検証コマンド結果は PASS として記載されています。
- Step 1 の reference authority entrypoint を合成入力に対して実行した結果（この JSON をそのまま entrypoint の出力として扱います）:
  `{"decision": "nonclosing_required", "level": "A2", "reason_code": "a2_contract_deferred", "repo": "squne121/loop-protocol", "issue_number": 900002, "pr_number": 900001, "pr_body_sha256": "0000000000000000000000000000000000000000000000000000000000000000", "effective_kind": "non-closing", "body_verdict": "valid", "body_reason": "ok"}`

手順:

1. Step 1: 上の entrypoint 結果に従い、linked Issue を `Refs` から #900002 と特定します（`Closes` 不在だけを理由に `REQUEST_CHANGES` にはしません）。完了したら、単独の行として `REFERENCE_POLICY_STEP1_LINKED_ISSUE_RESOLVED_FROM_REFS` を出力します。
2. AC / evidence の評価: Refs 経由でも Issue contract の AC を通常どおり評価します。完了したら、単独の行として `REFERENCE_POLICY_STEP_AC_EVIDENCE_EVALUATED` を出力します。
3. verdict 出力: 完了したら、単独の行として `REFERENCE_POLICY_STEP_VERDICT_EMITTED` を出力し、その後に次の key だけを持つ JSON object を ```json フェンス付きコードブロックで 1 個だけ返します。
   - `verdict`: `APPROVE` / `REQUEST_CHANGES` / `HUMAN_REVIEW_REQUIRED` のいずれか（合成入力の結論）
   - `reviewed_head_sha`: 上記の 40 桁 hex
   - `blockers`: 文字列の配列（blocker が無ければ空配列）
   - `warnings`: 文字列の配列（無ければ空配列）
   - `linked_issue_resolution`: `source`（`refs` または `closes`）、`issue_number`（整数）、`decision`（entrypoint の `decision`）、`body_verdict`（entrypoint の `body_verdict`）を持つ object
   - `smoke_marker`: 文字列 `REFERENCE_POLICY_SMOKE_OK`
