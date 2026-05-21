# Step 5: 判定 / 終了 / フィードバック循環

Step 2-4 の結果を統合して、ループを次イテレーションに進めるか終了するかを判定する。

## 終了条件マトリクス

| 条件 | アクション |
|---|---|
| Step 4 で `LOOP_VERDICT.verdict: APPROVE` | `termination_reason: approved` を立て、終了処理へ |
| `LOOP_STATE.iteration >= LOOP_STATE.max_iterations` | `termination_reason: max_iterations` を立て、fail-close で人間判断 |
| Step 1-2-4 のいずれかで `human_review_required: true` を SubAgent が返した | `termination_reason: human_escalation` を立て、即停止 |
| Step 2 が `FAIL` または Step 4 が `REQUEST_CHANGES` で iteration 余裕あり | LOOP_STATE.iteration += 1、Step 1 に戻る（fix_delta を渡す）|

## REQUEST_CHANGES 時の fix_delta 構築

LOOP_VERDICT.blockers と TEST_VERDICT の失敗内容から fix_delta を生成し、Step 1 の implementation-worker に渡す:

```yaml
fix_delta:
  iteration: <次の iteration 番号>
  blockers:
    - "<LOOP_VERDICT.blockers から抽出>"
  test_failures:
    - "<TEST_VERDICT.result が FAIL の場合の失敗詳細>"
  pr_review_comment_url: <pr-reviewer が投稿した verdict コメントの URL>
```

## 終了処理（approved）

```bash
# LOOP_STATE を最終 YAML として会話履歴に記録
# PR は人間がマージ判断（orchestrator はマージしない）

# Issue コメントで終了報告（機械可読フィールドを含む）
gh issue comment <issue_number> --body "## impl-review-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: APPROVE
- PR: <PR URL>
- 次アクション: 人間レビュー → マージ → post-merge-cleanup

\`\`\`yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  follow_up_issues:
    - request_dedupe_key: \"...\"
      issue_number: 123
      issue_url: \"https://github.com/...\"
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed

  note_only_observations:
    - dedupe_key: \"...\"
      source_url: \"...\"
      source_note_id: \"...\"
      summary: \"...\"
\`\`\`"
```

### APPROVE 時の follow-up Issue 自動起票

`LOOP_VERDICT.follow_up_issue_requests` が空でない場合、main thread は APPROVE 確定直後に各リクエストを `issue-author` SubAgent に委譲して `create-issue` 経由で **即時自動起票** する。

**mandatory_follow_up の処理タイミング**: `severity: mandatory_follow_up` のリクエストは APPROVE 確定**前**に create/reuse する。未 materialize の状態で APPROVE してはならない。

pr-review-judge が `LOOP_VERDICT` の `follow_up_issue_requests` フィールドに格納した non-blocker NOTE（任意改善提案・観察事項）が起票対象となる。詳細スキーマは `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_ISSUE_REQUEST_V1` を参照。

```
for each req in LOOP_VERDICT.follow_up_issue_requests:
  - severity: mandatory_follow_up → APPROVE 前に必ず起票（dedupe_key チェック後）
  - severity: optional_follow_up → APPROVE 後に dedupe_key チェック後、重複なければ起票
  - severity: note_only → 起票せず、終了報告コメントの note_only_observations に記録

  dedupe チェック（severity: mandatory_follow_up / optional_follow_up）:
    gh issue list --repo squne121/loop-protocol --state all \
      --search '"<req.dedupe_key>"' --json number,title,url,state,stateReason,labels
    重複あり（open）→ スキップ（既存 Issue 番号を記録、status: reused_open）
    重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
    重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
    重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
    重複なし → 起票（## Source セクションに dedupe_key を含める）
    ※ closed Issue を open に差し戻す場合は human escalation が必要（自動起票不可）
```

起票・スキップした follow-up Issue の情報を終了報告コメントの `follow_up_issues` フィールドに列挙する。

## 終了処理（max_iterations）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: max_iterations 到達 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 上限 iteration: <max_iterations>
- 最終 blockers: <LOOP_STATE.blockers_history の最新>
- PR: <PR URL>
- 人間判断を仰ぎます: 追加 iteration を許可するか、別アプローチを検討するか"
```

## 終了処理（human_escalation）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: 人間判断要請 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 発生 step: <last_step>
- 詳細: <SubAgent が返した human_review_required の理由>
- PR: <PR URL>
- 人間の確認後、ループ再開または別アプローチを選択してください"
```

## Output

各終了条件に応じた LOOP_STATE 最終 YAML を会話履歴に記録する。

```yaml
LOOP_STATE:
  ...（全フィールド）
  iteration: <最終 iteration 数>
  last_step: judgment
  termination_reason: approved | max_iterations | human_escalation
```

その後、orchestrator は次のユーザー入力を待つ（自動で次イテレーションに進む決定済みなら Step 1 を再呼び出し）。
