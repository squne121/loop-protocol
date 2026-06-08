# Step 4: PR Review

Step 2 が `PASS` / `PARTIAL` で完了したら、`pr-reviewer` SubAgent に PR レビューを委譲する。Step 2 が `FAIL` の場合は本ステップをスキップして Step 5 に直行（REQUEST_CHANGES 確定）。

Codex CLI: spawn the custom agent named pr-reviewer for this step; the root thread must not edit files, run tests, commit, push, or make the review judgment directly.

## 委譲呼び出し

```
subagent_type: pr-reviewer
inputs:
  pr_number: <Step 1 で取得した PR 番号>
  reviewed_head_sha: <現在の HEAD SHA>
```

SubAgent 側は `.claude/skills/pr-review-judge/SKILL.md` の手順を実行し、verdict コメントを PR に投稿する。

## 期待する出力

pr-reviewer が `gh pr review --comment` で投稿する verdict コメントに含まれる `LOOP_VERDICT` YAML:

```yaml
LOOP_VERDICT:
  verdict: APPROVE | REQUEST_CHANGES
  blockers: []
  mergeable: MERGEABLE | CONFLICTING | UNKNOWN
  mergeStateStatus: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  reviewed_head_sha: <SHA>
```

## 判定

orchestrator は LOOP_VERDICT YAML を読み取り、次ステップを決定する:

| verdict | 次アクション |
|---|---|
| `APPROVE` | LOOP_STATE.termination_reason = "approved" を立て、Step 5 で終了処理 |
| `REQUEST_CHANGES` | blockers を LOOP_STATE.blockers_history に追加、Step 5 で iteration 判定 |

LOOP_VERDICT の YAML 解析方法は `step-5-mergeability-handling.md` を参照（最新コメントの抽出ルール含む）。

## reviewed_head_sha 整合チェック

LOOP_VERDICT に含まれる `reviewed_head_sha` が現在の PR head SHA と一致しない場合、pr-reviewer は古い head をレビューしている可能性がある:

```bash
CURRENT_HEAD=$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)
```

不一致 → orchestrator は `LOOP_STATE.blockers_history` に "stale review on $REVIEWED_SHA vs current $CURRENT_HEAD" を記録し、Step 4 を再委譲（最新 head での再レビュー）。

## 出力

LOOP_STATE.last_step = "pr_review" に更新、LOOP_STATE.last_loop_verdict に APPROVE / REQUEST_CHANGES を記録、Step 5 へ進む。
